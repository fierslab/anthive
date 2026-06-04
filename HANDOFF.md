# anthive4 — handoff guide

Single-cell RNA-seq data browser. Curated `.h5ad` datasets → DuckDB → REST API →
web UI + MCP + JupyterLite. ~100 datasets, mostly human microglia.

This document orients a fresh contributor (human or AI) onto the codebase
domain by domain. Every section points to a focused `CLAUDE.md` with the
deeper, more current detail.

---

## TL;DR — how the parts fit together

```
.h5ad  ──[ingest]──▶  .duckdb  ──[api]──▶  REST API ──┬──▶  [web]  + [frontend/browser]
  ▲                      ▲                            ├──▶  [frontend/streamlit]
  │ [annot]              │ [annot] writes obs cols    └──▶  [frontend/jupyter]
  │ writes back          │ here first; ant-merge-h5ad
  │ later                │ pushes them back to .h5ad
  └──[ingest]────────────┘
                                                      ┌──▶  [mcp]   (LLM clients)
                                                      └──▶  [analytics]  (SQL sandbox)
[ops] supervises everything, syncs .h5ad+.duckdb to S3
```

Source of truth is the `.h5ad`. The `.duckdb` is a derivative AND the
fast-path annotation target. The split exists because rewriting a 60 GB
`.h5ad` to add one obs column would be glacial; rewriting just the duckdb
takes seconds, and the merge back into h5ad happens out-of-band.

---

## The seven domains

Each has its own `CLAUDE.md` that a focused Claude Code session will load
when you `cd` there. Use this table to pick which one to open.

| Domain | Lives in | Owns | When to open it |
| --- | --- | --- | --- |
| **api** | `bin/api/` ([CLAUDE.md](bin/api/CLAUDE.md)) | `ant-serve` REST API + `ant-mcp` MCP server | Add/change a `/databases/...` endpoint, plot a new geom, debug `/admin/release` coordination, tune the plot cache |
| **web** | `bin/web/` ([CLAUDE.md](bin/web/CLAUDE.md)) | `ant-browse` — the FastAPI server that renders Jinja templates from API responses | Add a route, change response shape, fix breadcrumbs/navigation |
| **ingest** | `bin/ingest/` ([CLAUDE.md](bin/ingest/CLAUDE.md)) | `ant-convert`, `ant-merge-h5ad`, `ant-build-all`, `ant-h5ad-check`, `ant-duckdb-check` | New h5ad → duckdb conversion, pending-column merges, h5ad/duckdb schema validators |
| **annot** | `bin/annot/` ([CLAUDE.md](bin/annot/CLAUDE.md)) | `ant-score`, `ant-starcat`, `ant-annotate`, `ant-suggest-tops` | New module-score panel, manual obs columns from TSV, top_genes curation |
| **meta** | `bin/meta/` ([CLAUDE.md](bin/meta/CLAUDE.md)) | `ant-md`, `ant-md-check`, `ant-check`, `ant-cellxgene`, `ant-geneset`, `ant-api-check`, `ant-s3-check` | Edit `.md` frontmatter, validate the catalog, import a CELL×GENE dataset, manage genesets |
| **ops** | `bin/ops/` ([CLAUDE.md](bin/ops/CLAUDE.md)) | `ant-supervisor`, `ant-sync`, S3 helpers, dev wrappers | Run the stack, S3 sync, debug supervisor restart, build reports |
| **frontend** | `frontend/{browser,streamlit,jupyter}/` ([CLAUDE.md](frontend/CLAUDE.md)) | three web UIs: Jinja templates (browser), Streamlit dashboards, JupyterLite notebooks | UI changes, HTML/CSS/JS, page layout |

Plus the shared core: **`lib/`** ([CLAUDE.md](lib/CLAUDE.md)) — the DuckDB query layer, plot rendering, mus annotation, metadata IO. Almost any tool above will pull from here.

And the **standalone client**: `antclient/` ([CLAUDE.md](antclient/CLAUDE.md)) — a pip-installable REST client. The only piece structured for extraction. No `lib/` dependency.

---

## Common operations

### Run the stack locally
```bash
make serve-debug         # supervisor + caddy on :8501
make serve-status        # what's running
make serve-restart SVC=caddy  # restart one service
make serve-stop          # SIGTERM the supervisor (cascades)
```
Caddy on `:8501` fronts everything. Backends bind to `127.0.0.1` by default.

### Optionally add a password
```bash
make caddy-passwd        # interactive — generates config/caddy-auth-basic.conf
make caddy-passwd-clear  # remove auth
```
Protects `/`, `/browse/*`, `/api/*`, `/jupyter/*`, `/streamlit/*`. MCP and OAuth-discovery probes stay open.

