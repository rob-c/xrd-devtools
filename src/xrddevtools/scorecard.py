"""Score how hard a Python code base would be for a new maintainer to take over.

The function-level gate in :mod:`xrddevtools.maintainability` answers "is any single
function too complicated?". A new maintainer asks a broader question: how much
of this code can I read, trust and change without its author beside me? This
tool answers that with a small set of project-level measurements, each chosen
because it predicts the cost of a hand-over and each with a published,
widely used threshold behind it:

==========================  ============================================  ==========
Measurement                 What it predicts for a newcomer               Direction
==========================  ============================================  ==========
Maintainability Index       Radon's MI, SLOC-weighted: a composite of     higher
                            volume, complexity and comments (0-100)
Mean cyclomatic complexity  How many paths an average function has        lower
Complex functions           Share of functions over McCabe's 10           lower
Hard-to-read functions      Share over SonarSource's cognitive limit 15   lower
Long functions              Share over pylint's 50 statements             lower
Many-parameter functions    Share over pylint's 5 arguments               lower
Deeply nested functions     Share over pylint's 5 nested blocks           lower
Oversized modules           Share of modules over pylint's 1000 lines     lower
Explanation density         Comment + docstring lines per code line       higher
Docstring coverage          Modules, classes and functions documented     higher
Type-annotation coverage    Functions whose signature is fully annotated  higher
Duplicated code             Lines inside repeated 8-line windows          lower
==========================  ============================================  ==========

"Industry average" is not a number anyone publishes for all of these, so the
tool measures it: ``baseline`` runs the identical analysis over a reference
corpus of widely used open-source Python libraries (by default, those installed
in the current environment that are named in ``REFERENCE_CORPUS``) and records
each measurement's median. ``score`` then places a project against that corpus:
for every measurement it reports the project's value, the corpus median and the
project's percentile within the corpus, oriented so that 100 always means
"easier to take over than every reference project". The headline
**Takeover Ease Index** is the mean of those percentiles; 50 is the industry
average by construction.

Only static analysis is performed - nothing in the analysed trees is imported
or executed - so the corpus can safely include any installed distribution.

How the family uses it: ``scorecard_baseline.json`` beside this module is the
committed corpus measurement (:data:`BASELINE`), and every repository of the
chain scores its ``src/`` against it through the pytest gate in
:mod:`xrddevtools.pytest_plugin`, so a change that makes a code base harder to
hand over shows up in CI. Re-run ``baseline`` only deliberately (a different
interpreter or corpus versions shift the medians); never edit the JSON by hand
to make a score pass.

Reading order: :func:`measure` -> :func:`analyse_file` (per-file counters into a
:class:`Tally`) -> :func:`summarise` (ratios) -> :func:`score` (percentiles
against the baseline) -> :func:`render`. The definitions of what counts are
deliberately the simplest that match the cited tool, and each lives in one
small function so a disputed number can be traced to one rule.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import statistics
import sys
import tokenize
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from complexipy import code_complexity
from radon.complexity import cc_visit_ast
from radon.metrics import mi_visit

#: Thresholds, each the default of a widely used tool so that a reader can look
#: the rule up rather than take this file's word for it.
CCN_LIMIT = 10  # McCabe (1976); radon grade B/C boundary; flake8 mccabe guidance
COGNITIVE_LIMIT = 15  # SonarSource rule python:S3776 default
STATEMENTS_LIMIT = 50  # pylint too-many-statements (R0915) default
ARGUMENTS_LIMIT = 5  # pylint too-many-arguments (R0913) default
NESTING_LIMIT = 5  # pylint too-many-nested-blocks (R1702) default
MODULE_LINES_LIMIT = 1000  # pylint too-many-lines (C0302) default
DUPLICATE_WINDOW = 8  # normalised lines per duplicate block (CPD-style)

#: Widely used, actively maintained open-source libraries whose installed
#: sources make up the default reference corpus. They span HTTP, cloud SDKs,
#: crypto, CLIs, templating, packaging, testing and documentation tooling.
REFERENCE_CORPUS: tuple[str, ...] = (
    "requests",
    "urllib3",
    "botocore",
    "click",
    "jinja2",
    "pygments",
    "rich",
    "packaging",
    "pip",
    "_pytest",
    "pluggy",
    "coverage",
    "mkdocs",
    "markdown",
    "docutils",
    "babel",
    "fsspec",
    "dateutil",
    "cryptography",
    "jwt",
    "idna",
    "charset_normalizer",
    "platformdirs",
    "pathspec",
    "more_itertools",
    "twine",
    "build",
    "hatchling",
    "radon",
    "yaml",
    "watchdog",
    "rfc3986",
    "keyring",
    "asn1crypto",
    "defusedxml",
    "typer",
    "markdown_it",
    "griffe",
    "xdist",
)

#: The committed corpus measurement every repository of the chain is scored
#: against; shipped beside this module so a checkout and an install agree.
BASELINE = Path(__file__).with_name("scorecard_baseline.json")

#: Directory names that are never the library's own maintained code.
SKIP_PARTS = frozenset({"_vendor", "vendor", "__pycache__", "tests", "test"})

#: name -> True when a larger value is better for a newcomer.
HIGHER_IS_BETTER: dict[str, bool] = {
    "maintainability_index": True,
    "mean_ccn": False,
    "complex_functions_pct": False,
    "hard_to_read_functions_pct": False,
    "long_functions_pct": False,
    "many_parameter_functions_pct": False,
    "deeply_nested_functions_pct": False,
    "oversized_modules_pct": False,
    "explanation_density": True,
    "docstring_coverage_pct": True,
    "annotation_coverage_pct": True,
    "duplicated_lines_pct": False,
}

#: The human-readable row titles :func:`render` prints, thresholds spelled out.
LABELS: dict[str, str] = {
    "maintainability_index": "Maintainability Index (0-100)",
    "mean_ccn": "Mean cyclomatic complexity",
    "complex_functions_pct": f"Functions with CCN > {CCN_LIMIT} (%)",
    "hard_to_read_functions_pct": f"Functions with cognitive > {COGNITIVE_LIMIT} (%)",
    "long_functions_pct": f"Functions with > {STATEMENTS_LIMIT} statements (%)",
    "many_parameter_functions_pct": f"Functions with > {ARGUMENTS_LIMIT} parameters (%)",
    "deeply_nested_functions_pct": f"Functions nested > {NESTING_LIMIT} deep (%)",
    "oversized_modules_pct": f"Modules > {MODULE_LINES_LIMIT} lines (%)",
    "explanation_density": "Comment+docstring lines per code line",
    "docstring_coverage_pct": "Docstring coverage (%)",
    "annotation_coverage_pct": "Fully annotated functions (%)",
    "duplicated_lines_pct": "Duplicated lines (%)",
}


@dataclass
class Tally:
    """Running totals for one code base, summed file by file.

    Every percentage in the final report is a ratio of two of these counters,
    so the totals - not per-file averages - are what is kept: a 20-line helper
    module must not weigh as much as a 2,000-line protocol engine.
    """

    files: int = 0
    code_lines: int = 0
    explanation_lines: int = 0
    mi_weighted: float = 0.0
    functions: int = 0
    ccn_total: int = 0
    complex_functions: int = 0
    hard_to_read: int = 0
    long_functions: int = 0
    many_parameters: int = 0
    deeply_nested: int = 0
    oversized_modules: int = 0
    documentable: int = 0
    documented: int = 0
    annotatable: int = 0
    annotated: int = 0
    duplicated_lines: int = 0
    unparsable: list[str] = field(default_factory=list)


def _pct(part: float, whole: float) -> float:
    """``part`` as a percentage of ``whole``; an empty whole scores zero."""
    return 100.0 * part / whole if whole else 0.0


def _line_kinds(source: str) -> tuple[set[int], set[int]]:
    """The physical lines holding code, and those holding comments or docstrings.

    A line that carries both code and a trailing comment counts as both, which
    is what a reader experiences. Docstrings are string-expression statements
    in the first position of a module, class or function body.
    """
    code: set[int] = set()
    notes: set[int] = set()
    # Pass 1: the tokenizer separates comments from code, but sees a docstring
    # as an ordinary STRING token, i.e. code. Multi-line tokens (triple-quoted
    # strings, bracketed continuations) mark every line they span.
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            notes.add(tok.start[0])
        elif tok.type not in (
            tokenize.NL,
            tokenize.NEWLINE,
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.ENDMARKER,
            tokenize.ENCODING,
        ):
            code.update(range(tok.start[0], tok.end[0] + 1))
    # Pass 2: only the AST knows which strings are docstrings; move their lines
    # from code to explanation. Other string literals stay code.
    for node in ast.walk(ast.parse(source)):
        doc = _docstring_node(node)
        if doc is not None:
            span = range(doc.lineno, (doc.end_lineno or doc.lineno) + 1)
            notes.update(span)
            code.difference_update(span)
    return code, notes


def _docstring_node(node: ast.AST) -> ast.Expr | None:
    """The docstring statement of a module, class or function, if it has one."""
    if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    body = node.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[0]
    return None


#: The statements pylint's R1702 counts as nesting a block.
_BLOCKS = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.TryStar,
    ast.With,
    ast.AsyncWith,
    ast.Match,
)


def _own_nodes(function: ast.AST) -> Iterator[ast.AST]:
    """Every node inside ``function`` that is not inside a nested def or class.

    A nested function is measured as a function of its own, exactly as the
    gate in :mod:`xrddevtools.maintainability` measures it.
    """
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _nesting(nodes: Sequence[ast.stmt], depth: int = 0) -> int:
    """The deepest stack of nested blocks under ``nodes`` (pylint's R1702)."""
    return max((_block_depth(node, depth) for node in nodes), default=depth)


def _block_depth(node: ast.stmt, depth: int) -> int:
    """How deep the blocks go inside one statement found at ``depth``."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return depth  # measured as a definition of its own
    is_block = isinstance(node, _BLOCKS)
    inner = depth + 1 if is_block else depth
    return max(
        (_nesting(body, level) for body, level in _bodies(node, depth, inner) if body),
        default=inner,
    )


def _bodies(node: ast.stmt, depth: int, inner: int) -> Iterator[tuple[list[ast.stmt], int]]:
    """Each statement list a block holds, with the depth its contents sit at.

    An ``elif`` is stored as an ``if`` alone in its parent's ``orelse``, but a
    reader sees it at the parent's level, so that one keeps ``depth``.
    """
    yield getattr(node, "body", []), inner
    yield getattr(node, "finalbody", []), inner
    orelse = getattr(node, "orelse", [])
    is_elif = isinstance(node, ast.If) and len(orelse) == 1 and isinstance(orelse[0], ast.If)
    yield orelse, depth if is_elif else inner
    for wrapper in [*getattr(node, "handlers", []), *getattr(node, "cases", [])]:
        yield wrapper.body, inner


def _parameters(function: ast.FunctionDef | ast.AsyncFunctionDef, in_class: bool) -> list[ast.arg]:
    """The parameters a caller supplies: ``self``/``cls`` of a method excluded.

    Positional-only, ordinary and keyword-only parameters all count, as in
    pylint's R0913: grouping options behind ``*`` makes calls clearer but does
    not make the function take less. ``*args`` and ``**kwargs`` are not
    counted. Only the conventional first-parameter names are dropped, so a
    ``@staticmethod`` keeps every parameter it has.
    """
    a = function.args
    params = [*a.posonlyargs, *a.args, *a.kwonlyargs]
    if in_class and params and params[0].arg in ("self", "cls", "mcs", "metacls"):
        params = params[1:]
    return params


def _fully_annotated(function: ast.FunctionDef | ast.AsyncFunctionDef, in_class: bool) -> bool:
    """Whether every parameter and the return value carry a type annotation.

    ``__init__`` may omit its return annotation: mypy accepts an unannotated
    ``-> None`` there once a parameter is annotated, so requiring it would
    penalise idiomatic code. ``self``/``cls`` never need annotations.
    """
    params = _parameters(function, in_class)
    extra = [p for p in (function.args.vararg, function.args.kwarg) if p is not None]
    if function.returns is None and function.name != "__init__":
        return False
    return all(p.annotation is not None for p in [*params, *extra])


#: What docstring coverage counts: a class or a (non-overload) function.
_Definition = ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef


def _definitions(
    body: Sequence[ast.stmt], in_class: bool = False
) -> Iterator[tuple[_Definition, bool]]:
    """Every class and function under ``body``, each with whether it is a method."""
    for node in body:
        yield from _definitions_in(node, in_class)


def _definitions_in(node: ast.stmt, in_class: bool) -> Iterator[tuple[_Definition, bool]]:
    """The definitions in one statement: itself, if it is one, and its contents."""
    if isinstance(node, ast.ClassDef):
        yield node, in_class
        yield from _definitions(node.body, True)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if _is_overload(node):
            return  # a typing declaration of the real function, not code of its own
        yield node, in_class
        yield from _definitions(node.body, False)
    else:  # an ``if TYPE_CHECKING:`` or ``try:`` block keeps its context
        children = [c for c in ast.iter_child_nodes(node) if isinstance(c, ast.stmt)]
        yield from _definitions(children, in_class)


def _is_overload(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether ``node`` is a ``@typing.overload`` stub.

    Overload stubs only declare extra signatures for the one implementation
    that follows them; they have no body to read or maintain, so counting
    them would charge a typed API several times for one function. The same
    rule is applied to the reference corpus.
    """
    for decorator in node.decorator_list:
        name = (
            decorator.attr if isinstance(decorator, ast.Attribute) else getattr(decorator, "id", "")
        )
        if name == "overload":
            return True
    return False


def _is_public(name: str) -> bool:
    """Public in the PEP 8 sense; dunder methods count as public API."""
    return not name.startswith("_") or (name.startswith("__") and name.endswith("__"))


def _cognitive(source: str) -> dict[int, int]:
    """Cognitive complexity per function, keyed by the line its ``def`` is on."""
    try:
        result = code_complexity(source)
    except Exception:  # complexipy rejects a few exotic files; score them as simple
        return {}
    return {f.line_start: f.complexity for f in result.functions}


def _normalise(line: str) -> str:
    """A line reduced to what makes it a copy: no indentation, no trailing space."""
    return " ".join(line.split())


def analyse_file(path: Path, tally: Tally, windows: Counter[str], owners: list[list[str]]) -> None:
    """Add one source file's measurements to ``tally``.

    Duplicate detection needs the whole code base, so this records the file's
    hashed windows in ``windows`` and, in ``owners``, which window covers which
    line; :func:`finish_duplicates` resolves them once every file is seen.
    """
    parsed = _parse(path)
    if parsed is None:
        tally.unparsable.append(str(path))
        return
    source, tree, code, notes = parsed
    _tally_module(tally, source, tree, code, notes)
    _tally_definitions(tally, source, tree)
    _record_windows(path, source, code, windows, owners)


def _parse(path: Path) -> tuple[str, ast.Module, set[int], set[int]] | None:
    """The source, its syntax tree and its line kinds - or ``None`` if unreadable."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        code, notes = _line_kinds(source)
    except (SyntaxError, UnicodeDecodeError, ValueError, tokenize.TokenError):
        return None
    return source, tree, code, notes


def _tally_module(
    tally: Tally, source: str, tree: ast.Module, code: set[int], notes: set[int]
) -> None:
    """The file-level counts: size, explanation, MI and the module docstring."""
    tally.files += 1
    tally.code_lines += len(code)
    tally.explanation_lines += len(notes)
    tally.oversized_modules += len(code) > MODULE_LINES_LIMIT
    try:
        mi = float(mi_visit(source, multi=True))
    except Exception:  # radon rejects a handful of exotic files; they score 0
        mi = 0.0
    tally.mi_weighted += mi * len(code)
    tally.documentable += 1
    tally.documented += _docstring_node(tree) is not None


def _tally_definitions(tally: Tally, source: str, tree: ast.Module) -> None:
    """Docstring counts for every public definition; the rest for functions.

    Private definitions are left out of docstring coverage, matching the
    public-only D1xx rules of pydocstyle; every function, private or not,
    counts toward the per-function shares.
    """
    ccn_by_line = _ccn_by_line(tree)
    cognitive = _cognitive(source)
    for node, in_class in _definitions(tree.body):
        if _is_public(node.name):
            tally.documentable += 1
            tally.documented += _docstring_node(node) is not None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _tally_function(tally, node, in_class, ccn_by_line, cognitive)


def _ccn_by_line(tree: ast.Module) -> dict[int, int]:
    """Radon's cyclomatic complexity for every function, keyed by its ``def`` line.

    Radon reports its own block objects, not AST nodes, so the line number is
    the join key. Radon's block list already holds methods beside their
    classes; reading each class's ``methods`` as well is harmless (the dict
    collapses repeats) and keeps the lookup right whichever form radon
    returns. Class blocks land in the map too, keyed by the ``class`` line,
    where no function lookup ever hits them.
    """
    blocks = cc_visit_ast(tree)
    flat = [*blocks, *(m for b in blocks for m in getattr(b, "methods", []))]
    return {b.lineno: b.complexity for b in flat if hasattr(b, "complexity")}


def _tally_function(
    tally: Tally,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    in_class: bool,
    ccn_by_line: dict[int, int],
    cognitive: dict[int, int],
) -> None:
    """One function's contribution to every per-function share.

    A function radon did not report (it lists nested functions only as
    closures of their parent) gets
    the minimum CCN of 1 rather than being dropped, so every function the
    AST walk finds is counted exactly once in every share.
    """
    ccn = ccn_by_line.get(node.lineno, 1)
    statements = sum(isinstance(n, ast.stmt) for n in _own_nodes(node))
    tally.functions += 1
    tally.ccn_total += ccn
    tally.complex_functions += ccn > CCN_LIMIT
    tally.hard_to_read += cognitive.get(node.lineno, 0) > COGNITIVE_LIMIT
    tally.long_functions += statements > STATEMENTS_LIMIT
    tally.many_parameters += len(_parameters(node, in_class)) > ARGUMENTS_LIMIT
    tally.deeply_nested += _nesting(node.body) > NESTING_LIMIT
    tally.annotatable += 1
    tally.annotated += _fully_annotated(node, in_class)


def _record_windows(
    path: Path, source: str, code: set[int], windows: Counter[str], owners: list[list[str]]
) -> None:
    """Hash every run of :data:`DUPLICATE_WINDOW` meaningful lines in the file.

    Imports, decorators and near-empty lines (``)``, ``else:``) are skipped:
    every module repeats them, and counting them would call idiom copying.
    """
    meaningful = _meaningful_lines(source, code)
    for i in range(len(meaningful) - DUPLICATE_WINDOW + 1):
        chunk = meaningful[i : i + DUPLICATE_WINDOW]
        joined = "\n".join(text for _, text in chunk)
        # 96-bit digests: collisions are negligible at corpus scale, and the
        # counter stays small enough to hold every window of botocore.
        digest = hashlib.blake2b(joined.encode(), digest_size=12).hexdigest()
        windows[digest] += 1
        owners.append([digest, str(path), *(str(n) for n, _ in chunk)])


def _meaningful_lines(source: str, code: set[int]) -> list[tuple[int, str]]:
    """The file's meaningful code lines, normalised, with their line numbers.

    Only code lines take part: a repeated comment or docstring paragraph is
    not duplicated logic. Windows slide over meaningful lines, not physical
    ones, so blank lines or a reflowed import block cannot hide a copy.
    """
    lines = source.splitlines()
    normalised = [(n, _normalise(lines[n - 1])) for n in sorted(code) if n <= len(lines)]
    return [(n, text) for n, text in normalised if _is_meaningful(text)]


def _is_meaningful(text: str) -> bool:
    """Whether a normalised line says enough to make a copy of it notable."""
    return len(text) > 3 and not text.startswith(("import ", "from ", "@"))


def finish_duplicates(tally: Tally, windows: Counter[str], owners: list[list[str]]) -> None:
    """Count every line that lies inside a window seen more than once.

    Lines are collected as a ``(path, line)`` set because consecutive windows
    overlap by seven lines: a 20-line copy is many windows but 20 lines. Both
    the original and the copy count, as they would for CPD or jscpd.
    """
    duplicated: set[tuple[str, str]] = set()
    for digest, path, *numbers in owners:
        if windows[digest] > 1:
            duplicated.update((path, n) for n in numbers)
    tally.duplicated_lines = len(duplicated)


def python_files(root: Path, *, skip_tests: bool = True) -> Iterator[Path]:
    """Every ``.py`` file under ``root`` that is the project's own maintained code.

    Only *directory* names are checked against :data:`SKIP_PARTS`, so a module
    called ``test.py`` is still measured while a ``tests/`` package is not.
    An explicitly named file is always measured.
    """
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*.py")):
        parts = set(path.relative_to(root).parts[:-1])
        if skip_tests and parts & SKIP_PARTS:
            continue
        yield path


def measure(paths: Iterable[Path], *, skip_tests: bool = True) -> dict[str, Any]:
    """All measurements for the code under ``paths``, as one flat dictionary.

    Every root shares one duplicate index, so a block copied between two of
    the given directories is found too.
    """
    tally = Tally()
    windows: Counter[str] = Counter()
    owners: list[list[str]] = []
    for root in paths:
        for path in python_files(root, skip_tests=skip_tests):
            analyse_file(path, tally, windows, owners)
    finish_duplicates(tally, windows, owners)
    return summarise(tally)


def summarise(t: Tally) -> dict[str, Any]:
    """Turn raw counters into the reported measurements.

    MI is averaged weighted by code lines, so a large module's score counts
    for proportionally more than a tiny ``__init__.py``'s (radon reports MI
    per file only). An empty code base scores zero rather than dividing by it.
    """
    return {
        "files": t.files,
        "code_lines": t.code_lines,
        "functions": t.functions,
        "maintainability_index": round(t.mi_weighted / t.code_lines, 2) if t.code_lines else 0.0,
        "mean_ccn": round(t.ccn_total / t.functions, 3) if t.functions else 0.0,
        "complex_functions_pct": round(_pct(t.complex_functions, t.functions), 2),
        "hard_to_read_functions_pct": round(_pct(t.hard_to_read, t.functions), 2),
        "long_functions_pct": round(_pct(t.long_functions, t.functions), 2),
        "many_parameter_functions_pct": round(_pct(t.many_parameters, t.functions), 2),
        "deeply_nested_functions_pct": round(_pct(t.deeply_nested, t.functions), 2),
        "oversized_modules_pct": round(_pct(t.oversized_modules, t.files), 2),
        "explanation_density": round(t.explanation_lines / t.code_lines, 3)
        if t.code_lines
        else 0.0,
        "docstring_coverage_pct": round(_pct(t.documented, t.documentable), 2),
        "annotation_coverage_pct": round(_pct(t.annotated, t.annotatable), 2),
        "duplicated_lines_pct": round(_pct(t.duplicated_lines, t.code_lines), 2),
        "unparsable_files": len(t.unparsable),
    }


def installed_package_dir(name: str) -> Path | None:
    """Where an installed top-level package lives, found without importing it.

    Importing would execute corpus code (and be slow for botocore); walking
    ``sys.path`` in order finds the same copy an import would. A namespace
    package without ``__init__.py`` is not found and is left out.
    """
    for entry in sys.path:
        candidate = Path(entry) / name
        if (candidate / "__init__.py").is_file():
            return candidate
        if candidate.with_suffix(".py").is_file():
            return candidate.with_suffix(".py")
    return None


def build_baseline(names: Sequence[str]) -> dict[str, Any]:
    """Measure every available corpus package and keep each one's numbers.

    Packages that are not installed are skipped, so the corpus is whatever
    the environment holds; the result records each project's distribution
    and version so a later re-baseline can be compared like for like. The
    medians are what the scorecard tests compare against.
    """
    from importlib.metadata import PackageNotFoundError, packages_distributions, version

    owners = packages_distributions()
    projects: dict[str, Any] = {}
    for name in names:
        where = installed_package_dir(name)
        if where is None:
            continue
        dist = (owners.get(name) or [name])[0]
        try:
            ver = version(dist)
        except PackageNotFoundError:
            ver = "unknown"
        projects[name] = {"distribution": dist, "version": ver, **measure([where])}
    medians = {
        key: round(statistics.median(p[key] for p in projects.values()), 3)
        for key in HIGHER_IS_BETTER
    }
    return {"schema": 1, "python": sys.version.split()[0], "projects": projects, "median": medians}


def load_baseline(path: Path = BASELINE) -> dict[str, Any]:
    """The corpus measurement at ``path`` (by default the committed :data:`BASELINE`)."""
    baseline: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return baseline


def percentile(value: float, population: Sequence[float], higher_is_better: bool) -> float:
    """Share of the corpus this value beats, ties counting half (0-100).

    Ties count half so a project equal to every reference project scores
    exactly 50, the "industry average" the index promises by construction.
    An empty corpus says nothing, which is also 50.
    """
    if not population:
        return 50.0
    better = sum((value > p) if higher_is_better else (value < p) for p in population)
    ties = sum(value == p for p in population)
    return round(100.0 * (better + ties / 2) / len(population), 1)


def score(measured: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Place a project's measurements against the corpus, metric by metric.

    ``better_than_median`` is strict, except that zero against a zero median
    counts as better: for a "lower is better" share whose median is already
    0 % (say, oversized modules) zero is the best possible result.
    """
    rows = [_score_row(key, measured[key], baseline) for key in HIGHER_IS_BETTER]
    index = round(statistics.mean(r["percentile"] for r in rows), 1)
    return {
        "takeover_ease_index": index,
        "metrics_better_than_median": sum(r["better_than_median"] for r in rows),
        "metrics_total": len(rows),
        "rows": rows,
        "measured": measured,
    }


