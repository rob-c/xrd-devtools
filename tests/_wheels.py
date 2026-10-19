"""Synthetic wheels: just the ``.dist-info`` the packaging tools read."""

from __future__ import annotations

import zipfile
from collections.abc import Sequence
from pathlib import Path


def write_wheel(
    folder: Path,
    name: str,
    version: str,
    scripts: Sequence[str] = (),
    requires: Sequence[str] = (),
) -> Path:
    """A minimal wheel: METADATA and, only when it has commands, entry_points.txt."""
    path = folder / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        prefix = f"{name}-{version}.dist-info/"
        lines = [f"Name: {name}", f"Version: {version}", *(f"Requires-Dist: {r}" for r in requires)]
        archive.writestr(prefix + "METADATA", "\n".join(lines) + "\n")
        if scripts:
            points = "".join(f"{script} = cli:main\n" for script in scripts)
            archive.writestr(prefix + "entry_points.txt", "[console_scripts]\n" + points)
    return path


#: Each chain package's commands and default requirements, as its wheel declares them.
CHAIN = {
    "xrdcore": ([], []),
    "xrdsec": ([], ["xrdcore<0.5,>=0.4.0", "cryptography>=45"]),
    "xrdhttp": ([], ["xrdcore<0.5,>=0.4.0", "botocore>=1.35; extra == 's3'"]),
    "xrdclient": (
        ["xrd-cp"],
        ["xrdcore[fast]<0.5,>=0.4.0", "xrdsec<0.5,>=0.4.0", "xrdhttp[s3]<0.5,>=0.4.0"],
    ),
    "xgfalclient": (["gfal-copy"], ["xrdclient==0.4.0", "bcrypt>=4"]),
    "xrdarc": (["xrdarc"], ["xrdsec<0.5,>=0.4.0", "xrdhttp<0.5,>=0.4.0"]),
}
#: Third-party wheels of the chain's resolved wheelhouse.
THIRD_PARTY = {
    "cryptography": ["cffi>=2; platform_python_implementation != 'PyPy'"],
    "cffi": [],
    "botocore": ["python-dateutil<3,>=2.1", "urllib3<3,>=1.25.4", "awscrt==0.27; extra == 'crt'"],
    "python_dateutil": [],
    "bcrypt": [],
    "urllib3": [],
    "google_crc32c": [],
}


def chain_wheelhouse(folder: Path) -> Path:
    """The whole chain's resolved wheelhouse (:data:`CHAIN`, xrdarc included, and
    :data:`THIRD_PARTY`) written into ``folder``, which is returned.

    xrdcore's wheel carries its ``fast`` extra's conditional requirement, so a
    closure must follow an extra only when a requirement asks for it.
    """
    folder.mkdir(parents=True)
    for name, (scripts, requires) in CHAIN.items():
        write_wheel(folder, name, "0.4.0", scripts, requires)
    for name, requires in THIRD_PARTY.items():
        write_wheel(
            folder, name, "2.9.0.post0" if name == "python_dateutil" else "1.0.0", (), requires
        )
    write_wheel(folder, "xrdcore", "0.4.0", (), ["google-crc32c>=1.5; extra == 'fast'"])
    return folder
