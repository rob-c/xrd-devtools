"""The reusable workflows and the actions every repository's CI and release call.

They cannot run here (GitHub runs them), so what is pinned is the contract a
caller relies on: the inputs it passes exist, every job checks out the
candidates through the one action, credentials are never persisted, and each
gate the static job promises is a step that fails rather than skips.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
ACTION = ROOT / ".github" / "actions" / "candidates" / "action.yml"
PUBLISH = ROOT / ".github" / "actions" / "publish" / "action.yml"
#: The repositories that release; each builds its release in ``publish.yml``.
RELEASED = ("xrdcore", "xrdsec", "xrdhttp", "xrdclient", "xgfalclient", "xrdarc", "xrdwms")
CHAIN = ("xrdcore", "xrdsec", "xrdhttp", "xrdclient", "xgfalclient", "xrdarc", "xrdwms")
REUSABLE = ("static.yml", "tests.yml", "chain.yml", "package.yml", "platforms.yml")


def _read(name: str) -> str:
    """One workflow file's text."""
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_the_candidates_action_checks_out_every_repository_it_is_asked_for():
    action = ACTION.read_text(encoding="utf-8")
    for name in CHAIN:
        assert f"if: contains(format(' {{0}} ', inputs.packages), ' {name} ')" in action
        assert f"repository: rob-c/{name}\n" in action
        assert f"{name}-ref:" in action
    assert "repository: rob-c/xrd-devtools\n" in action  # always: its gates run
    assert action.count("persist-credentials: false") == len(CHAIN) + 1
    # Installed and built lowest layer first, whatever order the caller lists.
    loop = "for name in xrdcore xrdsec xrdhttp xrdclient xgfalclient xrdarc xrdwms; do"
    assert action.count(loop) == 2
    assert '-e "$ROOT/xrd-devtools"' in action


@pytest.mark.parametrize("name", REUSABLE)
def test_every_reusable_workflow_is_callable_and_read_only(name):
    workflow = _read(name)
    assert "\n  workflow_call:\n" in workflow
    assert "permissions:\n  contents: read\n" in workflow
    # Steps only (comment lines show callers how to use the workflow).
    uses = re.findall(r"^ +(?:- )?uses: (rob-c/xrd-devtools/\S+)", workflow, re.MULTILINE)
    assert set(uses) <= {"rob-c/xrd-devtools/.github/actions/candidates@main"}


@pytest.mark.parametrize("name", ["static.yml", "tests.yml", "package.yml"])
def test_jobs_on_the_callers_code_check_it_out_without_credentials(name):
    workflow = _read(name)
    assert (
        "      - uses: actions/checkout@v4\n        with:\n          persist-credentials: false\n"
        in (workflow)
    )
    assert "uses: rob-c/xrd-devtools/.github/actions/candidates@main" in workflow


def test_the_static_job_runs_every_gate_and_makes_skips_failures():
    workflow = _read("static.yml")
    for step in (
        "ruff check $LINT_PATHS",
        "ruff format --check $LINT_PATHS",
        "run: mypy\n",
        "run: complexipy --quiet\n",
        "-p xrddevtools.pytest_plugin",
        "xrddevtools maintainability check",
        "xrddevtools conformance --repo .",
    ):
        assert step in workflow, step
    assert 'XRD_DEVTOOLS_REQUIRED: "1"' in workflow


def test_the_test_matrix_comes_from_the_caller():
    workflow = _read("tests.yml")
    for axis in ("os", "python-versions", "extras", "include"):
        assert f"${{{{ fromJSON(inputs.{axis}) }}}}" in workflow
    assert '${MARKERS:+-m "$MARKERS"} $PYTEST_ARGS' in workflow


def test_the_chain_gates_check_out_all_seven_repositories_side_by_side():
    workflow = _read("chain.yml")
    assert "packages: xrdcore xrdsec xrdhttp xrdclient xgfalclient xrdarc xrdwms\n" in workflow
    for name in CHAIN:
        assert f"{name}-ref: ${{{{ inputs.{name}-ref }}}}" in workflow
    # The near-duplicate threshold is the hard gate's: no allow-list, no warn-only.
    assert "xrddevtools dupes --workspace workspace --fail-near 0.85\n" in workflow
    assert "xrddevtools conformance --workspace workspace" in workflow


