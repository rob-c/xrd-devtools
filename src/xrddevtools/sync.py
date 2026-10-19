"""``xrddevtools sync``: write the standard into every repository's managed blocks.

The chain's code standard used to be written out by hand in eight
``pyproject.toml`` files and seven CONTRIBUTING files, and each copy drifted
one edit at a time. Now it is written once, as data, in this package's
``standard/`` directory:

* ``standard.toml`` - the standard's values (the ruff rules, mypy's
  strictness, the complexity limit, coverage's exclusions, the warnings
  filter) and the ban groups (what each library owns, with the lint
  message naming the replacement and CONTRIBUTING's explanation);
* ``repositories.toml`` - everything that may differ between repositories,
  declared: each one's role (library, client, jobs, tooling - the role picks
  the ban groups), the chain packages it may import (every other one is
  banned), what it owns, its coverage source and the third-party modules
  mypy may treat as untyped, each with its reason;
* ``templates/`` - the text of each block (``<block>.toml.tmpl``,
  ``<block>.md.tmpl``), with ``$placeholders``.

A *managed block* is the text between two marker lines in a repository's
file - a TOML comment in ``pyproject.toml``, an HTML comment in Markdown -
so the file stays valid either way::

    # >>> xrddevtools sync: ruff (generated from xrd-devtools/src/xrddevtools/standard) >>>
    [tool.ruff]
    ...
    # <<< xrddevtools sync: ruff <<<

``xrddevtools sync --workspace DIR`` (or ``--repo PATH``) rewrites every
block from the standard, leaving everything outside the markers alone - a
repository's own keys (mypy's ``files``, pytest's ``markers``) sit beside
the blocks in the same tables. ``--check`` writes nothing and reports what
it would change; :func:`drift` is the same check as findings, which
:mod:`xrddevtools.conformance` reports, so a hand edit inside a block fails
CI. A file missing a block's markers is a finding too: sync never guesses
where a block belongs, so a new block's markers are placed by hand once.
"""

from __future__ import annotations

import argparse
import importlib.resources
import json
import re
import sys
import textwrap
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from importlib.resources.abc import Traversable
from pathlib import Path
from string import Template
from typing import Any

from xrddevtools.workspace import REPOSITORIES, checkouts, python_trees

#: Where the standard's data and templates live (package data).
STANDARD: Traversable = importlib.resources.files("xrddevtools") / "standard"
#: The managed blocks of ``pyproject.toml``, in the order sync reports them.
PYPROJECT_BLOCKS = (
    "ruff",
    "banned-api",
    "mypy",
    "mypy-overrides",
    "complexipy",
    "coverage",
    "pytest-warnings",
)
#: Where the standard lives, as a block's begin marker names it.
SOURCE = "xrd-devtools/src/xrddevtools/standard"
#: A begin or end marker inside a comment; ``name`` is the block's.
_MARKER = re.compile(
    r"^(?:#|<!--) (?P<edge>>>>|<<<) xrddevtools sync: (?P<name>[\w-]+)\b.*(?:>>>|<<<)(?: -->)?$"
)


@dataclass(frozen=True, slots=True)
class Style:
    """How one kind of file writes a marker: as a comment that leaves it valid."""

    opening: str
    closing: str

    def begin(self, name: str) -> str:
        """The line that opens block ``name``, naming where its content comes from."""
        head = f"{self.opening}>>> xrddevtools sync: {name}"
        return f"{head} (generated from {SOURCE}) >>>{self.closing}"

    def end(self, name: str) -> str:
        """The line that closes block ``name``."""
        return f"{self.opening}<<< xrddevtools sync: {name} <<<{self.closing}"


TOML = Style("# ", "")
MARKDOWN = Style("<!-- ", " -->")


@dataclass(frozen=True, slots=True)
class Repository:
    """One repository's declared differences (``repositories.toml``), and its Python trees."""

    name: str
    role: str
    depends_on: tuple[str, ...]
    trees: tuple[str, ...]
    owns: frozenset[str] = frozenset()
    bans: Mapping[str, str] = field(default_factory=dict)
    messages: Mapping[str, str] = field(default_factory=dict)
    coverage_source: tuple[str, ...] = ()
    coverage_concurrency: tuple[str, ...] = ()
    relax_test_annotations: bool = False
    untyped: tuple[Mapping[str, Any], ...] = ()


class SyncError(Exception):
    """A file or declaration sync cannot work with (the message says which and why)."""


@cache
def standard() -> dict[str, Any]:
    """``standard.toml``: the values every repository shares."""
    return tomllib.loads(STANDARD.joinpath("standard.toml").read_text(encoding="utf-8"))


