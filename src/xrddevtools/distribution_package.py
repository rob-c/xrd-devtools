#!/usr/bin/env python3
"""Build private, offline RPM/DEB runtimes from an already resolved wheelhouse.

Since 0.4.0 the storage clients are built on three libraries (xrdcore,
xrdsec, xrdhttp), so the shared runtime is owned by the lowest layer, not by
a client: the ``storage-clients-libs`` package (:data:`RUNTIME`) holds those
three library wheels and every third-party wheel, and each client package
(xrdclient, xgfalclient and, when its wheel is in the wheelhouse, xrdarc)
adds only its own wheel. Every client package has an exact package-manager
dependency on that runtime release, and xgfalclient additionally on exactly
the xrdclient release it was tested with. These are deployment bundles, not
Fedora/Debian archive submissions.

Why a separate runtime package rather than xrdclient owning it (as it did
before 0.4.0): xrdarc depends on the libraries but on neither storage client,
so it must be installable without xrdclient; and the libraries are the one
thing every client package really shares.

Where it runs: inside the disposable distribution containers driven by
:mod:`xrddevtools.platforms` (``packaging/linux.sh`` runs this file after resolving the
wheelhouse with ``pip download --only-binary=:all:``), once per target OS,
because the snapshot contains native wheels built against that OS's glibc.
xrdclient's ``docs/platforms.md`` (*RPM and DEB deployment bundles*) is the
operator's guide; ``tests/test_distribution_tools.py`` pins the contracts
below. It imports nothing but the standard library, because the build
interpreter in the container runs it as a plain file.

The layout every package shares:

* ``/opt/storage-clients`` (:data:`PREFIX`) is a ``pip --prefix`` tree that
  the runtime package owns; each client installs only its own wheel into it.
* ``/usr/bin/<command>`` are symlinks to ``PREFIX/bin/<command>``, which are
  tiny ``sh`` wrappers that run the system interpreter with ``-I -S``
  (isolated mode, no site-packages) on :data:`LAUNCHER`. A user's
  ``PYTHONPATH``, ``~/.local`` or a distro ``python3-urllib3`` therefore
  cannot shadow the tested snapshot.
* ``/usr/share/doc/<name>/wheel-manifest.json`` lists every wheel and its
  SHA-256, so an operator can tell which dependency versions a host runs
  when a security advisory lands (the bundle must then be rebuilt).

What a maintainer must not break: the *exact* ``name = version-release``
package-manager dependencies (:func:`plan`). All packages share one
site-packages; any other runtime or xrdclient release would be untested
against the client that requires it. The runtime package's version is
xrdcore's: a rebuild that changes another library or a third-party wheel
without changing xrdcore's version must increment ``--release`` for every
package, exactly like any other dependency-only rebuild.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Sequence
from email.message import Message
from email.parser import Parser
from pathlib import Path
from typing import Any

#: The shared runtime root every package installs under.
PREFIX = Path("/opt/storage-clients")
#: The package that owns :data:`PREFIX`, :data:`LAUNCHER`, the libraries and
#: every third-party wheel.
RUNTIME = "storage-clients-libs"
#: The library distributions the runtime package must contain, lowest first;
#: the first one's version is the runtime package's version.
LIBRARIES = ("xrdcore", "xrdsec", "xrdhttp")
#: Every client package this tool can build, with the other client packages
#: it requires exactly (beyond the runtime). xrdarc depends on no client.
CLIENTS = {"xrdclient": (), "xgfalclient": ("xrdclient",), "xrdarc": ()}
#: The one-line summary of each kind of package.
SUMMARIES = {
    RUNTIME: "Shared Python libraries and runtime for the storage clients",
    "client": "Python storage client with an isolated offline runtime",
}
#: ``PREFIX/launch.py``, the single entry point every command wrapper execs.
#: Run with ``-I -S``, so it must put the snapshot on ``sys.path`` itself: the
#: ``lib``/``lib64`` pair covers pip's purelib and platlib on Red Hat-style
#: layouts as well as Debian's. A command is found by the name it was invoked
#: under (``$0``) among the snapshot's ``console_scripts`` - with ``-I -S``
#: the snapshot holds the only distributions on the path - so neither a new
#: command nor a new client package needs a launcher change. ``--smoke
#: SCRIPT`` runs an arbitrary file inside the same isolated runtime;
#: ``linux.sh`` uses it to run the installed-package smoke test against
#: exactly what the packages installed. ``{minor}`` is filled with the target
#: interpreter's ``X.Y``.
LAUNCHER = """import importlib.metadata as metadata
import pathlib
import runpy
import sys
root = pathlib.Path(__file__).parent
sys.path[:0] = [str(root / lib / "python{minor}" / "site-packages") for lib in ("lib", "lib64")]
if sys.argv[1] == "--smoke":
    script = sys.argv.pop(2)
    sys.argv.pop(1)
    runpy.run_path(script, run_name="__main__")
