"""The config-conformance check: one standard, the same in every repository.

The chain holds every repository to one code standard (no grandfathered
code): the same ruff rules with nothing ignored, mypy strict over the tests
too, complexipy at 15 with no snapshot, 100 % line-and-branch coverage with
the same exclusions, and every warning an error. Each repository states the
standard in its own ``pyproject.toml``, so it can drift one edit at a time;
``xrddevtools conformance --workspace DIR`` (or ``--repo PATH``) proves it
has not. A repository conforms when:

* **ruff**: ``line-length = 100``, ``target-version = "py311"``; the rule
  selection is exactly :data:`STANDARD_SELECT` (plus ``TID251`` when the
  repository bans imports); nothing is ignored, there are no per-file
  ignores, and no other knob (``exclude``, ``extend-ignore``, ...) is set;
* **mypy**: ``strict = true`` for Python 3.11 over every Python tree; an
  override may only (a) let *test* functions go unannotated while their
  bodies stay checked, (b) set ``ignore_missing_imports`` for named
  *third-party* modules, or (c) ``follow_imports = "normal"``;
* **complexipy**: ``max-complexity-allowed = 15``, ``failed = true``, over
  every Python tree;
* **coverage**: ``branch = true``, ``fail_under = 100``, exactly
  :data:`STANDARD_EXCLUDE_ALSO`, and no ``omit``;
* **pytest**: ``filterwarnings`` starts with ``"error"``, any further entry
  ignores one exact category in one named module, and ``addopts`` does not
  switch warnings off;
* **no suppression comment** lacks its error code and a reason (same line, or
  the comment just above), and none of the kinds that are never acceptable
  (``pragma: no cover``, ``complexipy: ignore``, ``fmt: off``, ...) appears;
* **no snapshot or baseline file** (:data:`SNAPSHOT_FILES`) exists;
* **every managed block** of ``pyproject.toml`` and ``CONTRIBUTING.md`` is
  what ``xrddevtools sync`` would write (:func:`xrddevtools.sync.drift`), so
  a hand edit between the markers fails CI.

The values themselves (the rule selection, the exclusions, ...) are read
from ``standard/standard.toml``, the file ``sync`` renders: the check and
the blocks cannot disagree about what the standard is. The semantic checks
below still matter: they hold the keys *outside* the managed blocks (mypy's
``files``, extra overrides, ...) to the standard, and they explain a
departure in terms of the setting rather than as a changed block.

The documented per-role differences (:mod:`xrddevtools.workspace` names the
roles) are what the check does *not* compare: the banned-API list
(``[tool.ruff.lint.flake8-tidy-imports.banned-api]``, a library's and a
client's differ; every entry must still say what to use instead), which
third-party modules mypy may treat as untyped, the coverage ``source`` and
the pytest paths and markers.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import tokenize
import tomllib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xrddevtools import sync
from xrddevtools.workspace import (
    OWN_PACKAGES,
    REPOSITORIES,
    ROLES,
    SKIPPED_PARTS,
    checkouts,
    python_files,
    python_trees,
)

#: The standard's values, from the one place they are written down
#: (``standard/standard.toml``, which :mod:`xrddevtools.sync` also renders).
_STANDARD = sync.standard()
#: The ruff rule selection every repository uses, in no particular order.
STANDARD_SELECT = frozenset(_STANDARD["ruff"]["select"] + _STANDARD["ruff"]["security"])
#: The banned-API rule; selected exactly when a banned-API table exists.
BANNED_API_RULE: str = _STANDARD["ruff"]["banned-api-rule"]
#: The settings whose values are the standard, table by table.
RUFF_SETTINGS = {key: _STANDARD["ruff"][key] for key in ("line-length", "target-version")}
MYPY_SETTINGS = dict(_STANDARD["mypy"])
COMPLEXIPY_SETTINGS = dict(_STANDARD["complexipy"])
COVERAGE_RUN_SETTINGS = {"branch": True}
#: ``[tool.ruff]`` and ``[tool.ruff.lint]`` keys the standard knows about.
RUFF_KEYS = frozenset({"line-length", "target-version", "src", "lint"})
LINT_KEYS = frozenset({"select", "ignore", "isort", "flake8-tidy-imports"})
#: ``[tool.mypy]`` keys the standard knows about; anything else is a finding.
MYPY_KEYS = frozenset(
    {
        "python_version",
        "strict",
        "files",
        "mypy_path",
        "explicit_package_bases",
        "untyped_calls_exclude",
        "overrides",
    }
)
#: The only relaxations a test-module override may make.
TEST_RELAXATIONS = {"disallow_untyped_defs": False, "disallow_incomplete_defs": False}
#: ``[tool.complexipy]`` keys the standard knows about.
COMPLEXIPY_KEYS = frozenset({"paths", "max-complexity-allowed", "failed", "quiet"})
#: The coverage exclusions every repository shares, in this order.
STANDARD_EXCLUDE_ALSO: tuple[str, ...] = tuple(_STANDARD["coverage"]["exclude_also"])
COVERAGE_REPORT_SETTINGS = {
    "fail_under": _STANDARD["coverage"]["fail_under"],
    "exclude_also": list(STANDARD_EXCLUDE_ALSO),
}
#: Coverage settings that exempt code from the gate by configuration.
COVERAGE_EXEMPTIONS = frozenset({"omit", "exclude_lines", "partial_branches"})
#: Files that freeze old findings so only new ones fail: never allowed.
SNAPSHOT_FILES = frozenset(
    {
        "complexipy-snapshot.json",
        "mypy-baseline.txt",
        ".mypy-baseline.txt",
        "ruff-baseline.json",
        ".ruff-baseline",
        ".pylint-baseline",
        "pylint-baseline.txt",
        ".secrets.baseline",
        ".flake8-baseline",
        "coverage-baseline.json",
    }
)
#: Suppressions that need an error code and a reason: ``noqa``, ``type: ignore``
#: and ``pyright: ignore``. ``code`` is the specific code (``: SIM115``,
#: ``[arg-type]``) and ``rest`` whatever follows it on the line.
_CODED = (
    re.compile(r"#\s*(?P<kind>noqa)(?P<code>:\s*[A-Z]+\d+(\s*,\s*[A-Z]+\d+)*)?(?P<rest>.*)"),
    re.compile(r"#\s*(?P<kind>(type|pyright):\s*ignore)(?P<code>\[[\w\-, ]+\])?(?P<rest>.*)"),
)
#: Suppressions that are never acceptable, whatever they say.
_FORBIDDEN = re.compile(
    r"#\s*(pragma:\s*no\s*(cover|branch)|complexipy:\s*ignore|nosec|fmt:\s*(off|skip)"
    r"|ruff:\s*noqa|mypy:\s*(ignore-errors|disable-error-code)|isort:\s*skip)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One way a repository departs from the standard."""

    repo: str
    where: str
    message: str

    def __str__(self) -> str:
        """``repo: where: message``, one finding per line of the report."""
        return f"{self.repo}: {self.where}: {self.message}"


