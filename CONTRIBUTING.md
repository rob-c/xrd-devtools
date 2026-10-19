# Contributing to xrd-devtools

Every repository of the chain trusts what this one says: a gate that passes
wrongly lets a regression into seven code bases at once, and one that fails
wrongly blocks them all. Changes are welcome when they keep that trust.

## Development setup

Python 3.11 through 3.14. The tools need radon, complexipy and packaging;
the suite needs the `dev` extra:

```console
$ python3 -m venv .venv
$ .venv/bin/python -m pip install -e '.[dev]'
$ .venv/bin/pytest -q -n auto
```

`pyproject.toml` puts `src` on the test path. The suite is hermetic: the
packaging tools are tested with synthetic wheels and a fake `subprocess`,
never a real container, package manager or index.

## Definition of done

```console
$ ruff check src tests
$ ruff format --check src tests
$ mypy
$ complexipy --quiet
$ pytest -q -p no:cacheprovider -n auto --cov --cov-report=term-missing:skip-covered
$ xrddevtools conformance --repo .
```

- **100 % of lines and branches** (`fail_under = 100`), reached with a test
  that makes the case happen.
- **The same standard as the chain** ([the code
  standard](#the-code-standard)), checked by its own conformance check, and
  the same release-metadata gate (`tests/test_release_metadata.py`).
- **Plain-file modules stay plain.** `installed_smoke.py`,
  `distribution_package.py`, `chain_preflight.py`, `check_dist.py` and
  `release.py` run as `python path/to/module.py` where this package is not
  installed; they import only the standard library (and `packaging`), and no
  module here may share a name with a standard-library or dependency module
  (`tests/test_package.py` pins both).
- **A gate changes only on purpose.** Tightening the standard is a change to
  every repository: run `xrddevtools conformance --workspace ..` and
  `xrddevtools dupes --workspace ..` over the workspace first, and land the
  repositories' fixes before the stricter gate. Never loosen a gate, add an
  allow-list or re-baseline the scorecard to make a repository pass. How a
  change to the standard reaches every repository: [Changing the
  standard](#changing-the-standard).
- **Changelog.** Every user-visible change, in [CHANGELOG.md](CHANGELOG.md).

## The code standard

<!-- >>> xrddevtools sync: code-standard (generated from xrd-devtools/src/xrddevtools/standard) >>> -->
Every repository of the chain is held to one code standard, and every line
of it is held to the standard: old code passes the same checks as new code.

- **Ruff** selects `E`, `F`, `W`, `I`, `UP`, `B`, `C4`, `SIM`, `RUF`, the
  high-confidence security checks `S102`, `S307`, `S506`, `S602`, `S608`,
  `S609`, `S701` and the banned-API rule `TID251` (see *Banned APIs*), with
  `ignore = []` and no per-file ignores, over `src`, `tests`; `ruff format`
  formats it, 100 columns wide.
- **mypy** is strict (Python 3.11) over `src`, `tests`. A test function may
  leave its fixtures unannotated where `pyproject.toml` allows it, but its
  body is still checked; only a named third-party module that ships no types
  may be imported untyped, with the reason beside it.
- **complexipy** allows no function above 15, over `src`, `tests`, with no
  snapshot of old offenders.
- **Coverage** is 100 % of lines and branches, reached by a test that makes
  each case happen. The only exclusions are `if TYPE_CHECKING:`,
  `@overload`, `raise NotImplementedError`, `\.\.\.$`, `if __name__ ==
  .__main__.:`; there is no `omit`.
- **Every warning is an error** (`filterwarnings = ["error"]`): ours are
  fixed, not filtered.
- **No suppression comment**: no `# noqa`, `# pragma: no cover`, `# fmt:
  off`, `# complexipy: ignore` or `# nosec`. A `# type: ignore[code]` only
  where it cannot be avoided (a test passing a wrongly typed value on
  purpose), with its error code and the reason beside it or just above.
- **No snapshot or baseline** of old findings, for any tool.

The settings behind these rules are the managed blocks of `pyproject.toml`
(between `# >>> xrddevtools sync` markers), and this section is one of the
managed blocks of this file (between `<!-- >>> xrddevtools sync` markers).
`xrddevtools sync` writes them all from the standard, kept once in
xrd-devtools (`src/xrddevtools/standard`); `xrddevtools conformance`, which
CI runs, fails if one is edited by hand. To change the standard, change it
there and run `xrddevtools sync --workspace ..` over every repository.
<!-- <<< xrddevtools sync: code-standard <<< -->

## Banned APIs

<!-- >>> xrddevtools sync: banned-apis (generated from xrd-devtools/src/xrddevtools/standard) >>> -->
Ruff's `TID251` rule refuses the APIs whose job another part of the chain
does, in every tree ruff checks (`src`, `tests`), with no per-file ignore
and no `noqa`; the list is `[tool.ruff.lint.flake8-tidy-imports.banned-api]`
in `pyproject.toml`, and each entry's message names what to use instead. A
need for a banned API is met by the library that owns it growing an API,
with its tests - never by an exception here.

| Banned | Use instead | Why |
|---|---|---|
| `xrdcore`, `xrdsec`, `xrdhttp`, `xrdclient`, `xgfalclient`, `xrdarc`, `xrdwms` | - | the tools judge checkouts and wheels that may be absent, half-refactored or another version: they read files or run a subprocess, never import |
<!-- <<< xrddevtools sync: banned-apis <<< -->

The chain's own ban lists (no raw `http.client`, no bare
`ThreadPoolExecutor`, no unguarded XML parser, ...) name xrdcore, xrdsec
and xrdhttp as the replacements, so they do not apply here: this
repository may not depend on them. `xrddevtools conformance` checks that
every repository selects `TID251` exactly when it has such a table, that
every entry says what to use instead, and that nothing else of
`flake8-tidy-imports` is configured.

## Changing the standard

The standard is written once, here, as data
(`src/xrddevtools/standard/`): `standard.toml` holds its values and the ban
groups, `repositories.toml` every difference a repository may have (its
role, which picks its ban groups; what it may import; what it owns; its
coverage source; the third-party modules mypy may treat as untyped, each
with its reason), and `templates/` the text of each managed block. Every
repository's `pyproject.toml` and `CONTRIBUTING.md` carry those blocks
between `xrddevtools sync` markers - a TOML comment
(`# >>> xrddevtools sync: <block> ... >>>` ... `# <<< xrddevtools sync:
<block> <<<`) or an HTML comment in Markdown - and keep everything of
their own (mypy's `files`, pytest's `markers`, notes on what a library
owns) beside them. To change the standard:

1. Edit the data or a template, with a test here for anything new.
2. `xrddevtools sync --workspace ..` rewrites every block in every
   repository (`--check` only reports, and fails, if one would change). A
   block's markers are placed by hand, once; sync never guesses where a
   block belongs, and refuses a file whose markers do not pair up or whose
   result would not parse.
3. Run every repository's gates, land their fixes with the change, and
   record it in each CHANGELOG.

`xrddevtools conformance` (CI's static and chain jobs) fails a block that
differs from what sync would write, so a hand edit between the markers
cannot land - change the standard here instead, or, for a difference that
really is one repository's, declare it in `repositories.toml`.

## Workflows

The reusable workflows under `.github/workflows` are called by every
repository as `rob-c/xrd-devtools/.github/workflows/<name>.yml@main`, and the
`candidates` and `publish` actions as
`rob-c/xrd-devtools/.github/actions/<name>@main`.
An input a caller passes is API: add inputs with defaults, never rename or
remove one without updating every caller in the same change.
`tests/test_workflows.py` pins the contract.
