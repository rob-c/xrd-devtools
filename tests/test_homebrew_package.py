"""The Homebrew command line: what it refuses, and a trial bundle with a local URL."""

from __future__ import annotations

import pytest

from xrddevtools import homebrew_package as brew

from tests._wheels import chain_wheelhouse, write_wheel


def test_a_trial_bundle_points_its_formula_at_the_local_archive(tmp_path):
    wheelhouse = chain_wheelhouse(tmp_path / "wheelhouse")
    output = tmp_path / "out"
    assert brew.main(["--wheelhouse", str(wheelhouse), "--output", str(output)]) == 0
    assert sorted(path.name for path in output.glob("*.rb")) == [
        "xgfalclient.rb",
        "xrdarc.rb",
        "xrdclient.rb",
    ]
    archive = next(output.glob("xrdarc-0.4.0-cp314-*-r0-wheels.tar.gz"))
    assert f'url "{archive.resolve().as_uri()}"' in (output / "xrdarc.rb").read_text()
    # A re-run never mixes two wheelhouses' archives and hashes.
    with pytest.raises(FileExistsError):
        brew.main(["--wheelhouse", str(wheelhouse), "--output", str(output)])


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--base-url", "http://example.test/"], "https:// base URL"),
        (["--revision", "-1"], "--revision must be zero or a positive integer"),
    ],
)
def test_unsafe_urls_and_negative_revisions_are_usage_errors(tmp_path, capsys, extra, message):
    wheelhouse = chain_wheelhouse(tmp_path / "wheelhouse")
    with pytest.raises(SystemExit, match="2"):
        brew.main(["--wheelhouse", str(wheelhouse), "--output", str(tmp_path / "o"), *extra])
    assert message in capsys.readouterr().err
    assert not (tmp_path / "o").exists()


def test_a_wheelhouse_without_a_client_is_a_usage_error(tmp_path, capsys):
    (tmp_path / "wheelhouse").mkdir()
    write_wheel(tmp_path / "wheelhouse", "xrdcore", "0.4.0")
    argv = ["--wheelhouse", str(tmp_path / "wheelhouse"), "--output", str(tmp_path / "o")]
    with pytest.raises(SystemExit, match="2"):
        brew.main(argv)
    assert "holds no client wheel (xrdclient, xgfalclient, xrdarc)" in capsys.readouterr().err
