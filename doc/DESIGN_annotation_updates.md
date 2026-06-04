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
- Existing tooling (ant-serve, MCP, streamlit, analytics_query) continues to query a single duckdb — zero downstream changes

### Cons

- The .h5ad goes out of sync with the .duckdb (intentional, but a footgun if someone re-runs ant-convert without realising)
- Adding only — column rename/delete still needs full rebuild (rare in practice)
- DuckDB has to be opened read/write briefly; needs careful locking against ant-serve's read-only connection pool. Mitigation: ant-serve's connection pool is read-only on a separate file handle; concurrent writes are fine on the WAL layer, but we'd want to pause / invalidate the metadata cache afterward (already a known issue — see `d92651c` rescan pool-close fix)
- Type promotion is fiddly — if a numerical column has mixed int/float, the ADD COLUMN type must match. Best to refuse on ambiguity and force the annotator to specify

### Effort

~1–2 days. Components:

- `lib/anthive_db.py` — add `add_obscat_column()`, `add_obsnum_column()` methods that use `ALTER TABLE` + batched INSERT
- `bin/annot/ant-score` — switch the writeback path from "edit h5ad + rebuild" to "edit duckdb directly" (keep --rebuild as fallback)
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
- Tooling now has to know about multiple files: ant-sync, analytics_query, the MCP tools, the streamlit app
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

---

# Formalized contract (2026-05-25)

This section pins down the **concrete interfaces** for Option A + deferred merge.
Everything above is rationale; this is the spec the implementations target.

## 1. State model — how drift is tracked

A dataset can be in one of three states with respect to its h5ad ↔ duckdb pair:

| State | duckdb has | h5ad has | meaning |
| --- | --- | --- | --- |
| `clean` | cols A,B,C | cols A,B,C | converged — last `ant-convert` or merge was complete |
| `dirty` | cols A,B,C,**D** | cols A,B,C | duckdb has new cols not yet merged back to h5ad |
| `divergent` | cols A,B,**X** | cols A,B,**Y** | someone edited both sides independently — refuse to merge |

**State is stored ONLY in the duckdb** — never in the .md. The .md is a
semi-public, collaborator-editable file in git; pipeline state has no business
there. The single record is a `__pending_cols` table inside the duckdb:

```sql
CREATE TABLE __pending_cols (
    col_name      VARCHAR PRIMARY KEY,
    target_table  VARCHAR NOT NULL,    -- 'obscat' | 'obsnum'
    kind          VARCHAR NOT NULL,    -- 'cat' | 'num'
    added_at      TIMESTAMP NOT NULL,
    annotator     VARCHAR NOT NULL,    -- 'ant-score' | 'ant-starcat' | …
    run_id        VARCHAR NOT NULL,
    catalog       VARCHAR,             -- annotator-specific tag (e.g. MICROGLIA.V1)
    source_hash   VARCHAR              -- sha256 of the input spec
);
```

The merge tool **also** independently sanity-checks by diffing the live column
lists between duckdb and h5ad (see §6). `__pending_cols` is the audit trail;
the duckdb-vs-h5ad column diff is the ground truth that gets merged.

The `run_id` lets the merge tool back-trace which annotator wrote which
columns, and lets us roll back a single bad run without touching the others.

### Hazard: `ant-convert` wiping pending state

Because `__pending_cols` lives in the duckdb, a fresh `ant-convert <dataset>`
(rebuild from h5ad) silently destroys both the pending columns AND the record
of them. Mitigation — **dual check** before rebuild:

1. Read the existing duckdb's `__pending_cols` table → list A
2. Independently diff existing duckdb obs columns vs h5ad obs columns → list B
3. **Either list non-empty → refuse rebuild.** Belt-and-suspenders: a missing
   `__pending_cols` row (e.g. from a partially-failed writer) is still caught
   by the column diff, and vice versa.
4. Override flag `--force-discard-pending` exists, but logs the union of A and
   B prominently before discarding, and is never the default.

## 2. Annotator contract

A tool that wants to add columns calls `lib.anthive_db.AnnotationWriter` and must provide:

