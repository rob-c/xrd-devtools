#!/usr/bin/env python3
"""Release metadata: one version, one changelog section, metadata that agrees with both.

Every package of the chain is released the same way (CONTRIBUTING's
*Releasing*): the version lives in ``src/<package>/_version.py`` and nowhere
else, the build backend reads it from there, the CHANGELOG's newest section
is that version, and the project metadata names the repository the package
comes from. Each repository used to pin some of this in its own test (two
near-identical ``tests/test_release_metadata.py``, the libraries' version
asserts) and each publish workflow re-checked the rest in an inline script.
This module is both halves, once:

``check ROOT`` (:func:`checkout_problems`, and so
:func:`xrddevtools.pytest_plugin.release_gate` in every repository's suite)
    A checkout, statically - nothing is imported:

    * **one version source**: ``version`` is dynamic, hatch reads it from
      ``src/<package>/_version.py``, which assigns ``__version__`` one
      ``X.Y.Z`` literal, and no other module of ``src`` assigns a literal
      ``__version__`` of its own;
    * **the changelog**: every ``## [`` heading reads ``## [X.Y.Z] -
      Unreleased`` or ``... - YYYY-MM-DD``, newest first, and the first one
      is this version, exactly once; where the file keeps link references
      (``[X.Y.Z]: https://...``), this version has one, comparing against
      ``v<version>`` in this repository;
    * **the metadata**: ``project.name`` names the repository (a checkout's
      directory may be called anything), the README is ``README.md`` and is
      titled with that name, ``requires-python`` and the
      ``Programming Language`` classifiers name the same Python versions
      (:data:`SUPPORTED_PYTHONS`), the repository, issue and changelog URLs
      are this repository's, and the license files are present;
    * **the release workflow**, where there is one (``publish.yml``, the
      name every repository of the chain uses): it builds through xrd-devtools' shared ``publish``
      action for this package, and a PyPI upload goes through trusted
      publishing;
    * a documentation site (``mkdocs.yml``) is built into ``site/``, which
      ``.gitignore`` keeps out of the sdist.

``installed DIST --tag TAG --changelog FILE`` (:func:`installed_problems`)
    The publish action's check of the wheel it just installed into a clean
    environment: the tag is ``v<version>`` of the installed metadata, the
    imported package reports the same ``__version__``, that version's
    changelog section is dated, and, as asked, the wheel has no default
    dependency (``--no-dependencies``) or a module is *not* installed
    (``--absent``, e.g. xrdarc must not pull in a storage client).

This file runs as a plain script in the publish job's clean environment,
where xrddevtools is not installed, so it imports only the standard library
(``tests/test_package.py`` pins that).
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import importlib.util
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The Python versions every package supports, oldest first: ``requires-python``
#: is ``>=`` the first, and the classifiers name exactly these.
SUPPORTED_PYTHONS = ("3.11", "3.12", "3.13", "3.14")
#: Where every repository of the chain lives.
OWNER_URL = "https://github.com/rob-c"
#: The files a release carries beside the code (LGPL: the license and the GPL it extends).
LICENSE_FILES = ("LICENSE", "COPYING")
#: The release workflow a repository may have, and the shared action it must use.
RELEASE_WORKFLOW = "publish.yml"
PUBLISH_ACTION = "uses: rob-c/xrd-devtools/.github/actions/publish@main"
#: The only way an upload may reach PyPI: trusted publishing, from the caller's job.
PYPI_UPLOAD = "uses: pypa/gh-action-pypi-publish@release/v1"

#: A changelog section heading, as Keep a Changelog writes it.
_HEADING = re.compile(
    r"^## \[(?P<version>\d+\.\d+\.\d+)\] - (?P<date>Unreleased|\d{4}-\d{2}-\d{2})$"
)
#: A released version: three numbers, no pre-release or development suffix.
_RELEASE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
#: ``__version__ = "..."`` as a statement of its own, at any indentation.
_LITERAL_VERSION = re.compile(r"""^\s*__version__\s*(?::\s*str\s*)?=\s*["']([^"']*)["']""", re.M)


@dataclass(frozen=True, slots=True)
class Project:
    """A checkout's ``pyproject.toml``, with the names everything else is checked against.

    ``name`` is the distribution (``project.name``, which is also the
    repository's name); ``package`` the import package whose
    ``_version.py`` holds the version - the name without its hyphens
    (``xrd-devtools`` ships ``xrddevtools``).
    """

    root: Path
    pyproject: Mapping[str, Any]
    name: str
    package: str

    @property
    def table(self) -> Mapping[str, Any]:
        """The ``[project]`` table."""
        table: Mapping[str, Any] = self.pyproject.get("project", {})
        return table

    @property
    def repository(self) -> str:
        """The repository's URL: every repository of the chain is under :data:`OWNER_URL`."""
        return f"{OWNER_URL}/{self.name}"


def load(root: Path, package: str | None = None) -> Project:
    """Read ``root/pyproject.toml``; ``package`` overrides the import package's name."""
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    name = str(pyproject.get("project", {}).get("name", root.name))
    return Project(root, pyproject, name, package or name.replace("-", ""))


def version_problems(project: Project) -> tuple[str | None, list[str]]:
    """The version ``_version.py`` holds (``None`` if unreadable), and every problem found.

    A second source is what makes a wheel, a runtime ``__version__`` and a
    changelog disagree, so the version must be dynamic, read by hatch from
    the one file, and assigned nowhere else in ``src``.
    """
    expected = f"src/{project.package}/_version.py"
    hatch = project.pyproject.get("tool", {}).get("hatch", {}).get("version", {})
    problems = failed(
        (
            "version" in project.table.get("dynamic", []) and "version" not in project.table,
            'pyproject.toml: the version must be dynamic = ["version"], not static',
        ),
        (
            hatch.get("path") == expected,
            f"pyproject.toml: [tool.hatch.version] path must be {expected!r}",
        ),
    )
    source = project.root / expected
    found = _LITERAL_VERSION.findall(_text(source))
    if len(found) != 1:
        return None, [*problems, f"{expected}: must assign __version__ one string literal"]
    problems += failed(
        (bool(_RELEASE.match(found[0])), f"{expected}: {found[0]!r} is not a release version X.Y.Z")
    )
    return found[0], problems + _second_sources(project, source)


def failed(*checks: tuple[bool, str]) -> list[str]:
    """The message of every ``(holds, message)`` check that does not hold, in order."""
    return [message for holds, message in checks if not holds]


def _text(path: Path) -> str:
    """A file's text, or ``""`` for one that is not there (its absence is reported elsewhere)."""
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _second_sources(project: Project, source: Path) -> list[str]:
    """A problem for each other module of ``src`` that assigns a literal ``__version__``."""
    others = [
        path
        for path in sorted((project.root / "src").rglob("*.py"))
        if path != source and _LITERAL_VERSION.search(path.read_text(encoding="utf-8"))
    ]
    return [
        f"{path.relative_to(project.root).as_posix()}: a second version source "
        f"(import __version__ from {project.package}._version instead)"
        for path in others
    ]


def changelog_problems(text: str, version: str, repository: str) -> list[str]:
    """Why the CHANGELOG ``text`` does not lead with exactly one section for ``version``.

    Every section heading must parse, newest first, so the publish job and
    the GitHub release (which cut the notes out by heading) find the right
    one; ``repository`` is the URL a link reference must compare in.
    """
    lines = text.splitlines()
    headings = [line for line in lines if line.startswith("## [")]
    if not headings:
        return [f"CHANGELOG.md: no section heading; the first must be '## [{version}] - ...'"]
    problems = [
        f"CHANGELOG.md: {line!r} is not '## [X.Y.Z] - Unreleased' or '... - YYYY-MM-DD'"
        for line in headings
        if not _HEADING.match(line)
    ]
    parsed = [match.group("version") for line in headings if (match := _HEADING.match(line))]
    return (
        problems
        + _order_problems(parsed)
        + _first_section_problems(parsed, version)
        + _link_problems(lines, version, repository)
    )


def _order_problems(versions: Sequence[str]) -> list[str]:
    """The sections must be distinct releases, newest first (the order readers expect).

    ``versions`` are the well-formed headings' (``X.Y.Z``, so they compare as
    numbers); a malformed one was reported already.
    """
    keys = [tuple(int(part) for part in entry.split(".")) for entry in versions]
    if keys != sorted(set(keys), reverse=True):
        return [f"CHANGELOG.md: sections are not distinct releases, newest first: {versions}"]
    return []


def _first_section_problems(versions: Sequence[str], version: str) -> list[str]:
    """The newest section is the version being built: its notes are what is released."""
    if versions[:1] != [version]:
        return [f"CHANGELOG.md: the first section must be {version}, not {versions[:1]}"]
    return []


def _link_problems(lines: Sequence[str], version: str, repository: str) -> list[str]:
    """Where the changelog keeps link references, ``version`` needs one into ``repository``."""
    links = dict(line[1:].split("]: ", 1) for line in lines if re.match(r"^\[[^\]]+\]: \S", line))
    if not links:
        return []
    target = links.get(version, "")
    if not (target.startswith(f"{repository}/compare/") and target.endswith(f"...v{version}")):
        return [f"CHANGELOG.md: [{version}] must link {repository}/compare/<previous>...v{version}"]
    return []


def metadata_problems(project: Project) -> list[str]:
    """Departures of the project metadata from the repository it is released from."""
    table = project.table
    title = _text(project.root / "README.md").partition("\n")[0]
    problems = failed(
        (table.get("readme") == "README.md", 'pyproject.toml: readme must be "README.md"'),
        (title == f"# {project.name}", f"README.md: the first line must be '# {project.name}'"),
        *(
            ((project.root / name).is_file(), f"{name}: missing (every package ships its license)")
            for name in LICENSE_FILES
        ),
    )
    return (
        problems
        + _python_problems(table)
        + _url_problems(table.get("urls", {}), project.repository)
    )


def _python_problems(table: Mapping[str, Any]) -> list[str]:
    """``requires-python`` and the classifiers must name :data:`SUPPORTED_PYTHONS`, no more."""
    prefix = "Programming Language :: Python :: "
    named = sorted(
        entry.removeprefix(prefix)
        for entry in table.get("classifiers", [])
        if entry.startswith(f"{prefix}3.")
    )
    return failed(
        (
            table.get("requires-python") == f">={SUPPORTED_PYTHONS[0]}",
            f'pyproject.toml: requires-python must be ">={SUPPORTED_PYTHONS[0]}"',
        ),
        (
            named == sorted(SUPPORTED_PYTHONS),
            f"pyproject.toml: Python classifiers {named}, not {list(SUPPORTED_PYTHONS)}",
        ),
    )


def _url_problems(urls: Mapping[str, str], repository: str) -> list[str]:
    """The project URLs a user follows from PyPI must lead to this repository."""
    expected = {
        "Repository": repository,
        "Issues": f"{repository}/issues",
        "Changelog": f"{repository}/blob/main/CHANGELOG.md",
    }
    return [
        f"pyproject.toml: [project.urls] {key} must be {value!r}"
        for key, value in expected.items()
        if urls.get(key) != value
    ]


def workflow_problems(project: Project) -> list[str]:
    """The release workflow, if any, must build through the shared publish action."""
    workflow = project.root / ".github" / "workflows" / RELEASE_WORKFLOW
    if not workflow.is_file():
        return []
    text = workflow.read_text(encoding="utf-8")
    return [
        f".github/workflows/{RELEASE_WORKFLOW}: {problem}"
        for problem in _workflow_text_problems(text, project.name)
    ]


def _workflow_text_problems(text: str, name: str) -> list[str]:
    """What a release workflow's ``text`` must say: the shared build, trusted publishing."""
    # A commented-out upload job is not an upload.
    steps = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    return failed(
        (
            PUBLISH_ACTION in text and f"package: {name}\n" in text,
            f"must build with {PUBLISH_ACTION} (package: {name})",
        ),
        (
            PYPI_UPLOAD not in steps or "id-token: write" in steps,
            "the PyPI upload must use trusted publishing (id-token: write)",
        ),
    )


def site_problems(root: Path) -> list[str]:
    """A built documentation site must never reach an sdist: ``site/`` is ignored."""
    if not (root / "mkdocs.yml").is_file():
        return []
    ignore = root / ".gitignore"
    lines = ignore.read_text(encoding="utf-8").splitlines() if ignore.is_file() else []
    return [] if "site/" in lines else [".gitignore: must ignore site/ (mkdocs builds into it)"]


def checkout_problems(root: Path, package: str | None = None) -> list[str]:
    """Every release-metadata problem of the checkout at ``root`` (see the module docstring)."""
    project = load(root, package)
    version, problems = version_problems(project)
    if version is not None:
        problems += changelog_problems(_text(root / "CHANGELOG.md"), version, project.repository)
    return problems + metadata_problems(project) + workflow_problems(project) + site_problems(root)


@dataclass(frozen=True, slots=True)
class Installed:
    """What the publish job asks of the wheel it installed (the ``installed`` command line)."""

    distribution: str
    tag: str
    changelog: str
    package: str
    no_dependencies: bool = False
    absent: tuple[str, ...] = ()


def installed_problems(wheel: Installed) -> list[str]:
    """Why the installed ``wheel.distribution`` must not be released under ``wheel.tag``.

    Run by the clean environment's interpreter, outside the source tree, so
    the import is the wheel's.
    """
    version = importlib.metadata.version(wheel.distribution)
    return (
        _identity_problems(wheel, version)
        + _dated_problems(wheel.changelog, version)
        + _dependency_problems(wheel)
    )


def _identity_problems(wheel: Installed, version: str) -> list[str]:
    """The tag, the wheel metadata and the imported package name one version."""
    runtime = getattr(importlib.import_module(wheel.package), "__version__", None)
    return failed(
        (wheel.tag == f"v{version}", f"the tag must be v{version}"),
        (
            runtime == version,
            f"{wheel.package}.__version__ is {runtime!r}, the wheel's is {version}",
        ),
    )


def _dated_problems(changelog: str, version: str) -> list[str]:
    """A release's changelog section is dated: ``Unreleased`` means it was tagged too early."""
    headings = [line for line in changelog.splitlines() if line.startswith(f"## [{version}]")]
    if not headings:
        return [f"CHANGELOG.md has no {version} section"]
    if headings[0].endswith("Unreleased"):
        return [f"CHANGELOG.md: date the {version} section before tagging"]
    return []


def _dependency_problems(wheel: Installed) -> list[str]:
    """No default dependency where none is promised; no module installed that must not be."""
    required = importlib.metadata.requires(wheel.distribution) or []
    return failed(
        *(
            (not wheel.no_dependencies or "extra ==" in line, f"a default dependency: {line}")
            for line in required
        ),
        *(
            (
                importlib.util.find_spec(name) is None,
                f"{name} is installed: {wheel.distribution} must not need it",
            )
            for name in wheel.absent
        ),
    )


def parser() -> argparse.ArgumentParser:
    """``check ROOT`` or ``installed DIST --tag TAG --changelog FILE [...]``."""
    top = argparse.ArgumentParser(
        prog="xrddevtools release", description=(__doc__ or "").split("\n\n")[0]
    )
    commands = top.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="a checkout's release metadata")
    check.add_argument("root", type=Path)
    check.add_argument("--package", help="the import package, if not the name without hyphens")
    installed = commands.add_parser("installed", help="the wheel installed in this interpreter")
    installed.add_argument("distribution")
    installed.add_argument("--tag", required=True, help="the release tag, v<version>")
    installed.add_argument("--changelog", type=Path, required=True)
    installed.add_argument("--package", help="the import package, if not the distribution")
    installed.add_argument("--no-dependencies", action="store_true")
    installed.add_argument("--absent", action="append", default=[], help="must not be installed")
    return top


def main(argv: Sequence[str] | None = None) -> int:
    """Run one check, print each problem on its own line, and return 1 if there were any."""
    options = parser().parse_args(argv)
    if options.command == "check":
        problems = checkout_problems(options.root.resolve(), options.package)
        subject = str(options.root)
    else:
        problems = installed_problems(
            Installed(
                options.distribution,
                options.tag,
                options.changelog.read_text(encoding="utf-8"),
                options.package or options.distribution.replace("-", ""),
                options.no_dependencies,
                tuple(options.absent),
            )
        )
        subject = options.distribution
    print("\n".join(problems) or f"release: {subject} is consistent")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
