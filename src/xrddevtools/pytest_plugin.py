"""The family's gates as pytest tests: scorecard, maintainability and release metadata.

Every repository of the chain is held to the same take-over scorecard
(:mod:`xrddevtools.scorecard`: twelve measurements, each better than the
median of 39 widely used libraries), and it used to carry its own copy of the
code that finds, loads and runs it. Now each repository's
``tests/test_scorecard.py`` is a few lines::

    import pytest

    plugin = pytest.importorskip("xrddevtools.pytest_plugin", reason=...)
    test_xrdcore_is_easier_to_take_over_than_the_industry_median = plugin.scorecard_gate(
        __file__
    )

and xrdclient's ``tests/test_maintainability.py`` the same with
:func:`maintainability_gate`. Every repository's
``tests/test_release_metadata.py`` holds :func:`release_gate` (one version
source, a changelog led by that version, metadata that agrees -
:mod:`xrddevtools.release`). The gates read the repository's files
statically (nothing is imported), so they need no installed package - only
this module and, for the first two, the metrics libraries.

**How the plugin is found.** The chain's packages go to PyPI, which refuses
a direct-URL requirement, so no repository can declare a dependency on this
unpublished one. Each repository's ``[tool.pytest.ini_options] pythonpath``
lists a sibling checkout (``../xrd-devtools/src``) and CI's checkout
(``.dependencies/xrd-devtools/src``); anywhere else, install it (``pip
install -e``) or name it in ``PYTHONPATH``. Where none of these holds a
checkout - a wheel-only test environment, a Nix build - the
``importorskip`` above skips the gate with that environmental reason.

**When a skip is not acceptable.** A gate that quietly skips is no gate.
CI loads this module explicitly (``pytest -p xrddevtools.pytest_plugin``),
which fails at start-up if it cannot be imported, and sets
``XRD_DEVTOOLS_REQUIRED=1``, which turns the remaining environmental skip -
radon or complexipy (the ``dev`` extra) not installed - into a failure.
Loaded with ``-p``, the plugin also names the checkout and baseline it uses
in the report header.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NoReturn

import pytest

from xrddevtools._version import __version__

#: Set (to anything) to make a missing metrics library a failure, not a skip.
REQUIRED = "XRD_DEVTOOLS_REQUIRED"
#: What the gates measure with; both come with every repository's ``dev`` extra.
METRICS_LIBRARIES = ("radon", "complexipy")


def pytest_report_header() -> list[str]:
    """One header line: which xrd-devtools version and checkout the gates come from."""
    location = Path(__file__).resolve().parents[1]
    return [f"xrd-devtools {__version__} gates from {location}"]


def repository_root(test_file: str | Path) -> Path:
    """The repository a ``tests/<module>.py`` file belongs to."""
    return Path(test_file).resolve().parents[1]


def _unavailable(reason: str) -> NoReturn:
    """Skip for an environmental ``reason`` - or fail, where :data:`REQUIRED` says to."""
    if os.environ.get(REQUIRED):
        pytest.fail(f"{reason} (and {REQUIRED} is set)")
    pytest.skip(reason)


def require_metrics() -> None:
    """Skip (or fail) unless radon and complexipy can be imported."""
    for name in METRICS_LIBRARIES:
        if importlib.util.find_spec(name) is None:
            _unavailable(f"environmental: the gate measures with {name}, which is not installed")


def scorecard_gate(
    test_file: str | Path, *, paths: Sequence[str] = ("src",), min_index: float = 50.0
) -> Callable[[], None]:
    """A test asserting the repository of ``test_file`` beats the corpus median everywhere.

    Every one of the twelve measurements of ``paths`` (relative to the
    repository root) must be better than the reference corpus's median, and
    the Takeover Ease Index above ``min_index`` (50 is the corpus average by
    construction). The arguments are the repository's thresholds; the
    family's defaults are what every repository uses today.
    """
    root = repository_root(test_file)

    def gate() -> None:
        """Every one of the twelve measurements beats the reference-corpus median."""
        require_metrics()
        from xrddevtools import scorecard

        measured = scorecard.measure([root / path for path in paths])
        result = scorecard.score(measured, scorecard.load_baseline())
        worse = [row["label"] for row in result["rows"] if not row["better_than_median"]]
        assert not worse, f"{root.name} is worse than the corpus median on: {worse}"
        assert result["takeover_ease_index"] > min_index, result["takeover_ease_index"]

    gate.__doc__ = f"{root.name} is easier to take over than the industry median."
    return gate


def maintainability_gate(
    test_file: str | Path, *, config: str = "maintainability.json", timeout: float = 900
) -> Callable[[], None]:
    """A test asserting no function of the repository exceeds the policy in ``config``.

    The policy must also be no looser than the family's standard limits
    (:func:`xrddevtools.maintainability.policy_problems`). Measuring every
    function of a large repository takes minutes on a loaded runner, hence
    the generous ``timeout`` (pytest-timeout's marker).
    """
    root = repository_root(test_file)

    @pytest.mark.timeout(timeout)
    def gate() -> None:
        """No function exceeds any of the five absolute maintainability limits."""
        require_metrics()
        from xrddevtools import maintainability

        policy = maintainability.load_policy(root / config)
        assert not maintainability.policy_problems(policy)
        report = maintainability.analyze(policy)
        failures = maintainability.violations(report, policy.limits)
        details = "\n".join(
            f"{failure.key}: {failure.metric}={failure.actual:g} > {failure.limit:g}"
            for failure in failures
        )
        assert not failures, (
            f"{len(failures)} maintainability limits exceeded; refactor, never loosen:\n"
            f"{details}\nRun `xrddevtools maintainability hotspots` for the ranked view."
        )

    gate.__doc__ = f"No function of {root.name} exceeds a maintainability limit."
    return gate


def release_gate(test_file: str | Path, *, package: str | None = None) -> Callable[[], None]:
    """A test asserting the repository of ``test_file`` is consistent to release.

    :func:`xrddevtools.release.checkout_problems`: the version has one source
    (``src/<package>/_version.py``), the CHANGELOG leads with exactly that
    version's section, the project metadata and README name this
    repository, and a release workflow builds through the shared publish
    action. ``package`` is the import package where it is not the
    distribution's name without hyphens.
    """
    root = repository_root(test_file)

    def gate() -> None:
        """One version source, one changelog section for it, metadata that agrees."""
        from xrddevtools import release

        problems = release.checkout_problems(root, package)
        assert not problems, "release metadata is inconsistent:\n" + "\n".join(problems)

    gate.__doc__ = f"{root.name} has one version, one changelog section and agreeing metadata."
    return gate
