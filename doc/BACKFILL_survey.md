# BACKFILL_survey, clinical metadata gaps for `ant_donor` datasets

Reconnaissance for task #329. Which of the 132 datasets that carry `ant_donor`
but lack at least one clinical facet (age, PMI, Braak, disease, sex) can be
backfilled, from where, and at what cost. Read-only survey, no data touched.

## Scope

- 134 datasets serve `ant_donor`.
- 132 have at least one missing clinical facet (only 2 are complete).
- Missing-facet counts. sex 26, disease 75, braak 88, age 93, pmi 110.

The task brief cited ~113 gap datasets, so the true gap (132) is a bit larger.
The extra ~19 came out of counting facet-only omissions. Wachter is present in
ant_donor and ant_sex but missing ant_disease.

## Tier summary

| Tier | Datasets | Cells | Est. effort | Interpretation |
|---|---:|---:|---:|---|
| T1 | 18 | 2,144,809 | 9.2h | supp on disk OR CellxGene obs, likely just facet-alias fix (#326 territory) |
| T2 | 54 | 4,052,477 | 83.5h | DOI known, ESM publicly downloadable, no DUA |
| T3 | 12 | 550,504 | 9.0h | DUA-gated (Synapse ROSMAP / ADKP) |
| T4 | 26 | 2,232,861 | 51.0h | unpublished, integration, or author-contact required |
| T5 | 22 | 2,925,683 | 0.0h | not applicable (xenograft, mouse-only, single-donor, developmental, methods paper) |
| All | 132 | 11,906,334 | 152.8h |  |

Tier 5 is the biggest single class of "won't-fix". 22 datasets, 2.9M cells that
genuinely lack the semantic anchors for these facets. Xenografts have no human
donor. Mouse arms have no Braak. Single-donor case studies are trivially known.
Developmental atlases don't have PMI or Braak. Drop these from the plan.

## Tier 3 (DUA-gated), which portals

All Tier 3 rows are Synapse or AD Knowledge Portal deposits under the standard
ROSMAP DUA. One DUA covers all five studies below.

| Study | Synapse ID | Datasets | Note |
|---|---|---:|---|
| Mathys2023 | syn52293433 | 1 | only ant_age missing |
| Mathys2024 | syn53366818 + ROSMAP clinical | 7 | only ant_age missing across all 7 |
| GreenFujita2024 | syn53693904, syn53694215 | 1 | 437 ROSMAP donors |
| Zhou2020 | syn21670836 (snRNAseqAD_TREM2) | 2 | ROSMAP subset, DLPFC |
| Prater2023 | syn51272902 | 1 | already used at ingest, only ant_pmi missing |

The Mathys23/24 + Zhou2020 rows are all missing only ant_age, and the underlying
ROSMAP clinical is already on disk (their index.md notes confirm "ROSMAP clinical
is joined in"). These are almost certainly resolved by the age-alias work in #326
rather than a fresh Synapse fetch. Verify obs columns before starting a data pull.

## Priority ordering, top 10 to backfill first

Ranked by AD-relevance, cell count, and low tier (T1 or T2 preferred).
GreenFujita2024 is intentionally excluded because #326 is expected to land the
facet aliases. Retest after that.

| # | db_id | group | cells | missing | tier | effort | why |
|---:|---|---|---:|---|:-:|---:|---|
| 1 | `CA6/QTL` | Castonguay2026 | 1,004,112 | braak pmi | T1 | 30m | CellxGene deposit; 109 donors; only ant_pmi/ant_braak missing. Almost certainly  |
| 2 | `RX4/ALL` | Rexach2024 | 432,555 | braak pmi | T1 | 45m | CellxGene h5ad already carries donor metadata in obs; missing facets likely a re |
| 3 | `D26/ALL` | Dharshini2026 | 424,528 | age braak | T1 | 30m | 46 donors, all Braak-graded per index.md description. Almost certainly a facet-a |
| 4 | `F24/EXN` | Fancy2024 | 62,905 | pmi | T1 | 30m | 7 per-cell-type subsets, all missing only ant_pmi. Age/braak/sex already present |
| 5 | `F24/OLI` | Fancy2024 | 53,818 | pmi | T1 | 30m | 7 per-cell-type subsets, all missing only ant_pmi. Age/braak/sex already present |
| 6 | `F24/AST` | Fancy2024 | 34,060 | pmi | T1 | 30m | 7 per-cell-type subsets, all missing only ant_pmi. Age/braak/sex already present |
| 7 | `HM4/EXN` | HuukiMyers2024 | 24,809 | age braak pmi | T1 | 30m | 10 neurotypical control donors , Braak trivially 0. PMI available via spatialLIB |
| 8 | `O22/INH` | OteroGarcia2022 | 23,197 | age braak | T1 | 30m | 16 donors (8 AD Braak VI + 8 controls); Braak status is by design in the paper. |
| 9 | `F24/INN` | Fancy2024 | 18,265 | pmi | T1 | 30m | 7 per-cell-type subsets, all missing only ant_pmi. Age/braak/sex already present |
| 10 | `F24/MIC` | Fancy2024 | 15,292 | pmi | T1 | 30m | 7 per-cell-type subsets, all missing only ant_pmi. Age/braak/sex already present |

Bulk observation. The biggest wins are the Wachter2024 / SerranoPozo2024 pair (11
datasets, ~1M cells, all missing only ant_disease which is likely a facet-alias
fix from existing `pathology_stage`), Castonguay2026 (1M cells, CellxGene deposit),
LuChen2025 (13 datasets, 793k cells, De Strooper lab in-house so cheap access),
and Dharshini2026 (425k cells, CellxGene, Braak already known).

## Full survey, one row per gap dataset

Sorted by tier then by cell count descending.

### Tier 1, 18 datasets, 2,144,809 cells, ~9.2h

| db_id | group | cells | missing | supp source | join key | effort |
|---|---|---:|---|---|---|---:|
| `CA6/QTL` | Castonguay2026 | 1,004,112 | braak pmi | CellxGene b3ab2f02 + Nat Genet 10.1038/s41588-026-02544-8... | donor_id | 30m |
| `RX4/ALL` | Rexach2024 | 432,555 | braak pmi | CellxGene c53573b2-eff4 (donor obs) + Cell 10.1016/j.cell... | donor_id | 45m |
| `D26/ALL` | Dharshini2026 | 424,528 | age braak | CellxGene 0d35c0fd + Nat Comms 10.1038/s41467-026-68920-4 | donor_id | 30m |
| `F24/EXN` | Fancy2024 | 62,905 | pmi | GEO GSE264648 series matrix + Acta Neuropathol supp ("Sup... | donor_id | 30m |
| `F24/OLI` | Fancy2024 | 53,818 | pmi | GEO GSE264648 series matrix + Acta Neuropathol supp ("Sup... | donor_id | 30m |
| `F24/AST` | Fancy2024 | 34,060 | pmi | GEO GSE264648 series matrix + Acta Neuropathol supp ("Sup... | donor_id | 30m |
| `HM4/EXN` | HuukiMyers2024 | 24,809 | age braak pmi | Bioconductor spatialLIBD + Science 10.1126/science.adh193... | donor_id | 30m |
| `O22/INH` | OteroGarcia2022 | 23,197 | age braak | Neuron 10.1016/j.neuron.2022.06.021 STAR Methods supp | donor_id | 30m |
| `F24/INN` | Fancy2024 | 18,265 | pmi | GEO GSE264648 series matrix + Acta Neuropathol supp ("Sup... | donor_id | 30m |
| `F24/MIC` | Fancy2024 | 15,292 | pmi | GEO GSE264648 series matrix + Acta Neuropathol supp ("Sup... | donor_id | 30m |
| `F24/OPC` | Fancy2024 | 14,110 | pmi | GEO GSE264648 series matrix + Acta Neuropathol supp ("Sup... | donor_id | 30m |
| `HM4/INN` | HuukiMyers2024 | 11,067 | age braak pmi | Bioconductor spatialLIBD + Science 10.1126/science.adh193... | donor_id | 30m |
| `HM4/OLG` | HuukiMyers2024 | 10,894 | age braak pmi | Bioconductor spatialLIBD + Science 10.1126/science.adh193... | donor_id | 30m |
| `F24/VAS` | Fancy2024 | 5,520 | pmi | GEO GSE264648 series matrix + Acta Neuropathol supp ("Sup... | donor_id | 30m |
| `HM4/AST` | HuukiMyers2024 | 3,979 | age braak pmi | Bioconductor spatialLIBD + Science 10.1126/science.adh193... | donor_id | 30m |
| `HM4/EMU` | HuukiMyers2024 | 2,157 | age braak pmi | Bioconductor spatialLIBD + Science 10.1126/science.adh193... | donor_id | 30m |
| `HM4/OPC` | HuukiMyers2024 | 1,940 | age braak pmi | Bioconductor spatialLIBD + Science 10.1126/science.adh193... | donor_id | 30m |
| `HM4/MIC` | HuukiMyers2024 | 1,601 | age braak pmi | Bioconductor spatialLIBD + Science 10.1126/science.adh193... | donor_id | 30m |

### Tier 2, 54 datasets, 4,052,477 cells, ~83.5h

| db_id | group | cells | missing | supp source | join key | effort |
|---|---|---:|---|---|---|---:|
| `G21/ADB` | Gerrits2021 | 466,339 | age braak pmi sex | GEO GSE178528 + Acta Neuropathol 10.1007/s00401-021-02263... | donor_id | 60m |
| `P26/CSM` | Prinz2026 | 270,493 | age braak pmi | Zenodo 16938034 (Prinz-lab) + Nat Immunol 10.1038/s41590-... | donor_id / sample_id | 120m |
| `LC5/NCO` | LuChen2025 | 249,644 | age braak disease pmi | Nat Med 10.1038/s41591-026-04393-8 supp + in-house donor ... | donor_id | 180m |
| `GT3/FAL` | Gittings2023 | 160,658 | braak pmi | Acta Neuropathol 10.1007/s00401-023-02599-5 supp | donor_id | 60m |
| `SP4/APF` | SerranoPozo2024 | 151,688 | disease | Springer ESM for Nat Neuro 10.1038/s41593-024-01791-4 (sa... | donor_id | 45m |
| `LC5/XNB` | LuChen2025 | 144,125 | age braak disease pmi sex | Nat Med 10.1038/s41591-026-04393-8 supp + in-house donor ... | donor_id | 180m |
| `P26/HMA` | Prinz2026 | 135,067 | age braak pmi | Zenodo 16938034 (Prinz-lab) + Nat Immunol 10.1038/s41590-... | donor_id / sample_id | 120m |
| `SP4/AV1` | SerranoPozo2024 | 133,875 | disease | Springer ESM for Nat Neuro 10.1038/s41593-024-01791-4 (sa... | donor_id | 45m |
| `AY1/HIP` | Ayhan2021 | 129,905 | braak pmi | CellxGene deposit; Neuron 10.1016/j.neuron.2021.05.003 ST... | donor_id | 45m |
| `SP4/AIT` | SerranoPozo2024 | 122,591 | disease | Springer ESM for Nat Neuro 10.1038/s41593-024-01791-4 (sa... | donor_id | 45m |
| `GT3/OAL` | Gittings2023 | 120,807 | braak pmi | Acta Neuropathol 10.1007/s00401-023-02599-5 supp | donor_id | 60m |
| `SP4/AV2` | SerranoPozo2024 | 119,300 | disease | Springer ESM for Nat Neuro 10.1038/s41593-024-01791-4 (sa... | donor_id | 45m |
| `P26/HMI` | Prinz2026 | 118,762 | age braak pmi | Zenodo 16938034 (Prinz-lab) + Nat Immunol 10.1038/s41590-... | donor_id / sample_id | 120m |
| `LC5/SNX` | LuChen2025 | 112,698 | disease pmi | Nat Med 10.1038/s41591-026-04393-8 supp + in-house donor ... | donor_id | 180m |
| `ZE5/ZAC` | Zelic2025 | 111,761 | age braak disease pmi sex | GEO GSE287257 + Immunity 10.1016/j.immuni.2025.02.024 supp | donor_id | 60m |
| `SP4/AEC` | SerranoPozo2024 | 101,489 | disease | Springer ESM for Nat Neuro 10.1038/s41593-024-01791-4 (sa... | donor_id | 45m |
| `P26/HMP` | Prinz2026 | 75,876 | braak pmi | Zenodo 16938034 (Prinz-lab) + Nat Immunol 10.1038/s41590-... | donor_id / sample_id | 120m |
| `WA4/MPF` | Wachter2024 | 75,740 | age disease pmi | Springer ESM for Acta Neuropathol 10.1007/s00401-024-0270... | donor_id | 90m |
| `WA4/MEC` | Wachter2024 | 68,662 | age disease pmi | Springer ESM for Acta Neuropathol 10.1007/s00401-024-0270... | donor_id | 90m |
| `G23/MIC` | Gazestani2023 | 67,513 | age braak disease pmi sex | Cell 10.1016/j.cell.2023.08.005 supp + Zenodo 8319719 (or... | donor_id (Finnish DBS cohort) | 60m |
| `GT3/OFT` | Gittings2023 | 66,719 | braak pmi | Acta Neuropathol 10.1007/s00401-023-02599-5 supp | donor_id | 60m |
| `WA4/MV2` | Wachter2024 | 66,309 | age disease pmi | Springer ESM for Acta Neuropathol 10.1007/s00401-024-0270... | donor_id | 90m |
| `WA4/MIT` | Wachter2024 | 65,800 | age disease pmi | Springer ESM for Acta Neuropathol 10.1007/s00401-024-0270... | donor_id | 90m |
| `Y22/BBB` | Yang2022_BBB | 65,479 | braak pmi | CellxGene deposit; Nature 10.1038/s41586-021-04369-3 ESM | donor_id | 45m |
| `LC5/VCF` | LuChen2025 | 61,739 | age disease pmi | Nat Med 10.1038/s41591-026-04393-8 supp + in-house donor ... | donor_id | 180m |
| `GA4/FCD` | Galvao2024 | 61,525 | braak pmi | iScience 10.1016/j.isci.2024.111337 supp | donor_id | 45m |
| `WA4/MV1` | Wachter2024 | 60,946 | age disease pmi | Springer ESM for Acta Neuropathol 10.1007/s00401-024-0270... | donor_id | 90m |
| `MI2/MI2` | Miedema2022 | 60,937 | age braak disease pmi sex | Acta Neuropathol Comms 10.1186/s40478-021-01306-3 supp; N... | donor_id | 60m |
| `LC5/VNF` | LuChen2025 | 60,129 | age disease pmi | Nat Med 10.1038/s41591-026-04393-8 supp + in-house donor ... | donor_id | 180m |
| `MA2/HPM` | Marsh2022 | 56,198 | age braak pmi sex | GEO GSE201048 + Nat Neuro 10.1038/s41593-022-01022-8 | donor_id | 60m |
| `SC9/MSB` | Schirmer2019 | 48,919 | braak | Nature 10.1038/s41586-019-1404-z ESM + PRJNA544731 | donor_id | 45m |
| `WA4/MCR` | Wachter2024 | 48,217 | age disease pmi | Springer ESM for Acta Neuropathol 10.1007/s00401-024-0270... | donor_id | 90m |
| `LC5/SOL` | LuChen2025 | 42,698 | disease pmi | Nat Med 10.1038/s41591-026-04393-8 supp + in-house donor ... | donor_id | 180m |
| `SM2/MDB` | Smajic2022 | 41,435 | age braak pmi sex | Brain 10.1093/brain/awab446 supp | donor_id | 60m |
| `K22/AST` | Kamath2022 | 33,506 | braak | CellxGene deposit; Nat Neuro 10.1038/s41593-022-01061-1 E... | donor_id | 60m |
| `LC5/SEX` | LuChen2025 | 30,554 | disease pmi | Nat Med 10.1038/s41591-026-04393-8 supp + in-house donor ... | donor_id | 180m |
| `LC5/CEN` | LuChen2025 | 26,400 | age disease pmi | Nat Med 10.1038/s41591-026-04393-8 supp + in-house donor ... | donor_id | 180m |
| `LC5/NBB` | LuChen2025 | 25,660 | age disease pmi | Nat Med 10.1038/s41591-026-04393-8 supp + in-house donor ... | donor_id | 180m |
| `T20/FUL` | Thrupp2020 | 18,763 | age braak disease pmi sex | Cell Reports 10.1016/j.celrep.2020.108189 supp | donor_id | 60m |
| `T20/ISC` | Thrupp2020 | 18,763 | age braak disease pmi sex | Cell Reports 10.1016/j.celrep.2020.108189 supp | donor_id | 60m |
| `T20/ITG` | Thrupp2020 | 18,763 | age braak disease pmi sex | Cell Reports 10.1016/j.celrep.2020.108189 supp | donor_id | 60m |
| `J19/MSW` | Jakel2019 | 17,799 | braak pmi | CellxGene deposit; Nature 10.1038/s41586-019-0903-2 | donor_id | 45m |
| `ZE5/ZMG` | Zelic2025 | 17,384 | age braak disease pmi sex | GEO GSE287257 + Immunity 10.1016/j.immuni.2025.02.024 supp | donor_id | 60m |
| `O20/OMI` | Olah2020 | 16,245 | age braak pmi | static/Suppl.Data14.zip (Olah cluster/donor supp) | donor_id | 60m |
| `LC5/SIN` | LuChen2025 | 15,081 | disease pmi | Nat Med 10.1038/s41591-026-04393-8 supp + in-house donor ... | donor_id | 180m |
| `G19/ENT` | Grubman2019 | 13,214 | age braak pmi sex | GEO GSE138852 series matrix + Nat Neuro 10.1038/s41593-01... | donor_id | 45m |
| `S19/SMI` | Sankowski2019 | 12,672 | age braak pmi sex | Nat Neuro 10.1038/s41593-019-0532-y ESM | donor_id | 60m |
| `LC5/SAS` | LuChen2025 | 12,638 | disease pmi | Nat Med 10.1038/s41591-026-04393-8 supp + in-house donor ... | donor_id | 180m |
| `Y22/MIC` | Yang2022_BBB | 10,092 | braak pmi | CellxGene deposit; Nature 10.1038/s41586-021-04369-3 ESM | donor_id | 45m |
| `LC5/SMI` | LuChen2025 | 6,033 | disease pmi | Nat Med 10.1038/s41591-026-04393-8 supp + in-house donor ... | donor_id | 180m |
| `LC5/SOP` | LuChen2025 | 5,688 | disease pmi | Nat Med 10.1038/s41591-026-04393-8 supp + in-house donor ... | donor_id | 180m |
| `SM2/SMC` | Smajic2022 | 3,903 | age braak pmi sex | Brain 10.1093/brain/awab446 supp | donor_id | 60m |
| `S23/WHI` | Seeker2023 | 3,851 | age braak pmi | ANC 10.1186/s40478-023-01568-z supp | donor_id | 45m |
| `SC9/MSM` | Schirmer2019 | 1,425 | braak | Nature 10.1038/s41586-019-1404-z ESM + PRJNA544731 | donor_id | 45m |

### Tier 3, 12 datasets, 550,504 cells, ~9.0h

| db_id | group | cells | missing | supp source | join key | effort |
|---|---|---:|---|---|---|---:|
| `P23/MIC` | Prater2023 | 127,367 | pmi | Synapse syn51272902 , DUA already used at ingest | donor_id | 30m |
| `MY4/MRM` | Mathys2024 | 92,800 | age | Synapse syn53366818 + ROSMAP clinical , DUA | individualID | 45m |
| `GF4/MIC` | GreenFujita2024 | 86,612 | age disease pmi sex | Synapse syn53693904 + ROSMAP clinical , DUA | individualID | 30m |
| `M23/IMM` | Mathys2023 | 83,889 | age | Synapse syn52293433 (ROSMAP clinical) , DUA | individualID | 45m |
| `Z20/ALL` | Zhou2020 | 63,163 | age | Synapse syn21670836 (snRNAseqAD_TREM2, AD Knowledge Porta... | individualID | 60m |
| `MY4/MEC` | Mathys2024 | 23,035 | age | Synapse syn53366818 + ROSMAP clinical , DUA | individualID | 45m |
| `MY4/MTH` | Mathys2024 | 21,834 | age | Synapse syn53366818 + ROSMAP clinical , DUA | individualID | 45m |
| `MY4/MHC` | Mathys2024 | 20,115 | age | Synapse syn53366818 + ROSMAP clinical , DUA | individualID | 45m |
| `MY4/MMT` | Mathys2024 | 10,964 | age | Synapse syn53366818 + ROSMAP clinical , DUA | individualID | 45m |
| `MY4/MPF` | Mathys2024 | 8,673 | age | Synapse syn53366818 + ROSMAP clinical , DUA | individualID | 45m |
| `MY4/MAG` | Mathys2024 | 8,179 | age | Synapse syn53366818 + ROSMAP clinical , DUA | individualID | 45m |
| `Z20/MIC` | Zhou2020 | 3,873 | age | Synapse syn21670836 (snRNAseqAD_TREM2, AD Knowledge Porta... | individualID | 60m |

### Tier 4, 26 datasets, 2,232,861 cells, ~51.0h

| db_id | group | cells | missing | supp source | join key | effort |
|---|---|---:|---|---|---|---:|
| `F26/LCC` | Francken2026 | 847,754 | disease pmi | Metacell integration of SEAAD/ROSMAP/Leuven/Macosko cohorts | SEACell → cohort split | 240m |
| `F26/ACC` | Francken2026 | 330,543 | disease pmi | Metacell integration of SEAAD/ROSMAP/Leuven/Macosko cohorts | SEACell → cohort split | 240m |
| `DB6/CPF` | DeBaene2026 | 250,764 | age braak disease pmi | Unpublished centenarian xenograft | iPSC donor | 60m |
| `F26/MCC` | Francken2026 | 182,134 | disease pmi | Metacell integration of SEAAD/ROSMAP/Leuven/Macosko cohorts | SEACell → cohort split | 240m |
| `D25/HAR` | Davis2025 | 142,851 | age braak disease pmi sex | Unpublished; internal | ? | 120m |
| `H25/AQC` | Hou2025 | 92,722 | age braak disease pmi | Unpublished; internal | ? | 120m |
| `D25/DAV` | Davis2025 | 43,552 | age braak disease pmi | Unpublished; internal | ? | 120m |
| `D25/SEX` | Davis2025 | 43,552 | age braak disease pmi | Unpublished; internal | ? | 120m |
| `S26/AQR` | Sun2026 | 38,029 | age braak disease pmi | Unpublished; internal cohort | ? | 120m |
| `S26/AQM` | Sun2026 | 37,752 | age braak disease pmi | Unpublished; internal cohort | ? | 120m |
| `S26/AQN` | Sun2026 | 37,483 | age braak disease pmi | Unpublished; internal cohort | ? | 120m |
| `S26/NCM` | Sun2026 | 37,225 | age braak disease pmi | Unpublished; internal cohort | ? | 120m |
| `S26/BLM` | Sun2026 | 34,056 | age braak disease pmi | Unpublished; internal cohort | ? | 120m |
| `S26/BPM` | Sun2026 | 34,056 | age braak disease pmi | Unpublished; internal cohort | ? | 120m |
| `S26/AQB` | Sun2026 | 33,949 | age braak disease pmi | Unpublished; internal cohort | ? | 120m |
| `S26/AQP` | Sun2026 | 33,949 | age braak disease pmi | Unpublished; internal cohort | ? | 120m |
| `SH9/SMB` | Shah2019 | 10,435 | age braak disease pmi sex | No DOI in index.md; likely older methods paper | ? | 60m |
| `SH9/SHA` | Shah2019 | 1,790 | age braak disease pmi sex | No DOI in index.md; likely older methods paper | ? | 60m |
| `H25/EBI` | Hou2025 | 59 | age braak disease pmi | Unpublished; internal | ? | 120m |
| `H25/RHG` | Hou2025 | 59 | age braak disease pmi | Unpublished; internal | ? | 120m |
| `H26/EB2` | Hou2026 | 49 | age braak disease pmi | Unpublished; internal (very small, 88 cells total) | ? | 60m |
| `H25/EBN` | Hou2025 | 30 | age braak disease pmi | Unpublished; internal | ? | 120m |
| `H25/EBR` | Hou2025 | 29 | age braak disease pmi | Unpublished; internal | ? | 120m |
| `H26/EBA` | Hou2026 | 24 | age braak disease pmi | Unpublished; internal (very small, 88 cells total) | ? | 60m |
| `H26/EPE` | Hou2026 | 8 | age braak disease pmi | Unpublished; internal (very small, 88 cells total) | ? | 60m |
| `H26/EBP` | Hou2026 | 7 | age braak disease pmi | Unpublished; internal (very small, 88 cells total) | ? | 60m |

### Tier 5, 22 datasets, 2,925,683 cells, ~0.0h

| db_id | group | cells | missing | supp source | join key | effort |
|---|---|---:|---|---|---|---:|
| `MZ4/SSM` | MallachZielonka2024 | 1,715,829 | braak disease pmi sex | Cell Reports 10.1016/j.celrep.2024.114216 , mouse model | sample_id | 0m |
| `M22/PFC` | Ma2022 | 172,120 | braak pmi | CellxGene deposit; Science 10.1126/science.abo7257 | donor_id | 0m |
| `M24/M23` | Mancuso2024 | 127,755 | age braak disease pmi | GEO GSE216999 + Nat Neuro 10.1038/s41593-024-01600-y ESM | donor_id (iPSC line) | 0m |
| `M24/RMF` | Mancuso2024 | 127,755 | age braak disease pmi | GEO GSE216999 + Nat Neuro 10.1038/s41593-024-01600-y ESM | donor_id (iPSC line) | 0m |
| `C25/PNR` | Clarence2025 | 101,924 | age braak pmi | CellxGene + Nat Genet 10.1038/s41588-025-02083-8 | donor_id | 0m |
| `B26/SOT` | Blackburn2026 | 88,582 | age braak disease pmi | Unpublished trisomy-21 xenograft | iPSC line | 0m |
| `B25/SCT` | Borrie2025 | 77,684 | age braak disease pmi sex | Unpublished xenograft | iPSC line | 0m |
| `B26/SOF` | Blackburn2026 | 73,888 | age braak disease pmi | Unpublished trisomy-21 xenograft | iPSC line | 0m |
| `B26/TSO` | Blackburn2026 | 73,888 | age braak disease pmi | Unpublished trisomy-21 xenograft | iPSC line | 0m |
| `AL1/CBL` | Aldinger2021 | 69,174 | age braak pmi | CellxGene deposit; Nat Neuro 10.1038/s41593-021-00872-y E... | donor_id | 0m |
| `Si5/SSI` | Simpson2025 | 59,900 | age braak disease pmi | Unpublished xenograft; internal | iPSC line | 0m |
| `Si5/MSS` | Simpson2025 | 42,270 | age braak disease pmi | Unpublished xenograft; internal | iPSC line | 0m |
| `Si5/SSF` | Simpson2025 | 42,270 | age braak disease pmi | Unpublished xenograft; internal | iPSC line | 0m |
| `MZ4/CMM` | MallachZielonka2024 | 37,840 | age braak disease pmi sex | Cell Reports 10.1016/j.celrep.2024.114216 , mouse model | sample_id | 0m |
| `AZ5/NST` | AlbertiniZielonka2025 | 35,298 | age braak disease pmi sex | Nat Neuro 10.1038/s41593-025-02125-8 supp; xenograft | iPSC line | 0m |
| `AZ5/SLM` | AlbertiniZielonka2025 | 22,420 | age braak disease pmi sex | Nat Neuro 10.1038/s41593-025-02125-8 supp; xenograft | iPSC line | 0m |
| `P24/AST` | Pan2024 | 18,890 | age braak | Acta Neuropathol Comms 10.1186/s40478-024-01853-5 supp | donor_id (n=1) | 0m |
| `S20/SIE` | Sierksma2020 | 15,599 | age braak disease pmi sex | EMBO Mol Med 10.15252/emmm.201910606 , mouse model paper | sample_id | 0m |
| `HZ3/NLP` | HouZielonka2023 | 8,611 | age braak disease pmi sex | Molecular Cell 10.1016/j.molcel.2023.10.029; in vitro + x... | iPSC line | 0m |
| `HZ3/LPS` | HouZielonka2023 | 8,395 | age braak disease pmi sex | Molecular Cell 10.1016/j.molcel.2023.10.029; in vitro + x... | iPSC line | 0m |
| `TW5/CMA` | TsaiWyssCoray2025 | 3,970 | age braak disease pmi | Cell Reports 10.1016/j.celrep.2025.116624 supp , mouse arm | sample_id | 0m |
| `P24/MIC` | Pan2024 | 1,621 | age braak | Acta Neuropathol Comms 10.1186/s40478-024-01853-5 supp | donor_id (n=1) | 0m |

## Per-group notes

Free-form notes on the trickier groups.

- **Castonguay2026** (T1). CellxGene deposit; 109 donors; only ant_pmi/ant_braak missing. Almost certainly a facet rename from existing obs.
- **Dharshini2026** (T1). 46 donors, all Braak-graded per index.md description. Almost certainly a facet-alias fix.
- **Fancy2024** (T1). 7 per-cell-type subsets, all missing only ant_pmi. Age/braak/sex already present. Supp Table 1 has PMI for 24 donors.
- **HuukiMyers2024** (T1). 10 neurotypical control donors , Braak trivially 0. PMI available via spatialLIBD. 7 per-cell-type datasets.
- **OteroGarcia2022** (T1). 16 donors (8 AD Braak VI + 8 controls); Braak status is by design in the paper.
- **Rexach2024** (T1). CellxGene h5ad already carries donor metadata in obs; missing facets likely a rename (ant_pmi/ant_braak).
- **Ayhan2021** (T2). 5 epilepsy donors from living surgical resections. PMI = 0 by design. Only ant_pmi/ant_braak flagged. Braak N/A (living, no path). Tier 5 for those two facets.
- **Galvao2024** (T2). 8 FCD (focal cortical dysplasia) donors, mostly pediatric surgical. Braak N/A. PMI = 0 (living surgical). ant_pmi/ant_braak are Tier 5 semantically.
- **Gazestani2023** (T2). Living donors (DBS surgery). Braak N/A. Age/sex/PMI(=0) in supp.
- **Gerrits2021** (T2). 18 donors (10 AD, 8 ctrl); ESM has full clinical.
- **Gittings2023** (T2). C9ORF72-ALS/FTD study, 3 datasets. Braak N/A (not AD). PMI in supp.
- **Grubman2019** (T2). 6 AD + 6 control EC donors; sex present, age/PMI/Braak in ESM.
- **Jakel2019** (T2). 9 MS+control donors; Braak N/A (MS not AD); PMI in ESM.
- **Kamath2022** (T2). Only ant_braak missing (age/pmi/disease/sex all present). Braak stage is in the paper supp for the 18 donors.
- **LuChen2025** (T2). 13 datasets (per cell class + spatial modalities). Ant_pmi/ant_disease missing on all 13; some also missing age/braak. Effort scales because of dataset count.
- **Marsh2022** (T2). 3 methods-paper donors run in duplicate. Clinical minimal (technical study). Braak likely N/A.
- **Miedema2022** (T2). 5 progressive-MS donors, no controls. Braak N/A. Age/PMI/sex likely in ESM.
- **Olah2020** (T2). Olah supp Data 14 (17 donors) + Nat Commun ESM tables. Zip on disk, needs unzip + parse.
- **Prinz2026** (T2). Cross-species meta-atlas; per-source-cohort clinical would need reconstruction. Mouse arms are Tier 5.
- **Sankowski2019** (T2). 5 human donors, mixed surgical + PM. Braak partially applicable.
- **Schirmer2019** (T2). 12 MS + 9 control donors; Braak N/A (MS); age/PMI/sex present in ESM per index.md.
- **Seeker2023** (T2). White-matter atlas; small microglia subset (3.8k cells).
- **SerranoPozo2024** (T2). Only ant_disease missing , likely a facet alias fix from existing pathology_stage rather than fetching new data. Effort might drop to 15 min.
- **Smajic2022** (T2). 6 idiopathic PD + 5 controls; Braak-Lewy staging in supp (not Braak-NFT/AD, semantically different).
- **Thrupp2020** (T2). 4 methods-paper donors. Age/sex in ESM; Braak likely not applicable (surgical resection donors).
- **Wachter2024** (T2). 32 donors along Braak strata, APOE+TREM2 encoded. Supp table lists age/PMI/pathology stage. Missing disease facet is just a rename of pathology_stage → diagnosis.
- **Yang2022_BBB** (T2). Vascular VINE-seq atlas; 17 AD+control donors; Braak/PMI in supp.
- **Zelic2025** (T2). 12 donors (4 ctrl + 8 ALS). Braak N/A (ALS not AD). Age/PMI/sex in supp.
- **GreenFujita2024** (T3). 437 ROSMAP donors , clinical is on-disk per index.md. Age/PMI/sex/disease should all be joinable. If ROSMAP obs already in obs, this is a facet alias fix (see task note).
- **Mathys2023** (T3). Only ant_age missing. ROSMAP DUA already in place per index.md; join by individualID; downgrade to Tier 1 if #326 age-alias lands.
- **Mathys2024** (T3). 7 datasets, only ant_age missing on each. Same story as Mathys2023 , likely a facet alias problem, not a fetch.
- **Prater2023** (T3). Only ant_pmi missing. Braak/CERAD already on obs. PMI is in the source Seurat; likely alias fix.
- **Zhou2020** (T3). 32 ROSMAP DLPFC donors; only ant_age missing. Same alias story as Mathys23/24.
- **Davis2025** (T4). 3 datasets, unpublished.
- **DeBaene2026** (T4). Xenograft but donor IDs come from real centenarian+AD+control donors , age IS applicable and available internally. Others (Braak) irrelevant. Coordinate with H De Baene.
- **Francken2026** (T4). Metacells not donors; per-metacell aggregation. Not meaningful to attach donor facets. Might be Tier 5 semantically.
- **Hou2025** (T4). 5 datasets. Unpublished; lab contact.
- **Hou2026** (T4). 4 tiny datasets (22 cells each). May be test/technical data.
- **Shah2019** (T4). No DOI recorded; needs paper lookup.
- **Sun2026** (T4). 8 datasets, all clinical facets missing. Unpublished; need lab contact.
- **AlbertiniZielonka2025** (T5). Human microglia xenografted into AppNL-G-F mice. No human clinical facets applicable.
- **Aldinger2021** (T5). Developmental cerebellum (PCW 9-21). Braak/PMI meaningless for embryos/fetuses. Only ant_age truly relevant; check whether existing PCW obs was aliased.
- **Blackburn2026** (T5). Xenograft; DOI pending. Ant_age/braak/pmi/disease meaningless for iPSC-derived cells.
- **Borrie2025** (T5). Xenograft , no human clinical semantics.
- **Clarence2025** (T5). 10 postnatal-development donors, all neurologically normal, no AD path. Braak/PMI not applicable; ant_age is the only relevant facet.
- **HouZielonka2023** (T5). In vitro + xenograft human microglia. No human clinical.
- **Ma2022** (T5). 4 human control donors, cross-primate paper. Braak trivially 0. PMI in ESM.
- **MallachZielonka2024** (T5). Mouse spatial transcriptomics; no human donor semantics.
- **Mancuso2024** (T5). Xenografted iPSC microglia. Anonymised donor lines. No Braak/PMI/age applicable.
- **Pan2024** (T5). Single-donor case study, CSF1R-RD leukoencephalopathy. Braak N/A. Age/PMI trivial.
- **Sierksma2020** (T5). Mouse model (APPswe/PS1 + Thy-TAU22). Human clinical facets meaningless.
- **Simpson2025** (T5). Xenograft human microglia in mice per project_xenograft_studies memory. No human clinical facets meaningful.
- **TsaiWyssCoray2025** (T5). Mouse cerebellum MERFISH microglia. Not human.

## Method

- Facet coverage from `/api/facets/{ant_donor,ant_age,ant_pmi,ant_braak,ant_disease,ant_sex}`.
- Gap set is datasets in ant_donor missing from at least one clinical facet.
- Per-group supp source pulled from `data/h5adstore/<group>/index.md` YAML
  (`doi`, `accession`) plus the free-text descriptions.
- On-disk files scanned under `data/h5adstore/<group>/static/`.
- Effort estimates are eyeballed against the Lee2026 reference backfill
  (TableS3/S4 CSV join, ~30 min).

## Caveats

- Tier 1 numbers assume the obs data is already on the h5ad or duckdb and the
  fix is a facet-alias rename. If the obs column is missing entirely, that row
  moves to Tier 2 and adds ~30 min.
- I did not open individual h5ads to confirm obs column names. That is the
  next step per dataset before starting the backfill.
- "Braak N/A" annotations for MS, PD, and ALS studies are semantic. Those
  studies don't stage by Braak-NFT (some use Braak-Lewy or Braak-tau in
  different scales). Attaching them under the same `ant_braak` facet would
  create a mixed-scale problem. Keep them missing rather than backfilling.
- Xenograft datasets (Tier 5) have a donor identity in the sense of "which iPSC
  line was xenografted" but no human clinical context in the AD-facet sense. If
  a future decision is to expose iPSC-donor age / genotype under a separate
  facet, revisit these.