else:
    name = pathlib.Path(sys.argv.pop(1)).name
    for entry in metadata.entry_points(group="console_scripts", name=name):
        sys.exit(entry.load()())
    sys.exit("Unknown storage-client command: " + name)
"""

#: A requirement's distribution name and optional ``[extras]``.
_REQUIREMENT = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?")
#: The extra a ``Requires-Dist`` marker makes the requirement conditional on.
_EXTRA = re.compile(r"""extra\s*==\s*["']([^"']+)["']""")


def canonical(name: str) -> str:
    """``name`` normalised as PEP 503 does, so ``Python_Dateutil`` finds ``python-dateutil``."""
    return re.sub(r"[-_.]+", "-", name).lower()


def dist_name(wheel: Path) -> str:
    """The canonical distribution name of ``wheel``, from its filename (PEP 427)."""
    return canonical(wheel.name.split("-", 1)[0])


def wheel_metadata(wheel: Path) -> tuple[Message, list[str]]:
    """``wheel``'s parsed ``METADATA`` and its console-script names (possibly none).

    Read from the wheel itself rather than guessed from its filename. A
    library wheel has no ``entry_points.txt`` at all, which is not an error.
    """
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        member = next(n for n in names if n.endswith(".dist-info/METADATA"))
        meta = Parser().parsestr(archive.read(member).decode("utf-8"))
        entries = configparser.ConfigParser()
        points = member.replace("METADATA", "entry_points.txt")
        if points in names:
            entries.read_string(archive.read(points).decode("utf-8"))
    scripts = list(entries["console_scripts"]) if entries.has_section("console_scripts") else []
    return meta, scripts


def wheel_info(wheel: Path) -> dict[str, Any]:
    """The name, version and console-script names declared inside ``wheel``.

    Only a plain ``major.minor.patch`` version is accepted (:class:`ValueError`
    otherwise): RPM and Debian order ``rc``/``dev`` suffixes differently from
    PEP 440, so a pre-release package could sort *after* the final one and
    never upgrade.
    """
    meta, scripts = wheel_metadata(wheel)
    version = meta["Version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Deployment bundles require a numeric major.minor.patch release version")
    return {"name": meta["Name"], "version": version, "scripts": scripts}


def requirement(line: str) -> tuple[str, frozenset[str], str | None]:
    """A ``Requires-Dist`` line as (canonical name, extras asked for, extra it needs).

    The last item is the parent extra the requirement is conditional on
    (``; extra == "fast"``), or ``None`` for an unconditional one. Other
    markers are not evaluated: :func:`closure` relies on the wheelhouse,
    which ``pip download`` resolved for this very platform, holding exactly
    the wheels whose markers were true.
    """
    spec, _, marker = line.partition(";")
    found = _REQUIREMENT.match(spec)
    if found is None:
        raise ValueError(f"unreadable requirement: {line!r}")
    extras = frozenset(e.strip() for e in (found.group(2) or "").split(",") if e.strip())
    needed = _EXTRA.search(marker)
    return canonical(found.group(1)), extras, needed.group(1) if needed else None


def closure(wheels: list[Path], root: str) -> list[Path]:
    """The wheels in ``wheels`` that ``root``'s default install needs, ``root``'s own included.

    Followed through ``Requires-Dist``, with extras a requirement asks for
    (``xrdcore[fast]``) followed too. A requirement with no wheel here is
    skipped: its marker was false on this platform, or ``pip install
    --no-index`` will say which wheel is missing.
    """
    named = {dist_name(w): w for w in wheels}
    seen: set[tuple[str, frozenset[str]]] = set()
    pending = [(canonical(root), frozenset[str]())]
    while pending:
        name, extras = pending.pop()
        if (name, extras) in seen or name not in named:
            continue
        seen.add((name, extras))
        pending += _needed(named[name], extras)
    return sorted({named[name] for name, _ in seen})


def _needed(wheel: Path, extras: frozenset[str]) -> list[tuple[str, frozenset[str]]]:
    """What installing ``wheel`` with ``extras`` asks for: each requirement and its own extras.

    A requirement conditional on an extra counts only when that extra is
    among ``extras``; every other marker is left to the wheelhouse (see
    :func:`requirement`).
    """
    lines = wheel_metadata(wheel)[0].get_all("Requires-Dist") or []
    parsed = [requirement(line) for line in lines]
    return [(name, wanted) for name, wanted, needs in parsed if needs is None or needs in extras]


def manifest(wheels: list[Path]) -> list[dict[str, Any]]:
    """Each wheel's filename and SHA-256: the inventory shipped in every package."""
    return [
        {"filename": w.name, "sha256": hashlib.sha256(w.read_bytes()).hexdigest()} for w in wheels
    ]