### Convert a new h5ad
```bash
./bin/ant convert path/to/file.h5ad      # via dispatcher
./bin/ingest/ant-convert path/to/file.h5ad   # direct
```
The h5ad must follow `doc/H5AD_INGESTION_SPEC.md` — log-norm in `.X`,
raw counts in `layers['counts']`, at least one `obsm['X_*']` embedding.

### Score modules from the genesetstore
```bash
./bin/ant score <db_id>               # writes to duckdb (fast path)
./bin/ingest/ant-merge-h5ad <db_id>   # later: merge back into .h5ad
```

### Sync to S3
```bash
./bin/ant sync status                                  # what's drift
./bin/ant sync push 'Sierksma2025/Village.6M/*'        # scoped push
./bin/ant sync pull                                    # h5ad only
make sync-update                                       # full cycle
```

### Run the test suite
```bash
make test    # PYTHONPATH=lib python -m pytest tests/
```

---

## Data stores (separate git repos)

Two store repos live under `data/` (gitignored from this repo, each is its
own checkout):

- `data/h5adstore/` — github.com/badslab/h5adstore. Holds `.md` (tracked) +
  `.h5ad` (S3-only) + `.duckdb` (S3-only). `.h5ad.mus` and `.env` cascade
  files for experimental annotation also tracked.
- `data/genesetstore/` — genesets organised per study (`.grp`, `.rnk`).
  Consumed by `bin/annot/ant-score`.

The split exists because the `.md` / `.mus` metadata is small text that
should be diffable, while the `.h5ad` / `.duckdb` blobs are too big for
git and live on S3.

---

## doc/ — design and reference

| File | When to read |
| --- | --- |
| `ARCHITECTURE.md` | Big picture, components, data flow |
| `H5AD_INGESTION_SPEC.md` | Contract for incoming h5ad files |
| `DATABASE_SCHEMA.md` | DuckDB tables produced by ant-convert |
| `REST_API_SPEC.md` | API endpoints + response shapes |
| `DESIGN_annotation_updates.md` | How annot/ writes columns without rebuilding |
| `METADATA.md`, `METADATA_STRUCTURE.md`, `API_METADATA.md` | `.md` frontmatter and `.mus` schema |
| `CONVERT_PROCESS.md`, `CHECK_PROCESS.md` | Pipeline-level processes |
| `PERFORMANCE.md` | Tuning notes (DuckDB threads, plot cache, chunking) |
| `S3_UPLOAD.md` | S3 layout + sync strategy |
| `JUPYTERLITE_BUILD.md` | How `frontend/jupyter/` becomes `webapp/jupyter/` |
| `WEB_INTERFACE.md`, `WEBAPP-WORKFLOW.md` | Web UI design notes |
| `CONFIG.md` | `anthive.yaml` reference |
| `ERROR_HANDLING.md` | Error conventions across tools |
| `DEVELOPMENT.md` | How to set up a dev environment |
| `QUICKSTART.md` | Getting-started for someone brand new |

`doc/QA_FINDINGS_2026-05-19.md` is a frozen QA snapshot from May 2026 —
historical, intentionally not updated to current paths.

---

## Conventions

- **Every `bin/<domain>/ant-*` is a `uv run --script`** with PEP-723
  dependencies in the shebang. Adding a dep = editing the script's
  `# dependencies = [...]` block. No shared venv.
- **`sys.path` shim**: scripts find lib via
  `script_dir.parent.parent / 'lib'` (two levels up to repo root).
- **The `ant` dispatcher** (`bin/ant`) walks all six subfolders to find
  `ant-<cmd>`, so `ant convert ...` works regardless of which subfolder
  holds the script.
- **Heavy-deps Python** (scanpy/anndata/R) uses the `anthive-data`
  micromamba env at `envs/anthive-data`. Most things just use `uv`.
- **`_`-prefixed folders** under the data stores are staging/archive,
  excluded everywhere. Inactive datasets are marked with `.inactive`
  sidecars.
- **`data/`, `webapp/`, `tmp/`, `node_modules/`, build artifacts** are
  gitignored.

---

## Where to start (by role)

- **Adding a plot geom or query endpoint**: `bin/api/CLAUDE.md` →
  `bin/api/ant-serve` → `lib/plotting.py` or `lib/anthive_db.py`.
- **Tweaking the UI**: `frontend/browser/CLAUDE.md` → `frontend/browser/templates/`.
- **Importing a new dataset**: `doc/H5AD_INGESTION_SPEC.md` → `bin/ingest/CLAUDE.md` → `./bin/ant convert`.
- **Computing module scores**: `bin/annot/CLAUDE.md` → `./bin/ant score`.
- **Anything DuckDB-internal**: `lib/CLAUDE.md` → `lib/anthive_db.py`.
- **The supervisor or sync misbehaving**: `bin/ops/CLAUDE.md` → `tmp/caddy.log` + `./bin/ops/ant-supervisor status`.
