"""Measure and gate Python maintainability metrics.

Radon supplies cyclomatic complexity and Halstead Volume, Complexipy supplies
cognitive complexity, and the small AST visitors below make the NPath and
nesting definitions visible and testable.  Run ``xrddevtools maintainability
--help`` from a repository root for the human and machine-readable reports.

This is the *per-function* gate: a repository that keeps a policy file
(xrdclient's ``maintainability.json``: ``src``, ``tools``, ``tests``,
``benchmarks``, ``examples``) runs ``check`` in CI and through
:func:`xrddevtools.pytest_plugin.maintainability_gate`, which fails if any
single function in the policy's scan paths exceeds any of the five absolute
limits. Its sibling :mod:`xrddevtools.scorecard` asks the broader
whole-codebase question. xrdclient's ``docs/maintainability.md`` explains each
metric and why the limits are the standard values; :data:`STANDARD_LIMITS`
pins them, and the gate refuses a policy that loosens any. A failing check is
fixed by refactoring the function, never by raising a limit or adding an
exclusion.

Reading order: :func:`analyze_source` measures one file's functions (each
function is measured in isolation, nested definitions removed, so a closure's
complexity is charged to the closure and not to its parent);
:func:`analyze` walks the policy's files; :func:`violations` compares with the
limits; the ``render_*`` functions and :func:`main` are presentation only.
"""

from __future__ import annotations

import argparse
import ast
import copy
import csv
import fnmatch
import io
import json
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from complexipy import code_complexity
from radon.complexity import cc_visit_ast
from radon.metrics import h_visit_ast

#: The five measurements, in report-column order; each needs a policy limit.
METRICS = ("ccn", "cognitive", "npath", "halstead_volume", "max_nesting")
#: The family's limits, each the published default of the tool or paper it comes
#: from (McCabe's 10, SonarSource's cognitive 15, Nejmeh's NPath 200, a
#: Halstead Volume a reviewer can hold in mind, four nested blocks). A
#: repository's policy may be stricter, never looser: :func:`policy_problems`.
STANDARD_LIMITS: dict[str, float] = {
    "ccn": 10.0,
    "cognitive": 15.0,
    "npath": 200.0,
    "halstead_volume": 500.0,
    "max_nesting": 4.0,
}
#: NPath grows multiplicatively and can overflow anything readable in a few
#: dozen ``if`` statements; past this the answer is just "far over 200".
NPATH_SATURATION = 1_000_000_000_000


@dataclass(frozen=True)
class FunctionMetrics:
    """The five measurements of one function, plus where it is.

    ``key`` is ``path::Qualified.name`` and is unique within a report (see
    :func:`_function_keys`); it is what violations and the CSV/JSON reports use
    to identify a function across runs.
    """

    key: str
    path: str  # relative to the policy root, POSIX separators
    name: str  # dotted scope within the file, e.g. ``Class.method.inner``
    line: int
    end_line: int
    ccn: int  # Radon: 1 + decisions (branches, loops, handlers, boolean operators)
    cognitive: int  # Complexipy: breaks in linear flow, weighted by nesting
    npath: int  # this module: structurally distinct paths, saturating
    halstead_volume: float  # Radon: length * log2(vocabulary) of operators/operands
    max_nesting: int  # this module: deepest control-flow block stack


@dataclass(frozen=True)
class FileMetrics:
    """One file's roll-up: the worst function value per metric, Halstead summed.

    Maxima rather than sums because the gate is per function - a file of many
    simple functions is not a problem - while total Halstead Volume is a
    reasonable proxy for how much code the file asks a reader to take in.
    """

    path: str
    lines: int
    functions: int
    ccn: int
    cognitive: int
    npath: int
    halstead_volume: float
    max_nesting: int


@dataclass(frozen=True)
class Report:
    """Every measured file and function from one :func:`analyze` run."""

    files: tuple[FileMetrics, ...]
    functions: tuple[FunctionMetrics, ...]


@dataclass(frozen=True)
class Violation:
    """One metric of one function over its limit."""

    key: str
    metric: str
    actual: float
    limit: float


@dataclass(frozen=True)
class FunctionNode:
    """A function's AST node together with its dotted scope (``Class.method``)."""

    name: str
    node: ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True)
class Policy:
    """The parsed ``maintainability.json``: what to scan and the limits.

    Relative scan paths and exclusion patterns are interpreted against
    ``root``, the directory holding the policy file, so the check gives the
    same answer from any working directory.
    """

    root: Path
    paths: tuple[str, ...]
    exclude: tuple[str, ...]
    limits: Mapping[str, float]


