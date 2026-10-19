"""The chain's repositories, their roles, and where their Python lives.

A *workspace* is the directory holding sibling checkouts - the layout every
developer of the chain has (``~/src/dev/gfal/xrdcore``, ``.../xrdclient``,
...) and the one CI builds under ``.dependencies/``. The duplicate gate and
the conformance check walk it; both need to know which checkouts belong to
the chain and what each one is, because the standard allows a few documented
differences *per role* (a library and a client ban different imports).

Roles: the three **libraries** every client is built on; the three
**clients**; xrdwms, the **jobs** layer on top of all of them; and this
repository, the **tooling**.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

#: Every repository of the chain, lowest layer first.
REPOSITORIES: tuple[str, ...] = (
    "xrdcore",
    "xrdsec",
    "xrdhttp",
    "xrdclient",
    "xgfalclient",
    "xrdarc",
    "xrdwms",
)
#: The role of each repository, this one's included.
ROLES: dict[str, str] = {
    "xrdcore": "library",
    "xrdsec": "library",
    "xrdhttp": "library",
    "xrdclient": "client",
    "xgfalclient": "client",
    "xrdarc": "client",
    "xrdwms": "jobs",
    "xrd-devtools": "tooling",
}
#: The top-level importable packages the chain's repositories own; a mypy or
#: coverage setting naming one of these is about our code, not a third party's.
OWN_PACKAGES = frozenset(
    {
        "xrdcore",
        "xrdsec",
        "xrdhttp",
        "xrdclient",
        "xgfalclient",
        "gfal2",
        "gfal2_util",
        "xrdarc",
        "xrdwms",
        "xrddevtools",
        "tests",
        "benchmarks",
        "examples",
        "tools",
    }
)
#: The directories that hold handwritten Python in a checkout, where present.
PYTHON_TREES: tuple[str, ...] = (
    "src",
    "tests",
    "tools",
    "benchmarks",
    "examples",
    ".github/scripts",
)
#: Directory names never searched: caches, environments and build output.
SKIPPED_PARTS = frozenset(
    {
        "__pycache__",
        ".venv",
        "venv",
        "build",
        "dist",
        "site",
        ".dependencies",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".complexipy_cache",
        ".hypothesis",
        ".tox",
        "node_modules",
    }
)


def checkouts(workspace: Path, names: Sequence[str] = REPOSITORIES) -> list[Path]:
    """The checkouts of ``names`` present in ``workspace`` (a ``pyproject.toml`` marks one)."""
    return [workspace / name for name in names if (workspace / name / "pyproject.toml").is_file()]


def python_files(root: Path) -> Iterator[Path]:
    """Every ``.py``/``.pyi`` file under ``root``, sorted, outside :data:`SKIPPED_PARTS`."""
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.py*")):
        if path.suffix in (".py", ".pyi") and not SKIPPED_PARTS.intersection(
            path.relative_to(root).parts
        ):
            yield path


def python_trees(checkout: Path) -> list[str]:
    """The :data:`PYTHON_TREES` of ``checkout`` that hold at least one Python file."""
    return [tree for tree in PYTHON_TREES if next(python_files(checkout / tree), None)]
