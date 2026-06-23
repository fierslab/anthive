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
    base.html       # shared chrome (nav, auth menu, footer, storage notice)
    index.html      # landing — collection list with context pills
    folder.html     # one collection — per-dataset table
    dataset.html    # one dataset — info, biology, provenance, obs cols, viz
    plot.html       # in-app plot UI (form + preview + downloads)
    about.html      # /about — logo hero, what-it-is, live catalogue counts
    genesets.html, geneset.html, geneset_experiment.html
  anthive_logo.png  # the (white) brand logo — grayscale+alpha, for dark bg
  __init__.py       # so the directory imports cleanly if ever needed
  README.md         # user-facing overview
```

There is no separate `static/` directory — page-specific CSS and JS sit
inline in the matching `*.html` (the JS budget is small enough that
splitting into separate files would only add HTTP requests).  Sitewide
styles live in `templates/base.html`'s `<style>` block.  The one bundled
binary asset, `anthive_logo.png`, is **base64-inlined into a data URI once at
import** (`browser._LOGO_DATA_URI`, passed to `about.html`) — so even the logo
stays self-contained, with no static dir or asset route.  It's a white
line-art logo (grayscale+alpha PNG), so it's shown on a dark hero panel.

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
| `/about` | logo hero + what-it-is + live dataset/collection counts | `about.html` |

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
- **plot.html**: five DE geoms — `volcano` / `ma` (cascading
  study→term→contrast pickers, loaded client-side from `/de/…`), `forest`
  (gene + optional narrowing), and `de_heatmap` (REST 2.7: the DE result
  matrix — contrasts × genes/modules filled with a chosen effect size;
  study→term cascade + `value` auto/estimate/log2fc/zeff/effsize +
  `sig_overlay` + optional `contrasts`/`genes` CSV narrowers; server caps
  rows×cols at 2000). All four are in `DE_GEOMS` (so `initDeCascade` runs)
  and `canRender` (de_heatmap needs study+term). The geom-switcher only
  lists them when the dataset has DE. `ma` surfaces the server's "needs
  base_mean" 400 for GLMM/Wilcoxon studies. **`de_quadrant`** (API 2.12)
  compares two DE contrasts gene-by-gene; it has its OWN `initDeQuadrant` (the
  shared `initDeCascade` only handles one triple) with db-parameterised
  `_fillStudies/_fillTermsDb/_fillContrastsDb` — **two independent cascades**
  (X = path dataset; Y = `db_y`||path), the Y study pre-seeded to the X study
  (no ongoing X→Y coupling). Per-axis stat + `mode` are `<select>`s (NOT radios
  — `buildUrl`/`fieldVal` mishandle radio groups). It is NOT in `DE_GEOMS`
  (own init branch). Both `volcano` and `ma` have an
  **optional axis-clamp** fieldset (REST 2.7.2): `log2fc_clip` +
  `neglog10p_clip` (volcano), `log2fc_clip` + `logmean_clip` (ma) — blank =
  no clamp (`buildUrl` omits empties); a 0/out-of-range value gets the
  server's 422 in-status.
- **de_browser.html**: the full term→contrast→gene explorer (see its route
  above).

### The visualize dropdown + polymorphic scatter

- The dataset page's plot entry points live in **one top-right
  `visualize ▾` dropdown** (`dataset.html`), not a long button row.
- `scatter` / `hexbin` are **polymorphic**: an `embedding ⇄ X vs Y` mode
  radio toggles between the obsm-map form and a two-continuous-variable
  form. `setScatterMode()` shows AND enables/disables every control in the
  toggled fieldsets; `buildUrl`/`syncURL` skip `disabled` and `_`-prefixed
  fields so only the active mode submits.
- **2-axis pack (REST API 2.4):** XY mode (scatter/hexbin) + the `kde2d`
  geom expose per-axis **transform** dropdowns (`transform_x/_y` — linear,
  log, log1p, signed_log1p, asinh, signed_sqrt, rank; rendered by the
  `transform_select` Jinja macro) and a shared **overlays** fieldset
  (`marginals`, `regline`, `asinh_scale`). Hexbin density mode adds a
  a "clip density to percentiles" checkbox that pre-fills (editable)
  `vmin_quantile`/`vmax_quantile`. Legacy `log_x/log_y` URLs map to
  `transform_*=log1p` on load (back-compat). `kde2d` is gated in `canRender`
  on x+y both set.
- **Plot-form UX conventions (keep these when adding controls):**
  - **Help → hover `ⓘ`.** Verbose explanatory prose goes in the `info(text)`
    Jinja macro (`<span class="info" title="…">ⓘ</span>`) on the relevant
    legend/label, NOT inline `<small>` blocks. `text` must contain no `"`.
  - **Single-choice selects show as static text.** Two mechanisms, same
    `.field-static` look: `collapseSingleSelects(form)` *removes* a truly-static
    single-option `<select>` (embedding/layer) and drops in a hidden input
    (so `buildUrl`/`syncURL` still see the value); the DE **cascade** selects
    (study/term/contrast) instead use `reflectSingle()` — it hides the
    `<select>` and shows a static twin when there's one real option, and
    reveals the dropdown again if it repopulates to several (so a single-
    contrast term reads as text without breaking re-selection). `reflectSingle`
    is called at the end of `populateTerms`/`populateContrasts` and via
    `reflectDeCascade()` once the cascade settles. Mark a select
    `data-keep-select` to opt out (the geom switcher does); the `disabled`
    guard keeps removal safe with the polymorphic-scatter mode toggle.
  - **"📓 notebook"** (`#open-notebook`) opens the current plot as a one-off
    JupyterLite notebook. It packs `{db_id, geom, ...all plot params}` into a
    **urlsafe-base64 PATH segment** → `/api/notebook/{b64}/plot.ipynb` (ant-serve
    API 2.8, returns an `.ipynb` with data-pull + plot + caption + browser-only
    disclaimer), and `window.open`s `/jupyter/lab/index.html?fromURL=<that>`.
    The base64 path (no query string) is deliberate: JupyterLite's open-url
    extension names the saved file after the URL basename, so a query string
    would mash into the filename and break open-as-notebook — `plot.ipynb` stays
    clean. JupyterLite fetches it through the user's Authelia session (no creds
    in URL). Guarded on `canRender()`. (A query-param variant
    `/databases/{id}/plot/{geom}/notebook?…` also exists for curl/CLI.)
  - **Downloads are blob-fetched** (`downloadBlob(fmt, ext)`), not `<a href>` —
    so the long API URL never shows in the browser status bar on hover, and a
    failed render surfaces inline. `copy URL` copies `window.location.href`
    (the shareable interactive-page URL, full domain), not the image URL.
  - **Size presets are orientation-aware:** buttons carry `data-long`/
    `data-short`; a landscape/portrait toggle (`.orient-btn`) decides which is
    width vs height. Settings panel is a fixed 400px with a 150px label column
    so labels don't wrap.
