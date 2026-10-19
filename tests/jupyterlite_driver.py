"""Headless-Chromium driver for anthive's JupyterLite site.

Shared by ``tests/test_jupyterlite_e2e.py`` (a local build behind a local
reverse proxy, ``make test-jupyter``) and
``tests/live/test_site_jupyterlite.py`` (a deployed site,
``make test-live-site``). It is a helper module, not a test file.

How it drives JupyterLite: every request for ``jupyter-lite.json`` is
intercepted and ``exposeAppInBrowser`` is switched on in the answer, so the
page publishes the JupyterLab application object as ``window.jupyterapp``.
The served site itself is not changed. Through that object the driver opens
notebooks, starts Pyodide kernels and runs code with
``kernel.requestExecute``, collecting stdout, stderr, the result text and
any error from the iopub messages.

Login: ``login_mode`` ``none`` (anonymous), ``basic`` (HTTP credentials on
the browser context) or ``authelia`` (the real Authelia portal form is
filled in). Credentials are never logged.

Needs ``playwright`` and a Chromium for it (``playwright install chromium``);
:func:`browser_unavailable_reason` says why a browser cannot start.
"""
from __future__ import annotations

import base64
import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# JupyterLite pulls Pyodide and its wheels from this CDN at kernel start.
PYODIDE_CDN_PROBE = "https://cdn.jsdelivr.net/pyodide/v0.29.3/full/pyodide.js"

APP_LOAD_TIMEOUT_MS = 90_000
KERNEL_START_TIMEOUT_S = 180
CELL_TIMEOUT_S = 300

_JS_WAIT_APP = """
async () => {
  const app = window.jupyterapp;
  await app.restored;
  await app.serviceManager.ready;
  return true;
}
"""

_JS_KERNELSPEC_READY = """
() => {
  const specs = window.jupyterapp.serviceManager.kernelspecs.specs;
  return !!(specs && specs.kernelspecs && specs.kernelspecs.python);
}
"""

# Run code in a kernel. ``target`` is either {session: path} (a kernel
# session the driver started itself, no notebook widget) or {widget: path}
# (the kernel of an open notebook widget).
_JS_EXECUTE = """
async ({target, code, timeoutMs}) => {
  const app = window.jupyterapp;
  let kernel = null;
  if (target.session) {
    window.__anthiveSessions = window.__anthiveSessions || {};
    let session = window.__anthiveSessions[target.session];
    if (!session) {
      session = await app.serviceManager.sessions.startNew({
        path: target.session, type: 'notebook', name: target.session,
        kernel: {name: 'python'}});
      window.__anthiveSessions[target.session] = session;
    }
    kernel = session.kernel;
  } else {
    const widget = Array.from(app.shell.widgets('main')).find(
      w => w.context && w.context.path === target.widget);
    if (!widget) { return {status: 'error', error: 'no open notebook ' + target.widget}; }
    await widget.sessionContext.ready;
    kernel = widget.sessionContext.session && widget.sessionContext.session.kernel;
    if (!kernel) { return {status: 'error', error: 'notebook has no kernel: ' + target.widget}; }
  }
  const out = {stdout: '', stderr: '', result: null, error: null, traceback: null};
  const future = kernel.requestExecute({code, store_history: false, stop_on_error: true});
  future.onIOPub = (msg) => {
    const kind = msg.header.msg_type, content = msg.content;
    if (kind === 'stream') { out[content.name] += content.text; }
    else if (kind === 'execute_result' || kind === 'display_data') {
      const data = content.data || {};
      out.result = data['text/plain'] || (data['image/png'] ? '<image/png>' : out.result);
    } else if (kind === 'error') {
      out.error = content.ename + ': ' + content.evalue;
      out.traceback = (content.traceback || []).join('\\n');
    }
  };
  const timer = new Promise((resolve) => setTimeout(
    () => resolve({content: {status: 'timeout'}}), timeoutMs));
  const reply = await Promise.race([future.done, timer]);
  out.status = reply.content.status;
  if (out.status === 'timeout') {
    out.error = 'no reply after ' + timeoutMs + ' ms (kernel status ' + kernel.status +
                ', connection ' + kernel.connectionStatus + ')';
  }
  return out;
}
"""

