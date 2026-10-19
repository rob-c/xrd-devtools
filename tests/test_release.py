"""The release-metadata checks: a consistent checkout passes, and each kind of drift is named."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import runpy
import sys
import types
from pathlib import Path

import pytest

from xrddevtools import release

PYPROJECT = """
[project]
name = "xrdexample"
dynamic = ["version"]
readme = "README.md"
requires-python = ">=3.11"
classifiers = [
  "Programming Language :: Python :: 3 :: Only",
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
CHANGELOG = """# Changelog

## [0.4.0] - Unreleased

- Everything.

## [0.3.2] - 2026-10-07

- Something.
"""
PUBLISH = """name: Publish
jobs:
  build:
    steps:
      - uses: rob-c/xrd-devtools/.github/actions/publish@main
        with:
          package: xrdexample
  publish:
    permissions:
      id-token: write
    steps:
      - uses: pypa/gh-action-pypi-publish@release/v1
"""


def checkout(tmp_path: Path, **files: str) -> Path:
    """A consistent repository, with ``files`` (``/`` spelled ``__``) replaced or added."""
    root = tmp_path / "xrdexample"
    contents = {
        "pyproject.toml": PYPROJECT,
        "CHANGELOG.md": CHANGELOG,
        "README.md": "# xrdexample\n\nAn example.\n",
        "LICENSE": "LGPL\n",
        "COPYING": "GPL\n",
        "src/xrdexample/__init__.py": "from xrdexample._version import __version__\n",
        "src/xrdexample/_version.py": '"""The version."""\n\n__version__ = "0.4.0"\n',
        ".github/workflows/publish.yml": PUBLISH,
    } | {name.replace("__", "/"): text for name, text in files.items()}
    for name, text in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def test_a_consistent_checkout_has_no_problem(tmp_path):
    assert release.checkout_problems(checkout(tmp_path)) == []


def test_every_repository_of_the_workspace_is_consistent():
    """The real checkouts beside this one, where the workspace has them."""
    workspace = Path(__file__).resolve().parents[2]
    roots = [workspace / name for name in ("xrdcore", "xrd-devtools")]
    for root in roots:
        if (root / "pyproject.toml").is_file():
            assert release.checkout_problems(root) == [], root.name


def test_the_version_must_have_one_dynamic_source(tmp_path):
    static = PYPROJECT.replace('dynamic = ["version"]', 'version = "0.4.0"')
    moved = PYPROJECT.replace("src/xrdexample/_version.py", "src/xrdexample/__init__.py")
    assert release.checkout_problems(checkout(tmp_path, **{"pyproject.toml": static})) == [
        'pyproject.toml: the version must be dynamic = ["version"], not static'
    ]
    assert release.checkout_problems(checkout(tmp_path, **{"pyproject.toml": moved})) == [
        "pyproject.toml: [tool.hatch.version] path must be 'src/xrdexample/_version.py'"
    ]


def test_the_version_file_assigns_one_release_literal_and_nothing_else_does(tmp_path):
    root = checkout(tmp_path)
    version = root / "src/xrdexample/_version.py"
    version.write_text("VERSION = '1'\n")
    problems = release.checkout_problems(root)
    assert "src/xrdexample/_version.py: must assign __version__ one string literal" in problems
    version.write_text('__version__ = "0.4.0.dev0"\n')
    problems = release.checkout_problems(root)
    assert "src/xrdexample/_version.py: '0.4.0.dev0' is not a release version X.Y.Z" in problems
    version.write_text('__version__ = "0.4.0"\n')
    (root / "src/xrdexample/cli.py").write_text('    __version__: str = "0.4.0"\n')
    assert release.checkout_problems(root) == [
        "src/xrdexample/cli.py: a second version source "
        "(import __version__ from xrdexample._version instead)"
    ]


def test_without_a_version_the_changelog_is_not_judged(tmp_path):
    root = checkout(tmp_path, **{"CHANGELOG.md": ""})
    (root / "src/xrdexample/_version.py").unlink()
    assert release.checkout_problems(root) == [
        "src/xrdexample/_version.py: must assign __version__ one string literal"
    ]


@pytest.mark.parametrize(
    ("changelog", "problem"),
    [
        ("# Changelog\n", "no section heading; the first must be '## [0.4.0] - ...'"),
        (
            "## [0.4.0] - soon\n",
            "'## [0.4.0] - soon' is not '## [X.Y.Z] - Unreleased' or '... - YYYY-MM-DD'",
        ),
        (
            "## [0.3.2] - 2026-10-07\n## [0.4.0] - Unreleased\n",
            "sections are not distinct releases, newest first: ['0.3.2', '0.4.0']",
        ),
        (
            "## [0.4.0] - Unreleased\n## [0.4.0] - 2026-10-01\n",
            "sections are not distinct releases, newest first: ['0.4.0', '0.4.0']",
        ),
        ("## [0.3.2] - 2026-10-07\n", "the first section must be 0.4.0, not ['0.3.2']"),
        (
            CHANGELOG + "\n[0.3.2]: https://github.com/rob-c/xrdexample/compare/v0.3.1...v0.3.2\n",
            "[0.4.0] must link https://github.com/rob-c/xrdexample/compare/<previous>...v0.4.0",
        ),
    ],
)
def test_each_changelog_drift_is_named(tmp_path, changelog, problem):
    problems = release.checkout_problems(checkout(tmp_path, **{"CHANGELOG.md": changelog}))
    assert f"CHANGELOG.md: {problem}" in problems


def test_link_references_are_checked_only_where_the_changelog_keeps_them(tmp_path):
    linked = CHANGELOG + (
        "\n[0.4.0]: https://github.com/rob-c/xrdexample/compare/v0.3.2...v0.4.0\n"
        "[0.3.2]: https://github.com/rob-c/xrdexample/compare/v0.3.1...v0.3.2\n"
    )
    assert release.checkout_problems(checkout(tmp_path, **{"CHANGELOG.md": linked})) == []


def test_the_metadata_names_this_repository(tmp_path):
    drifted = (
        PYPROJECT.replace('readme = "README.md"', 'readme = "README.rst"')
        .replace('requires-python = ">=3.11"', 'requires-python = ">=3.10"')
        .replace('  "Programming Language :: Python :: 3.14",\n', "")
        .replace("rob-c/xrdexample/issues", "rob-c/other/issues")
    )
    root = checkout(tmp_path, **{"pyproject.toml": drifted, "README.md": "# Example\n"})
    (root / "COPYING").unlink()
    assert release.checkout_problems(root) == [
        'pyproject.toml: readme must be "README.md"',
        "README.md: the first line must be '# xrdexample'",
        "COPYING: missing (every package ships its license)",
        'pyproject.toml: requires-python must be ">=3.11"',
        "pyproject.toml: Python classifiers ['3.11', '3.12', '3.13'], "
        "not ['3.11', '3.12', '3.13', '3.14']",
        "pyproject.toml: [project.urls] Issues must be "
        "'https://github.com/rob-c/xrdexample/issues'",
    ]


def test_a_release_workflow_builds_through_the_shared_action(tmp_path):
    inline = PUBLISH.replace("rob-c/xrd-devtools/.github/actions/publish@main", "./build")
    untrusted = PUBLISH.replace("id-token: write", "contents: read")
    commented = "# - uses: pypa/gh-action-pypi-publish@release/v1\n" + PUBLISH.replace(
        "      - uses: pypa/gh-action-pypi-publish@release/v1\n", ""
    ).replace("id-token: write", "contents: read")
    where = ".github/workflows/publish.yml"
    root = checkout(tmp_path, **{".github__workflows__publish.yml": inline})
    assert release.checkout_problems(root) == [
        f"{where}: must build with {release.PUBLISH_ACTION} (package: xrdexample)"
    ]
    root = checkout(tmp_path, **{".github__workflows__publish.yml": untrusted})
    assert release.checkout_problems(root) == [
        f"{where}: the PyPI upload must use trusted publishing (id-token: write)"
    ]
    root = checkout(tmp_path, **{".github__workflows__publish.yml": commented})
    assert release.checkout_problems(root) == []
    (root / where).unlink()
    assert release.checkout_problems(root) == []  # no release workflow: nothing to check


def test_a_documentation_site_is_kept_out_of_the_sdist(tmp_path):
    root = checkout(tmp_path, **{"mkdocs.yml": "site_name: x\n"})
    assert release.checkout_problems(root) == [
        ".gitignore: must ignore site/ (mkdocs builds into it)"
    ]
    (root / ".gitignore").write_text("dist/\nsite/\n")
    assert release.checkout_problems(root) == []


def test_the_import_package_can_be_named(tmp_path):
    root = checkout(tmp_path)
    (root / "src/xrdexample").rename(root / "src/example")
    assert release.checkout_problems(root, "example") == [
        "pyproject.toml: [tool.hatch.version] path must be 'src/example/_version.py'"
    ]


def installed(
    monkeypatch: pytest.MonkeyPatch,
    version: str = "0.4.0",
    runtime: str = "0.4.0",
    requires: list[str] | None = None,
    present: tuple[str, ...] = (),
) -> None:
    """Fake the installed distribution ``xrdexample`` the publish job checks."""
    module = types.ModuleType("xrdexample")
    module.__dict__["__version__"] = runtime
    monkeypatch.setitem(sys.modules, "xrdexample", module)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: version)
    monkeypatch.setattr(importlib.metadata, "requires", lambda name: requires)
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name: object() if name in present else None
    )


