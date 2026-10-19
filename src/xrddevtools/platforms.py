#!/usr/bin/env python3
"""Run the shared distribution matrix with rootless Podman (or Docker).

``packaging/platforms.json`` (at the root of the xrd-devtools checkout,
:data:`CHECKOUT`) lists one container per supported Linux
distribution (image, package family, interpreter, OS packages to install).
This tool starts each one with the chain's sibling checkouts mounted
read-only - the libraries xrdcore, xrdsec and xrdhttp, the clients xrdclient
and xgfalclient, and xrdarc when the workspace has it (:func:`sources`) - and
this xrd-devtools checkout, and runs ``packaging/linux.sh`` inside it, which
builds, installs and tests the
wheels and the native packages. CI uses ``--list`` to fan the same matrix
out as a GitHub Actions ``include`` list, so local runs and CI cannot drift
apart. xrdclient's ``docs/platforms.md`` (*Run the Linux matrix*) is the
operator's guide.

Each platform gets its own ``--output/<name>/`` directory holding the full
container log, the resolved image identity and a ``result.json``; the exit
status is non-zero if any platform failed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

#: The xrd-devtools checkout this module runs from: ``packaging/`` (the matrix,
#: ``linux.sh`` and the Nix expression) lives at its root, beside ``src/``, and
#: the checkout is mounted into every container so ``linux.sh`` can run the
#: package builder and the smoke test from it. The platform tools therefore
#: run from a checkout (editable install or ``PYTHONPATH=src``), never a wheel.
CHECKOUT = Path(__file__).resolve().parents[2]
#: The container matrix, shared with CI; see the module docstring.
MATRIX = CHECKOUT / "packaging/platforms.json"


#: Hosted runners for each architecture the CI matrix builds on.
RUNNERS = {"amd64": "ubuntu-latest", "arm64": "ubuntu-24.04-arm"}

#: The image architectures ``--arch`` may select.
ARCHITECTURES = ("amd64", "arm64")


def config() -> dict[str, Any]:
    """The whole of :data:`MATRIX`: containers, native platforms and the source checkouts."""
    return cast("dict[str, Any]", json.loads(MATRIX.read_text(encoding="utf-8")))


def sources(workspace: Path) -> list[str]:
    """The checkouts of ``workspace`` to mount: every required one, and each optional one present.

    The lists are :data:`MATRIX`'s ``sources``, lowest layer first, which is
    also the order ``linux.sh`` builds them in. A missing required checkout
    is :func:`validate_workspace`'s error, so it is listed here regardless.
    """
    chain = config()["sources"]
    optional = [
        name for name in chain["optional"] if (workspace / name / "pyproject.toml").is_file()
    ]
    return [*chain["required"], *optional]


def matrix() -> list[dict[str, Any]]:
    """The container definitions from :data:`MATRIX`."""
    return cast("list[dict[str, Any]]", config()["containers"])


def ci_matrix() -> dict[str, Any]:
    """Every container on every architecture, each on its own native runner.

    The RPM, DEB and wheel bundles are architecture-specific, so each
    architecture's artifacts must come from a job running natively on it
    rather than under emulation; this is the ``include`` list CI fans out.
    """
    return {
        "include": [
            {**target, "arch": arch, "runner": runner}
            for target in matrix()
            for arch, runner in RUNNERS.items()
        ]
    }


def command(
    engine: str, workspace: Path, output: Path, target: dict[str, Any], arch: str | None = None
) -> list[str]:
    """The ``podman run`` (or ``docker run``) argv for one matrix ``target``.

    The flags are the security boundary of a run that installs OS packages
    as root inside the container: only the chain's source trees and this
    xrd-devtools checkout (read-only, one mount each, never their parent)
    and the output directory are
    mounted, privilege escalation is disabled, and CPU, memory and process
    limits keep a runaway test from starving the host. ``label=disable`` is
    needed on SELinux hosts, where the read-only bind mounts would otherwise
    be unreadable. ``--pull=always`` makes every
    run test the distribution's current image rather than a stale local one.
    An explicit ``arch`` selects that image variant and runs it under the
    engine's emulation when it differs from the host; the default is native.
    """
    platform = [f"--platform=linux/{arch}"] if arch else []
    mounts = [
        argument
        for name in sources(workspace)
        for argument in ("-v", f"{workspace.resolve() / name}:/src/{name}:ro")
    ]
    tooling = ["-v", f"{CHECKOUT}:/src/xrd-devtools:ro"]
    return [
        engine,
        "run",
        "--rm",
        "--pull=always",
        *platform,
        "--cpus=4",
        "--memory=2g",
        "--pids-limit=2048",
        "--security-opt=no-new-privileges",
        "--security-opt=label=disable",
        *mounts,
        *tooling,
        "-v",
        f"{output.resolve()}:/artifacts:rw",
        target["image"],
        "bash",
        "/src/xrd-devtools/packaging/linux.sh",
        target["family"],
        target["python"],
        *target["packages"],
    ]


def run(
    engine: str, workspace: Path, output: Path, target: dict[str, Any], arch: str | None = None
) -> dict[str, Any]:
    """Run one platform to completion and record its result under ``output``.

    A failing container is a result, not an exception (``check=False``), so
    one broken distribution does not abort the rest of the matrix. The image
    digest is recorded because ``--pull=always`` means the tag alone does not
    say what was tested, and the architecture because an emulated run proves
    the packages but not the kernel.
    """
    output.mkdir(parents=True, exist_ok=True)
    with (output / "container.log").open("w", encoding="utf-8") as log:
        result = subprocess.run(
            command(engine, workspace, output, target, arch),
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    inspected = subprocess.run(
        [engine, "image", "inspect", target["image"]], capture_output=True, text=True, check=False
    )
    (output / "image.json").write_text(inspected.stdout, encoding="utf-8")
    report = {
        "platform": target["name"],
        "image": target["image"],
        "arch": arch or "native",
        "exit_code": result.returncode,
        "ok": result.returncode == 0,
    }
    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report), flush=True)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    """Print the matrix (``--list``) or run the selected platforms; non-zero if any failed.

    ``--jobs`` is capped at 4 because each container is given 4 CPUs and
    2 GiB and runs the full hermetic suites; more in parallel mostly turns
    timing-sensitive tests into flakes rather than finishing sooner.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="print the container matrix as JSON")
    parser.add_argument("--engine", choices=("podman", "docker"), default="podman")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="directory of sibling checkouts: xrdcore/, xrdsec/, xrdhttp/, xrdclient/, "
        "xgfalclient/ and, optionally, xrdarc/",
    )
    parser.add_argument("--output", type=Path, default=Path("platform-results"))
    parser.add_argument("--jobs", type=int, choices=(1, 2, 3, 4), default=1)
    parser.add_argument(
        "--arch",
        choices=ARCHITECTURES,
        help="image architecture to run (default: the host's own; others need emulation)",
    )
    parser.add_argument("--platform", action="append", choices=[t["name"] for t in matrix()])
    args = parser.parse_args(argv)
    if args.list:
        print(json.dumps(ci_matrix()))
        return 0
    validate_workspace(parser, args.workspace)
    selected = [t for t in matrix() if not args.platform or t["name"] in args.platform]
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        pending = [
            pool.submit(run, args.engine, args.workspace, args.output / t["name"], t, args.arch)
            for t in selected
        ]
        reports = [task.result() for task in pending]
    return int(any(not report["ok"] for report in reports))


def validate_workspace(parser: argparse.ArgumentParser, workspace: Path) -> None:
    """Exit with a usage error unless ``workspace`` holds every required checkout.

    Checked before any container starts: a wrong ``--workspace`` would
    otherwise surface minutes later as an obscure failure inside each one.
    """
    for name in config()["sources"]["required"]:
        if not (workspace / name / "pyproject.toml").is_file():
            parser.error(f"{workspace} must contain {name}/pyproject.toml")


if __name__ == "__main__":
    raise SystemExit(main())
