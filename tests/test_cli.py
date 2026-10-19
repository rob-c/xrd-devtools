"""The ``xrddevtools`` dispatcher: every command reaches its module, and only it."""

from __future__ import annotations

import importlib
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from xrddevtools import cli


def test_every_command_names_a_module_with_a_main():
    for name, (module, summary) in cli.COMMANDS.items():
        assert callable(importlib.import_module(module).main), name
        assert summary and summary in cli.usage()


def test_a_command_gets_the_rest_of_the_command_line_and_its_status_is_returned(monkeypatch):
    calls = []

    def fake(argv: list[str]) -> int:
        calls.append(argv)
        return 7

    monkeypatch.setattr("xrddevtools.dupes.main", fake)
    assert cli.main(["dupes", "--workspace", "w"]) == 7
    assert calls == [["--workspace", "w"]]


def test_help_version_and_usage_errors(capsys):
    assert cli.main(["--help"]) == 0
    assert "commands:" in capsys.readouterr().out
    assert cli.main([]) == 2
    assert "usage: xrddevtools" in capsys.readouterr().err
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.startswith("xrddevtools 0.")
    assert cli.main(["nothing"]) == 2
    assert "unknown command 'nothing'" in capsys.readouterr().err


def test_a_command_does_not_import_the_others():
    # ``dupes`` needs neither radon nor complexipy, so it must not load them.
    probe = (
        "import sys, contextlib; from xrddevtools import cli\n"
        "with contextlib.suppress(SystemExit): cli.main(['dupes', '--help'])\n"
        "assert 'xrddevtools.dupes' in sys.modules\n"
        "assert not {'xrddevtools.scorecard', 'radon', 'complexipy'} & set(sys.modules)\n"
    )
    src = str(Path(cli.__file__).resolve().parents[1])
    done = subprocess.run(
        [sys.executable, "-c", probe], env={**os.environ, "PYTHONPATH": src}, check=False
    )
    assert done.returncode == 0


def test_python_dash_m_runs_the_same_command(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["xrddevtools", "--version"])
    with pytest.raises(SystemExit) as exited:
        runpy.run_module("xrddevtools", run_name="__main__")
    assert exited.value.code == 0
    assert "xrddevtools" in capsys.readouterr().out
