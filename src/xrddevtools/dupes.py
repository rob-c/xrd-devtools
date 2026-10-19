"""The duplicate gate: the same function must not live in two repositories.

The chain was split out of one client into libraries precisely so that each
piece of behaviour has one implementation. A function copied into a second
repository instead of imported from the first silently forks that behaviour:
a fix lands in one copy and not the other. ``xrddevtools dupes --workspace
DIR`` finds such copies statically (nothing is imported or run):

* **Duplicates** (the gate; exit status 1): two functions in *different*
  repositories whose bodies are structurally identical once every local name,
  parameter and string literal is replaced by a placeholder - a renamed copy
  is still a copy. Only functions of at least :data:`MIN_STATEMENTS`
  statements count (docstring excluded); below that, a match is an idiom
  (``return self._x``), not a copy.
* **Near-duplicates**: same-named functions in different repositories
  whose normalised token streams are at least :data:`SIMILARITY` alike - a
  copy that has started to drift, which is worth a look before the drift
  becomes a behaviour difference. Below :data:`NEAR_FAILURE` a warning; from
  it (``--fail-near`` names another threshold) an error that fails the gate
  like an exact copy: by then it is a copy that a fix will reach only half
  of, and the fix is the same - one implementation, in the lowest layer
  both repositories depend on.

What is compared: a function's body with names (``ast.Name``), parameters
(``ast.arg``, annotations dropped) and string constants normalised; attribute
names, keyword-argument names, numbers and the shape of the code are kept,
because they are what makes two bodies *do* the same thing. Each
repository's ``src`` and ``tests`` are scanned (``--tree`` narrows or widens
that): a test helper copied between suites is a second harness to keep in
step, and a client test that re-tests a library is a second copy of the
library's suite - the case belongs in the library, the harness in a public
testing module both suites import.

There is deliberately no allow-list. The chain mirrors upstream APIs (gfal2,
pyxrootd) but no two of its repositories mirror the *same* one, so a
cross-repository copy is never required; the fix for a finding is always to
move the function to the lowest layer both repositories depend on.
"""

from __future__ import annotations

import argparse
import ast
import copy
import difflib
import hashlib
import json
import sys
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypeGuard

from xrddevtools.workspace import REPOSITORIES, checkouts, python_files

#: The smallest body (in statements, docstring excluded) that counts.
MIN_STATEMENTS = 6
#: The token-stream similarity from which same-named functions are reported.
SIMILARITY = 0.8
#: The similarity from which a near-duplicate fails the gate (``--fail-near`` overrides).
NEAR_FAILURE = 0.85
#: The trees of every checkout scanned unless ``--tree`` says otherwise.
TREES = ("src", "tests")

_FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True, slots=True)
class Function:
    """One function long enough to compare, with its normalised fingerprint.

    ``fingerprint`` is a SHA-256 over the normalised body's ``ast.dump`` (so
    equal fingerprints mean structurally identical bodies); ``tokens`` is the
    same body as a pre-order stream of node kinds and kept identifiers, which
    :func:`similarity` aligns.
    """

    repo: str
    path: str
    name: str
    line: int
    statements: int
    fingerprint: str
    tokens: tuple[str, ...]

    def where(self) -> str:
        """``path:line name``, the form every report line uses."""
        return f"{self.path}:{self.line} {self.name}"


@dataclass(frozen=True, slots=True)
class NearDuplicate:
    """Two same-named functions in different repositories that are alike but not equal."""

    first: Function
    second: Function
    similarity: float


@dataclass(frozen=True, slots=True)
class Findings:
    """Everything one scan found: the gate's duplicates and the warnings.

    ``fail_near`` is the similarity from which a near-duplicate fails the
    gate too. There is no switch to turn that off and no allow-list.
    """

    duplicates: tuple[tuple[Function, ...], ...]
    near: tuple[NearDuplicate, ...]
    unparsable: tuple[str, ...]
    fail_near: float = NEAR_FAILURE

    @property
    def failing_near(self) -> tuple[NearDuplicate, ...]:
        """The near-duplicates alike enough to fail the gate."""
        return tuple(near for near in self.near if near.similarity >= self.fail_near)

    @property
    def failed(self) -> bool:
        """Whether the gate fails: a copy (exact, or near enough), or a file it cannot read."""
        return bool(self.duplicates or self.unparsable or self.failing_near)


class _Normaliser(ast.NodeTransformer):
    """Replace what a copy would rename: names, parameters and string literals."""

    def visit_Name(self, node: ast.Name) -> ast.Name:
        """Every variable reads the same, so ``total`` and ``count`` match."""
        return ast.copy_location(ast.Name(id="_", ctx=node.ctx), node)

    def visit_arg(self, node: ast.arg) -> ast.arg:
        """Parameters likewise, and their annotations go: a retyped copy is a copy."""
        node.arg = "_"
        node.annotation = None
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.Constant:
        """Strings (messages, keys) are normalised; numbers are kept, they change behaviour."""
        if isinstance(node.value, str):
            return ast.copy_location(ast.Constant(value="s"), node)
        return node


