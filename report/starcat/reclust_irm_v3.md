# IRM Recluster Report — renzo_mg_irm_recluster

**Date**: 2026-07-15
**Source**: `renzo_mg_fixed.h5ad` (id=RMF, 127,755 cells × 37,163 genes)
**Subset**: IRM cells (`cell.state_v2.1` contains "IRM") — **6920 cells**

## Software Versions

| Package | Version |
| --- | --- |
| scanpy | 1.12.1 |
| anndata | 0.12.11 |
| numpy | 2.4.3 |
| pandas | 2.3.3 |
| leidenalg | 0.11.0 |
| igraph | 1.0.0 |

## Methods

1. **Subset**: Filtered `obs["cell.state_v2.1"]` for cells containing "IRM". 6920 cells retained.
2. **HVG**: HVG selection succeeded: 2000 HVGs (`sc.pp.highly_variable_genes`, `flavor='seurat'`, `n_top_genes=2000`).
3. **Scale**: `sc.pp.scale(max_value=10)`.
4. **PCA**: `sc.tl.pca(n_comps=50, random_state=42)`, using HVGs.
5. **Neighbors**: `sc.pp.neighbors(n_neighbors=15, use_rep='X_pca', random_state=42)`.
6. **UMAP**: `sc.tl.umap(random_state=42)`, stored as `obsm["X_umap_v3"]`. Original `X_umap` from parent dataset preserved.
7. **Leiden sweep**: resolutions [0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0], `random_state=42`, `flavor='leidenalg'`.
8. **Module scores**: `sc.tl.score_genes` on 8 Mancuso2024/Cell_states gene sets (HM, DAM, IRM, HLA, CRM1, CRM2, tCRM, RM), `random_state=42`.
9. **Label assignment**: per cluster, the state with highest mean module score is assigned. CRM1/CRM2/tCRM collapsed to CRM. Labels written to `obs["cell.state_v3"]`.

## Leiden Resolution Sweep

| Resolution | N clusters | In range (4-6) |
| --- | --- | --- |
| 0.2 | 5 | yes | <-- CHOSEN
| 0.3 | 7 | no |
| 0.4 | 8 | no |
| 0.5 | 9 | no |
| 0.6 | 10 | no |
| 0.8 | 12 | no |
| 1.0 | 13 | no |

**Chosen resolution**: 0.2 → **5 clusters**

## Per-Cluster Results

| Cluster | N cells | Label | Margin | Top-3 scores (mean±std) |
| --- | --- | --- | --- | --- |
| 0 | 4317 | RM LOW-CONF RM-FLAG | 6.0% | RM=0.7638±0.2055, HLA=0.7177±0.1853, DAM=0.3298±0.0934 |
| 1 | 1198 | RM LOW-CONF RM-FLAG | 0.9% | RM=0.6496±0.1698, HLA=0.6439±0.1389, IRM=0.4571±0.1272 |
| 2 | 856 | HLA LOW-CONF | 2.0% | HLA=0.6877±0.1448, RM=0.6743±0.1534, DAM=0.3511±0.0817 |
| 3 | 286 | HLA LOW-CONF | 4.9% | HLA=0.4759±0.1819, RM=0.4525±0.1949, CRM2=0.3733±0.0883 |
| 4 | 263 | HLA LOW-CONF | 7.3% | HLA=0.7602±0.1234, RM=0.7045±0.1373, IRM=0.5407±0.0802 |

## cell.state_v3 Label Counts

| Label | N cells |
| --- | --- |
| RM | 5515 |
| HLA | 1405 |


### RM-Flagged Clusters (out-of-scope)
Clusters where top-scoring state was RM: **0, 1**
These clusters require biological review — RM was not in the expected label set (IRM/HLA/DAM/HM/CRM).

### Low-Confidence Assignments (<10% margin)
Clusters with ambiguous top state: **0, 1, 2, 3, 4**


## Output Files

| File | Description |
| --- | --- |
| `renzo_mg_irm_recluster.h5ad` | Subset h5ad (gzip compressed), obs includes cell.state_v3, leiden_irm_v3, score_* columns |
| `renzo_mg_irm_recluster.h5ad.mus` | Copied verbatim from renzo_mg_fixed (same organism/tissue/technology) |
| `renzo_mg_irm_recluster.md` | Anthive catalog entry (id=IRR, group=Mancuso2024/M24) |
| `tmp/reclust_irm_v3.py` | Full provenance script |

**Next step**: run `ant convert Mancuso2024/renzo_mg_irm_recluster.h5ad` to build the duckdb.