_JS_OPEN_WIDGETS = """
() => {
  const rows = [];
  for (const widget of window.jupyterapp.shell.widgets('main')) {
    if (!widget.context) continue;
    const session = widget.sessionContext && widget.sessionContext.session;
    rows.push({path: widget.context.path,
               kernel: session && session.kernel ? session.kernel.name : null,
               ncells: widget.content && widget.content.model ? widget.content.model.cells.length : null});
  }
  const dialog = document.querySelector('.jp-Dialog');
  return {rows, dialog: dialog ? dialog.innerText.slice(0, 300) : null};
}
"""

_JS_CELL_SOURCES = """
(path) => {
  const widget = Array.from(window.jupyterapp.shell.widgets('main')).find(
    w => w.context && w.context.path === path);
  const cells = widget.content.model.cells;
  const out = [];
  for (let i = 0; i < cells.length; i++) {
    const cell = cells.get(i);
    out.push({type: cell.type, source: cell.sharedModel.getSource()});
  }
  return out;
}
"""

_JS_OPEN_PATH = """
async (path) => {
  await window.jupyterapp.commands.execute('docmanager:open', {path, factory: 'Notebook'});
  return true;
}
"""


@dataclass
class CellResult:
    """Outcome of one executed code cell."""

    status: str
    stdout: str = ""
    stderr: str = ""
    result: Optional[str] = None
    error: Optional[str] = None
    traceback: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def describe(self) -> str:
        """A short, secret-free description for assertion messages."""
        text = f"status={self.status}"
        if self.error:
            text += f" error={self.error}"
        if self.stdout:
            text += f" stdout={self.stdout[-800:]!r}"
        if self.stderr:
            text += f" stderr={self.stderr[-800:]!r}"
        if self.traceback:
            text += "\n" + re.sub(r"\x1b\[[0-9;]*m", "", self.traceback)[-2000:]
        return text


def _module_missing_reason() -> Optional[str]:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return "playwright is not installed (run via `uv run --with playwright`)"
    return None


def browser_unavailable_reason() -> Optional[str]:
    """``None`` when headless Chromium starts, else why it does not."""
    reason = _module_missing_reason()
    if reason:
        return reason
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            browser.close()
    except Exception as exc:  # noqa: BLE001 - any launch failure means skip
        first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc)
        return (f"headless Chromium does not start ({first_line}); "
                f"install it with `uv run --with playwright playwright install chromium`")
    return None


def network_unavailable_reason(probe_url: str = PYODIDE_CDN_PROBE,
                               timeout_s: float = 10.0) -> Optional[str]:
    """``None`` when the Pyodide CDN answers, else why the kernel cannot load."""
    import urllib.request
    try:
        request = urllib.request.Request(probe_url, method="HEAD")
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            if response.status >= 400:
                return f"Pyodide CDN answered {response.status} for {probe_url}"
    except Exception as exc:  # noqa: BLE001
        return f"no network access to the Pyodide CDN ({probe_url}): {exc}"
    return None


def notebook_b64(params: Dict[str, Any]) -> str:
    """The url-safe base64 the web UI uses in ``/api/notebook/{b64}/...``."""
    raw = json.dumps(params).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@dataclass
