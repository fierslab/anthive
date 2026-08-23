# Non-microglia geneset wishlist — 2026-08-23

Status: initial scope. Three catalogs shipped from PanglaoDB (broad markers).
Further catalogs listed here are candidates awaiting supplementary-table
access. Order = rough priority.

## Shipped (this pass)

- `PanglaoDB2019/Glia_markers/` — 10 cell-type marker panels (astrocytes,
  oligodendrocytes, OPCs, ependymal, Bergmann glia, radial glia, tanycytes,
  Schwann, satellite glia, neural stem/precursor).
- `PanglaoDB2019/Neuron_markers/` — 17 cell-type marker panels (pan-neuron,
  interneurons, pyramidal, GABAergic, glutaminergic, dopaminergic, etc.).
- `PanglaoDB2019/Vasculature_markers/` — 5 brain-relevant vascular / barrier
  cell-type panels (endothelial, BBB endothelial, pericytes, meningeal,
  choroid plexus).

Provenance: PanglaoDB v2020-03-27 (`PanglaoDB_markers_27_Mar_2020.tsv.gz`),
filtered to rows whose `species` field contains `Hs`. Symbols are the
`official gene symbol` column verbatim (HGNC uppercase). Doi:
[10.1093/database/baz046](https://doi.org/10.1093/database/baz046).

These are **broad markers, not modules** — good for cell-type ID and QC.
For state/module structure use the study-specific catalogs.

## Candidates — Astrocytes

| Source | Description | Access route |
|---|---|---|
| Liddelow 2017 (Nature) | Pan-reactive / A1 / A2 panels — mouse origin, widely reused as human orthologs | Nature paywall. Full lists appear in Zamanian 2012 Supp Tables (J Neurosci, open). Need PDF text extraction from the tables. |
| Zamanian 2012 (J Neurosci) | LPS-only / MCAO-only / shared reactive astrocyte genes (56+ pan-reactive) | Open access. Extract Table 1 + Table 2 gene lists from the PDF supplementary. |
| Habib 2020 (Nat Neurosci) | Disease-associated astrocytes (DAA) markers — mouse 5xFAD | Nature paywall. Supp Table 1 gene lists available at PMC (need pdf extraction). Ship as `organism: mouse`. |
| Sadick 2022 (Neuron) | Human snRNA-seq astrocyte + oligodendrocyte subtypes with marker genes | Cell paywall for supp; analysis code on [GitHub liddelowlab/Sadick_et_al._2022](https://github.com/liddelowlab/Sadick_et_al._2022). Marker gene lists in Supp Table S3/S4 — need direct download from Cell. |
| Leng 2021 (Nat Neurosci) | Human AD entorhinal + SFG astrocyte subpopulations | Nature paywall. Supp Table 5. |
| Escartin 2021 (Nat Neurosci) | Consensus reactive astrocyte nomenclature panel | Nature paywall. |
| Endo 2022 (Science) | Reactive astrocyte states (multi-model integration) | Science paywall. |
| Ma 2021 (Nat Comm) | Human APOE4 astrocyte trajectory | Open access. Supp Table S3 has module gene lists. |
| Zheng 2023 (Nat Aging) | Astrocyte WGCNA modules in AD | Nature paywall. |

## Candidates — Neurons

| Source | Description | Access route |
|---|---|---|
| Mathys 2019 (Nature) | Excitatory / inhibitory neuron subclusters, DE genes in AD (ROSMAP) | Nature paywall. Supp Table 2. |
| Grubman 2019 (Nat Neurosci) | Entorhinal cortex snRNA-seq — neuron subtype markers | Nature paywall. |
| Cain 2023 (Nat Neurosci) | Neuron vulnerability signatures in AD | Nature paywall. |
| Leng 2021 (Nat Neurosci) | Excitatory neuron subpopulations in AD (RORB+, etc.) | Nature paywall. |
| Yang 2022 (Nat Aging) | Pyramidal neuron aging signatures | Nature paywall. |
| Allen Institute (Hodge 2019, Nature) | MTG excitatory / inhibitory subclass markers | Open access via [Allen portal](https://portal.brain-map.org/). |

## Candidates — Oligodendrocyte / OPC

| Source | Description | Access route |
|---|---|---|
| Zhou 2020 (Nat Med) | Oligodendrocyte AD-associated modules | Nature paywall. |
| Marques 2016 (Science) | Oligodendrocyte lineage states (MOL1-MOL6, COP, NFOL) | Science paywall. Supp Table S1 has state markers; also curated at [linnarssonlab.org](http://linnarssonlab.org/oligodendrocytes/). |
| Sadick 2022 (Neuron) | Human oligodendrocyte subtypes in AD | See astrocyte row above. |
| Falcao 2018 (Nat Med) | Disease-associated oligodendrocytes (DA-OL) in EAE | Nature paywall. |
| Kenigsbuch 2022 (Nat Neurosci) | Oligodendrocyte aging module (MOL2/MOL5 shift) | Nature paywall. |

## Candidates — Vasculature (finer than PanglaoDB)

| Source | Description | Access route |
|---|---|---|
| Yang 2022 (Nature) | Human brain vasculature atlas — arterial/venous/capillary EC + arteriolar/venular SMC + fibromyocyte + pericyte subclusters | Nature paywall. Supp Data 1 has full marker table. |
| Garcia 2022 (Science) | Human brain vasculature single-cell — parallel resource to Yang | Science paywall. |
| Vanlandewijck 2018 (Nature) | Mouse brain vasculature zonation | Nature paywall. Data at [betsholtzlab.org](http://betsholtzlab.org/VascularSingleCells/database.html). |

## Candidates — Pathway / general (deprioritized)

MSigDB HALLMARK, KEGG, Reactome — not cell-type-specific and covered by GSEA
tooling. Not part of this pass. Would require a different shape (a fourth
"Study" for MSigDB with dozens of experiments).

## Access blockers

Most of the primary literature above is behind Nature / Cell / Science
paywalls when accessed programmatically. Manual retrieval + parsing of
supplementary Excel/PDF files is required to ship exact gene lists. If the
user has institutional access (KU Leuven), the workflow would be:

1. Manually download Supp Table X (Excel).
2. Drop it under `data/genesetstore/<Study>/<Experiment>/_raw/`.
3. Add an `ingest_<study>.py` script to `bin/annot/` or `scratchpad/` that
   parses the Excel to `.grp` + `.md` files with clean HGNC symbols.
4. `ant-geneset validate <Study>` and restart ant-serve.

The PanglaoDB catalog shipped in this pass is a broad first coverage
layer — the study-specific module/state catalogs above are the higher-value
follow-ups.
