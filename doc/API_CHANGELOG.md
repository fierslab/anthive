# Anthive REST API — Changelog

Versioned record of contract changes to `ant-serve`. The single source of
truth for "is my client still up-to-date?" is the **`API_VERSION`** string
in `bin/api/ant-serve` (currently surfaced at `/api/health.api_version`,
`/api/version.version`, and as `info.version` in `/api/openapi.json`).

### Versioning rules

* `patch` (2.2.x) — bug fix only, no contract change. Clients don't need to update.
* `minor` (2.x.0) — **additive**. New endpoints, new optional params with safe defaults, new optional response fields. Existing callers keep working unchanged.
* `major` (x.0.0) — removes / renames endpoints, changes response shape, tightens an existing param's constraints. Old clients break.

### How to check if your client is up-to-date

```sh
curl -s http://<host>/api/version
```

Returns a compact JSON with the version stamp, recommended antclient pin,
the canonical OpenAPI URL, the supported plot geoms, and the list of
deprecated endpoints. Cache the `version` field client-side; if it changes,
re-read `/api/openapi.json` to regenerate bindings (or read this file to
understand the diff).

The OpenAPI spec itself (`/api/openapi.json`) is auto-generated from
FastAPI route signatures, so it's **always** current — there is no risk of
the spec drifting away from the running code.

---

## 2.12.0 — 2026-06-22

### Added — `de_quadrant` plot geom (16th geom)

New `GET /databases/{db_id:path}/plot/de_quadrant` endpoint. Renders a
2-experiment effect-vs-effect scatter: one gene per point at
`(stat_x, stat_y)` where `stat` is the gene's DE statistic on each
axis. Quadrants encode concordance vs discordance; colour encodes the
4-way significance category (significant in X only / Y only / both /
neither, gated by `padj_threshold` independently on each side).

Query-param surface (in addition to the standard plot knobs):

* `study`, `term`, `contrast` — A side (lives on the path `db_id`).
* `db_y` (optional, default = path db_id), `study_y`, `term_y`,
  `contrast_y` — B side.
* `axis_x`, `axis_y` — one of `log2fc` (default), `signed_log10p`
  ( -log10(padj) * sign(log2fc) ), or `zeff`. Picked independently
  per axis.
* `padj_threshold` (default 0.05) — significance cutoff for the
  colour split.
* `n_label` (default 8) — top-N concordant + top-N discordant genes
  to label, ranked by `|stat_x * stat_y|`.
* `mode` — `scatter` (default) | `hex` (hexbin density variant).
* `log2fc_clip` — symmetric visual clamp applied to BOTH axes.

Cross-dataset comparisons are allowed only when both datasets carry
the same `organism` field in the obs catalog metadata; mismatch
returns 400 with a descriptive detail. Genes present on only one
side are dropped from the scatter (inner-join on `gene_name`), but
the caption reports the full set sizes (`|X|`, `|Y|`, union,
intersection) so callers can see how much got dropped. If the
intersection is empty, the endpoint returns 400 (not 500) with a
"no genes in common" explanation.

