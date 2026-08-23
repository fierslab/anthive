# Astroglia (and Neuron / Other) Dataset Wishlist — 2026-08-23

Goal: expand the anthive4 store to cover **astrocytes** as broadly as we do
microglia today, plus a shorter starter for **neurons** and other
non-microglial types. Human, AD-relevant + control cohorts preferred;
single-nucleus > single-cell for post-mortem tissue.

## Inventory-of-record (2026-08-23)

Sources consulted:
- CellxGene Discover REST API (`/curation/v1/datasets`, 2,216 datasets across
  388 collections; filtered to human brain tissue + `cell_type=astrocyte` →
  35 collections)
- GEO supplementary listings for named papers (Grubman 2019, Habib 2020,
  Sadick 2022, Kamath 2022, Lerma-Martin 2024, Absinta 2021)
- WebSearch for known-cited AD-astrocyte papers

Not consulted this pass (flag for follow-up): AD Knowledge Portal
(Sage/Synapse), Human Cell Atlas Data Portal, EGA/dbGaP-controlled datasets.

## Section 1 — What we already have (astrocyte-tagged)

Grepped `data/h5adstore/*/*.h5ad.mus` for `cell_type=astrocyte`. Then
manually cross-checked group-level `.env`:

| Group | Files w/ astrocyte tag | Notes |
|---|---|---|
| SerranoPozo2024 | 5 (EC, ITG, PFC, V1, V2) | 628k astrocytes, 5 regions × 32 donors; already the anchor |
| Gabitto2024 | 2 (Seattle_MTG_Astro, Seattle_PFC_Astro) | SEA-AD |
| Francken2026 | 2 (PFC_Astrocytes_combat + meta) | Unpublished; SEACell pseudobulk |
| Pan2024 | 1 (Astrocytes) | CSF1R disorder + AD |
| Preman2024 | 1 (Astrocytes) | APOE mouse; useful comparator |
| Shah2019 | 1 (human_astrocytes_a12) + mouse | Unpublished / Disha |
| Zhou2020 | 1 (all_nuclei) | Contains astrocyte fraction |
| Hasel2021 | 1 (mouse_neuroinflammation) | Mouse only |

Plus astrocyte-containing but not tagged-at-object level: **Dharshini2026**
(L4 resilience), **Seeker2023** (WM microglia; has astrocyte fraction),
**Gerrits2021**, and others whose `all_nuclei`/multi-celltype objects
include an astrocyte cluster.

## Section 2 — Astrocyte wishlist (32 CellxGene + external)

Priority scale — H (must-have, AD-relevant), M (nice-to-have), L
(exploratory / niche). Access column tags:

- `open-cxg` — direct H5AD download from CellxGene datasets.cellxgene.cziscience.com
- `open-geo` — direct HTTP from GEO FTP (counts / mtx tar)
- `dua-synapse` — Sage/Synapse Data Use Agreement required
- `dua-broad` — Broad Single-Cell Portal login required
- `dua-dbgap` — dbGaP controlled access

### Tier H1 — AD and dementia (astrocyte-heavy)

| # | Paper | Cells | Access | CID / accession | Status |
|---|---|---|---|---|---|
| A1 | Rexach 2024 *Cell* — Cross-dementia (AD, Pick, PSP) | 432,555 | open-cxg | c53573b2-... / GSE243639 | **staged** |
| A2 | Sadick 2022 *Neuron* — Astro+Oligo AD APOE-ε2/3 | ~40k | open-geo | GSE167494 (1.5 GB tar) | **staged** (needs mtx→h5ad) |
| A3 | Grubman 2019 *Nat Neurosci* — EC AD | 13,214 | open-geo | GSE138852 (12 MB csv) | **staged** (csv→h5ad) |
| A4 | Habib 2020 *Nat Neurosci* — DAA (mouse) | ~5k astro | open-geo | GSE143758 (77 MB) | **staged** (mouse) |
| A5 | Leng 2021 *Nat Neurosci* — EC + SFG AD/tau vulnerability | ~42k | dua-synapse | syn21788402 | **BLOCKED** |
| A6 | Lee 2024 medRxiv PsychAD — cross-disorder PFC | 7,652,614 | open-cxg | 84ce6837-... | pending (30+ GB) |
| A7 | Gabitto 2024 SEA-AD — full atlas (not just Astro) | 3,031,001 | open-cxg | 1ca90a2d-... | overlaps with our Gabitto2024; skip full re-download |
| A8 | Yang 2022 — brain vascular BBB + AD | 65,479 | open-cxg | 433700dc-... | **staged** |

### Tier H2 — Parkinson's / synucleinopathy astrocytes