@cache
def declarations() -> dict[str, Any]:
    """``repositories.toml``: the roles and what may differ per repository."""
    return tomllib.loads(STANDARD.joinpath("repositories.toml").read_text(encoding="utf-8"))


def template(name: str) -> Template:
    """The block template ``templates/<name>.tmpl``.

    The suffix keeps tools from mistaking a template for the real thing:
    ruff, for one, would read a file named ``ruff.toml`` as its configuration.
    """
    return Template(STANDARD.joinpath("templates", f"{name}.tmpl").read_text(encoding="utf-8"))


def repository(checkout: Path) -> Repository:
    """The declarations of the repository checked out at ``checkout`` (its directory name).

    Raises :class:`SyncError` for a directory that is not a declared repository.
    """
    declared = declarations()["repositories"].get(checkout.name)
    if declared is None:
        raise SyncError(f"{checkout.name}: not a repository of the chain (repositories.toml)")
    return Repository(
        name=checkout.name,
        role=declared["role"],
        depends_on=tuple(declared["depends-on"]),
        trees=tuple(python_trees(checkout)),
        owns=frozenset(declared.get("owns", [])),
        bans=declared.get("bans", {}),
        messages=declared.get("messages", {}),
        coverage_source=tuple(declared["coverage-source"]),
        coverage_concurrency=tuple(declared.get("coverage-concurrency", [])),
        relax_test_annotations=declared["relax-test-annotations"],
        untyped=tuple(declared.get("untyped", [])),
    )


def toml_value(value: object) -> str:
    """``value`` as TOML: strings (JSON's escapes are TOML's), booleans, integers, lists."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | tuple):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _depends(name: str) -> set[str]:
    """Every chain package ``name`` is built on, directly or not."""
    repositories = declarations()["repositories"]
    found: set[str] = set()
    pending = list(repositories[name]["depends-on"])
    while pending:
        current = pending.pop()
        if current not in found:
            found.add(current)
            pending += repositories[current]["depends-on"]
    return found


def relation(repo: Repository, other: str) -> str:
    """How ``repo`` stands to ``other``, a chain package it may not import (``[layering]``)."""
    roles = {name: entry["role"] for name, entry in declarations()["repositories"].items()}
    if repo.role == "tooling":
        return "tooling"
    if repo.name in _depends(other):
        return "below"
    return "peer" if roles[other] == roles[repo.name] == "library" else "beside"


def layering(repo: Repository) -> dict[str, str]:
    """The chain packages ``repo`` must not import, each with its ban message."""
    allowed = set(repo.depends_on) | {repo.name}
    words = standard()["layering"]
    return {
        other: Template(words[relation(repo, other)]).substitute(repo=repo.name, other=other)
        for other in REPOSITORIES
        if other not in allowed
    }


@dataclass(frozen=True, slots=True)
class Ban:
    """One ban group as a repository applies it: the names it bans, its message, why."""

    names: tuple[str, ...]
    message: str
    why: str


def group_bans(repo: Repository) -> list[Ban]:
    """The ban groups of ``repo``'s role, less what it owns, in its own wording where it has one."""
    groups = standard()["bans"]
    applied = [
        Ban(
            tuple(name for name in groups[group]["names"] if name not in repo.owns),
            repo.messages.get(group, groups[group]["msg"]),
            groups[group]["why"],
        )
        for group in declarations()["roles"][repo.role]["bans"]
        if group not in repo.owns
    ]
    return [ban for ban in applied if ban.names]


def bans(repo: Repository) -> dict[str, str]:
    """Every name ``repo`` bans, with its message: its role's groups, its own, the layering."""
    banned = {name: ban.message for ban in group_bans(repo) for name in ban.names}
    return banned | dict(repo.bans) | layering(repo)


def _quoted(names: Iterable[str]) -> str:
    """Names as Markdown code, comma-separated."""
    return ", ".join(f"`{name}`" for name in names)


def ruff_block(repo: Repository) -> str:
    """``[tool.ruff]`` and ``[tool.ruff.lint]``: the rules, nothing ignored.

    The banned-API rule is always selected: every repository bans at least
    the chain packages it may not import.
    """
    ruff = standard()["ruff"]
    rule = f'\n  # Banned APIs: the banned-api table.\n  "{ruff["banned-api-rule"]}",'
    return template("ruff.toml").substitute(
        line_length=ruff["line-length"],
        target_version=toml_value(ruff["target-version"]),
        select=", ".join(map(toml_value, ruff["select"])),
        security=", ".join(map(toml_value, ruff["security"])),
        banned_api_rule=rule,
    )


