#!/usr/bin/env python3
"""Vendor the pyvark client into the JupyterLite contents.

JupyterLite's Pyodide kernel imports ``vark`` from the contents folder
(``frontend/jupyter/jupyter/vark/``), so the notebooks need no PyPI install
of the client. This script copies ``vark/*.py`` from a pyvark checkout over
that folder, unchanged, and records what was copied in
``frontend/jupyter/vendored_pyvark.json`` (version, commit, sha256 per file).
``tests/test_jupyterlite_static.py`` fails when the vendored files and that
record disagree, or when the record does not match the client's
``__version__``.

Input:  a pyvark checkout (default ``../pyvark`` next to this repo) with a
        clean ``vark/`` folder.
Output: ``frontend/jupyter/jupyter/vark/*.py`` and
        ``frontend/jupyter/vendored_pyvark.json``.

Usage::

    make vendor-pyvark PYVARK=/path/to/pyvark
    python3 frontend/jupyter/vendor_pyvark.py /path/to/pyvark
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENDOR_DIR = HERE / "jupyter" / "vark"
RECORD = HERE / "vendored_pyvark.json"
SOURCE_URL = "https://codeberg.org/mfiers/pyvark"


def file_sha256(path: Path) -> str:
    """Return the hex sha256 of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_version(init_py: Path) -> str:
    """Return ``__version__`` from a vark ``__init__.py``."""
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']',
                      init_py.read_text(), re.M)
    if not match:
        raise SystemExit(f"no __version__ in {init_py}")
    return match.group(1)


def git_output(checkout: Path, *args: str) -> str:
    """Run git in ``checkout`` and return stripped stdout."""
    return subprocess.run(["git", "-C", str(checkout), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def main(argv: list[str]) -> int:
    checkout = Path(argv[1] if len(argv) > 1 else HERE.parents[2] / "pyvark").resolve()
    source_dir = checkout / "vark"
    if not (source_dir / "__init__.py").is_file():
        raise SystemExit(f"{source_dir} is not a pyvark vark/ package")
    dirty = git_output(checkout, "status", "--porcelain", "--", "vark")
    if dirty:
        raise SystemExit(f"{source_dir} has uncommitted changes; commit them first:\n{dirty}")
    commit = git_output(checkout, "rev-parse", "HEAD")
    sources = sorted(source_dir.glob("*.py"))

    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    for stale in VENDOR_DIR.glob("*.py"):
        stale.unlink()
    shutil.rmtree(VENDOR_DIR / "__pycache__", ignore_errors=True)
    for source in sources:
        shutil.copy2(source, VENDOR_DIR / source.name)

    record = {
        "package": "pyvark",
        "source": SOURCE_URL,
        "version": read_version(VENDOR_DIR / "__init__.py"),
        "commit": commit,
        "files": {path.name: file_sha256(VENDOR_DIR / path.name) for path in sources},
    }
    RECORD.write_text(json.dumps(record, indent=2) + "\n")
    print(f"vendored pyvark {record['version']} ({commit[:7]}): "
          f"{len(sources)} files -> {VENDOR_DIR.relative_to(HERE.parents[1])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