| # | Paper | Cells | Access | CID / accession | Status |
|---|---|---|---|---|---|
| A9 | Kamath 2022 *Nat Neurosci* — SN dopamine PD (9 celltypes) | 572,156 total | open-cxg | b0f0b447-... | **staged** (9 h5ads, 4.5 GB) |
| A10 | Menon 2026 bioRxiv — multi-region PD cell atlas | 8,802,568 | open-cxg | 16876983-... | pending (30 GB full, 31 GB subsample) |
| A11 | Martirosyan 2024 *Sci Data* multi-region PD atlas | 2,096,155 | open-cxg | d5d0df8f-... | pending (28 GB) |
| A12 | Smajic 2022 midbrain PD | already in store | — | — | have |

### Tier H3 — MS / demyelination

| # | Paper | Cells | Access | CID / accession | Status |
|---|---|---|---|---|---|
| A13 | Jäkel 2019 *Nature* — MS WM | 17,799 | open-cxg | 16c1e722-... | **staged** |
| A14 | Absinta 2021 *Nature* — chronic active MS | ~66k | SRA fastq (PRJNA544731) | needs re-processing | **BLOCKED** (fastq only) |
| A15 | Lerma-Martin 2024 *Nat Neurosci* — subcortical MS | ~90k paired snRNA+ST | open-geo | GSE279181 (RAW tar) | pending |
| A16 | Schirmer 2019 | already in store | — | — | have |
| A17 | Miedema 2022 | already in store | — | — | have |

### Tier H4 — ALS/FTD/spinal cord

| # | Paper | Cells | Access | CID / accession | Status |
|---|---|---|---|---|---|
| A18 | Gittings 2023 *Acta Neuropathol* — C9-ALS/FTD frontal+occipital | 461,290 | open-cxg | aee9c366-... | **staged** (3 of 4: frontal C9-ALS 161k, occipital C9-ALS 121k, occipital C9-FTD 67k; frontal C9-FTD 113k skipped) |
| A19 | Takeuchi 2025 *Brain* — ALS motor cortex + spinal cord multiome | 138,294 | open-cxg | 0986e4cd-... | pending |
| A20 | Zelic 2025 spinal cord | already in store | — | — | have |

### Tier M — cortical development / evolution / other adult

| # | Paper | Cells | Access | CID | Status |
|---|---|---|---|---|---|
| A21 | Castonguay 2026 *Nat Genet* — cerebellum eQTL (essential tremor + normal) | 1,004,112 | open-cxg | b3ab2f02-... | **staged** |
| A22 | Ayhan 2021 *Neuron* — hippocampus A-P axis (epilepsy) | 129,905 | open-cxg | f17b9205-... | **staged** |
| A23 | Aldinger 2021 *Nat Neurosci* — cerebellar development | 69,174 | open-cxg | 1b014f39-... | **staged** |
| A24 | Ma 2022 *Science* — dlPFC primate evolution (human subset) | 172,120 | open-cxg | e1fa9900-... | **staged** |
| A25 | Clarence 2025 *Nat Genet* — postnatal brain snRNA/ATAC | 101,924 | open-cxg | f406a653-... | **staged** |
| A26 | Wang 2025 *Nature* — developing neocortex multiome | 232,328 | open-cxg | ad2149fc-... | pending (2.6 GB) |
| A27 | Velmeshev 2023 *Science* — pre/postnatal cortical dev | 709,372 | open-cxg | bacccb91-... | pending (5.6 GB) |
| A28 | Nascimento 2024 *Nature* — EC neuron recruitment young children | 153,005 | open-cxg | cae8bad0-... | pending |
| A29 | Galvão 2024 *iScience* — focal cortical dysplasia | 61,525 | open-cxg | 9e4e8f1d-... | **staged** |
| A30 | Galvão 2026 bioRxiv — pediatric epilepsy adolescent brain | 166,016 | open-cxg | 4624894f-... | pending |
| A31 | Siletti 2023 *Science* — Human Brain Cell Atlas v1.0 | 1,957,555 (astro subset) | open-cxg | 283d65eb-... | pending (huge; download only "All non-neuronal cells" 4.4 GB) |
| A32 | Jorstad 2023 *Science* — neocortex cytoarchitecture | 2,361,061 | open-cxg | d17249d2-... | pending |

### Tier L — tumor / integrated / less-relevant to AD focus

| # | Paper | Cells | Access | CID | Status |
|---|---|---|---|---|---|
| A33 | Ruiz-Moreno GBmap | 1,474,241 | open-cxg | 999f2a15-... | skip (glioblastoma) |
| A34 | He 2024 organoid atlas | 3,687,800 | open-cxg | de379e5f-... | skip (organoid) |
| A35 | Cao 2020 fetal atlas | 5,064,268 | open-cxg | c114c20f-... | skip (mostly non-brain) |

