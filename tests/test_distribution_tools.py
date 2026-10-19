"""Distribution definitions and shared packaging contracts stay coordinated.

Moved from xrdclient with the tools: the container matrix and ``linux.sh``
(:mod:`xrddevtools.platforms`), the RPM/DEB builder
(:mod:`xrddevtools.distribution_package`), the Homebrew bundles
(:mod:`xrddevtools.homebrew_package`), the installed-package smoke test and
the release pre-flight (:mod:`xrddevtools.chain_preflight`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType

import pytest

from xrddevtools import chain_preflight, platforms
from xrddevtools import distribution_package as packages
from xrddevtools import homebrew_package as brew
from xrddevtools import installed_smoke as smoke

from tests._wheels import chain_wheelhouse, write_wheel


@pytest.fixture
def wheel(tmp_path):
    def make(name="xrdclient", version="0.2.0"):
        return write_wheel(tmp_path, name, version, ["xrd-cp"])

    return make


@pytest.fixture
def wheelhouse(tmp_path):
    """The whole chain's resolved wheelhouse, xrdarc included; returns the directory."""
    return chain_wheelhouse(tmp_path / "wheelhouse")


def test_matrix_covers_every_requested_container_and_native_platform():
    config = json.loads(platforms.MATRIX.read_text())
    assert {t["name"] for t in config["containers"]} == {
        "alma8",
        "alma9",
        "alma10",
        "ubuntu24",
        "ubuntu26",
        "centos9-stream",
        "centos10-stream",
        "fedora",
        "fedora-rawhide",
    }
    assert config["native"] == ["nixos-26.05", "homebrew-macos-intel", "homebrew-macos-arm64"]
    assert config["sources"] == {
        "required": ["xrdcore", "xrdsec", "xrdhttp", "xrdclient", "xgfalclient"],
        "optional": ["xrdarc"],
    }
    assert len({t["name"] for t in config["containers"]}) == len(config["containers"])
    for target in config["containers"]:
        assert target["python"] == (
            "python3.12" if target["name"] in {"alma8", "alma9", "centos9-stream"} else "python3"
        )
        assert "/" in target["image"] and ":" in target["image"]


@pytest.mark.parametrize("engine", ["podman", "docker"])
@pytest.mark.parametrize("with_arc", [False, True])
def test_container_uses_readonly_sources_and_no_host_privileges(tmp_path, engine, with_arc):
    if with_arc:
        (tmp_path / "xrdarc").mkdir()
        (tmp_path / "xrdarc/pyproject.toml").touch()
    argv = platforms.command(engine, tmp_path, tmp_path / "output", platforms.matrix()[0])
    assert argv[:3] == [engine, "run", "--rm"]
    mounts = [argv[i + 1] for i, flag in enumerate(argv) if flag == "-v"]
    names = ["xrdcore", "xrdsec", "xrdhttp", "xrdclient", "xgfalclient"] + ["xrdarc"] * with_arc
    expected = [f"{tmp_path / name}:/src/{name}:ro" for name in names]
    tooling = f"{platforms.CHECKOUT}:/src/xrd-devtools:ro"
    assert mounts == [*expected, tooling, f"{(tmp_path / 'output').resolve()}:/artifacts:rw"]
    assert argv[argv.index("bash") + 1] == "/src/xrd-devtools/packaging/linux.sh"
    assert not {"--privileged", "--network=host"} & set(argv)
    assert {"--security-opt=no-new-privileges", "--pull=always"} <= set(argv)
    assert not [a for a in argv if a.startswith("--platform")]


def test_ci_matrix_runs_every_container_on_both_architectures():
    include = platforms.ci_matrix()["include"]
    assert len(include) == 2 * len(platforms.matrix())
    assert {(e["name"], e["arch"]) for e in include} == {
        (t["name"], arch) for t in platforms.matrix() for arch in platforms.ARCHITECTURES
    }
    for entry in include:
        assert entry["runner"] == platforms.RUNNERS[entry["arch"]]
        assert ("arm" in entry["runner"]) is (entry["arch"] == "arm64")


