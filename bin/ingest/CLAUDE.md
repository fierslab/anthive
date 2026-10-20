# bin/ingest/ — h5ad → duckdb pipeline

The conversion + maintenance tools that turn `.h5ad` files into the
`.duckdb` files served by `bin/api/ant-serve`.

| Tool | Role |
| --- | --- |
| `ant-convert` | Full rebuild of a `.duckdb` from a `.h5ad`. Refuses if `__pending_cols` is non-empty (`--force-discard-pending` to override). |
| `ant-merge-h5ad` | Cold-path merge of pending duckdb obs columns back into the h5ad without rewriting X. Self-heals half-completed merges. |
| `ant-restore-obs` | Writes the obs columns an `.md` lists (`obscat_include` / `obsnum_include`) but the `.duckdb` lacks, reading the `.h5ad` **obs only** (h5py, no X). For datasets converted with empty include lists, where a full `ant-convert` would need the whole h5ad in RAM (over 100 GB for Gabitto2024 ExN). Goes through `AnnotationWriter(verbatim_names=True)` with `record_pending=False`: author column names with spaces are accepted and nothing lands in `__pending_cols` (the columns are already in the h5ad). `plan` / `apply`; afterwards run `ant-rebuild-obs-views --only <db_id>`. |
| `ant-build-all` | Walk the whole h5adstore, rebuild stale `.duckdb` (h5ad newer than duckdb), refresh `.md` (sha256/n_obs/layer_stats). Parallel-friendly. |
| `ant-h5ad-check` | Validate one `.h5ad` against the ingestion contract — log-norm in `.X`, raw counts in `layers['counts']`, embedding present, etc. |
| `ant-drop-indexes` | Rewrite existing `.duckdb` files without the redundant ART indexes older ant-convert runs created (flock + sentinel + ant-serve release/restore, verified content, atomic swap). `--dry-run` first. |
| `ant-duckdb-check` | Validate a `.duckdb` schema (`__cells`, `__genes`, `__obscat`, `__obsnum`, `__layer__*`, views, schema_version). |
| `ant-ingest-check` | **Read-only** check of a dataset submission (a group folder or one h5ad) before publication: 33 checks with frozen ids (`ant ingest-check --list-checks`), JSON report `anthive.ingest_check/1` plus markdown, exit 0 / 1 / 2. Runs `ant-convert` on a hard-linked scratch copy when there is no `.duckdb`, runs `ant-duckdb-check` on a private copy, never writes into the folder or the store. Package `lib/ingest_check/`, thresholds in `lib/ingest_check/thresholds.py` and `lib/facet_rules.py`. Docs: `doc/DATASET_INGESTION.md`. |
| `ant-ingest-index` | Builds the overlap indexes `ant-ingest-check` reads (donor digests with demographics, hashed cell barcodes, gene names per organism) under `config.get_ingest_index_dir()`, never in the store. Incremental; `build`, `status`. |
| `ant-ingest` | Manual ingestion from S3 `__incoming/` (one run by hand). h5ad deposits go through the shared pipeline `lib/ingest_pipeline.py` (pull pinned to the ETag, sha256 against the sidecar, catalogue `.md` fill or version bump, `ant-convert` in `config.get_ingest_work_dir()/manual`, `ant-ingest-check` where errors and held reasons block, atomic placement, rescan, catalog push with re-stamped `s3_*`). The prune ledger lives in `<ingest_state_dir>/pending_prune.json` (the old `<store>/.staging/_pending_prune.json` is read once and renamed `.migrated`). `prune` removes recorded work files and `__incoming/` keys; `purge <db_id> [--keep N]` / `purge --all` remove archived versions (`_versions/`) locally and on S3 with `--dry-run`, a typed confirmation or `--yes`, `--local-only`. DE-only deposits still go through `ant-de-ingest` here. Uses `lib.sidecar_merge`. |
| `ant-ingest-watch` | The S3 watch route (design section 21, how-to in `doc/DATASET_INGESTION.md`): a long-running poller of `ingest_watch.prefix` (`--once`, `status [--json]`, `approve <item> --by NAME`, `reject <item> --by NAME`, `retry <item> --by NAME` for needs_decision/failed after the cause is fixed). Keys with unsafe segments are malformed and never touched. Ready after two identical listings and `settle_seconds`; one item at a time at nice 19 / idle IO; file lock `<ingest_state_dir>/ingest_watch.lock` (shared with `ant-ingest`); SQLite state `ingest_watch.sqlite` (seen/stable/pulled/checked/live/held/failed/needs_decision/rejected, events, deletions, versions). Placement through a swap journal in `<ingest_state_dir>/swaps/` (hidden staged siblings, hard links into `_versions/`, `.md` renamed last; `recover_swaps` before every poll). Updates: minor (data) or patch (sidecar / seed caveats), store-wins sidecar merge with `override_<key>=true`, access/license changes and update-marker mismatches are `needs_decision` (a data_use change is applied with a changelog line and a caveat since 2.47.0); public data is always held. Deletes only consumed keys of a live, served (new sha256 + version via `/info`), catalog-verified item, each HEAD-compared with the pulled ETag right before its delete (`IfMatch` only on top when the `_selftest` check shows the backend enforces it; the lab bucket ignores it), keeping shared static while a sibling is pending; a swap rolled forward by recovery is finished (push, post steps, versions, commit) when its item comes back. Refuses runtime dirs inside the checkout (`make ingest-watch` sets them to `INGEST_WATCH_DIR`). Runs as its own systemd unit or `make ingest-watch`, never under ant-supervisor. API deposits (2.46, `lib/dataset_deposit.py`): an item whose h5ad and sidecar ETags are a completed deposit's gets `Item.deposit` (identity and `update_of` from the record, the deposit id in the content signature, only its own deposit's static/DE files), items locked by a completing deposit wait, an unlinked item carrying `x-amz-meta-deposit` (or any unlinked item with `accept_unlinked: false`) is refused as `needs_decision`, an unreadable deposit store stops the poll, each poll expires stale deposits, deletes the incoming objects of rejected deposits (`clean_rejected`; a reject is final and never continued), sweeps the staging prefix and writes owner caveats (`owner set` command), and after each final state the record, a redacted report copy (`<ingest_state_dir>/deposit_reports/`) and a depositor e-mail are written; the api must share `ingest_state_dir`. Core in `lib/ingest_watch.py`, `lib/ingest_pipeline.py`, `lib/ingest_md_fill.py`, `lib/ingest_versions.py`; tests `tests/test_ingest_watch*.py` (fake S3, runner and server; one test runs the real `ant-ingest-check`). Artefact and collection deposits (2.47, `lib/artefact_pipeline.py`): each poll ends with `ArtefactPublisher.poll` (purges first, then the completed artefact and collection deposits, then a `needs_decision` row for any unlinked object under `<prefix><data_project>/artefacts/`); `plan_items` never turns those keys into dataset items. `ANTHIVE_INGEST_SWAP_PAUSE_SECONDS` is a test hook (pause inside a commit) for the crash-recovery e2e only. |