DATED = CHANGELOG.replace("## [0.4.0] - Unreleased", "## [0.4.0] - 2026-10-20")


def test_an_installed_release_agrees_with_its_tag_and_changelog(monkeypatch):
    installed(monkeypatch, requires=['google-crc32c>=1.5; extra == "fast"'])
    wheel = release.Installed("xrdexample", "v0.4.0", DATED, "xrdexample", True, ("xrdclient",))
    assert release.installed_problems(wheel) == []


def test_each_installed_disagreement_is_named(monkeypatch):
    installed(monkeypatch, runtime="0.3.2", requires=["cryptography>=42"], present=("xrdclient",))
    wheel = release.Installed("xrdexample", "v0.4.1", CHANGELOG, "xrdexample", True, ("xrdclient",))
    assert release.installed_problems(wheel) == [
        "the tag must be v0.4.0",
        "xrdexample.__version__ is '0.3.2', the wheel's is 0.4.0",
        "CHANGELOG.md: date the 0.4.0 section before tagging",
        "a default dependency: cryptography>=42",
        "xrdclient is installed: xrdexample must not need it",
    ]
    no_section = release.Installed("xrdexample", "v0.4.0", "# Changelog\n", "xrdexample")
    assert "CHANGELOG.md has no 0.4.0 section" in release.installed_problems(no_section)


