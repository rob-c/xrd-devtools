"""The release-artifact checks the libraries' package and publish jobs run."""

from __future__ import annotations

import io
import re
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from xrddevtools import check_dist


def artifacts(
    dist: Path, wheel_members: list[str], sdist_members: list[str], name: str = "pkg"
) -> None:
    """A wheel and an sdist of ``name`` 1.0 in ``dist`` holding the given member names."""
    dist.mkdir(exist_ok=True)
    with zipfile.ZipFile(dist / f"{name}-1.0-py3-none-any.whl", "w") as wheel:
        for member in wheel_members:
            wheel.writestr(member, "")
    with tarfile.open(dist / f"{name}-1.0.tar.gz", "w:gz") as sdist:
        for member in sdist_members:
            info = tarfile.TarInfo(member)
            sdist.addfile(info, io.BytesIO(b""))


GOOD_WHEEL = ["pkg/__init__.py", "pkg/py.typed", "pkg-1.0.dist-info/METADATA"]
GOOD_SDIST = ["pkg-1.0/pyproject.toml", "pkg-1.0/src/pkg/__init__.py"]


def test_clean_artifacts_pass(tmp_path, capsys):
    artifacts(tmp_path / "dist", GOOD_WHEEL, GOOD_SDIST)
    assert check_dist.main(["contents", "pkg", str(tmp_path / "dist")]) == 0
    assert "pkg-1.0-py3-none-any.whl: 3 files; pkg-1.0.tar.gz: 2 members" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("wheel", "sdist", "message"),
    [
        ([*GOOD_WHEEL, "tests/test_x.py"], GOOD_SDIST, "files outside the package: ['tests']"),
        (GOOD_WHEEL[::2], GOOD_SDIST, "has no pkg/py.typed"),
        ([*GOOD_WHEEL, "pkg/x.pyc"], GOOD_SDIST, "carries compiled bytecode"),
        (GOOD_WHEEL, [*GOOD_SDIST, "pkg-1.0/.mypy_cache/x"], "carries build or cache files"),
        (GOOD_WHEEL, [*GOOD_SDIST, "pkg-1.0/src/pkg/x.pyc"], "carries build or cache files"),
        (GOOD_WHEEL, [*GOOD_SDIST, "pkg-1.0/.coverage.host.1"], "carries build or cache files"),
    ],
)
def test_each_stray_member_is_refused(tmp_path, wheel, sdist, message):
    artifacts(tmp_path / "dist", wheel, sdist)
    with pytest.raises(SystemExit, match=re.escape(message)):
        check_dist.contents("pkg", tmp_path / "dist")


def test_a_stale_second_build_is_refused(tmp_path):
    artifacts(tmp_path / "dist", GOOD_WHEEL, GOOD_SDIST)
    (tmp_path / "dist" / "pkg-0.9-py3-none-any.whl").write_bytes(b"")
    with pytest.raises(SystemExit, match="expected one pkg-"):
        check_dist.contents("pkg", tmp_path / "dist")


def installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str = "1.0",
    site_name: str = "site-packages",
) -> Path:
    """A package ``fakepkg`` 1.0 as a wheel installs it: under ``site-packages``, with metadata."""
    site = tmp_path / site_name
    package = site / "fakepkg"
    (package / "sub").mkdir(parents=True)
    (package / "__init__.py").write_text(f'__version__ = "{version}"\n')
    (package / "py.typed").write_text("")
    (package / "public.py").write_text("")
    (package / "_private.py").write_text("raise RuntimeError('a private module is not imported')\n")
    (package / "sub" / "__init__.py").write_text("")
    meta = site / "fakepkg-1.0.dist-info"
    meta.mkdir()
    (meta / "METADATA").write_text("Metadata-Version: 2.1\nName: fakepkg\nVersion: 1.0\n")
    monkeypatch.syspath_prepend(str(site))
    for name in [n for n in sys.modules if n.startswith("fakepkg")]:
        monkeypatch.delitem(sys.modules, name)
    return package


def test_every_public_module_of_the_installed_wheel_imports(tmp_path, monkeypatch, capsys):
    installed(tmp_path, monkeypatch)
    assert check_dist.main(["imports", "fakepkg"]) == 0
    assert capsys.readouterr().out == "fakepkg 1.0: imported 2 public modules\n"


def test_an_installed_package_that_disagrees_with_its_wheel_is_refused(tmp_path, monkeypatch):
    installed(tmp_path, monkeypatch, version="0.9")
    with pytest.raises(SystemExit, match=r"runtime version 0\.9 is not the wheel.s 1\.0"):
        check_dist.imports("fakepkg")


def test_a_wheel_without_its_type_marker_is_refused(tmp_path, monkeypatch):
    (installed(tmp_path, monkeypatch) / "py.typed").unlink()
    with pytest.raises(SystemExit, match=r"py\.typed is not installed"):
        check_dist.imports("fakepkg")


def test_a_package_imported_from_a_source_tree_is_refused(tmp_path, monkeypatch):
    installed(tmp_path, monkeypatch, site_name="src")
    with pytest.raises(SystemExit, match="not the installed wheel"):
        check_dist.imports("fakepkg")


def test_a_malformed_command_line_is_a_usage_error(capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["check_dist.py", "contents"])
    assert check_dist.main() == 2
    assert "usage: check_dist.py" in capsys.readouterr().err
