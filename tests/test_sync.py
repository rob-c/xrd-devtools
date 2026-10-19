"""``xrddevtools sync``: the standard written into managed blocks, and drift from it found."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from xrddevtools import sync
from xrddevtools.workspace import REPOSITORIES

#: A library's pyproject.toml with every managed block's markers and nothing in them.
SKELETON = """[project]
name = "xrdcore"

[tool.pytest.ini_options]
testpaths = ["tests"]
{pytest-warnings}
{coverage}

{ruff}

{banned-api}

{mypy}
files = ["src", "tests"]

{mypy-overrides}

{complexipy}
"""


def markers(style: sync.Style, name: str) -> str:
    """An empty managed block."""
    return f"{style.begin(name)}\n{style.end(name)}"


def workspace(tmp_path: Path, name: str = "xrdcore") -> Path:
    """A checkout of ``name`` with empty managed blocks in both files and Python in two trees."""
    root = tmp_path / name
    (root / "src" / name).mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / name / "__init__.py").write_text('"""A package."""\n')
    (root / "tests" / "test_it.py").write_text('"""A test."""\n')
    blocks = {block: markers(sync.TOML, block) for block in sync.PYPROJECT_BLOCKS}
    (root / "pyproject.toml").write_text(SKELETON.format_map(blocks))
    sections = "\n\n".join(
        f"## {block}\n\n{markers(sync.MARKDOWN, block)}"
        for block in ("code-standard", "banned-apis", "devtools")
    )
    (root / "CONTRIBUTING.md").write_text(f"# Contributing to {name}\n\n{sections}\n")
    return root


def declared(name: str) -> sync.Repository:
    """The declarations of ``name`` (its trees read from a directory that has none)."""
    return sync.repository(Path("/nonexistent") / name)


def test_every_repository_of_the_workspace_is_in_sync():
    """The real checkouts beside this one, where the workspace has them."""
    root = Path(__file__).resolve().parents[2]
    for checkout in [root / name for name in ("xrdcore", "xrdclient", "xrd-devtools")]:
        if (checkout / "pyproject.toml").is_file():
            assert sync.drift(checkout) == [], checkout.name


def test_sync_fills_empty_blocks_and_the_result_is_valid_and_stable(tmp_path):
    root = workspace(tmp_path)
    assert sync.sync(root, write=False) == [
        # In the order the blocks appear in the file.
        "xrdcore/pyproject.toml: would update pytest-warnings, coverage, ruff, banned-api, "
        "mypy, mypy-overrides, complexipy",
        "xrdcore/CONTRIBUTING.md: would update code-standard, banned-apis, devtools",
    ]
    assert sync.drift(root) != []
    assert sync.sync(root, write=True)[0].startswith("xrdcore/pyproject.toml: updated ")
    assert sync.drift(root) == []
    assert sync.sync(root, write=True) == []
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    assert pyproject["tool"]["mypy"]["files"] == ["src", "tests"]  # the repository's own key
    assert pyproject["tool"]["mypy"]["strict"] is True
    assert pyproject["tool"]["complexipy"]["paths"] == ["src", "tests"]
    assert pyproject["tool"]["coverage"]["run"]["source"] == ["xrdcore"]
    banned = pyproject["tool"]["ruff"]["lint"]["flake8-tidy-imports"]["banned-api"]
    assert "concurrent.futures.ThreadPoolExecutor" not in banned  # xrdcore owns fan-out
    assert banned["xrdsec"]["msg"].startswith("xrdcore sits below xrdsec")


def test_a_hand_edit_inside_a_block_is_drift_and_sync_restores_it(tmp_path):
    root = workspace(tmp_path)
    sync.sync(root, write=True)
    path = root / "pyproject.toml"
    path.write_text(
        path.read_text().replace("max-complexity-allowed = 15", "max-complexity-allowed = 30")
    )
    assert sync.drift(root) == [
        (
            "pyproject.toml",
            "managed block 'complexipy' differs from the standard: run `xrddevtools sync`",
        )
    ]
    assert sync.sync(root, write=True) == ["xrdcore/pyproject.toml: updated complexipy"]
    assert "max-complexity-allowed = 15" in path.read_text()


def test_a_reworded_begin_marker_is_restored(tmp_path):
    root = workspace(tmp_path)
    sync.sync(root, write=True)
    path = root / "CONTRIBUTING.md"
    path.write_text(
        path.read_text().replace(
            sync.MARKDOWN.begin("devtools"), "<!-- >>> xrddevtools sync: devtools >>> -->"
        )
    )
    assert sync.sync(root, write=False) == ["xrdcore/CONTRIBUTING.md: would update devtools"]


def test_missing_and_unknown_blocks_are_problems_and_nothing_is_written(tmp_path):
    root = workspace(tmp_path)
    path = root / "CONTRIBUTING.md"
    text = path.read_text().replace(
        markers(sync.MARKDOWN, "devtools"), markers(sync.MARKDOWN, "extra")
    )
    path.write_text(text)
    report = sync.sync(root, write=True)
    assert report[1].startswith(
        "xrdcore/CONTRIBUTING.md: no managed block 'devtools': add its markers"
    )
    assert report[2] == "xrdcore/CONTRIBUTING.md: block 'extra' is not part of the standard"
    assert path.read_text() == text  # a file with a problem is never written


def test_a_missing_file_is_a_problem(tmp_path):
    root = workspace(tmp_path)
    sync.sync(root, write=True)
    (root / "CONTRIBUTING.md").unlink()
    assert sync.drift(root) == [
        (
            "CONTRIBUTING.md",
            "missing: create it with the markers of ['banned-apis', 'code-standard', 'devtools']",
        )
    ]


def test_a_result_that_would_not_parse_is_refused(tmp_path):
    root = workspace(tmp_path)
    path = root / "pyproject.toml"
    path.write_text(path.read_text().replace('files = ["src", "tests"]', "files = ["))
    problems = [problem for _, problem in sync.drift(root)]
    assert problems[0].startswith("would not be valid TOML: ")


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ([sync.TOML.end("ruff")], "line 1: unexpected <<< marker of block 'ruff'"),
        ([sync.TOML.begin("ruff"), sync.TOML.begin("mypy")], "line 2: unexpected >>> marker"),
        ([sync.TOML.begin("ruff"), sync.TOML.end("mypy")], "line 2: unexpected <<< marker"),
        ([sync.TOML.begin("ruff"), "x = 1"], "block 'ruff' is never closed"),
        ([markers(sync.TOML, "ruff")] * 2, "blocks ['ruff'] appear more than once"),
    ],
)
def test_markers_that_do_not_pair_up_are_refused(lines, message):
    text = "\n".join(lines)
    with pytest.raises(sync.SyncError, match=message.replace("[", r"\[").replace("]", r"\]")):
        sync.spans(text.splitlines())
    outcome = sync.apply(text, sync.TOML, {})
    assert outcome.text == text and outcome.changed == () and len(outcome.problems) == 1


def test_markers_are_comments_in_either_format():
    assert sync.TOML.begin("ruff").startswith("# >>> xrddevtools sync: ruff ")
    assert sync.MARKDOWN.end("devtools") == "<!-- <<< xrddevtools sync: devtools <<< -->"
    text = f"a\n{sync.TOML.begin('x')}\nold\n{sync.TOML.end('x')}\nb"
    outcome = sync.apply(text, sync.TOML, {"x": "new\n"})
    assert outcome.text == f"a\n{sync.TOML.begin('x')}\nnew\n{sync.TOML.end('x')}\nb"
    assert outcome.changed == ("x",)


def test_toml_values():
    assert sync.toml_value(True) == "true" and sync.toml_value(False) == "false"
    assert sync.toml_value(15) == "15"
    assert sync.toml_value(["a", 'b"c', "\\.$"]) == '["a", "b\\"c", "\\\\.$"]'
    assert sync.toml_value(("x",)) == '["x"]'


def test_layering_names_each_relation():
    assert sync.relation(declared("xrdcore"), "xrdclient") == "below"
    assert sync.relation(declared("xrdsec"), "xrdhttp") == "below"  # xrdhttp[testing] needs it
    assert sync.relation(declared("xrdclient"), "xrdarc") == "beside"
    assert sync.relation(declared("xrd-devtools"), "xrdcore") == "tooling"
    assert set(sync.layering(declared("xrdwms"))) == set()  # it may import every one
    assert set(sync.layering(declared("xrd-devtools"))) == set(REPOSITORIES)


def test_two_libraries_neither_built_on_the_other_are_peers(monkeypatch):
    real = sync.declarations()
    repositories = dict(real["repositories"])
    repositories["xrdhttp"] = {**repositories["xrdhttp"], "depends-on": ["xrdcore"]}
    monkeypatch.setattr(sync, "declarations", lambda: {**real, "repositories": repositories})
    assert sync.relation(declared("xrdsec"), "xrdhttp") == "peer"
    assert sync.layering(declared("xrdsec"))["xrdhttp"].startswith("xrdsec and xrdhttp never")


def test_bans_follow_the_role_less_what_a_repository_owns():
    xrdhttp = sync.bans(declared("xrdhttp"))
    assert "boto3" in xrdhttp and "botocore" not in xrdhttp  # owns a name of a group
    assert xrdhttp["gssapi"].endswith("(session Auth)")  # its own wording
    xrdsec = sync.bans(declared("xrdsec"))
    assert "gssapi" not in xrdsec  # owns a whole group
    xrdcore = sync.bans(declared("xrdcore"))
    assert xrdcore["ssl"].startswith("xrdcore speaks no protocol")  # a ban of its own
    client = sync.bans(declared("xrdarc"))
    assert "ssl.create_default_context" in client and "xrdclient" in client


def test_the_overrides_block_without_any_override():
    text = sync.mypy_overrides_block(declared("xrdarc"))
    assert text.startswith("# No override")
    assert "ignore_missing_imports" in sync.mypy_overrides_block(declared("xgfalclient"))
    assert 'module = ["tests.*"]' in sync.mypy_overrides_block(declared("xrdcore"))


def test_coverage_follows_threads_where_declared():
    assert 'concurrency = ["thread"]' in sync.coverage_block(declared("xrdarc"))
    assert "concurrency" not in sync.coverage_block(declared("xrdcore"))


def test_contributing_blocks_follow_the_role():
    assert list(sync.contributing_blocks(declared("xrd-devtools"))) == [
        "code-standard",
        "banned-apis",
    ]
    rows = sync.ban_rows(declared("xrdsec"))
    assert rows[-1].startswith("| `xrdhttp`, `xrdclient`, `xgfalclient`, `xrdarc`, `xrdwms` | - |")
    assert any(row.startswith("| `urllib3`, `requests`, `httpx` | xrdsec takes") for row in rows)


def test_rewrap_fills_paragraphs_and_bullets_and_leaves_tables():
    text = "one two\nthree\n\n- a b\n  c\n- d\n\n| x |\n|---|\n"
    assert sync.rewrap(text, width=6) == "one\ntwo\nthree\n\n- a b\n  c\n- d\n\n| x |\n|---|\n"
    assert sync.rewrap("| a | b |c|", width=4) == "| a | b |c|\n"


def test_an_unknown_repository_is_an_error(tmp_path):
    with pytest.raises(sync.SyncError, match="not a repository of the chain"):
        sync.repository(tmp_path / "xrdother")
    assert sync.drift(tmp_path / "xrdother") == [
        ("repositories.toml", "xrdother: not a repository of the chain (repositories.toml)")
    ]


def test_the_command_line(tmp_path, capsys):
    root = workspace(tmp_path)
    assert sync.main(["--repo", str(root), "--check"]) == 1
    assert "would update" in capsys.readouterr().out
    assert sync.main(["--workspace", str(tmp_path)]) == 0
    assert "updated" in capsys.readouterr().out
    assert sync.main(["--workspace", str(tmp_path), "--check"]) == 0
    assert capsys.readouterr().out == "sync: 1 repositories are up to date\n"
    (root / "CONTRIBUTING.md").unlink()
    assert sync.main(["--repo", str(root)]) == 1
    assert "CONTRIBUTING.md: missing" in capsys.readouterr().out


def test_usage_errors(tmp_path, capsys):
    assert sync.main(["--workspace", str(tmp_path)]) == 2
    assert "no repository of the chain" in capsys.readouterr().err
    other = tmp_path / "xrdother"
    other.mkdir()
    assert sync.main(["--repo", str(other)]) == 2
    assert "not a repository of the chain" in capsys.readouterr().err