def plan(wheels: list[Path]) -> list[dict[str, Any]]:
    """The packages to build from one wheelhouse: the runtime first, then each client.

    Each entry is a package description (``name``, ``version``, ``scripts``)
    plus ``wheels`` (exactly the wheels that package owns) and ``requires``
    (``(package, version)`` pairs it needs at exactly that version and the
    shared release). The runtime owns every wheel that is not a client's;
    a client owns only its own. Raises :class:`SystemExit` naming any
    library wheel the wheelhouse lacks, and :class:`ValueError` for a
    library or client that is not a plain release version.
    """
    named = {dist_name(w): w for w in wheels}
    runtime = runtime_package(named)
    clients = [client_package(named, name, runtime["version"]) for name in CLIENTS if name in named]
    return [runtime, *clients]


def runtime_package(named: dict[str, Path]) -> dict[str, Any]:
    """The :data:`RUNTIME` package of the wheelhouse ``named`` (canonical name to wheel).

    Every library wheel must be there, and each must be a plain release
    (checked here, because nothing else reads their versions); the runtime
    is versioned as the lowest library, xrdcore.
    """
    missing = [lib for lib in LIBRARIES if lib not in named]
    if missing:
        raise SystemExit(f"the wheelhouse has no wheel for {', '.join(missing)}")
    versions = [wheel_info(named[lib])["version"] for lib in LIBRARIES]
    owned = [wheel for name, wheel in sorted(named.items()) if name not in CLIENTS]
    return {"name": RUNTIME, "version": versions[0], "scripts": [], "requires": [], "wheels": owned}


def client_package(named: dict[str, Path], client: str, runtime_version: str) -> dict[str, Any]:
    """``client``'s package: its own wheel, requiring the runtime and its peers exactly."""
    peers = [(peer, wheel_info(named[peer])["version"]) for peer in CLIENTS[client]]
    info = wheel_info(named[client])
    return info | {"wheels": [named[client]], "requires": [(RUNTIME, runtime_version), *peers]}


def stage_runtime(
    stage: Path, wheels: list[Path], info: dict[str, Any], python: Path, minor: str
) -> None:
    """Lay out one package's files under ``stage`` as they will sit under ``/``.

    ``wheels`` are exactly the wheels this package owns (see :func:`plan`).
    The install is ``--no-deps --no-index``: dependency resolution already
    happened when the wheelhouse was built, and doing it again here could
    only reach the network or pick something untested. ``--compile`` makes
    the bytecode part of the package payload, so the package manager owns
    it and removal leaves no ``__pycache__`` behind; the wrappers run with
    ``-B`` so nothing is written into the runtime after installation. (The
    cost: a traceback names the staging path rather than the installed one.)
    Only the runtime package ships :data:`LAUNCHER`, because it owns the
    runtime; every client's wrappers reuse it.
    """
    prefix = stage / PREFIX.relative_to("/")
    # The *build* interpreter runs pip; ``--prefix`` lays the files out for the
    # target one. ``--ignore-installed`` stops pip skipping a wheel because the
    # build environment happens to have that distribution already.
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--compile",
            "--ignore-installed",
            "--only-binary=:all:",
            "--prefix",
            str(prefix),
            *map(str, wheels),
        ],
        check=True,
    )
    if info["name"] == RUNTIME:
        (prefix / "launch.py").write_text(LAUNCHER.format(minor=minor), encoding="utf-8")
    prune_scripts(prefix / "bin", info["scripts"])
    link_commands(stage, info["scripts"], python)
    doc = stage / "usr/share/doc" / info["name"]
    doc.mkdir(parents=True)
    (doc / "wheel-manifest.json").write_text(
        json.dumps(manifest(wheels), indent=2) + "\n", encoding="utf-8"
    )
    # Manual pages (xgfalclient ships gfal-*(1)) stay inside the private prefix
    # and are only linked into the system man path, like the commands.
    manuals = prefix / "share/man/man1"
    if manuals.exists():
        target = stage / "usr/share/man/man1"
        target.mkdir(parents=True)
        for page in manuals.glob("*.1"):
            (target / page.name).symlink_to(PREFIX / "share/man/man1" / page.name)


