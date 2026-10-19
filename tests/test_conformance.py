"""The conformance check: the standard passes, and each kind of drift is named."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from xrddevtools import conformance, sync

#: A ``pyproject.toml`` that is exactly the standard (a library's).
STANDARD = """
[tool.pytest.ini_options]
testpaths = ["tests"]
filterwarnings = ["error"]

[tool.coverage.run]
branch = true
source = ["xrdcore"]
parallel = true

[tool.coverage.report]
fail_under = 100
exclude_also = [
  "if TYPE_CHECKING:",
  "@overload",
  "raise NotImplementedError",
  "\\\\.\\\\.\\\\.$",
  "if __name__ == .__main__.:",
]

[tool.ruff]
line-length = 100
target-version = "py311"
src = ["src", "tests"]

[tool.ruff.lint]
select = [
  "E", "F", "W", "I", "UP", "B", "C4", "SIM", "RUF",
  "S102", "S307", "S506", "S602", "S608", "S609", "S701",
]
ignore = []

[tool.ruff.lint.isort]
known-first-party = ["xrdcore"]

[tool.mypy]
python_version = "3.11"
strict = true
files = ["src", "tests"]

[[tool.mypy.overrides]]
module = ["tests.*"]
disallow_untyped_defs = false
disallow_incomplete_defs = false
check_untyped_defs = true

[[tool.mypy.overrides]]
module = ["asn1crypto.*"]
ignore_missing_imports = true

[[tool.mypy.overrides]]
module = ["xrdcore.*"]
follow_imports = "normal"

[tool.complexipy]
paths = ["src", "tests"]
max-complexity-allowed = 15
failed = true
"""

#: The banned-API section a client adds (W5's lint), as the check expects it.
BANNED = """
[tool.ruff.lint.flake8-tidy-imports.banned-api]
"http.client".msg = "use xrdhttp.session"
"""


#: The real drift check; the tests of the settings themselves stand it in for nothing.
REAL_DRIFT = sync.drift


@pytest.fixture(autouse=True)
def _settings_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Judge the settings alone: these checkouts carry no managed blocks.

    Whether a block is what ``xrddevtools sync`` would write is
    :mod:`xrddevtools.sync`'s test; the test below that restores it checks
    that conformance reports it.
    """
    monkeypatch.setattr(sync, "drift", lambda checkout: [])


def checkout(tmp_path: Path, pyproject: str = STANDARD, name: str = "xrdcore") -> Path:
    """A checkout called ``name`` with ``pyproject`` and one source and one test module."""
    root = tmp_path / name
    (root / "src" / name).mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / name / "__init__.py").write_text('"""Doc."""\n')
    (root / "tests" / "test_it.py").write_text("def test_it():\n    pass\n")
    (root / "pyproject.toml").write_text(textwrap.dedent(pyproject))
    return root


def problems(tmp_path: Path, pyproject: str) -> list[str]:
    """The messages the check reports for a checkout with ``pyproject``."""
    return [
        finding.message for finding in conformance.check_repository(checkout(tmp_path, pyproject))
    ]


def test_the_standard_conforms(tmp_path, capsys):
    root = checkout(tmp_path)
    assert conformance.check_repository(root) == []
    assert conformance.main(["--repo", str(root)]) == 0
    assert capsys.readouterr().out == "conformance: 1 repositories conform\n"


def test_a_banned_api_list_is_a_per_role_section_with_tid251(tmp_path):
    with_ban = STANDARD.replace('"S701",\n]', '"S701", "TID251",\n]') + BANNED
    assert problems(tmp_path, with_ban) == []