```python
class AnnotationWriter:
    def __init__(self, duckdb_path: Path, md_path: Path, *, annotator: str, run_id: str):
        """Lock the duckdb for write; refuse if another writer holds the lock."""

    def add_numerical(
        self, col_name: str, values: pd.Series, *,
        source_hash: str | None = None,
        on_conflict: Literal['fail', 'overwrite'] = 'fail',
    ) -> None:
        """values index must be cell_name. ADD COLUMN obsnum.<col_name> DOUBLE; bulk insert."""

    def add_categorical(
        self, col_name: str, values: pd.Series, *,
        source_hash: str | None = None,
        on_conflict: Literal['fail', 'overwrite'] = 'fail',
    ) -> None:
        """values index must be cell_name. ADD COLUMN obscat.<col_name> VARCHAR; bulk insert."""

    def commit(self) -> None:
        """Atomic: update __pending_cols, release lock. Does NOT touch the .md."""

    def rollback(self) -> None:
        """DROP any columns added in this transaction; revert .md; release lock."""
```

Contract obligations on the annotator:

1. **`cell_name` alignment**: values.index must be a subset of duckdb `__cells.cell_name`.
   Extra cells → fail. Missing cells → fill NULL (logged warning).
2. **Type discipline**: numerical = `float64`. Categorical = string-coercible.
   Mixed int/float in numerical → fail with "specify --dtype float".
3. **No overwrite by default**: if a column exists in the target table, fail.
   `on_conflict='overwrite'` is opt-in and never the default for ant-score / ant-starcat CLI.
4. **All-or-nothing**: every `add_*` call inside a `with AnnotationWriter(...)` block
   is part of one transaction. Crash → automatic rollback.

`lib/anthive_db.py` grows ~150 lines for this; everything else stays.

## 3. CLI surface

### `ant-annotate` (new — thin wrapper around AnnotationWriter)

```
ant-annotate add-num <dataset.md> <col> <tsv>   # tsv: cell_name<TAB>value
ant-annotate add-cat <dataset.md> <col> <tsv>
ant-annotate status <dataset.md>                # show pending cols
ant-annotate rollback <dataset.md> --run-id sc-7f3a   # undo one run
```

Not the primary interface (annotators use the Python API directly), but useful for
ad-hoc tsv ingest and for testing.

### `ant-score` (existing — extended)

Replace its current "edit h5ad + queue rebuild" path with `AnnotationWriter`. The
`--rebuild` flag is **kept** for the rare case where someone needs a fresh duckdb
(rename/delete columns, change of underlying h5ad).

### `ant-merge-h5ad` (new — the cold path)

```
ant-merge-h5ad <dataset.md>           # merge pending cols from duckdb → h5ad
ant-merge-h5ad --all                  # walk h5adstore, merge every dirty dataset
ant-merge-h5ad --dry-run <dataset.md> # report what would change
```

Procedure:

1. Open duckdb read-only, read `__pending_cols` → expected list
2. Open h5ad read-only, list `obs/` keys → existing list
3. Compute the actual diff: `(obscat ∪ obsnum) − obs_keys`
4. Cross-check: actual diff == `__pending_cols.col_name`. Mismatch → refuse,
   dump both sides, exit nonzero (this is the `divergent` state guard)
5. For each pending column: read values from duckdb, write into h5ad/obs/
6. Update `obs.attrs['column-order']`
7. Reopen duckdb r/w, `DELETE FROM __pending_cols WHERE col_name IN (...)`
8. Touch h5ad mtime so `ant-sync push` picks it up

Cadence: **manual for now** — no cron. You run it before a push. We'll
revisit cadence once we see how often the dirty window matters in practice.

### `make sync-update` (existing — semantics extended)

For now: pull → build-stale → push. The user runs `ant-merge-h5ad --all` by
hand when they want pending columns to ride along on the next push.

(Once we want this automated: insert `merge-pending` between `build-stale`
and `push`. Trivially additive — the merge step is idempotent on `clean`
datasets.)

## 4. Locking and concurrency

Three potential writers, one reader (ant-serve):

