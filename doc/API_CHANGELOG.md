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

## 2.40.2 — 2026-10-02

Patch release: no endpoint changes. `/me/mcp` snippets gain more clients.

- New `doc/MCP_CLIENTS.md`: setting up anthive in 22 AI clients (Claude Code,
  claude.ai and Claude Desktop, Codex, ChatGPT, Gemini CLI, OpenCode, Cursor,
  VS Code / Copilot, Windsurf, Zed, Cline, Roo Code, Continue, Goose, Amp,
  Kiro, Warp, JetBrains, Junie, LM Studio, Cherry Studio, Pi) plus the
  `mcp-remote` bridge, each with a link to the vendor docs; cloud connectors
  need a publicly reachable site.
- The help page links to the documentation on GitLab (`docs_base_url`,
  `site.docs_base_url`) instead of repeating it.
- The MCP links page gets setup tabs for Codex, Gemini CLI, VS Code, Cursor
  and mcp-remote, and links to the full guide.

## 2.40.1 — 2026-10-02

Patch release: no endpoint or response changes. Web UI and data layout only.

- Paper PDFs moved out of the h5adstore (`*/static/*.pdf`) into the paper
  library (records in `bdslab/anthive/library_store`, PDFs on S3 under
  `_library/papers/`); collections link to their papers with `papers:`.
- Dataset pages get a Papers tab (before Downloads), collection pages a Papers
  panel (after the datasets), each paper with the reason it is linked; paper
  pages `/papers/<citekey>` and a Library page `/library`; Downloads lists the
  linked papers' PDFs.
- One shared, more distinctive section-heading style on collection and
  dataset pages.
- Version button in the top bar (from 2.40.0) and an exhaustive help page
  covering MCP links, every MCP tool, prompt and resource, skills (install with
  the skills CLI), several servers, the paper library and programmatic access.

## 2.40.0 (2026-10-02)

Additive: adding papers through the API, and a two-way library sync.
Existing clients keep working.

- `GET /papers/upload/requirements`: `required`, `recommended`, `fields`
  (per field `description`, `example`, `type`, optional `vocab`), `roles`,
  `folders`, `entrytypes`, `max_pdf_mb`.
- `POST /papers/upload` (multipart): `metadata` (JSON object or `.md`
  text), optional `file` (PDF), `folder` (`lab|datasets|methods|reading`),
  `enrich` (default true, fills only empty fields), `dry_run`, `update`.
  201 `{status: created, citekey, folder, path, pdf_sha256, record,
  filled_fields, warnings, missing_recommended}`; 200 for `updated` and
  `dry_run`; 409 `{status: duplicate, existing_citekey, matched_on:
  doi|pmid|sha256|citekey}`; 422 `{status: invalid, errors: [{field,
  message}], missing_required}`; 413 PDF too large; 415 not a PDF; 403
  unless the caller is in `library_upload_groups` (default `[admins]`).
  `papers_for` is stored on the paper as `anthive_proposed_links`, never
  written to the h5adstore. Post-commit steps (index, reload, S3) only add
  warnings.
- `GET /library/status` gains `upload_index` (state of the background index
  update after an upload).
- MCP: `paper_upload_requirements`, `check_paper`, `upload_paper` (the last
  two only for uploaders; PDF capped by `mcp.max_upload_mb`).
- `ant sync library` (also part of `ant sync push|pull` unless
  `--no-library`): records via git, PDFs two-way against S3 with conflict
  copies and opt-in delete propagation, literature index publisher rule.
  New config keys: `library_upload_groups`, `library_upload_max_pdf_mb`,
  `library_git_committer`, `library_git_push`, `library_s3_autopush`,
  `library_index_publisher`, `mcp.max_upload_mb`.

## 2.39.0 (2026-10-02)

Additive: the paper library and literature search. Existing clients keep
working.

