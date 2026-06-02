# webapp-src/ — JupyterLite examples (sources)

Sources for the JupyterLite static deployment served at `/jupyter/`.
The build copies them into `webapp/jupyter/` for distribution.

Interactive plotting (gene / UMAP / category / numerical) is **not**
in this directory anymore — it lives in `webapp-streamlit/` and is
reached via `/streamlit/`. The old marimo prototype was removed
2026-06.

## Layout
- `jupyter/` — example notebooks (`Getting_Started.ipynb`,
  `examples/01_umap.ipynb`, …) plus a vendored copy of `antclient.py`
  pinned to the API contract these notebooks expect.

## Everything runs in the browser via Pyodide

There is no Python process on the server for these notebooks. Deps install
client-side with `micropip`; API calls go out over the browser fetch path.

- **Streaming fetch must be ready before API calls.** urllib3's emscripten
  backend falls back to XHR (truncates responses ~56 KB → "Unterminated
  string" JSON errors) until the streaming service worker initialises. If
  you write a new notebook, do an `await wait_for_streaming_ready()` with
  a short timeout before any API call.
- API URL is auto-detected as `f"{js.location.origin}/api/"` (trailing
  slash — a bare `/api` 307-redirects and POSTs don't follow safely).

## Build

`make build-jupyter` runs `jupyter-lite build` and writes the static site
into `webapp/jupyter/`. Edits made in the browser are persisted to that
browser's local storage only; they are NOT saved back to the server.