- **Colour ramp anchoring (REST 2.6):** the settings panel has a
  `color_scale` select (`auto`/`sequential`/`divergent`) next to the palette
  pickers; `buildUrl` emits it only when not `auto`. The continuous palette
  list also carries 14 named single-hue white→colour ramps (`magenta`,
  `turquoise`, `brown`, …) with their `_r` variants. **`prefillFromURL` also
  applies `palette_*`/`color_scale` from the URL to the settings selects**
  (they're not `form [name]` fields) — so a curated viz link reproduces its
  exact colours in the live preview (URL wins for that page load, not
  persisted). This is why the Sierksma Fig-2 hexbin links render in their
  per-module colours.

## Sitewide UI conventions (base.html)

- **Help → hover `ⓘ`.** Verbose/explanatory prose hides behind a small `ⓘ`
  with a `title=` (the `.info` class is defined sitewide in `base.html`; `title`
  text must contain no `"`). Used in every view; plot.html also has a local
  `info(text)` Jinja macro. Prefer this over inline `<small>` help blocks.
- **Utility classes** (base.html, DRY — don't inline colors): `.muted`
  (`--text-muted`), `.muted-2` (#94a3b8), `.mono` (monospace).
- **Topbar brand logo** is `web/topbar_logo.png` (a dark-recoloured crop of the
  Authelia login mark), base64-inlined once and exposed as the Jinja **global**
  `topbar_logo` (`templates.env.globals`), so `base.html` shows it on every page
  without per-route context.
- **No-duplication rule on dataset.html:** headline biology (organism /
  technology / cell / tissue / disease / prep / year) lives ONLY in the coloured
  context pills (full names in their `title`); the **Biology** section shows only
  the remaining sample attributes (condition / sex / age / strain); **Provenance**
  shows only descriptive metadata (access / source / license / contributor);
  external resources (doi / pubmed / accession / url) are ONLY the `.ds-links`
  badges under the title. Each datum appears once. Sections hide when empty.

## Vocabulary shared across pages

- **Coloured "context" pills** (organism / tech / cell / tissue / disease
  / prep / year) on landing, folder, and dataset pages.  Palette is in
  `index.html`'s `.ctx-pill`, mirrored as `.grp-pill` in `folder.html` and
  `.ds-ctx .pill` in `dataset.html`.
- **Collection-constant metadata is hoisted to the folder header.** In
  `folder.html`, `get_collection_datasets` computes (via `_summarize_meta_field`
  over all datasets in the group) which fields are *constant* across the whole
  collection vs *variable*. Constant fields render as inert pills under the
  collection title (`collection_pills`) and their **table column is dropped**;
  only fields in `var_fields` keep a per-dataset column. So an all-microglia
  collection shows one "microglial cell" header pill instead of a repeated
  Cell column. Inline acc/src glyphs likewise only appear when access/source
  vary.
- **Access / source** are NOT pills in the list views — they're small
  desaturated glyphs (`.acc-src`, B&W) just *before* the collection/dataset
  name, and their value-filters live in the Collection column's header
  popover (`data-filter-fields="acc,src"` in `index.html`). External
  resources (DOI / accession / URL) render as small `.ext-lnk` 🔗 icons
  *after* the dataset name in `folder.html` (fed by doi/link in
  `get_collection_datasets`) and as a `.ds-links` row under the context bar
  on `dataset.html`.
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

## Developing here (the loop that actually works)

**Reload model.** Jinja templates auto-reload on every request — edit a
`*.html` and just refresh the browser. `browser.py` is imported **once** at
boot, so a Python change needs a browse restart:
`./bin/ops/ant-supervisor restart browse`. Caveat seen in the wild: if
`tmp/supervisor-state.json` has been wiped (e.g. a `tmp/` clean), the
`restart`/`status` CLI prints "No supervisor state file found" even though the
supervisor is alive — in that case `kill -TERM` the `ant-browse` child PID
directly (find it with `ps -eo pid,cmd | grep ant-browse`) and the running
supervisor respawns it (and rewrites the state file).

**Tests** live in `tests/test_web_browser.py` (~100 tests, the whole web
contract). They load `browser.py` via `importlib` (web/ isn't an importable
package), monkeypatch `browser._api_get` with an in-process fake so **no live
ant-serve is needed**, drive all routes through a FastAPI `TestClient`, and
render the **real Jinja templates** — so a broken route↔template contract or a
missing field fails here. New routes/markup → add a case (the suite already
covers DE gating, the visualize dropdown, XY mode, kde2d, acc/src inline,
external-link icons, the popover-filter guard, and the Authelia topbar).

There is **no shared venv** (every tool carries PEP-723 inline deps), so a bare
`python -m pytest` has nothing installed. Run via `uv` with the deps:

```sh
uv run --with pytest --with fastapi --with httpx --with jinja2 \
       --with markdown --with requests \
       env PYTHONPATH=lib python -m pytest tests/test_web_browser.py -q
```

`make test` wraps the same idea for the full repo suite (its `TEST_DEPS` adds
duckdb/numpy/pandas/h5py/scipy). A stray `uv.lock` may appear from `uv run` in
the repo root — delete it (this repo deliberately has no shared lock).

**Visual verification (headless).** The pages fetch `/api/**` at runtime;
Caddy proxies that to ant-serve on :8080 (and the whole site is behind
Authelia). Easiest loop: drive **ant-browse directly on :8081** and reroute
`/api/**` → :8080 in the headless browser, mirroring Caddy's
`handle_path /api/*` prefix strip:

```python
page.route("**/api/**", lambda r: r.continue_(
    url=r.request.url.replace("localhost:8081/api/", "localhost:8080/")))
```

Run chromium via `uv run --with playwright python …` (one-time
`uv run --with playwright python -m playwright install chromium`). To exercise
the auth topbar, inject the proxy headers with
`page.set_extra_http_headers({"Remote-User": "mf", "Remote-Name": "…"})`. Note
`:8501` (Caddy) is TLS + Authelia-gated, so it's not the convenient target for
scripted screenshots — go direct to :8081 with the reroute.

## Working with the API/viz side (multi-session)

Plotting, DE endpoints, and anything touching DuckDB/h5ad live in
`bin/api/ant-serve` + `lib/plotting.py` — usually a **separate Claude session**
(`anthive4`). When the UI needs new data or a server-side guard (e.g. "cap bar
plots at 100 bars", "volcano should fall back to a z-effect axis when log2fc is
null"), **request it there** instead of working around it in the client — the
server already returns helpful 400s that `plot.html` surfaces in-status.

Sessions share one git worktree + HEAD, so: **branch before committing**
(`web/<topic>` for UI work), commit eagerly (keeps `git checkout` safe for the
other session), FF-merge to `main` + delete the branch + push when shippable.
Coordinate via `pika message send --to anthive4`.

## Gotchas worth keeping in mind

- **The header popover is a child of its `<th>`** (`index.html`), so clicks
  inside it bubble to the th's click handler. That handler early-returns on
  `activePopover.contains(e.target)` — drop that guard and the filter
  checkboxes close the popover before their `change` fires (they become
  unclickable). The sort buttons are safe because they `stopPropagation()`.
- **`buildUrl`/`syncURL` skip `disabled` and `_`-prefixed fields**; the
  polymorphic scatter/hexbin mode switch relies on it (`setScatterMode`
  disables the inactive mode's whole fieldset). Keep that contract when adding
  form fields, and name UI-only controls with a leading `_` (e.g. `_xymode`,
  `_autoclip`).
- **DE term/contrast names can contain spaces and dashes** (e.g.
  `AppNLGF - AppHuHu`); they go in the plot URL as query params (encoded by
  `URLSearchParams`) and in `/de/{study}/terms/{term}` path segments
  (`encodeURIComponent`). Don't assume slug-safe.

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
- Reverse proxy: `config/Caddyfile` is the SINGLE source of path
  routing on dev AND prod (env vars flip the listener/TLS/auth split
  via the `caddy-{tls,auth,http-redirect}.{dev,prod}` snippets — see
  `doc/DEPLOY.md`). On prod, nginx is a thin SSL+Authelia shim that
  forwards every path to Caddy on `127.0.0.1:8500`
  (`config/nginx-intern-anthive.conf`).
- Companion in-browser notebooks: `frontend/jupyter/` (built into
  `webapp/jupyter/`, served as static files at `/jupyter/`).
- **Auth (Authelia cookie SSO):** the proxy (`config/Caddyfile` / nginx)
  gates every route via `forward_auth` and forwards `Remote-User` /
  `Remote-Name` / `Remote-Email` / `Remote-Groups` headers. `base.html`'s
  topbar reads these to show the signed-in user + a "sign out" action
  (`POST /authelia/api/logout` → `/`) and an account-settings link
  (`/authelia/`). Only renders when the header is present (un-proxied access
  shows nothing). **Never add HTTP Basic Auth to browser routes** — browsers
  resend stale creds and trip Authelia's rate-limiter/ban. See
  `doc/HANDOFF_authelia.md`.
