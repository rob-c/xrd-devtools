"""The take-over scorecard's measurements, each on small, hand-checked sources.

:mod:`xrddevtools.scorecard` is what every repository of the chain is gated
on, and what xrdclient's ``docs/maintainability.md`` quotes when it says the
family is easier to take over than the industry median, so every number it
reports is pinned here against a source small enough to count by hand.
"""

from __future__ import annotations

import ast
import json
import textwrap
from collections import Counter
from pathlib import Path

import pytest

from xrddevtools import scorecard as sc


def _measure(tmp_path: Path, source: str, name: str = "sample.py") -> dict[str, object]:
    """Measure one throwaway module written from ``source``."""
    path = tmp_path / name
    path.write_text(textwrap.dedent(source))
    measured: dict[str, object] = sc.measure([tmp_path])
    return measured


def test_explanation_density_counts_comments_and_docstrings(tmp_path: Path) -> None:
    measured = _measure(
        tmp_path,
        '''
        """Module docstring."""

        # a comment line
        def f(x: int) -> int:
            """Two
            lines."""
            return x  # trailing comment
        ''',
    )
    # Code lines: ``def`` and ``return`` (the trailing comment shares one).
    # Explanation lines: module docstring, comment, 2 docstring lines, trailing.
    assert measured["code_lines"] == 2
    assert measured["explanation_density"] == 2.5


def test_docstring_and_annotation_coverage(tmp_path: Path) -> None:
    measured = _measure(
        tmp_path,
        """
        class Public:
            def method(self, a: int) -> None:
                '''Documented.'''

            def _private(self, a):
                pass

        def bare(a: int):
            pass
        """,
    )
    # Documentable: module, Public, method, bare (``_private`` is not public).
    assert measured["docstring_coverage_pct"] == 25.0
    # Annotated: only ``method`` (``bare`` lacks a return, ``_private`` all).
    assert measured["annotation_coverage_pct"] == pytest.approx(33.33, abs=0.01)


def test_parameter_count_ignores_self(tmp_path: Path) -> None:
    measured = _measure(
        tmp_path,
        """
        class C:
            def five(self, a, b, c, d, e): pass

        def six(a, b, c, d, e, f): pass
        """,
    )
    assert measured["many_parameter_functions_pct"] == 50.0


def test_nesting_counts_blocks_and_treats_elif_as_one_level() -> None:
    deep = ast.parse(
        textwrap.dedent(
            """
            def f(x):
                if x:
                    for i in x:
                        while i:
                            with open(i):
                                try:
                                    if i:
                                        pass
                                except Exception:
                                    pass
            """
        )
    ).body[0]
    flat = ast.parse(
        textwrap.dedent(
            """
            def g(x):
                if x == 1:
                    pass
                elif x == 2:
                    pass
                elif x == 3:
                    if x:
                        pass
            """
        )
    ).body[0]
    assert isinstance(deep, ast.FunctionDef) and isinstance(flat, ast.FunctionDef)
    assert sc._nesting(deep.body) == 6
    assert sc._nesting(flat.body) == 2


def test_duplicated_windows_are_counted_once_per_line(tmp_path: Path) -> None:
    body = "\n".join(f"    value_{i} = compute({i}) + other({i})" for i in range(10))
    (tmp_path / "a.py").write_text(f"def a():\n{body}\n")
    (tmp_path / "b.py").write_text(f"def b():\n{body}\n")
    measured = sc.measure([tmp_path])
    # 10 identical lines in each file are inside a repeated 8-line window;
    # the two ``def`` lines differ, so 20 of 22 code lines are duplicated.
    assert measured["duplicated_lines_pct"] == pytest.approx(100 * 20 / 22, abs=0.01)