## Section 3 — Neuron wishlist (top 10)

For excitatory + inhibitory neurons in human brain. Not re-listing already
in-store objects (Gabitto2024, Zielonka2025 L23, OteroGarcia2022 Inhibitory).

| # | Paper | Cells | Access | Status |
|---|---|---|---|---|
| N1 | Menon 2026 PD multi-region | 8.8M (huge neuron fraction) | open-cxg | pending |
| N2 | Lee 2024 PsychAD PFC | 7.6M | open-cxg | pending |
| N3 | Rexach 2024 | 173k neurons | open-cxg | **staged** (via A1) |
| N4 | Lattke 2026 *Nat Med* — Down Syndrome cortical dev | 464k Exc + 249k Inh | open-cxg | pending |
| N5 | Bhaduri 2021 developing neocortex | 458k | open-cxg | pending |
| N6 | Wang 2025 developing neocortex | 232k | open-cxg | pending |
| N7 | Kamath 2022 DA + non-DA neurons | 22k + 91k | open-cxg | **staged** (via A9) |
| N8 | Nascimento 2024 EC young children | 153k | open-cxg | pending |
| N9 | Ma 2022 dlPFC primate | 172k | open-cxg | **staged** (via A24) |
| N10 | Ayhan 2021 hippocampus epilepsy | 130k | open-cxg | **staged** (via A22) |

## Section 4 — Other cell types (top per type, short)

**Oligodendrocyte** — 41 CellxGene collections. Top uncovered: Menon PD,
Lee PsychAD, Rexach (staged), Kamath Oligo (staged, 179k), Ayhan
(staged, 67k), Jäkel MS (staged).

**OPC** — Kamath OPC (staged, 14k), Rexach (staged), Clarence (staged).

**Endothelial / vascular** — Yang 2022 BBB (staged; **AD-relevant, 30
donors, 3 disease groups**), Kamath Endo (staged), Menon (pending).

**Pericyte / VLMC** — via Menon, Siletti HBCA (both pending).

**T cells / peripheral immune** — Menon multi-region (largest T-cell yield
in human brain snRNA-seq to date), Rexach (staged, small T-cell
fraction).

## Section 5 — What was downloaded / staged this pass

Total: **14 dataset packages**, **21.7 GB** across
`data/h5adstore/_staging/astrocyte/`.

| Folder | Files | Size | Format | Notes |
|---|---|---|---|---|
| Aldinger2021 | 1 h5ad | 540 M | CxG h5ad | 69,174 cells; 1,225 astro |
| Ayhan2021 | 1 h5ad | 673 M | CxG h5ad | 129,905 cells; 10,960 astro; epilepsy |
| Castonguay2026 | 1 h5ad | 5.8 G | CxG h5ad | 1M cells; 43,457 astro; cerebellum ET+normal |
| Clarence2025 | 1 h5ad | 1.2 G | CxG h5ad | 101,924 cells; 19,071 astro; postnatal |
| Galvao2024 | 1 h5ad | 627 M | CxG h5ad | 61,525 cells; 5,347 astro; FCD |
| Gittings2023 | 3 h5ad | 7.0 G | CxG h5ad | frontal C9-ALS 161k (14,802 astro); occipital C9-ALS 121k (12,914 astro); occipital C9-FTD 67k (8,532 astro) |
| Grubman2019 | 2 csv.gz | 12 M | GEO counts | Needs csv → h5ad conversion (`Grubman2019/`) |
| Habib2020 | 3 gz | 77 M | GEO counts | Mouse only; 3 timecourses |
| Jakel2019 | 1 h5ad | 44 M | CxG h5ad | 17,799 cells; 1,242 astro; MS WM |
| Kamath2022 | 9 h5ad | 4.5 G | CxG h5ad | Split by celltype; 33,506 astro, 22,048 DA, plus MG/Oligo/OPC/Endo/NonDA/NurrPos/NurrNeg |
| Ma2022 | 1 h5ad | 2.2 G | CxG h5ad | 172,120 cells; 23,359 astro; dlPFC human subset of primate atlas |
| Rexach2024 | 1 h5ad | 4.7 G | CxG h5ad | 432,555 cells; 59,096 astro; AD/Pick/PSP/ctrl |
| Sadick2022 | 1 tar | 1.5 G | GEO mtx tar | Needs `scanpy.read_mtx` + concat |
| Yang2022_BBB | 1 h5ad | 599 M | CxG h5ad | 65,479 cells; 1,482 astro; BBB in AD |

