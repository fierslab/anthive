# SP4 (SerranoPozo2024) Ingest Report

**Date:** 2026-08-12  
**Group:** SerranoPozo2024 (group_id: SP4)  
**Source DOI:** 10.1038/s41593-024-01791-4  
**Technology:** 10x Chromium 3' v3 snRNA-seq  
**Organism:** Human  
**Disease:** Alzheimer disease  

---

## Source data

| Cell type | Raw source |
|-----------|-----------|
| Astrocytes (5 regions) | `/data/1/project/tmp/Astrocytes/{EC,ITG(BA20),PFC(BA46),V1,V2}_upload/` |
| Microglia (6 regions incl. crossregion) | `/data/1/project/tmp/Microglia/{EC,ITG,PFC,V1,V2,crossregion}_upload/` |

Each deposit: `matrix.mtx` (genes × cells), `features.tsv`, `cell_annotation.txt`.  
Astrocytes additionally: `metadata.xlsx` with per-cell clinical annotation (110+ columns).

---

## Build parameters

| Parameter | Value |
|-----------|-------|
| Normalisation | `sc.pp.normalize_total(target_sum=1e4)` then `sc.pp.log1p()` |
| Raw counts preserved in | `layers['counts']` |
| Log-normalised counts in | `X` (AnnData default layer) |
| HVG selection | top 3000, Seurat flavour |
| PCA | 50 components, `random_state=42` |
| Neighbors | k=15, `random_state=42` |
| UMAP | `random_state=42` |
| Script | `/tmp/claude-664425682/-data-1-users-mark-anthive4/dfa20685-6d19-41d5-a373-d037f7a0ba1c/scratchpad/sp4_ingest.py` |

---

## Per-file results

| ID | File | Cells | Genes | h5ad | DuckDB | Non-zero values | Sparsity | Convert time |
|----|------|-------|-------|------|--------|-----------------|----------|--------------|
| AEC | SerranoPozo2024_Astro_EC | 101,489 | 36,601 | 1.3 GB | 128 MB | — | — | 2m 36s |
| AIT | SerranoPozo2024_Astro_ITG | 122,591 | 33,423 | 1.5 GB | 145 MB | — | — | 3m 9s |
| APF | SerranoPozo2024_Astro_PFC | 151,688 | 33,522 | 2.1 GB | 187 MB | 464,888,011 | 90.9% | 5m 20s |
| AV1 | SerranoPozo2024_Astro_V1 | 133,875 | 33,410 | 1.5 GB | 169 MB | 311,532,179 | 93.0% | 2m 55s |
| AV2 | SerranoPozo2024_Astro_V2 | 119,300 | 33,364 | 1.4 GB | 145 MB | 288,725,740 | 92.7% | 4m 19s |
| MEC | SerranoPozo2024_Micro_EC | 68,662 | 36,601 | 643 MB | 53 MB | — | — | 1m 28s |
| MIT | SerranoPozo2024_Micro_ITG | 65,800 | 33,423 | 651 MB | 51 MB | — | — | ~2m |
| MPF | SerranoPozo2024_Micro_PFC | 75,740 | 33,522 | 787 MB | 54 MB | 170,389,399 | 93.3% | 3m 19s |
| MV1 | SerranoPozo2024_Micro_V1 | 60,946 | 33,395 | 620 MB | 50 MB | 133,978,561 | 93.4% | 1m 28s |
| MV2 | SerranoPozo2024_Micro_V2 | 66,309 | 33,364 | 657 MB | 51 MB | 142,166,025 | 93.6% | 2m 4s |
| MCR | SerranoPozo2024_Micro_crossregion | 48,217 | 36,601 | 505 MB | 38 MB | — | — | 1m 26s |

**Total cells ingested:** 1,014,617  
**Total astrocyte cells:** 629,343 (5 regions)  
**Total microglia cells:** 385,274 (6 regions incl. crossregion)

---

## Metadata join (astrocytes only)

Astrocytes had per-cell clinical metadata in `metadata.xlsx`. Sheet-to-region mapping:

| Region | Sheet |
|--------|-------|
| EC | EC |
| ITG | BA20 |
| PFC | BA46 |
| V1 | V1 |
| V2 | V2 |

Join key: `cell_annotation` column in xlsx = cell barcode in `cell_annotation.txt`.  
**Barcode dropout: 0 for all 5 regions** (100% intersection verified before join).  
Columns joined per astrocyte file: 110+ clinical/annotation columns.  
ant-check excluded high-cardinality columns from `obscat_include` automatically.

Microglia: no xlsx available. obs contains only `region` and `sample_id`.  
MCR (crossregion): 145 unique `sample_id` values — correctly excluded from `obscat_include` by ant-check (high-cardinality).

---

## Obs metadata per cell type

| Cell type | obs columns | Source |
|-----------|-------------|--------|
| Astrocytes | 110+ (62 numeric, 41–42 categorical per ant-convert) | metadata.xlsx join |
| Microglia (per-region) | 2 categorical: `region`, `sample_id` | cell_annotation.txt |
| Microglia (crossregion) | 1 categorical: `region='mixed'` | assigned in script |

---

## Sidecars written

Each file has:
- `.md` — anthive catalog fields (id, title, version, year, date); SHA256 and layer_stats filled in by ant-check
- `.h5ad.mus` — experimental annotation (cell_type, region, doi, technology, source, contributor)

Microglia `.mus` overrides `cell_type=microglial cell` (folder `.env` defaults to `astrocyte`).  
Group `.env` and `index.md` were NOT modified (pre-existing).

---

## Catalog status

All 11 datasets registered via `POST http://127.0.0.1:8080/admin/rescan`.

```
SP4/AEC  SP4/AIT  SP4/APF  SP4/AV1  SP4/AV2
SP4/MCR  SP4/MEC  SP4/MIT  SP4/MPF  SP4/MV1  SP4/MV2
```

---

## S3 sync

Initiated: `./bin/ops/ant-sync push 'SerranoPozo2024/*'`  
Running asynchronously at time of report write; covers all `.h5ad`, `.duckdb`, and `.parquet` sidecars.

---

## QC notes

- APF (largest astrocyte file, 2.1 GB h5ad) required background processing due to wall-clock time; file integrity confirmed by ant-check (SHA256 verified, all structures present).
- No barcode dropout on any file. All joins were 100% clean.
- MCR `sample_id` correctly handled as high-cardinality (145 unique values); ant-check excluded it from obscat_include automatically.
- Sparse-CSR format maintained throughout; matrices never densified.
- Peak memory during concurrent processing: ~60 GB (well within 251 GB available).
- Microglia datasets were NOT scored with microglia-specific panels (optional task deferred).