# NPath arithmetic saturates at NPATH_SATURATION so a pathological function
# yields a bounded number (and bounded work) rather than a huge integer.
def _saturating_add(left: int, right: int) -> int:
    return min(NPATH_SATURATION, left + right)


def _saturating_multiply(left: int, right: int) -> int:
    return min(NPATH_SATURATION, left * right)


def _product(values: Iterable[int]) -> int:
    """Saturating product: independent sequential choices multiply paths."""
    result = 1
    for value in values:
        result = _saturating_multiply(result, value)
    return result


def _sum(values: Iterable[int]) -> int:
    """Saturating sum: mutually exclusive alternatives add paths."""
    result = 0
    for value in values:
        result = _saturating_add(result, value)
    return result


def _npath_expression(node: ast.AST | None) -> int:
    """Paths through an expression; 1 for anything that cannot branch.

    Short-circuit operators and conditional expressions branch inside an
    expression, which classic NPath (Nejmeh 1988) also counts. A lambda body is
    a separate function and is charged nothing here; any other node multiplies
    its children.
    """
    if node is None:
        return 1
    if isinstance(node, ast.BoolOp):
        # Short-circuiting can stop after any operand.
        return _sum(_npath_expression(value) for value in node.values)
    if isinstance(node, ast.IfExp):
        alternatives = _saturating_add(_npath_expression(node.body), _npath_expression(node.orelse))
        return _saturating_multiply(_npath_expression(node.test), alternatives)
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        return _npath_comprehension(node)
    if isinstance(node, ast.Lambda):
        return 1
    return _product(_npath_expression(child) for child in ast.iter_child_nodes(node))


#: The four comprehension forms, which every metric here treats alike.
_Comprehension = ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp


def _npath_comprehension(node: _Comprehension) -> int:
    """Paths through a comprehension, treating each generator like a loop.

    Each ``for`` clause either runs its body or does not (the ``1 +``), each
    ``if`` filter doubles that (passes or is skipped), and the element
    expression's own branches multiply the result.
    """
    paths = 1
    for generator in node.generators:
        branch = _saturating_add(1, _npath_expression(generator.iter))
        for condition in generator.ifs:
            branch = _saturating_multiply(branch, _npath_expression(condition) + 1)
        paths = _saturating_multiply(paths, branch)
    values = _comprehension_values(node)
    return _saturating_multiply(paths, _product(_npath_expression(value) for value in values))


def _comprehension_values(node: _Comprehension) -> tuple[ast.expr, ...]:
    """What a comprehension evaluates per item: ``key, value`` or the element."""
    if isinstance(node, ast.DictComp):
        return (node.key, node.value)
    return (node.elt,)


def _npath_block(statements: Sequence[ast.stmt]) -> int:
    """Statements in sequence: their path counts multiply."""
    return _product(_npath_statement(statement) for statement in statements)


def _npath_if(node: ast.If) -> int:
    """Body and ``else`` are alternatives; the test's own short-circuits multiply."""
    # An ``if`` without ``else`` still has two alternatives: the empty orelse
    # block is a product over nothing, i.e. one path.
    alternatives = _saturating_add(_npath_block(node.body), _npath_block(node.orelse))
    return _saturating_multiply(_npath_expression(node.test), alternatives)


def _npath_loop(node: Any) -> int:
    """Paths through ``for``/``while``: body, ``else``, or not iterating again."""
    # The extra path is the loop terminating without another iteration.
    alternatives = _sum((_npath_block(node.body), _npath_block(node.orelse), 1))
    # ``while`` has a test and ``for`` an iterable; either may branch itself.
    condition = getattr(node, "test", None) or getattr(node, "iter", None)
    return _saturating_multiply(_npath_expression(condition), alternatives)


def _npath_try(node: Any) -> int:
    """Success (body then ``else``) or any one handler, followed by ``finally``.

    Exceptions part-way through the body are not counted as extra paths; doing
    so would charge every ``try`` for each statement in it.
    """
    success = _saturating_multiply(_npath_block(node.body), _npath_block(node.orelse))
    handled = _sum(_npath_block(handler.body) for handler in node.handlers)
    alternatives = _saturating_add(success, handled)
    return _saturating_multiply(alternatives, _npath_block(node.finalbody))


