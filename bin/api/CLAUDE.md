# bin/api/ — data and protocol servers

The two **runtime daemons** that expose anthive's data over network protocols.

| Tool | Role |
| --- | --- |
| `ant-serve` | FastAPI REST API (port 8080) — `/databases`, `/cells`, `/plot`, `/analytics`, `/admin`. Mounted under `root_path="/api"` so Caddy can proxy `:8501/api/...` to it. |
| `ant-mcp` | MCP server (port 8090) — exposes search / plot / analytics as MCP tools for LLM clients via the streamable-http transport. |

Both are long-running uvicorn/FastMCP processes, started under `ant-supervisor`.
Both depend on `lib/anthive_db.py` (DuckDB query layer), `lib/plotting.py`
(matplotlib/seaborn rendering), `lib/analytics.py` (SELECT-only SQL sandbox),
and `lib/config.py` (anthive.yaml loader).

## The bits that bite (ant-serve)

- **One private DuckDB instance per dataset, one cursor per request.**
  `ConnectionPool` opens an in-memory instance, ATTACHes the `.duckdb`
  read-only as `db`, then hardens it (`allowed_directories` = data folder,
  the dataset's own resolved directory, spill dir; `enable_external_access
  = false`; `lock_configuration = true`). Handlers get `cursor()` + `USE db`
  and must hand it back via `return_connection` (`_pooled_db` does). No
  `SET` works on a pooled cursor, and a pandas DataFrame can't be queried
  by variable name (replacement scans are external access); use
  `cursor.register()`. Open instances are LRU-capped
  (`duckdb_max_open_databases`, default 48) because each starts
  `threads - 1` OS threads. Server knobs are `duckdb_server_threads`
  (default 8) and `duckdb_server_memory_limit`; `duckdb_threads` /
  `duckdb_memory_limit` belong to ant-convert.
- **Handlers that touch DuckDB, S3 or files are plain `def`** (Starlette
  threadpool). An `async def` handler must push every blocking call through
  `asyncio.to_thread`, or one slow query stalls every request. Large pandas
  serialisation holds the GIL, so big responses are streamed in chunks
  (see `_cells_json_chunks`).
- **Rescan never empties the registry.** `_scan_databases` /
  `_cache_all_metadata` build new maps and swap them in; a dataset whose
  metadata load fails transiently keeps its previous entry. Rescan retires
  pool instances: in-flight queries finish on the old one.
- **DuckDB blocks cross-process read-write.** Annotation writers (`ant-score`,
  `ant-starcat`, `ant-annotate` from `bin/annot/`) can't open a duckdb r/w
  while ant-serve holds it r/o. Coordination via `POST /admin/release/{db_id}`
  → write → `POST /admin/restore/{db_id}`.
- **Field-type detection** (gene / obs-num / obs-cat / embedding) is automatic
  from the name; plot and query endpoints depend on it.
- **Metadata cache** (`<tmp_dir>/metadata_cache.json`) persists across
  restarts so startup skips the slow per-dataset DuckDB load. An entry is
  valid only while the mtimes of the `.md`, the nearest `index.md`, the
  `.duckdb`, the scores sidecar, and `annotation_sig` (sibling `.h5ad.mus`,
  the whole `.env` cascade, the collection-root `index.md`) are unchanged
  (`_get_entry_fingerprint`). It does NOT know about code changes: when you
  add or change a key that `_load_database_metadata` derives, bump
  `_METADATA_CACHE_VERSION`, otherwise old entries keep serving the old
  payload until `POST /admin/rescan`. S3-backed datasets use a 24 h TTL.
  Tests: `tests/test_metadata_cache_invalidation.py`.
- **Plot cache** is in-memory LRU keyed on `(db_id, query, format, duckdb +
  scores-sidecar mtime)`; captures the X-Plot-Caption + bytes. `?cache=miss`
  skips the lookup, re-renders and refreshes the entry (`X-Plot-Cache:
  BYPASS`); it is not part of the key.
- `analytics_query` runs sandboxed SELECT-only SQL via `lib/analytics.py` —
  see `tests/test_analytics_security.py` before touching the sandbox.
- **Module scores live in two places**: legacy obsnum columns of the duckdb,
  and (most datasets) the `<stem>.scores.parquet` sidecar, which `obsnum`
  does not contain. Anything that lists or reads scores must cover both:
  `/module_scores` (`storage`), the score index, `/info`; SQL reaches the
  sidecar through the TEMP view `score_sidecar` (`lib/scores_parquet.py`,
  installed by the analytics sandbox and `/query/sql`). A wide one-column-
  per-score view was tried and dropped (aggregates the whole sidecar per
  query). Tests: `tests/test_score_sidecar_surfaces.py`.

## Help endpoints and `explain`

- `/help`, `/help/{topic}`, `/help/glossary[/{term}]`, `/help/search` serve
  `lib/help_content/` (the same files render the web help page). Markdown is
  derived from the HTML fragments by `help_content.html_to_markdown`. Public
  like `/version`; responses carry an `ETag` and `Cache-Control: public`.
  Keep the literal `/help/glossary` and `/help/search` routes above
  `/help/{topic}`.
- `_with_help(payload, *topic_ids)` adds `help_topics` to facet and context
  responses; the ids must exist (tested).
- `ant-mcp` has no `help_content` dependency: `explain`, `anthive://help` and
  `anthive://help/{topic}` call the REST routes, so the MCP needs no yaml data
  files. `GET /skill` serves `.claude/skills/anthive/SKILL.md`, a thin bootstrap.

## Personal MCP links (ant-serve side)

- Token store: `lib/mcp_tokens.py` (`McpLinkStore`), SQLite at
  `<state_dir>/mcp_tokens.db`. Tokens are HMAC-derived
  (`amcp_<public_id>_<mac>`), never stored; rotate = generation + 1.
- Endpoints: `GET /me/mcp`, `POST /me/mcp/rotate`, `DELETE /me/mcp`
  (Remote-User required), `GET/DELETE /admin/mcp-links[/{user}]` (admins),
  `POST /internal/mcp/verify` (internal secret only; Caddy 403s
  `/api/internal/*`). The web UI's `/account/mcp` page consumes `/me/mcp`.
- `_InternalSecretGuard`: a wrong `X-Anthive-Internal` is a 401 everywhere.
- `/admin/rescan|release|restore|refresh-views`, every `/admin/*-cache/*` and
  `/admin/*-index/*` route, `/admin/api-config/*` and `POST /genesets/rescan`,
  `/module-variability/rescan`, `/catalogs/rescan` use
  `_require_admin_or_local`: loopback tool calls without proxy headers,
  Remote-User or `X-Anthive-Anonymous` (ant-sync, AnnotationWriter,
  ant-refresh-views, ant-score), internal-secret calls without a user (tools
  in another container), or admins. Through the proxy a normal user gets 403.
  `tests/test_api_access.py` lists the guarded routes; add new server-control
  routes there.
- **Identity is `Remote-User` / `Remote-Groups` only** (2.45). `X-Remote-*`
  is never read (it let a client claim `admins`); the Caddy snippets drop it.
- **access=internal datasets need an identity** (2.45, `lib/dataset_access.py`,
  setting `internal_requires_auth`). `_DatasetAccessGuard` (pure ASGI) answers
  404 for `/databases/{hidden}/...` and dataset query parameters, and sets the
  context variable behind `registry.databases` / `registry.metadata_cache` /
  `list_databases()` (properties that drop hidden ids during an anonymous
  request; the full maps are `_databases_all` / `_metadata_all`). Anything that
  lists datasets from another source (score index, parquet indexes, cohort
  table, library links, pools) must filter with `_visible_db_ids` /
  `_hidden_db_ids()`, and a cache built inside a request must be keyed by the
  hidden set. Worker threads (`ThreadPoolExecutor`) do not inherit the context:
  decide the dataset list in the request thread. ant-browse forwards
  `Remote-User` (or `X-Anthive-Anonymous`) on every server-side call; ant-mcp
  sends `X-Anthive-Anonymous` without a link owner. Unknown and hidden datasets
  share one 404 (`UnknownDatabaseError`, `unknown_database_detail`).
- Secrets: `internal_secret` / `mcp.token_secret` in config, else env
  `ANTHIVE_INTERNAL_SECRET` / `ANTHIVE_MCP_TOKEN_SECRET`, else auto-created
  0600 files in `<state_dir>/secrets/` (`config.get_or_create_secret`).

- **Paper library + literature search** (2.39): `lib/library_service.py`
  holds the records, the resolved `papers:` references and the
  `LibraryIndex`; loaded in a thread at startup and on `/admin/rescan`.
  Handlers stay thin. Access (2.41): no per-paper rules; everyone past the
  site login sees the whole library and any text may go to AI clients
  (leftover `anthive_access`/`anthive_llm` are ignored with a validator
  warning). Query model: fastembed, cache from `get_model_cache_dir()`.

- **Papers a site lacks are invisible** (2.44): `LibraryService.load` links only citekeys that resolve
  (`papers_by_db`, `dataset_links`, `geneset_links`); missing ones live only in `state.unresolved`, shown
  by `status(admin=True)` (`GET /library/status` passes `_caller_is_admin`, never true on `auth.mode: none`).
  `related:` to missing citekeys is dropped in memory. PDF-missing warnings are derived live
  (`pdf_warnings`), never stored. A library without `.git` is fingerprinted by its record files
  (`_folder_fingerprint`). Tests: `tests/test_library_site_modes.py`. Do not add a surface that reads
  `papers:` front matter directly.

- **Paper upload** (2.40): `GET /papers/upload/requirements`, `POST
  /papers/upload` (multipart). Logic lives in `lib/library_upload.py`
  (validation, enrichment, duplicates, folder rule, flock under `.git/`,
  commit with the uploader as author); the handler only does auth
  (`_require_uploader`, `library_upload_groups`) and the post-commit steps
  (`_after_upload`: background `add_or_replace_sources`, library reload,
  S3 autopush). Post-commit failures are warnings, never 500s. A library
  that is not writable (`uploads_enabled()` false, e.g. `services.paper_upload`
  off) answers 503 `disabled` before any write, and requirements report
  `uploads_enabled: false`. Upload features are visible to uploaders only:
  both routes return 403 `forbidden` for others (before the 503) and are
  `include_in_schema=False`; ant-mcp hides `paper_upload_requirements`,
  `check_paper` and `upload_paper` unless the caller may upload
  (`UPLOAD_TOOLS`, `_may_upload`) and uploads are enabled
  (`AnthiveMCP.uploads_probe`, asked as the caller, cached 60 s). Role
  defaults to `background`; folder precedence proposals > lab > datasets >
  methods > reading.

- **Dataset deposits** (2.46, design section 22): `/datasets/deposit` (vocab, create, `{id}`, `/urls`,
  `/complete`, `/report`, `/withdraw`, `/approve`, `/reject`), tag `datasets-deposit`, hidden from the schema.
  Rules live in `lib/dataset_deposit.py` (`DepositService`, store `<ingest_state_dir>/deposits.sqlite`); the
  handlers only call `_require_dataset_uploader` (Remote-User in `datasets_upload_groups`, never anonymous,
  never on `auth.mode: none`; `admin_ok` for the admin routes approve/reject/expire/owners/`?all`) and turn
  `DepositError` into its JSON. URLs point at `<datasets_deposit_staging_prefix><deposit_id>/...`, NEVER at
  `ingest_watch.prefix`: complete copies the verified files in (security review C1; never presign a watched
  key). Presigning uses `datasets_deposit_s3` (else the `s3` keys); the keys never leave the server.
  Deposits above `datasets_deposit_async_complete_gb` complete in a daemon thread of this process (202,
  progress in the status); `_deposit_service()` calls `resume_interrupted()` on start, so a restart resumes
  completes another worker left `completing`. `GET /datasets/deposit/groups/{data_project}` is registered
  before `/{deposit_id}`. Status
  merges the watcher's `ingest_watch.sqlite` row (same `ingest_state_dir`, read-only) with the record;
  everything a depositor sees is redacted (`redacted_report`, `sanitize_text`). Another user's deposit is a
  404. Approve/reject call `ingest_watch.approve/reject(channel='api')`. ant-mcp: `my_deposits`,
  `deposit_status` (`DEPOSIT_TOOLS`, `_may_deposit`), read-only; no upload through MCP. Tests:
  `tests/test_dataset_deposit*.py` (fake S3 with presigned URLs and multipart).

- **Facet test results** (unreleased): `lib/facet_results.py` reads the
  output of `bin/annot/ant-facet-test[-modules]` from `facet_results_dir`
  (default `<h5adstore>/_facet_tests`, outside the catalogue scan). Routes:
  `/databases/{id}/facet-tests[/genes|/modules|/volcano.png]` (registered
  before the greedy `{db_id:path}/genes` route), `/facets/{name}/tests`,
  `/facets/{name}/effects[/top]` (`/top` first), `/genes/{gene}/facet-effects`,
  `/facets/cohorts` (before `/facets/{facet_name}`). Another job may rewrite
  the files at any time: never assume a file is complete. The cross-dataset
  gene tables of ALL facets and scopes are built in one pass (about 15 s CPU,
  1.3 GB) and each replication family (one per facet x scope x
  exclude_secondary, about 3 s) is computed once; gene cards are cached per
  gene. Keep every cache bound at least facets x scopes (`FAMILY_CACHE_ENTRIES`):
  a smaller LRU made each `/genes/{g}/facet-effects` recompute everything
  (2.44: 25 s warm, 170 s cold). Builds are single flight per key
  (`FacetResults._cached_build`: no global lock during a build, previous value
  served while a rebuild runs); pass `reader.prepare(datasets)` to several
  calls of one request so the files are stat-ed once. Caches built from a
  dataset list are keyed by `PreparedDatasets.visibility` (the set of db_ids)
  and sized for `VISIBILITY_KINDS` = 2, so signed-in and anonymous callers
  never evict each other. A donor-pooled unit is served only when every
  member is in the caller's list (`pool_is_visible`); `/facets/cohorts` lists
  served rows only.
- **Warm-up** (`_warm_caches`, config block `warmup`, `config.get_warmup_settings`):
  a nice-10 daemon thread after start builds the facet caches once per visitor
  kind (`_warmup_visitor_kinds`: signed-in unless `auth.mode: none`, anonymous
  when internal datasets are hidden, run under `_as_visitor`), then the
  per-donor values of `warmup.donor_genes`, then the module rank tables
  (`module_ranks: all | priority | off`, `module_rank_datasets` first,
  `workers` 1 or 2 against the store's 2 build slots). `ANTHIVE_WARMUP=0` or
  the older `ANTHIVE_FACET_WARMUP=0` skips it. Tests: the `warmup` tests in
  `tests/test_facet_results.py`. 2.46.1: while the warm-up has not finished the
  facet phase of a caller's visitor kind (`_FACET_WARMUP_PENDING`, filled by
  `_start_warmup`, emptied per kind in `_warm_caches`, also on failure), the
  cross-dataset facet routes call `_facet_warm_gate()` and answer 503
  `{status: "warming_up", retry_after}` with `Retry-After` instead of building
  the same tables on a request thread. Add the gate to any new cross-dataset
  facet route, after its parameter checks (an unknown facet stays a 404); the donor
  routes gate inside `_donor_units_payload`. The gate opens after
  `FACET_WARMUP_GATE_MAX_SECONDS` (900). ant-mcp's `api_request` turns the 503 into a
  plain "warming up" message. ant-serve, ant-browse and ant-mcp call
  `config.set_unknown_keys_lenient()` before loading the config. A 503 carrying `X-Anthive-Unavailable` (`EXPECTED_UNAVAILABLE_HEADER`:
  warming up, no geneset store) is logged at info once per reason, not as ERROR.
  `facets_visible` (config) only adds `hidden_by_config` to the facet
  listings; nothing is dropped from the API.
- **Artefact store** (REST 2.47.0, `doc/DESIGN_artefacts.md`, `lib/artefacts.py`): `_scan_databases` ends
  with `registry._scan_artefacts()`, which builds the collection registry (every `<Group>/index.md`, so
  analysis collections without datasets exist) and the artefact index and swaps both in (a failure keeps the
  old ones; `POST /admin/rescan?what=artefacts` reloads only these). Routes `/artefacts...` (tag `artefacts`):
  the access middleware knows only dataset paths and query parameters, so every route resolves through
  `_artefact_resolve` (access on the resolved artefact; invisible = the same 404 as missing) or filters with
  `_artefact_policy()` over the FULL registry maps plus `_artefact_viewer(request)`;
  `tests/test_artefacts_api.py` enumerates the routes, add new ones there. Downloads: 307 to a presigned GET
  for one key (`artefacts_s3` read keys, else `s3`), `no-store` and `no-referrer`, or a sha256-verified local
  copy with `nosniff` and `CSP: sandbox`; never put a signed URL in a log line or an MCP answer (Caddy drops
  the query of `Location` in its access log). `/groups/{id}` answers analysis collections from the registry;
  `/groups/{id}` and `/databases/{id}/info` carry `artefacts_count`.
- **Artefact and collection deposits** (REST 2.47.0, phase A2, `lib/artefact_deposit.py`): `_deposit_service()` builds
  `ArtefactDepositService`; `POST /datasets/deposit` dispatches on `deposit_kind` with `_deposit_call(..., kinds=(kind,))`
  and passes `_artefact_context(request)` (registry, index, policy, viewer); `_require_dataset_uploader(kinds=...)`
  counts `artefacts_upload_groups` (plus dataset uploaders) and `collections_create_groups` only where
  `artefacts_enabled` is on, admins always there; the generic routes accept all three kinds (the service shows a
  caller only their own deposits). `POST /artefacts/{gid}/{scope}/{id}/{version}/purge` records a purge request
  (202); the watcher carries it out. Tests: `tests/test_artefact_deposit_api.py` (rights matrix).
- **Collections and artefacts over MCP** (2.47): `list_collections` (`GET /collections`), `list_artefacts`, `get_artefact`,
  open to every caller (the API decides visibility; no role gate). Depositor text is labelled as such, download links are
  `public_api_url` + the API download route, never a signed URL. `_deposit_groups` adds `artefacts_upload_groups` and
  `collections_create_groups` where `artefacts_enabled`, so analysts see `my_deposits` / `deposit_status`. No upload
  over MCP. Tests: `tests/test_mcp_artefacts.py`. `GET /collections` (visibility: a dataset collection with a visible
  dataset, an analysis collection by its index.md access) is tested in `tests/test_artefacts_api.py`.
- **Catalog scan** walks the store with `lib/h5adstore.catalog_dataset_mds` (2.46.1), the same helper
  `ant-stack check` counts with: store-root documents, `static/` documents, `index.md`/`README.md`,
  `_` folders and inactive datasets are never datasets.

- **Facet engine v2** (2026-10-05): dataset metadata (`/databases`, info) also carries
  `replicate_unit`, `replicate_column`, `replicate_basis`, `disease_basis` (`_lineage_fields`); the
  facet results serve `r_value`, `k`, `m`, `anchors`, `analysis_level` (see `doc/API_CHANGELOG.md`);
  `/module-variability` and `gene-variance-by-facet` are marked `per-cell (descriptive)`; pooled
  units (`pool:<id>`) come from `_facet_tests/_pooled/` through `FacetResults.pooled_datasets()`.

- **Facet engine v3** (2026-10-06, API 2.44.0): dataset metadata carries `facet_scope` (cache version 6; `_lineage_fields`);
  every cross-dataset facet route takes `scope` (`_scope_param`, default: `human_postmortem`, or the facet's first allowed scope,
  e.g. `xenograft_human_in_mouse` for `app_nlgf`; `all` on `/facets/{f}/tests`) and answers `scope` / `scopes`; `/genes/{g}/facet-effects` adds
  `by_scope`; `/facets/{f}` and `/module-variability` filter by scope. Replication fields: `effect_gate_passed`, `median_logFC`,
  `effect_gate_tau`, `r_significant`, `k_treat`. Never pool values, cells or genes across scopes in a new endpoint (`_facet_scope_of`).

- **Views API** (2.45, `doc/DESIGN_facet_testing.md` "Views API"): `lib/facet_module_results.py` (`ModuleResults`, composition over
  the shared `FacetResults`, same single flight and cache bounds; warmed after the gene families in `_warm_facet_results`)
  serves `/facets/{f}/modules/top` and `/genesets/{study}/{experiment}/{name}/facet-effects`; `/facets/{f}/effects/overview`,
  `/genes/{g}/detection`, the forest, calls and paging live on the gene routes. Keep these cheap: the module tables are one
  pass over the small module parquets, the detection table is one groupby over `gene_stats_index` (`_detection_tables`,
  rebuilt per index snapshot, never a live scan), and the context flags read the geneset registry and the MGI ortholog table
  (`_gene_context`, cleared by `_build_score_index`). Gene cards are shared cached objects: add per-request fields on a copy
  (`_with_gene_context`). Modality is derived on the fly (`lib/modality.py`), not stored in the metadata cache.

- **Donor plots** (2.45, `doc/DESIGN_facet_testing.md` "Donor plots"): `lib/facet_donor_values.py` (`DonorValues`) rebuilds the
  replicate-level values of a unit from the per-sample sidecars the test tools write (`*.facet_samples.parquet`,
  `*.facet_sample_cells.parquet`, `*.facet_module_samples.parquet`, `*.facet_module_sample_scores.parquet`); ant-serve adds
  `GET /facets/{f}/donor-values` and `donor-plot.{png,svg,pdf}` (`_donor_units_payload`, `_gene_count_fetcher` reads one gene
  from the counts parquet through the pool) and `links` on gene and module card entries (`_donor_links`, `_with_module_links`;
  existence checks only, no sidecar is read for a card). Plots follow the clean-image rule: caption in the header and metadata,
  titles are cohort and n only. Never return a donor id or a per-donor covariate; results without sidecars answer
  `available: false` until `ant-facet-test[-modules] run --samples-only` backfills them. Tests: `tests/test_facet_donor_values.py`
  (the ant-serve routes are listed in `_SERVE_WANTED` of `tests/test_facet_results.py`: add new helpers there).

- **Donor variability** (2.45, `doc/DESIGN_donor_variability.md`): `lib/donor_variability.py` (`DonorVariability`, composed over the
  shared `FacetResults`: same file keys, JSON cache and cohort table) serves `GET /databases/{id}/donor-variability` (registered
  before the greedy `{db_id:path}/genes` route), `/genes/{gene}/donor-variability`,
  `/genesets/{study}/{experiment}/{name}/donor-variability` and `/donor-variability/top` (`_donor_variability`, `_dv_call` turns bad
  parameters into 400). A single gene or module is read with a pyarrow filter (the files are sorted by gene with small row
  groups), never by loading every dataset; the cross-cohort family reads only the p_excess columns of the cohort anchors and is
  cached per file version. Counted units are the cohort anchors with at least 30 replicates; scopes are never mixed. MCP tool
  `donor_variability`; help topic `donor-variability`. Tests: `tests/test_donor_variability_api.py` (the routes are in
  `_SERVE_WANTED` of `tests/test_facet_results.py`).

- **Module rank** (2.45): `GET /genesets/{study}/{experiment}/{name}/rank` (`lib/module_rank.py`) answers where a gene set's mean score
  ranks among all gene sets scored in the same dataset (scanpy source primary, UCell secondary). It reads the `.scores.parquet` sidecar,
  not `scores_index.parquet` (no cell type column, can lag the rescoring), and caches the per-dataset table under `<tmp_dir>/module_rank/`
  keyed on sidecar and duckdb stat; the first call per dataset reads the whole sidecar (up to a minute) with at most 2 builds at once, so
  clients ask per dataset (`?db_id=`). Tests: `tests/test_module_rank.py`.

- **Lineage and secondaries** (unreleased): dataset metadata carries
  `designation`, `reanalysis_of`, `donor_group`, `has_donor_uid`
  (`_lineage_fields`); `GET /donor-groups` lists the groups. Pooled or
  cross-dataset numbers take `exclude_secondary` (default true, helper
  `_secondary_db_ids`): `/gene_stats_all`, `/module-variability`,
  `/facets/{name}`, facet signal strength and the facet test views (listed,
  not counted). New pooled features must do the same.

## ant-mcp specifics

- FastMCP `streamable-http` transport on `/mcp`, bound to loopback.
- **Personal MCP links** (`doc/MCP_LINKS.md`): `/mcp/u/<token>` (rewritten to
  `/mcp`) or `Authorization: Bearer amcp_...`. `_McpLinkAuthMiddleware`
  verifies via ant-serve `POST /internal/mcp/verify` (header
  `X-Anthive-Internal: <internal_secret>`), caches `mcp.verify_cache_seconds`,
  rate-limits per token, and puts the owner in the ASGI scope. `AnthiveMCP`
  (FastMCP subclass) reads it per message, hides `ADMIN_ONLY_TOOLS` from
  non-admins, and every ant-serve call carries `Remote-User`/`Remote-Groups`
  + the internal secret, so tools run as that user.
- `mcp.require_token: false` restores the old unauthenticated `/mcp` (only
  for localhost-only setups). With tokens on, FastMCP's DNS-rebinding check is
  disabled, because behind Caddy the Host header is the public name.
- Pinned to `mcp>=1.10,<2`: the 2.x SDK renamed FastMCP.
- OAuth-discovery probes (`/.well-known/oauth-*`, `/register`) are
  short-circuited by Caddy with valid JSON 404s (see `config/Caddyfile`).
- Plot output saved to `webapp/plots/` for URL-based serving; falls back to
  data: URIs if `plots_dir` is null.
- `plot` calls the per-geom routes `/databases/{id}/plot/{geom}` and sends
  only the parameters that route declares (`PLOT_COMMON_PARAMS`,
  `PLOT_GEOM_PARAMS`; `tests/test_mcp_api_245.py` checks them against the
  ant-serve route signatures). Add a parameter to both when a route grows one.
- Links a tool prints for the user's browser are absolute: `public_api_url`
  (`mcp.public_url` + `/api`, the Caddy prefix). Never print an API-relative URL.
- Stored DE (`list_de_studies`, `de_study`, `gene_de`) is author or custom DE:
  label it so, mark cell-level methods descriptive (`de_method_level`, the same
  rule as the web UI's `_de_method_level`; keep the two in step) and never
  present it as replication. The all-datasets scan is cached an hour per caller.

## Hosts / ports

Default bind: `127.0.0.1`. Override via `anthive.yaml`:
```yaml
rest_api_host: 0.0.0.0   # bind on all interfaces (skip Caddy)
rest_api_port: 8080
```
MCP defaults to `127.0.0.1:8090` (`mcp.host` / `mcp.port` in anthive.yaml).

## Server-selection signal — `/health` carries the "how fast is this server"

A client (e.g. `vark`) configured with multiple anthive endpoints should
prefer the lowest `mean_response_ms` / `p50_response_ms` exposed by
`GET /api/health`.

The middleware (`RequestLoggingMiddleware` in `ant-serve`) records every
non-probe request's wall-clock into a 1024-slot ring buffer.  `/health`
and `/metrics` then expose:

| field | from | use |
| --- | --- | --- |
| `mean_response_ms` | `/health` + `/metrics.latency_ms.mean` | quick comparison; skewed by plot renders |
| `p50_response_ms`  | `/health` + `/metrics.latency_ms.p50` | recommended — outlier-resistant |
| `latency_ms.{p95,p99,max}` | `/metrics` only | diagnostics |
| `n_samples` | both | how many real requests fed the stats; `null` until first hit |

**Excluded** from the stats (so probes don't make a server look fast):
`/health`, `/metrics`, `/admin/plot-cache/stats` (matched by suffix).

### Recipe for vark (or any client picking between servers)

```python
def pick(endpoints, auth=None):
    import time, requests
    best, best_score = None, float('inf')
    for url in endpoints:
        try:
            t0 = time.perf_counter()
            r = requests.get(f"{url}/api/health", auth=auth, timeout=2)
            rtt_ms = (time.perf_counter() - t0) * 1000
            body = r.json() if r.status_code == 200 else {}
        except (requests.RequestException, ValueError):
            continue
        # Use server-side p50 once it has enough samples; otherwise fall
        # back to the network RTT we just observed.
        if body.get('n_samples', 0) >= 10:
            score = body.get('p50_response_ms') or rtt_ms
        else:
            score = rtt_ms
        if score < best_score:
            best, best_score = url, score
    return best
```

### Caveats worth knowing

- **Server-side wall-clock only.** Excludes network RTT between client
  and server — that's why the recipe above also tracks the probe's RTT.
- **Cold-start = `null`.** A freshly-restarted server has `n_samples=0`
  until traffic warms it up; clients should fall back to probe RTT.
- **Workload skew.** A server that mostly served `/info` looks fast even
  if its `/plot` is slow.  For honest comparisons across servers, hit
  representative endpoints, not just `/health`.
- **Ring buffer is per-process and in-memory.** Not persisted across
  restarts; not aggregated across replicas.
