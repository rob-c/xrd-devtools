#!/usr/bin/env python3
"""Pre-flight checks for releasing the chain's six packages together.

The 0.4.0 release is six packages, published lowest layer first and strictly
in this order (:data:`CHAIN`) - xrdcore, xrdsec, xrdhttp, xrdclient, xgfalclient,
xrdarc - and each step's wheel must install from what the previous steps put
on PyPI. xrdsec goes before xrdhttp: xrdhttp's ``testing`` extra (what
``xrdhttp.testing`` is built on) requires it, and xrdhttp's publish job
installs that extra. Two repositories of the workspace are not part of the
release: xrdwms (0.1.x, released on its own schedule, after the chain it
sits on) and xrd-devtools, which is never published at all (its
``Private :: Do Not Upload`` classifier makes PyPI refuse it). A mismatch
found after the first upload is expensive: PyPI never forgets a version, so
the fix is a yank and a new release of every package above it.
xrdclient's ``docs/releasing.md`` (*Releasing the 0.4.0 chain*) is the runbook; this
tool does its mechanical checks.

``workspace`` takes the directory of sibling checkouts and
checks, for every package of :data:`CHAIN`:

* its version (``src/<name>/_version.py``) is the chain's version;
* its requirements on the rest of the chain follow the layering
  (:data:`FAMILY`) with the agreed specifiers - ``>=V,<next-minor`` for a
  library, ``==V`` for xgfalclient's exact xrdclient pin - and nothing
  depends upward (xrdarc never on a client, xrdsec never on xrdhttp); an
  optional dependency (an extra) may name only a package released earlier,
  so every extra installs from PyPI when its package is published;
* its CHANGELOG has a ``## [V]`` section (dated, with ``--require-dated``);
* unless ``--no-build``: it builds (sdist and wheel, ``python -m build
  --no-isolation``, into a temporary directory), ``twine check --strict``
  passes, and the wheel's ``METADATA`` carries the same version and the
  same chain requirements as ``pyproject.toml``.

``pypi WHEEL...`` is the publish workflows' gate: every chain requirement of
the given wheels must already be downloadable from the index as a wheel, at
its lower bound (the version the chain was tested with). A publish job runs
it before uploading, so a client can never reach PyPI ahead of a library it
needs.

Findings are printed one per line (``--json`` for a machine-readable list);
the exit status is 1 if there are any. Needs ``packaging`` (a dependency of
``build``) and, for the build step, ``build``, ``hatchling`` and ``twine``
in the running interpreter.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from email.message import Message
from email.parser import Parser
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

#: The chain, lowest layer first: also the release order.
CHAIN = ("xrdcore", "xrdsec", "xrdhttp", "xrdclient", "xgfalclient", "xrdarc")
#: Per package: the chain packages it must require, and those it may.
#: Anything else of the chain is an upward or sideways dependency.
FAMILY: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "xrdcore": (frozenset(), frozenset()),
    "xrdsec": (frozenset({"xrdcore"}), frozenset()),
    "xrdhttp": (frozenset({"xrdcore"}), frozenset()),
    "xrdclient": (frozenset({"xrdcore", "xrdsec", "xrdhttp"}), frozenset()),
    "xgfalclient": (frozenset({"xrdclient", "xrdcore", "xrdsec", "xrdhttp"}), frozenset()),
    # The A-REX transport is xrdhttp's session since 0.4.0.
    "xrdarc": (frozenset({"xrdcore", "xrdsec", "xrdhttp"}), frozenset()),
}
#: ``(package, requirement)`` pairs pinned exactly rather than to a range.
EXACT = frozenset({("xgfalclient", "xrdclient")})
#: ``__version__ = "X.Y.Z"`` in ``_version.py``.
_VERSION = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def expected_specifier(package: str, dependency: str, version: str) -> SpecifierSet:
    """The specifier ``package`` must use for ``dependency`` in a chain at ``version``.

    A range up to the next minor while the chain is ``0.x`` (the next major
    after 1.0), so a library patch release reaches every client without a
    client release; xgfalclient's xrdclient pin is exact because the two
    share private behaviour and are only ever tested together.
    """
    if (package, dependency) in EXACT:
        return SpecifierSet(f"=={version}")
    major, minor = (int(part) for part in version.split(".")[:2])
    ceiling = f"{major}.{minor + 1}" if major == 0 else f"{major + 1}"
    return SpecifierSet(f">={version},<{ceiling}")


def project_version(checkout: Path, name: str) -> str | None:
    """The version in ``checkout/src/<name>/_version.py``, or ``None`` if there is none."""
    path = checkout / "src" / name / "_version.py"
    found = _VERSION.search(path.read_text(encoding="utf-8")) if path.is_file() else None
    return found.group(1) if found else None


def chain_requirements(lines: list[str]) -> dict[str, Requirement]:
    """The requirements in ``lines`` that name a package of :data:`CHAIN`, by canonical name.

    Requirements conditional on an extra (``; extra == "fast"`` in wheel
    metadata) are left out: they are not part of the default install.
    """
    found: dict[str, Requirement] = {}
    for line in lines:
        requirement = Requirement(line)
        name = canonicalize_name(requirement.name)
        if name in CHAIN and "extra" not in str(requirement.marker or ""):
            found[name] = requirement
    return found


def pyproject_requirements(checkout: Path) -> tuple[list[str], list[str]]:
    """``pyproject.toml``'s default dependencies, and every optional-dependency line."""
    project = tomllib.loads((checkout / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    optional = [
        line for lines in project.get("optional-dependencies", {}).values() for line in lines
    ]
    return list(project.get("dependencies", [])), optional


def layering_findings(
    package: str, requirements: dict[str, Requirement], version: str
) -> list[str]:
    """What is wrong with ``package``'s default chain requirements, one finding per problem."""
    required, allowed = FAMILY[package]
    findings = [
        f"{package}: does not require {name}" for name in sorted(required - set(requirements))
    ]
    for name in sorted(set(requirements) - required - allowed):
        findings.append(f"{package}: must not depend on {name} (layering)")
    for name, requirement in sorted(requirements.items()):
        findings += specifier_findings(package, requirement, version, name in required | allowed)
    return findings


def specifier_findings(
    package: str, requirement: Requirement, version: str, allowed: bool
) -> list[str]:
    """A finding if an allowed chain ``requirement`` of ``package`` has the wrong specifier."""
    name = canonicalize_name(requirement.name)
    wanted = expected_specifier(package, name, version)
    if not allowed or requirement.specifier == wanted:
        return []
    return [f"{package}: requires {name}{requirement.specifier}, expected {name}{wanted}"]


def optional_findings(package: str, lines: list[str], version: str) -> list[str]:
    """Chain requirements in optional dependencies use the release order and the specifiers.

    ``xrdcore[fast]`` from xrdclient, ``xrdsec`` from xrdhttp's ``testing``:
    each must name a package released before ``package`` (:data:`CHAIN`
    order), or the extra could not be installed when ``package`` is
    published.
    """
    findings = []
    for name, requirement in sorted(chain_requirements(lines).items()):
        if CHAIN.index(name) > CHAIN.index(package):
            findings.append(f"{package}: an extra requires {name}, which is released after it")
        findings += specifier_findings(package, requirement, version, True)
    return findings


def changelog_findings(
    package: str, checkout: Path, version: str, require_dated: bool
) -> list[str]:
    """A finding unless the CHANGELOG has a ``## [version]`` section (dated, if required)."""
    heading = _changelog_heading(checkout / "CHANGELOG.md", version)
    if heading is None:
        return [f"{package}: CHANGELOG.md has no [{version}] section"]
    if require_dated and not re.search(r"\d{4}-\d{2}-\d{2}\s*$", heading):
        return [f"{package}: CHANGELOG.md [{version}] is not dated ({heading!r})"]
    return []


def _changelog_heading(path: Path, version: str) -> str | None:
    """The ``## [version]`` heading line of the changelog at ``path``, if it has one."""
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    return next((line for line in text.splitlines() if line.startswith(f"## [{version}]")), None)


def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` capturing its output; a failure is a result, not an exception."""
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def failed(package: str, step: str, result: subprocess.CompletedProcess[str]) -> list[str]:
    """A finding quoting the last lines of a failed ``step``'s output, or none if it passed."""
    if result.returncode == 0:
        return []
    tail = " | ".join((result.stdout + result.stderr).strip().splitlines()[-3:])
    return [f"{package}: {step} failed ({result.returncode}): {tail}"]


def wheel_metadata(wheel: Path) -> Message:
    """The parsed ``METADATA`` of ``wheel`` (the one ``.dist-info`` a wheel holds)."""
    with zipfile.ZipFile(wheel) as archive:
        member = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
        return Parser().parsestr(archive.read(member).decode("utf-8"))


def _pinned(lines: list[str]) -> dict[str, str]:
    """The chain requirements of ``lines`` as text, by name: what two sources must agree on."""
    return {name: str(requirement) for name, requirement in chain_requirements(lines).items()}


def wheel_findings(package: str, wheel: Path, version: str, declared: list[str]) -> list[str]:
    """The built ``wheel``'s METADATA must carry ``version`` and the declared chain requirements."""
    meta = wheel_metadata(wheel)
    built = meta.get_all("Requires-Dist") or []
    findings = []
    if meta["Version"] != version:
        findings.append(f"{package}: wheel METADATA version {meta['Version']} is not {version}")
    if _pinned(built) != _pinned(declared):
        findings.append(
            f"{package}: wheel Requires-Dist {sorted(_pinned(built).values())} "
            "differs from pyproject.toml"
        )
    return findings


def build_findings(package: str, checkout: Path, version: str, declared: list[str]) -> list[str]:
    """Build ``checkout`` into a fresh directory, ``twine check`` it and inspect its wheel."""
    with tempfile.TemporaryDirectory(prefix=f"preflight-{package}-") as folder:
        built = run(
            [sys.executable, "-m", "build", "--no-isolation", "--outdir", folder, str(checkout)]
        )
        if findings := failed(package, "build", built):
            return findings
        dist = sorted(Path(folder).iterdir())
        findings = failed(
            package,
            "twine check",
            run([sys.executable, "-m", "twine", "check", "--strict", *map(str, dist)]),
        )
        wheels = [path for path in dist if path.suffix == ".whl"]
        if not wheels:
            return [*findings, f"{package}: the build produced no wheel"]
        return findings + wheel_findings(package, wheels[0], version, declared)


def package_findings(
    workspace: Path, package: str, version: str, options: argparse.Namespace
) -> list[str]:
    """Every finding for one package of the chain."""
    checkout = workspace / package
    if not (checkout / "pyproject.toml").is_file():
        return [f"{package}: no checkout at {checkout}"]
    found = project_version(checkout, package)
    findings = [] if found == version else [f"{package}: version is {found}, not {version}"]
    declared, optional = pyproject_requirements(checkout)
    findings += layering_findings(package, chain_requirements(declared), version)
    findings += optional_findings(package, optional, version)
    findings += changelog_findings(package, checkout, version, options.require_dated)
    if options.build:
        findings += build_findings(package, checkout, version, declared)
    return findings


def check_workspace(options: argparse.Namespace) -> list[str]:
    """Every finding for the chain in ``options.workspace``, in release order.

    The chain's version is ``--version`` if given, else xrdcore's (the
    lowest layer's), so a single stray version shows up as that package's
    finding rather than as five.
    """
    version = options.version or project_version(options.workspace / CHAIN[0], CHAIN[0])
    if version is None:
        return [f"{CHAIN[0]}: no version to check the chain against (pass --version)"]
    findings = []
    for package in options.packages or CHAIN:
        findings += package_findings(options.workspace, package, version, options)
    return findings


def published_pin(requirement: Requirement) -> str:
    """What must already be on the index for ``requirement``: its lower bound, exactly.

    The chain is tested against the lowest version its ranges allow, so
    that is the version that must exist; a requirement with no lower bound
    is checked as written.
    """
    for specifier in requirement.specifier:
        if specifier.operator in (">=", "==", "~="):
            return f"{requirement.name}=={specifier.version}"
    return str(requirement)


def check_published(options: argparse.Namespace) -> list[str]:
    """A finding for each chain requirement of ``options.wheels`` the index has no wheel for."""
    findings = []
    with tempfile.TemporaryDirectory(prefix="preflight-pypi-") as folder:
        for wheel in options.wheels:
            lines = wheel_metadata(wheel).get_all("Requires-Dist") or []
            for requirement in chain_requirements(lines).values():
                pin = published_pin(requirement)
                download = run(_download(pin, options.index_url, folder))
                findings += failed(Path(wheel).name, f"pip download {pin}", download)
    return findings


def _download(pin: str, index_url: str, folder: str) -> list[str]:
    """The ``pip download`` argv fetching exactly the wheel ``pin`` names, and nothing else."""
    return [
        sys.executable,
        *("-m", "pip", "download", "--only-binary=:all:", "--no-deps"),
        *("--index-url", index_url, "--dest", folder, pin),
    ]


def parser() -> argparse.ArgumentParser:
    """The command line: the ``workspace`` and ``pypi`` subcommands, each with ``--json``."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print the findings as a JSON list")
    top = argparse.ArgumentParser(
        prog="xrddevtools preflight",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = top.add_subparsers(dest="command", required=True)
    workspace = commands.add_parser("workspace", parents=[common], help="check before tagging")
    workspace.add_argument(
        "--workspace", type=Path, default=Path.cwd(), help="the checkouts' parent"
    )
    workspace.add_argument("--version", help="the chain's version (default: xrdcore's)")
    workspace.add_argument("--package", dest="packages", action="append", choices=CHAIN)
    workspace.add_argument("--no-build", dest="build", action="store_false", help="skip building")
    workspace.add_argument("--require-dated", action="store_true", help="changelogs must be dated")
    published = commands.add_parser("pypi", parents=[common], help="check before uploading")
    published.add_argument("wheels", nargs="+", type=Path)
    published.add_argument("--index-url", default="https://pypi.org/simple/")
    return top


def main(argv: list[str] | None = None) -> int:
    """Run the selected check, print its findings, and return 1 if there were any."""
    options = parser().parse_args(argv)
    findings = check_published(options) if options.command == "pypi" else check_workspace(options)
    print(
        json.dumps(findings, indent=2) if options.json else "\n".join(findings or ["no findings"])
    )
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