def banned_api_block(repo: Repository) -> str:
    """The banned-API table, one ``name.msg`` line per ban."""
    entries = "\n".join(
        f"{toml_value(name)}.msg = {toml_value(message)}" for name, message in bans(repo).items()
    )
    return template("banned-api.toml").substitute(entries=entries)


def mypy_block(repo: Repository) -> str:
    """The head of ``[tool.mypy]``; the repository's own keys (``files``, ...) follow it."""
    mypy = standard()["mypy"]
    return template("mypy.toml").substitute(
        python_version=toml_value(mypy["python_version"]), strict=toml_value(mypy["strict"])
    )


def mypy_overrides_block(repo: Repository) -> str:
    """The overrides the standard allows: unannotated test functions, named untyped modules."""
    parts = [template("mypy-tests.toml").template] if repo.relax_test_annotations else []
    parts += [
        template("mypy-untyped.toml").substitute(
            reason="\n# ".join(textwrap.wrap(entry["reason"], 76)),
            modules=toml_value(entry["modules"]),
        )
        for entry in repo.untyped
    ]
    if not parts:
        return "# No override: test functions are annotated too, and every import is typed.\n"
    return "\n".join(parts)


def complexipy_block(repo: Repository) -> str:
    """``[tool.complexipy]`` over every Python tree of the checkout."""
    complexipy = standard()["complexipy"]
    return template("complexipy.toml").substitute(
        paths=toml_value(list(repo.trees)),
        max_complexity=complexipy["max-complexity-allowed"],
        failed=toml_value(complexipy["failed"]),
    )


def coverage_block(repo: Repository) -> str:
    """``[tool.coverage.run]`` and ``[tool.coverage.report]``: every line and branch."""
    coverage = standard()["coverage"]
    concurrency = repo.coverage_concurrency
    return template("coverage.toml").substitute(
        source=toml_value(list(repo.coverage_source)),
        concurrency=f"\nconcurrency = {toml_value(list(concurrency))}" if concurrency else "",
        fail_under=coverage["fail_under"],
        exclude_also="\n".join(f"  {toml_value(line)}," for line in coverage["exclude_also"]),
    )


def pytest_block(repo: Repository) -> str:
    """The ``filterwarnings`` line of ``[tool.pytest.ini_options]``: every warning an error."""
    filters = standard()["pytest"]["filterwarnings"]
    return template("pytest-warnings.toml").substitute(filterwarnings=toml_value(filters))


_PYPROJECT_RENDERERS = {
    "ruff": ruff_block,
    "banned-api": banned_api_block,
    "mypy": mypy_block,
    "mypy-overrides": mypy_overrides_block,
    "complexipy": complexipy_block,
    "coverage": coverage_block,
    "pytest-warnings": pytest_block,
}


def _markdown_values(repo: Repository) -> dict[str, object]:
    """The placeholders the CONTRIBUTING templates share."""
    values = standard()
    return {
        "trees": _quoted(repo.trees),
        "rules": _quoted(values["ruff"]["select"]),
        "security": _quoted(values["ruff"]["security"]),
        "banned_api_rule": f"`{values['ruff']['banned-api-rule']}`",
        "line_length": values["ruff"]["line-length"],
        "python_version": values["mypy"]["python_version"],
        "max_complexity": values["complexipy"]["max-complexity-allowed"],
        "fail_under": values["coverage"]["fail_under"],
        "exclude_also": _quoted(values["coverage"]["exclude_also"]),
        "filterwarnings": toml_value(values["pytest"]["filterwarnings"]),
    }


def _cell(text: str) -> str:
    """Text safe inside a Markdown table cell."""
    return text.replace("|", "\\|")


def ban_rows(repo: Repository) -> list[str]:
    """CONTRIBUTING's ban table: a row per group, per own ban, and per kind of layering ban."""
    rows = [(ban.names, ban.message, ban.why) for ban in group_bans(repo)]
    rows += [((name,), message, "-") for name, message in repo.bans.items()]
    kinds: dict[str, list[str]] = {}
    for other in layering(repo):
        kinds.setdefault(relation(repo, other), []).append(other)
    why = standard()["layering-why"]
    rows += [(tuple(names), "-", why[kind]) for kind, names in kinds.items()]
    return [f"| {_quoted(names)} | {_cell(use)} | {_cell(reason)} |" for names, use, reason in rows]


