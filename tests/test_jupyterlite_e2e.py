"""End-to-end check of the JupyterLite build in headless Chromium.

Run with ``make test-jupyter`` (slow: it builds JupyterLite, downloads
Pyodide and wheels from the CDN, and needs Chromium). Under a plain
``make test`` the module is skipped.

What it does: serves a JupyterLite build together with a reverse proxy to a
running ant-serve under one origin (``tests/jupyterlite_proxy.py``, the same
``/jupyter/`` and ``/api/`` routes as ``config/Caddyfile``), then in the
browser:

* the lab app loads, is cross-origin isolated, and a Pyodide kernel starts;
* ``import vark`` gives the vendored client, at the version recorded in
  ``frontend/jupyter/vendored_pyvark.json``;
* an ``AnthiveClient`` on ``{origin}/api/`` reads ``/health`` and
  ``/version`` and lists datasets; with a dataset present, one expression
  query and one plot request succeed;
* every notebook shipped in ``frontend/jupyter/jupyter/`` opens and its code
  cells run (``Getting_Started.ipynb`` in full);
* the notebooks the web UI opens through ``?fromURL=`` (the dataset notebook
  and a plot notebook from ant-serve) open, get a kernel and run their
  first code cells, still importing the vendored vark.

Environment (``make test-jupyter`` sets the first two):

* ``ANTHIVE_TEST_JUPYTER=1``  enable the module
* ``ANTHIVE_JUPYTER_BUILD``   directory holding the ``jupyter/`` build
  (default ``webapp``)
* ``ANTHIVE_JUPYTER_API``     ant-serve URL (default ``http://localhost:8080``)

Missing browser, missing network to the Pyodide CDN or an unreachable API
skip with the reason.
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CONTENTS = REPO / "frontend" / "jupyter" / "jupyter"
RECORD = REPO / "frontend" / "jupyter" / "vendored_pyvark.json"

if os.environ.get("ANTHIVE_TEST_JUPYTER") != "1":
    pytest.skip("JupyterLite browser test: run `make test-jupyter` "
                "(sets ANTHIVE_TEST_JUPYTER=1)", allow_module_level=True)

from jupyterlite_driver import (  # noqa: E402
    CONNECT_CODE, QUERY_CODE, SETUP_CODE, WARMUP_CODE, JupyterLiteBrowser,
    browser_unavailable_reason, network_unavailable_reason, notebook_b64,
    parse_last_json)
from jupyterlite_proxy import JupyterLiteTestServer  # noqa: E402

BUILD_ROOT = Path(os.environ.get("ANTHIVE_JUPYTER_BUILD", REPO / "webapp"))
API_URL = os.environ.get("ANTHIVE_JUPYTER_API", "http://localhost:8080").rstrip("/")

pytestmark = pytest.mark.slow


def _api_reachable() -> bool:
    try:
        with urllib.request.urlopen(f"{API_URL}/health", timeout=10) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="module")
def lite():
    if not (BUILD_ROOT / "jupyter" / "jupyter-lite.json").is_file():
        pytest.fail(f"no JupyterLite build in {BUILD_ROOT}/jupyter "
                    f"(make build-jupyter JUPYTER_OUT={BUILD_ROOT}/jupyter)")
    for reason in (browser_unavailable_reason(), network_unavailable_reason()):
        if reason:
            pytest.skip(reason)
    with JupyterLiteTestServer(BUILD_ROOT, API_URL, port=0) as server:
        browser = JupyterLiteBrowser(site_url=server.url)
        try:
            browser.open_lab()
            yield browser
        finally:
            browser.close()


@pytest.fixture(scope="module")
def connected(lite):
    """Kernel with vark imported and ``client``/``databases`` defined."""
    setup = lite.run(SETUP_CODE)
    assert setup.ok, setup.describe()
    if not _api_reachable():
        pytest.skip(f"ant-serve not reachable at {API_URL} (start it or set ANTHIVE_JUPYTER_API)")
    result = lite.run(CONNECT_CODE)
    assert result.ok, result.describe()
    return parse_last_json(result.stdout)


def test_app_is_cross_origin_isolated(lite):
    assert lite.cross_origin_isolated(), "COOP/COEP headers missing: Pyodide needs them"


def test_kernel_imports_vendored_vark(lite):
    result = lite.run(SETUP_CODE)
    assert result.ok, result.describe()
    version, location = result.stdout.strip().splitlines()[-2:]
    expected = json.loads(RECORD.read_text())["version"]
    assert version == expected, f"kernel imports vark {version}, vendored record says {expected}"
    assert location == "/drive/vark/__init__.py", f"vark imported from {location}, not the contents"


def test_client_health_version_and_datasets(connected):
    assert connected["base_url"].endswith("/api"), connected["base_url"]
    assert connected["health_status"] in ("healthy", "ok", "degraded"), connected
    assert connected["api_version"], connected
    assert connected["n_databases"] >= 0


def test_anonymous_view_has_no_internal_datasets(connected):
    """Through the proxy the kernel is an anonymous remote caller: with
    ``internal_requires_auth`` on (the default) no non-public dataset shows."""
    assert connected["non_public_ids"] == [], (
        f"anonymous JupyterLite sees non-public datasets: {connected['non_public_ids'][:5]}")


def test_expression_query_and_plot(lite, connected):
    if connected["n_databases"] == 0:
        pytest.skip("empty catalogue: no dataset to query")
    result = lite.run(QUERY_CODE)
    assert result.ok, result.describe()
    payload = parse_last_json(result.stdout)
    assert payload["n_cells"] > 0, payload
    assert payload["plot_format"] == "png" and payload["png_magic"], payload


def _shipped_notebooks():
    return sorted(path.relative_to(CONTENTS).as_posix()
                  for path in CONTENTS.rglob("*.ipynb")
                  if ".ipynb_checkpoints" not in path.parts)


@pytest.mark.parametrize("notebook", _shipped_notebooks())
def test_shipped_notebook_runs(lite, connected, notebook):
    if connected["n_databases"] == 0:
        pytest.skip("empty catalogue: the shipped notebooks need a dataset")
    lite.open_notebook(notebook)
    code_cells = [cell for cell in lite.notebook_cells(notebook) if cell["type"] == "code"]
    results = lite.run_first_code_cells(notebook, len(code_cells))
    for result in results:
        assert result.ok, result.describe()
    assert len(results) == len(code_cells)


def test_dataset_notebook_from_url(lite, connected):
    """The dataset page's notebook button: ``?fromURL=/api/notebook/{b64}/dataset.ipynb``."""
    if connected["n_databases"] == 0:
        pytest.skip("empty catalogue: no dataset notebook to open")
    db_id = connected["ids"][0]
    url = f"/api/notebook/{notebook_b64({'db_id': db_id})}/dataset.ipynb"
    path = lite.open_from_url(url, "dataset.ipynb")
    results = lite.run_first_code_cells(path, 2)
    for result in results:
        assert result.ok, result.describe()
    check = lite.run_in_notebook(path, "import vark; print(vark.__version__)")
    assert check.ok, check.describe()
    expected = json.loads(RECORD.read_text())["version"]
    assert check.stdout.strip() == expected, (
        f"the generated notebook imports vark {check.stdout.strip()}, not the vendored {expected}")


