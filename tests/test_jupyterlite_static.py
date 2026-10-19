"""Fast checks of the JupyterLite sources and build (part of ``make test``).

No browser, no network. The browser end-to-end check is
``tests/test_jupyterlite_e2e.py`` (``make test-jupyter``); the deployed-site
check is ``tests/live/test_site_jupyterlite.py`` (``make test-live-site``).

Checked here:

* the vendored client ``frontend/jupyter/jupyter/vark/`` matches the record
  ``frontend/jupyter/vendored_pyvark.json`` written by
  ``frontend/jupyter/vendor_pyvark.py`` (same files, same sha256, same
  version), so a hand edit or a partial copy fails;
* when a pyvark checkout is found (``PYVARK_DIR``, else ``../pyvark``), its
  version equals the vendored one: with ``PYVARK_DIR`` set a difference
  fails, with the auto-detected sibling it is a warning;
* the vendored version satisfies the server's ``API_COMPATIBLE_PYVARK`` and
  its ``API_TARGET`` is not newer than ``API_VERSION``;
* every shipped notebook validates with nbformat, uses JupyterLite's
  ``python`` kernel, compiles (top-level ``await`` allowed) and, when it
  imports vark, installs ``requests`` first (Pyodide has no requests
  preinstalled; without it ``import vark`` fails);
* the build pins in the Makefile and ``docker/Dockerfile`` agree;
* a JupyterLite build, if one exists (``ANTHIVE_JUPYTER_BUILD``, else
  ``webapp``): its ``jupyter-lite.json`` files parse and name the Pyodide
  kernel; a build older than the sources is skipped with a note.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import warnings
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
JUPYTER_DIR = REPO / "frontend" / "jupyter"
CONTENTS = JUPYTER_DIR / "jupyter"
VENDOR_DIR = CONTENTS / "vark"
RECORD_PATH = JUPYTER_DIR / "vendored_pyvark.json"
SERVE = REPO / "bin" / "api" / "ant-serve"


def _version_of(init_py: Path) -> str:
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', init_py.read_text(), re.M)
    assert match, f"no __version__ in {init_py}"
    return match.group(1)


def _version_tuple(text: str) -> tuple:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def _record() -> dict:
    return json.loads(RECORD_PATH.read_text())


def _shipped_notebooks():
    return sorted(path for path in CONTENTS.rglob("*.ipynb")
                  if ".ipynb_checkpoints" not in path.parts)


# ── vendored client ──────────────────────────────────────────────────────

def test_vendored_files_match_record():
    record = _record()
    on_disk = {path.name for path in VENDOR_DIR.glob("*.py")}
    assert on_disk == set(record["files"]), (
        f"vendored files {sorted(on_disk)} differ from the record {sorted(record['files'])}; "
        f"re-vendor with `make vendor-pyvark`")
    for name, expected_sha in record["files"].items():
        actual_sha = hashlib.sha256((VENDOR_DIR / name).read_bytes()).hexdigest()
        assert actual_sha == expected_sha, (
            f"vark/{name} was changed after vendoring (sha256 differs from "
            f"vendored_pyvark.json); the vendored copy must equal pyvark {record['version']} "
            f"at {record['commit'][:7]}. Re-vendor with `make vendor-pyvark`")


def test_vendored_version_matches_record():
    record = _record()
    assert record["package"] == "pyvark"
    assert re.fullmatch(r"[0-9a-f]{40}", record["commit"]), record["commit"]
    assert _version_of(VENDOR_DIR / "__init__.py") == record["version"]


def test_pyvark_checkout_has_not_moved_on():
    explicit = os.environ.get("PYVARK_DIR")
    checkout = Path(explicit) if explicit else REPO.parent / "pyvark"
    init_py = checkout / "vark" / "__init__.py"
    if not init_py.is_file():
        pytest.skip(f"no pyvark checkout at {checkout} (set PYVARK_DIR to compare)")
    checkout_version = _version_of(init_py)
    vendored_version = _record()["version"]
    if checkout_version == vendored_version:
        return
    message = (f"pyvark checkout {checkout} is at {checkout_version}, JupyterLite vendors "
               f"{vendored_version}: run `make vendor-pyvark PYVARK={checkout}`")
    if explicit:
        pytest.fail(message)
    warnings.warn(message)


def test_vendored_client_is_compatible_with_this_server():
    source = SERVE.read_text()
    api_version = re.search(r'^API_VERSION = "([^"]+)"', source, re.M).group(1)
    compatible = re.search(r'^API_COMPATIBLE_PYVARK = "([^"]+)"', source, re.M).group(1)
    vendored = _record()["version"]
    for clause in compatible.split(","):
        match = re.fullmatch(r"\s*>=\s*([\d.]+)\s*", clause)
        assert match, f"unsupported API_COMPATIBLE_PYVARK clause {clause!r}"
        assert _version_tuple(vendored) >= _version_tuple(match.group(1)), (
            f"vendored pyvark {vendored} does not satisfy API_COMPATIBLE_PYVARK {compatible}")
    client_source = (VENDOR_DIR / "client.py").read_text()
    target = re.search(r'^\s+API_TARGET = "([^"]+)"', client_source, re.M).group(1)
    assert _version_tuple(target) <= _version_tuple(api_version), (
        f"vendored pyvark targets API {target}, newer than this server's {api_version}")


# ── notebooks ────────────────────────────────────────────────────────────

def test_notebooks_are_shipped():
    assert _shipped_notebooks(), f"no notebooks in {CONTENTS}"


@pytest.mark.parametrize("notebook", _shipped_notebooks(), ids=lambda path: path.name)
def test_notebook_is_valid(notebook):
    nbformat = pytest.importorskip("nbformat")
    raw = json.loads(notebook.read_text())
    for index, cell in enumerate(raw["cells"]):
        assert cell.get("id"), f"{notebook.name} cell {index} has no id (nbformat 4.5 needs one)"
    parsed = nbformat.reads(notebook.read_text(), as_version=4)
    nbformat.validate(parsed)
    kernel_name = parsed.metadata.get("kernelspec", {}).get("name")
    assert kernel_name == "python", (
        f"{notebook.name}: kernelspec {kernel_name!r}; JupyterLite's Pyodide kernel is 'python'")


@pytest.mark.parametrize("notebook", _shipped_notebooks(), ids=lambda path: path.name)
def test_notebook_code_compiles_and_installs_requests(notebook):
    cells = json.loads(notebook.read_text())["cells"]
    code = ["".join(cell["source"]) if isinstance(cell["source"], list) else cell["source"]
            for cell in cells if cell["cell_type"] == "code"]
    for index, source in enumerate(code):
        compile(source, f"{notebook.name}[code {index + 1}]", "exec",
                flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    joined = "\n".join(code)
    if re.search(r"^\s*(from vark import|import vark)", joined, re.M):
        install = re.search(r"micropip\.install\(\s*\[([^\]]*)\]", joined)
        assert install and re.search(r"['\"]requests['\"]", install.group(1)), (
            f"{notebook.name} imports vark but does not micropip-install 'requests' first; "
            f"Pyodide has no requests preinstalled")


# ── build pins + built site ──────────────────────────────────────────────

def _build_pins(text: str) -> list:
    return sorted(set(re.findall(r"--(?:from|with) '?([A-Za-z0-9_.=-]+)'?", text)))


def test_makefile_and_dockerfile_build_pins_agree():
    makefile = (REPO / "Makefile").read_text()
    block = makefile[makefile.index("build-jupyter:\n"):makefile.index("build-webapp:")]
    dockerfile = (REPO / "docker" / "Dockerfile").read_text()
    docker_block = dockerfile[dockerfile.index("FROM ${PYTHON_IMAGE} AS jupyter"):]
    docker_block = docker_block[:docker_block.index("\nFROM ")]
    assert _build_pins(block) == _build_pins(docker_block), (
        f"Makefile pins {_build_pins(block)} != docker/Dockerfile pins {_build_pins(docker_block)}")


def _build_dir() -> Path:
    return Path(os.environ.get("ANTHIVE_JUPYTER_BUILD", REPO / "webapp")) / "jupyter"


def test_built_site_config_is_valid():
    build = _build_dir()
    if not (build / "jupyter-lite.json").is_file():
        pytest.skip(f"no JupyterLite build in {build} (make build-jupyter)")
    configs = sorted(build.glob("**/jupyter-lite.json"))
    configs = [path for path in configs if "files" not in path.relative_to(build).parts]
    assert configs, "no jupyter-lite.json in the build"
    for path in configs:
        data = json.loads(path.read_text())
        assert "jupyter-config-data" in data, f"{path} has no jupyter-config-data"
    root = json.loads((build / "jupyter-lite.json").read_text())["jupyter-config-data"]
    extensions = {entry["name"] for entry in root.get("federated_extensions", [])}
    for required in ("@jupyterlite/pyodide-kernel-extension", "jupyterlab-open-url-parameter"):
        assert required in extensions, f"{required} missing from the build ({sorted(extensions)})"
    assert (build / "lab" / "index.html").is_file()


def test_built_site_is_current():
    build = _build_dir()
    built_init = build / "files" / "vark" / "__init__.py"
    if not built_init.is_file():
        pytest.skip(f"no JupyterLite build in {build} (make build-jupyter)")
    built_version = _version_of(built_init)
    vendored = _record()["version"]
    stale = [path.relative_to(CONTENTS).as_posix()
             for path in list(CONTENTS.rglob("*.ipynb")) + list(VENDOR_DIR.glob("*.py"))
             if not (build / "files" / path.relative_to(CONTENTS)).is_file()
             or (build / "files" / path.relative_to(CONTENTS)).read_bytes() != path.read_bytes()]
    if stale:
        pytest.skip(f"the build in {build} is older than the sources (vark {built_version}, "
                    f"sources {vendored}; differing: {', '.join(stale[:5])}): "
                    f"run make build-jupyter")
