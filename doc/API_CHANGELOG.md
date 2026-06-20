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
