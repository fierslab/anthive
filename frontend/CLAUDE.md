# frontend/ — secondary web surfaces

The "main" web UI is no longer here.  After the 2026-06-07 reorg, the
server-rendered Jinja browser was promoted to a **top-level seam** at
`./web/` (router + templates + launcher all under one folder, scoped by
`web/CLAUDE.md`).  What lives in `frontend/` now is the two ancillary
surfaces:

| Folder | What | Talks to | Driven by |
| --- | --- | --- | --- |
| `streamlit/` | Streamlit app (legacy / opt-in). Removed from the production stack — `/streamlit` on the reverse proxy returns 410.  Source kept for ad-hoc runs via `make serve-streamlit`. | REST API via `antclient` | `streamlit run streamlit_app.py` (manual only) |
| `jupyter/` | JupyterLite (Pyodide) source — notebooks pre-loaded with the anthive client. Built into `webapp/jupyter/` by `make build-jupyter`. | REST API via `antclient.py` | the static build served by Caddy at `/jupyter/` |

Common pattern: **none of these touch DuckDB or h5ad files directly.**
Everything goes through the REST API. If you find yourself reaching past
that, push the new operation into `bin/api/ant-serve` first.

## Build output

`make build-jupyter` writes to `webapp/jupyter/` (gitignored — build
artifact).  `make build-webapp` also copies `frontend/jupyter/index.html`
to `webapp/index.html`.

The `webapp/` directory is the static-file root Caddy serves at `/`. If
you've never run `make build-webapp`, only the JupyterLite path is empty;
the other routes are reverse-proxied to running services.

## Cross-references

- The main web UI is at **`./web/`** (router + templates + launcher).
  Not in this folder any more.
- `frontend/streamlit/` ↔ `make serve-streamlit` (manual; not in the
  supervisor's default service set).
- `frontend/jupyter/` ↔ `bin/ops/dev-jupyter` (dev loop) +
  `make build-jupyter` (production build).
