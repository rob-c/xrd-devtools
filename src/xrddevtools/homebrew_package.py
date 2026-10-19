"""Generate hash-verified, platform-specific wheel bundles and tap formulae.

This is the macOS counterpart of :mod:`xrddevtools.distribution_package`: where that
tool turns a resolved wheelhouse into RPM/DEB packages, this one turns it into
one ``.tar.gz`` of wheels per client (xrdclient, xgfalclient and, when its
wheel is in the wheelhouse, xrdarc) plus a Homebrew formula that installs
those wheels, offline, into a private virtualenv. Each bundle carries exactly
its client's dependency closure (:func:`~xrddevtools.distribution_package.closure`)
- the xrdcore, xrdsec and xrdhttp library wheels included - so no formula
needs another formula, or PyPI, at install time. xrdclient's
``docs/platforms.md`` (*Homebrew*) is the operator's guide and this
repository's ``.github/workflows/platforms.yml`` the CI job that runs it on
both macOS architectures.

Why wheels rather than a source formula: Homebrew's Python formulae normally
list every dependency as a ``resource`` and build it from source. The clients
promise a compiler-free install, and a native wheel (``cffi`` and friends)
matches exactly one CPU and one interpreter minor, so the bundle carries the
exact wheels CI resolved and tested, and the formula refuses to reach PyPI
(``PIP_NO_INDEX``) or to fall back to a source build (``PIP_ONLY_BINARY``).
That is also why the archive name is tagged with interpreter, platform and
revision: an Intel bundle must never be mistaken for an Apple Silicon one.

What a maintainer must not break: the SHA-256 in the formula is computed over
the archive bytes actually written, so the archive must not be rebuilt or
recompressed after this tool runs; upload it unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sysconfig
import tarfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xrddevtools.distribution_package import CLIENTS, closure, dist_name, manifest, wheel_info

#: Each client's command whose ``--help`` report ``brew test`` checks.
REPORT_COMMANDS = {"xrdclient": "xrd-cp", "xgfalclient": "gfal-copy", "xrdarc": "xrdarc"}
#: Each client's local-copy command, for the byte-preservation ``brew test``;
#: xrdarc submits jobs and has none.
COPY_COMMANDS = {"xrdclient": "xrd-cp", "xgfalclient": "gfal-copy"}


@dataclass(frozen=True, slots=True)
class Release:
    """Where and how one round of bundles is published; shared by every client.

    ``python`` is the Brew interpreter's ``major.minor`` the wheelhouse was
    resolved with (it names the ``python@X.Y`` formula dependency and the
    ``cpXY`` archive tag). ``base_url`` is the HTTPS directory the archives
    will be uploaded to, or ``None`` to point the formula at the local file
    for a trial ``brew install``. ``revision`` is Homebrew's counter for a
    rebuild that changes only dependencies, so users are offered the
    upgrade even though the client version is unchanged.
    """

    python: str
    base_url: str | None
    revision: int = 0


def formula(info: dict[str, Any], url: str, digest: str, python: str) -> str:
    """The Ruby formula for one client's bundle at ``url`` with SHA-256 ``digest``.

    The install block splits the wheelhouse into the client's own wheel and
    its dependencies: ``pip_install_and_link`` would otherwise link every
    dependency's console scripts into Homebrew's ``bin`` as well. The test
    block (:func:`formula_test`) is what ``brew test`` runs on a user's
    machine.
    """
    name = info["name"]
    return f'''class {name.capitalize()} < Formula
  include Language::Python::Virtualenv
  desc "Python storage client with an isolated wheel runtime"
  homepage "https://github.com/rob-c/{name}"
  url {json.dumps(url)}
  sha256 "{digest}"
  version "{info["version"]}"
  revision {info.get("revision", 0)}
  license "LGPL-3.0-or-later"
  depends_on "python@{python}"

  def install
    ENV["PIP_NO_INDEX"] = "1"
    ENV["PIP_ONLY_BINARY"] = ":all:"
    python = (Formula["python@{python}"].opt_bin/"python{python}").to_s
    venv = virtualenv_create(libexec, python, system_site_packages: false)
    wheels = Pathname.glob("wheelhouse/*.whl")
    own, deps = wheels.partition {{ |w| w.basename.to_s.start_with?("{name}-") }}
    venv.pip_install deps
    venv.pip_install_and_link own
  end

{formula_test(name)}end
'''


def formula_test(name: str) -> str:
    """The formula's ``test do`` block: the two things a broken bundle would get wrong.

    The CLI starts and reports in the shared ``storage-client-report``
    schema, and - for a client with a copy command - a real copy preserves
    bytes.
    """
    command = REPORT_COMMANDS[name]
    copy = COPY_COMMANDS.get(name)
    copy_test = (
        f'''    (testpath/"source").write("physics data")
    system bin/"{copy}", "source", "dest"
    assert_equal "physics data", (testpath/"dest").read
'''
        if copy
        else ""
    )
    return f"""  test do
    require "json"
    report = JSON.parse(shell_output("#{{bin}}/{command} --output-format json --help"))
    assert_equal "storage-client-report", report["schema"]
{copy_test}  end
"""


def bundle(wheelhouse: Path, output: Path, name: str, release: Release) -> None:
    """Write ``name``'s wheel archive, its manifest and its formula into ``output``.

    The wheelhouse holds the resolved runtime of every client; the bundle
    keeps ``name``'s dependency closure from it, so xrdclient's leaves out
    the xgfalclient and xrdarc wheels while xgfalclient's carries xrdclient
    and the libraries - a Homebrew formula gets its own virtualenv and cannot
    share another's the way the RPM/DEB packages share
    ``/opt/storage-clients``. Raises :class:`StopIteration` if the wheelhouse
    lacks ``name``'s own wheel, and :class:`ValueError` (from
    :func:`~xrddevtools.distribution_package.wheel_info`) for a non-release version.
    """
    wheels = closure(sorted(wheelhouse.glob("*.whl")), name)
    info = wheel_info(next(w for w in wheels if dist_name(w) == name))
    info["revision"] = release.revision
    # The host's platform tag (e.g. macosx-14.0-arm64) is the bundle's: the
    # wheelhouse was resolved on this machine, for this architecture.
    tag = f"cp{release.python.replace('.', '')}-{sysconfig.get_platform()}"
    archive = output / f"{name}-{info['version']}-{tag}-r{release.revision}-wheels.tar.gz"
    # Keep the archive flat at its root, so Homebrew doesn't strip wheelhouse/.
    manifest_file = output / f"{name}-manifest.json"
    manifest_file.write_text(json.dumps(manifest(wheels), indent=2) + "\n", encoding="utf-8")
    with tarfile.open(archive, "w:gz") as target:
        target.add(manifest_file, arcname="wheel-manifest.json")
        for wheel in wheels:
            target.add(wheel, arcname="wheelhouse/" + wheel.name)
    # Hash only after the archive is closed: these are the bytes users fetch.
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if release.base_url:
        url = release.base_url.rstrip("/") + "/" + archive.name
    else:
        url = archive.resolve().as_uri()
    (output / f"{name}.rb").write_text(formula(info, url, digest, release.python), encoding="utf-8")


def clients_in(wheelhouse: Path) -> list[str]:
    """The clients that have a wheel in ``wheelhouse``, in :data:`CLIENTS` order."""
    present = {dist_name(w) for w in wheelhouse.glob("*.whl")}
    return [client for client in CLIENTS if client in present]


def main(argv: Sequence[str] | None = None) -> int:
    """Validate the command line, then bundle each client in the wheelhouse into ``--output``.

    ``--output`` must not exist yet, so a re-run can never mix archives (and
    hashes) from two different wheelhouses. A published base URL must be
    HTTPS because Homebrew downloads the archive at install time and only
    the formula's checksum stands between that download and the user.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python-version", default="3.14")
    parser.add_argument("--base-url", help="HTTPS location where the bundles will be published")
    parser.add_argument(
        "--revision", type=int, default=0, help="increment for dependency-only rebuilds"
    )
    args = parser.parse_args(argv)
    if args.base_url and not args.base_url.startswith("https://"):
        parser.error("Published bundles need an https:// base URL")
    if args.revision < 0:
        parser.error("--revision must be zero or a positive integer")
    clients = clients_in(args.wheelhouse)
    if not clients:
        parser.error(f"{args.wheelhouse} holds no client wheel ({', '.join(CLIENTS)})")
    args.output.mkdir(parents=True, exist_ok=False)
    release = Release(args.python_version, args.base_url, args.revision)
    for name in clients:
        bundle(args.wheelhouse.resolve(), args.output, name, release)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
