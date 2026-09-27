# anthive4

Single-cell RNA-seq data browser. Curated `.h5ad` datasets are converted to
DuckDB, served over a REST API, and explored via web UI, an MCP server, and
in-browser notebooks. ~60 datasets, mostly human microglia.

## The one thing to internalise: `lib/` is the shared core

Every `bin/<domain>/ant-*` tool and the server import the same `lib/`
modules via a `sys.path.insert(0, lib_dir)` shim (NOT a pip install). So:

- A change in `lib/` ripples to **every** tool and the server at once.
- There is no version boundary between `lib/` and its consumers — treat it as
  one tightly-coupled unit. This is why the codebase is one repo, not many.

The Python REST client lives in its own repo at
[`codeberg.org/mfiers/pyvark`](https://codeberg.org/mfiers/pyvark)
(importable as `from vark import AnthiveClient`). It used to ship as
`anthive4/antclient/` — that was spun off 2026-06-22 in the same
commit that removed `frontend/streamlit/`.

## Data flow

```
.h5ad  ──ant-convert──▶  .duckdb  ──ant-serve──▶  REST API ──▶ web UI / MCP / notebooks
  ▲                         ▲
  │  ant-merge-h5ad         │  ant-score / ant-starcat / ant-annotate
  └─────────(pending)───────┘   write obs columns here first (fast path),
                                 sync back to .h5ad later (cold path)
```

- `.h5ad` is the long-term source of truth (raw + log-norm expression).
- `.duckdb` is the served derivative AND the live annotation target.
- Annotators write obs columns to the duckdb via `ALTER TABLE` (seconds), mark
  them in `__pending_cols`, and `ant-merge-h5ad` reflects them back into the
  h5ad later. See `doc/DESIGN_annotation_updates.md`.

## Sub-project seams (scoped by nested CLAUDE.md)

Each subfolder has its own `CLAUDE.md` so a Claude Code session opened
there auto-loads the right scope.

```
bin/
  api/       ant-serve, ant-mcp                    bin/api/CLAUDE.md
  ingest/    ant-convert, ant-merge-h5ad, …        bin/ingest/CLAUDE.md
  annot/     ant-score, ant-starcat, ant-suggest-tops, …   bin/annot/CLAUDE.md
  meta/      ant-md, ant-check, ant-cellxgene, …   bin/meta/CLAUDE.md
  ops/       ant-supervisor, ant-sync, dev-*       bin/ops/CLAUDE.md
  ant                                               ← dispatcher (walks subdirs)
web/         ant-browse + browser.py + templates/  web/CLAUDE.md      ← top-level web UI
lib/         shared core (anthive_db, plotting, …)  lib/CLAUDE.md
frontend/
  jupyter/   JupyterLite source (Pyodide), vendors codeberg.org/mfiers/pyvark
             as `vark/`                              frontend/jupyter/CLAUDE.md
config/      Caddyfile (single source of path routing on dev + prod),
             caddy-{tls,auth,http-redirect}.{dev,prod} (env-toggled snippets),
             nginx.conf (thin shim → Caddy for `make serve-nginx`),
             nginx-intern-anthive.conf (drop-in prod vhost),
             Caddyfile-prod-snippet (annotated env-var reference),
             caddy-auth.example (basic-auth template)
```

The web UI is now a **top-level seam** (`./web/`), not an `ant` subcommand.
That makes "the web UI" a self-contained Claude Code scope: `cd web/` and
you have the FastAPI router (`browser.py`), the Jinja templates, the static
assets, and the launcher (`ant-browse`) all under one folder.

| Working on… | `cd` here | Scope loaded |
| --- | --- | --- |
| REST/MCP plot or query endpoints | `bin/api/` | root + bin + bin/api |
| Web UI — router, templates, CSS, JS | `web/` | root + web |
| h5ad → duckdb ingestion | `bin/ingest/` | root + bin + bin/ingest |
| Scoring / curation | `bin/annot/` | root + bin + bin/annot |
| .md / .mus / catalog | `bin/meta/` | root + bin + bin/meta |
| Sync / supervisor / dev wrappers | `bin/ops/` | root + bin + bin/ops |

Data stores live in **separate git repos**: `data/h5adstore/` (`.md` + `.h5ad`
+ `.duckdb`), `data/genesetstore/`. The `.md` files are git-tracked; `.h5ad`
and `.duckdb` are synced to S3 via `bin/ops/ant-sync` (never committed).

## Conventions specific to this repo

- **Every `bin/<domain>/ant-*` tool is a self-contained `uv run --script`**
  with inline PEP-723 dependencies in the shebang header. Adding a dependency
  = editing that script's `# dependencies = [...]` block, NOT a shared venv.
  A missing dep there fails silently as an `ImportError` caught by a
  `try/except` (this has bitten us — e.g. `pyyaml` missing in `ant-merge-h5ad`).
- **The `ant` dispatcher** in `bin/ant` walks all subfolders to find an
  `ant-<cmd>` script, so `ant convert ...` works the same after the reorg.
- **`sys.path` shim**: scripts use `script_dir.parent.parent / 'lib'` (now
  two levels up, since they live in `bin/<domain>/`). The supervisor in
  `bin/ops/` uses `Path(__file__).parent.parent.parent` to reach repo root.
- Config (`anthive.yaml`) is loaded via `lib/config.py`; S3 creds + API
  host/port live there.
- Data-management Python that needs heavy deps (scanpy/anndata/R) uses the
  `anthive-data` micromamba env at `envs/anthive-data`.
- `_`-prefixed folders under the stores are staging/archive — excluded
  everywhere. Inactive datasets are marked with `.inactive` sidecars.

## Deployment: containers (the supported way)

Sites run as a docker compose stack described by one `site.yaml` and driven
by `bin/ops/ant-stack` (`doc/DEPLOY_CONTAINERS.md`). HPC ingest uses uv or
the Apptainer image built from `docker/anthive-ingest.def` (`doc/HPC.md`).
Images: `docker/Dockerfile` (one pinned venv from `docker/requirements-app.lock`,
regenerated by `make lock` when a script header changes) and
`docker/Dockerfile.ingest` (adds R/Seurat); internals in `doc/DOCKER.md`.

Every location a deployment may move (store, `state_dir`, `tmp_dir`,
`log_dir`, `webapp_dir`, DuckDB spill, ant-convert staging, ant-serve URL)
resolves through `lib/config.py` helpers with env > anthive.yaml > default
precedence, and `ANTHIVE_CONFIG` names a config outside the repo. **Never
hard-code a path under the repo for runtime writes** (the images mount the
tree read-only); add a key to `anthive.default.yaml` and a `get_*` helper.

## Where things are documented

`doc/` holds design + process docs (`ARCHITECTURE.md`,
`DESIGN_annotation_updates.md`, `CHECK_PROCESS.md`, …). `report/` holds
rendered scientific output. Keep design decisions in `doc/`, not inline.
