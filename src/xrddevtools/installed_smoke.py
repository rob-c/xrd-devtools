#!/usr/bin/env python3
"""Check installed distributions, every console entry point, and a local copy.

The last step of each platform job (``packaging/linux.sh``, twice - in the
wheel-only virtualenv and through the installed packages' launcher - and the
Homebrew CI job): run against what a user would actually get - a clean wheel-only
install, or the installed RPM/DEB through its isolated launcher - rather than
a source checkout. A failure here means the package is broken even if every
unit test passed, typically a missing data file, a console script whose
target moved, or a dependency the wheelhouse did not carry.

Since 0.4.0 the chain is six distributions: the libraries xrdcore, xrdsec and
xrdhttp, and the clients named by ``--clients`` (xrdclient and xgfalclient by
default; xrdarc when its package was built), each checked with the libraries
it is built on (:data:`LIBRARIES`). A client or library that should be there
but is not installed is a failure, not a skip.

It runs as a plain file (``python installed_smoke.py`` or the launcher's
``--smoke``), never with the source tree on ``sys.path``, so it imports
nothing from :mod:`xrddevtools` either. It prints one JSON summary line for the CI log and
exits non-zero (via an ``AssertionError`` or ``CalledProcessError``
traceback) on the first problem; the traceback is the diagnosis, so the
checks are bare ``assert`` statements.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata as metadata
import json
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from xml.etree import ElementTree

#: The client distributions ``--clients`` may name, and the libraries each is
#: built on (since 0.4.0 xrdarc's A-REX transport is xrdhttp's session, so
#: every client needs all three).
LIBRARIES = {
    "xrdclient": ("xrdcore", "xrdsec", "xrdhttp"),
    "xgfalclient": ("xrdcore", "xrdsec", "xrdhttp"),
    "xrdarc": ("xrdcore", "xrdsec", "xrdhttp"),
}
CLIENTS = tuple(LIBRARIES)
#: Per client: ``(command, argument, text its output must contain)``, where
#: ``{version}`` is the client's own version. These are the invocations a
#: user tries first; ``gfal-ls --version`` reports the gfal2 release
#: xgfalclient emulates, not its own.
VERSION_CHECKS = {
    "xrdclient": [("xrd-fs", "--version", "xrd-fs {version}"), ("xrd-proxy", "--help", "init")],
    "xgfalclient": [("gfal-ls", "--version", "gfal2 2.23.5")],
    "xrdarc": [("xrdarc", "--version", "xrdarc {version}")],
}
#: Each client's local-copy command; xrdarc submits jobs and has none.
COPY_COMMANDS = {"xrdclient": "xrd-cp", "xgfalclient": "gfal-copy"}


def console_checks(bin_dir: Path, name: str) -> int:
    """Run every console script ``name`` declares, from ``bin_dir``; return how many runs.

    The script list comes from the installed distribution's metadata, so a
    command added to ``pyproject.toml`` is checked without editing this file.
    ``--output-format json|xml --help`` is the cheapest invocation that still
    imports the whole command module and goes through the shared report
    writer, whose ``storage-client-report`` schema name both formats carry.
    Every command takes the report flags before a subcommand.
    """
    scripts = [
        e.name for e in metadata.distribution(name).entry_points if e.group == "console_scripts"
    ]
    for script in scripts:
        for output, schema_of in REPORT_SCHEMA.items():
            result = subprocess.run(
                [str(bin_dir / script), "--output-format", output, "--help"],
                capture_output=True,
                check=True,
            )
            assert schema_of(result.stdout) == "storage-client-report", (script, output)
    return len(scripts) * len(REPORT_SCHEMA)


def _json_schema(document: bytes) -> str:
    """The ``schema`` member of a JSON report."""
    schema: str = json.loads(document)["schema"]
    return schema


def _xml_schema(document: bytes) -> str:
    """The ``schema`` attribute of an XML report's root element."""
    return ElementTree.fromstring(document).attrib["schema"]


#: Each ``--output-format`` and how to read the schema name out of its report.
REPORT_SCHEMA = {"json": _json_schema, "xml": _xml_schema}


