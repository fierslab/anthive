# frontend/jupyter/ (JupyterLite sources)

Sources for the JupyterLite static deployment served at `/jupyter/`.
`make build-jupyter` copies them into `webapp/jupyter/` for distribution
(the JupyterLite build artifact, gitignored).

Interactive plotting (gene / UMAP / category / numerical) is **not**
in this directory. It now lives in:

- The **in-app plot UI** in `web/templates/plot.html`
  (reached via `/browse/<group>/<file>/plot/<geom>`).

The old marimo prototype was removed 2026-06; the legacy
`frontend/streamlit/` Streamlit dashboards were removed 2026-06-22.

## Layout

- `jupyter/` is the contents folder. It holds `Getting_Started.ipynb`, the
  only shipped notebook; add more here and the tests pick them up.
- `jupyter/vark/` is a **vendored**, unchanged copy of the Python REST client
  ([`codeberg.org/mfiers/pyvark`](https://codeberg.org/mfiers/pyvark)).
  Pyodide imports it from the contents root, so `from vark import
  AnthiveClient` works without a `micropip.install('pyvark')` step (PyPI's
  pyvark lags far behind; never install it in a notebook).
  Bump with `make vendor-pyvark PYVARK=../pyvark`, never by hand: it writes
  `vendored_pyvark.json` (version, commit, sha256) and `make test` fails
  when the files and the record differ.
- `vendor_pyvark.py` is the script behind `make vendor-pyvark`.

## Everything runs in the browser via Pyodide

There is no Python process on the server for these notebooks. Deps install
client-side with `micropip` from the Pyodide distribution; API calls go
out over the browser fetch path.

- **Every notebook that imports vark must install `requests` first**
  (`await micropip.install(['requests', 'pandas', ...])`). Pyodide does not
  preinstall it, and `import vark` fails with `No module named 'requests'`
  otherwise (Getting_Started was broken this way until 2.46.3). The same
  goes for ant-serve's generated notebooks (`_auth_cell` in
  `bin/api/ant-serve`).

- **Streaming fetch must be ready before API calls.** urllib3's emscripten
  backend falls back to XHR (truncates responses ~56 KB → "Unterminated
  string" JSON errors) until the streaming service worker initialises. If
  you write a new notebook, give it a beat (`await asyncio.sleep(0.5)`, as
  Getting_Started does) before the first large API call.
- API URL is auto-detected as `f"{js.location.origin}/api/"` (trailing
  slash: a bare `/api` 307-redirects and POSTs don't follow safely).

## Build

`make build-jupyter` runs `jupyter-lite build` and writes the static site
into `webapp/jupyter/`. Edits made in the browser are persisted to that
browser's local storage only; they are NOT saved back to the server.

## Tests

- `make test` runs `tests/test_jupyterlite_static.py` (vendored files vs
  record, notebooks valid, build pins of Makefile and Dockerfile agree).
- `make test-jupyter` runs headless Chromium end to end against a running
  ant-serve (`tests/test_jupyterlite_e2e.py`, driver
  `tests/jupyterlite_driver.py`, one-origin proxy
  `tests/jupyterlite_proxy.py`).
- `make test-live-site URL=...` does the same on a deployed site through its
  login (`tests/live/test_site_jupyterlite.py`).

Details in `doc/JUPYTERLITE_BUILD.md`.
