"""The RPM/DEB builder end to end, with pip, rpmbuild and dpkg replaced by fakes.

What runs for real is everything this tool decides: the package plan, the
staged layout, the wrappers and the package metadata; only the external
programs are recorded instead of executed.
"""

from __future__ import annotations

import argparse
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from xrddevtools import distribution_package as packages

from tests._wheels import chain_wheelhouse

#: What the fake pip "installs" into every prefix: each client's command and a
#: third-party script that must be pruned.
SCRIPTS = ("xrd-cp", "gfal-copy", "xrdarc", "jp.py")


class FakeTools:
    """Stand-ins for the build interpreter's pip, rpmbuild, dpkg and the target python."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, argv: Sequence[str], **_: Any) -> None:
        """Record ``argv``; lay out what pip or rpmbuild would have produced."""
        args = [str(arg) for arg in argv]
        self.calls.append(args)
        if "pip" in args:
            binary = Path(args[args.index("--prefix") + 1]) / "bin"
            binary.mkdir(parents=True)
            for script in SCRIPTS:
                (binary / script).write_text("#!/build/env/bin/python\n")
        elif args[0] == "rpmbuild":
            top = Path(args[args.index("--define") + 1].removeprefix("_topdir "))
            (top / "RPMS" / "x86_64").mkdir(parents=True)
            name = Path(args[-1]).read_text().split("Name: ")[1].split("\n")[0]
            (top / "RPMS" / "x86_64" / f"{name}.rpm").write_text(name)

    def check_output(self, argv: Sequence[str], **_: Any) -> str:
        """The target interpreter's minor version, or dpkg's architecture."""
        return "amd64\n" if argv[0] == "dpkg" else "3.12\n"


@pytest.fixture
def tools(monkeypatch):
    fake = FakeTools()
    monkeypatch.setattr(subprocess, "run", fake.run)
    monkeypatch.setattr(subprocess, "check_output", fake.check_output)
    return fake


def _main(tmp_path: Path, family: str, *extra: str) -> Path:
    output = tmp_path / "packages"
    argv = [
        "--family",
        family,
        "--wheelhouse",
        str(chain_wheelhouse(tmp_path / "wheelhouse")),
        "--output",
        str(output),
        "--python",
        "/usr/bin/python3",
        *extra,
    ]
    assert packages.main(argv) == 0
    return output


def test_rpm_builds_the_runtime_then_each_client_and_collects_the_rpms(tmp_path, tools):
    output = _main(tmp_path, "rpm", "--release", "3")
    assert sorted(path.name for path in output.iterdir()) == [
        f"{packages.RUNTIME}.rpm",
        "xgfalclient.rpm",
        "xrdarc.rpm",
        "xrdclient.rpm",
    ]
    builds = [call for call in tools.calls if call[0] == "rpmbuild"]
    assert len(builds) == 4
    installs = [call for call in tools.calls if "pip" in call]
    # The runtime installs every shared wheel; each client only its own.
    assert [len([a for a in call if a.endswith(".whl")]) for call in installs] == [10, 1, 1, 1]


def test_deb_names_each_package_for_its_architecture_and_release(tmp_path, tools):
    output = tmp_path / "packages"
    _main(tmp_path, "deb")
    targets = [Path(call[-1]).name for call in tools.calls if call[0] == "dpkg-deb"]
    assert targets == [
        f"{packages.RUNTIME}_0.4.0-1_amd64.deb",
        "xrdclient_0.4.0-1_amd64.deb",
        "xgfalclient_0.4.0-1_amd64.deb",
        "xrdarc_0.4.0-1_amd64.deb",
    ]
    assert all(
        Path(call[-1]).parent == output.resolve() for call in tools.calls if call[0] == "dpkg-deb"
    )


def test_a_package_keeps_only_its_own_commands(tmp_path, tools):
    stage = tmp_path / "stage"
    info = {"name": "xrdclient", "version": "0.4.0", "scripts": ["xrd-cp"]}
    packages.stage_runtime(stage, [], info, Path("/usr/bin/python3"), "3.12")
    binary = stage / "opt/storage-clients/bin"
    assert [path.name for path in binary.iterdir()] == ["xrd-cp"]
    assert "launch.py" in (binary / "xrd-cp").read_text()


def test_an_existing_output_directory_is_refused(tmp_path, tools):
    (tmp_path / "packages").mkdir()
    with pytest.raises(FileExistsError):
        _main(tmp_path, "rpm")


@pytest.mark.parametrize("value", ["0", "-2"])
def test_releases_start_at_one(value):
    with pytest.raises(argparse.ArgumentTypeError, match="positive integer"):
        packages.positive_release(value)
    assert packages.positive_release("7") == 7


def test_a_prefix_without_any_scripts_needs_no_pruning(tmp_path):
    # Library and third-party wheels without console scripts leave no bin/.
    packages.prune_scripts(tmp_path / "bin", [])
    assert not (tmp_path / "bin").exists()