def _body(node: _FunctionNode) -> list[ast.stmt]:
    """The function's statements without its docstring."""
    body = node.body
    first = body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return body[1:]
    return body


def _statements(body: Sequence[ast.stmt]) -> int:
    """How many statements ``body`` holds, nested ones included."""
    return sum(isinstance(node, ast.stmt) for stmt in body for node in ast.walk(stmt))


def _tokens(node: ast.AST) -> Iterator[str]:
    """``node`` in pre-order: each node's kind, plus the identifiers normalisation keeps."""
    yield type(node).__name__
    if isinstance(node, ast.Attribute):
        yield node.attr
    elif isinstance(node, ast.keyword) and node.arg:
        yield node.arg
    elif isinstance(node, ast.Constant):
        yield repr(node.value)
    for child in ast.iter_child_nodes(node):
        yield from _tokens(child)


def functions_in(
    source: str, repo: str, path: str, min_statements: int = MIN_STATEMENTS
) -> list[Function]:
    """Every function (nested ones too) of ``source`` with at least ``min_statements``.

    Raises :class:`SyntaxError` for a file Python cannot parse.
    """
    # Counting statements is cheap; normalising a body is not, so only bodies
    # long enough to count are copied and fingerprinted.
    nodes = [node for node in ast.walk(ast.parse(source)) if _long_enough(node, min_statements)]
    return [_comparable(node, repo, path) for node in nodes]


def _long_enough(node: ast.AST, min_statements: int) -> TypeGuard[_FunctionNode]:
    """Whether ``node`` is a function whose body has at least ``min_statements``."""
    return isinstance(node, _FunctionNode) and _statements(_body(node)) >= min_statements


def _comparable(node: _FunctionNode, repo: str, path: str) -> Function:
    """One function with its body normalised, fingerprinted and tokenised."""
    body = _body(node)
    module = ast.Module(body=copy.deepcopy(body), type_ignores=[])
    normalised = _Normaliser().visit(module)
    digest = hashlib.sha256(ast.dump(normalised).encode()).hexdigest()
    tokens = tuple(token for stmt in normalised.body for token in _tokens(stmt))
    return Function(repo, path, node.name, node.lineno, _statements(body), digest, tokens)


def similarity(first: Function, second: Function, floor: float = 0.0) -> float:
    """How alike two functions' token streams are, from 0 to 1 (difflib's ratio).

    The full alignment is quadratic, so when difflib's cheap upper bound is
    already below ``floor`` that bound is returned instead: the caller only
    needs to know the pair cannot reach it.
    """
    matcher = difflib.SequenceMatcher(None, first.tokens, second.tokens, autojunk=False)
    bound = min(matcher.real_quick_ratio(), matcher.quick_ratio())
    return bound if bound < floor else matcher.ratio()


def duplicates(functions: Sequence[Function]) -> list[tuple[Function, ...]]:
    """Groups of identical functions spanning at least two repositories, largest first."""
    groups: dict[str, list[Function]] = defaultdict(list)
    for function in functions:
        groups[function.fingerprint].append(function)
    spanning = [tuple(g) for g in groups.values() if len({f.repo for f in g}) > 1]
    return sorted(spanning, key=lambda group: (-group[0].statements, group[0].where()))


def near_duplicates(
    functions: Sequence[Function], threshold: float = SIMILARITY
) -> list[NearDuplicate]:
    """Same-named, unequal functions of different repositories at least ``threshold`` alike.

    Only same-named pairs are compared: aligning every pair of the chain's
    functions would take hours, and a drifting copy usually keeps its name
    (a renamed exact copy is the gate's, whatever its name). Dunder methods
    are left out: every ``__init__`` assigns attributes.
    """
    found = [
        near
        for group in _by_name(functions)
        for index, first in enumerate(group)
        for second in group[index + 1 :]
        for near in _near_pair(first, second, threshold)
    ]
    return sorted(found, key=lambda near: (-near.similarity, near.first.where()))


def _by_name(functions: Sequence[Function]) -> list[list[Function]]:
    """The functions grouped by name, dunder methods left out."""
    groups: dict[str, list[Function]] = defaultdict(list)
    for function in functions:
        groups[function.name].append(function)
    return [group for name, group in groups.items() if not name.startswith("__")]