def prune_scripts(binary: Path, scripts: list[str]) -> None:
    """Remove everything pip put in ``binary`` except this package's own ``scripts``.

    Third-party wheels bring console scripts of their own (botocore's
    ``jmespath`` its ``jp.py``, ``cffi`` its ``cffi-gen-src``), whose
    shebangs name the *build* virtualenv: on the target they could only fail
    with a confusing "bad interpreter". No storage command needs them, and
    an empty ``bin`` is removed so the runtime package does not own one.
    """
    if not binary.is_dir():
        return
    for entry in binary.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry)
        elif entry.name not in scripts:
            entry.unlink()
    if not scripts:
        binary.rmdir()


def link_commands(stage: Path, scripts: list[str], python: Path) -> None:
    """Replace pip's script for each of ``scripts`` with an isolated wrapper in ``/usr/bin``.

    pip's generated scripts would point at the build venv's interpreter,
    which does not exist on the target; ``"$0"`` passes the command's own
    name so :data:`LAUNCHER` can find the matching entry point. A package
    with no commands (the runtime) gets no ``/usr/bin`` at all.
    """
    if not scripts:
        return
    binary = stage / "usr/bin"
    binary.mkdir(parents=True)
    for script in scripts:
        wrapper = stage / PREFIX.relative_to("/") / "bin" / script
        wrapper.write_text(
            f'#!/bin/sh\nexec {python} -I -S -B {PREFIX}/launch.py "$0" "$@"\n', encoding="utf-8"
        )
        wrapper.chmod(0o755)
        (binary / script).symlink_to(PREFIX / "bin" / script)


def summary(info: dict[str, Any]) -> str:
    """The package-manager summary line for ``info``'s kind of package."""
    return SUMMARIES.get(info["name"], SUMMARIES["client"])


def rpm_files(stage: Path, info: dict[str, Any]) -> str:
    """The ``%files`` section for ``info``, derived from what was actually staged.

    The runtime owner owns :data:`PREFIX` itself, so removal leaves nothing
    behind; a client claims only the entries it staged. Optional entries are
    listed only when staged, because an empty ``%files`` glob is an
    rpmbuild error.
    """
    lines = [str(PREFIX) if info["name"] == RUNTIME else f"{PREFIX}/*"]
    if (stage / "usr/bin").exists():
        lines.append("/usr/bin/*")
    lines.append(f"/usr/share/doc/{info['name']}")
    if (stage / "usr/share/man/man1").exists():
        lines.append("/usr/share/man/man1/*")
    return "\n".join(lines)


