"""The per-function maintainability gate: each metric on hand-checked functions."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from xrddevtools import maintainability as mt


def _function(source: str) -> ast.FunctionDef:
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.FunctionDef)
    return node


def test_all_five_metrics_are_reported_per_function() -> None:
    source = """
def choices(x):
    if x > 0:
        for value in range(x):
            if value % 2:
                return value
    else:
        return -x
    return 0
"""
    (measured,) = mt.analyze_source(source, "sample.py")

    assert measured.key == "sample.py::choices"
    assert measured.ccn == 4
    assert measured.cognitive == 7
    assert measured.npath == 5
    assert measured.halstead_volume > 0
    assert measured.max_nesting == 3


def test_npath_multiplies_independent_branches() -> None:
    node = _function(
        """
def independent(a, b):
    if a:
        use(a)
    if b:
        use(b)
"""
    )

    assert mt.npath_complexity(node) == 4
    assert mt.max_nesting_depth(node) == 1


def test_elif_does_not_inflate_lexical_nesting() -> None:
    node = _function(
        """
def select(value):
    if value == 1:
        return "one"
    elif value == 2:
        return "two"
    else:
        return "other"
"""
    )

    assert mt.max_nesting_depth(node) == 1
    assert mt.npath_complexity(node) == 3


def test_nested_functions_are_scored_separately() -> None:
    source = """
def outer(x):
    def inner(y):
        if y:
            return y + 1
        return 0
    return inner(x)
"""
    outer, inner = mt.analyze_source(source, "nested.py")

    assert outer.key == "nested.py::outer"
    assert outer.ccn == 1
    assert outer.cognitive == 0
    assert outer.npath == 1
    assert inner.key == "nested.py::outer.inner"
    assert inner.ccn == 2
    assert inner.cognitive == 1
    assert inner.npath == 2


def test_absolute_limits_apply_to_every_function() -> None:
    limits = {
        "ccn": 1,
        "cognitive": 0,
        "npath": 1,
        "halstead_volume": 1_000_000,
        "max_nesting": 1,
    }
    functions = mt.analyze_source("def f():\n    if ready():\n        return 1\n", "x.py")
    report = mt.Report((), functions)

    failures = mt.violations(report, limits)

    assert {failure.metric for failure in failures} == {"ccn", "cognitive", "npath"}
    assert {failure.limit for failure in failures} == {0, 1}
    assert mt.violation_counts(failures) == {
        "ccn": 1,
        "cognitive": 1,
        "npath": 1,
        "halstead_volume": 0,
        "max_nesting": 0,
    }


def test_hotspot_report_contains_only_functions_over_a_limit() -> None:
    functions = mt.analyze_source(
        "def good():\n    return 1\n\ndef bad(x):\n    if x:\n        return 1\n",
        "x.py",
    )
    limits = dict.fromkeys(mt.METRICS, 1_000_000)
    limits["ccn"] = 1
    report = mt.Report((), functions)

    rendered = mt.render_hotspots_table(report, limits)

    assert "x.py::bad" in rendered
    assert "x.py::good" not in rendered


def test_policy_file_discovery_honours_exclusions(tmp_path: Path) -> None:
    (tmp_path / "code").mkdir()
    included = tmp_path / "code" / "included.py"
    excluded = tmp_path / "code" / "generated.py"
    included.write_text("def included():\n    pass\n", encoding="utf-8")
    excluded.write_text("def generated():\n    pass\n", encoding="utf-8")
    policy = mt.Policy(
        root=tmp_path,
        paths=("code",),
        exclude=("code/generated.py",),
        limits=dict.fromkeys(mt.METRICS, 1),
    )

    assert list(mt.iter_python_files(policy)) == [included]


def _npath(source: str) -> int:
    return mt.npath_complexity(_function(source))


def _depth(source: str) -> int:
    return mt.max_nesting_depth(_function(source))


@pytest.mark.parametrize(
    ("source", "paths"),
    [
        # Short-circuit: either operand can end the expression.
        ("def f(a, b):\n    return a and b\n", 2),
        # A conditional expression: test, then one of two values.
        ("def f(a):\n    return 1 if a else 2\n", 2),
        # A comprehension's ``for`` runs or not, its filter passes or not.
        ("def f(xs):\n    return [x for x in xs if x]\n", 4),
        ("def f(xs):\n    return {x for x in xs}\n", 2),
        ("def f(xs):\n    return {k: v for k, v in xs}\n", 2),
        ("def f(xs):\n    return sum(x for x in xs)\n", 2),
        # A lambda body is a function of its own.
        ("def f():\n    return lambda a: a or 1\n", 1),
        # Loops: body, ``else`` or no further iteration.
        ("def f(a):\n    while a:\n        a -= 1\n    else:\n        pass\n", 3),
        ("def f(xs):\n    for x in xs:\n        pass\n", 3),
        # try: success or one handler, then finally.
        (
            "def f():\n    try:\n        a()\n    except E:\n        b()\n"
            "    except F:\n        c()\n    else:\n        d()\n    finally:\n        e()\n",
            3,
        ),
        ("def f():\n    try:\n        a()\n    except* E:\n        b()\n", 2),
        # match: each case, plus falling through without ``case _``.
        ("def f(x):\n    match x:\n        case 1:\n            pass\n", 2),
        (
            "def f(x):\n    match x:\n        case 1 if x or y:\n            pass\n"
            "        case _:\n            pass\n",
            4,
        ),
        ("def f():\n    with a() if b else c():\n        pass\n", 2),
        ("async def f():\n    async with a():\n        pass\n", 1),
        # Nested definitions are measured separately.
        (
            "def f():\n    def g():\n        if x:\n            pass\n    class C:\n        pass\n",
            1,
        ),
    ],
)
def test_npath_counts_each_kind_of_branch(source, paths):
    node = ast.parse(source).body[0]
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    assert mt.npath_complexity(node) == paths


def test_npath_saturates_instead_of_growing_without_bound():
    body = "".join(f"    if a{i}:\n        pass\n" for i in range(45))
    assert _npath(f"def f():\n{body}") == mt.NPATH_SATURATION


@pytest.mark.parametrize(
    ("source", "depth"),
    [
        ("def f():\n    return 1\n", 0),
        # An ``else`` holding more than a lone ``if`` nests one level deeper.
        (
            "def f(a):\n    if a:\n        pass\n    else:\n        a = 1\n"
            "        if a:\n            pass\n",
            2,
        ),
        ("def f(a):\n    return 1 if a else (2 if a else 3)\n", 2),
        ("def f(xs):\n    return [y for x in xs if x for y in x]\n", 2),
        ("def f(xs):\n    return {k: v for k, v in xs}\n", 1),
        ("def f(a):\n    while a:\n        with a:\n            pass\n", 2),
        (
            "def f():\n    g = lambda: [x for x in y]\n"
            "    def h():\n        if a:\n            pass\n",
            0,
        ),
    ],
)
def test_nesting_counts_blocks_expressions_and_comprehensions(source, depth):
    assert _depth(source) == depth


def test_methods_async_functions_and_nested_classes_are_measured_on_their_own():
    source = """
