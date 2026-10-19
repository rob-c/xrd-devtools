#!/usr/bin/env bash
# Executed only INSIDE a disposable container. Never run on the host.
#
# xrddevtools.platforms mounts each checkout of the 0.4.0 chain read-only at
# /src/<name>: the libraries xrdcore, xrdsec and xrdhttp, the clients
# xrdclient and xgfalclient, and xrdarc when the workspace has it; and the
# xrd-devtools checkout this script lives in at /src/xrd-devtools, whose
# package builder and smoke test run below as plain files. Everything is
# built from those checkouts, so no candidate needs to be on PyPI yet.
set -euo pipefail
test -d /src/xrdclient
test -d /src/xrd-devtools
test -d /artifacts
family=$1
interpreter=$2
shift 2
if [[ "$family" == rpm ]]; then
    dnf -y install "$@"
    dnf clean all
else
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends "$@"
fi
cp /etc/os-release /artifacts/os-release
"$interpreter" --version > /artifacts/python-version.txt
# Lowest layer first. xrdarc is optional; every other checkout is required
# (platforms.py refuses a workspace without them).
projects=()
for project in xrdcore xrdsec xrdhttp xrdclient xgfalclient xrdarc; do
    if [[ -d "/src/$project" ]]; then
        projects+=("$project")
    fi
done
# The client packages built below, and the order they are removed in.
clients=(xrdclient xgfalclient)
if [[ -d /src/xrdarc ]]; then
    clients+=(xrdarc)
fi
mkdir /work
# Explicit source list: don't copy .git, credentials, venvs or build caches.
# xrd-devtools sits beside the projects, as in a workspace, so each suite's
# take-over and maintainability gates find it (pythonpath ../xrd-devtools/src).
for project in "${projects[@]}" xrd-devtools; do
    mkdir "/work/$project"
    for entry in src tests tools benchmarks examples packaging share typings .github .gitignore pyproject.toml README.md LICENSE COPYING CHANGELOG.md maintainability.json; do
        if [[ -e "/src/$project/$entry" ]]; then
            cp -R "/src/$project/$entry" "/work/$project/"
        fi
    done
done
tools=/work/xrd-devtools/src/xrddevtools
"$interpreter" -m venv /work/build-env
buildpy=/work/build-env/bin/python
"$buildpy" -m pip install --only-binary=:all: 'pip>=24' 'build>=1,<2' 'hatchling>=1.27,<1.32' 'twine>=6,<7'
mkdir /artifacts/wheels /artifacts/wheelhouse
for project in "${projects[@]}"; do
    "$buildpy" -m build --no-isolation --outdir /artifacts/wheels "/work/$project"
done
"$buildpy" -m twine check --strict /artifacts/wheels/*
# Binary-only resolution of the whole chain. --find-links lets the clients'
# requirements on the libraries resolve to the wheels just built rather than
# to PyPI, where they may not be published yet.
"$buildpy" -m pip download --only-binary=:all: --find-links /artifacts/wheels --dest /artifacts/wheelhouse /artifacts/wheels/*.whl
"$interpreter" -m venv /work/runtime
runtimepy=/work/runtime/bin/python
"$runtimepy" -m pip install --no-index --only-binary=:all: --find-links=/artifacts/wheelhouse /artifacts/wheels/*.whl
"$runtimepy" -m pip check
"$runtimepy" -m pip inspect > /artifacts/dependencies.json
"$runtimepy" "$tools/installed_smoke.py" --bin-dir /work/runtime/bin --clients "${clients[@]}"
"$buildpy" "$tools/distribution_package.py" --family "$family" --wheelhouse /artifacts/wheelhouse --output /artifacts/packages --python "$(command -v "$interpreter")"
if [[ "$family" == rpm ]]; then
    dnf -y install /artifacts/packages/*.rpm
else
    apt-get install -y /artifacts/packages/*.deb
fi
"$interpreter" -I -S -B /opt/storage-clients/launch.py --smoke "$tools/installed_smoke.py" --bin-dir /usr/bin --clients "${clients[@]}"
# Run all hermetic/fault/CLI/VOMS tests as an ordinary user: root would mask
# permission failures. Real-server interoperability remains a separate CI gate.
"$runtimepy" -m pip install --only-binary=:all: pytest pytest-cov pytest-timeout pytest-xdist fsspec 'hypothesis>=6.100,<7'
if ! "$runtimepy" -m pip install --only-binary=:all: 'complexipy>=6,<7' 'radon>=6,<7'; then
    # A pre-release interpreter (Fedora Rawhide) can predate the complexipy
    # wheels. The take-over and maintainability gates then skip themselves
    # (an environmental skip; CI runs them on released Pythons, where they are
    # required); every runtime test still runs here.
    echo "complexipy/radon unavailable: the xrd-devtools gates will skip" >&2
fi
useradd --create-home tester
mkdir /artifacts/tests
# Keep the bind-mounted parent owned by the runner so it can record results
# after the container exits, including when a test process is killed.
chown -R tester /work /artifacts/tests
suite_status=0
for project in "${projects[@]}"; do
    cd "/work/$project"
    runuser -u tester -- "$runtimepy" -m pytest -q -p no:cacheprovider -n 4 -m 'not interop and not parity' --timeout=300 --cov --cov-fail-under=0 --cov-report="xml:/artifacts/tests/$project-coverage.xml" --junitxml="/artifacts/tests/$project-tests.xml" || suite_status=1
done
# Package-manager removal must remove the commands and leave no private
# runtime. Clients first, the runtime package that owns the prefix last.
removal=()
for (( index=${#clients[@]}-1; index>=0; index-- )); do
    removal+=("${clients[index]}")
done
removal+=(storage-clients-libs)
if [[ "$family" == rpm ]]; then
    rpm -e "${removal[@]}"
else
    dpkg --purge "${removal[@]}"
fi
test ! -e /usr/bin/xrd-cp
test ! -e /usr/bin/gfal-copy
test ! -e /usr/bin/xrdarc
test ! -e /opt/storage-clients
exit "$suite_status"
