# One expression for the whole 0.4.0 chain: the libraries xrdcore, xrdsec and
# xrdhttp, the clients xrdclient and xgfalclient, and xrdarc. Runtime
# dependency constraints are checked by Nix's Python hooks; none are relaxed
# or removed. Each package's `dependencies` mirror its pyproject.toml.
#
# The expression lives in xrd-devtools (packaging/nix); sources default to
# sibling checkouts of the xrd-devtools one (the workspace layout and CI's
# `clients/` directory); pass `--arg workspace ./dir` or `--arg <name>Src
# ./path` to use others. xgfalclient and xrdarc are built only when their
# source is present.
{ pkgs ? import <nixpkgs> { }
, workspace ? ../../..
, xrdclientSrc ? workspace + "/xrdclient"
, xrdcoreSrc ? workspace + "/xrdcore"
, xrdsecSrc ? workspace + "/xrdsec"
, xrdhttpSrc ? workspace + "/xrdhttp"
, xgfalclientSrc ? (if builtins.pathExists (workspace + "/xgfalclient/pyproject.toml") then workspace + "/xgfalclient" else null)
, xrdarcSrc ? (if builtins.pathExists (workspace + "/xrdarc/pyproject.toml") then workspace + "/xrdarc" else null)
}:
let
  py = pkgs.python312Packages;
  version = src: name:
    let
      lines = pkgs.lib.splitString "\n" (builtins.readFile (src + "/src/${name}/_version.py"));
      line = builtins.head (builtins.filter (pkgs.lib.hasPrefix "__version__ = ") lines);
    in builtins.head (builtins.match "__version__ = \"([0-9]+\\.[0-9]+\\.[0-9]+)\"" line);
  # A working checkout carries build output and caches: an old wheel left in
  # dist/ would even be what the build hooks inspect. Keep only sources.
  ignored = [ "dist" "build" "site" "result" ".venv" "venv" ".dependencies" "htmlcov"
              ".mypy_cache" ".pytest_cache" ".ruff_cache" ".hypothesis" ".complexipy_cache"
              "__pycache__" ];
  sourceOf = src: pkgs.lib.cleanSourceWith {
    src = pkgs.lib.cleanSource src;
    filter = path: type:
      let base = baseNameOf path;
      in !(builtins.elem base ignored || pkgs.lib.hasPrefix ".coverage" base);
  };
  make = name: src: deps: extra: py.buildPythonPackage ({
    pname = name;
    version = version src name;
    src = sourceOf src;
    pyproject = true;
    build-system = [ py.hatchling ];
    # The upper bound works around Twine's metadata inspection, which Nix
    # doesn't use. Change only this build-tool bound, not runtime requirements.
    postPatch = ''
      substituteInPlace pyproject.toml --replace-fail "hatchling>=1.27,<1.32" "hatchling>=1.27"
    '';
    dependencies = deps;
    nativeCheckInputs = with py; [ pytestCheckHook pytest-timeout pytest-xdist pytest-cov fsspec hypothesis ];
    # The xrd-devtools gates (tests/test_scorecard.py, xrdclient's
    # tests/test_maintainability.py) skip themselves here: the sandbox holds
    # no xrd-devtools checkout, and this Nixpkgs has no complexipy. They are
    # required in CI.
    disabledTestMarks = [ "interop" "parity" ];
    pytestFlags = [ "-q" "-n" "4" "--timeout=300" "-p" "no:cacheprovider" ];
    pythonImportsCheck = [ name ];
    SSL_CERT_FILE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
    # Nixpkgs' OpenSSL prefers NIX_SSL_CERT_FILE (set by cacert's setup hook)
    # over SSL_CERT_FILE, which the TLS tests point at their own test CA.
    preCheck = ''
      unset NIX_SSL_CERT_FILE
    '';
    meta = {
      description = "Python storage client";
      homepage = "https://github.com/rob-c/${name}";
      license = pkgs.lib.licenses.lgpl3Plus;
      platforms = pkgs.lib.platforms.unix;
    };
  } // extra);
  xrdcore = make "xrdcore" xrdcoreSrc [ ] {
    # Hardware-assisted CRC-32C; the tests cover the pure-Python path too.
    optional-dependencies.fast = [ py.google-crc32c ];
  };
  # The libraries' import-boundary tests (tests/test_package.py) start
  # `python -I` children; they hand the child the installed packages'
  # locations and the parent's sys.path, so they run here too, with every
  # dependency on PYTHONPATH.
  xrdsec = make "xrdsec" xrdsecSrc [ xrdcore py.asn1crypto py.cryptography ] { };
  # botocore is xrdhttp's ``s3`` extra: the storage clients ask for it, xrdarc
  # does not. The suite tests xrdhttp.s3 too, and its TLS socket-pair tests
  # make a throwaway certificate (both from xrdhttp's dev extra).
  xrdhttp = make "xrdhttp" xrdhttpSrc [ xrdcore ] {
    optional-dependencies.s3 = [ py.botocore ];
    nativeCheckInputs = with py; [ pytestCheckHook pytest-timeout pytest-xdist pytest-cov cryptography botocore ];
  };
  libraries = [ xrdcore xrdsec xrdhttp ];
  # ``xrdhttp[s3]``, as both clients' pyprojects require it.
  s3 = xrdhttp.optional-dependencies.s3;
  xrdclient = make "xrdclient" xrdclientSrc (libraries ++ s3 ++ [ py.cryptography ]) { };
  xgfalclient = make "xgfalclient" xgfalclientSrc
    ([ xrdclient ] ++ libraries ++ s3 ++ (with py; [ bcrypt cryptography ])) { };
  # xrdarc's A-REX transport is xrdhttp's session (no urllib3 since 0.4.0).
  xrdarc = make "xrdarc" xrdarcSrc [ xrdcore xrdsec xrdhttp py.cryptography ] { };
  # Every command a user of each package tries first, and what it must print.
  versionChecks = [
    [ "xrd-fs --version" "xrd-fs ${xrdclient.version}" ]
    [ "xrd-proxy --help" "init" ]
  ] ++ pkgs.lib.optionals (xgfalclientSrc != null) [ [ "gfal-ls --version" "gfal2 2.23.5" ] ]
    ++ pkgs.lib.optionals (xrdarcSrc != null) [ [ "xrdarc --version" "xrdarc ${xrdarc.version}" ] ];
in {
  inherit xrdcore xrdsec xrdhttp xrdclient;
} // pkgs.lib.optionalAttrs (xrdarcSrc != null) {
  inherit xrdarc;
} // pkgs.lib.optionalAttrs (xgfalclientSrc != null) {
  inherit xgfalclient;
  nixosTest = pkgs.testers.runNixOSTest {
    name = "storage-clients";
    nodes.machine = { ... }: {
      environment.systemPackages = [ xrdclient xgfalclient ]
        ++ pkgs.lib.optionals (xrdarcSrc != null) [ xrdarc ];
    };
    testScript = ''
      import json
      start_all()
      machine.wait_for_unit("multi-user.target")
      machine.succeed("printf 'physics data' > /tmp/source")
      for tool in ("xrd-cp", "gfal-copy"):
          report = json.loads(machine.succeed(f"{tool} --output-format json /tmp/source /tmp/{tool}"))
          assert report["summary"]["ok"]
          machine.succeed(f"cmp /tmp/source /tmp/{tool}")
      for tool in ("xrd-fs", "gfal-ls"${pkgs.lib.optionalString (xrdarcSrc != null) ", \"xrdarc\""}):
          report = json.loads(machine.succeed(f"{tool} --output-format json --help"))
          assert report["summary"]["exit_code"] == 0
          machine.succeed(f"{tool} --output-format xml --help | grep 'schema=\"storage-client-report\"'")
      for command, expected in ${builtins.toJSON versionChecks}:
          output = machine.succeed(f"{command} 2>&1")
          assert expected in output, (command, output)
    '';
  };
}
