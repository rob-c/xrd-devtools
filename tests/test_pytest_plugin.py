"""The scorecard and maintainability gates as each repository's suite runs them."""

from __future__ import annotations

import importlib.util
import json
import textwrap
from pathlib import Path

import pytest

from xrddevtools import pytest_plugin

#: A small module that is easy to take over by every one of the twelve measures.
TIDY = '''
"""Unit conversion for transfer reports.

Rates arrive in bytes per second; people read megabytes per second.
"""

#: Bytes in one (decimal) megabyte, as transfer tools report rates.
MEGABYTE = 1_000_000


def to_megabytes(rate: float) -> float:
    """``rate`` (bytes per second) in megabytes per second.

    A negative rate is a clock step, not a transfer, and reads as zero.
    """
    # Clamp first: a negative rate must never reach a report.
    return max(rate, 0.0) / MEGABYTE
'''
#: The opposite: undocumented, unannotated, deeply nested and long-winded.
UNTIDY = (
    "def f(a, b, c, d, e, g, h):\n"
    + "".join(f"    {'    ' * depth}if a > {depth}:\n" for depth in range(8))
    + "        "
    + "    " * 8
    + "return b\n"
)


def repository(tmp_path: Path, source: str, policy: dict[str, object] | None = None) -> Path:
    """A repository with one module in ``src`` and a ``tests/test_gate.py`` to anchor a gate."""
    root = tmp_path / "xrdexample"
    (root / "src" / "xrdexample").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "xrdexample" / "__init__.py").write_text(textwrap.dedent(source))
    (root / "tests" / "test_gate.py").write_text("")
    if policy is not None:
        (root / "maintainability.json").write_text(json.dumps(policy))
    return root / "tests" / "test_gate.py"


def standard_policy(**limits: float) -> dict[str, object]:
    """``maintainability.json`` with the standard limits, overridden by ``limits``."""
    from xrddevtools.maintainability import STANDARD_LIMITS

    return {"schema": 1, "paths": ["src"], "exclude": [], "limits": STANDARD_LIMITS | limits}


def test_the_gate_belongs_to_the_repository_of_its_test_file(tmp_path):
    anchor = repository(tmp_path, TIDY)
    assert pytest_plugin.repository_root(anchor) == tmp_path / "xrdexample"
    gate = pytest_plugin.scorecard_gate(anchor)
    assert gate.__doc__ == "xrdexample is easier to take over than the industry median."


def test_a_tidy_repository_passes_the_scorecard(tmp_path):
    pytest_plugin.scorecard_gate(repository(tmp_path, TIDY))()


def test_an_untidy_repository_is_told_which_measures_are_worse(tmp_path):
    gate = pytest_plugin.scorecard_gate(repository(tmp_path, UNTIDY))
    with pytest.raises(AssertionError, match="xrdexample is worse than the corpus median on"):
        gate()


def test_the_index_threshold_is_the_repositorys_to_raise(tmp_path):
    gate = pytest_plugin.scorecard_gate(repository(tmp_path, TIDY), min_index=100)
    with pytest.raises(AssertionError):
        gate()


def test_maintainability_gate_passes_and_fails_on_the_policy(tmp_path):
    anchor = repository(tmp_path, TIDY, standard_policy())
    gate = pytest_plugin.maintainability_gate(anchor)
    assert gate.__doc__ == "No function of xrdexample exceeds a maintainability limit."
    assert [mark.name for mark in getattr(gate, "pytestmark", [])] == ["timeout"]
    gate()
    over = repository(tmp_path / "over", UNTIDY, standard_policy())
    with pytest.raises(AssertionError, match="refactor, never loosen"):
        pytest_plugin.maintainability_gate(over)()


def test_maintainability_gate_refuses_a_loosened_policy(tmp_path):
    anchor = repository(tmp_path, TIDY, standard_policy(ccn=50))
    with pytest.raises(AssertionError):
        pytest_plugin.maintainability_gate(anchor)()


def test_missing_metrics_skip_environmentally_unless_required(monkeypatch):
    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: None if name == "complexipy" else real(name),
    )
    monkeypatch.delenv(pytest_plugin.REQUIRED, raising=False)
    with pytest.raises(pytest.skip.Exception, match=r"environmental: .* complexipy"):
        pytest_plugin.require_metrics()
    monkeypatch.setenv(pytest_plugin.REQUIRED, "1")
    with pytest.raises(pytest.fail.Exception, match="XRD_DEVTOOLS_REQUIRED is set"):
        pytest_plugin.require_metrics()


def test_loaded_with_dash_p_it_names_itself_in_the_header():
    (line,) = pytest_plugin.pytest_report_header()
    assert line.startswith("xrd-devtools 0.") and "gates from" in line


def test_the_release_gate_passes_a_consistent_checkout_and_names_each_problem(tmp_path):
    root = tmp_path / "xrdexample"
    (root / "src" / "xrdexample").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "xrdexample" / "_version.py").write_text('__version__ = "0.4.0"\n')
    (root / "pyproject.toml").write_text(
        textwrap.dedent(
            """
            [project]
            name = "xrdexample"
            dynamic = ["version"]
            readme = "README.md"
            requires-python = ">=3.11"
            classifiers = [
              "Programming Language :: Python :: 3.11",
              "Programming Language :: Python :: 3.12",
              "Programming Language :: Python :: 3.13",
              "Programming Language :: Python :: 3.14",
            ]
            [project.urls]
            Repository = "https://github.com/rob-c/xrdexample"
            Issues = "https://github.com/rob-c/xrdexample/issues"
            Changelog = "https://github.com/rob-c/xrdexample/blob/main/CHANGELOG.md"
            [tool.hatch.version]
            path = "src/xrdexample/_version.py"
            """
        )
    )
    for name, text in {
        "README.md": "# xrdexample\n",
        "CHANGELOG.md": "## [0.4.0] - Unreleased\n",
        "LICENSE": "",
        "COPYING": "",
    }.items():
        (root / name).write_text(text)
    gate = pytest_plugin.release_gate(root / "tests" / "test_release_metadata.py")
    assert gate.__doc__ == (
        "xrdexample has one version, one changelog section and agreeing metadata."
    )
    gate()
    (root / "CHANGELOG.md").write_text("## [0.3.2] - 2026-10-07\n")
    with pytest.raises(AssertionError, match=r"the first section must be 0\.4\.0"):
        gate()
    (root / "src" / "xrdexample").rename(root / "src" / "example")
    other = pytest_plugin.release_gate(root / "tests" / "t.py", package="example")
    with pytest.raises(AssertionError, match=r"path must be 'src/example/_version\.py'"):
        other()
