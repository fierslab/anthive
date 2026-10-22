# bin/ — CLI tools, organised by domain

Each script lives in a domain subfolder so a `cd` into any one of them
gives a Claude Code session a clean, narrow scope. Each subfolder has
its own `CLAUDE.md` with the bits that matter for that domain.

| Folder | What it owns | Detail |
| --- | --- | --- |
| `api/` | the running data + protocol servers (`ant-serve`, `ant-mcp`) | `bin/api/CLAUDE.md` |
| `ingest/` | h5ad → duckdb conversion + validation pipeline | `bin/ingest/CLAUDE.md` |
| `annot/` | scoring + obs-annotation tools | `bin/annot/CLAUDE.md` |
| `meta/` | catalog metadata (`.md` / `.mus`), validators, genesets, CellxGene import | `bin/meta/CLAUDE.md` |
| `ops/` | supervisor, S3 sync, dev helpers | `bin/ops/CLAUDE.md` |
| `ant` | dispatcher — walks every subfolder to find an `ant-<cmd>` script. Path stays `bin/ant`. |

The **web UI** lives outside `bin/` now: see `web/` at the repo root. It
is launched via `web/ant-browse` (NOT `ant browse`), supervised by
`bin/ops/ant-supervisor`, and proxied by Caddy/nginx.

## Shared conventions

- **uv-script shebangs**: every `ant-*` is `#!/usr/bin/env -S uv run --quiet --script`
  with PEP-723 deps in the header. Editing deps = editing that header,
  not a shared `pyproject.toml`. No shared venv.
- **Packages that write stored files are capped**: `"duckdb>=1.3,<1.6"` (duckdb 2.0 writes files 1.5 cannot
  read) and `"anndata>=...,<0.13"` (h5ad encodings change between 0.x minors) in every header, matching the
  image lock. `tests/test_dependency_caps.py` fails on a header without the cap; raise a cap only together
  with the lock (`docker/gen-lock`) and every host's script environments.
- **`sys.path` shim**: scripts add `lib/` to `sys.path` via
  `script_dir.parent.parent / 'lib'` (two levels up from `bin/<domain>/`).
- **Coordination with ant-serve**: anything that writes a duckdb (ingest +
  annot) goes through `lib/anthive_db.AnnotationWriter`, which POSTs
  `/admin/release/{db_id}` to ant-serve before opening r/w and
  `/admin/restore/{db_id}` after.
- **`__pending_cols`** is the audit trail for incremental obs writes. See
  `bin/ingest/CLAUDE.md` for the full state machine.

## Tests

`make test` (= `PYTHONPATH=lib python -m pytest tests/`). The annotation-update
suite needs duckdb/pandas/h5py:
```
uv run --with pytest --with "duckdb>=1.3,<1.6" --with pandas --with numpy --with h5py \
    --no-project python -m pytest tests/test_annotation_writer.py
```
