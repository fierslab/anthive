# web/ — the anthive web UI

Everything for the server-rendered web UI lives here: the FastAPI router,
the Jinja templates, the launcher, the static assets.  This used to be
split across `bin/web/` and `frontend/browser/`; after the 2026-06-07
reorg it's one folder so a single Claude Code session can hold the whole
UI scope at once.

## Layout

```
web/
  ant-browse        # uv-script launcher; supervisor runs this. Port 8081.
  browser.py        # the FastAPI router, mounted at /
  templates/        # Jinja2 templates (one per page + base.html)
    base.html       # shared chrome (nav, footer, storage notice)
    index.html      # landing — collection list with context pills
    folder.html     # one collection — per-dataset table
    dataset.html    # one dataset — info, biology, provenance, obs cols, viz
    plot.html       # in-app plot UI (form + preview + downloads)
    genesets.html, geneset.html, geneset_experiment.html
  __init__.py       # so the directory imports cleanly if ever needed
  README.md         # user-facing overview
```

There is no separate `static/` directory — page-specific CSS and JS sit
inline in the matching `*.html` (the JS budget is small enough that
splitting into separate files would only add HTTP requests).  Sitewide
styles live in `templates/base.html`'s `<style>` block.

## How it boots

`web/ant-browse` is a `uv run --script` PEP-723 shebang.  It:

1. Adds `../lib` to `sys.path` (so it can `from config import get_config`).
2. Imports `browser.py` via `importlib` (the module name `anthive_web_browser`
   is purely internal — chosen to not collide with anything).
3. Creates the FastAPI app, registers the router from `browser.py`, mounts
   it at `/`.
4. Talks to `ant-serve` (port 8080) over HTTP for every data fetch — this
   process never touches DuckDB or h5ad directly.

## Routes (the public contract)

| Path | Renders | Template |
| --- | --- | --- |
| `/` | dataset catalog (grouped by collection) | `index.html` |
| `/browse/{group}` | one collection | `folder.html` |
| `/browse/{group}/{file}` | one dataset (info + viz launcher) | `dataset.html` |
| `/browse/{group}/{file}/plot/{geom}` | interactive plot UI for one geom on one dataset | `plot.html` |
| `/genesets`, `/genesets/{study}`, `/genesets/{study}/{exp}` | geneset browsing | `genesets*.html` |

The plot page talks to the REST API directly from the browser via JS —
the FastAPI router doesn't proxy plot requests.  It just hands the
client a populated form (with curated favourites + smart pseudotime
detection) and lets the client hit `/api/databases/{id}/plot/{geom}`.

## Vocabulary shared across pages

- **Coloured "context" pills** (organism / tech / cell / tissue / disease
  / prep / access / source / year) on landing, folder, and dataset
  pages.  Palette is in `index.html`'s `.ctx-pill`, mirrored as
  `.grp-pill` in `folder.html` and `.ds-ctx .pill` in `dataset.html`.
- **Abbreviation maps** in `browser.py`: `_CELLTYPE_ABBREV`,
  `_TISSUE_ABBREV`, `_DISEASE_ABBREV`, `_ORGANISM_ABBREV`,
  `_TECHNOLOGY_ABBREV`, `_ACCESS_ABBREV`, `_SOURCE_ABBREV`,
  `_SUSPENSION_ABBREV`.  Hover-tooltip carries the full term.
- **Collapsible obs-prefix groups** on the dataset page: items sharing
  a `::`- or `__`-delimited prefix collapse into a `[+] prefix:: (N)`
  chip.  Purely client-side JS in `dataset.html`.
- **Suggestion chips** under each plot-form input: curated favourites
  (blue) + pattern-detected smart picks (amber, e.g. pseudotime) +
  append-mode green chips for CSV fields (heatmap genes, rolling y).
  Implementation in `plot.html`'s `prefillAndChips()`.

## Running it

- Production / debug: under the supervisor — `bin/ops/ant-supervisor`
  has `browse` in `DEFAULT_SERVICES`, runs `./web/ant-browse` on
  port 8081, reverse-proxied at `/` by Caddy on port 8501.
- Standalone: `make serve-browse` (or `./web/ant-browse` directly).
- Restart only this service while the rest of the stack stays up:
  `./bin/ops/ant-supervisor restart browse`.

## What NOT to do

- **Don't talk to DuckDB or h5ad from here.**  If the REST API doesn't
  expose what you need, add an endpoint to `bin/api/ant-serve` first.
  The web UI is a client of the API; pushing past that contract creates
  duplicate code paths that drift.
- **Don't add a `static/` directory** without weighing the cost.
  Inline CSS/JS keeps the page self-contained and avoids cache-bust
  headaches.  Splitting only makes sense once a file crosses a few KB
  AND is reused across pages.

## Cross-references

- Shared core: `lib/` (anthive_db, plotting, config) — see `lib/CLAUDE.md`.
- REST API talked to: `bin/api/ant-serve` — see `bin/api/CLAUDE.md`.
- Reverse proxy: `config/Caddyfile` (debug) and `config/nginx.conf` (prod).
- Companion in-browser notebooks: `frontend/jupyter/` (built into
  `webapp/jupyter/`, served as static files at `/jupyter/`).
