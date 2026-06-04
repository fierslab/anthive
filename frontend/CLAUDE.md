# frontend/ — user-facing web surfaces

Three independent UIs onto the anthive REST API, all of them clients of
`bin/api/ant-serve` (port 8080).

| Folder | What | Talks to | Driven by |
| --- | --- | --- | --- |
| `browser/` | Server-rendered FastAPI + Jinja2 catalog + dataset pages + in-app plot UI. The "main" web UI. | `/databases`, `/databases/{id}/info`, `/databases/{id}/plot`, `/genesets` | `bin/web/ant-browse` |
| `streamlit/` | Streamlit app for exploratory custom dashboards. | REST API via `antclient` | `streamlit run streamlit_app.py` (started by supervisor or `make serve-streamlit`) |
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

- `frontend/browser/` ↔ `bin/web/` (router lives in `bin/web/ant-browse`;
  templates + CSS + JS live here).
- `frontend/streamlit/` ↔ `bin/ops/streamlit-serve` (standalone runner) +
  `ant-supervisor` (production launcher).
- `frontend/jupyter/` ↔ `bin/ops/dev-jupyter` (dev loop) +
  `make build-jupyter` (production build).