| Actor | Read | Write | Mode |
| --- | --- | --- | --- |
| ant-serve | yes | no | persistent read-only handle pool |
| ant-score | yes | yes | short transactional write |
| ant-starcat | yes | yes | short transactional write |
| ant-merge-h5ad | yes | reads duckdb; writes h5ad | duckdb read-only |
| ant-convert (rebuild) | no | rewrites duckdb file | exclusive |

Mechanism:

- **Cooperative file lock**: `<dataset>.duckdb.lock` (flock). All writers acquire
  it for the duration of their transaction. ant-serve does not take the lock.
- **Post-write rescan**: writers append the dataset id to
  `data/h5adstore/.rescan_queue`. ant-serve's existing rescan-pool path (see
  `d92651c` fix) picks this up and invalidates the connection for that dataset.
- **Rebuild barrier**: `ant-convert` takes the lock *and* sets a sidecar
  `<dataset>.duckdb.rebuilding` file. AnnotationWriter refuses to open during this.
  Recovery on crash: the lock plus the sidecar both have to be removed manually
  with a documented `ant-convert --reset-lock` (intentional friction).

DuckDB's own WAL is sufficient for the ALTER TABLE itself; the file lock is
about serialising the .md update with the duckdb update.

## 5. Type promotion rules

The trap noted in §"Cons" — numerical column with mixed int/float — is resolved by
declaring an explicit contract:

- `add_numerical` always creates a `DOUBLE` column. Integers coerce up. No surprises.
- `add_categorical` always creates a `VARCHAR` column. Codes / booleans coerce to string.
- If you really want an integer column for a count, name it `..._count` and use
  `add_numerical`; the storage cost is negligible at our scale.