def test_dependencies_are_allowed_unless_none_are_promised(monkeypatch):
    installed(monkeypatch, requires=["cryptography>=42"])
    wheel = release.Installed("xrdexample", "v0.4.0", DATED, "xrdexample")
    assert release.installed_problems(wheel) == []


def test_the_command_line(tmp_path, monkeypatch, capsys):
    root = checkout(tmp_path)
    assert release.main(["check", str(root)]) == 0
    assert capsys.readouterr().out == f"release: {root} is consistent\n"
    (root / "COPYING").unlink()
    assert release.main(["check", str(root)]) == 1
    assert "COPYING: missing" in capsys.readouterr().out
    installed(monkeypatch)
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(DATED)
    argv = ["installed", "xrdexample", "--tag", "v0.4.0", "--changelog", str(changelog)]
    assert release.main([*argv, "--no-dependencies", "--absent", "xrdclient"]) == 0
    assert capsys.readouterr().out == "release: xrdexample is consistent\n"
    assert release.main([*argv[:3], "v9", *argv[4:], "--package", "xrdexample"]) == 1
    assert capsys.readouterr().out == "the tag must be v0.4.0\n"


def test_it_runs_as_a_plain_script(tmp_path, monkeypatch, capsys):
    """The publish action runs ``python release.py`` where xrddevtools is not installed."""
    root = checkout(tmp_path)
    monkeypatch.setattr(sys, "argv", ["release.py", "check", str(root)])
    with pytest.raises(SystemExit) as exited:
        runpy.run_path(release.__file__, run_name="__main__")
    assert exited.value.code == 0
    assert "is consistent" in capsys.readouterr().out
