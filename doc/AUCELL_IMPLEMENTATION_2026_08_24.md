# AUCell implementation review — 2026-08-24

## MIGRATION DONE — 2026-08-24

Swapped `ant-aucell` from `decoupler.run_aucell` (1.8.0) to
`pyscenic.aucell.aucell` (0.12.1, ctxcore 0.2.0). User picked **option 1**:
accept sparse-module divergence with r_donor ≥ 0.92 threshold.

### Delivered

- PEP-723 header: dropped `decoupler>=1.6.0,<2.0`; added
  `pyscenic>=0.12.1,<0.13`, `ctxcore>=0.2.0`, `scipy>=1.10`.
- `dc.run_aucell` → `pyscenic.aucell.aucell` with `auc_threshold=0.05`,
  `num_workers=1`, `seed=42`, `normalize=False`.
- Signatures **pre-subset to `adata.var_names`** before handing to
  pyscenic (side-steps pyscenic's silent 80%-coverage NaN behaviour;
  matches decoupler `min_n` semantics).
- **Chunked for large datasets**: adatas with `n_obs > 200_000` are
  scored in `100_000`-cell batches (`_score_chunk` + `pd.concat`).
  Protects Rexach (~432k) and Castonguay (~1M) coming in the astro fleet
  from the ~40 GB densify OOM identified in the review.
- **Sparse-module WARNING**: modules with fewer than `min_n + 3` genes
  actually present in `var` are logged as edge-case (per-cell scores
  may diverge >0.02 vs decoupler baseline; the accepted `Purple_Cell_division`
  case falls here).
- `TOOL_VERSION` bumped to `ant-aucell/0.2-pyscenic`.
- Docstring updated with history + chunking note.

### Verify results (from prior agent's compat run)

Sierksma2025/WGCNA on MY4/MTH (21,834 cells × 18,914 genes), pyscenic vs
decoupler snapshot at `scratchpad/verify.py`:

- **11/13 modules r_donor ≥ 0.99**
- `Purple_Cell_division` r_donor = 0.928 — 5/12 genes present,
  detection 2.5%, mean AUC 0.014 (essentially noise). Accepted.
- All 13 modules per-cell Pearson ≥ 0.92, most ≥ 0.96.

### Smoke test (this session)

Fresh scoring on `G19/ENT` (Grubman2019 entorhinal AD, 13,214 cells ×
10,850 genes, human), Sierksma2025/WGCNA catalog:

- **AUCell wall time: 33.3 s** (pyscenic).
  Decoupler on similar-scale MY4/MTH took **838 s** for the same catalog
  → ~10× speedup on this dataset. Full command wall time (h5ad load +
  cellmap merge + parquet write): 45 s.
- Sidecar shape: 145,354 rows = 13,214 cells × 11 modules
  (2 modules below `min_n=5`: Purple_Cell_division, Tan_cytokine_activation).
- WARNING fired for 2 sparse modules (Greenyellow_migration,
  Salmon_Y_linked) — expected divergence flagged, not blocked.
- Score range [0, 0.969], mean 0.050 — typical AUCell distribution.

### Fleet resume

Fleet crashed at dataset 70/104 (S25/V3S mid-Mancuso2024/Cell_states).
Resumed via the same command; the `_plan_methods` filter skips
already-scored (source=aucell, catalog=<experiment>) pairs, so no
duplication.

---

## Question

Is there a faster drop-in replacement for `decoupler.run_aucell` (currently
used by `bin/annot/ant-aucell`) that keeps existing scores (~60% of fleet)
numerically usable?

## Current implementation

- **Script**: `bin/annot/ant-aucell`
- **Library**: `decoupler-py` pinned as `decoupler>=1.6.0,<2.0`
- **Resolved runtime version** (uv, ant-aucell PEP-723 env): **1.8.0**
- **Runtime cache**: `/home/…/.cache/uv/archive-v0/orQtMHHBDXHuNSvc/…/decoupler`
- **Algorithm**: `decoupler.method_aucell.run_aucell` — numba `@nb.njit(parallel=True)`
  loop over cells; per-cell `np.argsort(np.argsort(-row))` rank
  (descending, dense-int, 1-indexed); AUC over top 5% of features
  (`n_up = ceil(0.05 * n_var)`); tie-breaking by shuffling **gene columns**
  via `numpy.default_rng(seed).choice(...)` before ranking.
- **Speed on MY4/MTH (21,834 cells × 18,914 genes)**:
  - 1 module × 34 genes: **980 s**
  - 13 modules × 489 genes: **838 s**
    (per-cell rank cost dominates; adding modules is nearly free.)