@pytest.mark.parametrize("arch", platforms.ARCHITECTURES)
def test_an_explicit_architecture_selects_that_image_variant(tmp_path, arch):
    argv = platforms.command("docker", tmp_path, tmp_path / "out", platforms.matrix()[0], arch)
    assert f"--platform=linux/{arch}" in argv
    assert argv.index(f"--platform=linux/{arch}") < argv.index(platforms.matrix()[0]["image"])


def test_workspace_error_is_actionable(tmp_path):
    parser = argparse.ArgumentParser()
    with pytest.raises(SystemExit, match="2"):
        platforms.validate_workspace(parser, tmp_path)
    for name in ("xrdcore", "xrdsec", "xrdhttp", "xrdclient"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "pyproject.toml").touch()
    with pytest.raises(SystemExit, match="2"):  # xgfalclient is required; xrdarc is not
        platforms.validate_workspace(parser, tmp_path)
    (tmp_path / "xgfalclient").mkdir()
    (tmp_path / "xgfalclient/pyproject.toml").touch()
    platforms.validate_workspace(parser, tmp_path)


def test_nonroot_suites_do_not_take_ownership_of_the_runner_artifact_directory():
    script = (platforms.MATRIX.parent / "linux.sh").read_text()
    assert "mkdir /artifacts/tests" in script
    assert "chown -R tester /work /artifacts/tests\n" in script
    assert "xml:/artifacts/tests/$project-coverage.xml" in script
    assert 'junitxml="/artifacts/tests/$project-tests.xml"' in script


def test_linux_script_builds_the_whole_chain_and_removes_the_runtime_last():
    script = (platforms.MATRIX.parent / "linux.sh").read_text()
    assert "for project in xrdcore xrdsec xrdhttp xrdclient xgfalclient xrdarc; do" in script
    assert (
        "--only-binary=:all: --find-links /artifacts/wheels --dest /artifacts/wheelhouse" in script
    )
    assert script.count('--clients "${clients[@]}"') == 2  # venv and installed packages
    assert "removal+=(storage-clients-libs)" in script
    assert "test ! -e /opt/storage-clients\n" in script


def test_linux_script_runs_the_tools_from_the_mounted_devtools_checkout():
    script = (platforms.MATRIX.parent / "linux.sh").read_text()
    assert "test -d /src/xrd-devtools\n" in script
    # Beside the projects, so each suite's gates find ../xrd-devtools/src.
    assert 'for project in "${projects[@]}" xrd-devtools; do' in script
    assert "tools=/work/xrd-devtools/src/xrddevtools\n" in script
    assert script.count('"$tools/installed_smoke.py"') == 2
    assert '"$buildpy" "$tools/distribution_package.py"' in script
    assert "--ignore" not in script  # the gates skip themselves, environmentally


@pytest.mark.parametrize("code", [0, 1, 125])
def test_runner_retains_failure_logs_and_image_identity(tmp_path, monkeypatch, code):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            a[0], code, stdout='[{"Digest":"sha256:test"}]'
        ),
    )
    report = platforms.run("podman", tmp_path, tmp_path / "result", platforms.matrix()[0])
    assert report["ok"] is (code == 0)
    assert report["exit_code"] == code
    assert report["arch"] == "native"
    assert json.loads((tmp_path / "result/result.json").read_text()) == report
    assert "sha256:test" in (tmp_path / "result/image.json").read_text()
    assert (tmp_path / "result/container.log").is_file()


def test_library_wheel_without_commands_has_no_scripts(tmp_path):
    path = write_wheel(tmp_path, "xrdcore", "0.4.0")
    assert packages.wheel_info(path) == {"name": "xrdcore", "version": "0.4.0", "scripts": []}