def rpm(stage: Path, output: Path, info: dict[str, Any], python: Path) -> None:
    """Wrap ``stage`` in a binary RPM and copy it into ``output``.

    ``AutoReqProv: no`` stops rpmbuild deriving requirements from the bundled
    native libraries - those are satisfied inside the bundle, and the
    auto-generated sonames would tie the package to build-host details.
    ``__os_install_post`` is disabled for the same reason: its brp scripts
    would strip, recompile and re-shebang the snapshot. What the runtime
    really needs from the OS is listed by hand: the interpreter the
    wrappers exec, glibc, and libgcc for native wheels' unwinding. A client
    needs only the exact runtime (and peer) releases, which bring those.
    """
    top = stage.parent / "rpmbuild"
    top.mkdir()
    release = info.get("release", 1)
    exact = ", ".join(f"{name} = {version}-{release}" for name, version in info["requires"])
    dependencies = exact or f"{python}, glibc, libgcc"
    # The spec only copies the already-staged tree into the buildroot: all
    # layout decisions were made in stage_runtime, shared with the DEB path.
    spec = top / "package.spec"
    spec.write_text(
        f'''%global __os_install_post %{{nil}}
Name: {info["name"]}
Version: {info["version"]}
Release: {release}
Summary: {summary(info)}
License: LGPL-3.0-or-later
URL: https://github.com/rob-c/{info["name"]}
AutoReqProv: no
Requires: {dependencies}
%description
Storage client. Dependencies are private wheel snapshots; rebuild for updates.
%install
mkdir -p "%{{buildroot}}"
cp -a "{stage}/." "%{{buildroot}}/"
%files
{rpm_files(stage, info)}
''',
        encoding="utf-8",
    )
    subprocess.run(["rpmbuild", "-bb", "--define", f"_topdir {top}", str(spec)], check=True)
    for artifact in top.glob("RPMS/*/*.rpm"):
        shutil.copy2(artifact, output)


def deb(stage: Path, output: Path, info: dict[str, Any], python: Path) -> None:
    """Wrap ``stage`` in a ``.deb`` for the build host's architecture, into ``output``.

    The dependencies mirror :func:`rpm`'s. ``libc6 (>= 2.28)`` is the oldest
    glibc the manylinux wheels in the snapshot may need; the package is still
    only meant for the release it was built on. ``--root-owner-group``
    gives the files root ownership without building as root.
    """
    # The snapshot holds native wheels, so the package is for this host's
    # architecture only (never ``all``).
    arch = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
    release = info.get("release", 1)
    exact = ", ".join(f"{name} (= {version}-{release})" for name, version in info["requires"])
    dependencies = exact or f"{python.name}, libc6 (>= 2.28), libgcc-s1"
    control = stage / "DEBIAN"
    control.mkdir()
    (control / "control").write_text(
        f"""Package: {info["name"]}
Version: {info["version"]}-{release}
Architecture: {arch}
Maintainer: Robert Currie <robert.andrew.currie@gmail.com>
Section: science
Priority: optional
Depends: {dependencies}
Description: {summary(info)}
 Dependencies are private wheel snapshots; rebuild for security updates.
""",
        encoding="utf-8",
    )
    target = output / f"{info['name']}_{info['version']}-{release}_{arch}.deb"
    subprocess.run(
        ["dpkg-deb", "--build", "--root-owner-group", str(stage), str(target)], check=True
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Build the runtime package, then one package per client wheel, into ``--output``.

    ``--python`` must be the *system* interpreter the packages will depend on
    (its resolved path is written into the wrappers), not a virtualenv: the
    target host has no venv. ``--output`` must not exist yet, so no stale
    package from an earlier build can be uploaded by mistake. ``--release``
    is shared by every package of one run, because the exact dependencies
    between them name it.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", choices=("rpm", "deb"), required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True, help="system interpreter, not a venv")
    parser.add_argument(
        "--release", type=positive_release, default=1, help="increment for dependency-only rebuilds"
    )
    args = parser.parse_args(argv)
    python = args.python.resolve()
    # Ask the target interpreter itself: its minor version names the
    # site-packages directory LAUNCHER puts on ``sys.path``.
    minor = subprocess.check_output(
        [str(python), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"], text=True
    ).strip()
    packages = plan(sorted(args.wheelhouse.resolve().glob("*.whl")))
    args.output.mkdir(parents=True, exist_ok=False)
    for info in packages:
        info["release"] = args.release
        with tempfile.TemporaryDirectory(prefix="storage-package-") as folder:
            stage = Path(folder) / "stage"
            stage.mkdir()
            stage_runtime(stage, info["wheels"], info, python, minor)
            {"rpm": rpm, "deb": deb}[args.family](stage, args.output.resolve(), info, python)
    return 0


def positive_release(value: str) -> int:
    """``--release`` parser: RPM and Debian revisions start at 1, so 0 is refused."""
    release = int(value)
    if release < 1:
        raise argparse.ArgumentTypeError("--release must be a positive integer")
    return release


if __name__ == "__main__":
    raise SystemExit(main())