## Which tools rewrite files (do not run these on a submission or a live dataset)

- `ant-h5ad-check` rewrites the dataset `.md`, and `bin/meta/ant-check` calls it as a subprocess.
- `ant-duckdb-check` is **not** read-only: it builds a missing `__gene_bins` table inside the database,
  may write `default_layer` into the `.md`, and appends to `./ant-<date>.log` in the working directory.
  Run it on a copy. (It modified a served database once during the P1 work.)
- `bin/meta/ant-md-check` writes ids unless `--no-fix`; `lib/md_check.py` skips `_` folders below the scan
  root, so pass the group folder itself as the root.
- `ant-convert` resolves symlinks: a scratch copy must be a hard link or a copy, or the `.duckdb` lands
  next to the original.

For anything that must stay untouched use `ant-ingest-check`. `ant-ingest` and `ant-ingest-watch` never call
the legacy h5ad checker or `bin/meta/ant-check` (`tests/test_ingest_watch_rules.py` greps their sources).

## DE sidecar

`ant-convert` re-imports `<stem>.de.parquet` into the fresh duckdb (full study rows
included). A failed reimport aborts the conversion (non-zero exit); a staged
build leaves the served duckdb untouched. `ant-duckdb-check` flags sidecar
studies the duckdb lacks.

## Dependencies

These tools rely on `lib/h5ad_utils.py` (load_h5ad, dtype inspection),
`lib/layer_stats.py` (per-layer min/max/median/q90/%zeros), `lib/metadata.py`
(parse/write `.md` frontmatter), and `lib/anthive_db.py` (DuckDB schema).

## Coordination with ant-serve

`ant-convert` and `ant-merge-h5ad` need **read-write** access to the
duckdb, but `ant-serve` holds it **read-only**. The `AnnotationWriter` context
manager (in `lib/anthive_db.py`) handles the dance:

1. POST `/admin/release/{db_id}` to ant-serve → pool closes idle handles, evicts the db key.
2. Open duckdb r/w, do work, commit.
3. POST `/admin/restore/{db_id}` → re-enable.

`--no-coordinate` skips this when ant-serve isn't running.

## Pending columns

`__pending_cols` is the audit trail for obs columns added incrementally by
`bin/annot/ant-score`, `ant-starcat`, `ant-annotate`. `ant-convert` refuses
to rebuild if anything is pending (you'd lose those columns); use
`ant-merge-h5ad` first to push them back into the .h5ad.

`pending_state_diagnosis()` in `lib/anthive_db.py` is the shared truth used
by both `ant-convert`'s guard and `ant-merge-h5ad`: returns `clean`,
`only_in_duckdb`, `already_merged`, `unrecorded`, `missing_from_duckdb`.

## Required-h5ad-contract

For ant-convert to accept a file, the .h5ad must have:
- `.X` = **log-normalized expression** (not raw counts, not SCT residuals)
- `layers['counts']` = raw integer counts
- At least one `obsm['X_*']` embedding (UMAP recommended)
- Consistent `var` index (gene symbols, not Ensembl IDs unless you're OK
  with `top_genes` being empty)

See `doc/INGESTION_CHECKLIST.md` for the longer form.