def test_wheel_metadata_and_manifest_are_not_guessed(wheel):
    path = wheel()
    assert packages.wheel_info(path) == {
        "name": "xrdclient",
        "version": "0.2.0",
        "scripts": ["xrd-cp"],
    }
    assert packages.manifest([path]) == [
        {"filename": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    ]


def test_deployment_bundle_rejects_ambiguous_version(wheel):
    with pytest.raises(ValueError, match=r"major\.minor\.patch"):
        packages.wheel_info(wheel(version="0.2.0rc1"))


#: Each Homebrew bundle's wheels: its client's dependency closure, nothing more.
BUNDLES = {
    "xrdclient": {
        *("xrdclient", "xrdcore", "xrdsec", "cryptography", "cffi", "google_crc32c"),
        # xrdhttp's ``s3`` extra, which xrdclient asks for: botocore and its own.
        *("xrdhttp", "botocore", "python_dateutil", "urllib3"),
    },
    # xrdarc takes plain xrdhttp: no botocore, and no urllib3 since 0.4.0.
    "xrdarc": {"xrdarc", "xrdsec", "xrdcore", "xrdhttp", "cryptography", "cffi"},
}
BUNDLES["xgfalclient"] = BUNDLES["xrdclient"] | {"xgfalclient", "bcrypt"}


@pytest.mark.parametrize("name", ["xrdclient", "xgfalclient", "xrdarc"])
@pytest.mark.parametrize("revision", [0, 2])
def test_brew_bundle_has_real_hash_and_only_its_runtime(wheelhouse, tmp_path, name, revision):
    output = tmp_path / "output"
    output.mkdir()
    release = brew.Release("3.14", "https://example.test/releases", revision)
    brew.bundle(wheelhouse, output, name, release)
    info = (output / f"{name}.rb").read_text()
    archive = next(output.glob(f"{name}-0.4.0-cp314-*-r{revision}-wheels.tar.gz"))
    assert hashlib.sha256(archive.read_bytes()).hexdigest() in info
    assert 'ENV["PIP_NO_INDEX"] = "1"' in info
    assert "system_site_packages: false" in info
    assert f"revision {revision}\n" in info
    assert f"https://example.test/releases/{archive.name}" in info
    assert ('(testpath/"dest").read' in info) is (name != "xrdarc")
    manifest = json.loads((output / f"{name}-manifest.json").read_text())
    assert {entry["filename"].split("-")[0] for entry in manifest} == BUNDLES[name]


def test_closure_follows_requested_extras_and_skips_absent_marker_dependencies(wheelhouse):
    wheels = sorted(wheelhouse.glob("*.whl"))
    names = {packages.dist_name(w) for w in packages.closure(wheels, "xrdhttp")}
    # xrdhttp alone: its ``s3`` extra (botocore) was not asked for.
    assert names == {"xrdhttp", "xrdcore"}
    names = {packages.dist_name(w) for w in packages.closure(wheels, "xrdclient")}
    # xrdclient asks for ``xrdhttp[s3]``; botocore's ``crt`` extra was not asked
    # for, and python-dateutil is found by its canonical name.
    assert {"xrdhttp", "botocore", "python-dateutil", "urllib3"} <= names
    assert "awscrt" not in names
    assert packages.closure(wheels, "not-in-the-wheelhouse") == []


def test_unreadable_requirement_is_an_error():
    with pytest.raises(ValueError, match="unreadable"):
        packages.requirement("[broken]")
    assert packages.requirement("A_B[x, y]>=1; extra == 'z'") == ("a-b", frozenset("xy"), "z")


def test_installed_commands_never_write_into_the_packaged_runtime(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **k: calls.append(argv))
    stage = tmp_path / "stage"
    (stage / "opt/storage-clients/bin").mkdir(parents=True)  # what pip would have created
    (stage / "opt/storage-clients/bin/cffi-gen-src").touch()  # a dependency's script
    info = {"name": "xrdclient", "version": "0.2.0", "scripts": ["xrd-cp"]}
    packages.stage_runtime(stage, [], info, Path("/usr/bin/python3.12"), "3.12")
    assert "--compile" in calls[0] and "--no-compile" not in calls[0]
    wrapper = (stage / "opt/storage-clients/bin/xrd-cp").read_text()
    assert "exec /usr/bin/python3.12 -I -S -B /opt/storage-clients/launch.py" in wrapper
    assert (stage / "usr/bin/xrd-cp").readlink() == Path("/opt/storage-clients/bin/xrd-cp")
    assert not (stage / "opt/storage-clients/launch.py").exists()  # the runtime's, not a client's
    assert [p.name for p in (stage / "opt/storage-clients/bin").iterdir()] == ["xrd-cp"]


def test_only_the_runtime_package_ships_the_launcher_and_it_has_no_commands(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda argv, **k: None)
    stage = tmp_path / "stage"
    (stage / "opt/storage-clients/share/man/man1").mkdir(parents=True)
    (stage / "opt/storage-clients/share/man/man1/tool.1").touch()
    # Third-party console scripts, with the build environment's shebangs.
    (stage / "opt/storage-clients/bin/__pycache__").mkdir(parents=True)
    (stage / "opt/storage-clients/bin/jp.py").write_text("#!/build/env/bin/python\n")
    info = {"name": packages.RUNTIME, "version": "0.4.0", "scripts": []}
    packages.stage_runtime(stage, [], info, Path("/usr/bin/python3"), "3.12")
    assert "python3.12" in (stage / "opt/storage-clients/launch.py").read_text()
    assert not (stage / "usr/bin").exists()
    assert not (stage / "opt/storage-clients/bin").exists()
    assert (stage / "usr/share/man/man1/tool.1").is_symlink()
    assert (stage / f"usr/share/doc/{packages.RUNTIME}/wheel-manifest.json").is_file()


def test_private_launcher_preserves_redhat_and_debian_library_locations():
    text = packages.LAUNCHER.format(minor="3.12")
    assert '("lib", "lib64")' in text
    assert '"python3.12"' in text
    # Any client's commands, xrdarc's included, without naming the clients.
    assert 'entry_points(group="console_scripts", name=name)' in text


def test_package_plan_gives_the_runtime_every_shared_wheel_and_pins_exact_releases(wheelhouse):
    plan = packages.plan(sorted(wheelhouse.glob("*.whl")))
    assert [p["name"] for p in plan] == [packages.RUNTIME, "xrdclient", "xgfalclient", "xrdarc"]
    runtime, native, gfal, arc = plan
    assert runtime["version"] == "0.4.0" and runtime["requires"] == []
    owned = {packages.dist_name(w) for w in runtime["wheels"]}
    assert {"xrdcore", "xrdsec", "xrdhttp", "cryptography", "bcrypt", "urllib3"} <= owned
    assert not owned & set(packages.CLIENTS)
    for client in (native, gfal, arc):
        assert [packages.dist_name(w) for w in client["wheels"]] == [client["name"]]
    assert native["requires"] == arc["requires"] == [(packages.RUNTIME, "0.4.0")]
    assert gfal["requires"] == [(packages.RUNTIME, "0.4.0"), ("xrdclient", "0.4.0")]


def test_package_plan_without_xrdarc_builds_only_the_two_clients(wheelhouse):
    (wheelhouse / "xrdarc-0.4.0-py3-none-any.whl").unlink()
    plan = packages.plan(sorted(wheelhouse.glob("*.whl")))
    assert [p["name"] for p in plan] == [packages.RUNTIME, "xrdclient", "xgfalclient"]


def test_package_plan_refuses_a_wheelhouse_without_the_libraries(wheelhouse):
    (wheelhouse / "xrdhttp-0.4.0-py3-none-any.whl").unlink()
    with pytest.raises(SystemExit, match="no wheel for xrdhttp"):
        packages.plan(sorted(wheelhouse.glob("*.whl")))


def test_runtime_package_rejects_a_prerelease_library(wheelhouse):
    (wheelhouse / "xrdsec-0.4.0-py3-none-any.whl").unlink()
    write_wheel(wheelhouse, "xrdsec", "0.4.1rc1")
    with pytest.raises(ValueError, match=r"major\.minor\.patch"):
        packages.plan(sorted(wheelhouse.glob("*.whl")))


#: xgfalclient's exact requirements: the shared runtime and the xrdclient it was tested with.
GFAL_REQUIRES = [(packages.RUNTIME, "0.4.0"), ("xrdclient", "0.4.0")]


@pytest.mark.parametrize("release", [1, 2])
def test_gfal_reuses_exact_native_runtime_in_debian_metadata(tmp_path, monkeypatch, release):
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: "amd64\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
    info = {"name": "xgfalclient", "version": "0.4.0", "release": release}
    packages.deb(stage, tmp_path, info | {"requires": GFAL_REQUIRES}, Path("/usr/bin/python3.12"))
    control = (stage / "DEBIAN/control").read_text()
    assert (
        f"Depends: {packages.RUNTIME} (= 0.4.0-{release}), xrdclient (= 0.4.0-{release})\n"
        in control
    )


def test_runtime_deb_depends_on_the_system_interpreter(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: "arm64\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
    info = {"name": packages.RUNTIME, "version": "0.4.0", "requires": []}
    packages.deb(stage, tmp_path, info, Path("/usr/bin/python3.12"))
    control = (stage / "DEBIAN/control").read_text()
    assert "Depends: python3.12, libc6 (>= 2.28), libgcc-s1\n" in control
    assert f"Package: {packages.RUNTIME}\nVersion: 0.4.0-1\nArchitecture: arm64\n" in control


@pytest.mark.parametrize("release", [1, 2])
def test_gfal_reuses_exact_native_runtime_in_rpm_metadata(tmp_path, monkeypatch, release):
    stage = tmp_path / "stage"
    (stage / "usr/bin").mkdir(parents=True)
    (stage / "usr/share/man/man1").mkdir(parents=True)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
    info = {"name": "xgfalclient", "version": "0.4.0", "release": release}
    packages.rpm(stage, tmp_path, info | {"requires": GFAL_REQUIRES}, Path("/usr/bin/python3.12"))
    spec = (tmp_path / "rpmbuild/package.spec").read_text()
    assert f"Requires: {packages.RUNTIME} = 0.4.0-{release}, xrdclient = 0.4.0-{release}\n" in spec
    assert "\n/opt/storage-clients/*\n/usr/bin/*\n" in spec
    assert "\n/usr/share/man/man1/*\n" in spec


def test_runtime_rpm_owns_its_directory_so_removal_leaves_nothing(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
    info = {"name": packages.RUNTIME, "version": "0.4.0", "requires": []}
    packages.rpm(stage, tmp_path, info, Path("/usr/bin/python3.12"))
    spec = (tmp_path / "rpmbuild/package.spec").read_text()
    assert "\n/opt/storage-clients\n" in spec
    assert "/opt/storage-clients/*" not in spec
    # No commands and no manual pages: an empty %files glob fails rpmbuild.
    assert "/usr/bin/*" not in spec and "man1" not in spec
    assert "Requires: /usr/bin/python3.12, glibc, libgcc\n" in spec
    assert "Summary: Shared Python libraries" in spec


def test_installed_smoke_takes_per_client_bin_dirs():
    assert smoke.client_bin("xrdarc=/opt/arc/bin") == ("xrdarc", Path("/opt/arc/bin"))
    for bad in ("xrdarc", "nothing=/bin"):
        with pytest.raises(argparse.ArgumentTypeError):
            smoke.client_bin(bad)
    assert set(smoke.VERSION_CHECKS) == set(smoke.CLIENTS) == set(packages.CLIENTS)
    assert set(smoke.COPY_COMMANDS) == set(brew.COPY_COMMANDS)


# ---------------------------------------------------------------------------
# xrddevtools.chain_preflight
# ---------------------------------------------------------------------------

#: A consistent 0.4.0 chain: each package's default and optional dependencies.
GOOD_CHAIN = {
    "xrdcore": ([], {"fast": ["google-crc32c>=1.5"]}),
    "xrdsec": (["xrdcore>=0.4.0,<0.5", "cryptography>=45"], {}),
    "xrdhttp": (["xrdcore>=0.4.0,<0.5"], {"s3": ["botocore>=1.35"]}),
    "xrdclient": (
        ["xrdcore>=0.4.0,<0.5", "xrdsec>=0.4.0,<0.5", "xrdhttp[s3]>=0.4.0,<0.5"],
        {"fast": ["xrdcore[fast]>=0.4.0,<0.5"]},
    ),
    "xgfalclient": (
        [
            "xrdclient==0.4.0",
            "xrdcore>=0.4.0,<0.5",
            "xrdsec>=0.4.0,<0.5",
            "xrdhttp[s3]>=0.4.0,<0.5",
        ],
        {},
    ),
    "xrdarc": (["xrdcore>=0.4.0,<0.5", "xrdsec>=0.4.0,<0.5", "xrdhttp>=0.4.0,<0.5"], {}),
}


def write_checkout(
    workspace: Path,
    name: str,
    dependencies: Sequence[str],
    optional: Mapping[str, Sequence[str]] | None = None,
    version: str = "0.4.0",
    heading: str | None = None,
) -> Path:
    """A checkout with the three files the pre-flight reads."""
    root = workspace / name
    (root / "src" / name).mkdir(parents=True, exist_ok=True)
    (root / "src" / name / "_version.py").write_text(f'"""Doc."""\n__version__ = "{version}"\n')
    extras = "".join(f"{key} = {json.dumps(value)}\n" for key, value in (optional or {}).items())
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\ndependencies = {json.dumps(dependencies)}\n'
        f"[project.optional-dependencies]\n{extras}"
    )
    title = heading or "## [0.4.0] - Unreleased"
    (root / "CHANGELOG.md").write_text(f"# Changelog\n\n{title}\n\n- Things.\n")
    return root


@pytest.fixture
def chain(tmp_path):
    """A workspace holding :data:`GOOD_CHAIN`; returns the directory."""
    for name, (dependencies, optional) in GOOD_CHAIN.items():
        write_checkout(tmp_path, name, dependencies, optional)
    return tmp_path


def preflight(workspace: Path, *extra: str) -> list[str]:
    """Run ``chain_preflight workspace --no-build`` in-process; the parsed findings."""
    options = chain_preflight.parser().parse_args(
        ["workspace", "--workspace", str(workspace), "--no-build", *extra]
    )
    return chain_preflight.check_workspace(options)


def test_a_consistent_chain_has_no_findings(chain):
    assert preflight(chain) == []
    assert preflight(chain, "--version", "0.4.0", "--package", "xrdarc") == []


def test_layering_and_pin_mistakes_are_each_reported(chain):
    write_checkout(chain, "xrdsec", ["xrdcore>=0.4.0", "xrdhttp>=0.4.0,<0.5"], version="0.4.1")
    write_checkout(chain, "xgfalclient", ["xrdclient>=0.4.0,<0.5", "xrdcore>=0.4.0,<0.5"])
    write_checkout(chain, "xrdarc", [*GOOD_CHAIN["xrdarc"][0], "xrdclient==0.4.0"])
    write_checkout(chain, "xrdclient", GOOD_CHAIN["xrdclient"][0], {"fast": ["xrdcore[fast]>=0.3"]})
    assert preflight(chain) == [
        "xrdsec: version is 0.4.1, not 0.4.0",
        "xrdsec: must not depend on xrdhttp (layering)",
        "xrdsec: requires xrdcore>=0.4.0, expected xrdcore<0.5,>=0.4.0",
        "xrdclient: requires xrdcore>=0.3, expected xrdcore<0.5,>=0.4.0",
        "xgfalclient: does not require xrdhttp",
        "xgfalclient: does not require xrdsec",
        "xgfalclient: requires xrdclient<0.5,>=0.4.0, expected xrdclient==0.4.0",
        "xrdarc: must not depend on xrdclient (layering)",
    ]


def test_an_extra_may_only_require_a_package_released_earlier(chain):
    # xrdhttp's testing extra on xrdsec is the release order; the reverse is not,
    # and an extra naming its own package (an "all" extra) is fine.
    write_checkout(chain, "xrdhttp", ["xrdcore>=0.4.0,<0.5"], {"testing": ["xrdsec>=0.4.0,<0.5"]})
    write_checkout(
        chain,
        "xrdsec",
        GOOD_CHAIN["xrdsec"][0],
        {"http": ["xrdhttp>=0.4.0,<0.5"], "all": ["xrdsec[http]>=0.4.0,<0.5"]},
    )
    assert preflight(chain) == ["xrdsec: an extra requires xrdhttp, which is released after it"]


def test_missing_checkouts_versions_and_changelog_sections_are_findings(chain):
    shutil.rmtree(chain / "xrdhttp")
    (chain / "xrdcore/CHANGELOG.md").write_text("# Changelog\n\n## [0.3.2] - 2026-10-07\n")
    (chain / "xrdsec/src/xrdsec/_version.py").unlink()
    assert preflight(
        chain, "--package", "xrdcore", "--package", "xrdsec", "--package", "xrdhttp"
    ) == [
        "xrdcore: CHANGELOG.md has no [0.4.0] section",
        "xrdsec: version is None, not 0.4.0",
        f"xrdhttp: no checkout at {chain / 'xrdhttp'}",
    ]
    (chain / "xrdcore/src/xrdcore/_version.py").unlink()
    assert preflight(chain) == ["xrdcore: no version to check the chain against (pass --version)"]


def test_require_dated_refuses_an_unreleased_changelog_section(chain):
    write_checkout(chain, "xrdcore", [], heading="## [0.4.0] - 2026-10-20")
    findings = preflight(chain, "--require-dated")
    assert len(findings) == 5
    assert "xrdcore" not in " ".join(findings)
    assert findings[0] == "xrdsec: CHANGELOG.md [0.4.0] is not dated ('## [0.4.0] - Unreleased')"


def test_after_1_0_the_range_is_the_next_major():
    assert str(chain_preflight.expected_specifier("xrdsec", "xrdcore", "1.2.0")) == "<2,>=1.2.0"
    assert str(chain_preflight.expected_specifier("xgfalclient", "xrdclient", "1.2.0")) == "==1.2.0"


def fake_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    requires: Sequence[str],
    version: str = "0.4.0",
    codes: Sequence[int] = (0, 0),
    wheel: bool = True,
) -> tuple[ModuleType, list[list[str]]]:
    """Make :func:`chain_preflight.run` build a synthetic wheel; returns the recorded argv."""
    calls: list[list[str]] = []
    results = iter(codes)

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if "build" in argv and wheel:
            folder = Path(argv[argv.index("--outdir") + 1])
            write_wheel(folder, "xrdsec", version, (), requires)
            (folder / f"xrdsec-{version}.tar.gz").write_bytes(b"")
        return subprocess.CompletedProcess(
            argv, next(results), stdout="out\n", stderr="last line\n"
        )

    monkeypatch.setattr(chain_preflight, "run", run)
    return chain_preflight, calls


def test_the_built_wheel_must_match_the_declared_metadata(chain, tmp_path, monkeypatch):
    module, calls = fake_build(tmp_path, monkeypatch, requires=["xrdcore<0.5,>=0.4.0", "x>=1"])
    declared = GOOD_CHAIN["xrdsec"][0]
    assert module.build_findings("xrdsec", chain / "xrdsec", "0.4.0", declared) == []
    assert calls[0][1:4] == ["-m", "build", "--no-isolation"]
    assert calls[1][1:5] == ["-m", "twine", "check", "--strict"] and len(calls[1]) == 7
    module, _ = fake_build(tmp_path, monkeypatch, requires=["xrdcore>=0.3"], version="0.4.1")
    assert module.build_findings("xrdsec", chain / "xrdsec", "0.4.0", declared) == [
        "xrdsec: wheel METADATA version 0.4.1 is not 0.4.0",
        "xrdsec: wheel Requires-Dist ['xrdcore>=0.3'] differs from pyproject.toml",
    ]


def test_without_no_build_each_selected_package_is_built_and_inspected(
    chain, tmp_path, monkeypatch
):
    module, calls = fake_build(tmp_path, monkeypatch, requires=["xrdcore<0.5,>=0.4.0"])
    options = module.parser().parse_args(
        ["workspace", "--workspace", str(chain), "--package", "xrdsec"]
    )
    assert module.check_workspace(options) == []
    assert [call[1:3] for call in calls] == [["-m", "build"], ["-m", "twine"]]


@pytest.mark.parametrize(
    ("codes", "wheel", "expected"),
    [
        ((2,), True, ["xrdsec: build failed (2): out | last line"]),
        ((0, 1), True, ["xrdsec: twine check failed (1): out | last line"]),
        ((0, 0), False, ["xrdsec: the build produced no wheel"]),
    ],
)
def test_build_and_twine_failures_are_findings(
    chain, tmp_path, monkeypatch, codes, wheel, expected
):
    module, _ = fake_build(tmp_path, monkeypatch, requires=[], codes=codes, wheel=wheel)
    assert module.build_findings("xrdsec", chain / "xrdsec", "0.4.0", []) == expected


def test_a_real_build_of_a_tiny_project_passes(tmp_path):
    # A real build needs the build backend and twine, which an install
    # without the dev extra (and its build tools) does not have.
    pytest.importorskip("hatchling", reason="optional: the hatchling build backend is absent")
    pytest.importorskip("twine", reason="optional: twine (the dev extra) is not installed")
    root = write_checkout(tmp_path, "xrdcore", [])
    (root / "src/xrdcore/__init__.py").write_text('"""Doc."""\n')
    (root / "README.md").write_text("# xrdcore\n")
    project = (root / "pyproject.toml").read_text()
    (root / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
        + project.replace(
            "dependencies = []",
            'version = "0.4.0"\ndescription = "x"\nreadme = "README.md"\ndependencies = []',
        )
    )
    assert chain_preflight.build_findings("xrdcore", root, "0.4.0", []) == []


def test_pypi_preflight_downloads_each_chain_requirement_at_its_lower_bound(tmp_path, monkeypatch):
    wheel = write_wheel(
        tmp_path,
        "xgfalclient",
        "0.4.0",
        (),
        ["xrdclient==0.4.0", "xrdcore<0.5,>=0.4.0", "bcrypt>=4", "xrdhttp; extra == 'x'", "xrdsec"],
    )
    calls = []

    def run(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, int(argv[-1] == "xrdsec"), stdout="", stderr="no")

    monkeypatch.setattr(chain_preflight, "run", run)
    assert chain_preflight.main(["pypi", str(wheel), "--json"]) == 1
    assert [argv[-1] for argv in calls] == ["xrdclient==0.4.0", "xrdcore==0.4.0", "xrdsec"]
    assert all("--only-binary=:all:" in argv and "--no-deps" in argv for argv in calls)
    assert calls[0][calls[0].index("--index-url") + 1] == "https://pypi.org/simple/"


def test_preflight_prints_findings_and_sets_the_exit_status(chain, capsys):
    assert chain_preflight.main(["workspace", "--workspace", str(chain), "--no-build"]) == 0
    assert capsys.readouterr().out == "no findings\n"
    write_checkout(chain, "xrdhttp", [])
    assert (
        chain_preflight.main(["workspace", "--workspace", str(chain), "--no-build", "--json"]) == 1
    )
    assert json.loads(capsys.readouterr().out) == ["xrdhttp: does not require xrdcore"]


def test_a_failing_step_is_a_result_with_its_output_not_an_exception():
    script = "import sys; print('out'); sys.exit(3)"
    result = chain_preflight.run([sys.executable, "-c", script])
    assert (result.returncode, result.stdout) == (3, "out\n")
    assert chain_preflight.failed("xrdsec", "step", result) == ["xrdsec: step failed (3): out"]
