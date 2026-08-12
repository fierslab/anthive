# Sierksma2025 paper figures → interactive anthive plots

Mapping of the figures in `20260407_PRS_Figs_NatNeurosci_V1.pdf` to **interactive
anthive web visualizations**, where the underlying data exists in the catalogue.

**Read this first — what these are (and aren't):**

- These are **rough, interactive analogs**, not pixel reproductions. anthive
  renders its own plot from the same underlying single-cell data; palettes,
  normalization, subsetting and statistical models differ from the paper.
- Every link is **live and verified to render** (I hit each plot endpoint;
  only included the ones that returned a real figure).
- **Links are relative** to the deployed web UI. In dev that's
  `https://localhost.localdomain:8501` (behind Authelia — log in first); in
  prod it's `https://data.nevel.eu`. Each link opens the **interactive plot
  page** (form + live preview + PNG/SVG/PDF download), so you can swap the
  gene / module / contrast right there.
- **Datasets used** (the S25 collection):
  `S25/V6A` = Village.6M (235k cells, the main one),
  `S25/V6W` = Village.6M WGCNA modules (the 13 `ME*` modules as "genes",
  shares the UMAP).

Figures that are **imaging, flow cytometry, GSEA bar charts, derived
heatmaps/correlation matrices, power analyses, or donor-level scatters** have
no single-cell plot equivalent in anthive and are noted as such — see the
[Remarks](#remarks--limitations) at the end.

---

## Fig 1 — microglia village approach

- **1A** (workflow schematic) — diagram, not data. No plot.
- **1B** microglia composition per sample — **stacked bar** (REST API 2.6
  `bar_mode=stack`), one bar per mouse stacked by donor line — the village
  composition, matching the paper's panel:
  [`bar x=mouse, stacked by donor line`](/browse/S25/V6A/plot/bar?x=mouse&color=patient_geno_short&bar_mode=stack)
  · [`grouped by Village`](/browse/S25/V6A/plot/bar?x=mouse&color=patient_geno_short&bar_mode=stack&group_by=Village)
  · [`100% proportions`](/browse/S25/V6A/plot/bar?x=mouse&color=patient_geno_short&bar_mode=stack&normalize=true).
- **1C** Sex: Female vs Male volcano — **direct DE match**:
  [`volcano sex_mouse / m`](/browse/S25/V6A/plot/volcano?study=DE_interpretable_glmm_vs_grandmean&term=sex_mouse&contrast=m).
  Also browsable: [DE explorer → sex_mouse](/browse/S25/V6A/de/DE_interpretable_glmm_vs_grandmean?term=sex_mouse).

## Fig 2 — genotype-specific homeostatic states

- **2A** per-donor UMAP density — **density on the UMAP**, interactively subset to
  one donor line:
  [`hexbin x=X_umap`](/browse/S25/V6A/plot/hexbin?x=X_umap)
  · one donor: [`…&subset=patient_geno_short:L23`](/browse/S25/V6A/plot/hexbin?x=X_umap&subset=patient_geno_short:L23)
  (no faceting — pick a donor with the subset field). Genotype on the UMAP:
  [`scatter color=Strain`](/browse/S25/V6A/plot/scatter?x=X_umap&color=Strain).
- **2B** DEGs-per-line bar — a derived per-line count; not a single-cell plot.
- **2C** microtype × module enrichment heatmap — derived statistic; no equivalent.
- **2D** modules on the UMAP — **module score on the UMAP** (V6W shares the embedding):
  [`scatter color=MEmagenta`](/browse/S25/V6W/plot/scatter?x=X_umap&color=MEmagenta)
  — swap `MEmagenta` for any of: MEblack, MEblue, MEbrown, MEgreen,
  MEgreenyellow, MEpink, MEpurple, MEred, MEsalmon, MEtan, MEturquoise, MEyellow.
- **2E** donor × module Z-score heatmap — derived; no equivalent (but see 2F).
- **2F** MEmagenta / MEbrown Z-score per donor — **module forest across donors**:
  [`forest MEmagenta`](/browse/S25/V6W/plot/forest?gene=MEmagenta&study=Module_DE_interpretable_lmem_vs_grandmean&term=patient_geno_short_relevel)
  · [`forest MEbrown`](/browse/S25/V6W/plot/forest?gene=MEbrown&study=Module_DE_interpretable_lmem_vs_grandmean&term=patient_geno_short_relevel).

## Fig 3 — cohort-wide response to amyloid (huDAM + antigen-presenting)

- **3A** AppNLGF vs AppHu volcano — **direct DE match**:
  [`volcano Strain / AppNLGF - AppHuHu`](/browse/S25/V6A/plot/volcano?study=DE_interpretable_glmm_vs_grandmean&term=Strain&contrast=AppNLGF%20-%20AppHuHu)
  · full [DE explorer → Strain](/browse/S25/V6A/de/DE_interpretable_glmm_vs_grandmean?term=Strain).
  Top genes (EDN1, CD9, ITGAX, APOE, TREM2…) are labelled on the volcano and
  individually forestable, e.g. [`forest CD9`](/browse/S25/V6A/plot/forest?gene=CD9&study=DE_interpretable_glmm_vs_grandmean&term=Strain).
- **3B** module enrichment bars (AppNLGF vs AppHu) — GSEA-style derived bars; the
  closest interactive analog is the module forest / module-on-UMAP above
  (e.g. brown = lysosome/huDAM, magenta = antigen presentation).
- **3C** FACS density (FSC × HLA-DR) — flow cytometry; not in the scRNA data.
- **3D–H** HLA-DR / HLA-ABC / CD80 / CD86 / PDL1 protein (MFI) by genotype —
  flow protein, **not** RNA. **Transcriptomic analog** = expression of the same
  genes by genotype:
  [`violin HLA-DRA by Strain`](/browse/S25/V6A/plot/violin?x=Strain&y=HLA-DRA)
  · [`CD80`](/browse/S25/V6A/plot/violin?x=Strain&y=CD80)
  · [`CD86`](/browse/S25/V6A/plot/violin?x=Strain&y=CD86)
  · [`CD274 (PD-L1)`](/browse/S25/V6A/plot/violin?x=Strain&y=CD274)
  (bar variant: [`bar CD80 by Strain`](/browse/S25/V6A/plot/bar?x=Strain&y=CD80)).
  RNA ≠ protein — directional analog only.

## Fig 4 — MHCII activation as the dominant programmed response

- **4A** per-donor amyloid-response UMAP density — **MHCII / antigen-presentation
  microtype score on the UMAP**:
  [`scatter color=Antigen-presenting response (HLA)_microtype`](/browse/S25/V6A/plot/scatter?x=X_umap&color=Antigen-presenting%20response%20%28HLA%29_microtype)
  · disease-associated: [`color=Disease associated (DAM)_microtype`](/browse/S25/V6A/plot/scatter?x=X_umap&color=Disease%20associated%20%28DAM%29_microtype).
  (hexbin variant works too; subset by donor for the per-donor panels.)
- **4B** donor × module Z-score heatmap — derived; no equivalent.
- **4C** MEmagenta / MEbrown Z-score per donor — same module forests as **2F**.

## Fig 5 — MHCII activation reflects donor status and polygenic risk

- **5A** required-sample-size bar (power analysis) — pure statistic; no plot.
- **5B** mean log2FC magenta genes by ND/AD — donor-level derived value; not a
  single-cell plot.
- **5C** t-statistic per MHCII gene — derived DE stat; **per-gene analog** =
  forest of each gene across the Strain contrast, e.g.
  [`forest CD74`](/browse/S25/V6A/plot/forest?gene=CD74&study=DE_interpretable_glmm_vs_grandmean&term=Strain)
  · [`CIITA`](/browse/S25/V6A/plot/forest?gene=CIITA&study=DE_interpretable_glmm_vs_grandmean&term=Strain)
  · [`MS4A6A`](/browse/S25/V6A/plot/forest?gene=MS4A6A&study=DE_interpretable_glmm_vs_grandmean&term=Strain).
- **5D** mean log2FC magenta vs PRS (per donor, regression) — donor-level scatter
  (29 points); anthive plots cells, not donor aggregates → no equivalent.

## Supplementary

- **S1A** proportion-of-cells boxplot — derived. **S1B** UMAP with hMG /
  proliferating / CAMs labels — needs a clean cell-class categorical; the
  candidate columns (`Category`, `Status`) currently error on the server
  (mixed-type bug, see Remarks), so this one isn't cleanly reproducible today.
  Genotype/donor on the UMAP works as a stand-in
  ([`color=patient_geno_short`](/browse/S25/V6A/plot/scatter?x=X_umap&color=patient_geno_short)).
  **S1C** DEG-count bars — derived. **S1D/E** correlation heatmaps — derived.
  **S1F** HLA-DQB1 donor/strain effect — **reproducible** as expression by group:
  [`violin HLA-DQB1 by Strain`](/browse/S25/V6A/plot/violin?x=Strain&y=HLA-DQB1)
  · [`by donor line`](/browse/S25/V6A/plot/violin?x=patient_geno_short&y=HLA-DQB1).
- **S2** Female vs Male GSEA (GO/KEGG NES bars) — enrichment; no anthive geom.
  (The sex DE itself is Fig 1C's volcano.)
- **S3** module correlation heatmap + network — derived; no equivalent.
- **S4** WGCNA module Z-score per donor (control) — **module forests**, one per
  module, e.g.
  [`forest MEblack`](/browse/S25/V6W/plot/forest?gene=MEblack&study=Module_DE_interpretable_lmem_vs_grandmean&term=patient_geno_short_relevel)
  (swap any `ME*`). Same family as 2F.
- **S5** IHC / xenograft microscopy — imaging; no equivalent.
- **S6** ex-vivo T-cell activation (flow) — different assay/modality; no equivalent.
- **S7** donor-specific module induction under Aβ — **module forests** as in S4,
  but on the amyloid-response term (browse the
  [V6W module DE explorer](/browse/S25/V6W/de/Module_DE_interpretable_lmem_vs_grandmean)
  and pick the relevant term/contrast).
- **S8** village-vs-single-graft correlation bars — derived; no equivalent.
- **S9** magenta log2FC vs PRS scatter — donor-level (same as 5D); no equivalent.

---

## Remarks / limitations

- **What maps well:** anything that's *DE* (volcanoes, gene/module forests) or
  *a value on the UMAP* (gene expression, microtype scores, WGCNA module
  scores, genotype/donor labels) reproduces cleanly and is genuinely useful
  interactively — you can swap gene/module/contrast on the page. The paper's
  whole "module-on-UMAP" and "module-per-donor" story (Figs 2, 4, S4, S7) maps
  directly because the 13 `ME*` modules are first-class in `S25/V6W` and it
  shares the UMAP.
- **What doesn't map (by design):** imaging/IHC (S5), flow cytometry
  (3C–H, S6), GSEA/enrichment bars (3B, S2), derived correlation/Z-score
  heatmaps (2C/E, 4B, S3, S1D/E), power analyses (5A), and **donor-level
  aggregate scatters** (5D, S9) — anthive plots *cells*, not per-donor summary
  points. The flow panels have transcriptomic *analogs* (gene expression by
  genotype) but RNA ≠ surface protein.
- **No faceting.** The paper's small-multiple grids (per-donor UMAPs in 2A/4A,
  per-module forests in S4/S7) become *one interactive plot* where you pick the
  donor (via `subset=`) or module — not a static grid. That's the trade-off:
  less at-a-glance, more explorable.
- **Server bug to fix (flag to the API/`anthive4` side):** colouring a scatter
  by `Category` or `Status` returns HTTP 500
  (`'<' not supported between instances of 'str' and 'float'`) — those obs
  columns mix strings with NaN/floats and the colour sort chokes. Clean
  categoricals (`Strain`, `Village`, `patient_geno_short`, `Sex_donor`,
  `APOE_SNP`) work. This is what blocks a faithful S1B cell-class UMAP.
- **Curation gap:** `S25/V6A` has no `top_genes` curated in its `.md`, so the
  plot forms don't pre-suggest marker genes. Adding `top_genes` (e.g. the Fig 3
  huDAM/MHCII markers) + `top_num` (the microtype scores) would make these
  plots one-click from the dataset page rather than requiring you to type the
  field name.
- **DE term zoo:** the GLMM study exposes 14 terms incl. `*_sexreg`,
  `*_huhu_specific`, `*_nlgf_specific` variants — useful for the donor-specific
  panels, browsable via the [DE explorer](/browse/S25/V6A/de/DE_interpretable_glmm_vs_grandmean).
