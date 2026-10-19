"""xrddevtools: the tooling that serves the whole xrdcore/xrdclient chain.

Nothing here is imported by a package of the chain at run time; everything is
what a maintainer or a CI job runs *over* the chain:

* :mod:`.scorecard` and :mod:`.maintainability` - the take-over scorecard
  (twelve project-level measurements against a corpus of 39 widely used
  libraries) and the per-function maintainability gate;
* :mod:`.pytest_plugin` - the scorecard, maintainability and
  release-metadata gates as pytest tests, so each repository's suite
  carries them in a few lines;
* :mod:`.dupes` - the duplicate gate: the same function in two repositories;
* :mod:`.conformance` - every repository's ruff, mypy, complexipy, coverage
  and pytest configuration against the one standard;
* :mod:`.sync` - writes that standard (``standard/``: its values, what may
  differ per repository, the text of each block) into every repository's
  managed blocks of ``pyproject.toml`` and ``CONTRIBUTING.md``;
* :mod:`.release` - release metadata: one version source, a changelog led
  by it, metadata that agrees (a checkout), and the publish job's check of
  the installed wheel;
* :mod:`.chain_preflight` - the release pre-flight of the six-package chain;
* :mod:`.platforms`, :mod:`.distribution_package`, :mod:`.homebrew_package`,
  :mod:`.installed_smoke` and :mod:`.check_dist` - the Linux container
  matrix, the RPM/DEB and Homebrew bundles, the installed-package smoke test
  and the release-artifact checks;
* :mod:`.workspace` - the names and roles of the chain's repositories;
* :mod:`.cli` - the ``xrddevtools`` command that dispatches to all of them.

Where to start reading: :mod:`.cli` lists every command and the module that
implements it. What a maintainer must not break: :mod:`.installed_smoke`,
:mod:`.distribution_package`, :mod:`.chain_preflight`, :mod:`.check_dist`
and :mod:`.release` also run as plain files, in interpreters where this package is not installed,
so they import nothing from it and only the standard library (plus
``packaging`` for the pre-flight).
"""

from __future__ import annotations

from ._version import __version__

__all__ = ["__version__"]
