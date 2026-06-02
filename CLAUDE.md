# anthive4

Single-cell RNA-seq data browser. Curated `.h5ad` datasets are converted to
DuckDB, served over a REST API, and explored via web UI, an MCP server, and
in-browser notebooks. ~60 datasets, mostly human microglia.

## The one thing to internalise: `lib/` is the shared core

Every `bin/ant-*` tool and the server import the same `lib/` modules via a
`sys.path.insert(0, lib_dir)` shim (NOT a pip install). So:

- A change in `lib/` ripples to **every** tool and the server at once.
- There is no version boundary between `lib/` and its consumers — treat it as
  one tightly-coupled unit. This is why the codebase is one repo, not many.

`antclient/` is the exception: a standalone pip-installable REST client with
**no** `lib/` dependency. It's the only piece structured for extraction today.

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

| Area | What | Nested CLAUDE.md |
| --- | --- | --- |
| `lib/` | shared core (the contract everything depends on) | `lib/CLAUDE.md` |
| `bin/` | CLI tools — **server** (ant-serve/mcp/browse) + **pipeline** (convert/score/starcat/…) | `bin/CLAUDE.md` |
| `antclient/` | standalone REST client package | `antclient/CLAUDE.md` |
| `webapp-src/` | JupyterLite example notebooks (in-browser, Pyodide) | `webapp-src/CLAUDE.md` |

Data stores live in **separate git repos**: `data/h5adstore/` (`.md` + `.h5ad`
+ `.duckdb`), `data/genesetstore/`. The `.md` files are git-tracked; `.h5ad`
and `.duckdb` are synced to S3 via `ant-sync` (never committed).

## Conventions specific to this repo

- **Every `bin/ant-*` tool is a self-contained `uv run --script`** with inline
  PEP-723 dependencies in the shebang header. Adding a dependency = editing
  that script's `# dependencies = [...]` block, NOT a shared venv. A missing
  dep there fails silently as an `ImportError` caught by a `try/except` (this
  has bitten us — e.g. `pyyaml` missing in `ant-merge-h5ad`).
- Config (`anthive.yaml`) is loaded via `lib/config.py`; S3 creds + API
  host/port live there.
- Data-management Python that needs heavy deps (scanpy/anndata/R) uses the
  `anthive-data` micromamba env at `envs/anthive-data`.
- `_`-prefixed folders under the stores are staging/archive — excluded
  everywhere. Inactive datasets are marked with `.inactive` sidecars.

## Where things are documented

`doc/` holds design + process docs (`ARCHITECTURE.md`,
`DESIGN_annotation_updates.md`, `CHECK_PROCESS.md`, …). `report/` holds
rendered scientific output. Keep design decisions in `doc/`, not inline.