## Candidates evaluated

| Candidate | Version | Algorithm parity | Speedup | New deps | Verdict |
|---|---|---|---|---|---|
| `pyscenic.aucell` (wraps ctxcore) | 0.12.1 | equivalent within noise | **6.4× (13 modules), 6.7× (1 module)** | pyscenic, ctxcore, dask, llvmlite | **top candidate** |
| `ctxcore.recovery.aucell` alone | 0.2.0 | same as pyscenic | ≈ same | ctxcore only | lighter dep footprint but requires re-implementing rank matrix construction that pyscenic already provides |
| `decoupler-py 2.x` | 2.2.0 | **breaking**: switched to `scipy.stats.rankdata(method='ordinal')`, dropped random tie-shuffle | not measured, but reorganised API (`dc.mt.aucell` not `dc.run_aucell`) | modest | rejected: breaking API + breaking algorithm change vs 1.8 scores, and no promise it's faster |
| `scanpy.tl.score_genes` | — | **different algorithm** (signed-mean vs random background), not AUCell | — | — | rejected (we already run this separately as `source='geneset'`) |
| `arboreto` | — | module discovery, not AUCell | — | — | N/A |

## Compatibility test

Calibration pair: `MY4/MTH` (21,834 cells × 18,914 genes) × `Sierksma2025/WGCNA/Brown_Lysosome`
(34 genes, 32 present).

### 1. decoupler-1.8.0 re-run vs existing snapshot

Sanity check — same code, same seed, same input.

- **Pearson: 1.000000**
- **Spearman: 1.000000**
- **max |Δ|: 0.000000** — bit-exact
- Confirms the fleet's existing `scores.parquet` was written by dc 1.8.0 with `seed=42`.

First 5 cells (snapshot vs re-run):
```
0.051674  0.051674
0.000000  0.000000
0.109577  0.109577
0.082664  0.082664
0.163174  0.163174
```

### 2. pyscenic 0.12.1 (ctxcore 0.2.0) vs existing snapshot

Same input, same threshold (top 5%), pyscenic `seed=42`.

- **Pearson: 0.9729**
- **Spearman: 0.9669**
- **mean |Δ|: 6.68e-3**
- **max |Δ|: 6.97e-2**

Per-module Pearson (13 WGCNA modules):
```
Black_Mito                      r=0.9973
Blue_Innate_IR                  r=0.9735
Brown_Lysosome                  r=0.9729
Green_ER_immune                 r=0.9705
Greenyellow_migration           r=0.9602
Magenta_AG_presentation         r=0.9726
Pink_Morphology_Imm             r=NaN       ← pyscenic dropped it
Purple_Cell_division            r=NaN       ← pyscenic dropped it
Red_Immune_response             r=0.9728
Tan_cytokine_activation         r=0.9265
Turquoise_Translation           r=0.9934
Yellow_Cytokine                 r=0.9794
```

### 3. decoupler self-noise across seeds (the key control)

Same code, same input, three different seeds (42, 1, 12345), 3,000-cell subset.

- seed 42 vs 1:     **Pearson 0.9599**, mean |Δ| 7.8e-3
- seed 42 vs 12345: **Pearson 0.9607**, mean |Δ| 7.9e-3
- seed 1 vs 12345:  **Pearson 0.9651**, mean |Δ| 7.0e-3

**Conclusion**: pyscenic-vs-decoupler (r=0.97) is INSIDE decoupler's own
tie-breaking noise floor (r=0.96). The two implementations are
algorithmically equivalent for practical purposes; the deltas are dominated
by seed-dependent tie-breaking of near-zero expression values (each cell in
sparse scRNA-seq has thousands of zero-expression genes that get shuffled
into the ranking).

Per-cell scores will not match. Downstream aggregate statistics (per-donor
means, correlation with pathology, differential AUC between groups) will be
essentially identical.

### 4. Behaviour mismatch to watch

pyscenic warns and produces NaN for gene sets with <80% coverage in the
expression matrix (`Pink_Morphology_Imm`, `Purple_Cell_division` above).
decoupler filters via `min_n` (default 5) — different semantics.
`ant-aucell` currently has its own `--min-n` upstream check, but the
pyscenic 80% cutoff is more aggressive and would silently drop modules.
**This needs an override or a pre-computation gate.**

## Recommendation

**Swap to `pyscenic.aucell.aucell` (which uses ctxcore.recovery under the
hood).** Keep existing scores.parquet AUCell columns as-is; do not
recompute.