def _table(document: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    """The nested table at ``keys``, or an empty one where any level is missing."""
    node: Any = document
    for key in keys:
        node = node.get(key, {}) if isinstance(node, dict) else {}
    return node if isinstance(node, dict) else {}


def _unknown(table: Mapping[str, Any], known: Iterable[str], where: str) -> list[str]:
    """A message per key of ``table`` the standard does not know (``where`` names the table)."""
    return [
        f"{where}: {key!r} is not part of the standard" for key in sorted(set(table) - set(known))
    ]


def _covers(entries: Sequence[str], tree: str) -> bool:
    """Whether a list of paths (mypy ``files``, complexipy ``paths``) includes ``tree``."""
    normalised = [entry.strip("/").removeprefix("./") or "." for entry in entries]
    return any(e in (tree, ".") or e.startswith(tree + "/") for e in normalised)


def _uncovered(where: str, entries: str | Sequence[str], trees: Sequence[str]) -> list[str]:
    """A message per Python tree a path list (``where``) leaves out."""
    paths = [entries] if isinstance(entries, str) else entries
    return [f"{where} leave out {tree}/" for tree in trees if not _covers(paths, tree)]


def _settings(where: str, table: Mapping[str, Any], standard: Mapping[str, object]) -> list[str]:
    """A message per setting of ``table`` (``where``) that is not the ``standard`` value."""
    return [
        f"{where}: {key} is {table.get(key)!r}, not {value!r}"
        for key, value in standard.items()
        if table.get(key) != value
    ]


def ruff_problems(pyproject: Mapping[str, Any]) -> list[str]:
    """Departures of ``[tool.ruff]`` from the standard."""
    ruff = _table(pyproject, "tool", "ruff")
    lint = _table(ruff, "lint")
    problems = _unknown(ruff, RUFF_KEYS, "tool.ruff") + _unknown(lint, LINT_KEYS, "tool.ruff.lint")
    problems += _settings("tool.ruff", ruff, RUFF_SETTINGS)
    select = set(lint.get("select", []))
    missing = sorted(STANDARD_SELECT - select)
    extra = sorted(select - STANDARD_SELECT - {BANNED_API_RULE})
    if missing or extra:
        problems.append(f"tool.ruff.lint: select misses {missing} and adds {extra}")
    if lint.get("ignore", []):
        problems.append(f"tool.ruff.lint: ignores {lint['ignore']}")
    return problems + banned_api_problems(lint, BANNED_API_RULE in select)


def banned_api_problems(lint: Mapping[str, Any], selected: bool) -> list[str]:
    """The banned-API table: present exactly when ``TID251`` is, every entry with a message.

    Its contents differ by role (a library may use what a client must not),
    so they are not compared; but a ban that does not say what to use instead
    only tells a contributor what they cannot do.
    """
    tidy = _table(lint, "flake8-tidy-imports")
    where = "tool.ruff.lint.flake8-tidy-imports"
    banned = _table(tidy, "banned-api")
    problems = _unknown(tidy, ["banned-api"], where)
    if selected != bool(banned):
        problems.append(
            f"{where}: {BANNED_API_RULE} is selected but nothing is banned"
            if selected
            else f"{where}: bans imports but {BANNED_API_RULE} is not selected"
        )
    return problems + [
        f"{where}.banned-api: {name!r} has no msg naming the replacement"
        for name, entry in sorted(banned.items())
        if not _says_something(entry)
    ]


def _says_something(entry: object) -> bool:
    """Whether a banned-API entry carries a non-empty ``msg``."""
    message = entry.get("msg") if isinstance(entry, dict) else None
    return isinstance(message, str) and bool(message.strip())


def mypy_problems(pyproject: Mapping[str, Any], trees: Sequence[str]) -> list[str]:
    """Departures of ``[tool.mypy]`` and its overrides from the standard."""
    mypy = _table(pyproject, "tool", "mypy")
    problems = _unknown(mypy, MYPY_KEYS, "tool.mypy") + _settings("tool.mypy", mypy, MYPY_SETTINGS)
    problems += _uncovered("tool.mypy: files", mypy.get("files", []), trees)
    own = sorted(_own(mypy.get("untyped_calls_exclude", [])))
    if own:
        problems.append(f"tool.mypy: untyped_calls_exclude names our own modules {own}")
    for index, override in enumerate(mypy.get("overrides", [])):
        problems += [f"tool.mypy.overrides[{index}]: {p}" for p in override_problems(override)]
    return problems


def _own(modules: Iterable[str]) -> set[str]:
    """The entries of ``modules`` (dotted names or ``pkg.*`` patterns) that are the chain's."""
    return {module for module in modules if module.split(".")[0] in OWN_PACKAGES}


def override_problems(override: Mapping[str, Any]) -> list[str]:
    """Why one mypy override is not one of the three the standard allows.

    (a) test modules only: ``disallow_untyped_defs``/``disallow_incomplete_defs``
    false with ``check_untyped_defs`` true; (b) third-party modules only:
    ``ignore_missing_imports = true``; (c) ``follow_imports = "normal"``,
    which is mypy's default spelled out.
    """
    modules = override.get("module", [])
    modules = [modules] if isinstance(modules, str) else list(modules)
    settings = {key: value for key, value in override.items() if key != "module"}
    if settings == {"follow_imports": "normal"}:
        return []
    if "ignore_missing_imports" in settings:
        return _third_party_override_problems(modules, settings)
    return _test_override_problems(modules, settings)


def _third_party_override_problems(
    modules: Sequence[str], settings: Mapping[str, Any]
) -> list[str]:
    """Problems with an ``ignore_missing_imports`` override: alone, and third-party only."""
    if settings != {"ignore_missing_imports": True}:
        return [f"ignore_missing_imports must stand alone, got {sorted(settings)}"]
    own = sorted(_own(modules))
    return [f"ignore_missing_imports for our own modules {own}"] if own else []


def _test_override_problems(modules: Sequence[str], settings: Mapping[str, Any]) -> list[str]:
    """Problems with an override relaxing annotation rules (allowed for tests only)."""
    relaxed = {key: value for key, value in settings.items() if key != "check_untyped_defs"}
    if not relaxed or not relaxed.items() <= TEST_RELAXATIONS.items():
        return [f"{dict(settings)} is not an allowed override"]
    problems = []
    if {module.split(".")[0] for module in modules} != {"tests"}:
        problems.append(f"relaxes annotations for non-test modules {list(modules)}")
    if settings.get("check_untyped_defs") is not True:
        problems.append("relaxes annotations without check_untyped_defs = true")
    return problems


def complexipy_problems(pyproject: Mapping[str, Any], trees: Sequence[str]) -> list[str]:
    """Departures of ``[tool.complexipy]`` from the standard."""
    complexipy = _table(pyproject, "tool", "complexipy")
    return (
        _unknown(complexipy, COMPLEXIPY_KEYS, "tool.complexipy")
        + _settings("tool.complexipy", complexipy, COMPLEXIPY_SETTINGS)
        + _uncovered("tool.complexipy: paths", complexipy.get("paths", []), trees)
    )


def coverage_problems(pyproject: Mapping[str, Any]) -> list[str]:
    """Departures of ``[tool.coverage]`` from the standard (``source`` is the repository's)."""
    run = _table(pyproject, "tool", "coverage", "run")
    report = _table(pyproject, "tool", "coverage", "report")
    problems = _settings("tool.coverage.run", run, COVERAGE_RUN_SETTINGS)
    problems += _settings("tool.coverage.report", report, COVERAGE_REPORT_SETTINGS)
    if not run.get("source"):
        problems.append("tool.coverage.run: no source")
    for table, name in ((run, "run"), (report, "report")):
        problems += [
            f"tool.coverage.{name}: {key} exempts code from the gate"
            for key in sorted(COVERAGE_EXEMPTIONS & set(table))
        ]
    return problems


def pytest_problems(pyproject: Mapping[str, Any]) -> list[str]:
    """Departures of ``[tool.pytest.ini_options]``: every warning is an error."""
    options = _table(pyproject, "tool", "pytest", "ini_options")
    filters = options.get("filterwarnings", [])
    problems = []
    if filters[:1] != ["error"]:
        problems.append('tool.pytest.ini_options: filterwarnings does not start with "error"')
    problems += [problem for entry in filters[1:] for problem in _filter_problems(entry)]
    addopts = options.get("addopts", "")
    addopts = " ".join(addopts) if isinstance(addopts, list) else addopts
    if re.search(r"--disable-warnings|-p\s*no:warnings|-W\s*\S", addopts):
        problems.append("tool.pytest.ini_options: addopts switches warnings off")
    return problems


def _filter_problems(entry: str) -> list[str]:
    """Why a ``filterwarnings`` entry after ``"error"`` is more than one third-party exemption.

    Python's ``-W`` syntax is ``action:message:category:module:lineno``; the
    only acceptable entry ignores one exact category raised from one named
    module, whatever its message says.
    """
    action, message, category, module = [*entry.split(":"), "", "", ""][:4]
    where = f"tool.pytest.ini_options: filter {entry!r}"
    if action != "ignore" or not category or not module:
        return [f"{where} is not one category in one module"]
    return [f"{where} matches a message"] if message else []


def suppressions(source: str) -> Iterator[tuple[int, str]]:
    """``(line, problem)`` for each unacceptable suppression comment in ``source``.

    Comments are found with :mod:`tokenize`, so a ``# noqa`` inside a string
    is not one. A coded suppression is acceptable with its specific code and a
    reason: text after it on the line, or a comment on the line above (any
    decorators in between skipped, so a comment can explain an ``@overload``).
    """
    lines = source.splitlines()
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        line = token.start[0]
        forbidden = _FORBIDDEN.search(token.string)
        if forbidden:
            yield line, f"{forbidden.group(1)!r} is never allowed: make the code pass"
            continue
        yield from _coded_problem(token.string, line, lines)


def _coded_problem(comment: str, line: int, lines: Sequence[str]) -> Iterator[tuple[int, str]]:
    """The problem with a ``noqa``/``type: ignore`` comment, if it has one."""
    for pattern in _CODED:
        coded = pattern.search(comment)
        if coded is None:
            continue
        kind = coded.group("kind")
        if coded.group("code") is None:
            yield line, f"{kind!r} without a specific error code"
        elif not (_reason_after(coded.group("rest")) or _reason_above(line, lines)):
            yield line, f"{kind!r} without a reason (same line, or a comment just above)"
        return


def _reason_after(rest: str) -> bool:
    """Whether the text after the code (``# why``, ``- why``) says something."""
    return bool(rest.strip().lstrip("#-:\u2014 ").strip())


def _reason_above(line: int, lines: Sequence[str]) -> bool:
    """Whether a comment line sits just above ``line`` (decorators in between skipped)."""
    index = line - 2
    while index >= 0 and lines[index].strip().startswith("@"):
        index -= 1
    return index >= 0 and lines[index].strip().startswith("#")


def suppression_findings(checkout: Path, trees: Sequence[str]) -> list[Finding]:
    """Every unacceptable suppression comment in the checkout's Python trees."""
    findings = []
    for tree in trees:
        for path in python_files(checkout / tree):
            relative = path.relative_to(checkout).as_posix()
            try:
                found = list(suppressions(path.read_text(encoding="utf-8")))
            except (SyntaxError, UnicodeDecodeError, tokenize.TokenError):
                found = [(1, "cannot be tokenized")]
            findings += [Finding(checkout.name, f"{relative}:{n}", p) for n, p in found]
    return findings


def snapshot_findings(checkout: Path) -> list[Finding]:
    """A finding for every snapshot or baseline file anywhere in the checkout."""
    return [
        Finding(checkout.name, path.relative_to(checkout).as_posix(), "snapshot/baseline file")
        for path in sorted(checkout.rglob("*"))
        if path.name in SNAPSHOT_FILES
        and not SKIPPED_PARTS.intersection(path.relative_to(checkout).parts)
    ]


def check_repository(checkout: Path) -> list[Finding]:
    """Every finding for one checkout (its directory name is its repository name)."""
    name = checkout.name
    if name not in ROLES:
        return [Finding(name, str(checkout), f"not a repository of the chain ({sorted(ROLES)})")]
    pyproject = tomllib.loads((checkout / "pyproject.toml").read_text(encoding="utf-8"))
    trees = python_trees(checkout)
    problems = (
        ruff_problems(pyproject)
        + mypy_problems(pyproject, trees)
        + complexipy_problems(pyproject, trees)
        + coverage_problems(pyproject)
        + pytest_problems(pyproject)
    )
    findings = [Finding(name, "pyproject.toml", problem) for problem in problems]
    findings += [Finding(name, where, problem) for where, problem in sync.drift(checkout)]
    # Type stubs (xrdclient's ``typings/``) are not gated by complexipy, but a
    # suppression in one hides an error all the same.
    scanned = [*trees, *(["typings"] if (checkout / "typings").is_dir() else [])]
    return findings + suppression_findings(checkout, scanned) + snapshot_findings(checkout)


def parser() -> argparse.ArgumentParser:
    """The command line: ``--workspace DIR`` (every repository in it) or ``--repo PATH``."""
    top = argparse.ArgumentParser(
        prog="xrddevtools conformance", description=(__doc__ or "").split("\n\n")[0]
    )
    where = top.add_mutually_exclusive_group(required=True)
    where.add_argument("--workspace", type=Path, help="check every repository in this directory")
    where.add_argument("--repo", dest="repos", type=Path, action="append", help="one checkout")
    top.add_argument("--json", action="store_true", help="print the findings as a JSON list")
    return top


def main(argv: Sequence[str] | None = None) -> int:
    """Check, print one finding per line (or JSON), and return 1 if there were any.

    ``--workspace`` checks the chain's repositories and this one, where
    present; a workspace holding none of them is a usage error.
    """
    options = parser().parse_args(argv)
    targets = _targets(options)
    if not targets:
        print(f"conformance: no repository of the chain in {options.workspace}", file=sys.stderr)
        return 2
    findings = [finding for target in targets for finding in check_repository(target)]
    if options.json:
        print(json.dumps([vars_of(finding) for finding in findings], indent=2))
    else:
        print("\n".join(map(str, findings)) or f"conformance: {len(targets)} repositories conform")
    return int(bool(findings))


def _targets(options: argparse.Namespace) -> list[Path]:
    """The checkouts to check: each ``--repo``, or the chain's (and this one) in ``--workspace``."""
    if options.workspace is None:
        return [Path(path).resolve() for path in options.repos]
    return checkouts(options.workspace, [*REPOSITORIES, "xrd-devtools"])


def vars_of(finding: Finding) -> dict[str, str]:
    """A finding as a JSON object."""
    return {"repo": finding.repo, "where": finding.where, "message": finding.message}


if __name__ == "__main__":
    raise SystemExit(main())