- **Library** (`lib/library.py`, `doc/LIBRARY.md`): `GET /papers` (filters
  `role`, `celltype`, `tissue`, `disease`, `method`, `cohort`, `organism`,
  `author`, `year`, `q`), `GET /papers/{citekey}` (record, notes, linked
  datasets and gene sets by role, related papers), `GET /papers/{citekey}.bib`,
  `GET /library.bib` (`citekeys`, `classic`), `GET /papers/{citekey}/related`
  (`kind`: explicit, cohort, celltype, tissue, disease, method, author, cites,
  cited_by; each relation with weight and evidence), `GET /papers/{citekey}/file`
  (the PDF, streamed or a presigned S3 link), `GET /library/status`.
- **Literature search** (`lib/library_index.py`, `doc/LIBRARY_INDEX.md`):
  `GET /search/literature` (`q`, `k`, `mode` hybrid|keyword|semantic,
  `section`, `citekey`, `year`) returns passages with citation, page, section
  and a highlighted snippet. Semantic and hybrid fall back to keyword search
  with a warning when the server has no matching query model.
  `GET /papers/{citekey}/pages/{n}` returns the text of one page.
- **Datasets**: `GET /databases/{id}/papers` (linked papers, source first) and
  `GET /databases/{id}/context` (`level` brief|full, `budget_tokens`): a
  background pack for LLM analysis (metadata, caveats, obs columns, module
  scores, source papers with methods passages, related papers).
- **Access**: `anthive_access: restricted` papers are admin-only. Calls from
  ant-mcp (or with `for_llm=true`) honour `anthive_llm`: `never` hides the
  paper, `summary_only` keeps metadata and summary but no passages, notes,
  abstract or page text.
- `/admin/rescan` reloads the library and the index. `GET /skill` works again
  (it looked in the wrong folder and the file was missing from images).

## 2.38.0 — 2026-10-02

Additive: several named MCP links per user. Existing clients keep working.

- `GET /me/mcp` gains `links` (one entry per non-revoked link: `id`,
  `label`, `url`, `active`, `created`, `rotated_at`, `expires_at`,
  `last_used`), `max_links` and `snippets_template` (client snippets with a
  `{url}` placeholder). The 2.37 top-level fields (`has_link`, `url`,
  `created`, ..., `snippets`) stay and describe the user's first link.