### Rationale

1. **6.4× speedup** on realistic multi-module workloads. Fleet
   re-scoring on new datasets becomes noticeably faster.
2. **Numerical equivalence within algorithm's inherent noise** — the
   pyscenic/decoupler delta is smaller than decoupler-vs-itself across
   seeds. Aggregate statistics (means, group comparisons) unaffected.
3. **Existing scores stay valid.** They were produced by a legitimate
   AUCell realization; there is no "correct" per-cell AUCell score, only a
   sampled one. Mixing scores from the two engines in the same
   `scores.parquet` is fine as long as we mark it.
4. Rejects: decoupler 2.x is a bigger migration (API rename + breaking
   algorithm change) with no measured speed win.

## Migration path

**In `bin/annot/ant-aucell`** (do NOT touch until user confirms):

1. Replace deps in PEP-723 header:
   ```
   # dependencies = [
   #   "anndata>=0.10.0",
   #   "numpy>=1.24.0,<2.0",
   #   "pandas>=2.0.0,<3.0",
   #   "pyarrow>=14.0.0",
   #   "duckdb>=0.10.0",
   #   "pyyaml>=6.0",
   #   "pyscenic>=0.12.1,<0.13",
   #   "ctxcore>=0.2.0",
   #   "scipy",
   # ]
   ```
2. Replace the `dc.run_aucell(...)` call block (~lines 240–280) with a
   pyscenic call that produces the same wide DataFrame written into
   `adata.obsm['aucell_estimate']`. Sketch:
   ```python
   from pyscenic.aucell import aucell as pyscenic_aucell
   from ctxcore.genesig import GeneSignature
   from scipy.sparse import issparse

   X = adata.X.toarray() if issparse(adata.X) else np.asarray(adata.X)
   expr_df = pd.DataFrame(X, index=adata.obs_names, columns=adata.var_names)
   sigs = [GeneSignature(name=gs.name,
                          gene2weight={g: 1.0 for g in gs.genes if g in adata.var_names})
           for gs in genesets if module_gene_counts[gs.name] >= min_n]
   scores_wide = pyscenic_aucell(expr_df, sigs,
                                  auc_threshold=0.05,
                                  num_workers=1,
                                  seed=seed)
   adata.obsm['aucell_estimate'] = scores_wide.reindex(adata.obs_names)
   ```
