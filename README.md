# xrd-devtools

The tooling of the xrdcore / xrdclient chain: everything that serves the
whole chain rather than one package. Seven repositories - the libraries
xrdcore, xrdsec and xrdhttp, the clients xrdclient, xgfalclient and xrdarc,
and xrdwms on top - are held to one code standard and released together;
the gates that enforce the standard, the release pre-flight and the
distribution packaging live here, once, instead of in xrdclient's `tools/`
and in near-identical copies in every repository.

Nothing of the chain imports this package at run time, and it imports nothing
of the chain: it reads their checkouts and their wheels.

## What is here

| Command (`xrddevtools ...`) | Module | What it does |
| --- | --- | --- |
| `scorecard` | `scorecard` | The take-over scorecard: twelve project-level measurements (MI, complexity shares, parameters, nesting, explanation density, docstring and annotation coverage, duplication) against the median of 39 widely used libraries (`scorecard_baseline.json`). |
| `maintainability` | `maintainability` | The per-function gate: CCN, cognitive complexity, NPath, Halstead Volume and nesting against a repository's `maintainability.json` (xrdclient's), never looser than the standard limits. |
| `dupes` | `dupes` | The duplicate gate: fails on a function (six statements or more) whose normalised body appears in two repositories' `src` or `tests`; warns on same-named functions that have started to drift (similarity 0.8 or more) and fails on those 0.85 or more alike (`--fail-near` sets another threshold); no allow-list. |
| `conformance` | `conformance` | Every repository's ruff, mypy, complexipy, coverage and pytest configuration against the one standard; every managed block as `sync` writes it; no suppression comment without its code and a reason; no snapshot or baseline file. |
| `preflight` | `chain_preflight` | Release pre-flight: versions, layering, pins, changelogs and builds of the six released packages (`workspace`), and that a wheel's chain requirements are already on PyPI (`pypi`). |
| `platforms` | `platforms` | The Linux container matrix (`packaging/platforms.json`, `packaging/linux.sh`) under rootless Podman or Docker. |
| `package` | `distribution_package` | RPM/DEB deployment bundles from a resolved wheelhouse. |
| `homebrew` | `homebrew_package` | Hash-verified Homebrew wheel bundles and tap formulae. |
| `smoke` | `installed_smoke` | Smoke-tests what a user installed: every console script, the report schema, real copies. |
| `dist` | `check_dist` | Release-artifact checks: wheel and sdist contents, and every public module importing from a clean install. |
| `release` | `release` | Release metadata: a checkout's one version source, changelog and project metadata (`check`), and the installed wheel in a publish job (`installed`). |
| `sync` | `sync` | Writes the standard (`src/xrddevtools/standard/`) into every repository's managed blocks of `pyproject.toml` and `CONTRIBUTING.md`; `--check` only reports. |

`xrddevtools.pytest_plugin` turns the scorecard, maintainability and
release-metadata gates into pytest tests (`scorecard_gate(__file__)`,
`maintainability_gate(__file__)`, `release_gate(__file__)`), so each
repository's `tests/test_scorecard.py` and `tests/test_release_metadata.py`
are a few lines.

The standard itself lives here, once, as data: `src/xrddevtools/standard/`
holds its values and ban groups (`standard.toml`), what may differ per
repository (`repositories.toml`) and the text of each block (`templates/`).
`xrddevtools sync` writes it into the marked blocks of every repository's
`pyproject.toml` and `CONTRIBUTING.md`, and `xrddevtools conformance` fails
a block edited by hand: CONTRIBUTING.md, *Changing the standard*, has the
workflow.
`packaging/nix` is the chain's Nix expression (`nix-build packaging/nix -A
xgfalclient`, `-A xrdarc`, `-A nixosTest`).

The GitHub side is here too (`.github/`): reusable workflows every
repository's CI calls - `static.yml` (lint, types, complexity, scorecard,
conformance), `tests.yml` (the pytest matrix with the coverage gate),
`chain.yml` (duplicates and conformance across all seven checkouts),
`package.yml` (the libraries' release-artifact job), `platforms.yml` (the
distribution matrix) - the `candidates` composite action that checks out
the chain's candidates side by side, and the `publish` composite action
holding every step of a package's release build but the PyPI upload (which
trusted publishing keeps in the caller's job).

## How a repository gets it

The chain's packages go to PyPI, and PyPI refuses a direct-URL requirement,
so no repository can declare a dependency on this one (which is never
uploaded: its `Private :: Do Not Upload` classifier makes PyPI refuse it).
The tools are used from a checkout instead:

- **A developer** has the workspace layout - `xrd-devtools/` beside
  `xrdcore/`, `xrdclient/` and the rest. Every repository's
  `[tool.pytest.ini_options] pythonpath` lists `../xrd-devtools/src`, so its
  gates find this checkout without installing anything. For the command,
  `pip install -e ../xrd-devtools` once into the workspace environment.
- **CI** checks it out as `.dependencies/xrd-devtools` (the candidates
  action always does), which `pythonpath` also lists, and installs it.
- **Anywhere else**, install a checkout (`pip install -e`) or put its `src`
  on `PYTHONPATH`.

Where none of these holds a checkout - a wheel-only test environment, the Nix
sandbox - the gates skip with that environmental reason. CI makes them
required: it loads the plugin with `pytest -p xrddevtools.pytest_plugin`
(which fails at start-up if it cannot be imported) and sets
`XRD_DEVTOOLS_REQUIRED=1` (which turns a missing radon or complexipy into a
failure).

## Development

```console
$ python -m pip install -e '.[dev]'
$ python -m pytest -q -p no:cacheprovider -n auto --cov    # 100 % line and branch
$ ruff check src tests && ruff format --check src tests
$ mypy                                                      # strict, src and tests
$ complexipy --quiet                                        # no function above 15
$ xrddevtools conformance --repo .                          # held to its own standard
$ xrddevtools sync --workspace .. --check                   # every managed block is current
$ pytest -p xrddevtools.pytest_plugin tests/test_scorecard.py   # and its own scorecard
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the rules a change must keep, and
[CHANGELOG.md](CHANGELOG.md) for what moved here from where.

## License

LGPL-3.0-or-later, like every package of the chain: see [LICENSE](LICENSE)
and [COPYING](COPYING).