def test_plot_notebook_from_url(lite, connected):
    """The plot page's notebook button (histogram of the first gene)."""
    if connected["n_databases"] == 0:
        pytest.skip("empty catalogue: no plot notebook to open")
    db_id = connected["ids"][0]
    params = {"db_id": db_id, "geom": "histogram", "x": connected["first_gene"]}
    url = f"/api/notebook/{notebook_b64(params)}/plot.ipynb"
    path = lite.open_from_url(url, "plot.ipynb")
    results = lite.run_first_code_cells(path, 3)
    for result in results:
        assert result.ok, result.describe()


def test_warming_up_answer_is_retried_in_pyodide(lite, connected):
    """A 503 ``warming_up`` (simulated by intercepting the request) is slept
    on and retried by vark inside the Pyodide kernel (time.sleep in a web
    worker), and the call then returns the real answer."""
    lite.open_lab()   # a fresh page: earlier tests may have navigated away
    for code in (SETUP_CODE, CONNECT_CODE):
        prepared = lite.run(code)
        assert prepared.ok, prepared.describe()
    counters = lite.simulate_warming_up("**/api/version", times=1, retry_after=1)
    result = lite.run(WARMUP_CODE)
    assert result.ok, result.describe()
    payload = parse_last_json(result.stdout)
    assert counters == {"503": 1, "passed": 1}, (
        f"vark did not retry the 503 warming_up answer (requests seen: {counters}, "
        f"kernel output: {payload})")
    assert payload["version"] == connected["api_version"], payload
    assert payload["waited_s"] >= 0.9, payload
    assert "ServerWarmingUpWarning" in payload["warnings"], payload