def _is_wildcard_case(case: Any) -> bool:
    """Whether ``case`` is a bare ``case _:``, which leaves no fall-through path."""
    pattern = case.pattern
    return isinstance(pattern, ast.MatchAs) and pattern.pattern is None and pattern.name is None


def _npath_match(node: Any) -> int:
    """Each case is an alternative, plus falling through when no ``case _`` exists."""
    alternatives = _sum(_npath_block(case.body) for case in node.cases)
    if not any(_is_wildcard_case(case) for case in node.cases):
        # No case matched: control falls out of the ``match`` untouched.
        alternatives = _saturating_add(alternatives, 1)
    guards = _product(_npath_expression(case.guard) for case in node.cases)
    return _product((_npath_expression(node.subject), guards, alternatives))


def _npath_statement(node: ast.stmt) -> int:
    """Paths through one statement; nested ``def``/``class`` are measured separately."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return 1
    if isinstance(node, ast.If):
        return _npath_if(node)
    if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
        return _npath_loop(node)
    return _npath_other_statement(node)


def _npath_other_statement(node: ast.stmt) -> int:
    """The less common compound statements, split out to keep the dispatcher small."""
    if isinstance(node, (ast.Try, ast.TryStar)):
        return _npath_try(node)
    if isinstance(node, ast.Match):
        return _npath_match(node)
    # ``with`` is not a branch; only its context expressions and body can be.
    if isinstance(node, (ast.With, ast.AsyncWith)):
        contexts = _product(_npath_expression(item.context_expr) for item in node.items)
        return _saturating_multiply(contexts, _npath_block(node.body))
    # Simple statements (assignments, calls, returns, raises) branch only
    # inside the expressions they contain.
    return _product(_npath_expression(child) for child in ast.iter_child_nodes(node))


def npath_complexity(node: ast.AST) -> int:
    """Return structural execution paths for one function, excluding nested definitions.

    The NPath of Nejmeh (1988), adapted to Python: sequential statements
    multiply, alternatives add, and short-circuit operators, conditional
    expressions, comprehension filters, ``match`` fall-through and loop
    termination all count as branches. A straight-line function scores 1;
    ``docs/maintainability.md`` documents the rules and the tests in
    ``tests/test_maintainability.py`` pin each one.
    """

    return _npath_block(getattr(node, "body", ()))


#: Statements that open a nesting level. ``if`` is handled separately so an
#: ``elif`` chain stays at its ``if``'s depth instead of stairstepping.
_NESTING_BLOCKS = (
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.With,
    ast.AsyncWith,
    ast.TryStar,
    ast.Match,
)


def _max_children(nodes: Iterable[ast.AST], depth: int) -> int:
    """The deepest nesting among ``nodes``, each starting at ``depth``."""
    return max((_nesting(child, depth) for child in nodes), default=depth)


def _nesting_if(node: ast.If, depth: int) -> int:
    """Depth of an ``if``; an ``elif`` (a lone ``If`` in ``orelse``) is not deeper."""
    current = depth + 1
    found = max(current, _nesting(node.test, current), _max_children(node.body, current))
    # ``elif`` is stored as an ``if`` alone in ``orelse``; measure it from this
    # ``if``'s own depth so a long chain reads (and scores) as one level.
    if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
        return max(found, _nesting_if(node.orelse[0], depth))
    return max(found, _max_children(node.orelse, current))


def _nesting_comprehension(node: _Comprehension, depth: int) -> int:
    """Each ``for`` clause of a comprehension counts as one more level."""
    found = depth
    for generator in node.generators:
        depth += 1
        found = max(found, depth, _nesting(generator.iter, depth))
        found = max(found, _max_children(generator.ifs, depth))
    return max(found, _max_children(_comprehension_values(node), depth))


def _nesting(node: ast.AST, depth: int) -> int:
    """The deepest control-flow level reached at or below ``node``.

    Nested functions, classes and lambdas stop the descent: they are measured as
    functions of their own, like every other metric here.
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
        return depth
    if isinstance(node, ast.If):
        return _nesting_if(node, depth)
    if isinstance(node, _NESTING_BLOCKS):
        current = depth + 1
        return max(current, _max_children(ast.iter_child_nodes(node), current))
    # A conditional expression nests like an ``if`` statement for the reader.
    if isinstance(node, ast.IfExp):
        current = depth + 1
        return max(current, _max_children(ast.iter_child_nodes(node), current))
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        return _nesting_comprehension(node, depth)
    return _max_children(ast.iter_child_nodes(node), depth)


