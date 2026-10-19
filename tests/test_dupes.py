"""The duplicate gate: copies across repositories fail, idioms and drift do not."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from xrddevtools import dupes

#: A body long enough to count (six statements), as a repository would write it.
ORIGINAL = '''
def checksum_of(path, block=65536):
    """The adler32 of a file."""
    total = 1
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(block)
            if not chunk:
                break
            total = zlib.adler32(chunk, total)
    return total
'''
#: The same function copied into another repository and renamed throughout.
RENAMED_COPY = """
def adler_for(name: str, size: int = 65536) -> int:
    value = 1
    with open(name, "rb") as stream:
        while True:
            piece = stream.read(size)
            if not piece:
                break
            value = zlib.adler32(piece, value)
    return value
"""
#: The copy after it began to drift: one call and one number differ.
DRIFTED = """
def checksum_of(path, block=65536):
    total = 1
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(block * 4)
            if not chunk:
                break
            total = zlib.adler32(chunk, total) & 0xFFFFFFFF
    return total
"""


def repo(workspace: Path, name: str, files: dict[str, str]) -> Path:
    """A checkout of ``name`` holding ``files`` (paths relative to the checkout)."""
    root = workspace / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n')
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source))
    return root


def run(workspace: Path, *extra: str) -> dupes.Findings:
    """The findings ``xrddevtools dupes --workspace`` would report."""
    return dupes.find(dupes.parser().parse_args(["--workspace", str(workspace), *extra]))


def test_a_renamed_copy_in_another_repository_fails_the_gate(tmp_path, capsys):
    repo(tmp_path, "xrdcore", {"src/xrdcore/sums.py": ORIGINAL})
    repo(tmp_path, "xrdarc", {"src/xrdarc/util.py": RENAMED_COPY})
    found = run(tmp_path)
    assert found.failed
    ((first, second),) = found.duplicates
    assert (first.repo, first.name, second.repo, second.name) == (
        "xrdcore",
        "checksum_of",
        "xrdarc",
        "adler_for",
    )
    assert first.statements == 8
    assert dupes.main(["--workspace", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "error: duplicate (8 statements): xrdcore/src/xrdcore/sums.py:2 checksum_of" in out
    assert out.rstrip().endswith(
        "dupes FAILED: 1 duplicated functions, 0 near-duplicates (0 failing), 0 unparsable files"
    )


def test_copies_within_one_repository_and_short_idioms_are_not_the_gates_business(tmp_path):
    repo(tmp_path, "xrdcore", {"src/a.py": ORIGINAL, "src/b.py": RENAMED_COPY})
    idiom = "def get(self):\n    return self._value\n"
    repo(tmp_path, "xrdsec", {"src/c.py": idiom})
    repo(tmp_path, "xrdhttp", {"src/d.py": idiom})
    assert not run(tmp_path).failed


def test_drift_close_enough_to_a_copy_fails_the_gate(tmp_path, capsys):
    repo(tmp_path, "xrdcore", {"src/xrdcore/sums.py": ORIGINAL})
    repo(tmp_path, "xgfalclient", {"src/xgfalclient/sums.py": DRIFTED})
    found = run(tmp_path)
    assert not found.duplicates
    (near,) = found.near
    assert near.first.repo == "xrdcore" and near.second.repo == "xgfalclient"
    assert dupes.NEAR_FAILURE <= near.similarity < 1
    assert found.fail_near == dupes.NEAR_FAILURE
    assert found.failing_near == (near,) and found.failed
    assert dupes.main(["--workspace", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert f"error: near-duplicate ({near.similarity:.2f}): xrdcore/" in out
    assert out.rstrip().endswith("1 near-duplicates (1 failing), 0 unparsable files")


def test_drift_below_the_failure_threshold_is_a_warning_with_its_similarity(tmp_path, capsys):
    repo(tmp_path, "xrdcore", {"src/xrdcore/sums.py": ORIGINAL})
    repo(tmp_path, "xgfalclient", {"src/xgfalclient/sums.py": DRIFTED})
    (near,) = run(tmp_path).near
    above = run(tmp_path, "--fail-near", "0.99")
    assert above.near == (near,) and not above.failed
    assert dupes.main(["--workspace", str(tmp_path), "--fail-near", "0.99"]) == 0
    assert "warning: near-duplicate (0.9" in capsys.readouterr().out
    # Raising the warning bar above the pair's similarity silences it - unless
    # it would fail: what fails is always reported.
    assert run(tmp_path, "--similarity", "0.99", "--fail-near", "0.99").near == ()
    assert run(tmp_path, "--similarity", "0.99").near == (near,)


def test_json_says_which_near_duplicates_fail(tmp_path, capsys):
    repo(tmp_path, "xrdcore", {"src/xrdcore/sums.py": ORIGINAL})
    repo(tmp_path, "xgfalclient", {"src/xgfalclient/sums.py": DRIFTED})
    assert dupes.main(["--workspace", str(tmp_path), "--json"]) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["failed"] is True
    assert [near["failing"] for near in document["near_duplicates"]] == [True]


def test_dunders_and_different_names_are_not_compared_for_drift(tmp_path):
    repo(tmp_path, "xrdcore", {"src/a.py": DRIFTED.replace("checksum_of", "__call__")})
    repo(tmp_path, "xrdsec", {"src/b.py": ORIGINAL.replace("checksum_of", "__call__")})
    repo(tmp_path, "xrdhttp", {"src/c.py": DRIFTED.replace("checksum_of", "other")})
    assert run(tmp_path).near == ()


def test_src_and_tests_are_scanned_unless_the_trees_are_narrowed(tmp_path):
    # A test harness copied between two suites is a copy too.
    repo(tmp_path, "xrdcore", {"tests/helpers.py": ORIGINAL, "benchmarks/b.py": ORIGINAL})
    repo(tmp_path, "xrdsec", {"tests/helpers.py": ORIGINAL, "benchmarks/b.py": ORIGINAL})
    assert run(tmp_path).failed
    assert not run(tmp_path, "--tree", "src").failed
    assert len(run(tmp_path, "--tree", "benchmarks", "--tree", "tests").duplicates) == 1
    assert not run(tmp_path, "--tree", "tests", "--repo", "xrdcore").failed
    assert not run(tmp_path, "--tree", "tests", "--min-statements", "9").failed


def test_a_file_that_does_not_parse_fails_the_gate(tmp_path, capsys):
    repo(tmp_path, "xrdcore", {"src/broken.py": "def broken(:\n"})
    found = run(tmp_path)
    assert found.failed and found.unparsable == ("xrdcore/src/broken.py",)
    assert dupes.main(["--workspace", str(tmp_path)]) == 1
    assert "error: cannot parse xrdcore/src/broken.py" in capsys.readouterr().out


def test_json_names_every_copy_without_the_token_streams(tmp_path, capsys):
    repo(tmp_path, "xrdcore", {"src/a.py": ORIGINAL})
    repo(tmp_path, "xrdsec", {"src/b.py": RENAMED_COPY})
    repo(tmp_path, "xrdhttp", {"src/c.py": DRIFTED})
    assert dupes.main(["--workspace", str(tmp_path), "--json"]) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["failed"] is True
    assert [f["repo"] for f in document["duplicates"][0]] == ["xrdcore", "xrdsec"]
    assert "tokens" not in document["duplicates"][0][0]
    assert {n["second"]["repo"] for n in document["near_duplicates"]} == {"xrdhttp"}
    assert document["unparsable"] == []


def test_normalisation_keeps_what_changes_behaviour():
    def fingerprint(source: str) -> str:
        # The outermost function (ast.walk visits it before any nested one).
        return dupes.functions_in(textwrap.dedent(source), "r", "p", 1)[0].fingerprint

    base = "def f(a):\n    x = a.read(4)\n    return x\n"
    assert fingerprint(base) == fingerprint("def g(b: int):\n    y = b.read(4)\n    return y\n")
    # Parameters of nested functions and lambdas are names like any other.
    nested = "def f():\n    def g(a: int):\n        return a\n    return lambda b: b\n"
    renamed = "def f():\n    def g(c: str):\n        return c\n    return lambda d: d\n"
    assert fingerprint(nested) == fingerprint(renamed)
    assert fingerprint(base) != fingerprint("def f(a):\n    x = a.write(4)\n    return x\n")
    assert fingerprint(base) != fingerprint("def f(a):\n    x = a.read(5)\n    return x\n")
    keyword = "def f(a):\n    x = a.read(size=4, **a)\n    return x\n"
    assert fingerprint(keyword) != fingerprint(keyword.replace("size", "count"))
    (function,) = dupes.functions_in(keyword, "r", "p", 1)
    assert "size" in function.tokens
    assert fingerprint('def f():\n    """Doc."""\n    return "a"\n') == fingerprint(
        "def f():\n    return 'b'\n"
    )


def test_similarity_short_cuts_pairs_that_cannot_reach_the_floor():
    (long,) = dupes.functions_in(textwrap.dedent(ORIGINAL), "r", "p")
    (short,) = dupes.functions_in("def f():\n    return 1\n", "r", "p", 1)
    assert dupes.similarity(long, short, 0.8) < 0.8
    assert dupes.similarity(long, long) == 1.0


@pytest.mark.parametrize("bad", [["--workspace"], []])
def test_the_workspace_is_required(bad):
    with pytest.raises(SystemExit):
        dupes.parser().parse_args(bad)


def test_the_workspace_layout_skips_caches_and_names_the_trees_that_hold_python(tmp_path):
    from xrddevtools import workspace

    root = repo(
        tmp_path, "xrdcore", {"src/a.py": "", "src/__pycache__/a.py": "", "tests/t.pyi": ""}
    )
    (root / "src" / "notes.pyc").write_bytes(b"")
    found = [p.relative_to(root).as_posix() for p in workspace.python_files(root / "src")]
    assert found == ["src/a.py"]
    assert workspace.python_trees(root) == ["src", "tests"]
    assert list(workspace.python_files(root / "missing")) == []
    assert workspace.checkouts(tmp_path) == [root]
