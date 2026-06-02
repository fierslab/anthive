# bin/ — CLI tools

Two families share this directory. Both import `lib/` via
`sys.path.insert(0, script_dir.parent / 'lib')`.

Every tool is a standalone `#!/usr/bin/env -S uv run --quiet --script` with
inline PEP-723 deps in the header. **To add a dependency, edit that script's
`# dependencies = [...]` block** — there is no shared venv. `ant` is the
dispatcher (`ant serve` → `ant-serve`, etc.).

## Server family

| Tool | Role |
| --- | --- |
| `ant-serve` | FastAPI REST API over the duckdb store (port 8080) |
| `ant-mcp` | MCP server exposing search/plot/analytics (port 8090) |
| `ant-browse` | server-rendered web browse UI (port 8081) |
| `ant-supervisor` | runs the whole stack (serve+browse+mcp+streamlit+caddy), multiplexes logs |

### ant-serve — the bits that bite

- **Connections are read-only.** `ConnectionPool` opens each duckdb with
  `read_only=True` and pools up to N handles per db. Metadata is cached;
  `POST /admin/rescan` clears and reloads.
- **DuckDB blocks cross-process read-write.** DuckDB ≥1.5 refuses to open a
  file read-write while ant-serve holds it read-only (and vice-versa). So
  annotation writers cannot run while ant-serve has the file open. The
  coordination protocol:
  - `POST /admin/release/{db_id}` — pool closes idle handles, marks the
    db_key evicted (new requests get `503 Retry-After`), waits for in-use
    handles to drain, self-expires after 5 min.
  - `POST /admin/restore/{db_id}` — clears the eviction.
  - Writers (`AnnotationWriter` in `lib/`) call release→write→restore
    automatically when given `db_id` + `ant_serve_url`.
- `root_path="/api"` — the app is mounted under `/api`; a GET to `/api`
  (no trailing slash) 307-redirects to `/api/`. Always build URLs as
  `…/api/` (clients/notebooks do this).
- Field-type detection (gene vs obs-num vs obs-cat vs embedding) is automatic
  from the name; plot/query endpoints rely on it.
- `analytics_query` runs sandboxed SELECT-only SQL via `lib/analytics.py` —
  see `tests/test_analytics_security.py` before touching the sandbox.

## Pipeline family

Ingest → annotate → sync. None of these should run while ant-serve holds a
write lock on the target — they coordinate via the release endpoint above.

| Tool | Role |
| --- | --- |
| `ant-convert` | `.h5ad` → `.duckdb` (full rebuild). Refuses if `__pending_cols` non-empty unless `--force-discard-pending` |
| `ant-score` | scanpy module scores from the genesetstore → obs columns (default: writes duckdb via AnnotationWriter; `--rebuild` = legacy h5ad rewrite) |
| `ant-starcat` | project a dataset onto the `MICROGLIA_V1` starCAT catalog → 43 `starcat__MICROGLIA_V1__*` obs columns. Human-only; needs raw integer counts |
| `ant-annotate` | add obs columns from a TSV; `status` / `rollback --run-id` |
| `ant-merge-h5ad` | cold-path: merge pending duckdb obs columns back into the h5ad without rewriting X. Self-heals half-completed merges |
| `ant-sync` | push/pull `.h5ad` + `.duckdb` to/from S3 (never `.md`) |
| `ant-md`, `ant-md-check` | read/write & validate `.md` frontmatter (anthive-catalog fields only — annotation fields live in `.h5ad.mus` after 2026-06, `ant-md` refuses to set them) |
| `ant-check`, `ant-h5ad-check`, `ant-duckdb-check`, `ant-api-check`, `ant-s3-check` | validators |
| `ant-geneset`, `ant-inactive`, `ant-cellxgene`, `ant-db`, `ant-build-*` | geneset mgmt, soft-delete, CxG import, db inspection, builds |

### Annotation write path (shared by score/starcat/annotate)

All three write through `lib/anthive_db.AnnotationWriter` (see `lib/CLAUDE.md`):
flock the duckdb, `ALTER TABLE __obsnum/__obscat ADD COLUMN`, record in
`__pending_cols`, refresh the `obsnum`/`obscat` views, commit. They resolve
`ant_serve_url` from `anthive.yaml` and `db_id` from the `.md`; `--no-coordinate`
opts out when ant-serve is down.

## Testing

`make test` (= `PYTHONPATH=lib python -m pytest tests/`). The annotation-update
suite needs duckdb/pandas/h5py — run via
`uv run --with pytest --with duckdb --with pandas --with numpy --with h5py --no-project python -m pytest tests/test_annotation_writer.py …`.