Colour mapping (with default `palette_cat='tab10'`): neither sig =
light grey (#bbbbbb), X-only = tab10[0] (blue), Y-only = tab10[1]
(orange), both = tab10[3] (red). Reference overlays: dashed
`y = x` (concordance reference) plus solid axis-zero crosshairs.
Bottom-right corner carries the Pearson r + Spearman ρ + n.

Caller impact: purely additive. Existing geoms keep working
unchanged. Clients tracking `/version.plot_geoms` will see
`de_quadrant` appear in the list. vark / pyvark need to extend
their geom enums to expose it.

---

## 2.11.1 — 2026-06-22

### Changed — prettier notebook params + real DE de-novo snippets

Patch release on top of 2.11.0. Same endpoint contract; only the
content of the generated `.ipynb` changes.

* **Multi-line `PLOT_PARAMS` / `RENDER_PARAMS`** in cell 3 — non-empty
  `dict(...)` calls now render one kwarg per line, 4-space indented,
  with a trailing comma on the last entry. Empty dicts stay on one line
  (`RENDER_PARAMS = dict()`). Easier to skim, easier to edit. The
  non-identifier-key fallback (`**{"weird.key": "value"}`) gets the
  same line-per-entry layout.
* **Real matplotlib snippets for all 4 DE geoms** (`volcano`, `ma`,
  `forest`, `de_heatmap`) — these previously emitted a comment-only
  stub pointing at `lib/plotting.py`. They now produce a ~15-line
  matplotlib snippet good enough to recognise the plot type. No
  significance overlays, no `adjustText` labels, no auto-zero-centred
  divergent palettes — those polish steps still live server-side; the
  PNG from cell 4 remains the canonical rendering.

Caller impact: none on the wire — only the rendered notebook content
changes. Clients that were grepping the cell-8 source for the string
`"lib/plotting.py"` need to drop that assertion.

---

## 2.11.0 — 2026-06-22

### Changed — generated notebook content overhaul (8-cell layout, split params, de-novo snippet)

The plot-to-notebook generator (`/api/notebook/{b64}/...ipynb`,
`/api/databases/{id}/plot/{geom}/notebook[.ipynb]`) now returns an 8-cell
notebook instead of the old 3-cell version. The endpoint URLs and
response headers are unchanged — only the rendered `.ipynb` content
changes.

* **New cell order** (8 cells total, types
  `[markdown, code, code, code, markdown, code, markdown, code]`):
  1. markdown — title + caption + JupyterLite persistence disclaimer
  2. code — connect to the anthive server (generic auth setup,
     `"pyodide" in sys.modules` detection for the in-browser micropip
     install path)
  3. code — `DB_ID`, `GEOM`, `PLOT_PARAMS`, `RENDER_PARAMS`
  4. code — render the plot via the API (PNG + caption inline)
  5. markdown — "Pull the raw data" intro
  6. code — fetch the data as a pandas DataFrame
  7. markdown — "Render the plot yourself" intro
  8. code — matplotlib/seaborn snippet re-rendering the plot from the
     DataFrame
* **`PLOT_PARAMS` (biology) vs `RENDER_PARAMS` (visual)** are emitted as
  two separate `dict(...)` calls in cell 3 and merged at the call site
  in cells 4 + 6:
  ```python
  img = c.get_plot(DB_ID, GEOM, **PLOT_PARAMS, **RENDER_PARAMS, format="png")
  ```
  Biology bucket covers selectors (`x`, `y`, `color`, `subset`, `layer`,
  `study`, `term`, `contrast`, etc.) + hypothesis-level thresholds
  (`padj_threshold`, `log2fc_threshold`). Render bucket covers visual
  tweaks (`width`/`height`/`dpi`, `alpha`, palettes, clip ranges,
  `n_label`, `window`, etc.). Unknown keys default to the biology
  bucket.
* **`dict(...)` syntax instead of `{...}`** for the params — reads
  better and lets the user kwarg-unpack cleanly. Non-identifier keys
  (rare) fall back to `**{"weird.key": "value"}` for that entry only.
* **Generic auth cell** — the auth helper no longer branches on
  Authelia / HTTP Basic / public; the active line
  `c = AnthiveClient(BASE_URL)` works for all four deployment cases.
  Inline comments point at the pyvark and vark CLI documentation
  (`codeberg.org/mfiers/pyvark`, `codeberg.org/mfiers/vark`). The
  `X-Anthive-Auth-Case` response header is kept for client telemetry.
* **JupyterLite detection** uses `"pyodide" in sys.modules` instead of
  the older `try: from vark import AnthiveClient / except ImportError:
  micropip.install` pattern — avoids the misleading "ImportError →
  install" code path when the real issue (outside JupyterLite) is that
  the user needs to `pip install pyvark` first.
* **De-novo snippet (cell 8)** — geom-specific matplotlib/seaborn
  starting point that re-renders an approximation of the figure from
  the DataFrame in cell 6. Eleven geoms get real code (`scatter`,
  `hexbin`, `violin`, `box`, `bar`, `histogram`, `ecdf`, `kde`,
  `kde2d`, `heatmap`, `rolling`). Four DE-driven geoms (`volcano`,
  `ma`, `forest`, `de_heatmap`) get a comment-only stub pointing at
  the server-rendered PNG from cell 4 and at `lib/plotting.py` as the
  reference implementation.

Caller impact: clients that consumed the notebook content positionally
(e.g. "cell 1 = data pull", "cell 2 = render") need to follow the new
cell order. Endpoint URLs, content type
(`application/x-ipynb+json`), and response headers (`X-Plot-Title`,
`X-Plot-Filename`, `X-Plot-Caption`, `X-Anthive-Auth-Case`) are all
unchanged.

---

## 2.10.0 — 2026-06-22

### Added — three new knobs on `/plot/de_heatmap`

`/plot/de_heatmap` gains colour-scale clipping, exclude-filters, and
axis sorting. All additive — existing queries continue to render
identically.

* **Colour-scale clipping**: `?vmin=N`, `?vmax=N`, `?vmin_quantile=Q`,
  `?vmax_quantile=Q` (semantics mirror the existing `/plot/hexbin`
  knobs). Quantiles are computed over the finite cell values of the
  pivoted matrix that's actually being coloured.  On the
  auto-divergent palette: setting only `vmax` mirrors to `-vmax` for
  `vmin` (keeps zero centred); setting both lets the user choose an
  asymmetric range; quantiles are honoured asymmetrically too (the
  user's call).
* **Negative-prefix filters** on `?contrasts=` and `?genes=`:
  * `?genes=-APOE` — every gene except APOE (include-all baseline,
    then drop negatives).
  * `?genes=A,B,-A` — start from `{A, B}`, drop A → `{B}`.
  * A negative pointing at a value that isn't in the data is a
    silent no-op (no 404).
  * The "none of contrasts/genes matched" 404 only fires when explicit
    *positives* were given and missed; pure-negative queries fall back
    to include-all.
* **`?sort=LABEL`** + **`?sort_desc=true`**: if `LABEL` matches a
  column (gene/module), the rows are reordered by that column's
  values; if it matches a row (contrast), the columns are reordered
  by that row's values. NaNs always sort to the end. Mis-matched
  labels return `400` with the closest suggestions. Overrides any
  positional ordering from `?contrasts=` / `?genes=`. No-op when the
  matrix has only 1 row or 1 column.

The `X-Plot-Caption` header now includes the active clip range,
quantile band, exclude tokens, and sort note when these are used.

Caller impact: purely additive — older clients keep working unchanged.

---

## 2.9.2 — 2026-06-22

### Added — biology-keyed filename in the notebook URL, plus a no-render `/meta` descriptor

The base64-encoded-params notebook endpoint now accepts the biology-keyed
filename as a real URL path segment, so JupyterLite (which names the
saved file after the URL basename, ignoring `Content-Disposition`)
stores it under the descriptive basename instead of literal `plot.ipynb`.

* **New route** `GET /notebook/{b64params}/{filename}.ipynb`
  — `filename` is purely cosmetic (the server still drives content
  from the b64 blob) and must match `[A-Za-z0-9_-]{1,100}`. Otherwise
  identical behaviour to the old fixed `/plot.ipynb` route.
* **New route** `GET /notebook/{b64params}/meta` — returns just
  `{title, filename, filename_truncated}` for the encoded params,
  WITHOUT rendering the plot. Pure-Python / param-only, typically
  <50 ms. Lets clients pre-compute the biology-keyed filename before
  opening JupyterLite. Field kinds are not resolved against the DB
  (this is the static-descriptor path), so for scatter/hexbin with no
  explicit `x` the title's embedding falls back to "embedding" — the
  filename token is still stable.
* **Back-compat**: the legacy `GET /notebook/{b64params}/plot.ipynb`
  URL keeps working unchanged (registered as a second decorator on
  the same handler). The legacy query-string
  `GET /databases/{id}/plot/{geom}/notebook[.ipynb]?…` URL is also
  unchanged.

Caller impact: purely additive.

---

## 2.9.1 — 2026-06-22

### Changed — hexbin plots draw one silhouette instead of a per-hex grid

`/plot/hexbin` (and the embedded hexbin path used by `/plot/scatter` with
`geom=hexbin`) no longer outlines each individual hex. Instead, a single
thin black line traces the perimeter of the union of all filled hexes;
disconnected regions each get their own outline, and internal holes get
their own inner outline. Affects all three colour modes (density,
numerical / gene, categorical).

Pure visual change — no contract change to query params or response
headers. Hence `patch`.

---

## 2.9.0 — 2026-06-22

### Added — biology-keyed `X-Plot-Title` and `X-Plot-Filename` response headers

Every `/plot/{geom}` response (image AND data formats) and the
`/plot/{geom}/notebook` endpoint now carry two new headers alongside the
existing `X-Plot-Caption`:

* **`X-Plot-Title`** — a short, human-readable one-line title that
  describes **what** is plotted (biology), NOT the render knobs. Examples:
  * `S25/V6A: P2RY12 expression on X_umap`
  * `S25/V6A: cell density on X_umap - subset patient_geno_short=L276`
  * `S25/V6A: Sex_donor / F (volcano)`
  * `S25/V6W: DE matrix - patient_geno_short_relevel_sexreg`
* **`X-Plot-Filename`** — a filesystem-safe basename (snake_case,
  `[A-Za-z0-9_-]` only, no extension, capped at 80 chars). Examples:
  * `S25_V6A_scatter_P2RY12`
  * `S25_V6A_hexbin_density_patient_geno_short_L276`
  * `S25_V6A_volcano_Sex_donor_F`

Both headers are ASCII-safe (latin-1, same encoding pass as
`X-Plot-Caption`). When the filename gets truncated to the 80-char cap,
an extra header `X-Plot-Filename-Truncated: true` is emitted.

`Content-Disposition: attachment; filename="…"` (CSV/TSV plot data and
the notebook `.ipynb`) now uses the biology-keyed filename instead of
the old `{db_id}_{geom}` heuristic — saved files reflect what they're
of, not what knob produced them.

The notebook's in-cell H1 markdown (`# {db_id} · {geom}`) was also
changed to use the biology-keyed title.

**Title templates by geom** — see `lib/plotting.build_plot_descriptor`
for the canonical mapping (per-geom string template + filename recipe).

**Caller impact**: purely additive. `X-Plot-Caption` semantics are
unchanged. Clients still using the older `Content-Disposition` filename
will start receiving a more descriptive basename — adjust hardcoded
filename parsing accordingly (no schema change).

### Cache shape

The in-memory plot LRU now also stores `title` / `filename` /
`filename_truncated`. Older cache entries (in flight at restart, or
serialised in alternative implementations) without these fields fall
back to rebuilding the descriptor from the raw `PlotRequest` — graceful
degradation, no error.

---

## 2.8.0 — 2026-06-22

### Added — plot-to-notebook endpoint

New endpoint `GET /databases/{db_id:path}/plot/{geom}/notebook` returns a
JupyterLite-compatible `.ipynb` (nbformat 4) for any plot URL. Accepts the
**same query-param surface** as the matching `/plot/{geom}` endpoint
(`x`, `y`, `color`, `study`, `term`, `contrast`, `padj_threshold`,
`log2fc_threshold`, `log2fc_clip`, `neglog10p_clip`, `logmean_clip`,
`n_label`, `palette_*`, `transform_*`, `bar_mode`, `normalize`, `facet`,
`group_by`, `color_scale`, `layer`, `subset`, `sample`, `width`,
`height`, `dpi`, …) — everything that geom understands.

Response:

```
content-type:         application/x-ipynb+json
content-disposition:  attachment; filename="<db_id>_<geom>.ipynb"
x-anthive-auth-case:  authelia | basic | public | unknown
```

Notebook has exactly three cells:

1. **Markdown** — title + the same X-Plot-Caption the image carries +
   a JupyterLite persistence disclaimer + a pyvark[vark-config] tip.
2. **Code** — instantiates a `vark.AnthiveClient(BASE_URL)` and fetches
   the plot's source dataframe via `format=csv` into pandas.
3. **Code** — re-fetches via `format=png`, displays the image inline,
   prints the caption underneath.

`BASE_URL` is derived from `X-Forwarded-Host` (with `X-Forwarded-Proto`
fallback) so the notebook is portable across deployments.

The auth block in cell 2 is templated per request:

* `Remote-User` / `X-Forwarded-User` header present → Authelia case,
  username pre-filled in the commented escape-hatch.
* `Authorization: Basic` header present → HTTP Basic case, username
  pre-filled.
* `ANTHIVE_PUBLIC=1` env var on the server → passwordless case (no
  credentials needed).
* Otherwise → unknown case (gated, generic `<user>` / `<password>`).

The active (uncommented) line is always `c = AnthiveClient(BASE_URL)` —
works inside JupyterLite at this server's origin (cookie / basic carried
by the browser) and on workstations via `pyvark[vark-config]`
auto-discovery. The commented line is the explicit-password fallback for
users who copy the notebook elsewhere.

### Integration with JupyterLite

Open the notebook in the in-browser Jupyter via:

```
/jupyter/lab/index.html?fromURL=<urlencoded-notebook-URL>
```

JupyterLite materialises the file in IndexedDB; *File → Download* lets
the user persist changes off-browser.

---

## 2.6.0 — 2026-06-20

### Added — plot data export (`format=csv` | `format=tsv`)

Every `/plot/{geom}` endpoint now accepts `format=csv` and `format=tsv`
in addition to the existing image formats (`png` | `svg` | `pdf`).
Same URL + same params (x/y/color/subset/sample/transform/bar_mode/
facet/group_by/de selectors/etc.) — but the response is the
post-subset/post-sample dataframe the handler queried from the DB,
not the rendered image:

```
content-type:        text/csv; charset=utf-8
content-disposition: attachment; filename="<db_id>_<geom>.csv"
x-plot-caption:      data behind /plot/<geom> for <db_id>: <N> rows x <M> cols
```

The dataframe emitted depends on the geom:
* scatter / hexbin (embedding or XY) — cell-level: `cell_name, dim_0,
  dim_1[, color_field]` (or `x_field, y_field, color_field` in XY mode)
* violin / box / bar — cell-level: `cell_name, x_field[, y_field][, hue_field]`
* histogram / ecdf / kde — cell-level: `cell_name, x_field[, color_field]`
* rolling — cell-level: `cell_name, x_field, *y_fields[, color_field]`
* kde2d — cell-level: `cell_name, x_field, y_field`
* heatmap — aggregated: `y_field, *gene_columns` (per-group mean matrix,
  since that's what's plotted)
* volcano / ma / forest — DE rows: `gene, log2fc, padj, base_mean, ...`

Guarantees the export is consistent with the plotted image (no
client-side reconstruction via /query/sql, no drift between the data
behind the figure and what the user downloads).

### Added — `group_by` on `/plot/bar`

In-plot clustering of x-bars by a 3rd categorical (distinct from
`facet`'s small-multiples). Single axes; x re-ordered so all values in
one group are adjacent; vertical separator + bold group-label band
between groups. Used for Sierksma Fig 1B where mice should cluster
under their Village. Doesn't multiply bars; cap unchanged. Coexists
with `facet`.

---

## 2.5.0 — 2026-06-19

### Added — group/collection metadata endpoint

The web UI needed the prose body and the full frontmatter from each
group's `index.md` to render a per-collection landing page (citation,
accession, study description). Previously only `group`, `experiment`,
`authors`, `year`, and `default_dataset` were surfaced on the dataset
detail; the prose body was silently discarded.

* **`GET /api/groups/{group_id}`** — new endpoint. Returns the
  group-level metadata + prose body from the nearest `index.md`,
  plus a list of `{db_id, title, id}` for every dataset in the group.
  404 if the 3-char `group_id` doesn't match any dataset's catalog
  entry. Example payload keys:
  `group, group_id, experiment, authors, year, doi, pmid, accession,
   default_dataset, description, datasets[]`.
* **`GET /api/databases/{db_id}/info`** — the `collection` block now
  carries `group_id`, `doi`, `pmid`, `accession`, and `description`
  (markdown body of the nearest `index.md`) in addition to the prior
  fields. Existing callers keep working — additive only.

Trigger to rescan if you don't see the new fields after upgrading:
`POST /api/admin/rescan`. The metadata cache is per-process and
doesn't auto-invalidate on .md edits.

---

## 2.4.0 — 2026-06-12

### Added — 2-D scatter / hexbin / KDE pack for module-level exploration

Driven by a module-annotator request — making two-continuous-field
plots (e.g. WGCNA module score A vs B) actually readable on 235k+
cell datasets instead of a single dense blob.

* **`GET /databases/{db_id}/plot/kde2d`** — new geom.  2-D Gaussian
  kernel-density estimate over two continuous fields.  Fits the KDE
  on a configurable subsample (default 15,000 cells) and evaluates
  on a 200×200 grid.  Renders ``n_levels`` filled magma contours +
  optional thin white iso-density overlay.  Optional ``point_overlay``
  raster scatter under the contours.
* **Per-axis numerical transforms** on ``/plot/scatter`` (XY-mode),
  ``/plot/hexbin`` (XY-mode), and ``/plot/kde2d`` via new
  ``transform_x`` / ``transform_y`` query params.  Seven kinds:
    * ``linear`` (default)
    * ``log`` — natural log; non-positive values become NaN and are
      dropped.
    * ``log1p`` — ``log(1+x)``; the legacy ``log_x``/``log_y`` booleans
      still work as aliases for this.
    * ``signed_log1p`` — ``sign(x) * log1p(|x|)``.  Symmetric, defined
      for all reals, very close to ``asinh`` visually; cheap.
    * ``asinh`` — smooth (C∞) symmetric transform; tunable via
      ``asinh_scale``.
    * ``signed_sqrt`` — ``sign(x) * sqrt(|x|)``.  Gentler compression
      than log.
    * ``rank`` — percentile in [0, 1].  Gives every cell equal visual
      weight.
* **Auto-clipping the hexbin density colour scale** at user-supplied
  quantiles via new ``vmin_quantile`` / ``vmax_quantile`` params on
  ``/plot/hexbin``.  e.g. ``vmin_quantile=0.10&vmax_quantile=0.90``
  clips at the 10th–90th percentile of per-hex counts, restoring
  contrast in the bulk of the distribution when a few hexes are
  10× denser than the rest.  Default off (preserves prior behaviour).
  Density mode only; ignored when ``color`` is set.  The colorbar
  shows ``extend='both'`` arrows when clipping fires.
* **Optional marginal univariate KDE strips** on ``/plot/scatter``,
  ``/plot/hexbin``, and ``/plot/kde2d`` via ``marginals=true``.
* **Optional regression overlay** via ``regline=global`` (one line +
  R²/Pearson/Spearman/slope in a stats box) or ``regline=by_group``
  (one line per categorical-colour group, capped at 30 groups; only
  meaningful when ``color`` resolves to a categorical field).
  Server uses ``scipy.stats.linregress`` + ``spearmanr``.

### Changed

* ``plot_geoms`` in ``/version`` now lists 14 entries (added ``kde2d``).
* The hexbin density colorbar grows ``extend='both'`` arrows when
  auto-clipping is active.  No visible change when it isn't.

### Backwards compatibility

* All 2.3.0 endpoints, params, and response shapes unchanged.
* The legacy ``log_x`` / ``log_y`` booleans on scatter+hexbin still
  work; they're now aliases for ``transform_x=log1p`` / ``transform_y=log1p``.
* antclient 1.7.0 picks up everything new via ``**extra`` on
  ``get_plot``.  No client-API change required.

## 2.3.0 — 2026-06-09

### Added — differential-expression data endpoints

Read-only surfaces over the new `__de_study` + `__de` tables (see
`doc/DESIGN_var_and_de.md`). Datasets without any DE data return
empty lists / 404 cleanly.

* `GET /databases/{db_id}/de` — list DE studies on this dataset, with
  per-study row, term, and contrast counts.
* `GET /databases/{db_id}/de/{study_id}` — one study's metadata plus
  its (term, n_contrasts) list.
* `GET /databases/{db_id}/de/{study_id}/terms/{term}` — contrasts
  within one term, each with significance counts at padj < 0.05, 0.01,
  0.001.
* `GET /databases/{db_id}/de/{study_id}/terms/{term}/contrasts/{contrast}`
  — paged gene rows. Sortable by `padj` / `effsize` / `estimate`;
  filterable by `padj_max` and `abs_log2fc_min`; `limit` / `offset`
  for paging.
* `GET /databases/{db_id}/de/by-gene/{gene_name}` — every DE row for
  one gene across the dataset's studies / terms / contrasts. Forest-
  plot data source.

Ingestion is **not** an API endpoint — the analysis itself happens
offline via the `ant-de-ingest` CLI, which produces an
`<basename>.de.parquet` sidecar (backed up to S3 by `ant-sync`) and
re-imports it into the duckdb. `ant-convert` auto-re-ingests the
sidecar after a rebuild, so DE survives full duckdb rebuilds.

### Added — DE-driven plot geoms

Three new plot endpoints, slot into the existing per-geom shape:

* `GET /databases/{db_id}/plot/volcano?study=&term=&contrast=…` —
  log2fc on x, -log10(padj) on y, top genes labelled.  Tunable
  `padj_threshold`, `log2fc_threshold`, `n_label`.
* `GET /databases/{db_id}/plot/ma?study=&term=&contrast=…` —
  log10(base_mean) on x, log2fc on y.  Requires the underlying DE
  rows to carry `base_mean` (DESeq2 / edgeR ingest captures it
  automatically; Wilcoxon / GLMM rows don't — endpoint returns a 400
  with a helpful "switch to volcano or re-ingest from DESeq2" hint).
* `GET /databases/{db_id}/plot/forest?gene=…[&study=&term=&contrast=]`
  — one gene across every (study, term, contrast); point = log2fc
  (or estimate fallback), bar = 95% CI when available. Optional
  filter args narrow the view.

Updated `/version`:

* `plot_geoms` now includes `volcano`, `ma`, `forest`.

### Client compatibility

* `antclient >= 1.6.0` — still compatible. The new endpoints flow
  through `get_plot(geom='volcano', study=…, term=…, contrast=…)`
  via `**extra`; no client API change.
* All 2.2 endpoints still work — additive change.

---

## 2.2.0 — 2026-06-07

### Added — per-geom plot endpoints

The unified plot endpoint was split into one path per geom. Each
endpoint now declares only the Query parameters that geom understands,
so OpenAPI shows a focused operation per geom instead of one 30-knob
kitchen sink, and FastAPI 422 errors actually reference the param that
is wrong **for that geom**.

* `GET /databases/{db_id}/plot/scatter` — embedding **or** 2-axis (x, y both continuous). Knobs: `point_size`, `alpha`, `log_x`, `log_y`, `vmin`, `vmax`.
* `GET /databases/{db_id}/plot/hexbin` — same polymorphism as scatter, hex-aggregated. Knobs: `gridsize`, `mincnt`, `alpha`, `log_x`, `log_y`, `vmin`, `vmax`.
* `GET /databases/{db_id}/plot/violin`, `/box`, `/bar` — `x` (categorical, required), `y` (continuous, optional), `color` (categorical, optional). Bar adds `ci`.
* `GET /databases/{db_id}/plot/histogram`, `/ecdf`, `/kde` — `x` (continuous, required), `color` (categorical, optional). Histogram adds `bins`.
* `GET /databases/{db_id}/plot/heatmap` — `y` (categorical, required), `x` (csv genes, optional), `n_genes`, `zscore`.
* `GET /databases/{db_id}/plot/rolling` — rolling-mean trajectory. `x` (continuous, required), `y` (csv continuous, required), `color` (optional), `window`, `show_band`.

### Added — discoverability endpoints

* `GET /version` — version + canonical-contract pointers + plot geoms + deprecated-endpoints list. Designed for LLM clients to cache and diff.
* `GET /changelog` — this file, served as `text/markdown`.

### Added — connection-pool memory throttling

`/metrics` gained a `connection_pool.memory` block with the live state of the **progressive DuckDB memory throttling** (`total_budget_mb`, `min_per_conn_mb`, `last_per_conn_mb`, `next_acquire_per_conn_mb`, `throttle_ratio`, `temp_directory`). Solo queries get the full budget; concurrent queries share it, floored at the minimum per-conn cap. Past-budget queries spill to `temp_directory` rather than OOM-erroring.

### Deprecated — unified `/plot?geom=X`

`GET /databases/{db_id}/plot` (the old dispatcher with `geom` as a query parameter) is **deprecated** but still functional. The OpenAPI spec marks it `deprecated: true`. Scheduled for removal in **2.3**. New code should call the per-geom endpoints above.

### Removed

* The long-deprecated `palette` query parameter (back-compat alias for `palette_categorical`) was dropped from `_PlotRequest` — passing it is now silently ignored by the server, and antclient 1.6.0 dropped the kwarg entirely. Use `palette_categorical=`, `palette_continuous=`, `palette_divergent=`.

### Internal / safety

* Plot cache key now includes the geom (the geom moved from query string to URL path; without this two different geoms with the same params would have collided on the same cache entry).
* Cache key normalises numerics so `dpi=80`, `dpi=80.0`, `dpi=80.00` all hash to the same key; param order in the URL no longer matters.

### Client compatibility

* `antclient >= 1.6.0` — uses the per-geom URLs.
* `antclient 1.5.x` — still works via the deprecated `/plot?geom=X` shim. Pin to 1.5.x against a 2.1 server.

---

## 2.1.0 — 2026-06-05

### Added — speed signal + module score

* `/health` gained `mean_response_ms`, `p50_response_ms`, `n_samples` — fed by a 1024-slot ring buffer of non-probe request wall-clock. Designed for client-side server selection (`AnthiveClient.pick_fastest`).
* `/metrics` gained a `latency_ms` block with `p50`, `p95`, `p99`, `max`, `mean`, `n_samples`.
* `POST /databases/{db_id}/module_score` — Seurat-style module score on the fly from an arbitrary gene list. Knobs: `genes`, `layer`, `n_control`, `seed`, `subset`.
* `GET /databases/{db_id}/module_scores` — list pre-computed module-score obs columns.

### Added — geneset store

* `GET /genesets`, `/genesets/{study}/{experiment}`, `/genesets/{study}/{experiment}/{name}` — geneset registry (returns 404 if no geneset store configured).
* `POST /admin/rescan_genesets` — rebuild the geneset index from disk.

### Added — analytics sandbox

* `GET /databases/{db_id}/analytics/schema` — list tables + columns visible to the sandbox (call before constructing a query).
* `POST /databases/{db_id}/analytics/query` — sandboxed SELECT-only query. INSERT/UPDATE/DELETE/ATTACH rejected. Result persisted as Parquet under a `session_id`.
* `POST /databases/{db_id}/analytics/viz` — run a Python visualisation script server-side against the Parquet result of an earlier `analytics_query`. Sandboxed, AST-scanned, 60 s timeout.

### Added — admin coordination

* `POST /admin/release/{db_id}` — drop the r/o pool handles for a duckdb so an external writer can take the r/w lock.
* `POST /admin/restore/{db_id}` — clear the eviction so r/o queries resume.

---

## 2.0.0 — Initial release

* `GET /` — API info.
* `GET /databases`, `/databases/{db_id}/info` — discovery.
* `GET /databases/{db_id}/genes`, `/databases/{db_id}/cells` — gene + cell data retrieval.
* `GET /databases/{db_id}/layers`, `/databases/{db_id}/metadata_fields`, `/databases/{db_id}/embeddings`, `/databases/{db_id}/embeddings/{name}` — schema endpoints.
* `POST /databases/{db_id}/sql` — raw SQL execution (replaced by analytics sandbox in 2.1).
* `GET /databases/{db_id}/plot` — unified plot endpoint (split into per-geom endpoints in 2.2; deprecated).
* `POST /admin/rescan` — re-scan the database registry.
