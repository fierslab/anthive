# Design — annotation updates without full duckdb rebuilds

**Status:** discussion only — not yet implemented.
**Date:** 2026-05-23

## Problem

The current flow for adding new obs columns to a dataset is:

1. `ant-score` writes new columns into the `.h5ad` in place (atomic .tmp + rename)
2. `ant-convert` rewrites the entire `.duckdb` from scratch
3. `ant-sync push` uploads the new h5ad and duckdb to S3

For a 114 GB h5ad (e.g. Gabitto MTG_ExN) this means:

- ant-convert: 30–60 min CPU+IO
- duckdb size: ~20–40 GB → another 5–15 min S3 upload
- h5ad size: 114 GB → 1+ hour S3 upload if it changed

Adding 13 module-score columns to a single dataset thus blocks ~1.5–2 hours of pipeline time. A batch of 20 such updates blocks the better part of a day. This is the dominant friction in the current workflow.

## Current architecture (recap)

```
h5ad (source of truth) ──[ant-convert]──▶ duckdb (served)
   ▲                                          ▲
   │ ant-score writes here                    │ API queries here
```

Every annotation flows through the .h5ad first; the .duckdb is a pure derivative.

## Option A — `ALTER TABLE` incremental updates on the duckdb

Skip the h5ad write; mutate the duckdb directly.

### Mechanics

1. `ant-score` (or any annotator) computes new obs columns
2. Open the dataset's `.duckdb` read/write
3. `ALTER TABLE obscat ADD COLUMN <name> VARCHAR` (or `obsnum ADD COLUMN ... DOUBLE`)
4. Stream new values, joined on `cell_name`
5. Update the .md include lists
6. (optionally, deferred) reflect the change back into the .h5ad — see "deferred merge" below

### Pros

- Per-update cost drops from 30–60 min to **seconds**
- No giant uploads to S3 — only the .md (git) plus the slightly larger .duckdb get pushed
- No new file types, no new sync rules, no new schema introspection
- Existing tooling (ant-serve, MCP, marimo, analytics_query) continues to query a single duckdb — zero downstream changes

### Cons

- The .h5ad goes out of sync with the .duckdb (intentional, but a footgun if someone re-runs ant-convert without realising)
- Adding only — column rename/delete still needs full rebuild (rare in practice)
- DuckDB has to be opened read/write briefly; needs careful locking against ant-serve's read-only connection pool. Mitigation: ant-serve's connection pool is read-only on a separate file handle; concurrent writes are fine on the WAL layer, but we'd want to pause / invalidate the metadata cache afterward (already a known issue — see `d92651c` rescan pool-close fix)
- Type promotion is fiddly — if a numerical column has mixed int/float, the ADD COLUMN type must match. Best to refuse on ambiguity and force the annotator to specify

### Effort

~1–2 days. Components:

- `lib/anthive_db.py` — add `add_obscat_column()`, `add_obsnum_column()` methods that use `ALTER TABLE` + batched INSERT
- `bin/ant-score` — switch the writeback path from "edit h5ad + rebuild" to "edit duckdb directly" (keep --rebuild as fallback)
- Need a clear convention for what gets written: only WGCNA cols? all annotation? Probably both modes

## Option B — sidecar `.extras.duckdb` files

Don't touch either the main duckdb or the h5ad; write a separate file per dataset that holds added annotation.

### Mechanics

```
Sierksma2025/Pharmacogenomics/
    Pharmacogenomics.h5ad          # untouched
    Pharmacogenomics.duckdb        # untouched
    Pharmacogenomics.extras.duckdb # ← new column here
    Pharmacogenomics.md            # obscat_include lists ALL cols
                                   # (main + extras, with optional source tag)
```

At query time the API `ATTACH`es the sidecar, exposes its cols via a UNION view or per-column lookup.

### Pros (over Option A)

- Multiple independent annotation sources can each own a sidecar (Sierksma WGCNA, donor lookup, clustering reanalysis) without coordinating
- Cleaner rollback — `rm Pharmacogenomics.extras.duckdb` reverts cleanly
- Cleaner provenance — each sidecar has its own sha256/version/source ID

### Cons (over Option A)

- Every obs query becomes a JOIN across two (or more) duckdbs
- Schema introspection has to ATTACH each sidecar — slower API cold start
- Cell-id alignment is fragile: sidecar must use cell_name (slower joins) or sidecar must be regenerated whenever the main duckdb is rebuilt
- Tooling now has to know about multiple files: ant-sync, analytics_query, the MCP tools, marimo notebooks
- `obs_exclude` / `obscat_include` lists need a column → file mapping, or a strict "one sidecar per dataset" rule
- AnnData consumers (Python notebooks pulling h5ad directly) STILL don't see the new columns

### Effort

~1–2 weeks. Components:

- New sidecar format spec (cell_name + columns, no X)
- ConnectionPool refactor to ATTACH sidecars at open time
- ant-serve metadata loader to introspect main + sidecars together
- ant-sync to recognise `*.extras.duckdb` (push-only, never pull)
- ant-score to write to sidecar instead of main duckdb
- analytics_query SQL passthrough may need re-namespacing

