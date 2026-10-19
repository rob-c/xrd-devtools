"""What the package promises the interpreters that run its modules as plain files.

``installed_smoke.py``, ``distribution_package.py``, ``chain_preflight.py``,
``check_dist.py`` and ``release.py`` run as ``python path/to/module.py`` in
environments where xrddevtools is not installed (a clean wheel environment, a
container's build interpreter, a publish job). Python then puts
``src/xrddevtools/`` itself first on ``sys.path``, so every module there must
be importable under its bare name without hiding a standard-library or
dependency module, and those five may
import nothing but the standard library (and ``packaging``, for the
pre-flight).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "xrddevtools"
PLAIN_FILES = (
    "installed_smoke",
    "distribution_package",
    "chain_preflight",
    "check_dist",
    "release",
)
#: What the plain files may import beyond the standard library.
ALLOWED_THIRD_PARTY = {"packaging"}


def _imports(path: Path) -> set[str]:
    """The top-level names a module imports."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_no_module_hides_a_standard_library_or_dependency_module():
    modules = {path.stem for path in PACKAGE.glob("*.py")}
    assert not modules & set(sys.stdlib_module_names)
    assert not modules & {"packaging", "radon", "complexipy", "pytest"}


@pytest.mark.parametrize("name", PLAIN_FILES)
def test_plain_file_modules_need_only_the_standard_library(name):
    imported = _imports(PACKAGE / f"{name}.py") - {"__future__"}
    assert imported <= set(sys.stdlib_module_names) | ALLOWED_THIRD_PARTY, imported