def version_checks(bin_dir: Path, name: str) -> int:
    """Run ``name``'s :data:`VERSION_CHECKS` from ``bin_dir``; return how many ran."""
    version = metadata.version(name)
    for command, argument, expected in VERSION_CHECKS[name]:
        result = subprocess.run(
            [str(bin_dir / command), argument], capture_output=True, text=True, check=True
        )
        assert expected.format(version=version) in result.stdout + result.stderr, result
    return len(VERSION_CHECKS[name])


def distribution_checks(names: list[str]) -> None:
    """Each distribution's metadata version is its module's ``__version__``.

    A stale ``_version.py`` in a wheel would otherwise go unnoticed until a
    bug report. Also checks the one library behaviour every client relies on
    at import time - the DTD-refusing XML loader - and the pinned gfal2
    release xgfalclient emulates, which changes only deliberately.
    """
    for name in names:
        assert metadata.version(name) == importlib.import_module(name).__version__, name
    xml_checks()
    if "xgfalclient" in names:
        assert importlib.import_module("gfal2").get_version() == "2.23.5"


def xml_checks() -> None:
    """The installed ``xrdcore.safexml`` parses a plain document and refuses a ``DOCTYPE``.

    Every client's XML goes through it, so a broken or bypassed guard in the
    installed runtime would expose all of them to entity expansion at once.
    """
    safexml = importlib.import_module("xrdcore.safexml")
    assert safexml.fromstring(b"<doc/>").tag == "doc"
    try:
        safexml.fromstring(b"<!DOCTYPE doc><doc/>")
    except safexml.UnsafeXML:
        return
    raise AssertionError("xrdcore.safexml accepted a DOCTYPE")


def copy_checks(bins: dict[str, Path], clients: list[str]) -> int:
    """Copy one awkward file through each client's copy command; return how many copies.

    The file has a non-ASCII name and all 256 byte values, so path-encoding
    and binary-safety regressions both show up.
    """
    commands = [(COPY_COMMANDS[c], bins[c]) for c in clients if c in COPY_COMMANDS]
    with tempfile.TemporaryDirectory(prefix="storage-smoke-") as folder:
        source = Path(folder) / "source-physics-μ.dat"
        source.write_bytes(bytes(range(256)) * 4096)
        for command, binary in commands:
            dest = Path(folder) / command
            result = subprocess.run(
                [str(binary / command), "--output-format", "json", str(source), str(dest)],
                capture_output=True,
                check=True,
            )
            assert json.loads(result.stdout)["summary"]["ok"]
            assert source.read_bytes() == dest.read_bytes()
    return len(commands)


def client_bin(value: str) -> tuple[str, Path]:
    """``--client-bin NAME=DIR`` parser."""
    name, sep, directory = value.partition("=")
    if not sep or name not in CLIENTS:
        raise argparse.ArgumentTypeError(f"expected NAME=DIR with NAME one of {CLIENTS}")
    return name, Path(directory)


def main(argv: Sequence[str] | None = None) -> int:
    """Check versions, the XML loader, every command, the version commands and real copies.

    The imports happen after argument parsing so ``--help`` works even on a
    broken install. ``--client-bin`` exists for Homebrew, where each formula
    links its commands from its own keg prefix; everywhere else every
    client's commands are in ``--bin-dir``.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin-dir", type=Path, required=True)
    parser.add_argument("--clients", nargs="+", choices=CLIENTS, default=list(CLIENTS[:2]))
    parser.add_argument(
        "--client-bin", type=client_bin, action="append", default=[], metavar="NAME=DIR"
    )
    args = parser.parse_args(argv)
    bins = dict.fromkeys(args.clients, args.bin_dir) | dict(args.client_bin)
    libraries = dict.fromkeys(lib for client in args.clients for lib in LIBRARIES[client])
    distribution_checks([*libraries, *args.clients])
    commands = sum(console_checks(bins[c], c) + version_checks(bins[c], c) for c in args.clients)
    copies = copy_checks(bins, args.clients)
    summary = {"ok": True, "clients": args.clients, "command_checks": commands, "copies": copies}
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