def _near_pair(first: Function, second: Function, threshold: float) -> list[NearDuplicate]:
    """The pair as a near-duplicate, if it spans two repositories, differs and is alike."""
    if first.repo == second.repo or first.fingerprint == second.fingerprint:
        return []
    ratio = similarity(first, second, threshold)
    return [NearDuplicate(first, second, round(ratio, 2))] if ratio >= threshold else []


def scan(
    workspace: Path,
    repos: Sequence[str] = REPOSITORIES,
    trees: Sequence[str] = TREES,
    min_statements: int = MIN_STATEMENTS,
) -> tuple[list[Function], list[str]]:
    """Every comparable function of ``trees`` in each checkout, and the files that did not parse."""
    functions: list[Function] = []
    unparsable: list[str] = []
    for checkout in checkouts(workspace, repos):
        for tree in trees:
            for path in python_files(checkout / tree):
                relative = path.relative_to(workspace).as_posix()
                try:
                    source = path.read_text(encoding="utf-8")
                    functions += functions_in(source, checkout.name, relative, min_statements)
                except (SyntaxError, UnicodeDecodeError, ValueError):
                    unparsable.append(relative)
    return functions, unparsable


def find(options: argparse.Namespace) -> Findings:
    """Run the whole check that ``options`` (the parsed command line) describes."""
    functions, unparsable = scan(
        options.workspace, options.repos or REPOSITORIES, options.trees or TREES, options.min
    )
    fail_near = options.fail_near
    return Findings(
        tuple(duplicates(functions)),
        # Never report less than what fails: the warning floor is at most the failure one.
        tuple(near_duplicates(functions, min(options.similarity, fail_near))),
        tuple(unparsable),
        fail_near,
    )


def render(findings: Findings) -> str:
    """The plain-text report: errors first, then warnings, then a one-line verdict."""
    lines = [f"error: cannot parse {path}" for path in findings.unparsable]
    lines += [_duplicate_line(group) for group in findings.duplicates]
    failing = set(findings.failing_near)
    lines += [
        f"{'error' if near in failing else 'warning'}: near-duplicate ({near.similarity:.2f}): "
        f"{near.first.where()} ~ {near.second.where()}"
        for near in findings.near
    ]
    verdict = "FAILED" if findings.failed else "passed"
    lines.append(
        f"dupes {verdict}: {len(findings.duplicates)} duplicated functions, "
        f"{len(findings.near)} near-duplicates ({len(failing)} failing), "
        f"{len(findings.unparsable)} unparsable files"
    )
    return "\n".join(lines)


def _duplicate_line(group: Sequence[Function]) -> str:
    """One error line naming every copy of a duplicated function."""
    copies = "  |  ".join(function.where() for function in group)
    return f"error: duplicate ({group[0].statements} statements): {copies}"


def as_json(findings: Findings) -> str:
    """The findings as JSON (token streams left out: they are for comparing, not reading)."""

    def plain(function: Function) -> dict[str, object]:
        """One function without its token stream."""
        record = asdict(function)
        del record["tokens"]
        return record

    document = {
        "failed": findings.failed,
        "duplicates": [[plain(f) for f in group] for group in findings.duplicates],
        "near_duplicates": [
            {
                "similarity": n.similarity,
                "failing": n in findings.failing_near,
                "first": plain(n.first),
                "second": plain(n.second),
            }
            for n in findings.near
        ],
        "unparsable": list(findings.unparsable),
    }
    return json.dumps(document, indent=2)


def parser() -> argparse.ArgumentParser:
    """The command line; see the module docstring."""
    top = argparse.ArgumentParser(
        prog="xrddevtools dupes",
        description=(__doc__ or "").split("\n\n")[0],
    )
    top.add_argument("--workspace", type=Path, required=True, help="the checkouts' parent")
    top.add_argument(
        "--repo", dest="repos", action="append", choices=REPOSITORIES, help="limit the scan"
    )
    top.add_argument(
        "--tree", dest="trees", action="append", help="directories to scan (default: src and tests)"
    )
    top.add_argument("--min-statements", dest="min", type=int, default=MIN_STATEMENTS)
    top.add_argument("--similarity", type=float, default=SIMILARITY)
    top.add_argument(
        "--fail-near",
        type=float,
        default=NEAR_FAILURE,
        metavar="SIMILARITY",
        help=f"fail on a near-duplicate at least this alike (default {NEAR_FAILURE})",
    )
    top.add_argument("--json", action="store_true", help="print the findings as JSON")
    return top


def main(argv: Sequence[str] | None = None) -> int:
    """Scan, print the report, and return 1 if the gate failed."""
    options = parser().parse_args(argv)
    findings = find(options)
    output = as_json(findings) if options.json else render(findings)
    print(output, file=sys.stdout)
    return int(findings.failed)


if __name__ == "__main__":
    raise SystemExit(main())
