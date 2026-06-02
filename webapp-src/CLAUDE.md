# webapp-src/ — in-browser notebooks

Client-side notebooks that talk to ant-serve via `antclient` (no server-side
execution). Two stacks:

- `marimo/` — 4 reactive notebooks (`gene`, `umap`, `category`, `numerical`),
  the streamlit-replacement views. Exported to WASM and served at `/marimo/`.
- `jupyter/` — JupyterLite example notebooks + a vendored `antclient.py`.

## Everything runs in the browser via Pyodide

There is no Python process on the server for these. Deps install client-side
with `micropip`; API calls go out over the browser fetch path.

- **Streaming fetch must be ready before API calls.** urllib3's emscripten
  backend falls back to XHR (truncates responses ~56 KB → "Unterminated
  string" JSON errors) until the streaming service worker initialises. The
  `_packages` cell does `await wait_for_streaming_ready()` with a 5 s timeout
  before any request — keep that pattern.
- API URL is auto-detected as `f"{js.location.origin}/api/"` (trailing slash —
  a bare `/api` 307-redirects and POSTs don't follow it).
- Widget state round-trips to URL query params (`?db_id=…&gene=…`), so any view
  is one copy-paste from sharing.

## marimo cell rules (its static analyser enforces these)
- Each variable defined in exactly one cell. Use `_`-prefixed names for
  cell-private locals to avoid cross-cell collisions (`_values`, `_vals`, …).
- No bare `return` inside an `@app.cell` — marimo strips the function wrapper.
- One widget per cell with the widget as the last expression, so reactivity
  tracks correctly.

## Build
`make build-marimo` exports each notebook twice: `--mode run` (view-only,
streamlit replacement) and `--mode edit` (full editor, persists to browser
localStorage). Output → `webapp/marimo/`. Edits in the browser are NOT saved
back to the server.