def rewrap(markdown: str, width: int = 76) -> str:
    """Markdown re-wrapped at ``width``: each paragraph and bullet; tables are left alone.

    The templates are wrapped by hand, but the values substituted into them
    (a tree list, a rule list) vary in length, so a rendered line would
    otherwise run on or stop short.
    """
    paragraphs = []
    for paragraph in markdown.strip("\n").split("\n\n"):
        if paragraph.startswith("|"):
            paragraphs.append(paragraph)
            continue
        items = re.split(r"\n(?=- )", paragraph)
        paragraphs.append("\n".join(_fill(item, width) for item in items))
    return "\n\n".join(paragraphs) + "\n"


def _fill(item: str, width: int) -> str:
    """One paragraph or ``- `` bullet item, filled to ``width`` (a bullet's lines indented)."""
    bullet = item.startswith("- ")
    return textwrap.fill(
        " ".join(item.split()),
        width,
        subsequent_indent="  " if bullet else "",
        break_long_words=False,
        break_on_hyphens=False,
    )


def contributing_blocks(repo: Repository) -> dict[str, str]:
    """The managed sections of CONTRIBUTING.md for ``repo``'s role, rendered."""
    values = _markdown_values(repo)
    rendered = {
        "code-standard": template("code-standard.md").substitute(values),
        "banned-apis": template("banned-apis.md").substitute(
            values, rows="\n".join(ban_rows(repo))
        ),
        "devtools": template("devtools.md").substitute(values),
    }
    return {
        name: rewrap(rendered[name]) for name in declarations()["roles"][repo.role]["contributing"]
    }


def expected(checkout: Path) -> dict[str, tuple[Style, dict[str, str]]]:
    """Per managed file of ``checkout``: its marker style and every block, rendered."""
    repo = repository(checkout)
    pyproject = {name: _PYPROJECT_RENDERERS[name](repo) for name in PYPROJECT_BLOCKS}
    return {
        "pyproject.toml": (TOML, pyproject),
        "CONTRIBUTING.md": (MARKDOWN, contributing_blocks(repo)),
    }


@dataclass(frozen=True, slots=True)
class Span:
    """Where one block's content sits in a file's lines: ``lines[start:stop]``."""

    name: str
    start: int
    stop: int


def spans(lines: Sequence[str]) -> list[Span]:
    """Every managed block of a file, in order.

    Raises :class:`SyncError` for markers that do not pair up: an end
    without its begin, a begin without its end, a block inside a block, or
    one block twice.
    """
    found: list[Span] = []
    opened: tuple[str, int] | None = None
    for index, name, edge in _markers(lines):
        if edge == ">>>" and opened is None:
            opened = (name, index + 1)
        elif edge == "<<<" and opened is not None and opened[0] == name:
            found.append(Span(name, opened[1], index))
            opened = None
        else:
            raise SyncError(f"line {index + 1}: unexpected {edge} marker of block {name!r}")
    if opened is not None:
        raise SyncError(f"block {opened[0]!r} is never closed")
    _refuse_repeats([span.name for span in found])
    return found


def _markers(lines: Sequence[str]) -> list[tuple[int, str, str]]:
    """``(index, block, edge)`` for every marker line, ``edge`` being ``>>>`` or ``<<<``."""
    return [
        (index, marker.group("name"), marker.group("edge"))
        for index, line in enumerate(lines)
        if (marker := _MARKER.match(line.strip()))
    ]


def _refuse_repeats(names: Sequence[str]) -> None:
    """A block twice in one file would be written twice: refuse (:class:`SyncError`)."""
    twice = sorted({name for name in names if names.count(name) > 1})
    if twice:
        raise SyncError(f"blocks {twice} appear more than once")


@dataclass(frozen=True, slots=True)
class Outcome:
    """What syncing one file would do: its new text, the blocks it changes, its problems."""

    text: str
    changed: tuple[str, ...]
    problems: tuple[str, ...]


def apply(text: str, style: Style, blocks: Mapping[str, str]) -> Outcome:
    """``text`` with every block of ``blocks`` rewritten between its markers.

    A block whose markers are missing, and a block the standard does not
    have, are problems (the text is left as it was for them).
    """
    lines = text.splitlines()
    try:
        found = spans(lines)
    except SyncError as error:
        return Outcome(text, (), (str(error),))
    # Last to first: a rewritten block may change length, shifting what follows it.
    changed = [span.name for span in reversed(found) if _rewrite(lines, span, style, blocks)]
    changed.reverse()
    ending = "\n" if text.endswith("\n") else ""
    problems = _marker_problems({span.name for span in found}, style, blocks)
    return Outcome("\n".join(lines) + ending, tuple(changed), tuple(problems))