def test_the_banned_api_table_and_its_rule_go_together(tmp_path):
    selected = STANDARD.replace('"S701",\n]', '"S701", "TID251",\n]')
    assert problems(tmp_path / "a", selected) == [
        "tool.ruff.lint.flake8-tidy-imports: TID251 is selected but nothing is banned"
    ]
    assert problems(tmp_path / "b", STANDARD + BANNED) == [
        "tool.ruff.lint.flake8-tidy-imports: bans imports but TID251 is not selected"
    ]
    silent = selected + BANNED.replace('"use xrdhttp.session"', '""')
    assert problems(tmp_path / "c", silent) == [
        "tool.ruff.lint.flake8-tidy-imports.banned-api: 'http.client' has no msg naming"
        " the replacement"
    ]
    other = selected + BANNED + "[tool.ruff.lint.flake8-tidy-imports]\nban-relative-imports = 1\n"
    assert problems(tmp_path / "d", other) == [
        "tool.ruff.lint.flake8-tidy-imports: 'ban-relative-imports' is not part of the standard"
    ]


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("line-length = 100", "line-length = 120", "line-length is 120, not 100"),
        ('target-version = "py311"', 'target-version = "py39"', "target-version is 'py39'"),
        ('"S701",\n]', '"S701", "D",\n]', "select misses [] and adds ['D']"),
        ('"E", "F", ', "", "select misses ['E', 'F'] and adds []"),
        ("ignore = []", 'ignore = ["E501"]', "ignores ['E501']"),
        ("ignore = []", 'ignore = []\nper-file-ignores = {"x.py" = ["E"]}', "'per-file-ignores'"),
        ('src = ["src", "tests"]', 'src = ["src"]\nexclude = ["old"]', "tool.ruff: 'exclude'"),
        ("strict = true", "strict = false", "strict is False, not True"),
        ('python_version = "3.11"', 'python_version = "3.12"', "python_version is '3.12'"),
        ('files = ["src", "tests"]', 'files = ["src"]', "files leave out tests/"),
        ('files = ["src", "tests"]', 'files = ["src", "tests"]\nexclude = "x"', "'exclude'"),
        ('module = ["tests.*"]', 'module = ["xrdcore.*"]', "relaxes annotations for non-test"),
        ("check_untyped_defs = true", "check_untyped_defs = false", "without check_untyped_defs"),
        ("disallow_incomplete_defs = false", "warn_return_any = false", "is not an allowed"),
        ('module = ["asn1crypto.*"]', 'module = ["xrdsec.*"]', "for our own modules ['xrdsec.*']"),
        (
            "ignore_missing_imports = true",
            "ignore_missing_imports = true\nfollow_imports = 'skip'",
            "must stand alone",
        ),
        ("max-complexity-allowed = 15", "max-complexity-allowed = 30", "is 30, not 15"),
        ("failed = true", "failed = false", "failed is False, not True"),
        ('paths = ["src", "tests"]', 'paths = ["src"]', "paths leave out tests/"),
        ('paths = ["src", "tests"]', 'paths = ["src", "tests"]\nexclude = ["x"]', "'exclude'"),
        ("branch = true", "branch = false", "branch is False, not True"),
        ('source = ["xrdcore"]', "source = []", "no source"),
        ("fail_under = 100", "fail_under = 95", "fail_under is 95"),
        ('"@overload",\n', "", "exclude_also is ["),
        ("parallel = true", 'parallel = true\nomit = ["x.py"]', "omit exempts code"),
        ("fail_under = 100", 'fail_under = 100\nexclude_lines = ["x"]', "exclude_lines exempts"),
        ('filterwarnings = ["error"]', "filterwarnings = []", 'does not start with "error"'),
        (
            'filterwarnings = ["error"]',
            'filterwarnings = ["error", "ignore::DeprecationWarning"]',
            "is not one category in one module",
        ),
        (
            'filterwarnings = ["error"]',
            'filterwarnings = ["error", "ignore:old:DeprecationWarning:botocore"]',
            "matches a message",
        ),
        ('testpaths = ["tests"]', 'testpaths = ["tests"]\naddopts = "-W ignore"', "warnings off"),
        (
            'testpaths = ["tests"]',
            'testpaths = ["tests"]\naddopts = ["-ra", "-p", "no:warnings"]',
            "warnings off",
        ),
    ],
)
def test_each_departure_is_named(tmp_path, old, new, expected):
    assert old in STANDARD
    found = problems(tmp_path, STANDARD.replace(old, new, 1))
    assert any(expected in p for p in found), found


def test_named_third_party_exemptions_are_allowed_and_ours_are_not(tmp_path):
    allowed = STANDARD.replace(
        'files = ["src", "tests"]', 'files = "."\nuntyped_calls_exclude = ["distributed"]'
    )
    assert problems(tmp_path / "a", allowed) == []
    ours = allowed.replace('["distributed"]', '["xrdarc"]')
    assert problems(tmp_path / "b", ours) == [
        "tool.mypy: untyped_calls_exclude names our own modules ['xrdarc']"
    ]
    single = STANDARD.replace('module = ["asn1crypto.*"]', 'module = "asn1crypto"')
    assert problems(tmp_path / "c", single) == []
    # A third party's warning we cannot fix: one category, one module.
    filtered = STANDARD.replace(
        'filterwarnings = ["error"]',
        'filterwarnings = ["error", "ignore::DeprecationWarning:botocore"]',
    )
    assert problems(tmp_path / "d", filtered) == []


