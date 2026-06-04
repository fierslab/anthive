# antclient/ — standalone REST client

A pip-installable Python client for the anthive REST API. **No `lib/`
dependency** — this is the only piece structured for extraction into its own
repo / PyPI package.

## Layout
- `antclient.py` — `AnthiveClient` (the whole client)
- `pyproject.toml` + `setup.py` — packaging
- `__init__.py` — re-exports

## Things that matter

- **Base-URL normalisation**: the constructor `rstrip('/')`s the base URL, and
  request paths carry a leading `/`, so both `…/api` and `…/api/` work. In the
  browser it auto-detects `f"{location.origin}/api/"`.
- **Dual environment**: runs both as a normal pip package (uses `requests`) and
  inside Pyodide/WASM in the browser (installed via `micropip`, uses the
  emscripten fetch path). Keep new code compatible with both — avoid
  hard `requests`-only assumptions on import.
- **NaN handling**: see `doc/ANTCLIENT_NAN_HANDLING.md` — JSON null ↔ NaN
  conversions are deliberate; don't "simplify" them.

## Consumers
The `frontend/jupyter/jupyter/` notebooks import this client (via micropip) to talk
to ant-serve. Changes to method signatures here affect those notebooks. The
server-side `frontend/streamlit/` app also uses it for interactive plots.

## When extracting to its own repo
This is the clean candidate: copy `antclient/`, keep `pyproject.toml`, publish.
The server side need not change (it's a REST contract). Coordinate the API
version it targets via `doc/API_METADATA.md`.
