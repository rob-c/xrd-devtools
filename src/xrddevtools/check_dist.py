"""Release-artifact checks shared by the CI and publish workflows (standard library only).

Moved here from the identical ``.github/scripts/check_dist.py`` the three
libraries each carried. It runs as a plain file (``python
.dependencies/xrd-devtools/src/xrddevtools/check_dist.py ...``) in the clean
environment a wheel was installed into, where nothing of this package is
installed, so it imports only the standard library.

``contents <package> <dist-dir>``
    The wheel holds the package (``py.typed`` included) and its
    ``.dist-info`` and nothing else; the sdist holds no caches, coverage
    data, built site, virtual environment or compiled bytecode. A local build
    from a working tree is exactly where such things lie around, and PyPI
    never forgets an upload.

``imports <package>``
    Run with the interpreter of a clean environment the wheel was installed
    into, from a directory without the source tree: the installed version
    agrees with the wheel metadata, ``py.typed`` is installed, and every
    public module (no ``_``-prefixed part in its dotted name) imports - which
    is what proves the declared dependencies are the ones the code needs.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.resources
import pkgutil
import sys
import tarfile
import zipfile
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

#: Path parts no sdist may contain.
FORBIDDEN_PARTS = {
    "__pycache__",
    ".coverage",
    ".hypothesis",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    ".dependencies",
    "build",
    "dist",
    "htmlcov",
    "site",
    "venv",
}


def _one(directory: Path, pattern: str) -> Path:
    """The single artifact matching ``pattern``; two would mean a stale build."""
    found = sorted(directory.glob(pattern))
    if len(found) != 1:
        raise SystemExit(f"expected one {pattern} in {directory}, found {found}")
    return found[0]


def contents(package: str, dist: Path) -> None:
    """Check the wheel's and the sdist's member lists (:class:`SystemExit` on the first problem)."""
    wheel = _one(dist, f"{package}-*.whl")
    names = zipfile.ZipFile(wheel).namelist()
    _check_wheel(wheel.name, package, names)
    sdist = _one(dist, f"{package}-*.tar.gz")
    with tarfile.open(sdist) as archive:
        members = [member.name for member in archive.getmembers()]
    bad = [name for name in members if _is_build_or_cache(name)]
    if bad:
        raise SystemExit(f"{sdist.name} carries build or cache files: {bad}")
    print(f"{wheel.name}: {len(names)} files; {sdist.name}: {len(members)} members")


def _check_wheel(wheel: str, package: str, names: Sequence[str]) -> None:
    """The wheel holds the package (``py.typed`` included) and its metadata, and no bytecode."""
    stray = _stray_tops(package, names)
    if stray:
        raise SystemExit(f"{wheel} carries files outside the package: {stray}")
    if f"{package}/py.typed" not in names:
        raise SystemExit(f"{wheel} has no {package}/py.typed")
    if any(name.endswith((".pyc", ".pyo")) for name in names):
        raise SystemExit(f"{wheel} carries compiled bytecode")


def _stray_tops(package: str, names: Sequence[str]) -> list[str]:
    """The wheel's top-level entries that are neither the package nor its ``.dist-info``."""
    tops = {name.split("/", 1)[0] for name in names}
    return sorted(top for top in tops if top != package and not top.endswith(".dist-info"))


def _is_build_or_cache(member: str) -> bool:
    """Whether an sdist member (``pkg-1.0/...``) is build output, a cache or coverage data."""
    return bool(
        FORBIDDEN_PARTS.intersection(member.split("/")[1:])
        or member.endswith((".pyc", ".pyo"))
        or Path(member).name.startswith(".coverage")
    )


def imports(package: str) -> None:
    """Import every public module of the installed ``package``."""
    root = importlib.import_module(package)
    version = importlib.metadata.version(package)
    _check_installed(package, root, version)
    public = _public_modules(package, root)
    for name in public:
        importlib.import_module(name)
    print(f"{package} {version}: imported {len(public)} public modules")


def _check_installed(package: str, root: ModuleType, version: str) -> None:
    """The import is the installed wheel's: its version, its location, its ``py.typed``."""
    if getattr(root, "__version__", version) != version:
        raise SystemExit(f"runtime version {root.__version__} is not the wheel's {version}")
    source = Path(root.__file__ or "").resolve()
    if "site-packages" not in source.parts:
        raise SystemExit(f"{package} was imported from {source}, not the installed wheel")
    if not importlib.resources.files(package).joinpath("py.typed").is_file():
        raise SystemExit(f"{package}/py.typed is not installed")


def _public_modules(package: str, root: ModuleType) -> list[str]:
    """Every module of ``package`` with no ``_``-prefixed part in its dotted name."""
    return [
        info.name
        for info in pkgutil.walk_packages(root.__path__, f"{package}.")
        if not any(part.startswith("_") for part in info.name.split("."))
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch ``contents <package> <dist>`` or ``imports <package>``.

    A failed check exits through :class:`SystemExit` with its message (status
    1); a malformed command line is a usage error (status 2).
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 3 and args[0] == "contents":
        contents(args[1], Path(args[2]))
    elif len(args) == 2 and args[0] == "imports":
        imports(args[1])
    else:
        print("usage: check_dist.py contents <package> <dist> | imports <package>", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
