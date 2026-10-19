"""The container-matrix command line: the CI listing and a run of selected platforms."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from xrddevtools import platforms


def test_list_prints_the_ci_matrix(capsys):
    assert platforms.main(["--list"]) == 0
    assert json.loads(capsys.readouterr().out) == platforms.ci_matrix()


def _workspace(tmp_path: Path) -> Path:
    for name in ("xrdcore", "xrdsec", "xrdhttp", "xrdclient", "xgfalclient"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "pyproject.toml").touch()
    return tmp_path


@pytest.mark.parametrize(("code", "status"), [(0, 0), (3, 1)])
def test_selected_platforms_run_side_by_side_and_any_failure_fails(
    tmp_path, monkeypatch, capsys, code, status
):
    commands: list[list[str]] = []

    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        return subprocess.CompletedProcess(argv, code, stdout="[]")

    monkeypatch.setattr(subprocess, "run", run)
    argv = ["--workspace", str(_workspace(tmp_path)), "--output", str(tmp_path / "out")]
    argv += ["--platform", "alma9", "--platform", "ubuntu24", "--jobs", "2", "--arch", "arm64"]
    assert platforms.main(argv) == status
    containers = [c for c in commands if c[1] == "run"]
    assert sorted(c[c.index("bash") - 1] for c in containers) == sorted(
        t["image"] for t in platforms.matrix() if t["name"] in ("alma9", "ubuntu24")
    )
    assert all("--platform=linux/arm64" in c for c in containers)
    results = sorted((tmp_path / "out").glob("*/result.json"))
    assert [p.parent.name for p in results] == ["alma9", "ubuntu24"]
    assert len(capsys.readouterr().out.splitlines()) == 2


def test_a_wrong_workspace_is_refused_before_any_container_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("a container started"))
    with pytest.raises(SystemExit, match="2"):
        platforms.main(["--workspace", str(tmp_path)])