class JupyterLiteBrowser:
    """One headless browser context on one anthive site.

    Args:
        site_url: scheme + host [+ port] of the site, no trailing slash.
        jupyter_path: where JupyterLite is served (``/jupyter/``).
        login_mode: ``none``, ``basic`` or ``authelia``.
        username / password: credentials for ``basic`` / ``authelia``.
        ignore_https_errors: accept self-signed certificates (``tls internal``).
    """

    site_url: str
    jupyter_path: str = "/jupyter/"
    login_mode: str = "none"
    username: Optional[str] = None
    password: Optional[str] = None
    ignore_https_errors: bool = True
    console_errors: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        from playwright.sync_api import sync_playwright
        self.site_url = self.site_url.rstrip("/")
        if not self.jupyter_path.endswith("/"):
            self.jupyter_path += "/"
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch()
        context_args: Dict[str, Any] = {"ignore_https_errors": self.ignore_https_errors}
        if self.login_mode == "basic":
            context_args["http_credentials"] = {
                "username": self.username or "", "password": self.password or ""}
        self.context = self._browser.new_context(**context_args)
        self.context.route("**/jupyter-lite.json", self._expose_app)
        self.page = self.context.new_page()
        self.page.on("console", self._on_console)

    # ── plumbing ──────────────────────────────────────────────────────────

    def _on_console(self, message: Any) -> None:
        if message.type == "error":
            self.console_errors.append(message.text[:300])

    @staticmethod
    def _expose_app(route: Any) -> None:
        response = route.fetch()
        try:
            data = response.json()
        except Exception:  # noqa: BLE001 - not JSON: pass through untouched
            route.fulfill(response=response)
            return
        data.setdefault("jupyter-config-data", {})["exposeAppInBrowser"] = "true"
        route.fulfill(response=response, body=json.dumps(data))

    def close(self) -> None:
        for closer in (self.context.close, self._browser.close, self._playwright.stop):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass

    # ── login ─────────────────────────────────────────────────────────────

    def login(self) -> None:
        """Sign in through the site's real login (no-op for none/basic)."""
        if self.login_mode != "authelia":
            return
        self.page.goto(self.site_url + self.jupyter_path, wait_until="domcontentloaded")
        username_box = self.page.locator("#username-textfield")
        if username_box.count() == 0:
            username_box.wait_for(timeout=30_000)
        username_box.fill(self.username or "")
        self.page.locator("#password-textfield").fill(self.password or "")
        self.page.locator("#sign-in-button").click()
        # Authelia redirects back to the requested page after a good login.
        self.page.wait_for_url(
            re.compile(re.escape(self.site_url + self.jupyter_path) + ".*"),
            timeout=60_000)

    # ── app + kernels ─────────────────────────────────────────────────────

    def open_lab(self, query: str = "") -> None:
        """Load ``<jupyter_path>lab/index.html`` and wait for the app and
        the Pyodide kernelspec."""
        url = f"{self.site_url}{self.jupyter_path}lab/index.html{query}"
        response = self.page.goto(url, wait_until="domcontentloaded")
        if response is not None and response.status >= 400:
            raise AssertionError(f"{url} answered HTTP {response.status}")
        if "/authelia" in self.page.url:
            raise AssertionError(f"{url} redirected to the login portal (not signed in)")
        self.page.wait_for_function("() => !!(window.jupyterapp && window.jupyterapp.serviceManager)",
                                    timeout=APP_LOAD_TIMEOUT_MS)
        self.page.evaluate(_JS_WAIT_APP)
        self.page.wait_for_function(_JS_KERNELSPEC_READY, timeout=APP_LOAD_TIMEOUT_MS)

    def cross_origin_isolated(self) -> bool:
        return bool(self.page.evaluate("() => self.crossOriginIsolated"))

    def run(self, code: str, session: str = "anthive-e2e.ipynb",
            timeout_s: float = CELL_TIMEOUT_S) -> CellResult:
        """Run ``code`` in a kernel session of our own (started on first use)."""
        return self._execute({"session": session}, code, timeout_s)

    def run_in_notebook(self, path: str, code: str,
                        timeout_s: float = CELL_TIMEOUT_S) -> CellResult:
        """Run ``code`` in the kernel of the open notebook ``path``."""
        return self._execute({"widget": path}, code, timeout_s)

    def _execute(self, target: Dict[str, str], code: str, timeout_s: float) -> CellResult:
        raw = self.page.evaluate(_JS_EXECUTE, {"target": target, "code": code,
                                               "timeoutMs": int(timeout_s * 1000)})
        return CellResult(status=raw.get("status", "error"), stdout=raw.get("stdout", ""),
                          stderr=raw.get("stderr", ""), result=raw.get("result"),
                          error=raw.get("error"), traceback=raw.get("traceback"))

    def simulate_warming_up(self, url_glob: str, times: int = 1,
                            retry_after: int = 1) -> Dict[str, int]:
        """Answer the next ``times`` requests matching ``url_glob`` with the
        REST API's 503 ``warming_up`` body (API 2.46.1+), then let requests
        through. Exercises the client's retry path inside Pyodide without
        restarting a server. Returns live counters ``{'503': n, 'passed': n}``."""
        counters = {"503": 0, "passed": 0}

        def handler(route: Any) -> None:
            if counters["503"] < times:
                counters["503"] += 1
                route.fulfill(status=503, content_type="application/json",
                              headers={"Retry-After": str(retry_after),
                                       "Cross-Origin-Resource-Policy": "same-origin"},
                              body=json.dumps({"status": "warming_up",
                                               "retry_after": retry_after}))
                return
            counters["passed"] += 1
            route.continue_()

        self.context.route(url_glob, handler)
        return counters

    # ── notebooks ─────────────────────────────────────────────────────────

    def open_notebook(self, path: str) -> None:
        """Open a notebook from the site's contents (``Getting_Started.ipynb``)."""
        self.page.evaluate(_JS_OPEN_PATH, path)
        self.wait_for_notebook_kernel(path)

    def open_from_url(self, notebook_url: str, expected_name: str) -> str:
        """Open the lab with ``?fromURL=`` (the web UI's notebook buttons)
        and wait for the notebook's kernel. Returns its in-browser path."""
        self.open_lab("?fromURL=" + urllib.parse.quote(notebook_url, safe=""))
        return self.wait_for_notebook_kernel(expected_name)

    def wait_for_notebook_kernel(self, path: str,
                                 timeout_s: float = KERNEL_START_TIMEOUT_S) -> str:
        deadline = time.monotonic() + timeout_s
        state: Dict[str, Any] = {}
        while time.monotonic() < deadline:
            state = self.page.evaluate(_JS_OPEN_WIDGETS)
            for row in state["rows"]:
                if row["path"].split("/")[-1] == path.split("/")[-1] and row["kernel"]:
                    return row["path"]
            if state["dialog"]:
                raise AssertionError(f"a dialog blocks {path}: {state['dialog']!r}")
            time.sleep(1)
        raise AssertionError(f"{path} did not open with a running kernel within "
                             f"{timeout_s:.0f} s; open widgets: {state.get('rows')}")

    def notebook_cells(self, path: str) -> List[Dict[str, str]]:
        """``[{type, source}]`` of the open notebook ``path``."""
        return self.page.evaluate(_JS_CELL_SOURCES, path)

    def run_first_code_cells(self, path: str, count: int,
                             timeout_s: float = CELL_TIMEOUT_S) -> List[CellResult]:
        """Run the first ``count`` code cells of an open notebook, stopping
        at the first failure. Returns the results so far."""
        results: List[CellResult] = []
        code_cells = [cell for cell in self.notebook_cells(path) if cell["type"] == "code"]
        for index, cell in enumerate(code_cells[:count]):
            result = self.run_in_notebook(path, cell["source"], timeout_s)
            if not result.ok:
                result.error = f"code cell {index + 1} of {path}: {result.error}"
            results.append(result)
            if not result.ok:
                break
        return results


