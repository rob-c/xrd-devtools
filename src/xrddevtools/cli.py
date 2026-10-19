"""The ``xrddevtools`` command: one entry point for every chain-wide tool.

``xrddevtools <command> [arguments]`` runs the ``main(argv)`` of the module
:data:`COMMANDS` names for ``<command>``, with the rest of the command line.
Each module is imported only when its command runs, so ``xrddevtools dupes``
needs neither radon nor complexipy, and ``--help`` costs nothing.

Every module also stays runnable on its own (``python -m
xrddevtools.<module>``), and the few that must run in an interpreter where
this package is not installed (see :mod:`xrddevtools`) as plain files.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable, Sequence

from xrddevtools._version import __version__

#: Command name -> (implementing module, one-line summary for ``--help``).
COMMANDS: dict[str, tuple[str, str]] = {
    "scorecard": ("xrddevtools.scorecard", "take-over scorecard: measure, score, baseline"),
    "maintainability": (
        "xrddevtools.maintainability",
        "per-function limits: report, hotspots, check",
    ),
    "dupes": ("xrddevtools.dupes", "fail on a function duplicated across repositories"),
    "conformance": (
        "xrddevtools.conformance",
        "check every repository's tool configuration against the standard",
    ),
    "preflight": ("xrddevtools.chain_preflight", "release pre-flight: workspace, pypi"),
    "platforms": ("xrddevtools.platforms", "run the Linux distribution container matrix"),
    "package": ("xrddevtools.distribution_package", "build RPM/DEB bundles from a wheelhouse"),
    "homebrew": ("xrddevtools.homebrew_package", "build Homebrew wheel bundles and formulae"),
    "smoke": ("xrddevtools.installed_smoke", "smoke-test the installed clients"),
    "dist": ("xrddevtools.check_dist", "check release artifacts: contents, imports"),
    "release": (
        "xrddevtools.release",
        "release metadata: a checkout (check), the installed wheel (installed)",
    ),
    "sync": (
        "xrddevtools.sync",
        "write the standard's managed blocks into pyproject.toml and CONTRIBUTING.md",
    ),
}


def usage() -> str:
    """The top-level help: the synopsis and one line per command."""
    width = max(map(len, COMMANDS))
    lines = [
        "usage: xrddevtools <command> [arguments]   (xrddevtools <command> --help for more)",
        "",
        "commands:",
    ]
    lines += [f"  {name:<{width}}  {summary}" for name, (_, summary) in COMMANDS.items()]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch to the named command; 2 (a usage error) for an unknown or missing one.

    The status is the command's own: 0 for success, 1 for findings, 2 for a
    usage error - the convention every tool here follows.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(usage(), file=sys.stdout if args else sys.stderr)
        return 0 if args else 2
    if args[0] == "--version":
        print(f"xrddevtools {__version__}")
        return 0
    if args[0] not in COMMANDS:
        print(f"xrddevtools: unknown command {args[0]!r}\n\n{usage()}", file=sys.stderr)
        return 2
    module = importlib.import_module(COMMANDS[args[0]][0])
    command: Callable[[Sequence[str]], int] = module.main
    return command(args[1:])