def test_unparsable_files_are_reported_not_fatal(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("def broken(:\n")
    tally = sc.Tally()
    sc.analyse_file(tmp_path / "bad.py", tally, Counter(), [])
    assert tally.unparsable == [str(tmp_path / "bad.py")]


def test_percentile_orientation() -> None:
    population = [1.0, 2.0, 3.0, 4.0]
    assert sc.percentile(0.5, population, higher_is_better=False) == 100.0
    assert sc.percentile(5.0, population, higher_is_better=True) == 100.0
    assert sc.percentile(2.0, population, higher_is_better=True) == 37.5  # beats 1, ties 2


def test_the_committed_baseline_has_every_measurement() -> None:
    baseline = sc.load_baseline()
    assert baseline == json.loads(sc.BASELINE.read_text())
    assert len(baseline["projects"]) >= 30
    assert set(baseline["median"]) == set(sc.HIGHER_IS_BETTER)
    for project in baseline["projects"].values():
        assert set(sc.HIGHER_IS_BETTER) <= set(project)


def test_overload_stubs_are_not_counted_as_functions(tmp_path: Path) -> None:
    measured = _measure(
        tmp_path,
        """
        from typing import overload

        @overload
        def f(a: int, b: int, c: int, d: int, e: int, g: int) -> int: ...
        @overload
        def f(a: str, b: str, c: str, d: str, e: str, g: str) -> str: ...
        def f(a, b, c, d, e, g):
            return a
        """,
    )
    assert measured["functions"] == 1


def test_nested_definitions_are_counted_on_their_own(tmp_path: Path) -> None:
    measured = _measure(
        tmp_path,
        """
        import functools

        def outer(x):
            if x:
                def inner(y):
                    return y
                return inner, (lambda z: z)
            return None

        class Holder:
            @property
            def value(self) -> int:
                return 1

            @functools.cache
            def cached(self) -> int:
                return 2
        """,
    )
    # outer, inner, value and cached: decorators other than overload count.
    assert measured["functions"] == 4


def test_files_complexipy_or_radon_reject_still_count(tmp_path: Path, monkeypatch) -> None:
    def refuse(source: str, *args: object) -> object:
        raise RuntimeError("rejected")

    monkeypatch.setattr(sc, "code_complexity", refuse)
    monkeypatch.setattr(sc, "mi_visit", refuse)
    measured = _measure(tmp_path, '"""Doc."""\n\ndef f(x):\n    """Doc."""\n    return x\n')
    assert measured["files"] == 1 and measured["functions"] == 1
    assert measured["maintainability_index"] == 0.0
    assert measured["hard_to_read_functions_pct"] == 0.0


def test_test_directories_are_skipped_but_named_files_are_measured(tmp_path: Path) -> None:
    (tmp_path / "pkg" / "tests").mkdir(parents=True)
    (tmp_path / "pkg" / "test.py").write_text("X = 1\n")
    (tmp_path / "pkg" / "tests" / "test_x.py").write_text("Y = 2\n")
    assert list(sc.python_files(tmp_path)) == [tmp_path / "pkg" / "test.py"]
    assert len(list(sc.python_files(tmp_path, skip_tests=False))) == 2
    named = tmp_path / "pkg" / "tests" / "test_x.py"
    assert list(sc.python_files(named)) == [named]
    assert sc.measure([tmp_path / "missing"]) == sc.summarise(sc.Tally())


def _corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two installed-looking packages on ``sys.path``: a package and a module."""
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / "__init__.py").write_text('"""Alpha."""\n\ndef a(x):\n    return x\n')
    (tmp_path / "beta.py").write_text('"""Beta."""\n\ndef b(x, y):\n    return x + y\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    import importlib.metadata

    def version(dist: str) -> str:
        if dist == "alpha-dist":
            return "1.2"
        raise importlib.metadata.PackageNotFoundError(dist)

    monkeypatch.setattr(
        importlib.metadata, "packages_distributions", lambda: {"alpha": ["alpha-dist"]}
    )
    monkeypatch.setattr(importlib.metadata, "version", version)


def test_installed_packages_are_found_without_importing_them(tmp_path, monkeypatch) -> None:
    _corpus(tmp_path, monkeypatch)
    assert sc.installed_package_dir("alpha") == tmp_path / "alpha"
    assert sc.installed_package_dir("beta") == tmp_path / "beta.py"
    assert sc.installed_package_dir("no_such_package_here") is None


def test_the_baseline_records_each_project_and_the_medians(tmp_path, monkeypatch) -> None:
    _corpus(tmp_path, monkeypatch)
    baseline = sc.build_baseline(["alpha", "beta", "no_such_package_here"])
    assert baseline["schema"] == 1
    assert set(baseline["projects"]) == {"alpha", "beta"}
    assert baseline["projects"]["alpha"]["distribution"] == "alpha-dist"
    assert baseline["projects"]["alpha"]["version"] == "1.2"
    assert baseline["projects"]["beta"]["version"] == "unknown"
    assert set(baseline["median"]) == set(sc.HIGHER_IS_BETTER)
    assert baseline["median"]["docstring_coverage_pct"] == 50.0


def test_percentile_of_an_empty_corpus_is_the_average() -> None:
    assert sc.percentile(3.0, [], higher_is_better=True) == 50.0


def test_zero_against_a_zero_median_counts_as_better_and_renders(tmp_path) -> None:
    measured = _measure(
        tmp_path, '"""Doc."""\n\ndef f(x: int) -> int:\n    """Doc."""\n    return x\n'
    )
    # A corpus whose every median is zero: only "lower is better" zeros beat it.
    flat = dict.fromkeys(sc.HIGHER_IS_BETTER, 0.0)
    baseline = {"projects": {"p": flat, "q": flat}, "median": flat}
    rows = {row["metric"]: row for row in sc.score(measured, baseline)["rows"]}
    assert measured["oversized_modules_pct"] == 0.0
    assert rows["oversized_modules_pct"]["better_than_median"]
    assert rows["oversized_modules_pct"]["percentile"] == 50.0
    assert not rows["mean_ccn"]["better_than_median"]
    text = sc.render("demo", sc.score(measured, baseline), 2)
    assert text.startswith("demo: Takeover Ease Index ")
    assert "(industry average = 50; corpus of 2 projects)" in text
    assert "Mean cyclomatic complexity" in text and "<- worse than median" in text
    assert text.endswith("(1 files, 2 code lines, 1 functions)")


def test_the_command_line_measures_scores_gates_and_rebaselines(tmp_path, monkeypatch, capsys):
    source = tmp_path / "pkg"
    source.mkdir()
    (source / "m.py").write_text(
        '"""Doc."""\n\ndef f(x: int) -> int:\n    """Doc."""\n    return x\n'
    )
    assert sc.main(["measure", str(source)]) == 0
    assert json.loads(capsys.readouterr().out)["functions"] == 1
    assert sc.main(["score", str(source), "--name", "pkg"]) == 0
    assert capsys.readouterr().out.startswith("pkg: Takeover Ease Index")
    assert sc.main(["score", str(source), "--json", "--min-index", "0"]) == 0
    index = json.loads(capsys.readouterr().out)["takeover_ease_index"]
    assert sc.main(["score", str(source), "--json", "--min-index", str(index + 1)]) == 1
    capsys.readouterr()
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _corpus(corpus, monkeypatch)
    monkeypatch.setattr(sc, "REFERENCE_CORPUS", ("alpha", "beta"))
    output = tmp_path / "baseline.json"
    assert sc.main(["baseline", "--output", str(output)]) == 0
    assert capsys.readouterr().out == f"measured 2 projects -> {output}\n"
    written = sc.load_baseline(output)
    assert set(written["projects"]) == {"alpha", "beta"}
    custom = sc.main(["score", str(source), "--baseline", str(output), "--json"])
    assert custom == 0
    assert json.loads(capsys.readouterr().out)["metrics_total"] == len(sc.HIGHER_IS_BETTER)
