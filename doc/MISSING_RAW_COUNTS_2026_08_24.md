# Missing raw counts — audit 2026-08-24

Read-only audit of every dataset .h5ad in `data/h5adstore/*/` for the presence
of a raw-counts matrix (integer UMIs). Raw counts are required by starCAT and
by any downstream tool that expects a count matrix.

## Method

For each `<file>.md` sibling of a `<file>.h5ad` in `data/h5adstore/`
(excluding `_staging/`, `_papers/`, `_synapse_raw/`, `_*`), open the
`.h5ad` file with `h5py` (structure only, no full-matrix reads) and:

1. list keys in `/layers`, `/raw`, `/X`
2. sample the first 20 000 non-zero values of each candidate location and
   test whether they look integer (fraction of values within 1e-6 of a
   nearest integer > 0.995 and max > 1.5)
3. accept the first location that passes as a raw-counts candidate

Locations tried, in order: `layers/counts`, then alt names
(`UMIs`, `umis`, `raw_counts`, `counts_raw`, `raw`, `spliced`, …), then any
other layer that samples as integer, then `raw/X`, then `X`.

The scan does not use the `layer_stats` field in `.md` frontmatter as
authoritative — some `.md` files are stale (recorded counts stats no longer
reflect the actual file), so h5py is the source of truth here.

Suspension type + disease + organism come from the `.h5ad.mus` sibling with
folder-level `.env` cascade.

Scripts used: `/tmp/.../scan_h5ad_v2.py` (h5py peek), `.../enrich.py` (mus/env
merge), `.../final_table.py` (verdict + priority).

## Summary

- **Datasets scanned:** 149
- **Have raw counts (any form, usable):** 135
  - `layers/counts` (integer): 101
  - Alt-named integer layer (e.g. `UMIs`, `raw`, `raw_sum_counts`, `spliced`): 19
  - `raw/X` (integer): 15
- **Missing raw counts:** 14
  - `NO_COUNTS_ANYWHERE`: 12
  - `counts_layer_present_but_NOT_INTEGER` (mislabeled — see below): 2

### Missing datasets by suspension x n_cells

| suspension | ≥500k | 100k-500k | 10k-100k | 1k-10k | <1k |
|---|---|---|---|---|---|
| cell   | 0 | 4 | 4 | 0 | 0 |
| nucleus| 0 | 2 | 1 | 0 | 0 |
| na (proteomics/aggregate) | 0 | 0 | 0 | 0 | 3 |

### Two flavors of "missing"

1. **Truly missing** — `X` is log-normalised, no `raw/X`, no integer layer.
   Only possible fix: re-ingest from the original download / accession.
2. **`layers/counts` overwritten with log-norm** (2 files) — this is the
   astrocyte-ingest bug the parent thread flagged: the ingest re-normalised
   `X` in-place and wrote the log-norm array *back to `layers/counts`*, so
   the counts layer looks present but holds non-integer floats in [0.6, 5.8].

   - `Aldinger2021/Aldinger2021_cerebellum.h5ad` — **RECOVERABLE**: the
     original UMI counts survive in `raw/X` (69 174 × 26 326, int, max=200).
     A cheap fix: overwrite `layers/counts` from `raw/X`.
   - `Sankowski2019/sankowski2019_microglia.h5ad` — **NOT recoverable**:
     `raw/X` absent; `layers/counts` holds Seurat SCT-transformed values
     (min_nz=1.0001, max=564). Needs re-ingest from GSE.

## Top-14 missing datasets, sorted by n_cells desc