def test_every_python_tree_must_be_type_checked_and_measured(tmp_path):
    root = checkout(tmp_path)
    (root / "tools").mkdir()
    (root / "tools" / "x.py").write_text('"""Doc."""\n')
    found = [f.message for f in conformance.check_repository(root)]
    assert "tool.mypy: files leave out tools/" in found
    assert "tool.complexipy: paths leave out tools/" in found


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("x = 1  # noqa\n", "'noqa' without a specific error code"),
        ("x = 1  # type: ignore\n", "'type: ignore' without a specific error code"),
        ("x = 1  # noqa: E501\n", "'noqa' without a reason"),
        ("x = 1  # type: ignore[misc]\n", "'type: ignore' without a reason"),
        ("x = 1  # pyright: ignore[reportX]\n", "'pyright: ignore' without a reason"),
        ("if x:  # pragma: no cover\n    pass\n", "'pragma: no cover' is never allowed"),
        ("def f():  # complexipy: ignore\n    pass\n", "'complexipy: ignore' is never allowed"),
        ("x = 1  # nosec\n", "'nosec' is never allowed"),
        ("# fmt: off\nx = 1\n", "'fmt: off' is never allowed"),
        ("# mypy: ignore-errors\n", "'mypy: ignore-errors' is never allowed"),
    ],
)
def test_suppressions_need_a_code_and_a_reason_or_are_never_allowed(line, expected):
    ((number, problem),) = conformance.suppressions(line)
    assert number == 1 or "fmt" in expected or "mypy" in expected
    assert expected in problem


@pytest.mark.parametrize(
    "source",
    [
        "x = 1  # type: ignore[misc]  # assigning a frozen field is the refusal under test\n",
        "x = 1  # noqa: SIM112 - XRootD spells it so\n",
        "# Deliberately the wrong type: the refusal is under test.\n"
        "f(1)  # type: ignore[arg-type]\n",
        "# Overlaps the buffered overload on purpose.\n@overload\n"
        "def f(  # type: ignore[overload-overlap]\n    a: int,\n) -> int: ...\n",
        'text = "# noqa # type: ignore # pragma: no cover"\n',
    ],
)
def test_a_coded_suppression_with_its_reason_passes(source):
    assert list(conformance.suppressions(source)) == []


def test_suppressions_are_reported_where_they_are(tmp_path):
    root = checkout(tmp_path)
    (root / "tests" / "test_it.py").write_text("def test_it():\n    assert 1  # noqa\n")
    (root / "typings").mkdir()
    (root / "typings" / "lib.pyi").write_text("def f(): ...  # type: ignore\n")
    (root / "src" / "xrdcore" / "bad.py").write_text("x = (\n")
    found = {(f.where, f.message.split(" ")[0]) for f in conformance.check_repository(root)}
    assert found == {
        ("tests/test_it.py:2", "'noqa'"),
        ("typings/lib.pyi:1", "'type:"),
        ("src/xrdcore/bad.py:1", "cannot"),
    }


def test_snapshot_and_baseline_files_are_findings_outside_caches(tmp_path):
    root = checkout(tmp_path)
    (root / "complexipy-snapshot.json").write_text("{}")
    (root / ".venv" / "lib").mkdir(parents=True)
    (root / ".venv" / "lib" / "mypy-baseline.txt").write_text("")
    found = conformance.check_repository(root)
    assert [(f.where, f.message) for f in found] == [
        ("complexipy-snapshot.json", "snapshot/baseline file")
    ]
    assert str(found[0]) == "xrdcore: complexipy-snapshot.json: snapshot/baseline file"


def test_a_checkout_that_is_not_the_chains_is_refused(tmp_path):
    root = checkout(tmp_path, name="elsewhere")
    (finding,) = conformance.check_repository(root)
    assert "not a repository of the chain" in finding.message


def test_the_workspace_command_checks_every_repository_present(tmp_path, capsys):
    checkout(tmp_path, name="xrdcore")
    checkout(tmp_path, STANDARD.replace("fail_under = 100", "fail_under = 90"), "xrdsec")
    assert conformance.main(["--workspace", str(tmp_path)]) == 1
    assert capsys.readouterr().out == (
        "xrdsec: pyproject.toml: tool.coverage.report: fail_under is 90, not 100\n"
    )
    assert conformance.main(["--workspace", str(tmp_path), "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == [
        {
            "repo": "xrdsec",
            "where": "pyproject.toml",
            "message": "tool.coverage.report: fail_under is 90, not 100",
        }
    ]
    assert conformance.main(["--workspace", str(tmp_path / "xrdcore")]) == 2
    assert "no repository of the chain" in capsys.readouterr().err


def test_a_managed_block_that_is_not_the_standard_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "drift", REAL_DRIFT)
    root = checkout(tmp_path)
    found = [(f.where, f.message) for f in conformance.check_repository(root)]
    assert ("pyproject.toml", "no managed block 'ruff'") in [
        (where, message.split(":")[0]) for where, message in found
    ]
    assert ("CONTRIBUTING.md", "missing") in [
        (where, message.split(":")[0]) for where, message in found
    ]


def test_the_standard_values_are_the_ones_sync_writes():
    standard = sync.standard()
    assert tuple(standard["coverage"]["exclude_also"]) == conformance.STANDARD_EXCLUDE_ALSO
    assert {"E", "S701"} <= conformance.STANDARD_SELECT
    assert conformance.BANNED_API_RULE == "TID251"