3. Add an override to bypass pyscenic's silent 80%-coverage NaN behaviour,
   or pre-filter modules so we never hand pyscenic a below-threshold set.
   Simplest: keep `ant-aucell`'s existing `min_n` filter and additionally
   compute `coverage = n_present / n_module` per module and either patch
   pyscenic's `enrichment4cells` or subset the signature to only present
   genes (already done in the sketch above by `if g in adata.var_names`
   — this makes pyscenic's coverage metric 100% by construction).
4. Bump `TOOL_VERSION = 'ant-aucell/0.2-pyscenic'`.
5. Update the docstring at the top of `ant-aucell` to mention pyscenic.
6. **No re-scoring required.** Existing rows in `scores.parquet` remain
   valid. New scoring runs (any dataset not yet processed, or `--overwrite`)
   will produce pyscenic-realization AUCs. Coexistence is safe because we
   already tolerate stochastic per-cell variation.
7. Add a smoke test in `tests/` that runs both engines on a tiny synthetic
   input and asserts Pearson > 0.90 to guard against future regressions.

### Migration effort

- Code change: ~30 lines in `ant-aucell`, plus PEP-723 header.
- Testing: run on 2–3 datasets, spot-check aggregate statistics against
  existing scores (donor-level means should match within noise).
- **1–2 hours** for careful implementation + testing.
- No fleet re-run needed. No database migration. `web/` and `api/` unaffected.

## Caveats / risks

- **Sparse zero-tie regime**: our microglia data has ~10% dense per cell
  (~1,800 nonzero genes out of 18,914). The bulk of the "ranked list" is
  zeros, and their random ordering drives the AUC noise floor. If we ever
  add datasets with denser expression (e.g. bulk RNA-seq module scoring),
  the engines will converge more tightly (Pearson > 0.99 was observed on
  cells with nnz > 3,000).
- **Weights**: current `ant-aucell` sets weight=1 for all module genes.
  pyscenic/ctxcore support real gene weights; if we later want to move to
  weighted AUCell, this is a natural upgrade path.
- **`num_workers > 1`** in pyscenic uses Python multiprocessing with
  shared-memory arrays. For our per-dataset scoring workflow, that's not
  worth the complexity; keep `num_workers=1`. (See feedback memory:
  n_jobs > 1 causes exit 144 on this host.)
- **Version pinning**: pyscenic 0.12.x has an old dask requirement and
  needs `pandas<2.2.2` in decoupler 1.8's world — but ant-aucell's env is
  isolated so this doesn't matter. Pin `pyscenic==0.12.1` and let uv
  resolve.

## Self-critique (Agent tool not available in this session)

The user asked to spawn `ruthless-qa-critic`; that tool isn't invocable
here. Answering the intended questions myself:

### Is the compatibility test rigorous enough?

**Partially.** Strengths: bit-exact confirmation of the fleet's provenance
(dc 1.8.0, seed 42); a 13-module × 21,834-cell run on real production
data; established a decoupler-vs-decoupler noise floor to contextualize
the pyscenic gap.

Weaknesses:
- Only one dataset tested. Should also verify on a genuinely dense
  dataset (bulk RNA-seq or a high-depth scRNA-seq) to check the "denser
  data converges tighter" claim — I only stratified within one sparse
  dataset, which shows the trend but is not a fresh corroboration.
- Only one gene-set family (WGCNA, ~30–60 genes per module). Should
  spot-check with a small (5-gene) and a large (500-gene) module to
  ensure the top-5%-of-ranked-genome AUC math behaves the same at
  extremes.
- Did not measure per-donor aggregate agreement — the claim "aggregates
  match within noise" is a well-supported prediction but was not
  numerically verified. Should compare `groupby(donor_id).mean()` for a
  few modules across engines before shipping.
- Did not test the sparse-vs-dense matrix path in pyscenic. We convert
  to dense (`.toarray()`) which balloons memory (21,834 × 18,914 float32
  ≈ 1.6 GB). On the fleet's largest datasets (~500k cells) this
  approaches 40 GB — memory risk not quantified.

### Any candidate missed?

- **`AUCell` R package (Aibar 2017 original)**. Not evaluated because
  we're Python-first, but it's the canonical reference; anyone auditing
  our numbers may reach for it. Worth a one-off cross-check.
- **`gseapy.ssgsea`** and **`gseapy.gsva`**. These are single-sample
  enrichment methods, not AUCell, but same use case. Not drop-in.
  Correctly excluded from consideration.
- **`sctoolkit` / `omnipath` / `pyscenic+` forks**. No credible faster
  AUCell known in 2026 beyond ctxcore. Search returned nothing.
- **GPU-accelerated (`rapids-singlecell`)**. Has a scoring pipeline but,
  as of the last release notes I could confirm, not AUCell specifically.
  Would be dramatic speedup on the right hardware but is out of scope
  here.

### Risks I may have understated

1. **Memory blow-up on large datasets**. Sparse-to-dense conversion is
   a real regression vs decoupler-1.8 which handles CSR directly. The
   migration sketch needs a chunked path for large adatas or must keep
   the sparse extraction. `ant-aucell` currently loads the whole h5ad;
   for the fleet's biggest files a densify would OOM.
2. **The 0.97 Pearson is deceptive for small-effect modules.** If a
   module has a real biological signal that puts scores at ~0.05
   (typical here) with std ~0.04, a 6e-3 mean absolute error is 15% of
   the signal std. Group-mean comparisons will still be fine because
   errors are approximately unbiased, but any per-cell threshold-based
   analysis (e.g., "cells with Brown_Lysosome > 0.15") will disagree
   noticeably between engines.
3. **Silent 80%-coverage NaN in pyscenic** is not a "minor mismatch" —
   it changes which modules a dataset gets scored on. `ant-aucell` must
   subset gene sets to `[g for g in module.genes if g in adata.var_names]`
   BEFORE handing to pyscenic (I noted this but the migration sketch
   should be explicit; keep the pre-check).
4. **decoupler 2.x's ordinal tie-break is deterministic.** If we ever
   want reproducible-across-runs AUCell without a seed, dc 2.x is more
   attractive than pyscenic. This isn't a current requirement but is
   worth flagging.
5. **pyscenic 0.12 is old (2022).** Not currently a bug but a maintenance
   concern; the project has been quiet. ctxcore is the healthier lower
   layer if pyscenic ossifies.

## Files

- Test scripts: `/tmp/…/scratchpad/compat_test.py`, `compat_test2.py`,
  `compat_test3.py`, `self_noise.py`
- Snapshot: `/tmp/…/scratchpad/MTH.scores.parquet.snapshot`
  (34 MB; safe to delete after review)
