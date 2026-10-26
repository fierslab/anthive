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
    de_browser.html # term→contrast→gene DE explorer
    genes.html      # /genes lookup form, /genes/{g[,g,…]} single-gene atlas OR multi-gene heatmap
    privacy.html    # /privacy — chat data-handling note (draft copy)
    # gene-set & module browser:
    genesets.html          # group selector (gene sets / starCAT / find-by-gene)
    geneset.html           # one gene set (+ "where it varies most" panel)
    geneset_experiment.html
    geneset_starcat.html   # one starCAT program: top-N genes × weight bars
    geneset_variability.html  # module × dataset variance-explained heatmap
    # admin (Authelia `admins` only):
    edit_collection.html   # /admin/edit/{group} — index.md editor
    edit_dataset.html      # /admin/edit/{group}/{file} — .md + .mus editor
    admin_feedback.html    # /admin/feedback — chat feedback triage (no menu entry)
    account_mcp.html       # /account/mcp — the user's MCP links (+ all users' links for admins)
  anthive_logo.png  # the (white) brand logo — grayscale+alpha, for dark bg
  topbar_logo.png   # dark-recoloured topbar mark (base64-inlined Jinja global)
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
| `/` | front door (preview `front-door`): search box, browse entry points, two examples, closed "What is here"; old landing query strings (`?q=`, `?disease=`, `?view=`...) 302 to `/collections`; one visible collection still jumps into it (`?all=1` opts out) | `front_door.html` |
| `/collections` | dataset catalog (grouped by collection), "Browse by dataset"; `/datasets` redirects here | `index.html` |
| `/search?q=` | search results grouped by kind (preview `search`), from `GET /api/search` (API 2.48) | `search.html` |
| `/diseases`, `/diseases/{key}` · `/cell-types`, `/cell-types/{key}` | dataset counts per disease / cell type and the dataset list of one (previews `browse-disease`, `browse-celltype`), from `/databases` via `lib/catalog_search.py` | `browse.html` |
| `/browse/{group}` | one collection | `folder.html` |
| `/browse/{group}/{file}` | one dataset (info + viz launcher) | `dataset.html` |
| `/browse/{group}/{file}/plot/{geom}` | interactive plot UI for one geom on one dataset | `plot.html` |
| `/browse/{group}/{file}/plot/donor` `?facet&cell_type&gene` or `&module&method&source` | per donor plot of one gene or gene set for one facet and cell type (the values the facet test used); image from `/api/facets/{f}/donor-plot.{png,svg,pdf}` with `db_id`, numbers from `/donor-values`; registered before `plot_view` | `donor_plot.html` |
| `/browse/{group}/{file}/de/{study}` | differential-expression explorer for one study | `de_browser.html` |
| `/genesets` `?group=genesets\|starcat\|bygene` | group-selector browser | `genesets.html` |
| `/genesets/variability` `?tag=&statistic=&source=` | module × dataset variance-explained heatmap | `geneset_variability.html` |
| `/genesets/starcat/{catalog_id}/modules/{module_name}` | one starCAT program (top-N genes × weight) | `geneset_starcat.html` |
| `/genesets/{study}/{experiment}` | one experiment's geneset list | `geneset_experiment.html` |
| `/genesets/{study}/{experiment}/{name}` | one geneset (+ "where it varies most") | `geneset.html` |
| `/about` | logo hero + what-it-is + live dataset/collection counts | `about.html` |
| `/genes` | gene atlas entry: lookup form (`?q=` redirects to `/genes/{q}`); target of the top-bar `genes` link (`#nav-genes`), which replaced the old top-bar search box | `genes.html` (empty `gene`) |
| `/genes/{gene[,gene…]}` | single-gene atlas OR multi-gene heatmap (comma list) | `genes.html` |
| `/privacy` | chat data-handling note (draft) | `privacy.html` |
| `/admin/edit/{group}` · `/admin/edit/{group}/{file}` | index.md · dataset .md/.mus editors (admin) | `edit_*.html` |
| `/admin/feedback` | chat-feedback triage (admin) | `admin_feedback.html` |
| `/account/mcp` | the signed-in user's named MCP links: add (with label) / show / copy / rotate / revoke each, the "Set up with one paste" agent prompt (`#mcp-paste`), client setup snippets for a chosen link; admins also get the "All users' links" section (`#all`). 401 without `Remote-User` | `account_mcp.html` |
| `/admin/mcp-links` | 307 redirect to `/account/mcp#all` | none |
| `/api-guide` | how to use the REST API: login (basic auth on /api, netrc, env vars), curl first steps, pyvark, vark, admin-only endpoints and limits, API version; links on to `/api/docs`. Target of the user-menu entry `API` (`#api-menu-link`); the top bar has no API link (removed 2026-10-22, the dropdown renders in every auth mode, so the page stays reachable on auth.mode none too). Examples use `mcp.public_base_url` or the request host, always https; text adapts to `auth.mode` | `api_guide.html` |
| `/papers/{citekey}` | one library paper: record, summary/topics (draft markers), linked datasets grouped by role with the reason, related papers grouped by kind with evidence, PDF + BibTeX | `paper.html` |
| `/library` | all library papers, client-side search + role/year/cohort/cell-type filters | `library.html` |
| `/donor-groups` | every donor group (`GET /donor-groups`) with its datasets marked primary data / reanalysis / unresolved, `#dg-<slug>` anchors; linked from the dataset lineage row, `/facets` and the help page | `donor_groups.html` |

**Paper library UI (API 2.39+).** `dataset_view` fetches `/databases/{id}/papers`
and `folder_view` does so for every dataset of the collection (deduplicated,
"Linked to N of M datasets"; on the collection page the panel sits after the
dataset table and the collection description, just before Downloads); both render cards from the `paper_card` macro in
`_papers.html` (styles `.paper-card` / `.paper-role` in base.html). Source
papers come first; the badge and sentence come from `_paper_reason()`
(source / reanalysis / method / related, "preprint" for online/unpublished
entries). A summary listed in `draft_fields` gets the "draft, not yet
reviewed" marker. The dataset page shows a `Papers` tab directly before Downloads and a
server-rendered `#ds-paper-files` subsection in Downloads (paper PDFs come
from `/api/papers/{citekey}/file`, not from `static/`); that subsection keeps
the Downloads tab visible even when `/files` lists nothing. Everything hides
when the library is off or the call fails; there are no per-paper access
rules (2.41): everyone past the site login sees every paper. The paper page
shows the paper's own role badge (`_PAPER_ROLE_HELP`) and document `type`. The top-nav `library` link appears only
when `/library/status` reports papers (background-probed, cached:
`_library_available`). Gotcha: never name a dict key `values`/`items` for a
template (`t.values` resolves to the dict method).

**Facet docs (unreleased API).** `/facets` shows each facet's one-line
`summary` and the audit note (`definitions_status`); `/facets/{name}` shows the
definition on top (`#facet-definition`) and a Datasets card
(`#facet-datasets`): carriers with author column, mapping kind and value
translation, and the non-carriers folded in `#non-carriers` with name hints.
The dataset **Facet tab** (label of the Facet tab group, `#facets`, after Genesets, count badge; the effects are in its Gene sub-tab `#facet-gene`; shown
whenever the dataset has any `ant_*` facet) holds `#ds-facets` from
`GET /databases/{id}/facets`: unit line (`#fx-unit-line`: designation, donor
group, cohort; the spot where a "Replicate unit" entry goes), mapping tables
for ALL facets (analysed ones first, the rest under "Not analysed yet",
`#facets-not-analysed`; nothing is dropped), then "Facet DE, genes per cell type" (the Gene sub-tab) or the
explicit reason there are no tests (`_fx_reason_text` in browser.py turns the
facet-tool eligibility reason into a sentence). The Data tab keeps one pointer
line (`#ds-facets-pointer`). `#ds-facets` as a hash still works (mapped to
`facets`). Both pages use the macros in `_facet_mapping.html`.
Definitions live in `lib/obscol_tags_vocab.yaml` (`facet:` blocks), never in
templates. Those two fetches go through `_api_get_as_user`, which forwards
`Remote-User`/`Remote-Groups` so admins see `obs_hidden` source columns;
everyone else gets them redacted by ant-serve.