All staged h5ads validated with `anndata.read_h5ad`, obs schema captured,
cell counts match paper claims within 1%.

## Section 6 — BLOCKED downloads (need Mark to unlock)

| Paper | Reason | Where to go |
|---|---|---|
| **Leng 2021** *Nat Neurosci* AD EC+SFG | Synapse DUA | https://www.synapse.org/#!Synapse:syn21788402  (part of SEA-AD? probably ROSMAP-linked) |
| **Absinta 2021** *Nature* MS chronic active | Deposited as raw FASTQ only (PRJNA544731); needs full reprocessing | https://www.ncbi.nlm.nih.gov/bioproject/PRJNA544731 |
| **Kamath 2022 Slide-seq / raw** | dbGaP phs002879 | https://www.ncbi.nlm.nih.gov/projects/gap/cgi-bin/study.cgi?study_id=phs002879.v1.p12  (already have processed via CellxGene) |
| **AD Knowledge Portal / Synapse cohorts** (Mayo, MSBB, ROSMAP-raw beyond public) | Sage DUA | Portal login |
| **Menon 2026 / Martirosyan 2024** — huge but open | Large size only (30+ GB per file); not blocked, just needs decision to allocate space | queue for next batch |

## Section 7 — Ingestion caveats (READ before ant-convert)

- All CellxGene h5ads have X = **log-normalized** (matches store convention);
  raw counts in `layers['counts']` when present. **However** none of the
  staged files show a `layers` attribute — CxG typically packs raw into
  `raw.X`. Need to check via `ant-convert` / `mus h5ad check` before
  ingest; if `raw` is populated, keep the log X but reference `raw.X`
  for counts.
- Kamath 2022 h5ads are **10x scRNA-seq** (not snRNA-seq) for some cell
  types (MG, Endo, Astro) but snRNA-seq for others (DA, NonDA, Oligo,
  OPC, NurrPos, NurrNeg). Verify `suspension_type` per file at ingest.
- Grubman 2019 supplementary is a **dense CSV** (genes × cells); not
  ingestible as-is. Requires: `pd.read_csv` → transpose → sparse →
  `AnnData` with a manually built obs from the `covariates.csv`.
- Habib 2020 is mouse. Keep only if we want a cross-species DAA
  comparator; otherwise deprioritize.
- Sadick 2022 tar unpacks to per-sample MTX; needs `scanpy.read_mtx` +
  concat + per-donor obs table (from paper Supp Table S1 — not in the
  GEO metadata directly).
- Yang 2022 has `X_dtype=float64` (all others are float32) — worth
  down-casting during ant-convert.

## Section 8 — Deferred (not this pass)

- Siletti 2023 Human Brain Cell Atlas — considered but 16-dataset
  collection; would need selective ingest of "All non-neuronal cells"
  (4.4 GB) rather than the 30 GB neurons object.
- Bhaduri 2021 developing neocortex — didn't appear in the primary
  human-brain-astrocyte filter (may lack `cell_type=astrocyte` label at
  dataset level); relist next pass with broader `radial glia`
  matching.
- AD Knowledge Portal: Mathys 2019 *Nature* (ROSMAP, PFC, 80k nuclei —
  the original human-cortex AD paper). We may already have this via
  Mathys2024, but 2019 is a separate GSE (probably syn18485175). Flag
  for the next Synapse-access pass.
- Kang 2020 *Cell Reports* — human midbrain organoid; skip (organoid).
- Wilson 2023 medRxiv — chromatin accessibility in dementia; also on
  CellxGene?

## Section 9 — Actions

1. **Mark** to unlock: Synapse account for Leng 2021 (A5), Mathys 2019,
   any Sage/ROSMAP-raw beyond CellxGene mirrors.
2. **Next automated pass**: convert staged h5ads to duckdb via
   `ant-convert`, using per-collection index.md derived from CellxGene
   collection metadata. Priority: A1 (Rexach), A9 (Kamath), A13
   (Jakel), A22 (Ayhan), A25 (Clarence), A24 (Ma), Yang 2022, Kamath
   Astro.
3. **Later**: schedule the ≥30-GB downloads (Menon PD full, Lee PsychAD
   full, Martirosyan) with `nohup` + retry. NCBI limits transfer size
   per connection; CellxGene is faster and more reliable.
4. **Neurons + others** sweep: already largely covered by the same
   staged files (Rexach, Kamath, Ayhan, Ma, Clarence). Only Lee
   PsychAD, Menon PD, and Lattke Down Syndrome are neuron-primary
   additions.