- New: `POST /me/mcp/links` (body `{"label": "..."}`, optional; 409 at the
  per-user cap `mcp.max_links_per_user`, default 5),
  `POST /me/mcp/links/{id}/rotate` (only that link's token changes),
  `DELETE /me/mcp/links/{id}`, admin `DELETE /admin/mcp-links/{user}/{id}`.
  Unchanged: `POST /me/mcp/rotate` (first link, created if none),
  `DELETE /me/mcp` and `DELETE /admin/mcp-links/{user}` (all of a user's
  links).
- `GET /admin/mcp-links` returns one row per link and adds `id` and `label`.
- The link store converts a 2.37 database (one row per user) in place on
  first open; existing tokens stay valid and are labelled "default".
- New config keys: `mcp.max_links_per_user`, `issue_tracker_url` (web UI
  "Report an issue" menu entry).

## 2.37.2 — 2026-09-28

Patch release: no endpoint or response changes.

- Startup no longer waits for the module-score and by-gene indexes; they
  build in a background thread. A site whose catalog is mostly S3-remote
  (a fresh deployment that has not pulled every dataset) used to spend
  minutes opening every remote database before answering its first health
  check, so `ant-stack up` gave up on it. Until the background build
  finishes, module-score listings can be incomplete.
- Container images are built from `git archive` and verified by
  `docker/check-image`; the 2.37.1 images accidentally carried untracked
  local files (Authelia user database, server log, a nested checkout) and
  were republished clean.
- `ant-stack check` flags missing S3 key files when `s3.enabled`, warns on
  an empty catalog and on datasets without a local `.duckdb`, and refuses
  authelia mode with a `public_url` host Authelia cannot use as a cookie
  domain (no dot, not an IP, e.g. `localhost`) or without https.
  `site.example.yaml` no longer defaults to that combination.
- `ant-stack up` recreates the Authelia container on every run, so a fixed
  config replaces a container stuck in crash-loop backoff.
- Caddy gets `default_sni <public host>` from ant-stack, so browsing to the
  bare IP of a containerised site no longer aborts the TLS handshake.

## 2.37.1 — 2026-09-25

Middleware fixes. No endpoint or parameter changes.

- **Endpoints no longer block each other.** 71 handlers that ran DuckDB,
  S3 or file IO directly on the event loop are now plain `def` (Starlette
  threadpool). `/info` stayed under ~0.2 s while a 500k-row `/cells` ran;
  before it stalled up to 12 s.
- **`/cells` JSON and CSV are streamed** in 5k-row chunks (chunked
  transfer encoding, no `Content-Length`). The body is byte-identical.
- **`?cache=miss` works on plot endpoints** as documented: it skips the
  cache lookup, re-renders, refreshes the entry and answers
  `X-Plot-Cache: BYPASS`. It is no longer part of the cache key.
- **Plot cache fingerprint includes the `.scores.parquet` sidecar**, so
  a rescored dataset no longer serves stale score plots.
- **503 instead of 500 while a writer holds a dataset.** `/cells`,
  `/obs_summary`, `/genes/{gene}`, `/gene_stats` and the other handlers
  whose broad `except` used to turn the pool's 503 (and explicit 404s)
  into 500 now pass them through.
- **`POST /admin/restore/{db_id}` reloads that dataset's metadata**
  (new `metadata_reloaded` field), so columns an annotator added show up
  in `/info` without a rescan.
- **`/metrics.connection_pool` shape changed** (operator diagnostics):
  the adaptive-throttling block (`total_budget_mb`, `min_per_conn_mb`,
  `last_per_conn_mb`, `next_acquire_per_conn_mb`, `throttle_ratio`) is
  gone, since per-connection `SET memory_limit` never worked (DuckDB
  settings are per instance). New: `retiring_instances`, `lru_closes`,
  `max_open_databases`, `memory.per_db_limit_mb`, `memory.threads_per_db`.
- **Pool connections are sandboxed by DuckDB itself**:
  `enable_external_access=false` with `allowed_directories` limited to
  the data folder, each dataset's own directory and the spill directory,
  then `lock_configuration=true`. `read_text('/any/file')` and similar
  fail with a permission error on every pooled query path.

## 2.37.0 — 2026-08-13

### Added — `obs_derive`: value-map a new obs column at view time

Extends the `api:` section with `obs_derive` — publish a NEW obs
column computed from an existing one via a value mapping, without
touching the h5ad or the underlying duckdb tables.

```yaml
api:
  obs_derive:
    braak:                       # new column name (public)
      source: braak_stage        # existing obs col
      dtype: numerical           # → obsnum view (categorical → obscat)
      map:
        "0": 0
        "I": 1
        "II": 2
        "III": 3
        "IV": 4
        "V": 5
        "VI": 6
      default: null              # optional; unmapped values → NULL
```

The mapping compiles into a `CASE WHEN` expression inside the target
view's SELECT. When the `source` column lives in the sibling __obs*
table (e.g. an obscat string → obsnum numeric derivation), the view
now `LEFT JOIN`s that table too.

Rules:
- `source` must exist in either `__obscat` or `__obsnum` at rebuild
  time; unresolvable sources silently skip (log-friendly).
- `dtype`: `numerical` → the derived col lands in `obsnum`;
  `categorical` → `obscat`.
- `map` values are coerced to `float` (dtype=numerical) or `str`
  (dtype=categorical). Values not in `map` fall back to `default`
  (or NULL if `default` is omitted).
- New col names go through `safe_ident` — must be `[A-Za-z_][A-Za-z0-9_]*`.

Writeable via `POST /admin/api-config/{db_id}` alongside the existing
`obs_rename` / `obs_hidden` / `obs_value_{rename,hidden}` fields.
Reversible in one API call (`{"obs_derive": {}}`).

Additive; existing views without `obs_derive` are byte-for-byte
identical.

---

## 2.36.1 — 2026-08-08

### Fixed — `/notebook` endpoint 500 on `bin_color` and other recently-added params

`_NOTEBOOK_PARAM_TYPES` (the coercion table that turns raw query-string
values into typed Python for the notebook builder) was stale. Any URL
param not listed there was passed to the plot handler as `str`, so
`?bin_color=3` became `bin_color="3"` and blew up inside `pd.qcut` with
a numpy `str >= int` comparison. Same class of bug for any recently-
added param.

Added coercion for: `bin_color` (int), `step_size` (int),
`clip_x`/`clip_y` (float), `show_ci`/`show_ci_corrected`/`rank_x`/
`sort_desc` (bool).

Callers that already worked continue to work; this is strictly a
bug-fix for the notebook path.

---

## 2.36.0 — 2026-08-07

### Added — `bin_color` on `/plot/histogram`, `/plot/ecdf`, `/plot/kde`

Extends the numerical-to-categorical binning pattern (2.34 for
scatter/hexbin/rolling; 2.35 for kde2d) to the three univariate
distribution geoms. `color` may now be numerical or gene when paired
with `bin_color=N`; the distribution is then split into N per-group
overlays. Requiring `bin_color` for continuous `color` (else 400) also
closes a soft failure where a raw numerical hue silently produced an
un-plottable per-value legend.

Additive.

---

## 2.35.0 — 2026-08-07

### Added — multi-category overlay on `/plot/kde2d`

`/plot/kde2d` now accepts `color`, `bin_color`, and `bin_color_method`
(same semantics as scatter/hexbin/rolling). When `color` is set:

- Categorical → one line-contour set per group, overlaid on shared
  axes; distinct palette colour per group + category legend.
- Numerical or gene → require `bin_color=N` to bin first (else 400).

Groups are capped at 8 (readable overlay ceiling). Groups with fewer
than 5 finite points appear in the legend as ``(n=X, no KDE)`` without
drawn contours. Filled-contour density + colorbar are only shown in
single-group (no `color`) mode; multi-group renders line contours with
faint 10% α fills so overlapping regions read cleanly.

Bandwidth, transforms, and the evaluation grid are shared across
groups so contours can be visually compared 1:1.

Additive.

---

## 2.34.0 — 2026-08-07

### Added — on-the-fly binning of continuous colour fields (`bin_color`, `bin_color_method`)

Two new optional query params on `/plot/scatter` and `/plot/hexbin`:

| Param | Values | Effect |
| --- | --- | --- |
| `bin_color` | int, 2–20 | Bin a numerical-obs or gene `color` into N categorical bins before plotting. Ignored when `color` is already categorical or unset. |
| `bin_color_method` | `quantile` (default) or `width` | `quantile` = equal-count bins (`pd.qcut`); `width` = equal-width bins (`pd.cut`). |

Bin labels are formatted `"[NN] lo-hi"` with a zero-padded index prefix
so the alphabetical legend sort respects bin order. The colour field
name is rewritten to `"<col> · N method bins"` so the legend title
and figure caption self-document.

Example — colour a UMAP by age binned into 3 equal-count groups:

```
GET /api/databases/S25/PhM/plot/scatter?color=Age&bin_color=3
```

Example — colour by CX3CR1 expression, 5 equal-width bins:

```
GET /api/databases/S25/PhM/plot/hexbin?color=CX3CR1&bin_color=5&bin_color_method=width
```

Quantile binning uses `duplicates='drop'`; if many values coincide (dropout
genes, discrete metadata), the actual bin count is smaller than requested
and the resulting field name / caption reflects it.

Additive. Callers that don't pass `bin_color` see identical behaviour to 2.33.

---

## 2.33.0 — 2026-08-07

### Added — value-level rename + hide (`obs_value_rename`, `obs_value_hidden`)

Extends the `api:` section from 2.32 with two more knobs, applied
inside the same obscat view via `CASE` expressions:

```yaml
api:
  obs_value_rename:
    Gender:                         # collapse mixed vocab
      M: male
      F: female
      Male: male
      Female: female
  obs_value_hidden:
    Gender: [None, Unknown, ""]     # values that get NULL'd
```

Applied in this order inside the view's CASE for each affected col:
1. Hidden values → NULL (hide wins over rename if a value is in both)
2. Rename branches (many→one collapse supported)
3. Passthrough

Zero-cost to plain (unaffected) cols. Adds a few nanoseconds per row
per query on cols with a CASE — imperceptible next to plot render
time. Predicate pushdown still works: `WHERE sex = 'male'` on the
aliased col gets pushed to `Gender IN ('M', 'male', 'Male')` on the
underlying table.

Both endpoints (`POST /admin/api-config/{db_id}`,
`POST /admin/refresh-views/{db_id}`) now accept + echo these fields.

---

## 2.32.0 — 2026-08-07

### Added — `.md` `api:` section + admin endpoints for column rename/hide

New optional section in dataset `.md` YAML frontmatter:

```yaml
api:
  obs_rename:
    Gender: sex               # internal → public
    Cognitive.Status: cognitive_status
  obs_hidden:
    - internal_qc_col
```

Applied at the `obscat` / `obsnum` **view** projection layer in DuckDB:

* Renamed cols surface under the public alias in every API surface
  (`/info`, `/plot/*`, `/cells/*`, `/analytics/query` SELECT-side, CSV export).
* The old internal name is unreachable via the API — any endpoint that
  references it returns 400 "column not found".
* `obs_hidden` cols are omitted from the view entirely (same effect as
  rename, no public alias offered).
* A rename automatically hides the internal name — no separate hide
  entry needed.
* Cols stay unchanged in the underlying `__obsnum` / `__obscat` tables,
  so internal tools (`ant-annotate`, `ant-score`, direct SQL against
  `__obsnum`) continue to see the original names.

**Two new admin endpoints:**

* `POST /admin/api-config/{db_id}` — body `{obs_rename, obs_hidden}`
  writes the api section to the `.md` and rebuilds the views. Both keys
  are optional; omit to leave existing values alone; pass `{}`/`[]` to
  clear. Validates: no two renames targeting the same public name; no
  target colliding with `cell_id`/`cell_name`.
* `POST /admin/refresh-views/{db_id}` — rebuild the views from the
  current `.md` without touching it. Useful after an out-of-band
  edit (text editor, git pull).

Both go through the connection-pool release/restore dance so an
in-flight query completes before the r/w rebuild.

**h5adstore git**: the `.md` file is written to disk but NOT
auto-committed — the admin's job to commit + push separately.

**Client impact**: additive. Existing datasets without an `api:`
section behave unchanged. Clients that read `/info` will see whatever
names are exposed (rename applied) — they should treat those as the
canonical names for that dataset.

---

## 2.31.0 — 2026-07-29

### Added — `/plot/rolling` — `rank_x`, `show_ci`, `show_ci_corrected`, `step_size`

Two new optional booleans on `GET /databases/{id}/plot/rolling`:

* `rank_x` (default `false`) — plot against the cell rank along `x`
  (0..n-1 after sorting) instead of the raw `x` values. With `color`,
  ranks are computed per group. The axis label becomes `rank(<x>)` and
  the caption reflects the change. Motivation: pseudotime is often
  clumped (bimodal / saturated); rank-x gives each cell equal visual
  weight so the rolling mean isn't dominated by dense pseudotime
  regions.

* `show_ci` (default `false`) — shade a **95% confidence interval**
  around the rolling mean (±1.96·SE, where SE = rolling_std / √window).
  Complements the existing `show_band` (IQR); both may be enabled
  simultaneously — the CI band is narrower and slightly more opaque so
  they read as distinct layers.

* `show_ci_corrected` (default `false`) — shade a **95% CI corrected for
  autocorrelation between adjacent windows** (which overlap by
  window-1). Uses `n_eff = min(window, actual_n) · step_size / window`
  — with the default `step_size=1` (every point plotted) n_eff ≈ 1, so
  `SE_corrected = rolling_std / √n_eff ≈ rolling_std`. Prefer this over
  the naive `show_ci` on large windows — the naive band is too tight
  because it assumes each of the `window` cells is an independent draw.
  The interior median `n_eff` per trace is written to stdout for
  sanity-checking the correction factor.

* `step_size` (default `1`, range 1..50000) — stride between plotted
  rolling-window centers. Larger values subsample the plot (faster
  render, less clutter) AND scale n_eff in `show_ci_corrected`. Set
  `step_size == window` for independent, non-overlapping windows
  (`n_eff = window`, corrected CI ≈ naive CI).

**Client impact:** additive. Older clients that don't send these params
still get the raw-x, IQR-only behaviour (unchanged).

---

## 2.30.0 — 2026-07-02

### Added — `/catalogs` — unified module-catalog view (gene weights for starCAT + genesets)

Cross-source view of every module catalog in the anthive stack:

* `GET /catalogs[?source=geneset|starcat]` — list all catalogs
* `GET /catalogs/{source}/{catalog_id}` — one catalog with its module
  summaries + score recipes
* `GET /catalogs/{source}/{catalog_id}/modules/{module_name}[?top=N]` —
  one module's genes ± weights, top-N by |weight| for starCAT
* `GET /catalogs/{source}/{catalog_id}/scores/{score_name}` — one
  derived-score recipe (composite / binary threshold / multinomial)
* `POST /catalogs/rescan` — force cache invalidation

**What starCAT programs actually are** (relevant because the previous
API surfaced BOTH `usage__Microglia_Border_CAM` (continuous) AND
`Border_CAM_pos` (binary) — the latter is derivable from the former):

* Each cell gets a **usage vector** — one NNLS coefficient per program
  (21 programs), L1-normalized so they sum to 1. That's the atomic
  measurement.
* Derived **composite** scores (`DAM_progression`, `Activation_index`,
  `Disease_burden`, `DC_total`) are documented linear combinations of
  usage — coefficients + column names in the catalog YAML.
* Derived **binary calls** (`Border_CAM_pos`, `IRM_pos`, etc.) are just
  `usage[column] > threshold`, where the threshold is the 90th
  percentile computed on the in-build cells. Each binary call has one
  column and one threshold — trivially reproducible client-side.

`GET /catalogs/starcat/MICROGLIA_V1_0/scores/Border_CAM_pos` now
returns exactly the recipe:

```json
{"name": "Border_CAM_pos", "kind": "discrete",
 "columns": ["Microglia_Border_CAM"], "threshold": 0.0448,
 "normalization": "normalized"}
```

Once callers reach for the recipes, the `_pos` sidecar columns become
optional — a future release may drop them from the parquet sidecar
now that thresholds are queryable. Continuous `usage__*` columns
stay (they're the atomic signal).

`/genesets` is unchanged — the new `/catalogs` view is a wrapper on
top of the same registry, plus the starCAT side which had no API
surface before.

---

## 2.29.0 — 2026-07-02

### Fixed / added — `attach_sessions` columns usable as `x` / `y` in XY plots

`_handle_xy` (which powers `plot/scatter?x=<a>&y=<b>` and
`plot/hexbin?x=<a>&y=<b>` — the non-embedding XY modes) previously
refused any request where x or y was an `attach_sessions` column.
The color= embedding path worked already; the XY path was a
deferred TODO.

Now supported — all combinations:

* **session-vs-gene** e.g. `?x=<DAM_session_col>&y=SPP1`
* **session-vs-numerical-obs** e.g. `?x=<DAM_session>&y=microglia_score`
* **session-vs-session** e.g. `?x=<DAM_session>&y=<Homeo_session>`
  (works as long as the two sessions don't emit a column of the same
  name — the loader prefixes collisions with `session_1::` etc.)

Implementation: `_handle_xy` now categorises each axis into
`{gene, numerical, session}`, fetches genes+obs via
`get_cell_table` as before, then LEFT-merges each session frame
onto `data_df` by `cell_name`. Filter + sample constraints stay
correct because they're applied on the base fetch; missing cells
in the session land as NaN and get dropped by the existing
finite-value guard. CSV export (`_raw_df`) works transparently
since the columns are on `data_df` regardless of source.

No client change required — plot.html already passes
`attach_sessions` on all plot requests, and the previous 400 was
rendered as an error card. Users get a plot instead now.
Chat assistant's earlier "DAM score vs SPP1" refusal path now
succeeds.

---

## 2.28.0 — 2026-07-02

### Added — `GET /module-variability`

Fleet-wide per-`(dataset × source × catalog × module × tag × statistic)`
variability table. Sourced from `data/h5adstore/module_variability.parquet`
(produced by `bin/annot/ant-module-variability`).

Statistics — in a scale-invariant `[0, 1]` variance-explained frame so
categorical (η²/ω²) and numerical (R²/spearman) tags are directly
comparable, AND datasets with wildly different overall variability are on
the same scale (the statistics are ratios by construction).

Query params (all optional AND-composed):

* Filters: `dataset`, `source`, `catalog`, `module`, `tag`, `tag_column`,
  `statistic`.
* Value bounds: `min=`, `max=` (raw value), `min_abs=` (for signed stats
  like `spearman`).
* Sort: `sort=value:desc` (default), `value:asc`, `abs_value:desc`.
* Paging: `limit=N` (max 100 000, default 500).
* `format=json` (default), `csv`, or `tsv`.

Example — top 20 (dataset, module) where braak explains ≥ 10 % of
module-score variance:

```
GET /module-variability?tag=braak&statistic=R2&min=0.1&limit=20
```

`POST /module-variability/rescan` forces a reload from disk.

### Added — `module_variability` visible in the analytics SQL sandbox

The same table is now attached as a `TEMP VIEW module_variability` in
every `/analytics/query` call. Full SQL: JOIN with obs, aggregate,
group-by, whatever. E.g.:

```sql
SELECT dataset, AVG(value) AS mean_r2
FROM module_variability
WHERE tag = 'braak' AND statistic = 'R2'
GROUP BY dataset
ORDER BY mean_r2 DESC
LIMIT 10
```

### Deferred (behind-the-scenes)

* Added the `extra_parquet_views` argument to `lib/analytics.run_query`
  so ant-serve can attach ad-hoc parquets as SQL views. Composable —
  more global tables can be added later (e.g. per-DE-study summaries)
  without further signature churn.
* `bin/ops/ant-sync` now syncs `module_variability.parquet` (added to
  `SYNC_EXTENSIONS` + `PULL_DEFAULT_EXTS` as a specific-basename allow).

---

## 2.27.0 — 2026-07-01

### Added — External score sidecar (`<basename>.scores.parquet`) exposed as virtual obs columns

Module scores (Sierksma WGCNA, MICROGLIA_V1 starcat, future scorers)
now live in a **long-format parquet sidecar** next to each dataset's
`.duckdb`, keyed on `(cell_id, source, catalog, module, score)`. See
`lib/scores_parquet.py` and `bin/annot/ant-score-ext` /
`bin/annot/ant-starcat-ext` for the write path.

The API reads them transparently. Each `(source, catalog, module)`
row appears as a **virtual numerical obs column** with the legacy
naming convention that pre-sidecar callers already used:

* `source=geneset`  → `<catalog with / → ::>::<module>`
  (e.g. `Sierksma2025::WGCNA::Magenta_AG_presentation`)
* `source=starcat`  → `starcat__<catalog>__<module>`
  (e.g. `starcat__MICROGLIA_V1__DAM_progression`)

Impact on clients: **none** (additive). Score columns appear in
`GET /databases/{db_id}/info` `obs.numerical` alongside real obsnum
fields; `GET /plot/…?color=<score>` and analytics queries JOIN them
transparently.

Internal change of note: the metadata-cache fingerprint now includes
the sidecar's mtime so a new/updated `.scores.parquet` invalidates
cached entries on the next scan.

### Deprecated (soft) — writing new scores into the duckdb via `AnnotationWriter`

`ant-score` and `ant-starcat` still work, but **new module scores
should be written to the parquet sidecar** via `ant-score-ext` /
`ant-starcat-ext`. This decouples score compute from duckdb rebuilds
and lets a dataset carry different score panels without a full h5ad
→ duckdb round-trip. Existing in-duckdb score columns keep working;
the drop-and-remigrate tooling (`tmp/drop_conflicting_duckdb_scores.py`)
can move them out.

### Added — obs-column tag vocabulary (`.md` `obs_tags`)

New `.md` frontmatter field `obs_tags: {column: [tag, …]}` where each
tag is drawn from a curated 5-item vocab (`sex`, `braak`, `cluster`,
`age`, `genotype` — see `lib/obscol_tags_vocab.yaml`). Purely
additive; not surfaced in `/info` yet.

Tools:
* `ant md tag COLUMN=TAG1[,TAG2] FILE` — add tags (vocab-gated,
  hard-fails when column isn't in `obsnum_include ∪ obscat_include`)
* `ant md untag COLUMN[=TAG] FILE` — remove
* `ant md tags FILE [--by-tag TAG]` — list / reverse-lookup
* `ant md tag-vocab` — show the vocab
* `ant tag-propose DB_ID [--apply]` — heuristic proposer

---

## 2.13.0 — 2026-06-23

### Added — `GET /databases/{db_id:path}/pseudobulk`

Aggregated count matrix grouped by one or more obs categorical
columns, in the shape DESeq2 / edgeR expect: rows = genes, columns =
pseudobulk groups, plus a `gene_name` column.  The missing input for
bulk-style downstream tools (DESeq2, edgeR, limma-voom) — previously
callers had to round-trip the `.h5ad` themselves.

Query parameters:

* `group_by` (required) — comma-separated obs **categorical** column
  names that define the groups.  `leiden` → one column per leiden
  level; `leiden,donor` → one column per (leiden × donor) combination,
  with group ids formatted as `<leiden>_x_<donor>` (separator `_x_`
  chosen so it cannot clash with the values themselves).
* `layer` — layer to aggregate, default `counts`.  Must be integer-
  valued; the endpoint validates against the catalog `layer_stats`
  and 400s with a helpful "this layer looks log-normalized, use
  layer=counts" message when it isn't.
* `min_cells` — drop groups with fewer cells (default 10, range
  1..10000).  Dropped combinations are reported in the metadata so
  callers can audit them.
* `subset` — pre-filter applied BEFORE the GROUP BY, same format as
  the plot endpoints (`field:value`, `field:v1,v2`, `field:min..max`).
  Categorical only — numerical range filters are rejected with 400.
* `max_inline_mb` — threshold (default 100) for auto-escalation to
  async/temp-file mode.
* `async` (alias) — force the async branch regardless of size.
* `format` — `parquet` (default) | `csv` | `tsv`.  CSV/TSV are
  always synchronous; the colData metadata is exposed via an
  `X-Pseudobulk-Meta` header instead of being embedded in the body.

Sync (inline) response:

* Content-Type `application/x-parquet` (or `text/csv` / `text/tab-
  separated-values`), `Content-Disposition: attachment`.
* Parquet schema metadata key `anthive_pseudobulk_meta` carries the
  colData as JSON: per-group `n_cells`, `column_index`, the original
  obs-column values, plus dataset-level stats (`db_id`, `layer`,
  `group_by`, `subset`, `n_genes`, `n_groups`, `n_dropped_groups`,
  `dropped_groups`).
* Response headers: `X-Pseudobulk-N-Groups`, `X-Pseudobulk-N-Genes`,
  `X-Pseudobulk-Dropped-Groups`, `X-Pseudobulk-Layer`,
  `X-Pseudobulk-Group-By`, `X-Plot-Caption` (Latin-1 safe).

Async response (auto-escalated above `max_inline_mb` or via
`?async=true`):

* Content-Type `application/json`.
* Body `{artifact_url, expires_at, expires_in, size_bytes, sha256,
  db_id, layer, group_by, subset, min_cells, n_genes, n_groups,
  n_dropped_groups, groups, dropped_groups, caption}`.
* The parquet is written to
  `webapp/artifacts/pseudobulk/<token>.parquet` with TTL 1 hour and
  served by the reverse proxy at `/artifacts/pseudobulk/<token>.parquet`
  — same Authelia gate as `/api/`.  A background task sweeps expired
  files every 5 minutes.

Reverse-proxy note: the deployment may need a new `location
/artifacts/` block (see `config/nginx.conf` / `config/Caddyfile`).
Existing static-site location blocks for `/plots/` are the model.

Caller impact: purely additive.  Callers tracking `/version` will
NOT see `pseudobulk` in `plot_geoms` — it isn't a plot geom; it sits
under tag `data` alongside `/cells`.  vark and antomaton's
`pseudobulk_deseq` workflow can now drop their mock-mode fallbacks.

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