**Facet effects (unreleased API).** Donor-level facet test results are
rendered from one partial, `_facet_effects.html` (macros `fx_table`,
`fx_rows`, `fx_model`, `fx_sex_control`, `fx_methods_box`, `fx_recurring`,
`fx_gene_panel`, plus `fx_script` for expand-and-fetch and
`fx_forest_assets` for the HTML/CSS forest, `window.antFx.renderCard`).
The forest card (`renderCard(box, card, {open})`) is summary-first: one
`details.fxs-group` line per cell type group (units matched on
`celltype_group`, never on the raw `cell_type`, which is shown in small text),
"up in k of m independent cohorts" plus the median log2FC of the counted
rows; the per-dataset forest is collapsed under it, rows that do not count are
folded with their `not_counted_reason`, `q_counted` shows as "xn" under q
when `n_sibling_strata` > 1, and groups without a counted row sit in one
collapsed `details.fxs-rest`. Reanalyses (`is_secondary`) get the
"reanalysis, not counted" pill. The dataset page has a lineage row
(`#ds-lineage`: designation pill, reanalysis of, donor group link) from the
`designation`/`reanalysis_of`/`donor_group`/`has_donor_uid` fields of
`/info`, copied into the curated `info` dict in `dataset_view`. Layout rules:
`table.ds-facets td.num` (nowrap, right) out-specifies plain `.fx-table`
rules inside the dataset card, so fx rules that must win there are written as
`table.ds-facets table.fx-table td.x, table.fx-table td.x`.
Places: the "Facet DE, genes per cell type" section of the dataset Facet > Gene sub-tab
(`_facet_tests_view` groups units by facet column), "Effects across
datasets" on `/facets/{name}` (`#facet-effects`, replaces the per-cell "Top
variable genesets/genes" cards; `#fx-recurring` lists recurring genes per cell
type), and the gene page panel `#gene-facet-effects` (single-gene view only).
Every unit shows its model line with a details disclosure; the help page
sections are `#facet-effects`, `#facet-counting`, `#facet-howto` (PTPRG walkthrough)
and `#glossary`. `_fx_pills` returns (label, class, tooltip, glossary slug). `facets_visible` (config, read once in
`_facets_visible`) marks the other facets as not analysed: `/facets` and the
facet pages hide them (non-admins get a short notice), the dataset Facets tab
lists them under "Not analysed yet"; the `/facets` index no
longer shows the per-cell Signal column. Tests set `_facets_visible_cfg`
through an autouse fixture.

**Facet replication v2 (UI).** Badges say "replicated (r = ...)" only when the
API `r_value` < 0.05 (`replicated`); "k of m cohorts significant (q_treat <
0.05)" is descriptive, with the Simes partial conjunction / treat tau method
line above the groups. Each open group lists its `anchors` (pool units
`pool:<id>` render as text with a visible member list from `/facets/cohorts`,
never as links). Pills: `replicate level (donor)` / `cell-level, descriptive
only` (no tests, reason visible), `pooled`, `donor overlap not checked`;
`fx_unit_notes` prints disease definition, cohort role and not-counted reason
as visible lines. `/facets/{name}` renders first and fetches the units table
from `GET /facets/{name}/effects-table` (`_facet_effects_across.html`,
bound by `window.antFxBind`). The dataset unit line `#fx-unit-line` shows
Replicate unit and Disease basis (`_replicate_view`: `/info` fields first, else
the `/facet-tests` summary or its first unit). Hover-only text: base.html turns
`.info` and `.fx-pill` into focusable click/Enter bubbles.

**Facet engine v3 UI (API 2.44).** Results are shown per *scope* (`human_postmortem`,
`xenograft_human_in_mouse`; `in_vitro` listed, not tested; the pseudo scope `none` is dropped by
`_facet_scope_options`). `/facets` has a Scope column of links, `/facets/{name}?scope=` a selector
(`#facet-scope-nav`) and the `fx-scope` pill (base.html) on every result block; the gene panel has a scope
selector above per-scope facet tabs (JS config `FACETS` in `fx_gene_panel`, all calls pass `&scope=`). Tested
variants without a column (`age_adjusted`, `braak_threshold`, `apoe4_dose`) are tabs on the parent page
(`_FACET_VARIANTS`, `#fx-variants`, `window.antFxRecurring.setFacet`, fragment `?variant=&scope=`). Group lines
show: main badge "replicated (r)", "carried by" the two `r_cohorts`, a separate effect-filter pill, the second
tier badge (`replicated_effect`), "m includes n cohorts below 20 donors" (`n_small`), lambda, dropped covariates,
gene-class pills (`gene-class-flags`), age persistence. `analysis_level` `below_floor` reads "descriptive: below
the p-value floor" (`_floor_text`). Excluded datasets (`_fx_exclusion_text`, served `reason` starting
"excluded from the facet layer" or mouse-only) keep the Facets tab with `#fx-excluded`. Author-cluster facets get
a "dataset-scoped, no tests" note, `ant_tissue` an umbrella note plus `#ds-tissue-detail` (from `/info` `tissue`).