Recommendation: only worth doing once there's a second independent annotation source besides ant-score.

## Reverse merge — bringing the h5ad back in sync

If we go with Option A (write only to duckdb), the h5ad drifts. At some point we want to update the h5ad too, so that:

- Direct anndata/scanpy consumers see the new cols
- Disaster recovery (rebuild duckdb from h5ad) still works
- The h5ad remains the long-term cold-storage canonical record

### Mechanics

HDF5 supports adding new datasets to an existing file **without rewriting the rest**. The X matrix stays intact.

Sketch:

```python
import h5py
import numpy as np
import duckdb

con = duckdb.connect(duckdb_path, read_only=True)
with h5py.File(h5ad_path, 'r+') as f:
    cell_names_h5ad = f['obs']['_index'][:]
    h5_idx_of = {n.decode(): i for i, n in enumerate(cell_names_h5ad)}

    for col in new_cols_in_duckdb_but_not_h5ad:
        rows = con.execute(
            f'SELECT c.cell_name, o."{col}" '
            f'FROM obscat o JOIN __cells c USING (cell_id)'
        ).fetchall()
        n = len(cell_names_h5ad)
        arr = [None] * n
        for cn, val in rows:
            arr[h5_idx_of[cn]] = val

        if is_categorical(arr):
            cats = sorted({v for v in arr if v is not None})
            code_map = {v: i for i, v in enumerate(cats)}
            codes = np.array(
                [code_map.get(v, -1) for v in arr], dtype='i1'
            )
            grp = f['obs'].create_group(col)
            grp.create_dataset('categories', data=np.array(cats, dtype='S'))
            grp.create_dataset('codes', data=codes)
            grp.attrs['encoding-type'] = 'categorical'
            grp.attrs['encoding-version'] = '0.2.0'
            grp.attrs['ordered'] = False
        else:
            f['obs'].create_dataset(col, data=np.array(arr, dtype='f8'))

    existing = list(f['obs'].attrs.get('column-order', []))
    f['obs'].attrs['column-order'] = np.array(existing + new_cols, dtype='S')
```

### Cost

- Time per dataset: **seconds** (we never touch X)
- File grows: a few MB per column × n_cells
- Memory: tiny (just the new column array)

### Risks

1. **AnnData encoding compatibility** — the `encoding-type`, `encoding-version`, `column-order` attrs need to match what AnnData expects. The spec is well documented in the `anndata` source but fiddly. 3–4 iterations to get right.
2. **HDF5 doesn't reclaim space.** Adding-only is clean. If you ever delete a column, run `h5repack` periodically.
3. **AnnData format evolution.** Future versions might tighten the spec. Mitigation: rewrite the h5ad through `AnnData.write_h5ad` once a year for format migration.
4. **Cell-name correspondence.** Assert that `h5ad obs/_index == duckdb __cells.cell_name` before merging. If a reconvert ever reordered cells, this is broken.

### Effort

~1–2 days for a robust `ant-h5ad-merge` tool. Probably 50–200 lines.

## Deferred-merge architecture

Put it together:

```
                          ALTER TABLE
   annotator (ant-score)──────────────────▶ duckdb (canonical for queries)
                                                ▲
                                                │
                          nightly cron          │ ant-h5ad-merge
                                                ▼
                                           h5ad (canonical for disaster recovery
                                                 and direct python use)
```

- **Hot path** (daily annotation): seconds per update
- **Cold path** (nightly cron): merge any duckdb-only columns into h5ad
- **S3 push**: incremental — only the .md and the small duckdb diff get pushed; h5ad only changes when nightly merge ran

This breaks the "h5ad is source of truth" invariant — instead, the **duckdb is the live source of truth and the h5ad is eventually consistent**. The merge happens on a schedule, not on the critical path.

## My recommendation

1. **Implement Option A (ALTER TABLE)** when ready. Solves the immediate pain. ~1–2 days. Low architectural risk.
2. **Implement the merge tool** at the same time (~1–2 more days). Wire it as a nightly/weekly cron, not on the annotation critical path.
3. **Defer Option B (sidecars)** until you have a concrete second annotator that genuinely needs independent file ownership.

Decision points before implementing:

- Is the duckdb really the authoritative source now? (yes → Option A makes sense; no → you still need to write to h5ad first, killing the speedup)
- How often do columns get renamed/deleted? (rare → ALTER TABLE is fine; frequent → full rebuilds are unavoidable)
- Do we want concurrent annotators? (yes → sidecars; no → ALTER TABLE)

## Related issues

- `ant-h5ad-check` annotation wipe (QA finding) — losing `id` fields when regenerating .md is the SAME class of bug. A unified annotation-update tool should preserve these.
- ant-serve connection pool stale schema after rebuild (fixed in `d92651c`) — ALTER TABLE will need to invalidate the pool too. Same mechanism.
- Backend `/cells` JSON serialization perf — orthogonal but related; sample/limit is the workaround.
