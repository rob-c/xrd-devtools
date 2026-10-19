# Changelog

Notable changes are recorded here. This repository is versioned with the
chain whose gates it enforces; it is never uploaded to PyPI.

## [0.4.0] - Unreleased

### Added

- First release: the chain's tooling, gathered from where each piece had
  grown up.
- From xrdclient's `tools/`: `scorecard` (with `scorecard_baseline.json`,
  now package data with a `load_baseline()` and a default `--baseline`),
  `maintainability`, `chain_preflight`, `distribution_package`,
  `homebrew_package`, `installed_smoke` and `platforms`, with their tests
  (xrdclient's `tests/test_scorecard.py`, `tests/test_maintainability.py`
  and `tests/test_distribution_tools.py`, now covered to 100 %). xrdclient
  keeps only what is its own: `tools/pyxrootd_upstream.py`, the benchmarks
  and its `maintainability.json` policy.
- From xrdclient's `packaging/`: `linux.sh`, `platforms.json` and the Nix
  expression (`packaging/nix`), which now default to sibling checkouts of
  this one. The container matrix mounts this checkout as `/src/xrd-devtools`
  and runs the package builder and the smoke test from it.
- From the libraries' identical `.github/scripts/check_dist.py`:
  `check_dist`.
- `xrddevtools`, one command for all of them (`xrddevtools <command>`), each
  module imported only when its command runs.
- `pytest_plugin`: the take-over scorecard and the maintainability check as
  pytest tests (`scorecard_gate`, `maintainability_gate`), replacing the six
  near-identical `tests/test_scorecard.py` copies (547 lines) with five lines
  each. Without a checkout the gates skip for that environmental reason;
  `pytest -p xrddevtools.pytest_plugin` and `XRD_DEVTOOLS_REQUIRED=1` make
  them required (CI).
- `dupes`: the duplicate gate, promoted from an evaluation script - fails on a
  structurally identical function (six or more statements; names, parameters
  and strings normalised) in two repositories, `src` and `tests` alike, warns
  on drifting same-named copies. No allow-list.
- `conformance`: every repository's tool configuration against the standard,
  suppression comments and snapshot files included.
- `maintainability.STANDARD_LIMITS` and `policy_problems()`: the `check`
  gate refuses a policy looser than the standard or with exclusions.
- Reusable workflows `static.yml`, `tests.yml`, `chain.yml`, `package.yml`
  and `platforms.yml` (the last moved from xrdclient), and the one
  `candidates` composite action replacing xrdclient's
  `.github/actions/library-candidates` and xgfalclient's
  `.github/actions/candidates`.

- `preflight` enforces the release order: an extra may only require a
  package released before its own (xrdhttp's `testing` extra on xrdsec, so
  xrdsec is published first). xrdwms is the seventh and last package of the
  0.4.0 release (it requires every library and both storage clients, xrdarc
  through its `arc` extra); xrd-devtools is never released.
- xrd-devtools is held to its own gates, the take-over scorecard included
  (`tests/test_scorecard.py`; the scorecard's unit tests are
  `tests/test_scorecard_metrics.py`).
- Banned APIs: ruff `TID251` bans importing any package of the chain in
  this repository's code, which reads checkouts and wheels and never
  imports what it checks (CONTRIBUTING.md).
- `release` and `pytest_plugin.release_gate(__file__)`: the release-metadata
  gate every repository's `tests/test_release_metadata.py` now is - one
  version source (`src/<package>/_version.py`, dynamic, read by hatch, no
  second literal `__version__`), a CHANGELOG whose well-formed sections run
  newest first and start with exactly that version (and link it, where the
  file keeps link references), `readme`, `requires-python`, the Python
  classifiers and the repository/issues/changelog URLs naming this
  repository, the license files, `site/` ignored beside `mkdocs.yml`, and a
  release workflow built through the shared publish action with a
  trusted-publishing upload. Replaces xrdclient's and xgfalclient's
  near-identical `tests/test_release_metadata.py`. `release installed` is
  the publish job's check of the wheel it installed (tag, metadata, runtime
  `__version__`, dated changelog section, no default dependency, modules
  that must not be installed), replacing the five workflows' inline
  scripts; `release.py` is a plain standard-library file.
- The `publish` composite action (`.github/actions/publish`): every step of
  the chain's release builds but the upload - checkout, build, `twine check
  --strict`, `check_dist` contents and imports (`check-dist`), the PyPI
  pre-flight (`preflight`), a clean install with `pip check`, `release
  installed`, `post-install` checks, the suite against the installed wheel
  (`suite-requirements`), the artifact upload - running the tools from the
  action's own copy of this repository. every package's `publish.yml`
  (xrdcore, xrdsec, xrdhttp, xrdclient, xgfalclient, xrdarc, xrdwms) uses
  it; the PyPI upload stays in each caller's job, as trusted publishing
  requires.
- `sync` (`xrddevtools sync --workspace DIR | --repo PATH [--check]`) and
  the standard as data (`src/xrddevtools/standard/`: `standard.toml` with
  the values and the ban groups, `repositories.toml` with each repository's
  declared differences - role, what it may import, what it owns, its
  coverage source and threads, its named untyped modules - and the block
  templates). It writes the managed blocks of every repository's
  `pyproject.toml` (`ruff`, `banned-api`, `mypy`, `mypy-overrides`,
  `complexipy`, `coverage`, `pytest-warnings`) and `CONTRIBUTING.md`
  (`code-standard`, `banned-apis`, `devtools`) between marker comments,
  leaving everything else alone; the banned-API tables and CONTRIBUTING's
  ban tables come from the same ban groups and layering. All eight
  repositories are synced.
- `conformance` also fails a managed block that is not what `sync` would
  write (a hand edit, a missing block, an unknown one, markers that do not
  pair up), and reads the standard's values from `standard.toml` instead of
  its own copy.
- `dupes` is a hard gate on near-duplicates too: a same-named function in
  another repository 0.85 or more alike fails it like an exact copy
  (`--fail-near SIMILARITY` sets another threshold; there is no warn-only
  switch and no allow-list), and CI's chain job runs it so. Below 0.85 (down
  to `--similarity`, 0.8) it still warns.

### Fixed

- `maintainability`: integral limits and metrics printed on Python 3.11
  (`int.is_integer` arrived in 3.12).

- `installed_smoke`: xrdarc's packages are checked with all three libraries
  (its A-REX transport is xrdhttp's session since 0.4.0).
