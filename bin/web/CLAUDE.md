# bin/web/ — server-rendered web UI

| Tool | Role |
| --- | --- |
| `ant-browse` | FastAPI + Jinja2 server (port 8081) that renders the dataset catalog, dataset pages, and the in-app plot UI. It is a **client** of `bin/api/ant-serve`. |

## Architecture

`ant-browse` does not touch any DuckDB or .h5ad file directly. It calls
`bin/api/ant-serve`'s REST endpoints (e.g. `/databases`, `/databases/{id}/info`,
`/databases/{id}/plot`) and feeds the responses into templates.

Template + static asset source lives in `frontend/browser/`:
- `browser.py` — the FastAPI router (mounted at `/`)
- `templates/` — Jinja2 templates (`base.html`, `index.html`, `folder.html`,
  `dataset.html`, `plot.html`, `genesets*.html`)
- `static/` — CSS, JS, fonts

`ant-browse` imports `frontend/browser/browser.py` dynamically (the dash in
the folder name → `importlib.util.spec_from_file_location`).

## What you'll commonly edit here vs in frontend/browser/

- **Routes, response shape, error handling, plot-form context** → `bin/web/ant-browse`
- **HTML, CSS, JS, page layout** → `frontend/browser/templates/`, `frontend/browser/static/`

If you're working on UI only, `cd frontend/browser/` and the
`frontend/browser/CLAUDE.md` will scope a Claude session to just that layer.

## Hosts / ports

Default bind: `127.0.0.1:8081`. Override via `anthive.yaml`:
```yaml
browse_host: 0.0.0.0
browse_port: 8081
```