**Gene, gene set and facet views (tabs, 2026-10).** `genes.html` (single gene only), `geneset.html` and `facet.html`
(only when the facet has tests) put a one-line summary above a tab bar (`#vw-tabs`, `window.antViews.initTabs`, panels `#vt-<name>`,
buttons `data-vtab` / `data-hash` / optional `data-alias` for old hashes, no nested tabs). Gene: `1. Expression` (#expression, alias
#gene-view), `2. What shifts it` (#shifts), `3. Donor variability` (#variability, placeholder), `Author DE (as published)` (#author-de, shown
when present) and `Atlas (old)` (#atlas) last. Geneset: `1. Module score` (#module-score), `2. What shifts it`, `3. Donor variability`,
`Geneset page (old)` (#page). Facet: `Overview` (#overview, four numbered sections) and `Detail (old)` (#detail). A hash pointing into another
panel opens that panel; the atlas `&gv=` tail is kept. Partials: `_views.html` (CSS `.vw-*`, shared JS: call logic, grid >= 900 px and list
below, forest with q column, donor plot and links, tabs), `_view_gene.html`, `_view_geneset.html`, `_view_facet.html`. All rendering is
client-side from the API; `browser.py` passes `view_dbs` (`_view_db_map`: scope, group, human, secondary, `modality`, `celltype_col`) and serves
`GET /genes/{gene}/author-de`. Layout rules: every table that opens rows or sits in a tab is `table.vw-table.fixed` with a `<colgroup>`
(panel 1 is one table, opening a cell type adds rows to it; the facet matrix has fixed column widths and the cohort panel is a full-width row in
the same table), so nothing shifts on expand (verified at 1440, 1024 and 390 by comparing header widths). Matrix cell shows only k/m, three
colours, arrow for direction; caveats are one notes line under each matrix with a (?) into the help panel; no doc_block in the view tabs.
Donor plot and links (API facet-donor-values): `V.linksHtml(links, rows)` renders nothing when the replication entry has no `links`; it shows
the donor plot lazily (fetch + IntersectionObserver, caption from `X-Plot-Caption`), an "adjusted for covariates" toggle, "donor values (JSON)"
and "per-cell view in <dataset>" links. Fields read (API 2.45, `links` on every gene and module replication entry): `donor_plot`, `donor_plot_svg`, `donor_plot_adjusted`, `donor_values`, `donor_values_available` (false until the facet tests are rerun: the block then says the plot is available after the next run and shows no image), `anchors` (genes only, `{db_id, url}`; the API path `/databases/..` is turned into the `/browse/..` page). Panel 1 dataset rows link the dataset page and a violin of the gene
(or module score) by the dataset's `celltype_col`. The forest shows estimate, 95% CI and the per-cohort q.

**Geneset panel 1 rank (API 2.45).** `1. Module score` shows the "Rank among gene sets" bar (top x%, "of N" in dataset rows, cell type row =
mean over datasets, dotted line in opened rows), not the mean score: a mean score is not comparable between datasets. Rows are drawn from
`/databases/{id}/module_scores` at once and each rank fills in from `GET /genesets/{s}/{e}/{n}/rank?db_id=` (first call per dataset reads the
whole sidecar, so the cell shows "computing..." until it answers; the open/closed state of cell type rows survives the redraws). The scanpy
source is the only one shown; the (i) tooltip and the (?) help say UCell depends on gene-set size and that ranks compare sets within one dataset only.

**MCP links UI** talks to ant-serve's `GET /me/mcp`, `POST /me/mcp/links`,
`POST /me/mcp/links/{id}/rotate`, `DELETE /me/mcp/links/{id}`,
`GET /admin/mcp-links`, `DELETE /admin/mcp-links/{user}/{id}` client-side
(same-origin `/api`). The single account-menu entry (`#mcp-menu-link`) is
hidden until `/api/me/mcp` reports `enabled:true`; its label is "Connect an
MCP client" with no links and "MCP links" once the user has one. URLs and
snippets are masked by default; Copy always copies the real text. Rotate
and Revoke confirm first because the old URL dies immediately.

**One-paste setup prompt (`#mcp-paste`).** `_mcp_setup_prompt()` in
browser.py builds a numbered, plain-language prompt for an agent with a
shell (Claude Code, Codex, Gemini CLI, OpenCode, Cursor, ...): explain and
ask for OK, add the MCP connection at user scope, install the skills with
`npx skills`, optionally install vark (the user runs `vark server login`
in their own terminal), check, summarise. Safety rules (link only in
user-level config, never echoed in full, never ask for the password, stop
on failure) come first. The route renders it with
`MCP_PROMPT_LINK_PLACEHOLDER`; the JS swaps in the chosen link, so the
secret never sits in the HTML. Connection name per site from
`_mcp_connection_name()` (`anthive-<first host label>`); site URL from
`_site_base_url()` (`mcp.public_base_url` or request host).

**User menu (base.html).** Opens on hover, focus, or click/tap
(`#user-menu-toggle`, `.dropdown.open`), closes on outside click or Escape.
`.dropdown-menu::before` is an invisible bridge over the gap between toggle
and menu: without it the pointer leaves `.dropdown` while moving down and
the menu closes (the "fiddly dropdown" bug). The menu has "Report an issue"
(`#issue-tracker-link`, new tab) for signed-in users when
`issue_tracker_url` is set (Jinja global `issue_tracker_url()`); the old
admin-only "chat feedback" entry is gone (`/admin/feedback` still exists).

**Route ordering, geneset routes:** `/genesets/variability` and
`/genesets/starcat/{catalog_id}/modules/{name}` are registered *before* the
greedy `/genesets/{study}/{experiment}[/{name}]` routes so `variability` /
`starcat` aren't read as a study id. Same greedy-converter rule as `/browse`.

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

## Gene-set & module browser (`/genesets*`)

`/genesets` is a **group-selector** (`?group=`), rendered client-side from JSON
the server embeds (server fetches `/genesets` + `/catalogs` and passes the data):

- **`genesets`** — gene sets from the registry. Study → experiment **dropdowns**
  (`antRemember`-persisted), then that experiment's gene-set table. 128 sets
  across 4 studies made the old dump-everything view unusable; dropdowns show
  one slice (~15–20 rows). Single-choice dropdowns auto-collapse to a static
  label.
- **`starcat`** — starCAT programs from `/catalogs`. Modules grouped by
  biological `group` with descriptions; derived-score recipes (columns ·
  weights · threshold) in a `<details>`. Each module links to
  `geneset_starcat.html` (top-N genes × **weight bars**, from
  `/catalogs/starcat/{cat}/modules/{name}?top=N`).
- **`bygene`** — reverse lookup: type a gene → every set/program containing it,
  split gene-set vs starCAT. Backed by `GET /genesets/by-gene/{gene}`
  (**pending ant-serve** — shows a graceful "lookup pending" state until it
  ships; `?gene=` deep-links).

**`geneset.html`** shows `scored_in` (datasets where the set is a pre-computed
module score) + a **"where it varies most"** panel: top dataset × tag
associations for that set from `/module-variability?statistic=…`, with a
value-vs-all-modules range bar (per-(dataset,tag) min/max across all modules).

**`geneset_variability.html`** — fleet-wide **module × dataset heatmap**
(REST 2.28 `/module-variability`), colour = variance-explained of a chosen
`tag`. Critical framing (the stats are **per-cell / pseudoreplicated**, not
donor-level): a dismissable caveat banner (30-day `localStorage` ack), the raw
`F` statistic is hidden, categorical default is bias-corrected **ω²** (not
eta²), a warning fires when a tag covers ≤3 datasets, and the colour domain is
**percentile-clipped** (default 98th, configurable) so one saturating module
doesn't wash the map. Selectors persisted via `antRemember`.

`/module-variability` gotchas: it NaN-500s at `limit≳5000` → always pass
`min_abs=0` (drops NaN rows, dodges the 500). `/module-variability/dimensions`
(selector lists) and a donor-level (pseudobulk) statistic are **pending
ant-serve** — the client uses a hardcoded tag/statistic fallback meanwhile.
Naming trap: `/module-variability` keys starCAT rows on **score** names
(`DAM_pos`) under catalog `MICROGLIA_V1`; `/catalogs` uses **module** names
(`Microglia_DAM`) under `MICROGLIA_V1_0` — different spelling + module≠score.

## Downloads — raw h5ad + stored files (ant-serve endpoints ADDED here)

Dataset page has a **Downloads** section: fetches `GET /databases/{id}/files`
(lists the stored artifacts that exist: `h5ad` / `duckdb` / `scores` / `mus`,
each `{kind, filename, size_bytes, source: s3|local}`) and links each to
`GET /databases/{id}/download/{kind}`. That endpoint **307-redirects to a
short-TTL presigned S3 URL** (browser pulls straight from Hetzner object
storage — no multi-GB proxy) or streams a local `FileResponse`. Gate == catalog
visibility (any dataset the caller can see is downloadable — public + internal,
no admin gate). **These two endpoints live in `bin/api/ant-serve`** (registry
`list_artifacts` / `artifact_download` / `_s3_head_size`) — built from this
session because the S3 creds are there; see the multi-session note below.

## Plot thumbnails in markdown

Any dataset/collection `.md` can embed a live plot thumbnail with a compact
scheme: **`![caption](plot:GEOM?params&db=group/file)`** (on a dataset page
`db` defaults to that dataset; collection markdown must pass `&db=`).
`browser._expand_plot_thumbnails()` post-processes the rendered markdown:
`<img src="plot:…">` → `<a href=interactive-plot-page><img src=PNG-endpoint>`
with small `width/height/dpi` injected if absent, everything else (field,
colour, palette, transforms) preserved; unresolved `db` → an inline hint, not a
broken image. Authors never hand-write it — the plot toolbar's **🖼 copy
thumbnail md** button emits the token (drops size/format, appends `&db=`).
No ant-serve change: the plot PNG endpoint already exists.
(Since the round-3 security review every markdown body (dataset `.md`, collection `index.md`, gene set and
paper notes) goes through `_store_markdown` = `safe_markdown.render(..., demote_headings=False,
allow_plot_images=True)`: markdown-it-py with raw HTML off and the link allowlist. Deposits write seed `.md`
and `index.md` files, so no body is trusted. Plot thumbnails survive as `<img src="plot:...">` for
`_expand_plot_thumbnails`; every other image is dropped. Never call `markdown.markdown` on store text.)

## Admin editors (Authelia `admins` group, `_is_admin(request)`)

- **`/admin/edit/{group}`** (`edit_collection.html`) — collection `index.md`:
  group/group_id read-only, generic fields, markdown body + preview,
  `default_dataset` dropdown.
- **`/admin/edit/{group}/{file}`** (`edit_dataset.html`) — dataset `.md`
  (curatable whitelist + "Long Description" body) AND a **`.mus` biology-
  annotation editor** (REST 2.25 `/source.mus`): controlled fields as
  `<select>`/datalist from `GET /mus/vocab`, minimal-diff PUT, 400 vocab-gate
  surfaced inline. AI-curation chips (`/curation`) fill both `.md` and `.mus`.
- **`/admin/feedback`** (`admin_feedback.html`) — triage of chat feedback flags
  (`GET /chat/feedback`), colour-coded category + flagged message; no
  longer linked from the account menu (reach it by URL).
- Non-admin → 403 (`index.html` with an error); the server-side write gate in
  ant-serve is authoritative. Never add HTTP Basic Auth (see Auth note).

## Sitewide UI conventions (base.html)

- **Section headings: one style.** Every section on the collection and
  dataset pages (including the headings inside dataset tab panels and the
  Data-tab cards) is `<h2 class="section-head">`, the level below is
  `<h3 class="subsection-head">` (also in JS-built markup). Both are defined
  once in `base.html`: 15px semibold dark teal with a 3px accent bar for
  `.section-head`, 13px semibold teal for `.subsection-head`. Sentence case,
  no inline heading styles; an inline count goes in `<span class="muted">`
  and the `ⓘ` stays aligned. The first section heading in a tab panel or
  card loses its top margin automatically; use `.section-head.first` where a
  wrapper already provides spacing. Headings inside markdown descriptions
  (`.md-body`) stay neutral so sections remain distinct.
- **Help boxes are hidden, opened by an (i) after the header (2026-10-13, sitewide rule).** Every explanatory,
  intro or methods block longer than a line is `{% call doc_block('About ...', id='...') %}...{% endcall %}`
  (`_macros.html`), which renders `<div class="doc-box" role="region" hidden>` with no visible summary, plus
  `{{ doc_toggle('<id>', 'About ...') }}` placed right after the section header text (or the label it explains):
  a `<button class="doc-toggle" data-help-for aria-controls aria-expanded>` (i) that shows and hides the box.
  The toggle JS is the same as the (?) panels (`.help-btn, .doc-toggle` in base.html); a `#hash` pointing at the
  box or anything inside it opens it (`openTarget`). Open state is not remembered: boxes start hidden on every
  load. One short essential line stays visible next to it (for example "Exploratory. Effects are tested per
  donor within one cell type."). **Fallback:** `doc_block(..., summary=True)`, or a doc_block without an id,
  keeps the older `<details class="doc-block">` bar with an "(i) About ..." summary (remembered in
  `localStorage`), for boxes with no header to hang an (i) on. Current fallbacks: `fx-methods` (facet page,
  second box under one header), `mv-stats-help` (variability heatmap, second box), `gv-hm-about` (gene
  heatmap, JS-revealed block), `api-guide-mcp-about` (inside the Signing in section), the explorer box
  `<id>-about` in `_module_view.html` unless `mv_ctx.about_in_header` (true on the dataset GS panel), and the
  JS-built `docBlock()` boxes in `_facet_effects.html` and `_views.html` (call `window.antDocRestore(root)`
  after rendering). The effects box `fx-about` (from `fx_intro`) now also holds the flag legend
  (`#fx-flag-legend` is an `<h4>` inside it); its (i) sits on the header above `fx_intro`
  ("Facet DE, genes per cell type" on the dataset Gene sub-tab, "Effects across datasets" on the facet page). `/help`
  sections stay `<details class="help-section">`.
- **Facet label panel tags.** `#fx-unit-line` is a `table.fx-keys` (wrapped in `.table-wrap`): one row per
  key (`#fx-scope`, `#fx-replicate`, `#fx-control-basis`, `#fx-disease-basis`, `#fx-donor-group`,
  `#fx-designation`, `#fx-cohort` "Counts towards replication"), the value as a `.fx-tag` pill, an (i) per
  label opening a `doc-box` (`fx-help-*`) inside the value cell. The cohort help explains the curated
  cohort table (a draft awaiting sign-off) and holds the "About facets and their effects" link. The
  "dataset changed" warning `#fx-changed` stays visible.
- **Tooltips link to the glossary.** An `.info` or `.fx-pill` with
  `data-glossary="<term>"` shows its one-sentence `title` text plus a "More in
  the glossary" link to `/help#glossary-<term>` in the click bubble. Every slug
  used in a template must exist as `id="glossary-<slug>"` in `help.html`
  (`test_help_glossary_defines_every_term_the_pages_link_to`).
- **The conceptual help text is not in `help.html`.** The sections what,
  finding, pages, plots, genes, per-cell-per-donor, the facet sections, papers
  and the whole glossary are rendered from `lib/help_content/` (`{{
  help_topic_html('<id>') }}`, `{{ help_glossary_html() }}`), the same files the
  API (`/api/help`) and the MCP `explain` tool serve. Edit
  `lib/help_content/topics/<id>.yaml` or `glossary.yaml` (HTML fragments,
  `{{ docs }}` for the docs base URL, a topic's `children` become its `<h3>`
  subsections with the same ids), never the rendered section in `help.html`.
  The quickstart, AI, programmatic access and account sections stay in the
  template. `test_help_page_renders_every_topic_and_glossary_term_from_help_content`
  checks that the page and the files agree.
- **Help convention: (?) panels and (i) tooltips (2026-10, gene view first).**
  Two devices, never mixed. A section header is numbered and minimal ("1. Where is
  it expressed") with a filled teal round **(?)** button (`.help-btn`) that opens a
  larger `.help-panel` directly below it, closed by default: Jinja macro
  `section_help(title, id)` in `_macros.html` used with `{% call %}` (body = panel
  HTML); JS equivalent `antViews.helpBtn(panelId)` for a small inline (?) that opens
  the same panel. Column headers and labels get a plain circled **(i)** (`.info`,
  macro `col_info(text, glossary)` / `antViews.info(text)`) with a short hover or tap
  tooltip. No sub-labels in headers; their text goes into the (?) panel. Toggle JS
  and `#hash` opening (`antHelpOpen`) are in base.html. Older pages still use
  `doc_block`; migrate them to this pattern when touched.
- **Help → hover `ⓘ`.** Verbose/explanatory prose hides behind a small `ⓘ`
  with a `title=` (the `.info` class is defined sitewide in `base.html`; `title`
  text must contain no `"`). Used in every view; plot.html also has a local
  `info(text)` Jinja macro. Prefer this over inline `<small>` help blocks.
- **Utility classes** (base.html, DRY — don't inline colors): `.muted`
  (`--text-muted`), `.muted-2` (#94a3b8), `.mono` (monospace).
- **Remember selection state across reloads — `window.antRemember`** (base.html,
  `localStorage`): `get(key)` / `set(key,val)` / `bindSelect(sel,key)`. Selection
  state that ISN'T in the URL (dropdowns, filters) should survive a reload — use
  this everywhere. Already applied to: `/genesets` group tab + study/experiment
  + starCAT catalog/category, the variability heatmap tag/statistic/source/
  colour-scale, the gene-atlas organism/cell/tissue filters + heatmap metric.
  Per-browser, not per-account.
- **View switches carry state.** When switching views, keep as much stable as
  possible. The plot **geom switcher** carries the address-bar params PLUS the
  settings-panel values (`readSettingsFromForm()` — palette/color_scale/size/
  fonts, which `syncURL` does NOT write to the URL), so colormap etc. don't
  reset on switch. Base it on `location.search`, not `buildUrl()`, so
  auto-prefilled defaults (a smart `y`) don't leak in and misroute the mode.
- **Topbar brand logo** is `web/topbar_logo.png` (a dark-recoloured crop of the
  Authelia login mark), base64-inlined once and exposed as the Jinja **global**
  `topbar_logo` (`templates.env.globals`), so `base.html` shows it on every page
  without per-route context.
- **Topbar on narrow screens (<= 640px)** is one row: logo plus a menu button
  (`#nav-toggle`, `aria-controls="topbar-menu"`, `aria-expanded`). The nav
  links, version, Help and the flattened user menu (with API) live in
  `#topbar-menu`, which is `display: contents` on desktop (so the desktop row
  is unchanged) and a drop-down panel on phones. Escape, an outside click, a
  link click or widening past 640px closes it. There is no top-bar gene search
  any more; the `genes` link goes to `/genes`, which has its own lookup form.
  The dataset page's own collapsed action button reads "actions", not a
  second hamburger.
- **Data-assistant chat widget** (`#chat-launcher` + `#chat-panel`, bottom-right)
  is in `base.html` so the conversation follows the user across pages. It is
  **gated entirely client-side on `GET /api/chat/status`**: the launcher is
  `display:none` until the server reports `{enabled:true}` — a deployment with
  chat off shows no affordance (markup is present but hidden; a test asserts
  this). State (`session_id`, open flag, a 50-msg transcript mirror) lives in
  `sessionStorage` so it survives the full-page navigations the assistant
  triggers; the authoritative transcript is the server's (keyed by Authelia
  user × session). Streams over `fetch`+SSE (`POST /api/chat`), renders `token`
  bubbles, and executes `action` frames — `navigate` (sets the re-open flag then
  `window.location`) and `set_plot_param` (via an optional
  `window.__antApplyPlotParam` hook, not yet built). **The whole server half
  (`/api/chat*`, OpenRouter, session store, the enable gate) is ant-serve's**,
  not here — see `doc/DESIGN_chat_assistant.md` §9 for the locked wire contract
  this client is built to.
  - **One-time consent gate** (`#chat-gate`): blocks the chat body behind a
    data-handling notice until `POST /api/chat/ack`; driven by
    `GET /chat/status.acknowledged` (server-side per-user). `/privacy` has the
    full note.
  - **Per-message feedback flag** (`.msg-flag` ⚑ on each assistant bubble):
    category + optional comment → `POST /api/chat/feedback {session_id,
    turn_seq, round_idx, category, comment?}`; stays red once flagged.
    Requires per-message ids from the `message_start` SSE frame + history rows.
    Triaged at `/admin/feedback`.
- **dataset.html top layout (2026-06):** breadcrumb → **`.id-strip`** (copy-paste
  ids: data_project · group_id · dataset_id · db_id, each `user-select:all`, hover
  explains; admin **`.id-edit`** ✎ at the right) → **`.ds-prov`** provenance row
  (access / source / license / contributor + doi/pubmed/accession/link badges) →
  **title-row** (`<h1>` title + greyed `.basename` when it differs + visualize ▾)
  → no description text at all above the tab bar (Mark, 2026-10-22): the `.md`
  `description` key and then the body (minus its leading `# title` heading)
  render only in the Overview tab (`_overview_markdown`; deposits often write one
  paragraph to both, sometimes edited in one place only): a description the body
  holds is dropped, else each body paragraph the description holds is dropped;
  "holds" = contained, or at least 90 percent of the characters matched in order
  (`_text_covered_by`, exact containment only under 40 characters) → context pills → stats row. **No-duplication:**
  headline biology lives ONLY in the context pills; the **Biology** section shows
  only condition/sex/age/strain; provenance + external links live ONLY in the top
  `.ds-prov` row. Each datum appears once; sections hide when empty.
- **Tab bar order (2026-10-13):** `Overview` (only when markdown) · `Data` ·
  `Numerical` · `Categorical` · `Genes` (only when n_genes > 0) ·
  `Genesets` (only when module scores) ·
  the **Facet group** (when the dataset has facets or module scores; see
  "Facet tab group" below) ·
  `Differential expression` (only when DE data) · `Papers` (only when linked
  library papers) · `Artefacts` (always, 2.47; preview view `artefacts`, `.tab-empty` and "No artefacts for this
  dataset." without any) · `Downloads` (always; `loadFiles` adds `.tab-empty` and the
  panel says "Nothing to download for this dataset" when `/files` lists nothing
  and there are no paper PDFs) · `Caveats` (always, pinned far right, **never
  default-active**; level colour with caveats, `.tab-empty` plus "No caveats
  recorded for this dataset" without). `.tab-empty` (dataset.html) is the
  empty-state style: muted, no highlight, still clickable; it is not the
  `vw-preview` approval marker. Only the button order matters; panel DOM order
  is independent, and `activateTab` falls back to Overview, then Data.
  **Embeddings** live inside the Data tab as a card (`#obsm_keys`), not a
  separate tab. **Resources** and a standalone **Differential expression** tab
  exist when data is present (DE tab is conditional). Deep-links `#tab-embeddings`
  / `#tab-resources` are NOT implemented — those were a spec artifact; the
  actual anchors are `#tab-data` (for embeddings) and `#tab-de` (for DE).
- **Plot-page caveats (plot.html, all geoms incl. the DE ones)** are collapsed behind `#cv-toggle` (triangle icon + count, coloured by the highest level, `aria-expanded`, panel `#cv-panel`, closed by default; label hidden below 480px). The dataset page's own card is unchanged. Nothing requires high caveats to stay open.
- **Known caveats card** (dataset.html): rendered above the tab bar when
  `/info` returns a non-empty `caveats: [...]`. The Python side (`dataset_view`
  in `browser.py`) reduces the list to a `caveats_summary = {n, max_level}`
  via `_summarize_caveats(...)` — max-severity reduce is cleaner in Python
  than Jinja, and the same helper feeds `get_collection_datasets` so the
  folder page's `⚠` marker uses the identical ranking. Severity accents
  (`.caveat-{low,medium,high}`) are subdued monochrome tints + a coloured
  left border only — no garish fills; match the page palette. The folder
  page's marker (`.cv-mark.lvl-{low,medium,high}` in `folder.html`) links
  into `#tab-overview` so the caveat card is in view on landing.

## Catalogue landing page (`/`, index.html)

The default view is a light **list**, one entry per collection: line 1 the
paper title (fallback: collection name), line 2 authors, journal and year,
dataset and cell counts and the copyable collection code. On the right only
**tags that differ from the catalogue default** (`landing_facet_defaults`: the
most common value per field when at least 30% of collections carry it; more
than two unusual values in a field collapse into "N regions" and the like;
control values never become tags), plus a muted lock for non-public access.

A prominent search box on top filters client-side over a folded (lower-case,
accent-free) text per collection that browser.py builds in
`_collection_search_text` from every relevant field, including the titles,
basenames, ids, DOIs and markdown of the datasets inside. Multi-word queries
are AND. Facet chips (organism, cell type, tissue, method, disease) sit behind
a "Filters" button, OR within a group and AND across groups, counts reflect
the other active filters. State lives in the URL (`?q=`, `org=`, `cell=`,
`tissue=`, `method=`, `disease=` with `|` separators, `sort=`, `view=`) and in
antRemember. The original column table is still there behind **Table view**;
it reads the controller's matches from `window.__antVisibleGroups`, keeps its
own header popovers as extra filters, and the sort selector drives it via
`window.__antTableSortMode`. List items set `display:flex`, so the template
carries a `[hidden] { display:none !important }` rule; keep it.

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
- **Collapsible observation columns** on the dataset page: a few **favourites**
  (curated `top_cat`/`top_num`, else the first 8 columns) show first; the full
  set is collapsed behind a **`#obs-expand`** toggle that reveals **`.obs-table`**
  — a 2-column-on-wide grid of `.ocol` rows (type icon · name link · `.ostat`)
  with a filter box. (Replaced the old `::`/`__` prefix-grouping chips.) The
  `.ostat` currently shows the type word; richer per-column stats (n_unique /
  numeric range) await an ant-serve obs-summary endpoint.
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

**Tests** live in `tests/test_web_browser.py` (~150 tests, the whole web
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
duckdb/numpy/pandas/h5py/scipy). `uv.lock` in the repo root is tracked; never
delete it. If `uv run` changes it, restore it with `git checkout uv.lock`.

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

Default is to **request** the endpoint from the API session and build the
client. Occasionally you'll build a server endpoint yourself when the user asks
and the substance is small (e.g. the `/databases/{id}/files` + `/download/{kind}`
downloads this session) — the sessions share the worktree so it's possible;
when you do, **claim it via pika first** so you don't collide, and flag the
shared-file edits.

Sessions share one git worktree + HEAD, so: **branch before committing**
(`web/<topic>` for UI work), commit eagerly (keeps `git checkout` safe for the
other session), FF-merge to `main` + delete the branch + push when shippable.
Coordinate via pika — the API session reads as **`anthive`** on this host
(`pika message send --to anthive --from anthive-web …`; `pm` = check inbox
with `pika message list --me anthive-web --all`).

**Open ant-serve requests the client already consumes (graceful fallback until
they land):** `GET /genesets/by-gene/{gene}` (find-by-gene search);
`GET /module-variability/dimensions` + a **donor-level (pseudobulk)** statistic
(the pseudoreplication fix) + the NaN-500 fix; user-set **`xmin/xmax/ymin/ymax`**
axis limits on continuous-axis plots; biology-friendly **descriptions** on
starCAT modules/scores (`.description` on `/catalogs`). Wire each up when shipped
— no client change needed for most (the fallback path already renders).

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
  (`config/examples/nginx-vhost.{authelia,public}.conf.example`).
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

## Effect bar, ranked shifts, genes tab (2026-10)

- **One effect bar for the site** (`_effect_bar.html`, loaded by `base.html`, `window.antEffectBar`): fixed scale per unit (log2FC two-group +/-1 with a faint 10 percent tick, per Braak stage and per decade +/-0.3, per e4 allele +/-0.5; SD units the same), arrowhead when capped, warm up / cool down / grey not counted, filled = cohort q < 0.05, whisker = 95% CI (forest) or min to max (summary tables). Used by `V.forestHtml`, the fxf forest and anchors in `_facet_effects.html`, and the replicated genes / gene sets tables in `_view_facet.html`. No page draws its own effect marks; point size by donors is gone. `antEffectBar.pointStats` gives the donors cell ("n_test vs n_ref" for two-group facets), the small marker (smaller group < 10, trend n < 30, served `small` as fallback) and `informative`.
- **Geneset "What shifts it"**: matrix cells show the module result only; "Ranked shifts" (per scope, sorted by `p_pc`, top 5, rest folded) sits above the matrix and opens the cell on click; member genes are one line per facet under each matrix (`block.after`). API additions: `family_rank`/`family_size` on module variants, `min_effect`/`max_effect` on module top rows, `min_logFC`/`max_logFC` on gene top rows, `exclude_sex_chromosome` for the sex facet member counts.
- **Geneset tabs**: `Genes` (#genes) lists members with their own facet calls, provenance sits in the header (`_geneset_provenance`), the Donor variability tab is `hidden` on gene and gene set pages until panel 3 exists.

## Genes landing and expression grouping (2026-10)

- `/genes` shows "Your favourites" (client-side from `antSettings.favouriteGenes()`, localStorage only, signed-in users only), "Examples" (`LANDING_EXAMPLE_GENES` in browser.py, single genes) and "Compare several genes" (own entry, expression-only heatmap; comma-list pages carry `#gv-multi-note`).
- Gene panel 1 groups by cell type across all datasets (no scope split). Dataset category tags come from `VIEW_CATEGORIES` / `_view_category` in browser.py (a human dataset with one dataset-level cell type that maps to a group sits in that cell type's row whatever its category, spatial and unclassified excepted; only post-mortem, xenograft and biopsy rows are pooled into the row summary, in vitro and bulk rows are listed, tagged and marked "not pooled"; bulk shows n/a for % expressing and n counts samples; datasets without a mapped cell type, spatial and unclassified go under "Datasets without per-cell cell types"). Panel 2 keeps its scope separation. Cell-type names are never hyphenated.

## Approved views, Expression tab, gene set search (2026-10-12)

- **Approved views.** `VIEW_REGISTRY` / `APPROVED_VIEWS` at the top of `browser.py` are the one list of reviewed data views (keys such as `datasets`, `genes`, `genesets`, `gene-expression`, `geneset-expression`; everything else is greyed). Jinja globals `view_approved(key)` (raises KeyError on unknown keys), `view_class(key)` (`vw-preview`), `view_tip(key, text)`, and the macro `review_banner(key)` in `_macros.html` (slim muted banner, `.review-banner`). Greyed nav links get `vw-preview` plus a "preview" badge, tabs get `vw-preview` (CSS `::after` marker), pages and tab panels start with the banner. Approving a view = add its key to `APPROVED_VIEWS`. Utility pages are outside the mechanism. Nothing is hidden or removed.
- **Expression tab** (`_view_expression.html`, first and default tab of `/genes/{gene}` and `/genesets/{s}/{e}/{n}`). One renderer `window.antExpression` for both; gene rows come from `/gene_stats_all` (`rank_mean`: share of the genes of the dataset with a lower mean log-normalised expression over all cells), gene set rows from `GET /genesets/{s}/{e}/{n}/rank?db_id=` (share of the other sets scored on the same cells with a lower mean scanpy score, ties half; peers are in scope by construction because modules only have scores on cells of their scope), 3 requests at a time, ranks fill in. Same dataset-to-block rule for both (single known cell type, not spatial/unclassified; in vitro and bulk listed but not pooled; the rest under "Datasets without per-cell cell types"). Cell types sort by the median percent once every rank is in. The earlier gene panel 1 is the `Detection` tab, the earlier gene set panel 1 is `Module score` (built lazily on first open, so the slow rank calls are not made twice). No backend change.
- **Gene set search.** `/genesets` embeds every set (`_geneset_search_rows`, from `GET /genesets`) as JSON and filters client-side (all words, case, accents and `_ - /` ignored; arrows, Enter, Escape; `?q=`).

## Facet DE tab (2026-10-12, round 3)

- The tab formerly called "What shifts it" is **Facet DE** (`data-vtab="shifts"` kept as the internal name, `data-hash="facet-de"`, `data-alias="shifts"` so old links work; registry keys `gene-shifts` / `geneset-shifts`). The facet headline sentence sits at the top of this tab. Two equal unnumbered sections: "Facet DE by cell type" and "Facet DE in other datasets", each with its own (?) panel.
- Matrix columns are always `V.GROUPS` (microglia first) in every scope block, on gene and gene set pages; untested cells are dashes, so columns line up (table-layout fixed). Facet rows keep the fixed `HUMAN_FACETS` / `XENO_FACETS` order.
- An opened cell is `V.unitsTable(rows, EFFECT)`: one row per dataset (name, id, cohort, donors, LFC for genes or Effect (SD) for gene sets, adjusted p, a muted "not counted" line). No forest, boxplot or donor plot there (`V.linksHtml` is still defined in `_views.html` for other pages).
- "Other datasets" (`V.otherDatasetsHtml`): replication entries whose `celltype_group` is not a column (mixed groups such as "glia (mixed)", `unmapped`, rare cell types), flattened to dataset x facet rows from the served forests, first 25 shown; plus one line for untested datasets built from `view_dbs[...].facet_exclude_reason` (spatial, proteomics, metacell sets, set aside by decision) and scope `in_vitro` / pure mouse. No extra API call.

## Dataset Facet GS tab (2026-10-13)

- (Superseded in layout by the Facet tab group below: Facet GS is now the GS pill of that group.) The dataset page's Genesets tab (`data-tab="modules"`, `#modules`) holds only the rank table (`#obs-tbl-modrank`, `/module-ranks`). The per-cell association explorer (`_module_view.html`, `mv_ctx.id` `mv-modules-tab`) moved to its own tab: **Facet GS** (after Genesets and Facets) (`data-tab="facet-gs"`, panel `#tab-facet-gs`, hash `#facet-gs`, filter state `#facet-gs&mv=...`), shown under the same `n_module_scores` condition. Heading "Gene set scores by annotation column" with the (i), no count.
- Not approved: registry key `dataset-facet-gs` (tab `vw-preview`, panel starts with `review_banner`).
- Lazy: `mv_ctx.lazy_tab = 'facet-gs'` makes `_module_view.html` wait for `anthive:tab-activated` with that name (or an already active panel) before fetching `/module-variability`; the spinner is `data-tab-loading="facet-gs"` and is dismissed with the same name. Pages without `lazy_tab` load at once.
- Aliases in `handleHash`: `#modules&mv=...` and the legacy `#tab-modules-mv=...` open Facet GS and are rewritten to `#facet-gs&mv=...`; `#tab-facet-gs` opens Facet GS, `#tab-modules` opens Genesets; bare `#modules` stays on Genesets. `datasetUrl()` in the explorer (gene set pages) links `#facet-gs&mv=...`.

## Facet tab group (2026-10-13)

- Facets and Facet GS are one grouped tab, `<span class="tab-group" id="tab-group-facet">` in the bar (a span so the bar stays one flat `<div>`), holding three plain `.tab-btn` buttons the existing tab wiring treats as ordinary tabs: the label **Facet** (`data-tab="facets"`, `.tab-group-label`, panel `#tab-facets`, hash `#facets`), the pill **Gene** (`data-tab="facet-gene"`, `.tab-sub`, panel `#tab-facet-gene`, hash `#facet-gene`) and the pill **GS** (`data-tab="facet-gs"`, `.tab-sub` plus `vw-preview`, panel `#tab-facet-gs`). CSS in dataset.html: `.tab-group` draws the tab outline, `.tab-group:has(.tab-btn.active)` makes it the open tab, the active pill is filled. On phones the bar scrolls inside itself as before (`flex: none`).
- `#tab-facets` keeps the description: `#ds-facets` (heading, `ds-facets-about`, `#fx-unit-line`, `#fx-changed`, Analysed facets, Not analysed yet). `#tab-facet-gene` holds `#ds-facet-effects`: everything from `#facets-effects-head` on (fx_intro, effects blocks, `#fx-untested-extra`, `#fx-excluded`, `#fx-not-tested`) plus `fx_script()`, content unchanged. Both need `show_facets_tab`. With module scores but no facets the label is `.tab-empty` and opens `#ds-no-facets`; the Gene pill is not rendered. GS keeps its lazy load (`mv_ctx.lazy_tab`) and aliases.
- Hash routing: `#facets`, `#facet-gene`, `#facet-gs` (plus the Facet GS aliases above), `#ds-facets` and `#ds-facet-effects`; any other hash that is the id of an element inside a tab panel (for example `#facets-effects-head`, `#fx-...`) opens that panel and scrolls to it. Clicking a pill writes its hash.

## Facet > Gene matrix (2026-10-13)

- The Gene sub-tab (`#tab-facet-gene`, registry key `dataset-facet-gene`, not approved: the pill is `vw-preview` and the panel starts with
  `review_banner`) shows a facet x cell type matrix from `_facet_gene_matrix.html` (`fxg_matrix(matrix, by_facet)` markup,
  `fxg_script(db_id, browse_base, default_embedding)` once at the end of the panel so the markup carries no script). Data:
  `browser._facet_gene_matrix(summary)` (in `facet_tests.matrix`): columns are the unit strata ordered by cell type group like `V.GROUPS`
  on the gene page (`_FXG_GROUP_ORDER`, then `celltype_groups.groups()` order), rows the facets with a tested or descriptive unit in
  `_FXG_FACET_ORDER`; facets with no test anywhere go to `untested_rows` (one line under the matrix). Cells: `tested` (n at q < 0.05,
  `n_up` / `n_down` from API 2.45, colour step `l0`..`l4` by `_fxg_level`), `descriptive` (below the p-value floor), `nt` with
  `_fxg_short_reason` ('too few donors', 'one value only', 'no pathology data', ...), `none`.
- Clicking a cell (one at a time, `.sel`) opens `#fxg-genes` under the matrix: `/facet-tests/genes?facet&cell_type&celltype_source&q=0.05&limit=2000`,
  columns Gene (link) | log2 fold change (`antEffectBar.barHtml` with the CI, scale from `scaleKey(facet,'lfc')`) | q | donors | plot icons
  (`antPlotIcons.row`), top 25 then "show all", search box, CSV (`format=csv`) and volcano (`/facet-tests/volcano.png`) links.
- Per facet a hidden box `fxg-help-<facet>` (the (i) after the row label): age / App NL-G-F notes, then per unit the contrast and donors,
  `fx_model`, `fx_sex_control`, flag pills, lambda, `fx_unit_notes` and the gene set counts per method (temporary home until donor-level
  gene set results move to GS).
- The earlier tables (`_fx_effects_block`, `#fx-table-ant_*`, `#fx-untested-extra`) sit in the hidden box `#fxg-full`, opened by the
  "full effects table" toggle in the heading; `#fx-...` links open the Gene sub-tab (anchor fallback in `handleHash`) and the box (`openTarget`).

## Facet > GS matrix, plot links, list scale (2026-10-13)

- The GS sub-tab (`#tab-facet-gs`, still `dataset-facet-gs`, preview) now shows the donor-level gene set results in the Gene layout:
  `browser._facet_module_matrices(summary)` (in `facet_tests.module_matrices`) builds one `_facet_gene_matrix(summary, method, source)` per
  variant in `_FXG_MODULE_VARIANTS` order (pseudobulk | cell mean scanpy | cell mean UCell); the default is the first the dataset has. Cells
  count `modules[].n_q05` / `n_up` / `n_down` (API 2.45: q_param at >= 30 residual df, else q_perm, the `select_p` rule of
  `facet_module_results`). A segmented control (`.fxg-variants`) swaps the `.fxg-matrix-wrap[data-variant]` tables; "showing: ..." stays.
  Per facet boxes `fxs-help-<facet>` carry the module model, permutations, flags, lambda, notes and the per method counts (removed from the
  Gene boxes `fxg-help-<facet>`). Both kinds add the "grows with the number of donors" sentence.
- `_facet_gene_matrix.html` now has `fxg_styles()`, `fxg_matrix(matrix, prefix, kind, hidden)`, `fxg_help_boxes(rows, by_facet, kind)` and
  one list script `fxg_list_script(kind, root_id, list_id, db_id, browse_base, default_embedding, obs_columns, score_columns)` for both
  sub-tabs. The GS list reads `/facet-tests/modules?...&method&source&q=0.05&q_by=either&limit=2000` and keeps rows whose used q < 0.05;
  columns Gene set (link, muted catalog) | standardised effect (CI rescaled by std_effect / estimate) | q (title says model or permutation)
  | genes found / requested | donors | plots. `module_score_columns` (dataset_view) maps "Study/Experiment/module" to its per-cell score
  columns for the plot links.
- Plot links per row, for the row's facet and cell type: per donor first (`/api/facets/{facet}/donor-plot.png?...&db_id=` with `gene` or
  `module`+`method`+`source`, API 2.45 `db_id` filter), then per cell (descriptive) plots of the cells of that stratum (`subset=<celltype
  source column>:<cell type>`, none for `constant_all_cells`): violin (`x` facet column, `y` gene or score), ECDF and histogram coloured by the
  facet column, embedding coloured by the gene. Numeric facets: Braak uses `ant_braak_group` when present, otherwise a `rolling` trend and
  unsplit ECDF / histogram (the tooltip says so). Facet columns: `FACET_COLUMN` in the script.
- List bars are scaled to the rows shown (`listScale`: 95th percentile of |effect| and CI ends, rounded; longer bars end in the arrowhead):
  `antEffectBar` accepts a `{max, unit}` scale object for this. The header shows "(bar scale ±x)".
- The per-cell association explorer moved into the hidden box `#fxs-percell` ("per-cell associations (descriptive)" toggle in the GS
  heading): `mv_ctx.lazy_box` makes `_module_view.html` load on `anthive:help-toggled` (dispatched by base.html `setHelp`) for that box;
  `#facet-gs&mv=...` and the older aliases open the box in `handleHash`.
- The Facet tab group has a faint tint, token `--tab-group-tint` on `:root` in base.html (`#eef4f5`, dark value `#1d2a2e` under
  `prefers-color-scheme: dark` guarded by `:root:not([data-theme="light"])` and under `:root[data-theme="dark"]`); pills are white on it,
  the open group gains the full border and a 2px teal top line, the open pill stays filled.

## Other DE (author and custom) (2026-10-13)

- Stored DE studies (`__de_study` / `__de`) are shown under the facet matrices: gene-target studies at the end of the Facet > Gene panel,
  module-target studies in Facet > GS (before the per-cell box). Partial `_other_de.html`, macro
  `other_de_section(studies, kind, db_id, browse_base, default_embedding)` (reuses `fxg_styles`); registry key `dataset-other-de` (preview).
  Data: `browser._other_de_view(api_url, db_id, de_studies, obs)` (`other_de` in the dataset context), one
  `GET /databases/{id}/de/{study}/summary` per study (API 2.45: per (term, contrast) `n_sig` / `n_up` / `n_down` at padj < 0.05,
  `subset_filter`, `cluster_obs_column`). Rows are (term, contrast) labelled by `_de_row_label` ("3 vs all (cluster)", "K vs W
  (genotype)"), intercept rows dropped, rows after 20 behind "show all N contrasts"; one column for the cells the study covers
  (`_de_subset`: a single-key `subset_filter` on a categorical column becomes `subset=col:v1,v2`, else "study subset" with a note, or
  "all cells"). The term maps to a categorical obs column case-insensitively (else `cluster_obs_column` for cluster terms) for the per
  cell plots. Level from the method name (`_de_method_level`). The (i) per study: description, method, reference, covariates, subset,
  source, notes, why there is no per donor plot.
- A click opens the list under that study (`/de/{study}/terms/{term}/contrasts/{contrast}?padj_max=0.05&sort=padj&limit=2000`):
  gene (link; module studies plain) | effect (`effsize`, label log2 fold change or z by `efftype`, CI only when `estimate` equals
  `log2fc`) | adj. p | per cell plots (violin, ECDF, histogram by the term column, embedding; no per donor link). Header links: the full
  volcano and MA plot pages (`/plot/volcano|ma?study&term&contrast`). Bars use the same `listScale`.
- The Differential expression tab stays; it has a pointer line `#de-other-pointer` to `#ode-gene-head` / `#ode-module-head`.
- Gene page: `#vwg-ode` under the Facet DE tab ("Other DE (author and custom)", (?) panel, `review_banner('gene-other-de')`), filled
  from the same `/genes/{gene}/author-de` call as the Author DE tab (rows now carry `effsize`, `efftype`, `study_id`, `target`); hidden when
  the gene has no gene-target row. Gene set pages: not built, because no module-target study names its modules like a gene set (the
  stored module names are WGCNA eigengenes such as `MEred`).

## Per donor plot page (2026-10-13)

- `donor_plot_view` / `donor_plot.html`: breadcrumb and title row like the plot pages ("Per donor: PTPRG by Braak stage, microglial cell,
  L26/PSY"), the shared caveat button and panel (`_plot_caveats.html`, `plot_caveats_button` / `plot_caveats_panel`, also used by
  plot.html), an (i) box `dp-about`, a summary line from `/donor-values` (donors, cohort, estimate, q, counted), an "adjusted for
  covariates" checkbox (`adjusted=true`), PNG / SVG / PDF downloads and a link back to `#facet-gene` (`#facet-gs` for gene sets). The image
  is fetched as a blob so an API error shows as a sentence. The first plot icon of the Facet > Gene and GS lists points here.

## UI rules (binding, 2026-10-13 consistency sweep)

These rules come from the site audit (Fable, 2026-10-13) with Mark's overrides. Every change follows them.

1. One display label per entity in nav, h1, breadcrumb and tab: datasets, the collection name, the gene, gene sets, facets,
   library. Sentence case in h1 and tabs, lower case in nav. No raw vocabulary tokens (`10x_chromium`, `ant_sex`,
   `braak_threshold`) outside tooltips and the id strip: values go through `display_value` (Jinja filter, `_VALUE_LABELS`), facets
   through `_FACET_TEST_LABELS` / `facet_test_label`.
2. Page title format "<page> · anthive" in every `{% block title %}` (a test checks it). No em-dash anywhere; placeholders are
   "none" or an empty cell, never a dash.
3. One help device: a hidden `doc_block` opened by the (i) `doc_toggle` after the header, one visible essential line next to it.
   The (?) `section_help` panels render the same (i) glyph and box style. Column header (i) is a one-line tooltip. No numbered
   headers, no yellow notice boxes, no open-by-default details except the help quick start.
4. Preview state is the `review_banner` as the first element under the h1 (page) or at the top of the panel (tab), plus
   `vw-preview` on the tab; nothing else says "experimental". Empty tabs are `tab-empty` with a one-sentence panel. Earlier designs
   live in the "Older views" menu (`.vw-older`, key `older-views`), never in the primary tab set.
5. Tables: Name | Cells (or Samples for bulk) | Cells expressing (%) | Rank (%) | Std. dev. | Plots for expression; facet lists
   Name | Effect (bar, CI) | q | Donors | Plots. Unsigned metrics use teal #4f9a96, slate #6277a8 and sand #c2a05a; signed
   effects keep warm (up) and cool (down), always through the tokens `--effect-up` / `--effect-down` (bars) and
   `--effect-up-ink` / `--effect-down-ink` (text) on `:root` in base.html. Plot icons are the SVG `chart_icon` set
   (`antPlotIcons.SVG`: bar, line, umap, violin, donor) in one order: per donor, violin, histogram, ECDF, embedding. No emoji in
   links, menus or headings.
6. Matrices: rows are facets or contrasts in fixed order, columns are cell types in the fixed group order (microglia first); the
   corner cell and the section title name the unit ("genes", "gene sets", "cohorts"); a click opens one list under the matrix;
   colour by count only for donor-level results, one flat colour (`.fxg-cell.lcell`) and a "cell level, descriptive" pill for
   cell-level results, whose lists sort by effect with adj. p last.
7. Statistics wording: "q" for BH-adjusted p within one donor-level test, "adj. p" for author DE as supplied, "r" for replication
   across cohorts; never "padj", "q_param" or "q_perm" in visible text. A q of 0 reads "at floor". "donors" and "cells" are written
   out; "n=25" after a cell type means datasets and the tooltip says so. Per-cell results are always labelled "descriptive".
8. Counts in tab badges come from the same list the panel renders (the Facet badge is `facet_groups.n_listed`). One `.tab-count`
   style site-wide (base.html); overrides change colour only.
9. Every table is inside `.table-wrap`; the shared table renders at 390 px without overlap (names wrap, std column dropped below
   560 px). A page never scrolls horizontally.
10. Errors: one `error.html` (`_error_page`, and `install_error_pages(app)` for exceptions of page routes; JSON for /api and JSON
    clients); in-page failures use the `.plot-error` card pattern.
11. Prose: plain sentences, no "Label: value" constructions; stat strips as "6 facets · 88 datasets" (number first).

## Consistency sweep, what changed (2026-10-13)

- Other DE: cell-level studies (`_de_method_level` == 'cell') are flat and descriptive, lists sort by effect, adj. p last; rows are
  grouped per term (`study.groups`, collapsed above 5 contrasts, `.ode-term-toggle`), technical terms (`_DE_TECHNICAL_TERM`: batch,
  interaction, per line or sample) sit in a hidden box per study; term labels through `_DE_TERM_LABELS`; the MA link only where the
  method stores a mean (`de_has_mean`), the heatmap link moved from the DE tab into the Other DE list header.
- Facet label panel: `_facet_groups` gives Tested, Present not tested (with `not_tested_reason`; `ant_pmi` is a covariate) and
  Structural columns (`_STRUCTURAL_FACETS`, author clusters) in the hidden box `#facets-structural`.
- Dataset page: Data card values wrap, gene search count only while searching, caveat chips without raw tokens ("note", "object"),
  bulk datasets (`is_bulk`) say samples and hide "cells expressing".
- Per donor plot page sized to the frame with a cleaned caption; plot pages "<kind> in <dataset>" without emoji.
- `/datasets` redirects to `/`; the collection breadcrumb ends in the collection name; the library shows 50 papers at a time; the
  gene set Module score tab has a progress line.

## Artefacts (2026-10-19, REST 2.47)

- Registry key `artefacts` (not approved): the dataset page's **Artefacts** tab (between Papers and Downloads,
  `#artefacts`, count badge, `.tab-empty` when none), the collection page's `#coll-artefacts` section (artefacts of
  the collection and of its datasets, "Belongs to" column), and the artefact page `/artefacts/{group_id}/{scope}/{id}`
  (`artefact.html`, `?version=N` for an older version, the tombstone for a purged one). Shared table macro in
  `_artefacts.html`. Downloads stays raw h5ad + static only; artefacts never appear there.
- Every fetch is `_api_get_as_user` (identity headers kept): the API decides visibility, redacts `made_from`
  ("made from a dataset you cannot see") and logins. Two-stage purge (2.47, design 4.4): the purge button sits on
  live versions other than the latest (`#art-purge-form`, POST `/api/artefacts/.../v{N}/purge` with a reason),
  purged versions are greyed (`tr.art-purged`, "purged by X on D: reason", still linked and downloadable) with an
  unpurge button (`.art-unpurge`, POST `.../unpurge`), and admins get "delete permanently" on purged and failed
  versions (`#art-delete-form`, POST `.../delete` with reason + typed ref). Withdrawal (2.47.1): `#art-withdrawn`
  mark, admin `#art-withdraw-form` / `#art-unwithdraw` (POST `/api/artefacts/{g}/{s}/{id}/withdraw|unwithdraw`), and on
  a withdrawn artefact purge and delete buttons also on the latest (delete on live versions for admins). Purge controls show to the artefact
  owner, the latest version's uploader and admins (`_may_purge`); the API is the authority. A shown purged
  version carries `#art-shown-purged`; a deleted one the tombstone line. Help box `art-purge-help`.
- **Depositor text is never rendered raw**: titles through `safe_markdown.plain`, descriptions and change notes
  through `safe_markdown.render` (`lib/safe_markdown.py`: markdown-it-py with raw HTML off, allowlist
  re-serialisation, http(s)/mailto/relative links only, no backslashes, `rel=nofollow noopener noreferrer`,
  images dropped, headings demoted). Store bodies use the same renderer (see "Plot thumbnails in markdown").
- File links point at the API download route with `download`; the API answers a 307 to a signed attachment link or
  an attachment with `CSP: sandbox`, so active types (html, svg, notebooks) are never opened in the page. Only
  `.webp` previews are shown inline (`/api/.../preview/...`).
- Analysis collections (no datasets, `collection_kind: analysis` from `GET /groups/{id}`) render `folder.html` with
  `analysis=True`: no dataset table or filter, a one-line "Analysis collection by ..." note, the artefact list. Any
  other collection without visible datasets stays a 404.
- Help: topic `artefacts` (`lib/help_content/topics/artefacts.yaml`, section `#artefacts` of help.html); the (i)
  boxes `art-ds-help`, `art-coll-help`, `art-help`. Tests: `tests/test_web_artefacts.py`, `tests/test_safe_markdown.py`.


## Categorical tab, high-cardinality columns (2026-10-20)

A categorical column with more than `CATEGORICAL_PLOT_MAX_DISTINCT` (50) values loses its plot icons and lists its
`CATEGORICAL_TOP_LIST_N` (15) most common values (value, cells, share of cells with a value) in a closed
`details.cat-many`, under the line "<n> categories; show the 15 most common" ("about" above the exact-count limit
of /obs_summary, 5,000). The list comes from the page's one `/obs_summary?top_n=15` call (a GROUP BY per view with a
per-column LIMIT, memoised server side); no new route. The renderer is `antCatManyCell` in dataset.html (between
marker comments, run in node by the tests). Donor handling: `_categorical_value_policy` marks ant_donor,
ant_donor_uid and the replicate column ("1,499 donors; values not listed") and columns whose name matches
`_DONOR_LIKE_COLUMN` (donor, patient, projid, sample, batch, hashtag, ...); the page also hides a column whose top
values contain a value of a donor column. This hides values in the UI only: `/obs_summary` still returns the top
values of every column, donor columns included, as it did before. Plottable columns are unchanged (top-3 chips).

## ui switches: facets, caveats, API, MCP, jupyter (public mirrors, 2026-10-21; jupyter 2.47.1)

`ui.caveats: serious` (2.47.1): `ui.caveats` stays true for the pages, and `browser._shown_caveats` filters every
caveat list a page shows through `metadata.caveats_for_mode(caveats, _caveats_mode())` (level high and the known
defect kinds only). Any new place that shows caveats must go through `_shown_caveats`.

A site can switch parts of the UI off (`ui:` in anthive.yaml, rendered from site.yaml by ant-stack;
`config.get_ui_settings`). `browser._ui()` reads it once (tests set `browser._ui_cfg`; both web test files have an
autouse fixture that switches everything on), the Jinja global `ui` gives `{% if ui.facets %}` and friends, and
`window.antUi` carries the same flags for page scripts. A part that is off disappears and its pages answer 404
through the route dependency `_require_ui('<part>')`.

- **facets**: no `facets` nav link, no Facet tab group (label, Gene, GS) or `#ds-facets-pointer` on the dataset
  page, no facet API calls in `dataset_view` (facets, facet-tests, Other DE), no replicate unit row, no "facet"
  pill on `ant_*` column names (`col_label`, `formatColLabel` in plot.html and _module_view.html), no Facet DE
  tab on gene and gene set pages (no `/facet-effects` fetch; the gene set Genes tab lists members without facet
  calls), no `fx_gene_panel` in the old atlas. 404: `/facets`, `/facets/{name}`, `/facets/{name}/effects-table`,
  `/browse/{g}/{d}/plot/donor`.
- **caveats**: `caveats` is emptied in `dataset_view`, `plot_view`, `donor_plot_view` and the collection listing
  (`caveats_summary`), and the Caveats tab and panel are not rendered (not even the empty one).
- **api**: no API entry in the user menu, the version pill is a plain span (no changelog link), no
  notebook buttons (dataset page, plot page; the plot script uses `?.addEventListener`), no REST/notebook links
  on /about, no MCP-free AI/programmatic sections in help. 404: `/api-guide`.
- **mcp**: no `#mcp-menu-link`, no MCP text on help, /about or the API page. 404: `/account/mcp`, `/admin/mcp-links`.

Help: help.html wraps its own sections in `{% if ui.<part> %}`; the shared text in `lib/help_content` marks
whole topics and glossary terms with `ui: <part>` and passages with `data-ui="<part>"`, removed by
`help_content.strip_ui` through `_help_topic_html` / `_help_glossary_html` (hidden parts from `_ui_hidden()`).
Tests: `tests/test_web_ui_switches.py` (each switch off, all off, defaults on, glossary links of the remaining
pages resolve).

## Front door and dataset reading view (2026-10-26, preview)

- `/` is the biologist front door (`front_door.html`, shared pieces in `_front_door.html`: `fd_styles`, `fd_search`,
  `fd_dataset_row`). Counts, browse lists and the search use the datasets `/databases` returns for the visitor
  (identity forwarded), so access filtering is the API's. Rules of the search and the browse counts live in
  `lib/catalog_search.py` (shared with ant-serve `GET /search`); `FRONT_DOOR_EXAMPLES` in browser.py holds the two
  example questions (checked on the dev catalogue). Nav: `search` (key `front-door`, preview) links `/`, `datasets`
  links `/collections`.
- Dataset page tab bar in reading order: Overview, Data, Genes, Gene sets, Facets & DE (group `#tab-group-facet`:
  label `data-tab="facets-de"` opens its `data-default` sub-tab; pills Genes `facet-gene`, Gene sets `facet-gs`, DE
  studies `de`), Papers, Artefacts, Downloads, Caveats. With `ui.facets` off DE is a plain tab. Numerical, Categorical
  and the facet mapping (`#tab-facets`) are in `#ds-tech-bar` behind `#tech-toggle` ("Technical details", closed by
  default); `activateTab` opens it for a technical tab, `#technical` opens it alone, and open adds `body.ds-tech-names`,
  which shows the stored `ant_*` names (`.ann-raw`) next to the plain labels. All data-tab names are unchanged, so old
  hashes work. Data tab card `#ds-annotations` lists the `ant_*` columns with vocabulary labels (`annotation_label`
  Jinja global, from `lib/obscol_tags_vocab.yaml`), `ant_donor_uid` left out.