def max_nesting_depth(node: ast.AST) -> int:
    """Return lexical control-flow depth; a top-level control block has depth one.

    Counted: ``if`` (``elif`` stays level), loops, ``try``/``try*``, ``with``,
    ``match``, conditional expressions and comprehension generators. A
    function with no control flow scores 0.
    """

    return _max_children(getattr(node, "body", ()), 0)


class _FunctionCollector(ast.NodeVisitor):
    """Collect every function in a module, nested ones included, with its dotted scope."""

    def __init__(self) -> None:
        """Start at module scope with nothing collected."""
        self.scope: list[str] = []
        self.functions: list[FunctionNode] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Enter a class: its methods are named ``Class.method``; it is not measured."""
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Collect a ``def``."""
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Collect an ``async def`` exactly like a ``def``."""
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """Record ``node`` and keep walking: functions inside it are reported too."""
        name = node.name
        qualified = ".".join((*self.scope, name))
        self.functions.append(FunctionNode(qualified, node))
        self.scope.append(name)
        self.generic_visit(node)
        self.scope.pop()


class _NestedDefinitionStripper(ast.NodeTransformer):
    """Replace nested ``def``/``class`` statements with ``pass``.

    The libraries measure everything in the module they are given; stripping
    nested definitions is what makes their numbers per function, matching the
    project-owned NPath and nesting metrics.
    """

    def _replace(self, node: ast.AST) -> ast.Pass:
        """A ``pass`` at ``node``'s location, so line numbers stay meaningful."""
        return ast.copy_location(ast.Pass(), node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        """Drop a nested ``def``; it is measured as a function of its own."""
        return self._replace(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        """Drop a nested ``async def``; it is measured as a function of its own."""
        return self._replace(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        """Drop a nested class; its methods are measured as functions of their own."""
        return self._replace(node)


def _isolated_module(node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.Module:
    """A module containing only a copy of ``node``, its nested definitions stripped."""
    # Deep copy: stripping mutates the tree, and the caller still needs the
    # original nodes (their nested functions are measured next).
    root = copy.deepcopy(node)
    stripper = _NestedDefinitionStripper()
    root.body = [stripper.visit(statement) for statement in root.body]
    module = ast.Module(body=[root], type_ignores=[])
    return ast.fix_missing_locations(module)


def _library_metrics(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[int, int, float]:
    """CCN (Radon), cognitive complexity (Complexipy) and Halstead Volume (Radon).

    Complexipy only accepts source text, hence the round trip through
    :func:`ast.unparse`; comments and docstrings do not affect any of the three.
    """
    module = _isolated_module(node)
    # The module holds exactly one definition, so each library's first (and
    # only) function entry is this function. A method arrives without its
    # class, so radon reports it as a plain function, which is what we want.
    cyclomatic_blocks = cc_visit_ast(module)
    ccn = cyclomatic_blocks[0].complexity

    cognitive_report = code_complexity(ast.unparse(module))
    cognitive = cognitive_report.functions[0].complexity

    # Radon gives every function an entry (volume 0 when it has no operators,
    # e.g. ``def f(): pass``); the empty-report guard is purely defensive.
    halstead_report = h_visit_ast(module)
    volume = halstead_report.functions[0][1].volume if halstead_report.functions else 0.0
    return int(ccn), int(cognitive), round(float(volume), 3)


def _function_keys(path: str, functions: Sequence[FunctionNode]) -> Iterator[str]:
    """A stable unique key per function: ``path::name``, then ``#2``, ``#3`` for repeats.

    Repeats are real - ``typing.overload`` stubs and conditional definitions
    share a qualified name - and every function must still get its own key.
    """
    seen: dict[str, int] = {}
    for function in functions:
        base = f"{path}::{function.name}"
        seen[base] = seen.get(base, 0) + 1
        yield base if seen[base] == 1 else f"{base}#{seen[base]}"


def analyze_source(source: str, path: str = "<memory>") -> tuple[FunctionMetrics, ...]:
    """Measure every function, method and nested function in ``source``.

    ``path`` is only used for keys and error messages, so tests can measure
    snippets without writing files. A syntax error propagates.
    """
    tree = ast.parse(source, filename=path)
    collector = _FunctionCollector()
    collector.visit(tree)
    functions = collector.functions
    result = []
    # _function_keys yields exactly one key per function, so the zip is exact.
    for key, function in zip(_function_keys(path, functions), functions, strict=False):
        node = function.node
        ccn, cognitive, volume = _library_metrics(node)
        result.append(
            FunctionMetrics(
                key=key,
                path=path,
                name=function.name,
                line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                ccn=ccn,
                cognitive=cognitive,
                npath=npath_complexity(node),
                halstead_volume=volume,
                max_nesting=max_nesting_depth(node),
            )
        )
    return tuple(result)


def _file_rollup(path: str, source: str, functions: Sequence[FunctionMetrics]) -> FileMetrics:
    """Summarise one file's functions as described on :class:`FileMetrics`."""

    def maximum(metric: str) -> Any:
        """The worst value of ``metric`` in this file; 0 for a file with no functions."""
        return max((getattr(function, metric) for function in functions), default=0)

    return FileMetrics(
        path=path,
        lines=len(source.splitlines()),
        functions=len(functions),
        ccn=maximum("ccn"),
        cognitive=maximum("cognitive"),
        npath=maximum("npath"),
        halstead_volume=round(sum(function.halstead_volume for function in functions), 3),
        max_nesting=maximum("max_nesting"),
    )


def load_policy(config_path: Path) -> Policy:
    """Read and validate a policy file; :class:`ValueError` on schema or missing limits.

    A policy without a limit for some metric is rejected rather than defaulted,
    so a new metric cannot silently go ungated.
    """
    config_path = config_path.resolve()
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if data.get("schema") != 1:
        raise ValueError(f"unsupported maintainability policy schema in {config_path}")
    limits = data["limits"]
    missing = set(METRICS) - set(limits)
    if missing:
        raise ValueError(f"policy is missing limits for: {', '.join(sorted(missing))}")
    # Paths in the policy are relative to the policy file, not the cwd.
    root = config_path.parent
    return Policy(
        root=root,
        paths=tuple(data["paths"]),
        exclude=tuple(data.get("exclude", ())),
        limits={metric: float(limits[metric]) for metric in METRICS},
    )


def policy_problems(policy: Policy) -> list[str]:
    """Why ``policy`` is weaker than the family's standard, one line per reason.

    A limit above :data:`STANDARD_LIMITS` or any exclusion pattern lets a
    function skip the check every other function passes - grandfathering by
    configuration - so the ``check`` gate refuses such a policy outright.
    """
    problems = [
        f"limit {metric}={_format_number(policy.limits[metric])} is looser than the "
        f"standard {_format_number(limit)}"
        for metric, limit in STANDARD_LIMITS.items()
        if policy.limits[metric] > limit
    ]
    problems += [f"exclusion {pattern!r}: every file is measured" for pattern in policy.exclude]
    return problems


def _is_excluded(path: str, patterns: Sequence[str]) -> bool:
    """Whether a root-relative POSIX path matches an exclusion exactly or as a glob."""
    return any(path == pattern or fnmatch.fnmatch(path, pattern) for pattern in patterns)


def iter_python_files(policy: Policy, requested: Sequence[str] = ()) -> Iterator[Path]:
    """Each ``.py`` file under the requested paths (or the policy's), once, not excluded.

    Exclusions apply to explicitly requested paths too, so ``check some/file.py``
    cannot be used to measure what the policy deliberately skips. A missing
    path raises :class:`FileNotFoundError` instead of silently scanning nothing.
    """
    discovered = set()
    for raw_path in requested or policy.paths:
        # Overlapping requests (``src`` and ``src/xrdclient``) must not count a
        # file twice; exclusions are matched on the root-relative POSIX path so
        # a policy reads the same on every platform.
        for candidate in _python_files_at(policy.root, raw_path):
            relative = candidate.resolve().relative_to(policy.root).as_posix()
            if relative not in discovered and not _is_excluded(relative, policy.exclude):
                discovered.add(relative)
                yield candidate


def _python_files_at(root: Path, raw_path: str) -> Iterable[Path]:
    """The ``.py`` files one requested path names (relative paths are the policy root's).

    Anything that is neither a ``.py`` file nor a directory - a typo, or a
    non-Python file - is an error rather than a silently empty scan.
    """
    path = root / raw_path  # an absolute ``raw_path`` replaces ``root``
    if path.is_file() and path.suffix == ".py":
        return (path,)
    if path.is_dir():
        return path.rglob("*.py")
    raise FileNotFoundError(raw_path)


def analyze(policy: Policy, requested: Sequence[str] = ()) -> Report:
    """Measure every file :func:`iter_python_files` yields, in sorted (stable) order."""
    files: list[FileMetrics] = []
    functions: list[FunctionMetrics] = []
    for path in sorted(iter_python_files(policy, requested), key=lambda item: item.as_posix()):
        relative = path.resolve().relative_to(policy.root).as_posix()
        source = path.read_text(encoding="utf-8")
        measured = analyze_source(source, relative)
        files.append(_file_rollup(relative, source, measured))
        functions.extend(measured)
    return Report(tuple(files), tuple(functions))


def violations(
    report: Report,
    limits: Mapping[str, float],
) -> tuple[Violation, ...]:
    """Every (function, metric) pair over its limit; equal to the limit passes."""
    result = []
    # Compared as floats: limits come from JSON, and Halstead Volume is the
    # one non-integral metric.
    for function in report.functions:
        for metric in METRICS:
            actual = float(getattr(function, metric))
            limit = float(limits[metric])
            if actual > limit:
                result.append(Violation(function.key, metric, actual, limit))
    return tuple(result)


def violation_counts(failures: Sequence[Violation]) -> dict[str, int]:
    """How many violations each metric has, every metric present (zero included)."""
    counts = dict.fromkeys(METRICS, 0)
    for failure in failures:
        counts[failure.metric] += 1
    return counts


def _count_text(failures: Sequence[Violation]) -> str:
    """``ccn=1, cognitive=0, ...``: the per-metric counts as one log line."""
    counts = violation_counts(failures)
    return ", ".join(f"{metric}={counts[metric]}" for metric in METRICS)


def hotspot_functions(report: Report, limits: Mapping[str, float]) -> tuple[FunctionMetrics, ...]:
    """The functions with at least one metric over its limit, in report order."""
    result = []
    for function in report.functions:
        if any(float(getattr(function, metric)) > float(limits[metric]) for metric in METRICS):
            result.append(function)
    return tuple(result)


def _normalised_score(function: FunctionMetrics, limits: Mapping[str, float]) -> float:
    """The function's worst metric as a fraction of its limit, for ranking.

    Normalising lets one ranking mix metrics on very different scales (an
    NPath of 400 and a nesting of 5 are both "twice"/"1.25x the limit").
    """
    return max(float(getattr(function, metric)) / float(limits[metric]) for metric in METRICS)


def _column_widths(rows: Sequence[Sequence[str]], headers: Sequence[str]) -> list[int]:
    """The width of each column: its widest cell or header."""
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    return widths


def _render_row(row: Sequence[str], widths: Sequence[int]) -> str:
    return "  ".join(value.ljust(width) for value, width in zip(row, widths, strict=False))


def _table(rows: Sequence[Sequence[Any]], headers: Sequence[str]) -> str:
    """A plain left-aligned text table with a dashed rule under the header."""
    text_rows = [[str(value) for value in row] for row in rows]
    widths = _column_widths(text_rows, headers)
    header = _render_row(headers, widths)
    rule = "  ".join("-" * width for width in widths)
    body = [_render_row(row, widths) for row in text_rows]
    return "\n".join((header, rule, *body))


def render_table(report: Report, limits: Mapping[str, float], top: int) -> str:
    """The human ``report``: a row per file, then functions ranked worst first.

    ``top`` limits the function rows (zero means all); the ranking is by
    :func:`_normalised_score` with the key as tie-break, so output is stable.
    """
    file_rows = [
        (
            file.path,
            file.lines,
            file.functions,
            file.ccn,
            file.cognitive,
            file.npath,
            f"{file.halstead_volume:.1f}",
            file.max_nesting,
        )
        for file in report.files
    ]
    file_table = _table(
        file_rows,
        ("file", "lines", "funcs", "max CCN", "max cog", "max NPath", "Halstead V", "nest"),
    )
    # Rank every function, not just failures: the table is for spotting the
    # next function likely to cross a limit, before it does.
    ranked = sorted(
        report.functions,
        key=lambda function: (_normalised_score(function, limits), function.key),
        reverse=True,
    )
    if top:
        ranked = ranked[:top]
    function_rows = [
        (
            function.key,
            function.line,
            function.ccn,
            function.cognitive,
            function.npath,
            f"{function.halstead_volume:.1f}",
            function.max_nesting,
        )
        for function in ranked
    ]
    function_table = _table(
        function_rows, ("function", "line", "CCN", "cog", "NPath", "Halstead V", "nest")
    )
    label = "all functions" if not top else f"top {min(top, len(ranked))} functions"
    return (
        "Files (function maxima; Halstead V is the file total)\n"
        f"{file_table}\n\n{label}\n{function_table}"
    )


def render_json(report: Report) -> str:
    """The complete measurement record as JSON (``schema`` 1), for dashboards and diffs."""
    # The saturation value travels with the data so a consumer can tell a
    # capped NPath from a measured one.
    document = {
        "schema": 1,
        "npath_saturation": NPATH_SATURATION,
        "files": [asdict(file) for file in report.files],
        "functions": [asdict(function) for function in report.functions],
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def render_csv(report: Report) -> str:
    """One CSV row per function, columns in :class:`FunctionMetrics` field order."""
    output = io.StringIO()
    fields = tuple(FunctionMetrics.__dataclass_fields__)
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(asdict(function) for function in report.functions)
    return output.getvalue()


def _ranked_hotspots(report: Report, limits: Mapping[str, float]) -> tuple[FunctionMetrics, ...]:
    """Hotspot functions, worst (by :func:`_normalised_score`) first."""
    return tuple(
        sorted(
            hotspot_functions(report, limits),
            key=lambda function: (_normalised_score(function, limits), function.key),
            reverse=True,
        )
    )


def _failed_metrics(function: FunctionMetrics, limits: Mapping[str, float]) -> list[str]:
    """The names of the metrics on which ``function`` exceeds its limit."""
    return [
        metric for metric in METRICS if float(getattr(function, metric)) > float(limits[metric])
    ]


def render_hotspots_table(report: Report, limits: Mapping[str, float]) -> str:
    """The human ``hotspots`` drill-down: counts, then each failing function and metric."""
    hotspots = _ranked_hotspots(report, limits)
    rows = [
        (
            function.key,
            function.line,
            function.ccn,
            function.cognitive,
            function.npath,
            f"{function.halstead_volume:.1f}",
            function.max_nesting,
            ",".join(_failed_metrics(function, limits)),
        )
        for function in hotspots
    ]
    table = _table(
        rows,
        ("function", "line", "CCN", "cog", "NPath", "Halstead V", "nest", "failed"),
    )
    failures = violations(report, limits)
    return (
        f"Current hotspots: {len(hotspots)} functions, {len(failures)} metric violations\n"
        f"By metric: {_count_text(failures)}\n"
        f"{table}"
    )


def _hotspot_document(report: Report, limits: Mapping[str, float]) -> dict[str, Any]:
    """The ``hotspots --format json`` document: limits, summary counts, ranked functions."""
    functions = []
    # Each function carries its own failed metrics so a dashboard does not have
    # to re-derive them from the limits.
    for function in _ranked_hotspots(report, limits):
        measured = asdict(function)
        measured["failed_metrics"] = _failed_metrics(function, limits)
        functions.append(measured)
    failures = violations(report, limits)
    return {
        "schema": 1,
        "limits": dict(limits),
        "summary": {
            "files_scanned": len(report.files),
            "functions_scanned": len(report.functions),
            "hotspot_functions": len(functions),
            "metric_violations": len(failures),
            "violations_by_metric": violation_counts(failures),
        },
        "functions": functions,
    }


def render_hotspots_json(report: Report, limits: Mapping[str, float]) -> str:
    """The :func:`_hotspot_document` as indented, key-sorted JSON."""
    return json.dumps(_hotspot_document(report, limits), indent=2, sort_keys=True) + "\n"


def render_hotspots_csv(report: Report, limits: Mapping[str, float]) -> str:
    """Ranked hotspots as CSV, with the failed metrics comma-joined in one column."""
    output = io.StringIO()
    fields = (*FunctionMetrics.__dataclass_fields__, "failed_metrics")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for function in _ranked_hotspots(report, limits):
        row = asdict(function)
        row["failed_metrics"] = ",".join(_failed_metrics(function, limits))
        writer.writerow(row)
    return output.getvalue()


def _write_or_print(content: str, output: Path | None) -> None:
    """Write ``content`` to ``output``, or to stdout with exactly one trailing newline."""
    if output is None:
        print(content, end="" if content.endswith("\n") else "\n")
    else:
        output.write_text(content, encoding="utf-8")


def _format_number(value: float) -> str:
    """Integral values without a decimal point; Halstead Volume to three places.

    Metrics such as CCN arrive as ``int``, which has no ``is_integer()``
    before Python 3.12, hence the conversion.
    """
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.3f}"


def _check(report: Report, policy: Policy) -> int:
    """The ``check`` gate: a one-line pass on stdout, or every problem on stderr and 1.

    A policy weaker than the standard (:func:`policy_problems`) fails even
    when every function passes it. Each violation is listed individually (not
    just counted) because this is what a CI log shows; the reader should not
    need a second run to know which function to fix. The pass line goes to
    stdout and every failure to stderr, so a CI log shows the failures even
    when stdout is captured or discarded.
    """
    problems = policy_problems(policy)
    for problem in problems:
        print(f"maintainability policy: {problem}", file=sys.stderr)
    failures = violations(report, policy.limits)
    if failures:
        _print_violations(report, policy, failures)
    elif not problems:
        print(
            f"maintainability: {len(report.functions)} functions in {len(report.files)} files pass"
        )
    return int(bool(problems or failures))


def _print_violations(report: Report, policy: Policy, failures: Sequence[Violation]) -> None:
    """Every violation on stderr, with the per-metric counts and where to look next."""
    hotspot_count = len(hotspot_functions(report, policy.limits))
    print(
        f"Maintainability violations: {len(failures)} metrics across {hotspot_count} functions",
        file=sys.stderr,
    )
    print(f"By metric: {_count_text(failures)}", file=sys.stderr)
    for failure in failures:
        print(
            f"  {failure.key}: {failure.metric}={_format_number(failure.actual)} "
            f"> limit {_format_number(failure.limit)}",
            file=sys.stderr,
        )
    print(
        "Run `xrddevtools maintainability hotspots` for the ranked drill-down.",
        file=sys.stderr,
    )


def _parser() -> argparse.ArgumentParser:
    """The ``report`` / ``hotspots`` / ``check`` command line.

    ``--config`` defaults to ``maintainability.json`` in the current
    directory, which is why the documented invocation runs from the
    repository root. Every subcommand accepts paths to narrow the scan,
    e.g. while refactoring one module.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("maintainability.json"), help="policy JSON path"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    report = subparsers.add_parser("report", help="show per-file and per-function measurements")
    report.add_argument(
        "paths", nargs="*", help="optional files/directories instead of policy paths"
    )
    report.add_argument("--format", choices=("table", "json", "csv"), default="table")
    report.add_argument("--output", type=Path, help="write the report instead of stdout")
    report.add_argument("--top", type=int, default=25, help="table function count; zero means all")

    hotspots = subparsers.add_parser(
        "hotspots", help="list every function which exceeds an absolute limit"
    )
    hotspots.add_argument(
        "paths", nargs="*", help="optional files/directories instead of policy paths"
    )
    hotspots.add_argument("--format", choices=("table", "json", "csv"), default="table")
    hotspots.add_argument("--output", type=Path, help="write the hotspots instead of stdout")
    hotspots.set_defaults(top=0)  # every hotspot is listed

    check = subparsers.add_parser("check", help="fail if any function exceeds an absolute limit")
    check.add_argument(
        "paths", nargs="*", help="optional files/directories instead of policy paths"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one subcommand; the exit status is non-zero only for a failed ``check``."""
    args = _parser().parse_args(argv)
    # Measure first, whatever the subcommand: the three differ only in what
    # they do with the same report.
    policy = load_policy(args.config)
    report = analyze(policy, args.paths)
    if args.command == "check":
        return _check(report, policy)
    renderers = _HOTSPOT_RENDERERS if args.command == "hotspots" else _REPORT_RENDERERS
    content = renderers[args.format](report, policy.limits, args.top)
    _write_or_print(content, args.output)
    return 0


#: ``--format`` to the renderer of each subcommand's document; every one is
#: called with the report, the limits and ``--top`` (which only the report
#: table uses), and returns the text to write.
_Renderer = Callable[[Report, Mapping[str, float], int], str]
_HOTSPOT_RENDERERS: dict[str, _Renderer] = {
    "json": lambda report, limits, _: render_hotspots_json(report, limits),
    "csv": lambda report, limits, _: render_hotspots_csv(report, limits),
    "table": lambda report, limits, _: render_hotspots_table(report, limits) + "\n",
}
_REPORT_RENDERERS: dict[str, _Renderer] = {
    "json": lambda report, _, __: render_json(report),
    "csv": lambda report, _, __: render_csv(report),
    "table": lambda report, limits, top: render_table(report, limits, top) + "\n",
}


if __name__ == "__main__":
    raise SystemExit(main())
