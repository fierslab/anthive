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
| `/browse/{group}/{file}/de/{study}` | differential-expression explorer for one study | `de_browser.html` |
| `/genesets` | top-level geneset study list | `genesets.html` |
| `/genesets/{study}/{experiment}` | one experiment's geneset list | `geneset_experiment.html` |
| `/genesets/{study}/{experiment}/{name}` | one geneset's gene table | `geneset.html` |

The plot page talks to the REST API directly from the browser via JS —
the FastAPI router doesn't proxy plot requests.  It just hands the
client a populated form (with curated favourites + smart pseudotime
detection) and lets the client hit `/api/databases/{id}/plot/{geom}`.

**Route ordering matters.** `plot_view` and `de_study_view` are registered
*before* `dataset_view` because the latter's `{dataset_browse_id:path}`
converter is greedy and would otherwise swallow `…/plot/scatter` and
`…/de/{study}`. Keep new `/browse/{group}/{file}/…` sub-routes above
`dataset_view` in `browser.py`.

### Differential expression (REST API 2.3)

DE is surfaced in three places, all **gated on the dataset actually having
DE data** (`dataset_view` / `plot_view` fetch `/databases/{id}/de`; empty →
nothing DE shows):

- **dataset.html**: a stat-row badge (`DE: N studies, M contrasts`), a
  "Differential expression" panel listing studies, and a `differential
  expression` category in the visualize dropdown — each deep-links to the
  `de_browser` page or the DE plot forms.
- **plot.html**: three DE geoms — `volcano` / `ma` (cascading
  study→term→contrast pickers, loaded client-side from `/de/…`) and `forest`
  (gene + optional narrowing). The geom-switcher only lists them when the
  dataset has DE. `ma` surfaces the server's "needs base_mean" 400 for
  GLMM/Wilcoxon studies.
- **de_browser.html**: the full term→contrast→gene explorer (see its route
  above).

### The visualize dropdown + polymorphic scatter

- The dataset page's plot entry points live in **one top-right
  `visualize ▾` dropdown** (`dataset.html`), not a long button row.
- `scatter` / `hexbin` are **polymorphic**: an `embedding ⇄ X vs Y` mode
  radio toggles between the obsm-map form and a two-continuous-variable
  form (free-text x+y + log1p toggles). `buildUrl`/`syncURL` skip
  `disabled` and `_`-prefixed fields so only the active mode submits.

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

## Responsive / mobile

- **Wrap every `<table>` in `<div class="table-wrap">`.**  `.table-wrap`
  (defined in `base.html`: `overflow-x:auto`) keeps a wide table scrolling
  inside its box instead of widening the whole page on a phone.  This is a
  convention — any new table must be wrapped.  A route test asserts the
  wrapper is present (`tests/test_web_browser.py`).
- **Row-per-entity tables become 2-column cards below 760px via the shared
  `table.card-table` rule in `base.html`.**  Both the landing collection
  table (`index.html`) and the folder dataset table (`folder.html`) opt in
  with `class="card-table"`.  Below 760px each `<tr>` becomes a card whose
  cells pack into a `1fr 1fr` grid.  Per-cell hooks the rule consumes:
  - `td.card-title` — spans both columns, card header (no label)
  - `td.card-span`  — spans both columns (e.g. the context pill cluster, or
    the folder's `Cells × Genes` stat)
  - `td[data-label="…"]` — labelled `LABEL: value` via `::before`
  - empty `<td>` collapses out (`:empty`) so a card shows only the fields it
    has — **keep such cells free of inner whitespace** or `:empty` won't
    match and a blank labelled line appears.

  Adding the card layout to a new table = give the `<table>` `card-table`,
  then tag its cells with the hooks above.  No per-page CSS needed.
- Other breakpoints: `plot.html` sidebar+plot grid and `dataset.html`'s
  `.two-col` / `.info-grid` collapse to one column at ~760px.

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