def _marker_problems(present: set[str], style: Style, blocks: Mapping[str, str]) -> list[str]:
    """A problem per block of the standard the file lacks, and per block it has that is not one."""
    missing = [
        f"no managed block {name!r}: add its markers ({style.begin(name)!r} ... "
        f"{style.end(name)!r}), then run `xrddevtools sync`"
        for name in blocks
        if name not in present
    ]
    return missing + [
        f"block {name!r} is not part of the standard" for name in sorted(present - set(blocks))
    ]


def _rewrite(lines: list[str], span: Span, style: Style, blocks: Mapping[str, str]) -> bool:
    """Write ``span``'s block, markers included, into ``lines``; whether anything changed.

    The begin marker is rewritten too, so one whose wording has drifted is
    restored; a block the standard does not have is left as it is.
    """
    if span.name not in blocks:
        return False
    new = [style.begin(span.name), *blocks[span.name].rstrip("\n").splitlines()]
    new.append(style.end(span.name))
    old = lines[span.start - 1 : span.stop + 1]
    lines[span.start - 1 : span.stop + 1] = new
    return old != new


def sync_file(path: Path, style: Style, blocks: Mapping[str, str]) -> Outcome:
    """The outcome of syncing ``path`` (a missing file is one problem: create it first)."""
    if not path.is_file():
        return Outcome("", (), (f"missing: create it with the markers of {sorted(blocks)}",))
    outcome = apply(path.read_text(encoding="utf-8"), style, blocks)
    if style is TOML and not outcome.problems:
        try:
            tomllib.loads(outcome.text)
        except tomllib.TOMLDecodeError as error:
            return Outcome(outcome.text, outcome.changed, (f"would not be valid TOML: {error}",))
    return outcome


def drift(checkout: Path) -> list[tuple[str, str]]:
    """``(file, problem)`` for each managed block that is not what sync would write."""
    found: list[tuple[str, str]] = []
    try:
        files = expected(checkout)
    except SyncError as error:
        return [("repositories.toml", str(error))]
    for name, (style, blocks) in files.items():
        outcome = sync_file(checkout / name, style, blocks)
        found += [(name, problem) for problem in outcome.problems]
        found += [
            (name, f"managed block {block!r} differs from the standard: run `xrddevtools sync`")
            for block in outcome.changed
        ]
    return found


def sync(checkout: Path, *, write: bool) -> list[str]:
    """Sync every managed file of ``checkout``: one report line per change or problem.

    With ``write`` false nothing is written (``--check``). A file with a
    problem is never written, so a half-synced file cannot result.
    """
    report: list[str] = []
    for name, (style, blocks) in expected(checkout).items():
        path = checkout / name
        outcome = sync_file(path, style, blocks)
        where = f"{checkout.name}/{name}"
        report += [f"{where}: {problem}" for problem in outcome.problems]
        if outcome.changed and not outcome.problems:
            verb = "updated" if write else "would update"
            report.append(f"{where}: {verb} {', '.join(outcome.changed)}")
            if write:
                path.write_text(outcome.text, encoding="utf-8")
    return report


def parser() -> argparse.ArgumentParser:
    """The command line: ``--workspace DIR`` or ``--repo PATH`` (repeatable), ``--check``."""
    top = argparse.ArgumentParser(
        prog="xrddevtools sync", description=(__doc__ or "").split("\n\n")[0]
    )
    where = top.add_mutually_exclusive_group(required=True)
    where.add_argument("--workspace", type=Path, help="every repository in this directory")
    where.add_argument("--repo", dest="repos", type=Path, action="append", help="one checkout")
    top.add_argument("--check", action="store_true", help="write nothing; fail if out of date")
    return top


def main(argv: Sequence[str] | None = None) -> int:
    """Sync (or check) and print what changed; 1 for a problem, or any drift with ``--check``."""
    options = parser().parse_args(argv)
    targets = _targets(options)
    if not targets:
        print(f"sync: no repository of the chain in {options.workspace}", file=sys.stderr)
        return 2
    try:
        report = [line for target in targets for line in sync(target, write=not options.check)]
    except SyncError as error:
        print(f"sync: {error}", file=sys.stderr)
        return 2
    print("\n".join(report) or f"sync: {len(targets)} repositories are up to date")
    # Anything but an "updated" line is a problem, or (with --check) drift.
    return int(any(": updated " not in line for line in report))


def _targets(options: argparse.Namespace) -> list[Path]:
    """Each ``--repo``, or the chain's repositories (and xrd-devtools) in ``--workspace``."""
    if options.workspace is None:
        return [Path(path).resolve() for path in options.repos]
    return checkouts(options.workspace, [*REPOSITORIES, "xrd-devtools"])


if __name__ == "__main__":
    raise SystemExit(main())