| db_id | Group | n_cells | susp | verdict | has_raw_X | organism | AD? | Priority |
|---|---|---:|---|---|---|---|---|---|
| CSM | Prinz2026 | 270,493 | cell    | NO_COUNTS_ANYWHERE                       | no  | human,mouse | yes | **HIGH** |
| MMA | Prinz2026 | 172,497 | cell    | NO_COUNTS_ANYWHERE                       | no  | mouse       | yes | **HIGH** |
| MMI | Prinz2026 | 151,731 | cell    | NO_COUNTS_ANYWHERE                       | no  | mouse       | yes | **HIGH** |
| HMA | Prinz2026 | 135,067 | nucleus | NO_COUNTS_ANYWHERE                       | no  | human       | yes | **HIGH** (snRNA-seq AD) |
| RMF | Mancuso2024 | 127,755 | cell  | NO_COUNTS_ANYWHERE                       | no  | human       | ?   | MED |
| HMI | Prinz2026 | 118,762 | nucleus | NO_COUNTS_ANYWHERE                       | no  | human       | yes | **HIGH** (snRNA-seq AD) |
| IMM | SunMathys2023 | 83,889 | cell (metadata) | NO_COUNTS_ANYWHERE           | no  | human       | yes | **MED** (see caveat) |
| MIC | SunMathys2023 | 79,188 | cell (metadata) | NO_COUNTS_ANYWHERE           | no  | human       | yes | **MED** (see caveat) |
| SCT | Borrie2025 | 77,684 | cell    | NO_COUNTS_ANYWHERE                       | no  | human       | ?   | LOW |
| CBL | Aldinger2021 | 69,174 | nucleus | counts_layer_present_but_NOT_INTEGER (astrocyte-ingest overwrite) | **yes (raw/X)** | human | no | LOW (**RECOVERABLE from raw/X**) |
| SMI | Sankowski2019 | 12,672 | cell | counts_layer_present_but_NOT_INTEGER (Seurat SCT) | no | human | no | LOW |
| PHM | Lloyd2024 | 21 | na | NO_COUNTS_ANYWHERE | no | human,mouse | -   | N/A (proteomics — not sc-RNAseq) |
| PHu | Lloyd2024 | 11 | na | NO_COUNTS_ANYWHERE | no | human       | -   | N/A (proteomics) |
| PMo | Lloyd2024 | 10 | na | NO_COUNTS_ANYWHERE | no | mouse       | -   | N/A (proteomics) |

Caveat on SunMathys2023 — the `.env` says `suspension_type=cell`, but the
Mathys 2023 ROSMAP papers use snRNA-seq. Likely a mus/env metadata error, not
a data problem for the counts audit. If confirmed as snRNA-seq, priority
becomes **HIGH** (large snRNA-seq + AD, same class as HMA/HMI).

## Recommendations

### Immediate, cheap (script-level fix)

1. **Aldinger2021** — copy `raw/X` into `layers/counts` via
   `adata.layers['counts'] = adata.raw.X` and re-save. No re-ingest needed.
   Also fix whichever ingest step overwrote the counts layer so it doesn't
   happen again.

### Re-ingest from original data

Priority order — driven by (size × AD relevance × single-cell vs single-nucleus):

**HIGH — human microglia/myeloid, AD-relevant, ≥100 k cells/nuclei**
- Prinz2026 HMI (Human Microglia, snRNA-seq, 118 k)
- Prinz2026 HMA (Human Myeloid Atlas, snRNA-seq, 135 k)
- SunMathys2023 MIC + IMM (ROSMAP microglia + immune, 79 k + 84 k) — check
  and fix suspension metadata

**HIGH — mouse Prinz atlases (large, cross-disease including AD models)**
- Prinz2026 MMI (Mouse Microglia, scRNA-seq, 152 k)
- Prinz2026 MMA (Mouse Myeloid Atlas, scRNA-seq, 172 k)
- Prinz2026 CSM (Cross-species Microglia, integrated, 270 k)
  - Caveat: `CSM` is an integrated object across species; re-ingesting counts
    per-donor is only meaningful before integration. Recovering raw counts
    for a Harmony/scVI-integrated object may not be scientifically useful —
    verify with the biologist before ingesting.

**MED — Mancuso2024 `renzo_mg_fixed` (128 k human, disease unclear)**
- Confirm disease annotation via `.mus`; re-ingest from source if AD/xenograft
  relevant, otherwise leave.

**LOW**
- Sankowski2019 (12 k, disease = glioblastoma + epilepsy, not AD)
- Borrie2025 `LOF.sc.teset.sct` (test file, name suggests exploratory)

**Not applicable**
- Lloyd2024 proteomics files (n≤21 samples, protein abundance not per-cell UMI
  counts — the concept of "raw counts" does not apply).

## What NOT to re-ingest

- The 15 datasets that pass integer-check via `raw/X` and the 19 datasets
  using an alt-named layer (`UMIs`, `raw`, `spliced`, …) — these are
  fine, just need the ingest/reader to accept `raw/X` or the alt layer name.
  A better fix is in `bin/ingest/ant-convert` / `lib/mus.py`: teach the
  reader that "raw counts" is the first integer-valued matrix in
  {`layers/counts`, `layers/UMIs`, `layers/raw*`, `raw/X`}.

## Files

- Full per-dataset scan (all 149): `/tmp/.../scratchpad/scan_v3.tsv`
- Scripts: `/tmp/.../scratchpad/scan_h5ad_v2.py`, `enrich.py`, `final_table.py`

## Reproducibility

- Python 3 with h5py 3.16.0 from `envs/anthive-data`
- Sample size for integer check: 20 000 non-zero values per candidate matrix
- Integer threshold: `frac_int > 0.995` AND `max > 1.5`
- No random elements; no seed needed
- Scan takes ~2 min on the local filesystem
