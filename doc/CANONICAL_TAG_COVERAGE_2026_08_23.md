# Canonical Tag Coverage Audit — 2026-08-23

Audit of canonical tag completeness across all served anthive4 datasets.
Generated: 2026-08-23. Server state: 153 databases served.

**Coverage status key**: `P` = present (ant_ column live in API), `-` = absent.

## Per-tag summary

| Tag | Present | Missing | Coverage |
|-----|---------|---------|----------|
| `braak` | 54 | 99 | 35.3% |
| `braak_group` | 52 | 101 | 34.0% |
| `sex` | 100 | 53 | 65.4% |
| `host_sex` | 4 | 149 | 2.6% |
| `age` | 43 | 110 | 28.1% |
| `cluster` | 75 | 78 | 49.0% |
| `disease` | 27 | 126 | 17.6% |
| `condition` | 29 | 124 | 19.0% |
| `pmi` | 22 | 131 | 14.4% |
| `donor` | 104 | 49 | 68.0% |
| `apoe` | 37 | 116 | 24.2% |
| `region` | 47 | 106 | 30.7% |
| `genotype` | 11 | 142 | 7.2% |

## Coverage matrix

| Dataset | Title | brk | brg | sex | hsx | age | clu | dis | con | pmi | don | apo | reg | gen |
|---------|-------|---|---|---|---|---|---|---|---|---|---|---|---|---|
| AZ5/NST | NovaST_lecMicro_combined_CRX_40um_plaqueDista | - | - | - | - | - | - | - | - | - | P | - | P | - |
| AZ5/SLM | scrna_lecMicro_cleaned_integrated_sctransform | - | - | - | - | - | P | - | P | - | P | - | - | - |
| B25/CNT | LOF SCT | - | - | - | - | - | P | - | - | - | - | - | - | P |
| B25/SCT | LOF.sc.teset.sct | - | - | - | - | - | P | - | - | - | P | - | - | P |
| B26/SOF | seurat_obj_final | - | - | P | - | - | - | - | - | - | P | - | - | - |
| B26/SOT | seurat_obj_trisomy_initial | - | - | P | - | - | - | - | - | - | P | - | - | - |
| B26/TSO | trisomy_seurat_obj_w_umap | - | - | P | - | - | - | - | - | - | P | - | - | - |
| D25/DAV | davis2025.20250619 | - | - | P | P | - | - | - | - | - | P | - | - | - |
| D25/HAR | separately_cleaned_harmonised_190626 | - | - | - | P | - | - | - | P | - | P | - | - | - |
| D25/SEX | 20240812_sexmicroglia | - | - | P | P | - | - | - | - | - | P | - | - | - |
| D26/ALL | All | P | P | P | - | P | - | P | - | P | P | - | P | - |
| DB6/CPF | DeBaene2026 CEN prefreeze v1 | - | - | P | P | P | - | - | - | - | P | P | - | P |
| F26/ACC | PFC_Astrocytes_combat_260804 | P | P | P | - | P | - | - | - | - | P | P | - | - |
| F26/ATM | PFC_Astrocytes_meta_260806 | - | - | - | - | - | - | - | - | - | - | - | - | - |
| F26/LCC | PFC_L23_combat_260804 | P | P | P | - | P | - | - | - | - | P | P | - | - |
| F26/LTM | PFC_L23_meta_260806 | - | - | - | - | - | - | - | - | - | - | - | - | - |
| F26/MCC | PFC_Microglia_combat_260804 | P | P | P | - | P | - | - | - | - | P | P | - | - |
| F26/MTM | PFC_Microglia_meta_260806 | - | - | - | - | - | - | - | - | - | - | - | - | - |
| G21/ADB | Gerrits 2021 AD Brain (amyloid/tau staged) | - | - | - | - | - | - | P | - | - | P | - | P | - |
| G23/MIC | Gazestani 2023 NPH microglia | - | - | - | - | - | P | - | P | - | P | - | - | - |
| G24/MAs | Seattle_MTG_Astro | P | P | P | - | - | P | - | - | - | - | - | P | - |
| G24/MEn | Seattle_MTG_Endo | P | P | P | - | - | P | - | - | - | - | - | P | - |
| G24/MEx | Seattle_MTG_ExN | - | - | - | - | - | - | - | - | - | - | - | - | - |
| G24/MIn | Seattle_MTG_InN | P | P | P | - | - | P | - | - | - | - | - | P | - |
| G24/MMG | Seattle_MTG_MG | P | P | P | - | - | P | - | - | - | - | - | P | - |
| G24/MOP | Seattle_MTG_OPC | P | P | P | - | - | P | - | - | - | - | - | P | - |
| G24/MOl | Seattle_MTG_Oligo | P | P | P | - | - | P | - | - | - | - | - | P | - |
| G24/MVL | Seattle_MTG_VLMC | P | P | P | - | - | P | - | - | - | - | - | P | - |
| G24/PAs | Seattle_PFC_Astro | P | P | P | - | - | P | - | - | - | - | P | P | - |
| G24/PEn | Seattle_PFC_Endo | P | P | P | - | - | P | - | - | - | - | P | P | - |
| G24/PEx | Seattle_PFC_ExN | - | - | - | - | - | - | - | - | - | - | - | - | - |
| G24/PIn | Seattle_PFC_InN | P | P | P | - | - | P | - | - | - | - | P | P | - |
| G24/PMG | Seattle_PFC_MG | P | P | P | - | - | P | - | - | - | - | P | P | - |
| G24/POP | Seattle_PFC_OPC | P | P | P | - | - | P | - | - | - | - | P | P | - |
| G24/POl | Seattle_PFC_Oligo | P | P | P | - | - | P | - | - | - | - | P | P | - |
| G24/PVL | Seattle_PFC_VLMC | P | P | P | - | - | P | - | - | - | - | P | P | - |
| GF4/MIC | Green/Fujita 2024 ROSMAP DLPFC microglia | - | - | - | - | - | P | - | - | - | P | - | - | - |
| H21/MNI | Hasel 2021 Mouse Neuroinflammation (LPS) | - | - | P | - | - | - | - | P | - | - | - | - | - |
| H25/AQC | adata_qc | - | - | P | - | - | - | - | P | - | P | - | - | - |
| H25/EBI | exp5096BulkRNAseq2025_dds_inter | - | - | P | - | P | - | - | - | - | P | - | - | - |
| H25/EBN | exp5096BulkRNAseq2025_dds_nlgf | - | - | P | - | P | - | - | - | - | P | - | - | - |
| H25/EBR | exp5096BulkRNAseq2025_dds_hu | - | - | P | - | P | - | - | - | - | P | - | - | - |
| H25/FST | featurecounts_star | - | - | P | - | P | - | - | - | - | - | - | - | - |
| H25/HNO | Hou2025_nov16 | - | - | P | - | P | - | - | - | - | - | - | - | - |
| H25/RHG | rnaseq_human_genes | - | - | P | - | P | - | - | - | - | P | - | - | - |
| H26/EB2 | exp5244BulkRNAseq2026_dds2 | - | - | P | - | P | - | - | P | - | P | - | - | - |
| H26/EBA | exp5244BulkRNAseq2026_dds_A431E | - | - | P | - | P | - | - | - | - | P | - | - | - |
| H26/EBP | exp5244BulkRNAseq2026_dds_PSEN2 | - | - | P | - | P | - | - | - | - | P | - | - | - |
| H26/EPE | exp5244BulkRNAseq2026_dds_PSEN2_ESC | - | - | P | - | P | - | - | - | - | P | - | - | - |
| HZ3/GSM | GS_micro_invivo_3_7_Ctrl_clean_integrated | - | - | - | - | - | P | - | - | - | - | - | - | - |
| HZ3/LPS | LPS_sema_micro_invitro | - | - | - | - | - | P | - | P | - | P | - | - | - |
| HZ3/NLP | noLPS_sema_micro_invitro | - | - | - | - | - | P | - | P | - | P | - | - | - |
| L24/PHM | Lloyd.proteomics.human_mouse | - | - | - | - | - | - | - | - | - | - | - | - | - |
| L24/PHu | Lloyd.proteomics.human | - | - | - | - | - | - | - | - | - | - | - | - | - |
| L24/PMo | Lloyd.proteomics.mouse | - | - | - | - | - | - | - | - | - | - | - | - | - |
| L26/FMG | Lee2026 FreshMG (fresh PFC myeloid scRNA-seq) | - | - | - | - | - | P | - | - | - | P | - | - | - |
| L26/PSY | Lee2026 PsychAD (frozen PFC myeloid snRNA-seq | - | - | - | - | - | P | - | - | - | P | - | - | - |
| LC5/CEN | CEN_CNA_345_fixed_20241231 | P | - | P | - | - | P | - | - | - | P | P | - | - |
| LC5/NBB | NBB_CNA_345_fixed_20241231 | P | P | P | - | - | P | - | - | - | P | P | - | - |
| LC5/NCO | 20240904_CENT_OCT_sn_cleaned_batch_RINgenderG | - | - | P | - | - | P | - | - | - | P | - | - | - |
| LC5/SAS | 101010sn.1.astro | P | P | P | - | P | P | - | - | - | P | P | - | - |
| LC5/SEX | 101010sn.1.exc | P | P | P | - | P | P | - | - | - | P | P | - | - |
| LC5/SIN | 101010sn.1.inh | P | P | P | - | P | P | - | - | - | P | P | - | - |
| LC5/SMI | 101010sn.1.micro | P | P | P | - | P | P | - | - | - | P | P | - | - |
| LC5/SNX | 101010sn.1 | P | P | P | - | P | P | - | - | - | P | P | - | - |
| LC5/SOL | 101010sn.1.oligo | P | P | P | - | P | P | - | - | - | P | P | - | - |
| LC5/SOP | 101010sn.1.opc | P | P | P | - | P | P | - | - | - | P | P | - | - |
| LC5/VCF | visium_cent_full_allgenes_20241217 | P | - | P | - | - | P | - | - | - | P | P | - | - |
| LC5/VNF | visium_nbb_full_20240507 | P | P | P | - | - | P | - | - | - | P | P | - | - |
| LC5/XNB | xen_nbb11 | - | - | - | - | - | - | - | P | - | P | - | - | - |
| M19/CH9 | Chimera.H9 | - | - | - | - | - | - | - | - | - | - | - | - | - |
| M19/CMS | Chimera.Mouse | - | - | - | - | - | - | - | - | - | - | - | - | - |
| M19/IVT | InVitro | - | - | - | - | - | - | - | - | - | - | - | - | - |
| M19/PRH | Primary.Human | - | - | - | - | - | - | - | - | - | - | - | - | - |
| M24/M23 | Mancuso_2023 | - | - | P | - | P | - | - | - | - | P | - | - | P |
| M24/RMF | renzo_mg_fixed | - | - | P | - | P | - | - | - | - | P | - | - | P |
| MA2/HPM | Marsh 2022 Human Post-mortem Brain | - | - | - | - | - | - | P | - | - | P | - | - | - |
| MI2/MI2 | Miedema 2022 — Progressive MS Brain Macrophag | - | - | - | - | - | - | - | - | - | P | - | - | - |
| MY4/MAG | Mathys 2024 ROSMAP microglia — Angular gyrus  | P | P | P | - | - | - | P | - | P | P | - | P | - |
| MY4/MEC | Mathys 2024 ROSMAP microglia — Entorhinal cor | P | P | P | - | - | - | P | - | P | P | - | P | - |
| MY4/MHC | Mathys 2024 ROSMAP microglia — Hippocampus su | P | P | P | - | - | - | P | - | P | P | - | P | - |
| MY4/MMT | Mathys 2024 ROSMAP microglia — Middle tempora | P | P | P | - | - | - | P | - | P | P | - | P | - |
| MY4/MPF | Mathys 2024 ROSMAP microglia — PFC subset | P | P | P | - | - | - | P | - | P | P | - | P | - |
| MY4/MRM | Mathys 2024 Multiregion Microglia (ROSMAP) | P | P | P | - | - | - | P | - | P | P | - | P | - |
| MY4/MTH | Mathys 2024 ROSMAP microglia — Thalamus subse | P | P | P | - | - | - | P | - | P | P | - | P | - |
| MZ4/CMM | CosMx_mouse_combined | - | - | - | - | - | P | - | - | - | P | - | P | - |
| MZ4/SSM | StereoSeq_mouse_combined | - | - | - | - | P | - | - | - | - | P | - | P | - |
| O20/OMI | Olah 2020 Live Human Microglia | - | - | P | - | - | P | P | - | - | P | - | - | - |
| O22/INH | Inhibitory | P | P | P | - | P | - | P | - | P | P | - | - | - |
| P24/AST | Astrocytes | P | P | P | - | - | - | P | - | P | P | P | P | - |
| P24/MIC | Microglia | P | P | P | - | - | - | P | - | P | P | P | P | - |
| P26/CSM | Cross-species Microglia (integrated) | - | - | P | - | P | P | P | P | - | P | - | P | - |
| P26/HMA | Human Myeloid Atlas | - | - | P | - | P | P | P | P | - | P | - | P | - |
| P26/HMI | Human Microglia | - | - | P | - | P | P | P | P | - | P | - | P | - |
| P26/MMA | Mouse Myeloid Atlas | - | - | - | - | - | P | - | P | - | - | - | P | - |
| P26/MMI | Mouse Microglia | - | - | - | - | - | P | - | P | - | - | - | P | - |
| PM4/AST | Astrocytes | - | - | P | - | - | - | - | - | - | - | - | - | - |
| PM4/MIC | Microglia | - | - | P | - | - | - | - | - | - | - | - | - | - |
| S19/SMI | Sankowski 2019 Human Microglia | - | - | - | - | - | - | P | - | - | P | - | - | - |
| S20/SIE | Sierksma2020 | - | - | - | - | P | - | - | - | - | P | - | - | P |
| S23/WHI | White.Matter.Microglia | - | - | P | - | - | - | P | - | - | P | - | - | - |
| S25/PhM | Pharmacogenomics | - | - | - | - | - | - | - | - | - | - | - | - | - |
| S25/SGM | Single.Grafts | - | - | - | - | - | - | - | - | - | - | - | - | - |
| S25/V3M | Village.3M | - | - | P | - | - | - | - | - | - | - | - | - | - |
| S25/V6A | 6M Village | - | - | - | - | - | - | - | - | - | - | - | - | - |
| S25/V6W | Village.6M.WGCNA_modules | - | - | - | - | - | - | - | - | - | - | - | - | - |
| S26/AQB | adata_qc_removed_clusters_blob | - | - | P | - | - | P | - | P | - | P | - | - | - |
| S26/AQM | adata_qc_removed_clusters_mapping2024 | - | - | P | - | - | P | - | P | - | P | - | - | - |
| S26/AQN | adata_qc_removed_clusters_nocycling | - | - | P | - | - | P | - | P | - | P | - | - | - |
| S26/AQP | adata_qc_removed_clusters_blob_PDT | - | - | P | - | - | P | - | P | - | P | - | - | - |
| S26/AQR | adata_qc_removed_clusters | - | - | P | - | - | P | - | P | - | P | - | - | - |
| S26/BLM | adata_qc_removed_clusters_blob_mapping2024 | - | - | P | - | - | P | - | P | - | P | - | - | - |
| S26/BPM | adata_qc_removed_clusters_blob_PDT_mapping202 | - | - | P | - | - | P | - | P | - | P | - | - | - |
| S26/NCM | adata_qc_removed_clusters_nocycling_mapping20 | - | - | P | - | - | P | - | P | - | P | - | - | - |
| SC9/MSB | Schirmer 2019 MS Brain (all cell types) | - | - | P | - | P | - | P | - | P | P | - | P | - |
| SC9/MSM | Schirmer 2019 MS Microglia | - | - | P | - | P | - | P | - | P | P | - | P | - |
| SF9/CMR | carlo_mg_raw | - | - | P | - | P | - | - | - | - | - | - | - | P |
| SF9/SAN | salafrigerio_analyzed | - | - | P | - | P | P | - | - | - | - | - | - | P |
| SF9/SRW | salafrigerio_analyzed | - | - | P | - | P | - | - | - | - | - | - | - | P |
| SH9/SHA | Shah2019_human_astrocytes_a12 | - | - | - | - | - | P | - | - | - | P | - | - | P |
| SH9/SMB | Shah2019_mouse_brain_a36 | - | - | - | - | - | P | - | - | - | P | - | - | P |
| SM2/MDB | Smajic 2022 Midbrain (all cell types) | - | - | - | - | - | P | P | - | - | P | - | - | - |
| SM2/SMC | Smajic 2022 Microglia (PD midbrain) | - | - | - | - | - | P | P | - | - | P | - | - | - |
| SM3/IMM | ROSMAP PFC Immune Cells (Mathys 2023) | P | P | P | - | P | - | P | - | P | P | P | - | - |
| SM3/MIC | ROSMAP PFC Microglia (Mathys 2023) | P | P | P | - | P | - | P | - | P | P | P | - | - |
| SP4/AEC | SerranoPozo2024_Astro_EC | P | P | P | - | P | - | - | - | P | P | P | P | - |
| SP4/AIT | SerranoPozo2024_Astro_ITG | P | P | P | - | P | - | - | - | P | P | P | P | - |
| SP4/APF | SerranoPozo2024_Astro_PFC | P | P | P | - | P | - | - | - | P | P | P | P | - |
| SP4/AV1 | SerranoPozo2024_Astro_V1 | P | P | P | - | P | - | - | - | P | P | P | P | - |
| SP4/AV2 | SerranoPozo2024_Astro_V2 | P | P | P | - | P | - | - | - | P | P | P | P | - |
| Si5/MSS | module_scored_sorl1 | - | - | P | - | - | - | - | P | - | P | - | - | - |
| Si5/SSF | seurat_obj_sorl1_final | - | - | P | - | - | - | - | P | - | P | - | - | - |
| Si5/SSI | seurat_obj_sorl1_initial | - | - | P | - | - | - | - | P | - | P | - | - | - |
| T20/FUL | hnp_microglia_FULL | - | - | - | - | - | P | - | - | - | P | - | - | - |
| T20/ISC | hnp_microglia_intersection | - | - | - | - | - | P | - | - | - | P | - | - | - |
| T20/ITG | hnp_microglia_integrated | - | - | - | - | - | P | - | - | - | P | - | - | - |
| TW5/CMA | Cerebellar Microglia Aging (MERFISH) | - | - | P | - | P | P | - | - | - | P | - | P | - |
| V25/SAA | slidetag.analysis2.all | - | - | - | - | - | P | - | - | - | - | - | - | - |
| V25/SAL | slidetag.all | - | - | - | - | - | P | - | - | - | - | - | - | - |
| V25/SHL | slidetag.has_loc | - | - | - | - | - | P | - | - | - | - | - | - | - |
| V25/SSC | slide_st.counts | - | - | - | - | - | P | - | - | - | - | - | - | - |
| WA4/MCR | Wachter2024_Micro_crossregion | P | P | P | - | - | P | - | - | - | P | P | P | - |
| WA4/MEC | Wachter2024_Micro_EC | P | P | P | - | - | P | - | - | - | P | P | P | - |
| WA4/MIT | Wachter2024_Micro_ITG | P | P | P | - | - | P | - | - | - | P | P | P | - |
| WA4/MPF | Wachter2024_Micro_PFC | P | P | P | - | - | P | - | - | - | P | P | P | - |
| WA4/MV1 | Wachter2024_Micro_V1 | P | P | P | - | - | P | - | - | - | P | P | P | - |
| WA4/MV2 | Wachter2024_Micro_V2 | P | P | P | - | - | P | - | - | - | P | P | P | - |
| Z20/ALL | Zhou 2020 — DLPFC all nuclei (TREM2 variant s | P | P | P | - | - | P | P | P | P | P | - | - | - |
| Z20/MIC | Zhou 2020 — DLPFC microglia (TREM2 variant sn | P | P | P | - | - | P | P | P | P | P | - | - | - |
| Z25/Z2R | L23_19GPs_2025JUL29_RawCounts | - | - | - | - | - | - | - | - | - | - | - | - | - |
| Z25/Z3N | neurodyssey_metacells_annotated_20260211 | - | - | - | - | - | P | - | - | - | - | - | - | - |
| ZE5/ZAC | Zelic2025_Human_SpinalCord_AllCells | - | - | - | - | - | P | - | P | - | P | - | - | - |
| ZE5/ZMG | Zelic2025_Human_SpinalCord_Microglia | - | - | - | - | - | P | - | P | - | P | - | - | - |

## AMBIGUOUS cases — require user decision

The following cases were identified but NOT implemented automatically because
the mapping is non-trivial or the correct interpretation is unclear:

### Gabitto2024 — Age at Death and PMI as categorical strings
Files: G24/M* and G24/P*. `Age at Death` is stored as string age-bins
(e.g., "80-89 years"), not numerical. Renaming to `ant_age` (numerical tag)
would require parsing midpoint values. PMI similarly stored as "13-24 hours" bins.
**Decision needed**: Accept string age bins as `ant_age` (categorical override),
or compute numeric midpoint, or leave absent?

### Gabitto2024 — Disease field ambiguity
SEAAD data has `Consensus Clinical Dx` (clinical diagnosis: AD/LBD/Control) and
`Primary AD neuropath Dx` (neuropath: AD/Not AD). Neither maps cleanly to the
`disease` / `condition` split without domain knowledge of which is intended.
**Decision needed**: Which column should be `ant_disease`?

### Francken2026 ATM/LTM/MTM — Metacell braak/disease averages
F26/ATM, F26/LTM, F26/MTM are SEACell metacell aggregates. Their
`avg_Braak_SEAAD` is a per-metacell *mean* Braak score across constituent cells,
not a per-cell stage. Mapping `avg_Braak_SEAAD → ant_braak` would expose
floating-point averages where integer stages are expected.
**Decision needed**: Map anyway (users understand these are metacells)?
Or leave ant_braak absent for metacell datasets?

### Zielonka2025 Z3N — Metacell braak average
Z25/Z3N (neurodyssey_metacells_annotated_20260211): similar situation.
`mean_Mathys_Braak_numeric` is a per-metacell average. Same decision as above.

### Francken2026 / Gabitto2024 — Disease annotation via neuropathology
F26/MCC, F26/ACC, F26/LCC have neuropathology scores but no simple
`disease = AD/Control` column. `Cognitive_Status` or `neuropath_dx` could
fill `ant_disease` but requires confirming which label scheme is preferred.

### Thrupp2020 — No disease column found
T20/FUL, T20/ISC, T20/ITG: epilepsy-adjacent research with control donors.
No `disease` column in obs. These appear to be healthy controls throughout.
**Decision needed**: Add derived `ant_disease = control` constant? Or leave absent?

### Lee2026 — Missing clinical metadata in h5ad
L26/FMG and L26/PSY: Braak, CERAD, age, APOE, sex are in the paper's
supplementary tables but not embedded in the h5ad obs. Would require a table-join.
**Decision needed**: Is it worth backfilling from the paper supplementary tables?

## Implemented fixes summary

Collections modified (obs_rename/obs_derive additions to .md files):

- AlbertiniZielonka2025
- Borrie2025
- Davis2025
- DeBaene2026
- Dharshini2026
- Francken2026
- Gabitto2024
- Gazestani2023
- Gerrits2021
- GreenFujita2024
- Hou2025
- Hou2026
- HouZielonka2023
- Lee2026
- LuChen2025
- MallachZielonka2024
- Mancuso2024
- Marsh2022
- Olah2020
- OteroGarcia2022
- Pan2024
- Prinz2026
- SalaFrigerio2019
- Sankowski2019
- Schirmer2019
- SerranoPozo2024
- Shah2019
- Sierksma2020
- Smajic2022
- Sun2026
- SunMathys2023
- Thrupp2020
- TsaiWyssCoray2025
- Voet2025
- Wachter2024
- Zelic2025
- Zhou2020
- Zielonka2025

Total: 38 collections modified, all changes verified via API refresh-views.