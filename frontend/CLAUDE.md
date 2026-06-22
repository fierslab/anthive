# frontend/ — secondary web surfaces

The "main" web UI is no longer here.  After the 2026-06-07 reorg, the
server-rendered Jinja browser was promoted to a **top-level seam** at
`./web/` (router + templates + launcher all under one folder, scoped by
`web/CLAUDE.md`).  What lives in `frontend/` now is one ancillary surface:

| Folder | What | Talks to | Driven by |
| --- | --- | --- | --- |
| `jupyter/` | JupyterLite (Pyodide) source — notebooks pre-loaded with the anthive client. Built into `webapp/jupyter/` by `make build-jupyter`. | REST API via the vendored `vark/` package ([`codeberg.org/mfiers/pyvark`](https://codeberg.org/mfiers/pyvark)). | the static build served by Caddy at `/jupyter/` |

The legacy `frontend/streamlit/` dashboard was removed 2026-06-22 —
ant-browse covers the same plots (per-dataset, autocompleting, URL-
shareable). `/streamlit` on the reverse proxy continues to return 410
for anyone with a bookmark.

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
- `frontend/jupyter/` ↔ `bin/ops/dev-jupyter` (dev loop) +
  `make build-jupyter` (production build).