class Outer:
    async def method(self):
        class Inner:
            def helper(self):
                return 1
        async def inner():
            return 2
        return Inner, inner
"""
    keys = [m.key for m in mt.analyze_source(source, "c.py")]
    assert keys == [
        "c.py::Outer.method",
        "c.py::Outer.method.Inner.helper",
        "c.py::Outer.method.inner",
    ]


def test_repeated_names_get_distinct_keys():
    source = "def f():\n    pass\n\ndef f():\n    pass\n"
    assert [m.key for m in mt.analyze_source(source, "r.py")] == ["r.py::f", "r.py::f#2"]


def _policy_file(tmp_path: Path, **overrides: object) -> Path:
    data: dict[str, object] = {
        "schema": 1,
        "paths": ["code"],
        "exclude": [],
        "limits": dict(mt.STANDARD_LIMITS),
    }
    data.update(overrides)
    path = tmp_path / "maintainability.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_policies_are_validated_and_compared_with_the_standard(tmp_path):
    with pytest.raises(ValueError, match="schema"):
        mt.load_policy(_policy_file(tmp_path, schema=2))
    with pytest.raises(ValueError, match="missing limits for: npath"):
        limits = {k: v for k, v in mt.STANDARD_LIMITS.items() if k != "npath"}
        mt.load_policy(_policy_file(tmp_path, limits=limits))
    policy = mt.load_policy(_policy_file(tmp_path))
    assert policy.root == tmp_path.resolve() and policy.paths == ("code",)
    assert mt.policy_problems(policy) == []
    stricter = mt.load_policy(_policy_file(tmp_path, limits={**mt.STANDARD_LIMITS, "ccn": 5}))
    assert mt.policy_problems(stricter) == []
    looser = mt.load_policy(
        _policy_file(
            tmp_path, limits={**mt.STANDARD_LIMITS, "halstead_volume": 600.5}, exclude=["x.py"]
        )
    )
    assert mt.policy_problems(looser) == [
        "limit halstead_volume=600.500 is looser than the standard 500",
        "exclusion 'x.py': every file is measured",
    ]


def test_requested_paths_are_deduplicated_and_must_exist(tmp_path):
    (tmp_path / "code" / "pkg").mkdir(parents=True)
    module = tmp_path / "code" / "pkg" / "a.py"
    module.write_text("def a():\n    pass\n", encoding="utf-8")
    policy = mt.load_policy(_policy_file(tmp_path))
    found = list(mt.iter_python_files(policy, ["code", str(module), "code/pkg"]))
    assert found == [module]
    (tmp_path / "notes.txt").write_text("", encoding="utf-8")
    for missing in ("nowhere", "notes.txt"):
        with pytest.raises(FileNotFoundError, match=missing):
            list(mt.iter_python_files(policy, [missing]))


def _project(tmp_path: Path, body: str, **overrides: object) -> Path:
    (tmp_path / "code").mkdir(exist_ok=True)
    (tmp_path / "code" / "m.py").write_text(body, encoding="utf-8")
    (tmp_path / "code" / "empty.py").write_text("X = 1\n", encoding="utf-8")
    return _policy_file(tmp_path, **overrides)


SIMPLE = "def simple():\n    return 1\n"
COMPLEX = "def tangled(a, b, c):\n" + "".join(
    f"    if a > {i}:\n        b += {i}\n" for i in range(12)
)


def test_analyze_rolls_files_up_and_reports_render_every_format(tmp_path):
    policy = mt.load_policy(_project(tmp_path, SIMPLE + "\n\n" + COMPLEX))
    report = mt.analyze(policy)
    assert [f.path for f in report.files] == ["code/empty.py", "code/m.py"]
    empty, module = report.files
    assert (empty.functions, empty.ccn, empty.halstead_volume) == (0, 0, 0)
    assert module.functions == 2 and module.ccn == 13
    table = mt.render_table(report, policy.limits, top=1)
    assert "top 1 functions" in table and "code/m.py::tangled" in table
    assert "code/m.py::simple" not in table
    assert "all functions" in mt.render_table(report, policy.limits, top=0)
    document = json.loads(mt.render_json(report))
    assert document["npath_saturation"] == mt.NPATH_SATURATION
    assert [f["name"] for f in document["functions"]] == ["simple", "tangled"]
    rows = mt.render_csv(report).splitlines()
    assert rows[0].startswith("key,path,name,line") and len(rows) == 3
    hotspots = json.loads(mt.render_hotspots_json(report, policy.limits))
    assert hotspots["summary"]["hotspot_functions"] == 1
    assert hotspots["functions"][0]["failed_metrics"] == ["ccn", "npath"]
    csv_rows = mt.render_hotspots_csv(report, policy.limits).splitlines()
    assert csv_rows[1].startswith("code/m.py::tangled,") and csv_rows[1].endswith('"ccn,npath"')


def test_check_passes_lists_every_violation_and_refuses_a_loose_policy(tmp_path, capsys):
    config = str(_project(tmp_path, SIMPLE))
    assert mt.main(["--config", config, "check"]) == 0
    assert capsys.readouterr().out == "maintainability: 1 functions in 2 files pass\n"
    _project(tmp_path, SIMPLE, exclude=["code/empty.py"])
    assert mt.main(["--config", config, "check"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "maintainability policy: exclusion 'code/empty.py'" in captured.err
    _project(tmp_path, COMPLEX)
    assert mt.main(["--config", config, "check", "code/m.py"]) == 1
    err = capsys.readouterr().err
    assert "Maintainability violations: 2 metrics across 1 functions" in err
    assert "  code/m.py::tangled: ccn=13 > limit 10" in err
    assert "xrddevtools maintainability hotspots" in err


@pytest.mark.parametrize("command", ["report", "hotspots"])
@pytest.mark.parametrize("fmt", ["table", "json", "csv"])
def test_report_and_hotspots_print_or_write_each_format(tmp_path, capsys, command, fmt):
    config = str(_project(tmp_path, COMPLEX))
    assert mt.main(["--config", config, command, "--format", fmt]) == 0
    printed = capsys.readouterr().out
    assert "tangled" in printed and printed.endswith("\n") and not printed.endswith("\n\n")
    output = tmp_path / f"out.{fmt}"
    assert mt.main(["--config", config, command, "--format", fmt, "--output", str(output)]) == 0
    assert capsys.readouterr().out == ""
    assert output.read_text(encoding="utf-8").rstrip("\n") == printed.rstrip("\n")
