# frontend/jupyter/ — JupyterLite examples (sources)

Sources for the JupyterLite static deployment served at `/jupyter/`.
`make build-jupyter` copies them into `webapp/jupyter/` for distribution
(the JupyterLite build artifact, gitignored).

Interactive plotting (gene / UMAP / category / numerical) is **not**
in this directory — it now lives in:

- The **in-app plot UI** in `web/templates/plot.html`
  (reached via `/browse/<group>/<file>/plot/<geom>`).

The old marimo prototype was removed 2026-06; the legacy
`frontend/streamlit/` Streamlit dashboards were removed 2026-06-22.

## Layout

- `jupyter/` — example notebooks (`Getting_Started.ipynb`,
  `examples/01_umap.ipynb`, …)
- `jupyter/vark/` — **vendored** copy of the Python REST client
  ([`codeberg.org/mfiers/pyvark`](https://codeberg.org/mfiers/pyvark)).
  Pyodide picks it up as a top-level package — `from vark import
  AnthiveClient` works without a `micropip.install('pyvark')` step.
  Bump the client by copying the latest `vark/*.py` from a pyvark
  checkout over this directory.

## Everything runs in the browser via Pyodide

There is no Python process on the server for these notebooks. Deps install
client-side with `micropip` (only scientific Python stack — pandas,
matplotlib, seaborn — since `vark` is vendored locally); API calls go
out over the browser fetch path.

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