# Code snippets run in the kernel. They mirror what the shipped notebooks do:
# install requests (vark's HTTP layer) and pandas, import the vendored vark, connect to
# the same-origin /api/.
SETUP_CODE = """\
import sys
if 'pyodide' in sys.modules:
    import micropip
    await micropip.install(['requests', 'pandas'])
import vark
print(vark.__version__)
print(vark.__file__)
"""

CONNECT_CODE = """\
import js, json
from vark import AnthiveClient
client = AnthiveClient(f"{js.location.origin}/api/")
health = client.get_health()
version = client.get_version()
databases = client.get_databases()
first_gene = client.search_genes(databases[0]['id'], q='', limit=1)[0] if databases else None
print(json.dumps({
    'base_url': client.base_url,
    'health_status': health.get('status'),
    'api_version': version.get('version'),
    'compatible_pyvark': version.get('compatible_pyvark'),
    'n_databases': len(databases),
    'ids': [row.get('id') for row in databases],
    'access': sorted({str(row.get('access')) for row in databases}),
    'non_public_ids': [row.get('id') for row in databases if row.get('access') != 'public'],
    'first_gene': first_gene,
}))
"""

# Fills DB_ID from the first dataset (if any) and runs one expression query
# and one plot request.
QUERY_CODE = """\
import json
first = databases[0]['id']
gene = client.search_genes(first, q='', limit=1)[0]
cells = client.get_cells(first, genes=[gene], format='dataframe')
plot = client.get_plot(first, 'histogram', x=gene, sample=2000)
print(json.dumps({'db_id': first, 'gene': gene, 'n_cells': int(len(cells)),
                  'plot_format': plot.get('format'), 'plot_mime': plot.get('mime'),
                  'plot_bytes': len(plot.get('bytes') or b''),
                  'png_magic': (plot.get('bytes') or b'')[:4] == b'\\x89PNG'}))
"""


def parse_last_json(stdout: str) -> Dict[str, Any]:
    """The last line of ``stdout`` that parses as a JSON object."""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    raise AssertionError(f"no JSON object in kernel output: {stdout[-500:]!r}")


# Run after CONNECT_CODE with simulate_warming_up() on the /api/version URL:
# the client must sleep and retry, then return the real answer.
WARMUP_CODE = """\
import json, time, warnings
client.warmup_max_wait = 30
started = time.monotonic()
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter('always')
    answer = client.get_version()
print(json.dumps({'version': answer.get('version'),
                  'waited_s': round(time.monotonic() - started, 2),
                  'warnings': [type(item.message).__name__ for item in caught]}))
"""