def _score_row(key: str, value: float, baseline: dict[str, Any]) -> dict[str, Any]:
    """One measurement placed against the corpus: its median, percentile and verdict."""
    higher = HIGHER_IS_BETTER[key]
    population = [p[key] for p in baseline["projects"].values()]
    median = baseline["median"][key]
    beats = value > median if higher else value < median
    return {
        "metric": key,
        "label": LABELS[key],
        "value": value,
        "industry_median": median,
        "percentile": percentile(value, population, higher),
        "better_than_median": beats or value == median == 0,
    }


def render(name: str, result: dict[str, Any], corpus_size: int) -> str:
    """A plain-text table a reviewer can read in a terminal or paste in a PR."""
    out = [
        f"{name}: Takeover Ease Index {result['takeover_ease_index']} / 100 "
        f"(industry average = 50; corpus of {corpus_size} projects)",
        f"better than the corpus median on {result['metrics_better_than_median']} of "
        f"{result['metrics_total']} measurements",
        "",
        f"{'measurement':48} {'project':>9} {'median':>9} {'pctile':>7}",
    ]
    for r in result["rows"]:
        mark = "" if r["better_than_median"] else "  <- worse than median"
        out.append(
            f"{r['label']:48} {r['value']:>9} {r['industry_median']:>9} {r['percentile']:>7}{mark}"
        )
    m = result["measured"]
    out.append("")
    out.append(f"({m['files']} files, {m['code_lines']} code lines, {m['functions']} functions)")
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point; see ``--help``.

    ``score --min-index`` turns the report into a gate (exit 1 below the
    given index) for callers that want one; the project's own CI gate is the
    scorecard test, which asserts per measurement.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("baseline", help="measure the reference corpus")
    b.add_argument("--output", type=Path, required=True)
    m = sub.add_parser("measure", help="print raw measurements as JSON")
    m.add_argument("paths", nargs="+", type=Path)
    s = sub.add_parser("score", help="score a project against a baseline")
    s.add_argument("paths", nargs="+", type=Path)
    s.add_argument("--baseline", type=Path, default=BASELINE)
    s.add_argument("--name", default="project")
    s.add_argument("--json", action="store_true")
    s.add_argument(
        "--min-index",
        type=float,
        default=None,
        help="exit 1 unless the Takeover Ease Index is at least this",
    )
    args = parser.parse_args(argv)
    if args.command == "baseline":
        data = build_baseline(REFERENCE_CORPUS)
        args.output.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        print(f"measured {len(data['projects'])} projects -> {args.output}")
        return 0
    measured = measure(args.paths)
    if args.command == "measure":
        print(json.dumps(measured, indent=2))
        return 0
    baseline = load_baseline(args.baseline)
    result = score(measured, baseline)
    corpus = len(baseline["projects"])
    print(json.dumps(result, indent=2) if args.json else render(args.name, result, corpus))
    if args.min_index is not None and result["takeover_ease_index"] < args.min_index:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
