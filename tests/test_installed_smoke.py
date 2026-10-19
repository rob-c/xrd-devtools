"""The installed-package smoke test, run in-process against fake installed clients.

The real thing runs against an installed wheel or RPM/DEB; here the
distributions' metadata, their modules and their commands are fakes that
behave the way a healthy (or a deliberately broken) install does, so every
check - and the way each one fails - is pinned without a single install.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Sequence
from importlib.metadata import EntryPoint
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from xrddevtools import installed_smoke as smoke

#: Each fake distribution's console scripts.
COMMANDS = {
    "xrdclient": ("xrd-cp", "xrd-fs", "xrd-proxy"),
    "xgfalclient": ("gfal-copy", "gfal-ls"),
    "xrdarc": ("xrdarc",),
}


class UnsafeXML(ValueError):
    """The fake safexml's refusal."""


def _fromstring(document: bytes, refuse: bool = True) -> Any:
    if refuse and b"<!DOCTYPE" in document:
        raise UnsafeXML(document)
    return SimpleNamespace(tag="doc")


class FakeInstall:
    """Fake metadata, modules and commands of an installed chain at 0.4.0."""

    def __init__(self, *, refuses_dtd: bool = True, gfal2: str = "2.23.5") -> None:
        self.refuses_dtd = refuses_dtd
        self.gfal2 = gfal2
        self.runs: list[list[str]] = []

    def version(self, name: str) -> str:
        return "0.4.0"

    def distribution(self, name: str) -> Any:
        points = [EntryPoint(c, "cli:main", "console_scripts") for c in COMMANDS.get(name, ())]
        return SimpleNamespace(entry_points=[*points, EntryPoint("x", "y:z", "fsspec.specs")])

    def import_module(self, name: str) -> Any:
        if name == "xrdcore.safexml":
            refuse = self.refuses_dtd
            return SimpleNamespace(
                fromstring=lambda doc: _fromstring(doc, refuse), UnsafeXML=UnsafeXML
            )
        if name == "gfal2":
            return SimpleNamespace(get_version=lambda: self.gfal2)
        return SimpleNamespace(__version__="0.4.0")

    def run(self, argv: Sequence[str], **_: Any) -> subprocess.CompletedProcess[Any]:
        """A command of the fake install: report, version and copy behaviour."""
        args = [str(arg) for arg in argv]
        self.runs.append(args)
        command = Path(args[0]).name
        if args[1:] == ["--output-format", "json", "--help"]:
            out: Any = json.dumps({"schema": "storage-client-report"}).encode()
        elif args[1:] == ["--output-format", "xml", "--help"]:
            out = b'<report schema="storage-client-report"/>'
        elif args[1] in ("--version", "--help"):
            banner = {"gfal-ls": "gfal2 2.23.5", "xrd-proxy": "usage: init"}
            out = banner.get(command, f"{command} 0.4.0")
        else:
            shutil.copyfile(args[-2], args[-1])
            out = json.dumps({"summary": {"ok": True}}).encode()
        return subprocess.CompletedProcess(
            args, 0, stdout=out, stderr="" if isinstance(out, str) else b""
        )


@pytest.fixture
def install(monkeypatch):
    fake = FakeInstall()
    monkeypatch.setattr(smoke, "metadata", fake)
    monkeypatch.setattr(smoke, "importlib", fake)
    monkeypatch.setattr(subprocess, "run", fake.run)
    return fake


def test_every_command_version_and_copy_of_every_client_is_checked(install, capsys):
    argv = ["--bin-dir", "/opt/bin", "--clients", "xrdclient", "xgfalclient", "xrdarc"]
    argv += ["--client-bin", "xrdarc=/opt/arc/bin"]
    assert smoke.main(argv) == 0
    summary = json.loads(capsys.readouterr().out)
    # Two report formats per command, plus each client's version checks.
    assert summary == {
        "ok": True,
        "clients": ["xrdclient", "xgfalclient", "xrdarc"],
        "command_checks": 2 * 6 + 2 + 1 + 1,
        "copies": 2,
    }
    assert ["/opt/arc/bin/xrdarc", "--version"] in install.runs
    copies = [run for run in install.runs if run[-1].endswith(("xrd-cp", "gfal-copy"))]
    assert [Path(run[0]).name for run in copies] == ["xrd-cp", "gfal-copy"]


def test_the_default_clients_are_the_two_storage_clients(install, capsys):
    assert smoke.main(["--bin-dir", "/opt/bin"]) == 0
    assert json.loads(capsys.readouterr().out)["clients"] == ["xrdclient", "xgfalclient"]


def test_a_loader_that_accepts_a_doctype_fails_the_smoke_test(monkeypatch):
    fake = FakeInstall(refuses_dtd=False)
    monkeypatch.setattr(smoke, "metadata", fake)
    monkeypatch.setattr(smoke, "importlib", fake)
    with pytest.raises(AssertionError, match="accepted a DOCTYPE"):
        smoke.distribution_checks(["xrdcore"])


def test_a_different_gfal2_release_fails_the_smoke_test(monkeypatch):
    fake = FakeInstall(gfal2="2.23.6")
    monkeypatch.setattr(smoke, "metadata", fake)
    monkeypatch.setattr(smoke, "importlib", fake)
    smoke.distribution_checks(["xrdcore"])  # no xgfalclient: gfal2 is not consulted
    with pytest.raises(AssertionError):
        smoke.distribution_checks(["xrdcore", "xgfalclient"])