def test_the_package_job_runs_check_dist_from_the_devtools_checkout():
    workflow = _read("package.yml")
    script = ".dependencies/xrd-devtools/src/xrddevtools/check_dist.py"
    assert f'python {script} contents "$PACKAGE" dist' in workflow
    assert f'-I "$GITHUB_WORKSPACE"/{script} imports "$PACKAGE"' in workflow


def test_the_platform_jobs_build_from_the_candidates_beside_this_checkout():
    workflow = _read("platforms.yml")
    names = CHAIN[:-1]  # xrdwms is not packaged
    for name in names:
        assert workflow.count(f"\n      {name}-ref:\n") == 2  # manual runs and calls
        assert workflow.count(f"{name}-ref: ${{{{ inputs.{name}-ref || 'main' }}}}") == 3
    assert workflow.count("path: clients\n") == 3
    assert workflow.count("devtools-ref: ${{ env.DEVTOOLS_REF }}") == 3
    assert "nix-build clients/xrd-devtools/packaging/nix -A nixosTest" in workflow
    assert "python -m xrddevtools homebrew" in workflow
    assert workflow.count("clients/xrd-devtools/src/xrddevtools/installed_smoke.py") == 1


def test_this_repository_runs_its_own_gates_on_its_own_commit():
    workflow = _read("ci.yml")
    for name in ("static", "tests", "chain"):
        assert f"uses: ./.github/workflows/{name}.yml\n" in workflow
    assert workflow.count("devtools-ref: ${{ github.sha }}") == 3
    # Held to its own take-over scorecard: the static job's default module.
    assert "scorecard:" not in workflow
    assert "scorecard_gate(" in (ROOT / "tests" / "test_scorecard.py").read_text()


def _inputs(action: str) -> set[str]:
    """The input names an action declares (two-space keys under ``inputs:``)."""
    block = action.split("\ninputs:\n", 1)[1].split("\noutputs:\n", 1)[0]
    return set(re.findall(r"^  ([a-z][a-z-]*):$", block, re.MULTILINE))


def test_the_publish_action_holds_every_release_step_but_the_upload():
    action = PUBLISH.read_text(encoding="utf-8")
    assert "using: composite" in action
    order = [
        "uses: actions/checkout@v4\n      with:\n        persist-credentials: false",
        "run: python -m build",
        "run: twine check --strict dist/*",
        'run: python "$TOOLS/check_dist.py" contents "$PACKAGE" dist',
        'run: python "$TOOLS/chain_preflight.py" pypi dist/*.whl',
        'release/bin/python -m pip install --only-binary=:all: "$wheel${EXTRAS:+[$EXTRAS]}"',
        "release/bin/python -m pip check",
        'run: release/bin/python -I "$TOOLS/check_dist.py" imports "$PACKAGE"',
        'release/bin/python -I "$TOOLS/release.py" installed "$PACKAGE" "${options[@]}"',
        'bash -euo pipefail -c "$POST_INSTALL"',
        '"$py" -m pytest -q -p no:cacheprovider -n auto',
        "uses: actions/upload-artifact@v4",
    ]
    positions = [action.index(step) for step in order]
    assert positions == sorted(positions)
    # Trusted publishing ties PyPI's trust to the caller's job: no upload, no token here.
    assert "gh-action-pypi-publish" not in action.split("\nruns:\n")[1]
    assert "id-token" not in action.split("\nruns:\n")[1]
    # The scripts come from the action's own copy of this repository, outside the workspace.
    assert action.count("TOOLS: ${{ github.action_path }}/../../../src/xrddevtools") == 4
    assert ".dependencies" not in action
    for script in ("check_dist.py", "chain_preflight.py", "release.py"):
        assert (ROOT / "src" / "xrddevtools" / script).is_file()


def test_every_release_workflow_of_the_workspace_builds_with_it_and_passes_known_inputs():
    """The sibling checkouts' release workflows, where the workspace has them."""
    known = _inputs(PUBLISH.read_text(encoding="utf-8"))
    assert {"package", "check-dist", "preflight", "no-dependencies", "absent"} <= known
    for name in RELEASED:
        path = ROOT.parent / name / ".github" / "workflows" / "publish.yml"
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        build = text.split("uses: rob-c/xrd-devtools/.github/actions/publish@main\n", 1)[1]
        passed = re.findall(r"^          ([a-z][a-z-]*):", build.split("\n\n", 1)[0], re.M)
        assert f"package: {name}\n" in build and set(passed) <= known, (name, passed)
        assert text.count("pypa/gh-action-pypi-publish@release/v1") == 1