Categorical with extreme cardinality (e.g. cell_id-like strings, >10k unique) is
refused at the API level — the catch from the mass-rebuild plan ("only the really
non informative columns are omitted, eg, 20k+ cell-ids") stays enforced.

## 6. Failure modes (explicit)

| Failure | Detection | Recovery |
| --- | --- | --- |
| Annotator crashes mid-`add_*` | lock held, no commit | rollback in `__exit__` |
| Annotator crashes between `add_*` and `commit` | lock held, no commit | rollback in `__exit__` |
| Merge tool crashes mid-h5ad-write | h5ad has partial column | h5ad mtime not bumped, `__pending_cols` not cleared → next run retries cleanly |
| Cell-name mismatch (h5ad re-ordered) | merge tool asserts | refuse to merge, require explicit `ant-convert` |
| Two writers race | flock blocks the second | second one waits or errors after timeout |
| `ant-convert` run while dirty | `__pending_cols` non-empty in old duckdb | refuse rebuild unless `--force-discard-pending` |
| `__pending_cols` diverges from actual col diff | merge-time sanity check (§3 step 4) | refuse, dump both sides, require human |

## 7. starcat as the concrete first user

starcat (`/data/1/project/starcatstore/microglia/`) is the motivating use case
and worth walking through end-to-end to validate the design.

### What starcat needs from us

- **Raw integer counts** (not log-norm). Anthive4 stores log-norm in `adata.X`
  per [feedback_h5ad_layer_convention.md]. starcat reads h5ad directly — it
  does **not** go through ant-serve / duckdb.
- **HGNC symbols** in `var_names`. We already track this via `var_names_kind`.
- **Human myeloid cells.** First iteration is human-only. Mouse datasets follow
  the exact same pipeline — we just don't have a mouse catalog yet, so
  `ant-starcat` refuses non-human input with a clear error pointing here.
  Candidate human microglia sets: Blackburn2026, Borrie2025, Simpson2025,
  Sierksma2025 (all human microglia xenografts).

### What starcat produces

For each dataset:

- `usage` DataFrame — 21 numerical columns, one per program (`Microglia_DAM`, etc.)
- `scores` DataFrame — ~10 mixed columns: 4 numerical composites, ~5 boolean
  `*_pos`/`*_high`, 1 categorical `Identity_Label`.

Per dataset that's roughly **25 numerical + 1 categorical** new obs columns.

### Column naming convention

Every starcat-written column is named:

```
starcat__<CATALOG>__<PROGRAM_OR_SCORE>
```

with `__` (double underscore) as the separator. Reasoning:

- `__` is purely syntactic — no SQL operator collides with it, so columns are
  bare identifiers (no quoting needed in `SELECT`, no quoting needed in raw
  duckdb CLI / MCP analytics_query).
- Three-part structure makes provenance grep-able and lets a future v2
  catalog coexist (`starcat__MICROGLIA_V2__…` next to `starcat__MICROGLIA_V1__…`).
- Catalog version uses `_` instead of `.` (so `V1` for `MICROGLIA.V1`, not
  `V1.0`) — also to keep bare-identifier-safe.

For the first catalog at `/data/1/project/starcatstore/microglia/catalog/`:
**catalog tag = `MICROGLIA_V1`**.

Example columns from one starcat run:

| Field | Column name | obs table |
| --- | --- | --- |
| Program usage | `starcat__MICROGLIA_V1__Microglia_DAM` | obsnum |
| Composite | `starcat__MICROGLIA_V1__DAM_progression` | obsnum |
| Binary call | `starcat__MICROGLIA_V1__IRM_pos` | obscat |
| Identity argmax | `starcat__MICROGLIA_V1__Identity_Label` | obscat |

(Alternative considered: `starcat::MICROGLIA.V1::Microglia_DAM` per the verbal
spec. Rejected because `::` is the SQL cast operator and `.` is the
table-qualifier — both require double-quoting on every reference, which
silently breaks any hand-written SQL. The `__` form preserves the same
three-part structure with no quoting tax.)

### Wiring it in

A new `bin/annot/ant-starcat` does:

1. Refuse if `annotation.organism != 'human'` (mouse catalog doesn't exist yet).
2. Locate raw counts in the source h5ad. Tries `adata.X` → `adata.layers["counts"]`
   → `adata.raw.to_adata()`. Bails if none look like integers (§5.1 of ANNOTATE_CELLS.md).
3. Verify HGNC symbol overlap with catalog ≥ 1500 (§5.2 hard threshold).
4. Optionally subset to myeloid cells based on a configurable `cell_type` column.
5. Run `model.fit_transform(adata)` against `MICROGLIA.V1.0`.
6. Open `AnnotationWriter(duckdb_path, md_path, annotator='ant-starcat',
   run_id=..., catalog='MICROGLIA_V1')`.
7. `add_numerical("starcat__MICROGLIA_V1__Microglia_DAM", …)` × 21 programs
8. `add_numerical("starcat__MICROGLIA_V1__DAM_progression", …)` × 4 composites
9. `add_categorical("starcat__MICROGLIA_V1__IRM_pos", …)` × ~5 binary calls
10. `add_categorical("starcat__MICROGLIA_V1__Identity_Label", …)`
11. Commit. Write a sub-report under `report/starcat/<dataset>.md` with the
    quality flags from §7.5 of ANNOTATE_CELLS.md (gene overlap, residual,
    whole-cell baseline fraction, DC fraction).

### Environment

starcat pins `scikit-learn==1.3.2` and is incompatible with most of our other
deps. It gets its own micromamba env per the global CLAUDE.md rules:
`envs/anthive-starcat/`. `ant-starcat` is a shell script that activates this
env and runs the Python pipeline.

### Why this is a good first user

- Touches every part of the new pipeline (writer + .md + merge later).
- 25+ columns at once exercises the bulk path, not just the single-column case.
- Raw-counts requirement forces us to think about h5ad-source-of-truth-for-X
  vs duckdb-source-of-truth-for-obs early.
- Outputs are valuable to existing analyses — `starcat_Identity_Label` and
  `starcat_DAM_progression` are the kind of columns we'd plot in the streamlit
  views the moment they exist.

## 8. Decisions log (resolved on 2026-05-25 with MF)

1. **State location**: duckdb only (`__pending_cols`). The .md stays clean —
   it's a semi-public, git-tracked, collaborator-editable file and has no
   business holding pipeline state. Merge tool sanity-checks via independent
   duckdb-vs-h5ad column diff.
2. **starcat column naming**: `starcat__<CATALOG>__<PROGRAM>` with `__` as
   separator. First catalog tag is `MICROGLIA_V1`. Bare-identifier-safe; no
   quoting needed. (Rejected `::`/`.` form because of SQL operator collision.)
3. **Merge cadence**: manual for now. User runs `ant-merge-h5ad` by hand
   before a push. Revisit once we see how the dirty window behaves.
4. **Push integration**: `ant-merge-h5ad` does not push; `ant-sync push` does
   that on its normal schedule.
5. **Mouse datasets**: same pipeline, but human-only first iteration. No
   mouse catalog exists yet; `ant-starcat` refuses non-human input with a
   clear error.

## 9. Implementation order

1. `lib/anthive_db.py` — `AnnotationWriter` + `__pending_cols` table creation
2. `bin/ingest/ant-convert` — refuse-on-pending guard + `--force-discard-pending`
3. `bin/annot/ant-annotate` — tsv ingest + status + rollback
4. `bin/ingest/ant-merge-h5ad` — cold-path merge with sanity check
5. `bin/annot/ant-score` — switch its write path to `AnnotationWriter`
6. `bin/annot/ant-starcat` + `envs/anthive-starcat/` — first real user
7. Pick one human microglia dataset, run end-to-end (annotate → merge → push),
   confirm the streamlit views see the new columns

## 10. Known issue — DuckDB cross-process lock (discovered 2026-05-25)

While running the end-to-end test on `Schirmer2019/schirmer2019_microglia.duckdb`,
the writer failed with:

```
IOException: IO Error: Could not set lock on file "...duckdb":
Conflicting lock is held in .../python3.11 (PID 2701820) by user u0089478.
However, you would be able to open this database in read-only mode.
```

`ant-serve` was running and held the duckdb open with `read_only=True`.
**DuckDB 1.5.3 still blocks a different process from opening the same file
read-write, even when the existing holders are all read-only.** The
"AnnotationWriter coexists with ant-serve" assumption (§4 above) is wrong
under the current DuckDB version.

### Workaround options (no decision yet)

1. **Stop ant-serve during writes.** Cleanest semantics; awkward UX.
2. **`POST /admin/release/<db_id>`** endpoint that closes the cached connection
   in ant-serve's pool, then writer takes the lock, then ant-serve reopens
   on the next query. Needs a small ant-serve change and a write-then-rescan
   step in every annotator.
3. **Schedule annotation when ant-serve is down** (overnight cron).

The writer code itself is correct — the unit tests cover every path. This
is purely a deployment / operations question. Marking the end-to-end task as
*partial* until we pick a workaround.

### Implication for current code

The writer's flock + sentinel design still makes sense (it protects against
two annotators racing each other). The only missing piece is coordination
with ant-serve, which is an ant-serve change, not an AnnotationWriter change.

### Resolution (2026-05-25 — same day)

Added two admin endpoints to ant-serve:

- `POST /admin/release/{db_id}` — closes idle pooled connections, marks the
  db_key as evicted (so new requests get a 503 with `Retry-After`), waits up
  to 30s for in-use connections to drain. Eviction self-expires after 5 min
  so a crashed writer can't lock ant-serve out indefinitely.
- `POST /admin/restore/{db_id}` — clears the eviction. Issued by the writer's
  `__exit__` so ant-serve resumes serving the dataset immediately.

`AnnotationWriter` gained optional `db_id` and `ant_serve_url` kwargs. When
DuckDB raises `IOException: Conflicting lock`, the writer POSTs the release
endpoint, waits for `ok=True`, retries the r/w open, and on `__exit__`
issues a restore.

The CLI tools (`ant-annotate`, `ant-score`, `ant-starcat`, `ant-merge-h5ad`)
resolve the URL from `anthive.yaml` (`rest_api_host` + `rest_api_port`) and
the db_id from the dataset's `.md` / `index.md`. `--no-coordinate` opts out
(use when ant-serve is not running). 503s during writing show up to API
consumers as transient errors with `Retry-After: 5`.

Tested with a real cross-process lock conflict via a sidecar Python process
(`tests/test_ant_serve_release.py`, 4 scenarios). End-to-end test on a real
dataset is unblocked once ant-serve is restarted with the new endpoints.
