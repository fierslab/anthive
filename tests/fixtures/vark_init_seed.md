---
title: SkillTest2026 DLPFC microglia (Huuki-Myers 2024 subset)
description: >-
  Microglia (1,601 nuclei) from the snRNA-seq part of the spatialDLPFC study: human dorsolateral prefrontal cortex from 10 neurotypical adult donors, sampled at anterior, middle and posterior positions (18 samples), 10x Genomics 3-prime gene expression on nuclei, processed by the authors (log-normalised X, raw counts in layers). Deposited as a skill test copy of a public dataset.
replicate_unit: donor # one of: donor | animal | sample | none
replicate_column: BrNum
disease_basis: author_dx # one of: nia_reagan | braak_cerad | adnc | clinical_dx | author_dx
api:
  obs_rename:
    age: ant_age # column name equals the facet name
    diagnosis: ant_disease # column name equals the facet keyword diagnosis
    BrNum: ant_donor # column name equals the facet keyword brnum
    sex: ant_sex # column name equals the facet name
  obs_value_rename:
    sex: {F: f, M: m}
    diagnosis: {Control: control}
  obs_constant:
    ant_cell_type: {value: microglial cell, dtype: categorical}
    ant_tissue: {value: prefrontal cortex, dtype: categorical}
  obs_derive:
    ant_donor_uid: {source: BrNum, template: '{group_id}:{value}'}
  obs_hidden: [Barcode, key, SAMPLE_ID, cell_barcode, high_mito, low_sum, low_detected, discard_auto, collapsedCluster, prelimCluster]
caveats: []
---
