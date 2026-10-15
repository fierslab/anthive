---
title: SkillTest2026 DLPFC microglia (Huuki-Myers 2024 subset)
description: 'Microglia (1,601 nuclei) from the snRNA-seq part of the spatialDLPFC
  study: human dorsolateral prefrontal cortex from 10 neurotypical adult donors, sampled
  at anterior, middle and posterior positions (18 samples), 10x Genomics 3-prime gene
  expression on nuclei, processed by the authors (log-normalised X, raw counts in
  layers). Deposited as a skill test copy of a public dataset.'
replicate_unit: donor
replicate_column: BrNum
disease_basis: author_dx
api:
  obs_rename:
    age: ant_age
    diagnosis: ant_disease
    BrNum: ant_donor
    sex: ant_sex
  obs_value_rename:
    sex:
      F: f
      M: m
    diagnosis:
      Control: control
  obs_constant:
    ant_cell_type:
      value: microglial cell
      dtype: categorical
    ant_tissue:
      value: prefrontal cortex
      dtype: categorical
  obs_derive:
    ant_donor_uid:
      source: BrNum
      template: '{group_id}:{value}'
  obs_hidden:
  - Barcode
  - key
  - SAMPLE_ID
  - cell_barcode
  - high_mito
  - low_sum
  - low_detected
  - discard_auto
  - collapsedCluster
  - prelimCluster
caveats: []
id: SMI
year: 2024
sha256: 30d239185a1e8e2ea71490852eb384514a214bb6dda3a4de4c0563daebbc4bb1
n_obs: 1601
n_vars: 36601
default_layer: X
layers_include:
- X
- counts
obsm_include:
  X_harmony: X_harmony
  X_pca: X_pca
  X_tsne: X_tsne
  X_umap: X_umap
obsnum_include:
- age
- sum
- detected
- subsets_Mito_sum
- subsets_Mito_detected
- subsets_Mito_percent
- total
- doubletScore
- prelimCluster
- sizeFactor
obscat_include:
- Sample
- SAMPLE_ID
- pos
- BrNum
- round
- Position
- sex
- diagnosis
- high_mito
- low_sum
- low_detected
- discard_auto
- collapsedCluster
- kmeans
- cellType_broad_k
- cellType_k
- cellType_broad_hc
- cellType_hc
- cellType_layer
- layer_annotation
layer_stats:
  X:
    dtype: float32
    min_value: 0.0
    min_nonzero: 0.9084132313728333
    max_value: 11.134626388549805
    median: 2.6262478828430176
    q90: 3.895491600036621
    num_zeros: 55663848
    pct_zeros: 94.99241794129482
  counts:
    dtype: float32
    min_value: 0.0
    min_nonzero: 1.0
    max_value: 696.0
    median: 1.0
    q90: 3.0
    num_zeros: 55663848
    pct_zeros: 94.99241794129482
version: 1.0.1
changelog:
- version: 1.0.0
  date: '2026-10-15'
  sha256: 30d239185a1e8e2ea71490852eb384514a214bb6dda3a4de4c0563daebbc4bb1
  changes:
  - First version, ingested from an S3 deposit.
  - Deposited through the API by rookie (deposit dep_14e940e280a70e21a85f).
- version: 1.0.1
  date: '2026-10-15'
  changes:
  - 'DE through the deposit dep_3e0e0848ff107e714bb5 by rookie: added microglia_sex_F_vs_M_pseudobulk_deseq2.'
date: '2026-10-15'
ingest:
  route: s3-watch
  source_key: __incoming_test/SkillTest2026/skilltest_microglia.h5ad
  etag: f68821a785dc64c2aeed4fc0b64bf764
  date: '2026-10-15'
  depositor: rookie <rookie@skilltest.local>
  deposit:
    id: dep_14e940e280a70e21a85f
    user: rookie
---

# SkillTest2026 DLPFC microglia (Huuki-Myers 2024 subset)

Microglia (1,601 nuclei) from the snRNA-seq part of the spatialDLPFC study: human dorsolateral prefrontal cortex from 10 neurotypical adult donors, sampled at anterior, middle and posterior positions (18 samples), 10x Genomics 3-prime gene expression on nuclei, processed by the authors (log-normalised X, raw counts in layers). Deposited as a skill test copy of a public dataset.
