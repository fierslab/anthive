#set page(paper: "a4", margin: (top: 2cm, bottom: 2cm, left: 2cm, right: 2cm))
#set text(font: "DejaVu Sans", size: 9.5pt)
#set heading(numbering: "1.1.")
#show heading.where(level: 1): set text(size: 14pt, weight: "bold")
#show heading.where(level: 2): set text(size: 11pt, weight: "bold")
#show heading.where(level: 3): set text(size: 10pt, weight: "bold")
#set table(stroke: 0.5pt + gray, inset: 3pt)
#show raw: set text(font: "DejaVu Sans Mono", size: 9pt)

#align(center)[
  #text(size: 18pt, weight: "bold")[starCAT MICROGLIA\_V1 — cross-validation]
  \
  #text(size: 11pt, fill: gray)[22 human-microglia datasets · generated 2026-05-25 19:35 UTC]
]

#v(8pt)

= Overview

This report compares starCAT `MICROGLIA_V1` annotations against author-defined
labels and state-scores that already exist in each dataset's obs table.
The catalog assigns each cell a soft mixture across 21 microglia / DC / macrophage
programs and a single argmax `Identity_Label` from 13 identity categories.

For each dataset we show:

+ *QA flags* — gene overlap with the catalog (out of 2,727), median per-cell
  residual (high = poor catalog fit), and the WholeCell-baseline / DC-soft-signal
  fractions documented in `ANNOTATE_CELLS.md` §7.5.
+ *Identity\_Label distribution* — the argmax assignment per cell.
+ *Cross-validation* — where the author's obs columns include state scores
  (`HM/DAM/IRM/HLA/CRM/RM`, `*_enriched1`, etc.) or cell-type / cluster labels,
  we compute mean author-score per starcat label or a confusion-matrix-style
  cross-tab.

Strong agreement on rows where starcat says "DAM" and author scores show high
DAM is the simplest sanity check that *both* annotations describe the same biology.

== Summary table

#text(size: 9pt)[

#table(
    columns: (auto, auto, auto, auto, auto, auto, auto),
    inset: 3pt,
    align: (left, left, right, right, right, right, left),
    stroke: 0.5pt + gray,
    table.header(
      [*Dataset*], [*Tech*], [*Cells*], [*Resid*], [*WC%*], [*DC%*], [*Dominant label*],
    ),
      [Schirmer2019/schirmer2019\_microglia], [#text(font: "DejaVu Sans Mono", size: 8pt)[snRNA-seq]], [#text(font: "DejaVu Sans Mono")[1,425]], [#text(font: "DejaVu Sans Mono")[1.700]], [#text(font: "DejaVu Sans Mono")[0.0%]], [#text(font: "DejaVu Sans Mono")[29.1%]], [#text(size: 8pt)[Homeostatic (60%)]],
      [Pan2024/Microglia], [#text(font: "DejaVu Sans Mono", size: 8pt)[10x\_chromium]], [#text(font: "DejaVu Sans Mono")[1,621]], [#text(font: "DejaVu Sans Mono")[1.872]], [#text(font: "DejaVu Sans Mono")[0.0%]], [#text(font: "DejaVu Sans Mono")[37.8%]], [#text(size: 8pt)[Homeostatic (44%)]],
      [Smajic2022/smajic2022\_microglia], [#text(font: "DejaVu Sans Mono", size: 8pt)[snRNA-seq]], [#text(font: "DejaVu Sans Mono")[3,903]], [#text(font: "DejaVu Sans Mono")[1.183]], [#text(font: "DejaVu Sans Mono")[0.0%]], [#text(font: "DejaVu Sans Mono")[42.9%]], [#text(size: 8pt)[Homeostatic (71%)]],
      [LuChen2025/101010sn.1.micro], [#text(font: "DejaVu Sans Mono", size: 8pt)[10x\_chromium]], [#text(font: "DejaVu Sans Mono")[6,033]], [#text(font: "DejaVu Sans Mono")[1.547]], [#text(font: "DejaVu Sans Mono")[0.4%]], [#text(font: "DejaVu Sans Mono")[57.7%]], [#text(size: 8pt)[Homeostatic (50%)]],
      [HouZielonka2023/LPS\_sema\_micro\_invitro], [#text(font: "DejaVu Sans Mono", size: 8pt)[10x\_chromium]], [#text(font: "DejaVu Sans Mono")[8,395]], [#text(font: "DejaVu Sans Mono")[2.885]], [#text(font: "DejaVu Sans Mono")[0.0%]], [#text(font: "DejaVu Sans Mono")[99.6%]], [#text(size: 8pt)[DC\_pDC (39%)]],
      [HouZielonka2023/noLPS\_sema\_micro\_invitro], [#text(font: "DejaVu Sans Mono", size: 8pt)[10x\_chromium]], [#text(font: "DejaVu Sans Mono")[8,611]], [#text(font: "DejaVu Sans Mono")[2.543]], [#text(font: "DejaVu Sans Mono")[0.8%]], [#text(font: "DejaVu Sans Mono")[99.7%]], [#text(size: 8pt)[Lipid\_LAM (54%)]],
      [Olah2020/olah2020\_microglia], [#text(font: "DejaVu Sans Mono", size: 8pt)[10x\_chromium]], [#text(font: "DejaVu Sans Mono")[16,245]], [#text(font: "DejaVu Sans Mono")[4.099]], [#text(font: "DejaVu Sans Mono")[11.8%]], [#text(font: "DejaVu Sans Mono")[56.3%]], [#text(size: 8pt)[HLA\_MHCII (34%)]],
      [AlbertiniZielonka2025/scrna\_lecMicro\_cleaned\_integrated\_sctransform], [#text(font: "DejaVu Sans Mono", size: 8pt)[10x\_chromium]], [#text(font: "DejaVu Sans Mono")[22,420]], [#text(font: "DejaVu Sans Mono")[1.972]], [#text(font: "DejaVu Sans Mono")[47.4% ⚠ likely whole-cell]], [#text(font: "DejaVu Sans Mono")[53.7%]], [#text(size: 8pt)[Homeostatic\_MERTK (47%)]],
      [Gabitto2024/Seattle\_MTG\_MG], [#text(font: "DejaVu Sans Mono", size: 8pt)[10x\_chromium]], [#text(font: "DejaVu Sans Mono")[40,000]], [#text(font: "DejaVu Sans Mono")[1.090]], [#text(font: "DejaVu Sans Mono")[0.8%]], [#text(font: "DejaVu Sans Mono")[64.7%]], [#text(size: 8pt)[Homeostatic (56%)]],
      [Simpson2025/module\_scored\_sorl1], [#text(font: "DejaVu Sans Mono", size: 8pt)[10x\_chromium]], [#text(font: "DejaVu Sans Mono")[42,270]], [#text(font: "DejaVu Sans Mono")[2.974]], [#text(font: "DejaVu Sans Mono")[18.9%]], [#text(font: "DejaVu Sans Mono")[44.7%]], [#text(size: 8pt)[Homeostatic\_MERTK (75%)]],
      [Simpson2025/seurat\_obj\_sorl1\_final], [#text(font: "DejaVu Sans Mono", size: 8pt)[10x\_chromium]], [#text(font: "DejaVu Sans Mono")[42,270]], [#text(font: "DejaVu Sans Mono")[1.663]], [#text(font: "DejaVu Sans Mono")[16.8%]], [#text(font: "DejaVu Sans Mono")[56.4%]], [#text(size: 8pt)[Homeostatic\_MERTK (77%)]],
      [Gabitto2024/Seattle\_PFC\_MG], [#text(font: "DejaVu Sans Mono", size: 8pt)[10x\_chromium]], [#text(font: "DejaVu Sans Mono")[42,486]], [#text(font: "DejaVu Sans Mono")[1.095]], [#text(font: "DejaVu Sans Mono")[0.7%]], [#text(font: "DejaVu Sans Mono")[67.1%]], [#text(size: 8pt)[Homeostatic (57%)]],
      [Davis2025/20240812\_sexmicroglia], [#text(font: "DejaVu Sans Mono", size: 8pt)[10x\_chromium]], [#text(font: "DejaVu Sans Mono")[43,552]], [#text(font: "DejaVu Sans Mono")[1.333]], [#text(font: "DejaVu Sans Mono")[25.0%]], [#text(font: "DejaVu Sans Mono")[49.9%]], [#text(size: 8pt)[Homeostatic\_MERTK (58%)]],
      [Davis2025/davis2025.20250619], [#text(font: "DejaVu Sans Mono", size: 8pt)[10x\_chromium]], [#text(font: "DejaVu Sans Mono")[43,552]], [#text(font: "DejaVu Sans Mono")[1.333]], [#text(font: "DejaVu Sans Mono")[25.0%]], [#text(font: "DejaVu Sans Mono")[49.9%]], [#text(size: 8pt)[Homeostatic\_MERTK (58%)]],
      [Gazestani2023/microglia], [#text(font: "DejaVu Sans Mono", size: 8pt)[10x\_chromium]], [#text(font: "DejaVu Sans Mono")[67,513]], [#text(font: "DejaVu Sans Mono")[1.226]], [#text(font: "DejaVu Sans Mono")[0.3%]], [#text(font: "DejaVu Sans Mono")[44.8%]], [#text(size: 8pt)[Homeostatic (59%)]],
      [Blackburn2026/seurat\_obj\_final], [#text(font: "DejaVu Sans Mono", size: 8pt)[10x\_chromium]], [#text(font: "DejaVu Sans Mono")[73,888]], [#text(font: "DejaVu Sans Mono")[2.408]], [#text(font: "DejaVu Sans Mono")[17.8%]], [#text(font: "DejaVu Sans Mono")[61.1%]], [#text(size: 8pt)[Homeostatic\_MERTK (74%)]],
      [Blackburn2026/trisomy\_seurat\_obj\_w\_umap], [#text(font: "DejaVu Sans Mono", size: 8pt)[10x\_chromium]], [#text(font: "DejaVu Sans Mono")[73,888]], [#text(font: "DejaVu Sans Mono")[3.256]], [#text(font: "DejaVu Sans Mono")[21.2%]], [#text(font: "DejaVu Sans Mono")[54.0%]], [#text(size: 8pt)[Homeostatic\_MERTK (71%)]],
      [Borrie2025/LOF.sc.teset.counts], [#text(font: "DejaVu Sans Mono", size: 8pt)[10x\_chromium]], [#text(font: "DejaVu Sans Mono")[77,684]], [#text(font: "DejaVu Sans Mono")[2.353]], [#text(font: "DejaVu Sans Mono")[39.8% ⚠ likely whole-cell]], [#text(font: "DejaVu Sans Mono")[54.8%]], [#text(size: 8pt)[Homeostatic\_MERTK (53%)]],
      [GreenFujita2024/microglia], [#text(font: "DejaVu Sans Mono", size: 8pt)[10x\_chromium]], [#text(font: "DejaVu Sans Mono")[86,612]], [#text(font: "DejaVu Sans Mono")[1.229]], [#text(font: "DejaVu Sans Mono")[0.0%]], [#text(font: "DejaVu Sans Mono")[63.4%]], [#text(size: 8pt)[Homeostatic (63%)]],
      [Blackburn2026/seurat\_obj\_trisomy\_initial], [#text(font: "DejaVu Sans Mono", size: 8pt)[10x\_chromium]], [#text(font: "DejaVu Sans Mono")[88,582]], [#text(font: "DejaVu Sans Mono")[2.647]], [#text(font: "DejaVu Sans Mono")[14.2%]], [#text(font: "DejaVu Sans Mono")[72.1%]], [#text(size: 8pt)[Homeostatic\_MERTK (68%)]],
      [Mathys2024/multiregion\_microglia], [#text(font: "DejaVu Sans Mono", size: 8pt)[10x\_chromium]], [#text(font: "DejaVu Sans Mono")[92,800]], [#text(font: "DejaVu Sans Mono")[1.280]], [#text(font: "DejaVu Sans Mono")[0.0%]], [#text(font: "DejaVu Sans Mono")[50.4%]], [#text(size: 8pt)[Homeostatic (65%)]],
      [Mancuso\_2024/Mancuso\_2023], [#text(font: "DejaVu Sans Mono", size: 8pt)[10x\_chromium]], [#text(font: "DejaVu Sans Mono")[127,755]], [#text(font: "DejaVu Sans Mono")[1.954]], [#text(font: "DejaVu Sans Mono")[56.0% ⚠ likely whole-cell]], [#text(font: "DejaVu Sans Mono")[60.8%]], [#text(size: 8pt)[Homeostatic\_MERTK (47%)]],
)

]

#pagebreak()
= Per-dataset cross-validation

== Schirmer2019 / schirmer2019\_microglia

_Tech: snRNA-seq, n\_cells: 1,425_
#text(fill: orange)[⚠ 43 pending columns not merged]

#grid(columns: (1fr, 1fr), gutter: 12pt,
[
=== QA flags
#table(
    columns: (auto, auto),
    inset: 4pt,
    align: (left, left),
    stroke: 0.5pt + gray,
      [Cells], [#text(font: "DejaVu Sans Mono")[1,425]],
      [Gene overlap], [#text(font: "DejaVu Sans Mono")[2,727 / 2,727]],
      [Median residual], [#text(font: "DejaVu Sans Mono")[1.700]],
      [WholeCell% (high)], [#text(font: "DejaVu Sans Mono")[0.0%]],
      [DC% (\> 0.05)], [#text(font: "DejaVu Sans Mono")[29.1% ⚠ DC contamination]],
)

],
[
=== Identity\_Label distribution
#table(
    columns: (auto, auto, auto),
    inset: 3pt,
    align: (left, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*%*]),
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[851]], [#text(font: "DejaVu Sans Mono")[59.7%]],
      [DAM], [#text(font: "DejaVu Sans Mono")[241]], [#text(font: "DejaVu Sans Mono")[16.9%]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[146]], [#text(font: "DejaVu Sans Mono")[10.2%]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[72]], [#text(font: "DejaVu Sans Mono")[5.1%]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[49]], [#text(font: "DejaVu Sans Mono")[3.4%]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[19]], [#text(font: "DejaVu Sans Mono")[1.3%]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[18]], [#text(font: "DejaVu Sans Mono")[1.3%]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono")[18]], [#text(font: "DejaVu Sans Mono")[1.3%]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[5]], [#text(font: "DejaVu Sans Mono")[0.4%]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[0.2%]],
      [_other (2)_], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[0.2%]],
)

]
)

_No comparable author state-scores or label columns detected in this dataset; starcat annotation stands on its own._

#pagebreak()

== Pan2024 / Microglia

_Tech: 10x\_chromium, n\_cells: 1,621_
#text(fill: orange)[⚠ 43 pending columns not merged]

#grid(columns: (1fr, 1fr), gutter: 12pt,
[
=== QA flags
#table(
    columns: (auto, auto),
    inset: 4pt,
    align: (left, left),
    stroke: 0.5pt + gray,
      [Cells], [#text(font: "DejaVu Sans Mono")[1,621]],
      [Gene overlap], [#text(font: "DejaVu Sans Mono")[2,727 / 2,727]],
      [Median residual], [#text(font: "DejaVu Sans Mono")[1.872]],
      [WholeCell% (high)], [#text(font: "DejaVu Sans Mono")[0.0%]],
      [DC% (\> 0.05)], [#text(font: "DejaVu Sans Mono")[37.8% ⚠ DC contamination]],
)

],
[
=== Identity\_Label distribution
#table(
    columns: (auto, auto, auto),
    inset: 3pt,
    align: (left, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*%*]),
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[711]], [#text(font: "DejaVu Sans Mono")[43.9%]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[256]], [#text(font: "DejaVu Sans Mono")[15.8%]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[220]], [#text(font: "DejaVu Sans Mono")[13.6%]],
      [DAM], [#text(font: "DejaVu Sans Mono")[158]], [#text(font: "DejaVu Sans Mono")[9.7%]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[145]], [#text(font: "DejaVu Sans Mono")[8.9%]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono")[61]], [#text(font: "DejaVu Sans Mono")[3.8%]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[27]], [#text(font: "DejaVu Sans Mono")[1.7%]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[16]], [#text(font: "DejaVu Sans Mono")[1.0%]],
      [IRM], [#text(font: "DejaVu Sans Mono")[8]], [#text(font: "DejaVu Sans Mono")[0.5%]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[7]], [#text(font: "DejaVu Sans Mono")[0.4%]],
      [_other (2)_], [#text(font: "DejaVu Sans Mono")[12]], [#text(font: "DejaVu Sans Mono")[0.7%]],
)

]
)

_No comparable author state-scores or label columns detected in this dataset; starcat annotation stands on its own._

#pagebreak()

== Smajic2022 / smajic2022\_microglia

_Tech: snRNA-seq, n\_cells: 3,903_
#text(fill: orange)[⚠ 43 pending columns not merged]

#grid(columns: (1fr, 1fr), gutter: 12pt,
[
=== QA flags
#table(
    columns: (auto, auto),
    inset: 4pt,
    align: (left, left),
    stroke: 0.5pt + gray,
      [Cells], [#text(font: "DejaVu Sans Mono")[3,903]],
      [Gene overlap], [#text(font: "DejaVu Sans Mono")[2,727 / 2,727]],
      [Median residual], [#text(font: "DejaVu Sans Mono")[1.183]],
      [WholeCell% (high)], [#text(font: "DejaVu Sans Mono")[0.0%]],
      [DC% (\> 0.05)], [#text(font: "DejaVu Sans Mono")[42.9% ⚠ DC contamination]],
)

],
[
=== Identity\_Label distribution
#table(
    columns: (auto, auto, auto),
    inset: 3pt,
    align: (left, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*%*]),
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[2,767]], [#text(font: "DejaVu Sans Mono")[70.9%]],
      [DAM], [#text(font: "DejaVu Sans Mono")[538]], [#text(font: "DejaVu Sans Mono")[13.8%]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[149]], [#text(font: "DejaVu Sans Mono")[3.8%]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[113]], [#text(font: "DejaVu Sans Mono")[2.9%]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[84]], [#text(font: "DejaVu Sans Mono")[2.2%]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[60]], [#text(font: "DejaVu Sans Mono")[1.5%]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[50]], [#text(font: "DejaVu Sans Mono")[1.3%]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[37]], [#text(font: "DejaVu Sans Mono")[0.9%]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono")[33]], [#text(font: "DejaVu Sans Mono")[0.8%]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[32]], [#text(font: "DejaVu Sans Mono")[0.8%]],
      [_other (3)_], [#text(font: "DejaVu Sans Mono")[40]], [#text(font: "DejaVu Sans Mono")[1.0%]],
)

]
)

_No comparable author state-scores or label columns detected in this dataset; starcat annotation stands on its own._

#pagebreak()

== LuChen2025 / 101010sn.1.micro

_Tech: 10x\_chromium, n\_cells: 6,033_

#grid(columns: (1fr, 1fr), gutter: 12pt,
[
=== QA flags
#table(
    columns: (auto, auto),
    inset: 4pt,
    align: (left, left),
    stroke: 0.5pt + gray,
      [Cells], [#text(font: "DejaVu Sans Mono")[6,033]],
      [Gene overlap], [#text(font: "DejaVu Sans Mono")[2,727 / 2,727]],
      [Median residual], [#text(font: "DejaVu Sans Mono")[1.547]],
      [WholeCell% (high)], [#text(font: "DejaVu Sans Mono")[0.4%]],
      [DC% (\> 0.05)], [#text(font: "DejaVu Sans Mono")[57.7% ⚠ DC contamination]],
)

],
[
=== Identity\_Label distribution
#table(
    columns: (auto, auto, auto),
    inset: 3pt,
    align: (left, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*%*]),
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[3,002]], [#text(font: "DejaVu Sans Mono")[49.8%]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[1,179]], [#text(font: "DejaVu Sans Mono")[19.5%]],
      [DAM], [#text(font: "DejaVu Sans Mono")[564]], [#text(font: "DejaVu Sans Mono")[9.3%]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[418]], [#text(font: "DejaVu Sans Mono")[6.9%]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[299]], [#text(font: "DejaVu Sans Mono")[5.0%]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[178]], [#text(font: "DejaVu Sans Mono")[3.0%]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[89]], [#text(font: "DejaVu Sans Mono")[1.5%]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[81]], [#text(font: "DejaVu Sans Mono")[1.3%]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono")[78]], [#text(font: "DejaVu Sans Mono")[1.3%]],
      [DC\_cDC1], [#text(font: "DejaVu Sans Mono")[55]], [#text(font: "DejaVu Sans Mono")[0.9%]],
      [_other (3)_], [#text(font: "DejaVu Sans Mono")[90]], [#text(font: "DejaVu Sans Mono")[1.5%]],
)

]
)

=== Author label cross-tab
_Cell counts: rows = starcat Identity\_Label, columns = author's `first\_subtypes`. Concentration along rows = agreement._

#table(
    columns: (auto, auto, auto, auto, auto, auto, auto, auto),
    inset: 3pt,
    align: (left, right, right, right, right, right, right, right),
    stroke: 0.5pt + gray,
    table.header([*starcat label*], [*micro\_0*], [*micro\_1*], [*micro\_2*], [*micro\_3*], [*micro\_4*], [*micro\_5*], [*micro\_6*]),
      [Border\_CAM], [#text(font: "DejaVu Sans Mono", weight: "bold")[126]], [#text(font: "DejaVu Sans Mono")[22]], [#text(font: "DejaVu Sans Mono")[15]], [#text(font: "DejaVu Sans Mono")[60]], [#text(font: "DejaVu Sans Mono")[29]], [#text(font: "DejaVu Sans Mono")[32]], [#text(font: "DejaVu Sans Mono")[15]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[31]], [#text(font: "DejaVu Sans Mono")[9]], [#text(font: "DejaVu Sans Mono")[8]], [#text(font: "DejaVu Sans Mono")[14]], [#text(font: "DejaVu Sans Mono")[20]], [#text(font: "DejaVu Sans Mono")[4]], [#text(font: "DejaVu Sans Mono")[3]],
      [DAM], [#text(font: "DejaVu Sans Mono", weight: "bold")[208]], [#text(font: "DejaVu Sans Mono")[29]], [#text(font: "DejaVu Sans Mono", weight: "bold")[117]], [#text(font: "DejaVu Sans Mono")[50]], [#text(font: "DejaVu Sans Mono")[88]], [#text(font: "DejaVu Sans Mono")[45]], [#text(font: "DejaVu Sans Mono")[27]],
      [DC\_Mature], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[3]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[1]],
      [DC\_cDC1], [#text(font: "DejaVu Sans Mono")[10]], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[8]], [#text(font: "DejaVu Sans Mono")[13]], [#text(font: "DejaVu Sans Mono")[13]], [#text(font: "DejaVu Sans Mono")[5]], [#text(font: "DejaVu Sans Mono")[3]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[12]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[6]], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono")[5]], [#text(font: "DejaVu Sans Mono")[6]], [#text(font: "DejaVu Sans Mono")[3]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[58]], [#text(font: "DejaVu Sans Mono")[22]], [#text(font: "DejaVu Sans Mono")[17]], [#text(font: "DejaVu Sans Mono")[40]], [#text(font: "DejaVu Sans Mono")[20]], [#text(font: "DejaVu Sans Mono")[18]], [#text(font: "DejaVu Sans Mono")[3]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[24]], [#text(font: "DejaVu Sans Mono")[6]], [#text(font: "DejaVu Sans Mono")[21]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[20]], [#text(font: "DejaVu Sans Mono")[9]], [#text(font: "DejaVu Sans Mono")[1]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono", weight: "bold")[404]], [#text(font: "DejaVu Sans Mono", weight: "bold")[678]], [#text(font: "DejaVu Sans Mono", weight: "bold")[748]], [#text(font: "DejaVu Sans Mono", weight: "bold")[638]], [#text(font: "DejaVu Sans Mono", weight: "bold")[314]], [#text(font: "DejaVu Sans Mono", weight: "bold")[207]], [#text(font: "DejaVu Sans Mono")[13]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono", weight: "bold")[286]], [#text(font: "DejaVu Sans Mono", weight: "bold")[440]], [#text(font: "DejaVu Sans Mono")[71]], [#text(font: "DejaVu Sans Mono", weight: "bold")[128]], [#text(font: "DejaVu Sans Mono", weight: "bold")[145]], [#text(font: "DejaVu Sans Mono", weight: "bold")[102]], [#text(font: "DejaVu Sans Mono")[7]],
      [IRM], [#text(font: "DejaVu Sans Mono")[10]], [#text(font: "DejaVu Sans Mono")[4]], [#text(font: "DejaVu Sans Mono")[21]], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono")[8]], [#text(font: "DejaVu Sans Mono")[5]], [#text(font: "DejaVu Sans Mono")[2]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono", weight: "bold")[173]], [#text(font: "DejaVu Sans Mono")[12]], [#text(font: "DejaVu Sans Mono", weight: "bold")[113]], [#text(font: "DejaVu Sans Mono")[36]], [#text(font: "DejaVu Sans Mono")[29]], [#text(font: "DejaVu Sans Mono")[44]], [#text(font: "DejaVu Sans Mono")[11]],
)
#text(size: 7pt, fill: gray)[Author column: `first\_subtypes`]


#pagebreak()

== HouZielonka2023 / LPS\_sema\_micro\_invitro

_Tech: 10x\_chromium, n\_cells: 8,395_
#text(fill: orange)[⚠ 43 pending columns not merged]

#grid(columns: (1fr, 1fr), gutter: 12pt,
[
=== QA flags
#table(
    columns: (auto, auto),
    inset: 4pt,
    align: (left, left),
    stroke: 0.5pt + gray,
      [Cells], [#text(font: "DejaVu Sans Mono")[8,395]],
      [Gene overlap], [#text(font: "DejaVu Sans Mono")[2,697 / 2,727]],
      [Median residual], [#text(font: "DejaVu Sans Mono")[2.885]],
      [WholeCell% (high)], [#text(font: "DejaVu Sans Mono")[0.0%]],
      [DC% (\> 0.05)], [#text(font: "DejaVu Sans Mono")[99.6% ⚠ DC contamination]],
)

],
[
=== Identity\_Label distribution
#table(
    columns: (auto, auto, auto),
    inset: 3pt,
    align: (left, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*%*]),
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[3,255]], [#text(font: "DejaVu Sans Mono")[38.8%]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[3,113]], [#text(font: "DejaVu Sans Mono")[37.1%]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[1,149]], [#text(font: "DejaVu Sans Mono")[13.7%]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono")[327]], [#text(font: "DejaVu Sans Mono")[3.9%]],
      [DAM], [#text(font: "DejaVu Sans Mono")[192]], [#text(font: "DejaVu Sans Mono")[2.3%]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[154]], [#text(font: "DejaVu Sans Mono")[1.8%]],
      [IRM], [#text(font: "DejaVu Sans Mono")[100]], [#text(font: "DejaVu Sans Mono")[1.2%]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[34]], [#text(font: "DejaVu Sans Mono")[0.4%]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[23]], [#text(font: "DejaVu Sans Mono")[0.3%]],
      [DC\_cDC1], [#text(font: "DejaVu Sans Mono")[18]], [#text(font: "DejaVu Sans Mono")[0.2%]],
      [_other (2)_], [#text(font: "DejaVu Sans Mono")[30]], [#text(font: "DejaVu Sans Mono")[0.4%]],
)

]
)

=== Author state scores by starcat label
_For each starcat-assigned Identity\_Label, mean of the author's matching state-score columns. Diagonals (e.g. row=DAM, col=DAM) should be high._

#table(
    columns: (auto, auto, auto),
    inset: 3pt,
    align: (left, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*HM*]),
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[3,255]], [#text(font: "DejaVu Sans Mono")[0.22]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[3,113]], [#text(font: "DejaVu Sans Mono")[0.19]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[1,149]], [#text(font: "DejaVu Sans Mono")[0.24]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono")[327]], [#text(font: "DejaVu Sans Mono")[0.22]],
      [DAM], [#text(font: "DejaVu Sans Mono")[192]], [#text(font: "DejaVu Sans Mono")[0.27]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[154]], [#text(font: "DejaVu Sans Mono")[0.31]],
      [IRM], [#text(font: "DejaVu Sans Mono")[100]], [#text(font: "DejaVu Sans Mono")[0.23]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[34]], [#text(font: "DejaVu Sans Mono")[0.28]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[23]], [#text(font: "DejaVu Sans Mono")[0.31]],
      [DC\_cDC1], [#text(font: "DejaVu Sans Mono")[18]], [#text(font: "DejaVu Sans Mono")[0.21]],
)
#text(size: 7pt, fill: gray)[Author column mapping: HM→`HM\_enriched1`]


=== Author label cross-tab
_Cell counts: rows = starcat Identity\_Label, columns = author's `cluster`. Concentration along rows = agreement._

#table(
    columns: (auto, auto, auto, auto),
    inset: 3pt,
    align: (left, right, right, right),
    stroke: 0.5pt + gray,
    table.header([*starcat label*], [*0*], [*1*], [*2*]),
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[14]], [#text(font: "DejaVu Sans Mono", weight: "bold")[136]], [#text(font: "DejaVu Sans Mono")[4]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono")[13]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DAM], [#text(font: "DejaVu Sans Mono")[21]], [#text(font: "DejaVu Sans Mono", weight: "bold")[170]], [#text(font: "DejaVu Sans Mono")[1]],
      [DC\_cDC1], [#text(font: "DejaVu Sans Mono")[9]], [#text(font: "DejaVu Sans Mono")[9]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono", weight: "bold")[268]], [#text(font: "DejaVu Sans Mono", weight: "bold")[872]], [#text(font: "DejaVu Sans Mono")[9]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,216]], [#text(font: "DejaVu Sans Mono", weight: "bold")[2,025]], [#text(font: "DejaVu Sans Mono")[14]],
      [HLA\_MHCII], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[15]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[28]], [#text(font: "DejaVu Sans Mono")[6]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[5]], [#text(font: "DejaVu Sans Mono")[18]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [IRM], [#text(font: "DejaVu Sans Mono")[4]], [#text(font: "DejaVu Sans Mono")[11]], [#text(font: "DejaVu Sans Mono")[85]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono", weight: "bold")[2,651]], [#text(font: "DejaVu Sans Mono", weight: "bold")[448]], [#text(font: "DejaVu Sans Mono")[14]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono", weight: "bold")[106]], [#text(font: "DejaVu Sans Mono", weight: "bold")[221]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
)
#text(size: 7pt, fill: gray)[Author column: `cluster`]


#pagebreak()

== HouZielonka2023 / noLPS\_sema\_micro\_invitro

_Tech: 10x\_chromium, n\_cells: 8,611_
#text(fill: orange)[⚠ 43 pending columns not merged]

#grid(columns: (1fr, 1fr), gutter: 12pt,
[
=== QA flags
#table(
    columns: (auto, auto),
    inset: 4pt,
    align: (left, left),
    stroke: 0.5pt + gray,
      [Cells], [#text(font: "DejaVu Sans Mono")[8,611]],
      [Gene overlap], [#text(font: "DejaVu Sans Mono")[2,697 / 2,727]],
      [Median residual], [#text(font: "DejaVu Sans Mono")[2.543]],
      [WholeCell% (high)], [#text(font: "DejaVu Sans Mono")[0.8%]],
      [DC% (\> 0.05)], [#text(font: "DejaVu Sans Mono")[99.7% ⚠ DC contamination]],
)

],
[
=== Identity\_Label distribution
#table(
    columns: (auto, auto, auto),
    inset: 3pt,
    align: (left, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*%*]),
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[4,638]], [#text(font: "DejaVu Sans Mono")[53.9%]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[1,372]], [#text(font: "DejaVu Sans Mono")[15.9%]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[1,256]], [#text(font: "DejaVu Sans Mono")[14.6%]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[414]], [#text(font: "DejaVu Sans Mono")[4.8%]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[301]], [#text(font: "DejaVu Sans Mono")[3.5%]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[251]], [#text(font: "DejaVu Sans Mono")[2.9%]],
      [DAM], [#text(font: "DejaVu Sans Mono")[125]], [#text(font: "DejaVu Sans Mono")[1.5%]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono")[117]], [#text(font: "DejaVu Sans Mono")[1.4%]],
      [IRM], [#text(font: "DejaVu Sans Mono")[54]], [#text(font: "DejaVu Sans Mono")[0.6%]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[54]], [#text(font: "DejaVu Sans Mono")[0.6%]],
      [_other (3)_], [#text(font: "DejaVu Sans Mono")[29]], [#text(font: "DejaVu Sans Mono")[0.3%]],
)

]
)

=== Author state scores by starcat label
_For each starcat-assigned Identity\_Label, mean of the author's matching state-score columns. Diagonals (e.g. row=DAM, col=DAM) should be high._

#table(
    columns: (auto, auto, auto),
    inset: 3pt,
    align: (left, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*HM*]),
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[4,638]], [#text(font: "DejaVu Sans Mono")[0.27]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[1,372]], [#text(font: "DejaVu Sans Mono")[0.35]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[1,256]], [#text(font: "DejaVu Sans Mono")[0.32]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[414]], [#text(font: "DejaVu Sans Mono")[0.41]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[301]], [#text(font: "DejaVu Sans Mono")[0.43]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[251]], [#text(font: "DejaVu Sans Mono")[0.38]],
      [DAM], [#text(font: "DejaVu Sans Mono")[125]], [#text(font: "DejaVu Sans Mono")[0.33]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono")[117]], [#text(font: "DejaVu Sans Mono")[0.26]],
      [IRM], [#text(font: "DejaVu Sans Mono")[54]], [#text(font: "DejaVu Sans Mono")[0.34]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[54]], [#text(font: "DejaVu Sans Mono")[0.32]],
)
#text(size: 7pt, fill: gray)[Author column mapping: HM→`HM\_enriched1`]


=== Author label cross-tab
_Cell counts: rows = starcat Identity\_Label, columns = author's `cluster`. Concentration along rows = agreement._

#table(
    columns: (auto, auto, auto, auto),
    inset: 3pt,
    align: (left, right, right, right),
    stroke: 0.5pt + gray,
    table.header([*starcat label*], [*0*], [*1*], [*2*]),
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[32]], [#text(font: "DejaVu Sans Mono", weight: "bold")[382]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono")[7]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DAM], [#text(font: "DejaVu Sans Mono")[21]], [#text(font: "DejaVu Sans Mono", weight: "bold")[104]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_Mature], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_cDC1], [#text(font: "DejaVu Sans Mono")[5]], [#text(font: "DejaVu Sans Mono")[14]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono", weight: "bold")[377]], [#text(font: "DejaVu Sans Mono", weight: "bold")[994]], [#text(font: "DejaVu Sans Mono")[1]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono", weight: "bold")[509]], [#text(font: "DejaVu Sans Mono", weight: "bold")[747]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[15]], [#text(font: "DejaVu Sans Mono")[38]], [#text(font: "DejaVu Sans Mono")[1]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono", weight: "bold")[196]], [#text(font: "DejaVu Sans Mono")[55]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono", weight: "bold")[107]], [#text(font: "DejaVu Sans Mono", weight: "bold")[194]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [IRM], [#text(font: "DejaVu Sans Mono")[10]], [#text(font: "DejaVu Sans Mono")[9]], [#text(font: "DejaVu Sans Mono")[35]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono", weight: "bold")[3,829]], [#text(font: "DejaVu Sans Mono", weight: "bold")[802]], [#text(font: "DejaVu Sans Mono")[7]],
)
#text(size: 7pt, fill: gray)[Author column: `cluster`]


#pagebreak()

== Olah2020 / olah2020\_microglia

_Tech: 10x\_chromium, n\_cells: 16,245_
#text(fill: orange)[⚠ 43 pending columns not merged]

#grid(columns: (1fr, 1fr), gutter: 12pt,
[
=== QA flags
#table(
    columns: (auto, auto),
    inset: 4pt,
    align: (left, left),
    stroke: 0.5pt + gray,
      [Cells], [#text(font: "DejaVu Sans Mono")[16,245]],
      [Gene overlap], [#text(font: "DejaVu Sans Mono")[2,723 / 2,727]],
      [Median residual], [#text(font: "DejaVu Sans Mono")[4.099]],
      [WholeCell% (high)], [#text(font: "DejaVu Sans Mono")[11.8%]],
      [DC% (\> 0.05)], [#text(font: "DejaVu Sans Mono")[56.3% ⚠ DC contamination]],
)

],
[
=== Identity\_Label distribution
#table(
    columns: (auto, auto, auto),
    inset: 3pt,
    align: (left, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*%*]),
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[5,450]], [#text(font: "DejaVu Sans Mono")[33.5%]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[3,698]], [#text(font: "DejaVu Sans Mono")[22.8%]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[1,789]], [#text(font: "DejaVu Sans Mono")[11.0%]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[1,005]], [#text(font: "DejaVu Sans Mono")[6.2%]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[997]], [#text(font: "DejaVu Sans Mono")[6.1%]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[920]], [#text(font: "DejaVu Sans Mono")[5.7%]],
      [IRM], [#text(font: "DejaVu Sans Mono")[710]], [#text(font: "DejaVu Sans Mono")[4.4%]],
      [DAM], [#text(font: "DejaVu Sans Mono")[473]], [#text(font: "DejaVu Sans Mono")[2.9%]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[438]], [#text(font: "DejaVu Sans Mono")[2.7%]],
      [DC\_cDC1], [#text(font: "DejaVu Sans Mono")[320]], [#text(font: "DejaVu Sans Mono")[2.0%]],
      [_other (3)_], [#text(font: "DejaVu Sans Mono")[445]], [#text(font: "DejaVu Sans Mono")[2.7%]],
)

]
)

=== Author label cross-tab
_Cell counts: rows = starcat Identity\_Label, columns = author's `cell\_type`. Concentration along rows = agreement._

#table(
    columns: (auto, auto, auto, auto, auto, auto, auto),
    inset: 3pt,
    align: (left, right, right, right, right, right, right),
    stroke: 0.5pt + gray,
    table.header([*starcat label*], [*microglial cell*], [*T cell*], [*neural cell*], [*monocyte*], [*erythrocyte*], [*B cell*]),
      [Border\_CAM], [#text(font: "DejaVu Sans Mono", weight: "bold")[427]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[10]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [Cycling], [#text(font: "DejaVu Sans Mono", weight: "bold")[167]], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DAM], [#text(font: "DejaVu Sans Mono", weight: "bold")[473]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_Mature], [#text(font: "DejaVu Sans Mono")[56]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_cDC1], [#text(font: "DejaVu Sans Mono", weight: "bold")[313]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[5]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono", weight: "bold")[994]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono", weight: "bold")[898]], [#text(font: "DejaVu Sans Mono")[8]], [#text(font: "DejaVu Sans Mono")[6]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[8]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono", weight: "bold")[5,449]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono", weight: "bold")[3,683]], [#text(font: "DejaVu Sans Mono")[9]], [#text(font: "DejaVu Sans Mono")[2]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[4]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,002]], [#text(font: "DejaVu Sans Mono")[3]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [IRM], [#text(font: "DejaVu Sans Mono", weight: "bold")[708]], [#text(font: "DejaVu Sans Mono")[2]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,784]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[4]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
)
#text(size: 7pt, fill: gray)[Author column: `cell\_type`]


#pagebreak()

== AlbertiniZielonka2025 / scrna\_lecMicro\_cleaned\_integrated\_sctransform

_Tech: 10x\_chromium, n\_cells: 22,420_
#text(fill: orange)[⚠ 43 pending columns not merged]

#grid(columns: (1fr, 1fr), gutter: 12pt,
[
=== QA flags
#table(
    columns: (auto, auto),
    inset: 4pt,
    align: (left, left),
    stroke: 0.5pt + gray,
      [Cells], [#text(font: "DejaVu Sans Mono")[22,420]],
      [Gene overlap], [#text(font: "DejaVu Sans Mono")[2,041 / 2,727]],
      [Median residual], [#text(font: "DejaVu Sans Mono")[1.972]],
      [WholeCell% (high)], [#text(font: "DejaVu Sans Mono")[47.4% ⚠ likely whole-cell]],
      [DC% (\> 0.05)], [#text(font: "DejaVu Sans Mono")[53.7% ⚠ DC contamination]],
)

],
[
=== Identity\_Label distribution
#table(
    columns: (auto, auto, auto),
    inset: 3pt,
    align: (left, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*%*]),
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[10,549]], [#text(font: "DejaVu Sans Mono")[47.1%]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[3,231]], [#text(font: "DejaVu Sans Mono")[14.4%]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[2,098]], [#text(font: "DejaVu Sans Mono")[9.4%]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[1,725]], [#text(font: "DejaVu Sans Mono")[7.7%]],
      [IRM], [#text(font: "DejaVu Sans Mono")[1,602]], [#text(font: "DejaVu Sans Mono")[7.1%]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[1,043]], [#text(font: "DejaVu Sans Mono")[4.7%]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[758]], [#text(font: "DejaVu Sans Mono")[3.4%]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[624]], [#text(font: "DejaVu Sans Mono")[2.8%]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[241]], [#text(font: "DejaVu Sans Mono")[1.1%]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono")[203]], [#text(font: "DejaVu Sans Mono")[0.9%]],
      [_other (3)_], [#text(font: "DejaVu Sans Mono")[346]], [#text(font: "DejaVu Sans Mono")[1.5%]],
)

]
)

=== Author state scores by starcat label
_For each starcat-assigned Identity\_Label, mean of the author's matching state-score columns. Diagonals (e.g. row=DAM, col=DAM) should be high._

#table(
    columns: (auto, auto, auto, auto, auto, auto, auto, auto),
    inset: 3pt,
    align: (left, right, right, right, right, right, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*HM*], [*DAM*], [*IRM*], [*HLA*], [*CRM*], [*RM*]),
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[10,549]], [#text(font: "DejaVu Sans Mono")[0.30]], [#text(font: "DejaVu Sans Mono")[0.18]], [#text(font: "DejaVu Sans Mono")[0.07]], [#text(font: "DejaVu Sans Mono")[0.49]], [#text(font: "DejaVu Sans Mono")[0.23]], [#text(font: "DejaVu Sans Mono")[0.50]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[3,231]], [#text(font: "DejaVu Sans Mono")[0.26]], [#text(font: "DejaVu Sans Mono")[0.22]], [#text(font: "DejaVu Sans Mono")[0.09]], [#text(font: "DejaVu Sans Mono")[0.54]], [#text(font: "DejaVu Sans Mono")[0.24]], [#text(font: "DejaVu Sans Mono")[0.53]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[2,098]], [#text(font: "DejaVu Sans Mono")[0.05]], [#text(font: "DejaVu Sans Mono")[0.42]], [#text(font: "DejaVu Sans Mono")[0.11]], [#text(font: "DejaVu Sans Mono")[0.72]], [#text(font: "DejaVu Sans Mono")[0.15]], [#text(font: "DejaVu Sans Mono")[0.68]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[1,725]], [#text(font: "DejaVu Sans Mono")[0.14]], [#text(font: "DejaVu Sans Mono")[0.28]], [#text(font: "DejaVu Sans Mono")[0.12]], [#text(font: "DejaVu Sans Mono")[0.75]], [#text(font: "DejaVu Sans Mono")[0.22]], [#text(font: "DejaVu Sans Mono")[0.71]],
      [IRM], [#text(font: "DejaVu Sans Mono")[1,602]], [#text(font: "DejaVu Sans Mono")[0.16]], [#text(font: "DejaVu Sans Mono")[0.25]], [#text(font: "DejaVu Sans Mono")[0.28]], [#text(font: "DejaVu Sans Mono")[0.61]], [#text(font: "DejaVu Sans Mono")[0.22]], [#text(font: "DejaVu Sans Mono")[0.60]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[1,043]], [#text(font: "DejaVu Sans Mono")[0.15]], [#text(font: "DejaVu Sans Mono")[0.26]], [#text(font: "DejaVu Sans Mono")[0.09]], [#text(font: "DejaVu Sans Mono")[0.70]], [#text(font: "DejaVu Sans Mono")[0.20]], [#text(font: "DejaVu Sans Mono")[0.74]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[758]], [#text(font: "DejaVu Sans Mono")[0.12]], [#text(font: "DejaVu Sans Mono")[0.26]], [#text(font: "DejaVu Sans Mono")[0.08]], [#text(font: "DejaVu Sans Mono")[0.76]], [#text(font: "DejaVu Sans Mono")[0.16]], [#text(font: "DejaVu Sans Mono")[0.83]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[624]], [#text(font: "DejaVu Sans Mono")[0.21]], [#text(font: "DejaVu Sans Mono")[0.23]], [#text(font: "DejaVu Sans Mono")[0.09]], [#text(font: "DejaVu Sans Mono")[0.61]], [#text(font: "DejaVu Sans Mono")[0.25]], [#text(font: "DejaVu Sans Mono")[0.62]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[241]], [#text(font: "DejaVu Sans Mono")[0.14]], [#text(font: "DejaVu Sans Mono")[0.22]], [#text(font: "DejaVu Sans Mono")[0.09]], [#text(font: "DejaVu Sans Mono")[0.58]], [#text(font: "DejaVu Sans Mono")[0.16]], [#text(font: "DejaVu Sans Mono")[0.64]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono")[203]], [#text(font: "DejaVu Sans Mono")[0.10]], [#text(font: "DejaVu Sans Mono")[0.27]], [#text(font: "DejaVu Sans Mono")[0.11]], [#text(font: "DejaVu Sans Mono")[0.76]], [#text(font: "DejaVu Sans Mono")[0.19]], [#text(font: "DejaVu Sans Mono")[0.80]],
)
#text(size: 7pt, fill: gray)[Author column mapping: HM→`HM\_enriched1`, DAM→`DAM\_enriched1`, IRM→`IRM\_enriched1`, HLA→`HLA\_enriched1`, CRM→`tCRM\_enriched1`, RM→`TRM\_enriched1`]


=== Author label cross-tab
_Cell counts: rows = starcat Identity\_Label, columns = author's `Cluster\_coarse`. Concentration along rows = agreement._

#table(
    columns: (auto, auto, auto, auto, auto, auto, auto, auto, auto, auto),
    inset: 3pt,
    align: (left, right, right, right, right, right, right, right, right, right),
    stroke: 0.5pt + gray,
    table.header([*starcat label*], [*HM*], [*RM*], [*CRM*], [*DAM*], [*tCRM*], [*IRM*], [*HLA*], [*C3-high*], [*Prolif*]),
      [Border\_CAM], [#text(font: "DejaVu Sans Mono", weight: "bold")[177]], [#text(font: "DejaVu Sans Mono", weight: "bold")[103]], [#text(font: "DejaVu Sans Mono", weight: "bold")[144]], [#text(font: "DejaVu Sans Mono")[19]], [#text(font: "DejaVu Sans Mono")[95]], [#text(font: "DejaVu Sans Mono")[29]], [#text(font: "DejaVu Sans Mono")[30]], [#text(font: "DejaVu Sans Mono")[24]], [#text(font: "DejaVu Sans Mono")[3]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[21]], [#text(font: "DejaVu Sans Mono")[15]], [#text(font: "DejaVu Sans Mono")[22]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[16]], [#text(font: "DejaVu Sans Mono")[6]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[4]], [#text(font: "DejaVu Sans Mono", weight: "bold")[156]],
      [DAM], [#text(font: "DejaVu Sans Mono")[41]], [#text(font: "DejaVu Sans Mono")[16]], [#text(font: "DejaVu Sans Mono")[41]], [#text(font: "DejaVu Sans Mono")[12]], [#text(font: "DejaVu Sans Mono")[23]], [#text(font: "DejaVu Sans Mono")[7]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[4]], [#text(font: "DejaVu Sans Mono")[1]],
      [DC\_Mature], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono")[7]], [#text(font: "DejaVu Sans Mono")[12]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[12]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_cDC1], [#text(font: "DejaVu Sans Mono")[23]], [#text(font: "DejaVu Sans Mono")[52]], [#text(font: "DejaVu Sans Mono")[30]], [#text(font: "DejaVu Sans Mono")[7]], [#text(font: "DejaVu Sans Mono")[14]], [#text(font: "DejaVu Sans Mono")[14]], [#text(font: "DejaVu Sans Mono")[23]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono", weight: "bold")[132]], [#text(font: "DejaVu Sans Mono", weight: "bold")[306]], [#text(font: "DejaVu Sans Mono")[81]], [#text(font: "DejaVu Sans Mono")[30]], [#text(font: "DejaVu Sans Mono")[53]], [#text(font: "DejaVu Sans Mono")[31]], [#text(font: "DejaVu Sans Mono", weight: "bold")[111]], [#text(font: "DejaVu Sans Mono")[12]], [#text(font: "DejaVu Sans Mono")[2]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono", weight: "bold")[171]], [#text(font: "DejaVu Sans Mono", weight: "bold")[305]], [#text(font: "DejaVu Sans Mono", weight: "bold")[194]], [#text(font: "DejaVu Sans Mono")[49]], [#text(font: "DejaVu Sans Mono", weight: "bold")[125]], [#text(font: "DejaVu Sans Mono")[71]], [#text(font: "DejaVu Sans Mono")[95]], [#text(font: "DejaVu Sans Mono")[23]], [#text(font: "DejaVu Sans Mono")[10]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono", weight: "bold")[216]], [#text(font: "DejaVu Sans Mono", weight: "bold")[268]], [#text(font: "DejaVu Sans Mono", weight: "bold")[296]], [#text(font: "DejaVu Sans Mono", weight: "bold")[413]], [#text(font: "DejaVu Sans Mono", weight: "bold")[202]], [#text(font: "DejaVu Sans Mono")[93]], [#text(font: "DejaVu Sans Mono", weight: "bold")[203]], [#text(font: "DejaVu Sans Mono")[26]], [#text(font: "DejaVu Sans Mono")[8]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,192]], [#text(font: "DejaVu Sans Mono", weight: "bold")[400]], [#text(font: "DejaVu Sans Mono", weight: "bold")[386]], [#text(font: "DejaVu Sans Mono", weight: "bold")[377]], [#text(font: "DejaVu Sans Mono", weight: "bold")[460]], [#text(font: "DejaVu Sans Mono", weight: "bold")[188]], [#text(font: "DejaVu Sans Mono")[57]], [#text(font: "DejaVu Sans Mono", weight: "bold")[146]], [#text(font: "DejaVu Sans Mono")[25]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono", weight: "bold")[4,918]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,582]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,255]], [#text(font: "DejaVu Sans Mono", weight: "bold")[296]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,240]], [#text(font: "DejaVu Sans Mono", weight: "bold")[569]], [#text(font: "DejaVu Sans Mono", weight: "bold")[172]], [#text(font: "DejaVu Sans Mono", weight: "bold")[460]], [#text(font: "DejaVu Sans Mono")[57]],
      [IRM], [#text(font: "DejaVu Sans Mono")[64]], [#text(font: "DejaVu Sans Mono")[81]], [#text(font: "DejaVu Sans Mono", weight: "bold")[192]], [#text(font: "DejaVu Sans Mono")[36]], [#text(font: "DejaVu Sans Mono")[79]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,095]], [#text(font: "DejaVu Sans Mono")[41]], [#text(font: "DejaVu Sans Mono")[13]], [#text(font: "DejaVu Sans Mono")[1]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[81]], [#text(font: "DejaVu Sans Mono", weight: "bold")[245]], [#text(font: "DejaVu Sans Mono", weight: "bold")[253]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,344]], [#text(font: "DejaVu Sans Mono")[85]], [#text(font: "DejaVu Sans Mono")[55]], [#text(font: "DejaVu Sans Mono")[8]], [#text(font: "DejaVu Sans Mono")[24]], [#text(font: "DejaVu Sans Mono")[3]],
)
#text(size: 7pt, fill: gray)[Author column: `Cluster\_coarse`]


#pagebreak()

== Gabitto2024 / Seattle\_MTG\_MG

_Tech: 10x\_chromium, n\_cells: 40,000_
#text(fill: orange)[⚠ 43 pending columns not merged]

#grid(columns: (1fr, 1fr), gutter: 12pt,
[
=== QA flags
#table(
    columns: (auto, auto),
    inset: 4pt,
    align: (left, left),
    stroke: 0.5pt + gray,
      [Cells], [#text(font: "DejaVu Sans Mono")[40,000]],
      [Gene overlap], [#text(font: "DejaVu Sans Mono")[2,727 / 2,727]],
      [Median residual], [#text(font: "DejaVu Sans Mono")[1.090]],
      [WholeCell% (high)], [#text(font: "DejaVu Sans Mono")[0.8%]],
      [DC% (\> 0.05)], [#text(font: "DejaVu Sans Mono")[64.7% ⚠ DC contamination]],
)

],
[
=== Identity\_Label distribution
#table(
    columns: (auto, auto, auto),
    inset: 3pt,
    align: (left, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*%*]),
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[22,236]], [#text(font: "DejaVu Sans Mono")[55.6%]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[6,244]], [#text(font: "DejaVu Sans Mono")[15.6%]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[2,937]], [#text(font: "DejaVu Sans Mono")[7.3%]],
      [DAM], [#text(font: "DejaVu Sans Mono")[2,670]], [#text(font: "DejaVu Sans Mono")[6.7%]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[1,812]], [#text(font: "DejaVu Sans Mono")[4.5%]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[1,208]], [#text(font: "DejaVu Sans Mono")[3.0%]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[1,017]], [#text(font: "DejaVu Sans Mono")[2.5%]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono")[666]], [#text(font: "DejaVu Sans Mono")[1.7%]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[348]], [#text(font: "DejaVu Sans Mono")[0.9%]],
      [IRM], [#text(font: "DejaVu Sans Mono")[346]], [#text(font: "DejaVu Sans Mono")[0.9%]],
      [_other (3)_], [#text(font: "DejaVu Sans Mono")[516]], [#text(font: "DejaVu Sans Mono")[1.3%]],
)

]
)

_No comparable author state-scores or label columns detected in this dataset; starcat annotation stands on its own._

#pagebreak()

== Simpson2025 / module\_scored\_sorl1

_Tech: 10x\_chromium, n\_cells: 42,270_
#text(fill: orange)[⚠ 43 pending columns not merged]

#grid(columns: (1fr, 1fr), gutter: 12pt,
[
=== QA flags
#table(
    columns: (auto, auto),
    inset: 4pt,
    align: (left, left),
    stroke: 0.5pt + gray,
      [Cells], [#text(font: "DejaVu Sans Mono")[42,270]],
      [Gene overlap], [#text(font: "DejaVu Sans Mono")[2,716 / 2,727]],
      [Median residual], [#text(font: "DejaVu Sans Mono")[2.974]],
      [WholeCell% (high)], [#text(font: "DejaVu Sans Mono")[18.9%]],
      [DC% (\> 0.05)], [#text(font: "DejaVu Sans Mono")[44.7% ⚠ DC contamination]],
)

],
[
=== Identity\_Label distribution
#table(
    columns: (auto, auto, auto),
    inset: 3pt,
    align: (left, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*%*]),
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[31,749]], [#text(font: "DejaVu Sans Mono")[75.1%]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[5,726]], [#text(font: "DejaVu Sans Mono")[13.5%]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[2,567]], [#text(font: "DejaVu Sans Mono")[6.1%]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[618]], [#text(font: "DejaVu Sans Mono")[1.5%]],
      [IRM], [#text(font: "DejaVu Sans Mono")[607]], [#text(font: "DejaVu Sans Mono")[1.4%]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[300]], [#text(font: "DejaVu Sans Mono")[0.7%]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[257]], [#text(font: "DejaVu Sans Mono")[0.6%]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[233]], [#text(font: "DejaVu Sans Mono")[0.6%]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono")[115]], [#text(font: "DejaVu Sans Mono")[0.3%]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[71]], [#text(font: "DejaVu Sans Mono")[0.2%]],
      [_other (3)_], [#text(font: "DejaVu Sans Mono")[27]], [#text(font: "DejaVu Sans Mono")[0.1%]],
)

]
)

=== Author state scores by starcat label
_For each starcat-assigned Identity\_Label, mean of the author's matching state-score columns. Diagonals (e.g. row=DAM, col=DAM) should be high._

#table(
    columns: (auto, auto, auto, auto, auto, auto, auto, auto, auto, auto, auto),
    inset: 3pt,
    align: (left, right, right, right, right, right, right, right, right, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*HM*], [*DAM*], [*IRM*], [*HLA*], [*CRM*], [*RM*], [*prolif*], [*DC*], [*macro*]),
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[31,749]], [#text(font: "DejaVu Sans Mono")[0.82]], [#text(font: "DejaVu Sans Mono")[0.27]], [#text(font: "DejaVu Sans Mono")[0.07]], [#text(font: "DejaVu Sans Mono")[0.79]], [#text(font: "DejaVu Sans Mono")[0.10]], [#text(font: "DejaVu Sans Mono")[0.94]], [#text(font: "DejaVu Sans Mono")[-0.07]], [#text(font: "DejaVu Sans Mono")[-0.02]], [#text(font: "DejaVu Sans Mono")[0.09]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[5,726]], [#text(font: "DejaVu Sans Mono")[0.73]], [#text(font: "DejaVu Sans Mono")[0.33]], [#text(font: "DejaVu Sans Mono")[0.09]], [#text(font: "DejaVu Sans Mono")[0.80]], [#text(font: "DejaVu Sans Mono")[0.15]], [#text(font: "DejaVu Sans Mono")[0.81]], [#text(font: "DejaVu Sans Mono")[-0.07]], [#text(font: "DejaVu Sans Mono")[-0.01]], [#text(font: "DejaVu Sans Mono")[-0.11]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[2,567]], [#text(font: "DejaVu Sans Mono")[0.44]], [#text(font: "DejaVu Sans Mono")[0.75]], [#text(font: "DejaVu Sans Mono")[0.10]], [#text(font: "DejaVu Sans Mono")[1.04]], [#text(font: "DejaVu Sans Mono")[0.15]], [#text(font: "DejaVu Sans Mono")[1.27]], [#text(font: "DejaVu Sans Mono")[0.06]], [#text(font: "DejaVu Sans Mono")[-0.06]], [#text(font: "DejaVu Sans Mono")[-0.13]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[618]], [#text(font: "DejaVu Sans Mono")[0.55]], [#text(font: "DejaVu Sans Mono")[0.45]], [#text(font: "DejaVu Sans Mono")[0.13]], [#text(font: "DejaVu Sans Mono")[1.23]], [#text(font: "DejaVu Sans Mono")[0.23]], [#text(font: "DejaVu Sans Mono")[1.23]], [#text(font: "DejaVu Sans Mono")[-0.01]], [#text(font: "DejaVu Sans Mono")[0.04]], [#text(font: "DejaVu Sans Mono")[-0.08]],
      [IRM], [#text(font: "DejaVu Sans Mono")[607]], [#text(font: "DejaVu Sans Mono")[0.61]], [#text(font: "DejaVu Sans Mono")[0.39]], [#text(font: "DejaVu Sans Mono")[0.69]], [#text(font: "DejaVu Sans Mono")[0.89]], [#text(font: "DejaVu Sans Mono")[0.23]], [#text(font: "DejaVu Sans Mono")[1.07]], [#text(font: "DejaVu Sans Mono")[-0.05]], [#text(font: "DejaVu Sans Mono")[-0.04]], [#text(font: "DejaVu Sans Mono")[0.13]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[300]], [#text(font: "DejaVu Sans Mono")[0.50]], [#text(font: "DejaVu Sans Mono")[0.35]], [#text(font: "DejaVu Sans Mono")[0.10]], [#text(font: "DejaVu Sans Mono")[1.33]], [#text(font: "DejaVu Sans Mono")[0.19]], [#text(font: "DejaVu Sans Mono")[1.57]], [#text(font: "DejaVu Sans Mono")[0.02]], [#text(font: "DejaVu Sans Mono")[0.47]], [#text(font: "DejaVu Sans Mono")[-0.06]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[257]], [#text(font: "DejaVu Sans Mono")[0.54]], [#text(font: "DejaVu Sans Mono")[0.39]], [#text(font: "DejaVu Sans Mono")[0.11]], [#text(font: "DejaVu Sans Mono")[1.25]], [#text(font: "DejaVu Sans Mono")[0.26]], [#text(font: "DejaVu Sans Mono")[1.52]], [#text(font: "DejaVu Sans Mono")[-0.00]], [#text(font: "DejaVu Sans Mono")[0.40]], [#text(font: "DejaVu Sans Mono")[-0.03]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[233]], [#text(font: "DejaVu Sans Mono")[0.60]], [#text(font: "DejaVu Sans Mono")[0.28]], [#text(font: "DejaVu Sans Mono")[0.09]], [#text(font: "DejaVu Sans Mono")[0.73]], [#text(font: "DejaVu Sans Mono")[0.07]], [#text(font: "DejaVu Sans Mono")[0.82]], [#text(font: "DejaVu Sans Mono")[0.70]], [#text(font: "DejaVu Sans Mono")[-0.08]], [#text(font: "DejaVu Sans Mono")[-0.04]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono")[115]], [#text(font: "DejaVu Sans Mono")[0.47]], [#text(font: "DejaVu Sans Mono")[0.33]], [#text(font: "DejaVu Sans Mono")[0.14]], [#text(font: "DejaVu Sans Mono")[1.23]], [#text(font: "DejaVu Sans Mono")[0.38]], [#text(font: "DejaVu Sans Mono")[1.24]], [#text(font: "DejaVu Sans Mono")[0.01]], [#text(font: "DejaVu Sans Mono")[0.82]], [#text(font: "DejaVu Sans Mono")[-0.14]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[71]], [#text(font: "DejaVu Sans Mono")[0.59]], [#text(font: "DejaVu Sans Mono")[0.36]], [#text(font: "DejaVu Sans Mono")[0.12]], [#text(font: "DejaVu Sans Mono")[0.93]], [#text(font: "DejaVu Sans Mono")[0.21]], [#text(font: "DejaVu Sans Mono")[1.13]], [#text(font: "DejaVu Sans Mono")[0.09]], [#text(font: "DejaVu Sans Mono")[0.17]], [#text(font: "DejaVu Sans Mono")[0.40]],
)
#text(size: 7pt, fill: gray)[Author column mapping: HM→`HM8`, DAM→`DAM6`, IRM→`IRM9`, HLA→`HLA7`, CRM→`CRM5`, RM→`RM10`, prolif→`proliferating2`, DC→`dendritic1`, macro→`macrophage3`]


=== Author label cross-tab
_Cell counts: rows = starcat Identity\_Label, columns = author's `cluster\_id`. Concentration along rows = agreement._

#table(
    columns: (auto, auto, auto, auto, auto, auto, auto, auto, auto),
    inset: 3pt,
    align: (left, right, right, right, right, right, right, right, right),
    stroke: 0.5pt + gray,
    table.header([*starcat label*], [*0*], [*1*], [*2*], [*3*], [*4*], [*5*], [*6*], [*7*]),
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[28]], [#text(font: "DejaVu Sans Mono")[4]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[12]], [#text(font: "DejaVu Sans Mono")[11]], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono")[11]],
      [Cycling], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono", weight: "bold")[230]],
      [DAM], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[7]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[6]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_Mature], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[2]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_cDC1], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[3]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[4]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[2]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[8]], [#text(font: "DejaVu Sans Mono")[21]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[14]], [#text(font: "DejaVu Sans Mono", weight: "bold")[189]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[64]], [#text(font: "DejaVu Sans Mono")[2]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[11]], [#text(font: "DejaVu Sans Mono")[22]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[31]], [#text(font: "DejaVu Sans Mono", weight: "bold")[143]], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono")[44]], [#text(font: "DejaVu Sans Mono")[4]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[27]], [#text(font: "DejaVu Sans Mono", weight: "bold")[401]], [#text(font: "DejaVu Sans Mono")[12]], [#text(font: "DejaVu Sans Mono")[60]], [#text(font: "DejaVu Sans Mono")[81]], [#text(font: "DejaVu Sans Mono")[7]], [#text(font: "DejaVu Sans Mono")[27]], [#text(font: "DejaVu Sans Mono")[3]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono", weight: "bold")[894]], [#text(font: "DejaVu Sans Mono", weight: "bold")[2,036]], [#text(font: "DejaVu Sans Mono", weight: "bold")[986]], [#text(font: "DejaVu Sans Mono", weight: "bold")[794]], [#text(font: "DejaVu Sans Mono", weight: "bold")[615]], [#text(font: "DejaVu Sans Mono", weight: "bold")[282]], [#text(font: "DejaVu Sans Mono")[49]], [#text(font: "DejaVu Sans Mono")[70]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono", weight: "bold")[12,215]], [#text(font: "DejaVu Sans Mono", weight: "bold")[3,288]], [#text(font: "DejaVu Sans Mono", weight: "bold")[6,354]], [#text(font: "DejaVu Sans Mono", weight: "bold")[5,485]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,756]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,451]], [#text(font: "DejaVu Sans Mono", weight: "bold")[804]], [#text(font: "DejaVu Sans Mono", weight: "bold")[396]],
      [IRM], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[22]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[40]], [#text(font: "DejaVu Sans Mono")[23]], [#text(font: "DejaVu Sans Mono", weight: "bold")[506]], [#text(font: "DejaVu Sans Mono")[13]], [#text(font: "DejaVu Sans Mono")[3]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[13]], [#text(font: "DejaVu Sans Mono", weight: "bold")[2,147]], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono", weight: "bold")[184]], [#text(font: "DejaVu Sans Mono")[33]], [#text(font: "DejaVu Sans Mono")[33]], [#text(font: "DejaVu Sans Mono", weight: "bold")[150]], [#text(font: "DejaVu Sans Mono")[5]],
)
#text(size: 7pt, fill: gray)[Author column: `cluster\_id`]


#pagebreak()

== Simpson2025 / seurat\_obj\_sorl1\_final

_Tech: 10x\_chromium, n\_cells: 42,270_
#text(fill: orange)[⚠ 43 pending columns not merged]

#grid(columns: (1fr, 1fr), gutter: 12pt,
[
=== QA flags
#table(
    columns: (auto, auto),
    inset: 4pt,
    align: (left, left),
    stroke: 0.5pt + gray,
      [Cells], [#text(font: "DejaVu Sans Mono")[42,270]],
      [Gene overlap], [#text(font: "DejaVu Sans Mono")[2,723 / 2,727]],
      [Median residual], [#text(font: "DejaVu Sans Mono")[1.663]],
      [WholeCell% (high)], [#text(font: "DejaVu Sans Mono")[16.8%]],
      [DC% (\> 0.05)], [#text(font: "DejaVu Sans Mono")[56.4% ⚠ DC contamination]],
)

],
[
=== Identity\_Label distribution
#table(
    columns: (auto, auto, auto),
    inset: 3pt,
    align: (left, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*%*]),
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[32,584]], [#text(font: "DejaVu Sans Mono")[77.1%]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[5,059]], [#text(font: "DejaVu Sans Mono")[12.0%]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[2,333]], [#text(font: "DejaVu Sans Mono")[5.5%]],
      [IRM], [#text(font: "DejaVu Sans Mono")[555]], [#text(font: "DejaVu Sans Mono")[1.3%]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[510]], [#text(font: "DejaVu Sans Mono")[1.2%]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[453]], [#text(font: "DejaVu Sans Mono")[1.1%]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[329]], [#text(font: "DejaVu Sans Mono")[0.8%]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[204]], [#text(font: "DejaVu Sans Mono")[0.5%]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono")[101]], [#text(font: "DejaVu Sans Mono")[0.2%]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[97]], [#text(font: "DejaVu Sans Mono")[0.2%]],
      [_other (3)_], [#text(font: "DejaVu Sans Mono")[45]], [#text(font: "DejaVu Sans Mono")[0.1%]],
)

]
)

=== Author state scores by starcat label
_For each starcat-assigned Identity\_Label, mean of the author's matching state-score columns. Diagonals (e.g. row=DAM, col=DAM) should be high._

#table(
    columns: (auto, auto, auto, auto, auto, auto, auto, auto, auto, auto, auto),
    inset: 3pt,
    align: (left, right, right, right, right, right, right, right, right, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*HM*], [*DAM*], [*IRM*], [*HLA*], [*CRM*], [*RM*], [*prolif*], [*DC*], [*macro*]),
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[32,584]], [#text(font: "DejaVu Sans Mono")[0.82]], [#text(font: "DejaVu Sans Mono")[0.28]], [#text(font: "DejaVu Sans Mono")[0.07]], [#text(font: "DejaVu Sans Mono")[0.79]], [#text(font: "DejaVu Sans Mono")[0.11]], [#text(font: "DejaVu Sans Mono")[0.92]], [#text(font: "DejaVu Sans Mono")[-0.07]], [#text(font: "DejaVu Sans Mono")[-0.01]], [#text(font: "DejaVu Sans Mono")[0.08]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[5,059]], [#text(font: "DejaVu Sans Mono")[0.71]], [#text(font: "DejaVu Sans Mono")[0.34]], [#text(font: "DejaVu Sans Mono")[0.09]], [#text(font: "DejaVu Sans Mono")[0.81]], [#text(font: "DejaVu Sans Mono")[0.16]], [#text(font: "DejaVu Sans Mono")[0.82]], [#text(font: "DejaVu Sans Mono")[-0.07]], [#text(font: "DejaVu Sans Mono")[-0.01]], [#text(font: "DejaVu Sans Mono")[-0.12]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[2,333]], [#text(font: "DejaVu Sans Mono")[0.43]], [#text(font: "DejaVu Sans Mono")[0.77]], [#text(font: "DejaVu Sans Mono")[0.10]], [#text(font: "DejaVu Sans Mono")[1.04]], [#text(font: "DejaVu Sans Mono")[0.14]], [#text(font: "DejaVu Sans Mono")[1.27]], [#text(font: "DejaVu Sans Mono")[0.06]], [#text(font: "DejaVu Sans Mono")[-0.06]], [#text(font: "DejaVu Sans Mono")[-0.13]],
      [IRM], [#text(font: "DejaVu Sans Mono")[555]], [#text(font: "DejaVu Sans Mono")[0.60]], [#text(font: "DejaVu Sans Mono")[0.39]], [#text(font: "DejaVu Sans Mono")[0.70]], [#text(font: "DejaVu Sans Mono")[0.89]], [#text(font: "DejaVu Sans Mono")[0.24]], [#text(font: "DejaVu Sans Mono")[1.07]], [#text(font: "DejaVu Sans Mono")[-0.04]], [#text(font: "DejaVu Sans Mono")[-0.04]], [#text(font: "DejaVu Sans Mono")[0.13]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[510]], [#text(font: "DejaVu Sans Mono")[0.54]], [#text(font: "DejaVu Sans Mono")[0.47]], [#text(font: "DejaVu Sans Mono")[0.13]], [#text(font: "DejaVu Sans Mono")[1.25]], [#text(font: "DejaVu Sans Mono")[0.22]], [#text(font: "DejaVu Sans Mono")[1.25]], [#text(font: "DejaVu Sans Mono")[-0.01]], [#text(font: "DejaVu Sans Mono")[0.01]], [#text(font: "DejaVu Sans Mono")[-0.09]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[453]], [#text(font: "DejaVu Sans Mono")[0.44]], [#text(font: "DejaVu Sans Mono")[0.39]], [#text(font: "DejaVu Sans Mono")[0.10]], [#text(font: "DejaVu Sans Mono")[1.39]], [#text(font: "DejaVu Sans Mono")[0.15]], [#text(font: "DejaVu Sans Mono")[1.76]], [#text(font: "DejaVu Sans Mono")[0.04]], [#text(font: "DejaVu Sans Mono")[0.34]], [#text(font: "DejaVu Sans Mono")[-0.04]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[329]], [#text(font: "DejaVu Sans Mono")[0.47]], [#text(font: "DejaVu Sans Mono")[0.42]], [#text(font: "DejaVu Sans Mono")[0.10]], [#text(font: "DejaVu Sans Mono")[1.35]], [#text(font: "DejaVu Sans Mono")[0.15]], [#text(font: "DejaVu Sans Mono")[1.76]], [#text(font: "DejaVu Sans Mono")[0.03]], [#text(font: "DejaVu Sans Mono")[0.23]], [#text(font: "DejaVu Sans Mono")[-0.01]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[204]], [#text(font: "DejaVu Sans Mono")[0.57]], [#text(font: "DejaVu Sans Mono")[0.28]], [#text(font: "DejaVu Sans Mono")[0.09]], [#text(font: "DejaVu Sans Mono")[0.74]], [#text(font: "DejaVu Sans Mono")[0.07]], [#text(font: "DejaVu Sans Mono")[0.85]], [#text(font: "DejaVu Sans Mono")[0.73]], [#text(font: "DejaVu Sans Mono")[-0.07]], [#text(font: "DejaVu Sans Mono")[-0.04]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono")[101]], [#text(font: "DejaVu Sans Mono")[0.45]], [#text(font: "DejaVu Sans Mono")[0.32]], [#text(font: "DejaVu Sans Mono")[0.12]], [#text(font: "DejaVu Sans Mono")[1.21]], [#text(font: "DejaVu Sans Mono")[0.34]], [#text(font: "DejaVu Sans Mono")[1.29]], [#text(font: "DejaVu Sans Mono")[-0.00]], [#text(font: "DejaVu Sans Mono")[0.74]], [#text(font: "DejaVu Sans Mono")[-0.12]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[97]], [#text(font: "DejaVu Sans Mono")[0.54]], [#text(font: "DejaVu Sans Mono")[0.37]], [#text(font: "DejaVu Sans Mono")[0.10]], [#text(font: "DejaVu Sans Mono")[1.00]], [#text(font: "DejaVu Sans Mono")[0.17]], [#text(font: "DejaVu Sans Mono")[1.29]], [#text(font: "DejaVu Sans Mono")[0.07]], [#text(font: "DejaVu Sans Mono")[0.16]], [#text(font: "DejaVu Sans Mono")[0.32]],
)
#text(size: 7pt, fill: gray)[Author column mapping: HM→`HM8`, DAM→`DAM6`, IRM→`IRM9`, HLA→`HLA7`, CRM→`CRM5`, RM→`RM10`, prolif→`proliferating2`, DC→`dendritic1`, macro→`macrophage3`]


=== Author label cross-tab
_Cell counts: rows = starcat Identity\_Label, columns = author's `seurat\_clusters`. Concentration along rows = agreement._

#table(
    columns: (auto, auto, auto, auto, auto, auto, auto, auto, auto),
    inset: 3pt,
    align: (left, right, right, right, right, right, right, right, right),
    stroke: 0.5pt + gray,
    table.header([*starcat label*], [*0*], [*1*], [*2*], [*3*], [*4*], [*5*], [*6*], [*7*]),
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[31]], [#text(font: "DejaVu Sans Mono")[5]], [#text(font: "DejaVu Sans Mono")[5]], [#text(font: "DejaVu Sans Mono")[12]], [#text(font: "DejaVu Sans Mono")[14]], [#text(font: "DejaVu Sans Mono")[4]], [#text(font: "DejaVu Sans Mono")[15]], [#text(font: "DejaVu Sans Mono")[11]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono", weight: "bold")[195]],
      [DAM], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono")[13]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[5]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_Mature], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[2]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_cDC1], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[4]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[13]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[21]], [#text(font: "DejaVu Sans Mono")[39]], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[21]], [#text(font: "DejaVu Sans Mono", weight: "bold")[197]], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono", weight: "bold")[168]], [#text(font: "DejaVu Sans Mono")[2]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[22]], [#text(font: "DejaVu Sans Mono")[28]], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[22]], [#text(font: "DejaVu Sans Mono", weight: "bold")[102]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono", weight: "bold")[148]], [#text(font: "DejaVu Sans Mono")[3]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[21]], [#text(font: "DejaVu Sans Mono", weight: "bold")[348]], [#text(font: "DejaVu Sans Mono")[8]], [#text(font: "DejaVu Sans Mono")[50]], [#text(font: "DejaVu Sans Mono")[53]], [#text(font: "DejaVu Sans Mono")[5]], [#text(font: "DejaVu Sans Mono")[22]], [#text(font: "DejaVu Sans Mono")[3]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono", weight: "bold")[744]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,899]], [#text(font: "DejaVu Sans Mono", weight: "bold")[826]], [#text(font: "DejaVu Sans Mono", weight: "bold")[699]], [#text(font: "DejaVu Sans Mono", weight: "bold")[518]], [#text(font: "DejaVu Sans Mono", weight: "bold")[261]], [#text(font: "DejaVu Sans Mono")[41]], [#text(font: "DejaVu Sans Mono")[71]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono", weight: "bold")[12,335]], [#text(font: "DejaVu Sans Mono", weight: "bold")[3,626]], [#text(font: "DejaVu Sans Mono", weight: "bold")[6,505]], [#text(font: "DejaVu Sans Mono", weight: "bold")[5,636]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,949]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,526]], [#text(font: "DejaVu Sans Mono", weight: "bold")[577]], [#text(font: "DejaVu Sans Mono", weight: "bold")[430]],
      [IRM], [#text(font: "DejaVu Sans Mono")[4]], [#text(font: "DejaVu Sans Mono")[21]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[34]], [#text(font: "DejaVu Sans Mono")[20]], [#text(font: "DejaVu Sans Mono", weight: "bold")[456]], [#text(font: "DejaVu Sans Mono")[15]], [#text(font: "DejaVu Sans Mono")[5]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[9]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,972]], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono", weight: "bold")[149]], [#text(font: "DejaVu Sans Mono")[22]], [#text(font: "DejaVu Sans Mono")[30]], [#text(font: "DejaVu Sans Mono", weight: "bold")[143]], [#text(font: "DejaVu Sans Mono")[6]],
)
#text(size: 7pt, fill: gray)[Author column: `seurat\_clusters`]


#pagebreak()

== Gabitto2024 / Seattle\_PFC\_MG

_Tech: 10x\_chromium, n\_cells: 42,486_
#text(fill: orange)[⚠ 43 pending columns not merged]

#grid(columns: (1fr, 1fr), gutter: 12pt,
[
=== QA flags
#table(
    columns: (auto, auto),
    inset: 4pt,
    align: (left, left),
    stroke: 0.5pt + gray,
      [Cells], [#text(font: "DejaVu Sans Mono")[42,486]],
      [Gene overlap], [#text(font: "DejaVu Sans Mono")[2,727 / 2,727]],
      [Median residual], [#text(font: "DejaVu Sans Mono")[1.095]],
      [WholeCell% (high)], [#text(font: "DejaVu Sans Mono")[0.7%]],
      [DC% (\> 0.05)], [#text(font: "DejaVu Sans Mono")[67.1% ⚠ DC contamination]],
)

],
[
=== Identity\_Label distribution
#table(
    columns: (auto, auto, auto),
    inset: 3pt,
    align: (left, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*%*]),
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[24,208]], [#text(font: "DejaVu Sans Mono")[57.0%]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[5,670]], [#text(font: "DejaVu Sans Mono")[13.3%]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[3,247]], [#text(font: "DejaVu Sans Mono")[7.6%]],
      [DAM], [#text(font: "DejaVu Sans Mono")[2,715]], [#text(font: "DejaVu Sans Mono")[6.4%]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[2,152]], [#text(font: "DejaVu Sans Mono")[5.1%]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[1,366]], [#text(font: "DejaVu Sans Mono")[3.2%]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[1,095]], [#text(font: "DejaVu Sans Mono")[2.6%]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono")[725]], [#text(font: "DejaVu Sans Mono")[1.7%]],
      [IRM], [#text(font: "DejaVu Sans Mono")[358]], [#text(font: "DejaVu Sans Mono")[0.8%]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[357]], [#text(font: "DejaVu Sans Mono")[0.8%]],
      [_other (3)_], [#text(font: "DejaVu Sans Mono")[593]], [#text(font: "DejaVu Sans Mono")[1.4%]],
)

]
)

_No comparable author state-scores or label columns detected in this dataset; starcat annotation stands on its own._

#pagebreak()

== Davis2025 / 20240812\_sexmicroglia

_Tech: 10x\_chromium, n\_cells: 43,552_
#text(fill: orange)[⚠ 43 pending columns not merged]

#grid(columns: (1fr, 1fr), gutter: 12pt,
[
=== QA flags
#table(
    columns: (auto, auto),
    inset: 4pt,
    align: (left, left),
    stroke: 0.5pt + gray,
      [Cells], [#text(font: "DejaVu Sans Mono")[43,552]],
      [Gene overlap], [#text(font: "DejaVu Sans Mono")[2,727 / 2,727]],
      [Median residual], [#text(font: "DejaVu Sans Mono")[1.333]],
      [WholeCell% (high)], [#text(font: "DejaVu Sans Mono")[25.0%]],
      [DC% (\> 0.05)], [#text(font: "DejaVu Sans Mono")[49.9% ⚠ DC contamination]],
)

],
[
=== Identity\_Label distribution
#table(
    columns: (auto, auto, auto),
    inset: 3pt,
    align: (left, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*%*]),
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[25,108]], [#text(font: "DejaVu Sans Mono")[57.7%]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[6,696]], [#text(font: "DejaVu Sans Mono")[15.4%]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[4,038]], [#text(font: "DejaVu Sans Mono")[9.3%]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[3,981]], [#text(font: "DejaVu Sans Mono")[9.1%]],
      [IRM], [#text(font: "DejaVu Sans Mono")[1,609]], [#text(font: "DejaVu Sans Mono")[3.7%]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[1,089]], [#text(font: "DejaVu Sans Mono")[2.5%]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[371]], [#text(font: "DejaVu Sans Mono")[0.9%]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[229]], [#text(font: "DejaVu Sans Mono")[0.5%]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[168]], [#text(font: "DejaVu Sans Mono")[0.4%]],
      [DC\_cDC1], [#text(font: "DejaVu Sans Mono")[138]], [#text(font: "DejaVu Sans Mono")[0.3%]],
      [_other (3)_], [#text(font: "DejaVu Sans Mono")[125]], [#text(font: "DejaVu Sans Mono")[0.3%]],
)

]
)

=== Author state scores by starcat label
_For each starcat-assigned Identity\_Label, mean of the author's matching state-score columns. Diagonals (e.g. row=DAM, col=DAM) should be high._

#table(
    columns: (auto, auto, auto, auto, auto, auto, auto, auto, auto),
    inset: 3pt,
    align: (left, right, right, right, right, right, right, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*HM*], [*DAM*], [*IRM*], [*HLA*], [*CRM*], [*RM*], [*prolif*]),
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[25,108]], [#text(font: "DejaVu Sans Mono")[0.70]], [#text(font: "DejaVu Sans Mono")[0.18]], [#text(font: "DejaVu Sans Mono")[0.11]], [#text(font: "DejaVu Sans Mono")[0.73]], [#text(font: "DejaVu Sans Mono")[0.23]], [#text(font: "DejaVu Sans Mono")[0.72]], [#text(font: "DejaVu Sans Mono")[-0.04]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[6,696]], [#text(font: "DejaVu Sans Mono")[0.63]], [#text(font: "DejaVu Sans Mono")[0.24]], [#text(font: "DejaVu Sans Mono")[0.12]], [#text(font: "DejaVu Sans Mono")[0.78]], [#text(font: "DejaVu Sans Mono")[0.26]], [#text(font: "DejaVu Sans Mono")[0.68]], [#text(font: "DejaVu Sans Mono")[-0.01]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[4,038]], [#text(font: "DejaVu Sans Mono")[0.49]], [#text(font: "DejaVu Sans Mono")[0.34]], [#text(font: "DejaVu Sans Mono")[0.20]], [#text(font: "DejaVu Sans Mono")[1.22]], [#text(font: "DejaVu Sans Mono")[0.33]], [#text(font: "DejaVu Sans Mono")[1.13]], [#text(font: "DejaVu Sans Mono")[0.11]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[3,981]], [#text(font: "DejaVu Sans Mono")[0.38]], [#text(font: "DejaVu Sans Mono")[0.58]], [#text(font: "DejaVu Sans Mono")[0.16]], [#text(font: "DejaVu Sans Mono")[1.11]], [#text(font: "DejaVu Sans Mono")[0.29]], [#text(font: "DejaVu Sans Mono")[1.04]], [#text(font: "DejaVu Sans Mono")[0.22]],
      [IRM], [#text(font: "DejaVu Sans Mono")[1,609]], [#text(font: "DejaVu Sans Mono")[0.46]], [#text(font: "DejaVu Sans Mono")[0.31]], [#text(font: "DejaVu Sans Mono")[0.55]], [#text(font: "DejaVu Sans Mono")[1.03]], [#text(font: "DejaVu Sans Mono")[0.41]], [#text(font: "DejaVu Sans Mono")[0.99]], [#text(font: "DejaVu Sans Mono")[0.07]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[1,089]], [#text(font: "DejaVu Sans Mono")[0.51]], [#text(font: "DejaVu Sans Mono")[0.30]], [#text(font: "DejaVu Sans Mono")[0.13]], [#text(font: "DejaVu Sans Mono")[1.07]], [#text(font: "DejaVu Sans Mono")[0.35]], [#text(font: "DejaVu Sans Mono")[1.18]], [#text(font: "DejaVu Sans Mono")[0.15]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[371]], [#text(font: "DejaVu Sans Mono")[0.52]], [#text(font: "DejaVu Sans Mono")[0.30]], [#text(font: "DejaVu Sans Mono")[0.13]], [#text(font: "DejaVu Sans Mono")[1.16]], [#text(font: "DejaVu Sans Mono")[0.26]], [#text(font: "DejaVu Sans Mono")[1.28]], [#text(font: "DejaVu Sans Mono")[0.16]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[229]], [#text(font: "DejaVu Sans Mono")[0.50]], [#text(font: "DejaVu Sans Mono")[0.27]], [#text(font: "DejaVu Sans Mono")[0.14]], [#text(font: "DejaVu Sans Mono")[0.90]], [#text(font: "DejaVu Sans Mono")[0.28]], [#text(font: "DejaVu Sans Mono")[0.98]], [#text(font: "DejaVu Sans Mono")[0.08]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[168]], [#text(font: "DejaVu Sans Mono")[0.51]], [#text(font: "DejaVu Sans Mono")[0.29]], [#text(font: "DejaVu Sans Mono")[0.13]], [#text(font: "DejaVu Sans Mono")[1.06]], [#text(font: "DejaVu Sans Mono")[0.29]], [#text(font: "DejaVu Sans Mono")[1.19]], [#text(font: "DejaVu Sans Mono")[0.33]],
      [DC\_cDC1], [#text(font: "DejaVu Sans Mono")[138]], [#text(font: "DejaVu Sans Mono")[0.51]], [#text(font: "DejaVu Sans Mono")[0.29]], [#text(font: "DejaVu Sans Mono")[0.11]], [#text(font: "DejaVu Sans Mono")[1.06]], [#text(font: "DejaVu Sans Mono")[0.35]], [#text(font: "DejaVu Sans Mono")[1.17]], [#text(font: "DejaVu Sans Mono")[0.13]],
)
#text(size: 7pt, fill: gray)[Author column mapping: HM→`HM`, DAM→`DAM`, IRM→`IRM`, HLA→`HLA`, CRM→`CRM`, RM→`TRM`, prolif→`proliferating`]


=== Author label cross-tab
_Cell counts: rows = starcat Identity\_Label, columns = author's `annotations`. Concentration along rows = agreement._

#table(
    columns: (auto, auto, auto, auto, auto, auto, auto, auto),
    inset: 3pt,
    align: (left, right, right, right, right, right, right, right),
    stroke: 0.5pt + gray,
    table.header([*starcat label*], [*HM*], [*CRM*], [*tCRM*], [*HLA*], [*RM*], [*DAM*], [*IRM*]),
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[46]], [#text(font: "DejaVu Sans Mono")[28]], [#text(font: "DejaVu Sans Mono")[97]], [#text(font: "DejaVu Sans Mono")[5]], [#text(font: "DejaVu Sans Mono")[21]], [#text(font: "DejaVu Sans Mono")[17]], [#text(font: "DejaVu Sans Mono")[15]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[22]], [#text(font: "DejaVu Sans Mono")[17]], [#text(font: "DejaVu Sans Mono", weight: "bold")[101]], [#text(font: "DejaVu Sans Mono")[5]], [#text(font: "DejaVu Sans Mono")[22]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[1]],
      [DAM], [#text(font: "DejaVu Sans Mono")[6]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[7]], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[4]],
      [DC\_Mature], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[26]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[3]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[4]],
      [DC\_cDC1], [#text(font: "DejaVu Sans Mono")[17]], [#text(font: "DejaVu Sans Mono")[10]], [#text(font: "DejaVu Sans Mono")[87]], [#text(font: "DejaVu Sans Mono")[5]], [#text(font: "DejaVu Sans Mono")[12]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[6]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[82]], [#text(font: "DejaVu Sans Mono")[36]], [#text(font: "DejaVu Sans Mono", weight: "bold")[153]], [#text(font: "DejaVu Sans Mono")[17]], [#text(font: "DejaVu Sans Mono")[69]], [#text(font: "DejaVu Sans Mono")[7]], [#text(font: "DejaVu Sans Mono")[7]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono", weight: "bold")[109]], [#text(font: "DejaVu Sans Mono", weight: "bold")[162]], [#text(font: "DejaVu Sans Mono", weight: "bold")[619]], [#text(font: "DejaVu Sans Mono")[91]], [#text(font: "DejaVu Sans Mono")[86]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[21]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono", weight: "bold")[713]], [#text(font: "DejaVu Sans Mono", weight: "bold")[710]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,003]], [#text(font: "DejaVu Sans Mono", weight: "bold")[734]], [#text(font: "DejaVu Sans Mono", weight: "bold")[707]], [#text(font: "DejaVu Sans Mono")[82]], [#text(font: "DejaVu Sans Mono")[89]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono", weight: "bold")[3,041]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,730]], [#text(font: "DejaVu Sans Mono", weight: "bold")[458]], [#text(font: "DejaVu Sans Mono", weight: "bold")[856]], [#text(font: "DejaVu Sans Mono", weight: "bold")[365]], [#text(font: "DejaVu Sans Mono", weight: "bold")[127]], [#text(font: "DejaVu Sans Mono", weight: "bold")[119]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono", weight: "bold")[10,882]], [#text(font: "DejaVu Sans Mono", weight: "bold")[7,584]], [#text(font: "DejaVu Sans Mono", weight: "bold")[3,912]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,639]], [#text(font: "DejaVu Sans Mono", weight: "bold")[610]], [#text(font: "DejaVu Sans Mono", weight: "bold")[125]], [#text(font: "DejaVu Sans Mono", weight: "bold")[356]],
      [IRM], [#text(font: "DejaVu Sans Mono", weight: "bold")[114]], [#text(font: "DejaVu Sans Mono", weight: "bold")[446]], [#text(font: "DejaVu Sans Mono", weight: "bold")[282]], [#text(font: "DejaVu Sans Mono")[44]], [#text(font: "DejaVu Sans Mono", weight: "bold")[112]], [#text(font: "DejaVu Sans Mono")[16]], [#text(font: "DejaVu Sans Mono", weight: "bold")[595]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono", weight: "bold")[357]], [#text(font: "DejaVu Sans Mono", weight: "bold")[113]], [#text(font: "DejaVu Sans Mono", weight: "bold")[156]], [#text(font: "DejaVu Sans Mono", weight: "bold")[944]], [#text(font: "DejaVu Sans Mono", weight: "bold")[375]], [#text(font: "DejaVu Sans Mono", weight: "bold")[2,006]], [#text(font: "DejaVu Sans Mono")[30]],
)
#text(size: 7pt, fill: gray)[Author column: `annotations`]


#pagebreak()

== Davis2025 / davis2025.20250619

_Tech: 10x\_chromium, n\_cells: 43,552_
#text(fill: orange)[⚠ 43 pending columns not merged]

#grid(columns: (1fr, 1fr), gutter: 12pt,
[
=== QA flags
#table(
    columns: (auto, auto),
    inset: 4pt,
    align: (left, left),
    stroke: 0.5pt + gray,
      [Cells], [#text(font: "DejaVu Sans Mono")[43,552]],
      [Gene overlap], [#text(font: "DejaVu Sans Mono")[2,727 / 2,727]],
      [Median residual], [#text(font: "DejaVu Sans Mono")[1.333]],
      [WholeCell% (high)], [#text(font: "DejaVu Sans Mono")[25.0%]],
      [DC% (\> 0.05)], [#text(font: "DejaVu Sans Mono")[49.9% ⚠ DC contamination]],
)

],
[
=== Identity\_Label distribution
#table(
    columns: (auto, auto, auto),
    inset: 3pt,
    align: (left, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*%*]),
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[25,108]], [#text(font: "DejaVu Sans Mono")[57.7%]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[6,696]], [#text(font: "DejaVu Sans Mono")[15.4%]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[4,038]], [#text(font: "DejaVu Sans Mono")[9.3%]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[3,981]], [#text(font: "DejaVu Sans Mono")[9.1%]],
      [IRM], [#text(font: "DejaVu Sans Mono")[1,609]], [#text(font: "DejaVu Sans Mono")[3.7%]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[1,089]], [#text(font: "DejaVu Sans Mono")[2.5%]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[371]], [#text(font: "DejaVu Sans Mono")[0.9%]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[229]], [#text(font: "DejaVu Sans Mono")[0.5%]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[168]], [#text(font: "DejaVu Sans Mono")[0.4%]],
      [DC\_cDC1], [#text(font: "DejaVu Sans Mono")[138]], [#text(font: "DejaVu Sans Mono")[0.3%]],
      [_other (3)_], [#text(font: "DejaVu Sans Mono")[125]], [#text(font: "DejaVu Sans Mono")[0.3%]],
)

]
)

=== Author state scores by starcat label
_For each starcat-assigned Identity\_Label, mean of the author's matching state-score columns. Diagonals (e.g. row=DAM, col=DAM) should be high._

#table(
    columns: (auto, auto, auto, auto, auto, auto, auto, auto, auto),
    inset: 3pt,
    align: (left, right, right, right, right, right, right, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*HM*], [*DAM*], [*IRM*], [*HLA*], [*CRM*], [*RM*], [*prolif*]),
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[25,108]], [#text(font: "DejaVu Sans Mono")[0.70]], [#text(font: "DejaVu Sans Mono")[0.18]], [#text(font: "DejaVu Sans Mono")[0.11]], [#text(font: "DejaVu Sans Mono")[0.73]], [#text(font: "DejaVu Sans Mono")[0.23]], [#text(font: "DejaVu Sans Mono")[0.72]], [#text(font: "DejaVu Sans Mono")[-0.04]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[6,696]], [#text(font: "DejaVu Sans Mono")[0.63]], [#text(font: "DejaVu Sans Mono")[0.24]], [#text(font: "DejaVu Sans Mono")[0.12]], [#text(font: "DejaVu Sans Mono")[0.78]], [#text(font: "DejaVu Sans Mono")[0.26]], [#text(font: "DejaVu Sans Mono")[0.68]], [#text(font: "DejaVu Sans Mono")[-0.01]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[4,038]], [#text(font: "DejaVu Sans Mono")[0.49]], [#text(font: "DejaVu Sans Mono")[0.34]], [#text(font: "DejaVu Sans Mono")[0.20]], [#text(font: "DejaVu Sans Mono")[1.22]], [#text(font: "DejaVu Sans Mono")[0.33]], [#text(font: "DejaVu Sans Mono")[1.13]], [#text(font: "DejaVu Sans Mono")[0.11]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[3,981]], [#text(font: "DejaVu Sans Mono")[0.38]], [#text(font: "DejaVu Sans Mono")[0.58]], [#text(font: "DejaVu Sans Mono")[0.16]], [#text(font: "DejaVu Sans Mono")[1.11]], [#text(font: "DejaVu Sans Mono")[0.29]], [#text(font: "DejaVu Sans Mono")[1.04]], [#text(font: "DejaVu Sans Mono")[0.22]],
      [IRM], [#text(font: "DejaVu Sans Mono")[1,609]], [#text(font: "DejaVu Sans Mono")[0.46]], [#text(font: "DejaVu Sans Mono")[0.31]], [#text(font: "DejaVu Sans Mono")[0.55]], [#text(font: "DejaVu Sans Mono")[1.03]], [#text(font: "DejaVu Sans Mono")[0.41]], [#text(font: "DejaVu Sans Mono")[0.99]], [#text(font: "DejaVu Sans Mono")[0.07]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[1,089]], [#text(font: "DejaVu Sans Mono")[0.51]], [#text(font: "DejaVu Sans Mono")[0.30]], [#text(font: "DejaVu Sans Mono")[0.13]], [#text(font: "DejaVu Sans Mono")[1.07]], [#text(font: "DejaVu Sans Mono")[0.35]], [#text(font: "DejaVu Sans Mono")[1.18]], [#text(font: "DejaVu Sans Mono")[0.15]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[371]], [#text(font: "DejaVu Sans Mono")[0.52]], [#text(font: "DejaVu Sans Mono")[0.30]], [#text(font: "DejaVu Sans Mono")[0.13]], [#text(font: "DejaVu Sans Mono")[1.16]], [#text(font: "DejaVu Sans Mono")[0.26]], [#text(font: "DejaVu Sans Mono")[1.28]], [#text(font: "DejaVu Sans Mono")[0.16]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[229]], [#text(font: "DejaVu Sans Mono")[0.50]], [#text(font: "DejaVu Sans Mono")[0.27]], [#text(font: "DejaVu Sans Mono")[0.14]], [#text(font: "DejaVu Sans Mono")[0.90]], [#text(font: "DejaVu Sans Mono")[0.28]], [#text(font: "DejaVu Sans Mono")[0.98]], [#text(font: "DejaVu Sans Mono")[0.08]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[168]], [#text(font: "DejaVu Sans Mono")[0.51]], [#text(font: "DejaVu Sans Mono")[0.29]], [#text(font: "DejaVu Sans Mono")[0.13]], [#text(font: "DejaVu Sans Mono")[1.06]], [#text(font: "DejaVu Sans Mono")[0.29]], [#text(font: "DejaVu Sans Mono")[1.19]], [#text(font: "DejaVu Sans Mono")[0.33]],
      [DC\_cDC1], [#text(font: "DejaVu Sans Mono")[138]], [#text(font: "DejaVu Sans Mono")[0.51]], [#text(font: "DejaVu Sans Mono")[0.29]], [#text(font: "DejaVu Sans Mono")[0.11]], [#text(font: "DejaVu Sans Mono")[1.06]], [#text(font: "DejaVu Sans Mono")[0.35]], [#text(font: "DejaVu Sans Mono")[1.17]], [#text(font: "DejaVu Sans Mono")[0.13]],
)
#text(size: 7pt, fill: gray)[Author column mapping: HM→`HM`, DAM→`DAM`, IRM→`IRM`, HLA→`HLA`, CRM→`CRM`, RM→`TRM`, prolif→`proliferating`]


=== Author label cross-tab
_Cell counts: rows = starcat Identity\_Label, columns = author's `annotations`. Concentration along rows = agreement._

#table(
    columns: (auto, auto, auto, auto, auto, auto, auto, auto),
    inset: 3pt,
    align: (left, right, right, right, right, right, right, right),
    stroke: 0.5pt + gray,
    table.header([*starcat label*], [*HM*], [*CRM*], [*tCRM*], [*HLA*], [*RM*], [*DAM*], [*IRM*]),
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[46]], [#text(font: "DejaVu Sans Mono")[28]], [#text(font: "DejaVu Sans Mono")[97]], [#text(font: "DejaVu Sans Mono")[5]], [#text(font: "DejaVu Sans Mono")[21]], [#text(font: "DejaVu Sans Mono")[17]], [#text(font: "DejaVu Sans Mono")[15]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[22]], [#text(font: "DejaVu Sans Mono")[17]], [#text(font: "DejaVu Sans Mono", weight: "bold")[101]], [#text(font: "DejaVu Sans Mono")[5]], [#text(font: "DejaVu Sans Mono")[22]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[1]],
      [DAM], [#text(font: "DejaVu Sans Mono")[6]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[7]], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[4]],
      [DC\_Mature], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[26]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[3]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[4]],
      [DC\_cDC1], [#text(font: "DejaVu Sans Mono")[17]], [#text(font: "DejaVu Sans Mono")[10]], [#text(font: "DejaVu Sans Mono")[87]], [#text(font: "DejaVu Sans Mono")[5]], [#text(font: "DejaVu Sans Mono")[12]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[6]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[82]], [#text(font: "DejaVu Sans Mono")[36]], [#text(font: "DejaVu Sans Mono", weight: "bold")[153]], [#text(font: "DejaVu Sans Mono")[17]], [#text(font: "DejaVu Sans Mono")[69]], [#text(font: "DejaVu Sans Mono")[7]], [#text(font: "DejaVu Sans Mono")[7]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono", weight: "bold")[109]], [#text(font: "DejaVu Sans Mono", weight: "bold")[162]], [#text(font: "DejaVu Sans Mono", weight: "bold")[619]], [#text(font: "DejaVu Sans Mono")[91]], [#text(font: "DejaVu Sans Mono")[86]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[21]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono", weight: "bold")[713]], [#text(font: "DejaVu Sans Mono", weight: "bold")[710]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,003]], [#text(font: "DejaVu Sans Mono", weight: "bold")[734]], [#text(font: "DejaVu Sans Mono", weight: "bold")[707]], [#text(font: "DejaVu Sans Mono")[82]], [#text(font: "DejaVu Sans Mono")[89]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono", weight: "bold")[3,041]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,730]], [#text(font: "DejaVu Sans Mono", weight: "bold")[458]], [#text(font: "DejaVu Sans Mono", weight: "bold")[856]], [#text(font: "DejaVu Sans Mono", weight: "bold")[365]], [#text(font: "DejaVu Sans Mono", weight: "bold")[127]], [#text(font: "DejaVu Sans Mono", weight: "bold")[119]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono", weight: "bold")[10,882]], [#text(font: "DejaVu Sans Mono", weight: "bold")[7,584]], [#text(font: "DejaVu Sans Mono", weight: "bold")[3,912]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,639]], [#text(font: "DejaVu Sans Mono", weight: "bold")[610]], [#text(font: "DejaVu Sans Mono", weight: "bold")[125]], [#text(font: "DejaVu Sans Mono", weight: "bold")[356]],
      [IRM], [#text(font: "DejaVu Sans Mono", weight: "bold")[114]], [#text(font: "DejaVu Sans Mono", weight: "bold")[446]], [#text(font: "DejaVu Sans Mono", weight: "bold")[282]], [#text(font: "DejaVu Sans Mono")[44]], [#text(font: "DejaVu Sans Mono", weight: "bold")[112]], [#text(font: "DejaVu Sans Mono")[16]], [#text(font: "DejaVu Sans Mono", weight: "bold")[595]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono", weight: "bold")[357]], [#text(font: "DejaVu Sans Mono", weight: "bold")[113]], [#text(font: "DejaVu Sans Mono", weight: "bold")[156]], [#text(font: "DejaVu Sans Mono", weight: "bold")[944]], [#text(font: "DejaVu Sans Mono", weight: "bold")[375]], [#text(font: "DejaVu Sans Mono", weight: "bold")[2,006]], [#text(font: "DejaVu Sans Mono")[30]],
)
#text(size: 7pt, fill: gray)[Author column: `annotations`]


#pagebreak()

== Gazestani2023 / microglia

_Tech: 10x\_chromium, n\_cells: 67,513_
#text(fill: orange)[⚠ 43 pending columns not merged]

#grid(columns: (1fr, 1fr), gutter: 12pt,
[
=== QA flags
#table(
    columns: (auto, auto),
    inset: 4pt,
    align: (left, left),
    stroke: 0.5pt + gray,
      [Cells], [#text(font: "DejaVu Sans Mono")[67,513]],
      [Gene overlap], [#text(font: "DejaVu Sans Mono")[2,727 / 2,727]],
      [Median residual], [#text(font: "DejaVu Sans Mono")[1.226]],
      [WholeCell% (high)], [#text(font: "DejaVu Sans Mono")[0.3%]],
      [DC% (\> 0.05)], [#text(font: "DejaVu Sans Mono")[44.8% ⚠ DC contamination]],
)

],
[
=== Identity\_Label distribution
#table(
    columns: (auto, auto, auto),
    inset: 3pt,
    align: (left, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*%*]),
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[39,952]], [#text(font: "DejaVu Sans Mono")[59.2%]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[18,768]], [#text(font: "DejaVu Sans Mono")[27.8%]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[3,866]], [#text(font: "DejaVu Sans Mono")[5.7%]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[2,094]], [#text(font: "DejaVu Sans Mono")[3.1%]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[1,012]], [#text(font: "DejaVu Sans Mono")[1.5%]],
      [DAM], [#text(font: "DejaVu Sans Mono")[801]], [#text(font: "DejaVu Sans Mono")[1.2%]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono")[462]], [#text(font: "DejaVu Sans Mono")[0.7%]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[216]], [#text(font: "DejaVu Sans Mono")[0.3%]],
      [IRM], [#text(font: "DejaVu Sans Mono")[143]], [#text(font: "DejaVu Sans Mono")[0.2%]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[99]], [#text(font: "DejaVu Sans Mono")[0.1%]],
      [_other (3)_], [#text(font: "DejaVu Sans Mono")[100]], [#text(font: "DejaVu Sans Mono")[0.1%]],
)

]
)

=== Author label cross-tab
_Cell counts: rows = starcat Identity\_Label, columns = author's `anno\_orig\_state`. Concentration along rows = agreement._

#table(
    columns: (auto, auto, auto, auto, auto, auto, auto, auto, auto, auto, auto),
    inset: 3pt,
    align: (left, right, right, right, right, right, right, right, right, right, right),
    stroke: 0.5pt + gray,
    table.header([*starcat label*], [*Microglia\_PAQR5*], [*Microglia\_LYVE1\_CECR2*], [*Microglia\_MGAM*], [*Microglia\_GPNMB\_PLAT*], [*Microglia\_FTH1\_LYVE1*], [*Microglia\_GPR126\_HCG22*], [*Microglia\_TNFAIP3*], [*Microglia\_GPNMB\_LPL*], [*Macrophage\_CD200R1*], [*Microglia\_CCL3*]),
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[7]], [#text(font: "DejaVu Sans Mono")[14]], [#text(font: "DejaVu Sans Mono")[21]], [#text(font: "DejaVu Sans Mono", weight: "bold")[217]], [#text(font: "DejaVu Sans Mono")[12]], [#text(font: "DejaVu Sans Mono")[89]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,531]], [#text(font: "DejaVu Sans Mono")[3]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[6]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[1]],
      [DAM], [#text(font: "DejaVu Sans Mono")[11]], [#text(font: "DejaVu Sans Mono")[6]], [#text(font: "DejaVu Sans Mono")[70]], [#text(font: "DejaVu Sans Mono", weight: "bold")[126]], [#text(font: "DejaVu Sans Mono")[81]], [#text(font: "DejaVu Sans Mono", weight: "bold")[104]], [#text(font: "DejaVu Sans Mono")[64]], [#text(font: "DejaVu Sans Mono", weight: "bold")[257]], [#text(font: "DejaVu Sans Mono")[7]], [#text(font: "DejaVu Sans Mono")[5]],
      [DC\_Mature], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[5]],
      [DC\_cDC1], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[2]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[5]], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[5]], [#text(font: "DejaVu Sans Mono")[23]], [#text(font: "DejaVu Sans Mono")[9]], [#text(font: "DejaVu Sans Mono")[20]], [#text(font: "DejaVu Sans Mono")[15]], [#text(font: "DejaVu Sans Mono")[14]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[2]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[14]], [#text(font: "DejaVu Sans Mono")[8]], [#text(font: "DejaVu Sans Mono")[44]], [#text(font: "DejaVu Sans Mono")[17]], [#text(font: "DejaVu Sans Mono")[6]], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[1]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[22]], [#text(font: "DejaVu Sans Mono")[27]], [#text(font: "DejaVu Sans Mono", weight: "bold")[159]], [#text(font: "DejaVu Sans Mono")[62]], [#text(font: "DejaVu Sans Mono", weight: "bold")[528]], [#text(font: "DejaVu Sans Mono")[95]], [#text(font: "DejaVu Sans Mono")[29]], [#text(font: "DejaVu Sans Mono")[4]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[3]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono", weight: "bold")[16,072]], [#text(font: "DejaVu Sans Mono", weight: "bold")[5,164]], [#text(font: "DejaVu Sans Mono", weight: "bold")[5,608]], [#text(font: "DejaVu Sans Mono", weight: "bold")[3,444]], [#text(font: "DejaVu Sans Mono", weight: "bold")[2,357]], [#text(font: "DejaVu Sans Mono", weight: "bold")[507]], [#text(font: "DejaVu Sans Mono", weight: "bold")[905]], [#text(font: "DejaVu Sans Mono")[75]], [#text(font: "DejaVu Sans Mono")[4]], [#text(font: "DejaVu Sans Mono", weight: "bold")[535]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,235]], [#text(font: "DejaVu Sans Mono", weight: "bold")[7,813]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,973]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,209]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,919]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,566]], [#text(font: "DejaVu Sans Mono", weight: "bold")[582]], [#text(font: "DejaVu Sans Mono")[34]], [#text(font: "DejaVu Sans Mono")[19]], [#text(font: "DejaVu Sans Mono", weight: "bold")[117]],
      [IRM], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono")[13]], [#text(font: "DejaVu Sans Mono")[10]], [#text(font: "DejaVu Sans Mono")[31]], [#text(font: "DejaVu Sans Mono")[28]], [#text(font: "DejaVu Sans Mono")[13]], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[26]], [#text(font: "DejaVu Sans Mono")[6]], [#text(font: "DejaVu Sans Mono")[69]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,295]], [#text(font: "DejaVu Sans Mono", weight: "bold")[282]], [#text(font: "DejaVu Sans Mono")[84]], [#text(font: "DejaVu Sans Mono")[61]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,174]], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono", weight: "bold")[307]],
)
#text(size: 7pt, fill: gray)[Author column: `anno\_orig\_state` (showing top 10 of 13 levels)]


#pagebreak()

== Blackburn2026 / seurat\_obj\_final

_Tech: 10x\_chromium, n\_cells: 73,888_
#text(fill: orange)[⚠ 43 pending columns not merged]

#grid(columns: (1fr, 1fr), gutter: 12pt,
[
=== QA flags
#table(
    columns: (auto, auto),
    inset: 4pt,
    align: (left, left),
    stroke: 0.5pt + gray,
      [Cells], [#text(font: "DejaVu Sans Mono")[73,888]],
      [Gene overlap], [#text(font: "DejaVu Sans Mono")[2,724 / 2,727]],
      [Median residual], [#text(font: "DejaVu Sans Mono")[2.408]],
      [WholeCell% (high)], [#text(font: "DejaVu Sans Mono")[17.8%]],
      [DC% (\> 0.05)], [#text(font: "DejaVu Sans Mono")[61.1% ⚠ DC contamination]],
)

],
[
=== Identity\_Label distribution
#table(
    columns: (auto, auto, auto),
    inset: 3pt,
    align: (left, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*%*]),
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[54,316]], [#text(font: "DejaVu Sans Mono")[73.5%]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[8,655]], [#text(font: "DejaVu Sans Mono")[11.7%]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[4,845]], [#text(font: "DejaVu Sans Mono")[6.6%]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[1,982]], [#text(font: "DejaVu Sans Mono")[2.7%]],
      [IRM], [#text(font: "DejaVu Sans Mono")[1,187]], [#text(font: "DejaVu Sans Mono")[1.6%]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[1,116]], [#text(font: "DejaVu Sans Mono")[1.5%]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[601]], [#text(font: "DejaVu Sans Mono")[0.8%]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[580]], [#text(font: "DejaVu Sans Mono")[0.8%]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[330]], [#text(font: "DejaVu Sans Mono")[0.4%]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono")[203]], [#text(font: "DejaVu Sans Mono")[0.3%]],
      [_other (3)_], [#text(font: "DejaVu Sans Mono")[73]], [#text(font: "DejaVu Sans Mono")[0.1%]],
)

]
)

=== Author state scores by starcat label
_For each starcat-assigned Identity\_Label, mean of the author's matching state-score columns. Diagonals (e.g. row=DAM, col=DAM) should be high._

#table(
    columns: (auto, auto, auto, auto, auto, auto, auto, auto, auto, auto, auto),
    inset: 3pt,
    align: (left, right, right, right, right, right, right, right, right, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*HM*], [*DAM*], [*IRM*], [*HLA*], [*CRM*], [*RM*], [*prolif*], [*DC*], [*macro*]),
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[54,316]], [#text(font: "DejaVu Sans Mono")[0.74]], [#text(font: "DejaVu Sans Mono")[0.28]], [#text(font: "DejaVu Sans Mono")[0.09]], [#text(font: "DejaVu Sans Mono")[0.82]], [#text(font: "DejaVu Sans Mono")[0.26]], [#text(font: "DejaVu Sans Mono")[0.69]], [#text(font: "DejaVu Sans Mono")[-0.05]], [#text(font: "DejaVu Sans Mono")[-0.01]], [#text(font: "DejaVu Sans Mono")[0.00]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[8,655]], [#text(font: "DejaVu Sans Mono")[0.65]], [#text(font: "DejaVu Sans Mono")[0.35]], [#text(font: "DejaVu Sans Mono")[0.09]], [#text(font: "DejaVu Sans Mono")[0.83]], [#text(font: "DejaVu Sans Mono")[0.31]], [#text(font: "DejaVu Sans Mono")[0.63]], [#text(font: "DejaVu Sans Mono")[-0.04]], [#text(font: "DejaVu Sans Mono")[-0.08]], [#text(font: "DejaVu Sans Mono")[-0.13]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[4,845]], [#text(font: "DejaVu Sans Mono")[0.35]], [#text(font: "DejaVu Sans Mono")[0.77]], [#text(font: "DejaVu Sans Mono")[0.09]], [#text(font: "DejaVu Sans Mono")[1.08]], [#text(font: "DejaVu Sans Mono")[0.32]], [#text(font: "DejaVu Sans Mono")[1.04]], [#text(font: "DejaVu Sans Mono")[0.07]], [#text(font: "DejaVu Sans Mono")[-0.07]], [#text(font: "DejaVu Sans Mono")[-0.17]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[1,982]], [#text(font: "DejaVu Sans Mono")[0.49]], [#text(font: "DejaVu Sans Mono")[0.47]], [#text(font: "DejaVu Sans Mono")[0.12]], [#text(font: "DejaVu Sans Mono")[1.22]], [#text(font: "DejaVu Sans Mono")[0.40]], [#text(font: "DejaVu Sans Mono")[1.08]], [#text(font: "DejaVu Sans Mono")[0.01]], [#text(font: "DejaVu Sans Mono")[-0.04]], [#text(font: "DejaVu Sans Mono")[-0.13]],
      [IRM], [#text(font: "DejaVu Sans Mono")[1,187]], [#text(font: "DejaVu Sans Mono")[0.51]], [#text(font: "DejaVu Sans Mono")[0.37]], [#text(font: "DejaVu Sans Mono")[0.72]], [#text(font: "DejaVu Sans Mono")[0.90]], [#text(font: "DejaVu Sans Mono")[0.41]], [#text(font: "DejaVu Sans Mono")[0.80]], [#text(font: "DejaVu Sans Mono")[-0.03]], [#text(font: "DejaVu Sans Mono")[-0.04]], [#text(font: "DejaVu Sans Mono")[0.06]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[1,116]], [#text(font: "DejaVu Sans Mono")[0.47]], [#text(font: "DejaVu Sans Mono")[0.39]], [#text(font: "DejaVu Sans Mono")[0.10]], [#text(font: "DejaVu Sans Mono")[1.17]], [#text(font: "DejaVu Sans Mono")[0.40]], [#text(font: "DejaVu Sans Mono")[1.19]], [#text(font: "DejaVu Sans Mono")[0.03]], [#text(font: "DejaVu Sans Mono")[0.19]], [#text(font: "DejaVu Sans Mono")[-0.10]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[601]], [#text(font: "DejaVu Sans Mono")[0.45]], [#text(font: "DejaVu Sans Mono")[0.38]], [#text(font: "DejaVu Sans Mono")[0.11]], [#text(font: "DejaVu Sans Mono")[1.28]], [#text(font: "DejaVu Sans Mono")[0.30]], [#text(font: "DejaVu Sans Mono")[1.35]], [#text(font: "DejaVu Sans Mono")[0.03]], [#text(font: "DejaVu Sans Mono")[0.21]], [#text(font: "DejaVu Sans Mono")[-0.11]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[580]], [#text(font: "DejaVu Sans Mono")[0.50]], [#text(font: "DejaVu Sans Mono")[0.28]], [#text(font: "DejaVu Sans Mono")[0.12]], [#text(font: "DejaVu Sans Mono")[0.82]], [#text(font: "DejaVu Sans Mono")[0.17]], [#text(font: "DejaVu Sans Mono")[0.74]], [#text(font: "DejaVu Sans Mono")[0.76]], [#text(font: "DejaVu Sans Mono")[-0.05]], [#text(font: "DejaVu Sans Mono")[-0.07]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[330]], [#text(font: "DejaVu Sans Mono")[0.49]], [#text(font: "DejaVu Sans Mono")[0.38]], [#text(font: "DejaVu Sans Mono")[0.15]], [#text(font: "DejaVu Sans Mono")[0.99]], [#text(font: "DejaVu Sans Mono")[0.38]], [#text(font: "DejaVu Sans Mono")[0.93]], [#text(font: "DejaVu Sans Mono")[0.06]], [#text(font: "DejaVu Sans Mono")[0.10]], [#text(font: "DejaVu Sans Mono")[0.36]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono")[203]], [#text(font: "DejaVu Sans Mono")[0.36]], [#text(font: "DejaVu Sans Mono")[0.37]], [#text(font: "DejaVu Sans Mono")[0.13]], [#text(font: "DejaVu Sans Mono")[1.34]], [#text(font: "DejaVu Sans Mono")[0.42]], [#text(font: "DejaVu Sans Mono")[1.34]], [#text(font: "DejaVu Sans Mono")[0.03]], [#text(font: "DejaVu Sans Mono")[0.39]], [#text(font: "DejaVu Sans Mono")[-0.14]],
)
#text(size: 7pt, fill: gray)[Author column mapping: HM→`HM8`, DAM→`DAM6`, IRM→`IRM9`, HLA→`HLA7`, CRM→`CRM5`, RM→`RM10`, prolif→`proliferating2`, DC→`dendritic1`, macro→`macrophage3`]


=== Author label cross-tab
_Cell counts: rows = starcat Identity\_Label, columns = author's `seurat\_clusters`. Concentration along rows = agreement._

#table(
    columns: (auto, auto, auto, auto, auto, auto, auto, auto, auto, auto),
    inset: 3pt,
    align: (left, right, right, right, right, right, right, right, right, right),
    stroke: 0.5pt + gray,
    table.header([*starcat label*], [*0*], [*1*], [*2*], [*3*], [*4*], [*5*], [*6*], [*7*], [*8*]),
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[34]], [#text(font: "DejaVu Sans Mono")[44]], [#text(font: "DejaVu Sans Mono")[82]], [#text(font: "DejaVu Sans Mono")[66]], [#text(font: "DejaVu Sans Mono")[25]], [#text(font: "DejaVu Sans Mono")[27]], [#text(font: "DejaVu Sans Mono")[29]], [#text(font: "DejaVu Sans Mono")[5]], [#text(font: "DejaVu Sans Mono")[18]],
      [Cycling], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono", weight: "bold")[576]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DAM], [#text(font: "DejaVu Sans Mono")[4]], [#text(font: "DejaVu Sans Mono")[6]], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[31]], [#text(font: "DejaVu Sans Mono")[5]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_Mature], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[2]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_cDC1], [#text(font: "DejaVu Sans Mono")[7]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[4]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono", weight: "bold")[117]], [#text(font: "DejaVu Sans Mono")[43]], [#text(font: "DejaVu Sans Mono")[35]], [#text(font: "DejaVu Sans Mono")[79]], [#text(font: "DejaVu Sans Mono")[25]], [#text(font: "DejaVu Sans Mono", weight: "bold")[284]], [#text(font: "DejaVu Sans Mono")[10]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[8]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono", weight: "bold")[101]], [#text(font: "DejaVu Sans Mono", weight: "bold")[224]], [#text(font: "DejaVu Sans Mono", weight: "bold")[120]], [#text(font: "DejaVu Sans Mono")[70]], [#text(font: "DejaVu Sans Mono")[40]], [#text(font: "DejaVu Sans Mono", weight: "bold")[484]], [#text(font: "DejaVu Sans Mono")[55]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[22]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono", weight: "bold")[136]], [#text(font: "DejaVu Sans Mono", weight: "bold")[255]], [#text(font: "DejaVu Sans Mono", weight: "bold")[213]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,103]], [#text(font: "DejaVu Sans Mono")[88]], [#text(font: "DejaVu Sans Mono", weight: "bold")[125]], [#text(font: "DejaVu Sans Mono")[7]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[55]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono", weight: "bold")[2,382]], [#text(font: "DejaVu Sans Mono", weight: "bold")[2,069]], [#text(font: "DejaVu Sans Mono", weight: "bold")[813]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,846]], [#text(font: "DejaVu Sans Mono", weight: "bold")[558]], [#text(font: "DejaVu Sans Mono", weight: "bold")[326]], [#text(font: "DejaVu Sans Mono", weight: "bold")[185]], [#text(font: "DejaVu Sans Mono", weight: "bold")[371]], [#text(font: "DejaVu Sans Mono", weight: "bold")[105]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono", weight: "bold")[20,744]], [#text(font: "DejaVu Sans Mono", weight: "bold")[13,377]], [#text(font: "DejaVu Sans Mono", weight: "bold")[8,288]], [#text(font: "DejaVu Sans Mono", weight: "bold")[2,198]], [#text(font: "DejaVu Sans Mono", weight: "bold")[3,916]], [#text(font: "DejaVu Sans Mono", weight: "bold")[2,652]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,050]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,474]], [#text(font: "DejaVu Sans Mono", weight: "bold")[617]],
      [IRM], [#text(font: "DejaVu Sans Mono")[7]], [#text(font: "DejaVu Sans Mono")[39]], [#text(font: "DejaVu Sans Mono")[38]], [#text(font: "DejaVu Sans Mono", weight: "bold")[107]], [#text(font: "DejaVu Sans Mono", weight: "bold")[929]], [#text(font: "DejaVu Sans Mono")[49]], [#text(font: "DejaVu Sans Mono")[12]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[5]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[79]], [#text(font: "DejaVu Sans Mono", weight: "bold")[180]], [#text(font: "DejaVu Sans Mono", weight: "bold")[507]], [#text(font: "DejaVu Sans Mono", weight: "bold")[3,868]], [#text(font: "DejaVu Sans Mono")[59]], [#text(font: "DejaVu Sans Mono")[57]], [#text(font: "DejaVu Sans Mono")[9]], [#text(font: "DejaVu Sans Mono")[10]], [#text(font: "DejaVu Sans Mono")[76]],
)
#text(size: 7pt, fill: gray)[Author column: `seurat\_clusters`]


#pagebreak()

== Blackburn2026 / trisomy\_seurat\_obj\_w\_umap

_Tech: 10x\_chromium, n\_cells: 73,888_

#grid(columns: (1fr, 1fr), gutter: 12pt,
[
=== QA flags
#table(
    columns: (auto, auto),
    inset: 4pt,
    align: (left, left),
    stroke: 0.5pt + gray,
      [Cells], [#text(font: "DejaVu Sans Mono")[73,888]],
      [Gene overlap], [#text(font: "DejaVu Sans Mono")[2,700 / 2,727]],
      [Median residual], [#text(font: "DejaVu Sans Mono")[3.256]],
      [WholeCell% (high)], [#text(font: "DejaVu Sans Mono")[21.2%]],
      [DC% (\> 0.05)], [#text(font: "DejaVu Sans Mono")[54.0% ⚠ DC contamination]],
)

],
[
=== Identity\_Label distribution
#table(
    columns: (auto, auto, auto),
    inset: 3pt,
    align: (left, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*%*]),
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[52,810]], [#text(font: "DejaVu Sans Mono")[71.5%]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[8,762]], [#text(font: "DejaVu Sans Mono")[11.9%]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[5,418]], [#text(font: "DejaVu Sans Mono")[7.3%]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[2,452]], [#text(font: "DejaVu Sans Mono")[3.3%]],
      [IRM], [#text(font: "DejaVu Sans Mono")[1,379]], [#text(font: "DejaVu Sans Mono")[1.9%]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[1,125]], [#text(font: "DejaVu Sans Mono")[1.5%]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[707]], [#text(font: "DejaVu Sans Mono")[1.0%]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[601]], [#text(font: "DejaVu Sans Mono")[0.8%]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[294]], [#text(font: "DejaVu Sans Mono")[0.4%]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono")[267]], [#text(font: "DejaVu Sans Mono")[0.4%]],
      [_other (3)_], [#text(font: "DejaVu Sans Mono")[73]], [#text(font: "DejaVu Sans Mono")[0.1%]],
)

]
)

=== Author state scores by starcat label
_For each starcat-assigned Identity\_Label, mean of the author's matching state-score columns. Diagonals (e.g. row=DAM, col=DAM) should be high._

#table(
    columns: (auto, auto, auto, auto, auto, auto, auto, auto, auto, auto, auto),
    inset: 3pt,
    align: (left, right, right, right, right, right, right, right, right, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*HM*], [*DAM*], [*IRM*], [*HLA*], [*CRM*], [*RM*], [*prolif*], [*DC*], [*macro*]),
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[52,810]], [#text(font: "DejaVu Sans Mono")[0.74]], [#text(font: "DejaVu Sans Mono")[0.28]], [#text(font: "DejaVu Sans Mono")[0.08]], [#text(font: "DejaVu Sans Mono")[0.82]], [#text(font: "DejaVu Sans Mono")[0.26]], [#text(font: "DejaVu Sans Mono")[0.69]], [#text(font: "DejaVu Sans Mono")[-0.05]], [#text(font: "DejaVu Sans Mono")[-0.01]], [#text(font: "DejaVu Sans Mono")[0.00]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[8,762]], [#text(font: "DejaVu Sans Mono")[0.66]], [#text(font: "DejaVu Sans Mono")[0.34]], [#text(font: "DejaVu Sans Mono")[0.09]], [#text(font: "DejaVu Sans Mono")[0.82]], [#text(font: "DejaVu Sans Mono")[0.30]], [#text(font: "DejaVu Sans Mono")[0.62]], [#text(font: "DejaVu Sans Mono")[-0.05]], [#text(font: "DejaVu Sans Mono")[-0.08]], [#text(font: "DejaVu Sans Mono")[-0.13]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[5,418]], [#text(font: "DejaVu Sans Mono")[0.37]], [#text(font: "DejaVu Sans Mono")[0.76]], [#text(font: "DejaVu Sans Mono")[0.09]], [#text(font: "DejaVu Sans Mono")[1.07]], [#text(font: "DejaVu Sans Mono")[0.32]], [#text(font: "DejaVu Sans Mono")[1.03]], [#text(font: "DejaVu Sans Mono")[0.06]], [#text(font: "DejaVu Sans Mono")[-0.07]], [#text(font: "DejaVu Sans Mono")[-0.17]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[2,452]], [#text(font: "DejaVu Sans Mono")[0.51]], [#text(font: "DejaVu Sans Mono")[0.46]], [#text(font: "DejaVu Sans Mono")[0.12]], [#text(font: "DejaVu Sans Mono")[1.19]], [#text(font: "DejaVu Sans Mono")[0.40]], [#text(font: "DejaVu Sans Mono")[1.05]], [#text(font: "DejaVu Sans Mono")[0.01]], [#text(font: "DejaVu Sans Mono")[-0.04]], [#text(font: "DejaVu Sans Mono")[-0.12]],
      [IRM], [#text(font: "DejaVu Sans Mono")[1,379]], [#text(font: "DejaVu Sans Mono")[0.53]], [#text(font: "DejaVu Sans Mono")[0.36]], [#text(font: "DejaVu Sans Mono")[0.70]], [#text(font: "DejaVu Sans Mono")[0.89]], [#text(font: "DejaVu Sans Mono")[0.40]], [#text(font: "DejaVu Sans Mono")[0.77]], [#text(font: "DejaVu Sans Mono")[-0.03]], [#text(font: "DejaVu Sans Mono")[-0.04]], [#text(font: "DejaVu Sans Mono")[0.07]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[1,125]], [#text(font: "DejaVu Sans Mono")[0.49]], [#text(font: "DejaVu Sans Mono")[0.38]], [#text(font: "DejaVu Sans Mono")[0.10]], [#text(font: "DejaVu Sans Mono")[1.15]], [#text(font: "DejaVu Sans Mono")[0.41]], [#text(font: "DejaVu Sans Mono")[1.16]], [#text(font: "DejaVu Sans Mono")[0.02]], [#text(font: "DejaVu Sans Mono")[0.20]], [#text(font: "DejaVu Sans Mono")[-0.11]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[707]], [#text(font: "DejaVu Sans Mono")[0.53]], [#text(font: "DejaVu Sans Mono")[0.29]], [#text(font: "DejaVu Sans Mono")[0.12]], [#text(font: "DejaVu Sans Mono")[0.81]], [#text(font: "DejaVu Sans Mono")[0.18]], [#text(font: "DejaVu Sans Mono")[0.72]], [#text(font: "DejaVu Sans Mono")[0.72]], [#text(font: "DejaVu Sans Mono")[-0.05]], [#text(font: "DejaVu Sans Mono")[-0.07]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[601]], [#text(font: "DejaVu Sans Mono")[0.48]], [#text(font: "DejaVu Sans Mono")[0.37]], [#text(font: "DejaVu Sans Mono")[0.11]], [#text(font: "DejaVu Sans Mono")[1.25]], [#text(font: "DejaVu Sans Mono")[0.32]], [#text(font: "DejaVu Sans Mono")[1.29]], [#text(font: "DejaVu Sans Mono")[0.02]], [#text(font: "DejaVu Sans Mono")[0.22]], [#text(font: "DejaVu Sans Mono")[-0.11]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[294]], [#text(font: "DejaVu Sans Mono")[0.51]], [#text(font: "DejaVu Sans Mono")[0.39]], [#text(font: "DejaVu Sans Mono")[0.14]], [#text(font: "DejaVu Sans Mono")[1.00]], [#text(font: "DejaVu Sans Mono")[0.39]], [#text(font: "DejaVu Sans Mono")[0.93]], [#text(font: "DejaVu Sans Mono")[0.06]], [#text(font: "DejaVu Sans Mono")[0.10]], [#text(font: "DejaVu Sans Mono")[0.39]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono")[267]], [#text(font: "DejaVu Sans Mono")[0.37]], [#text(font: "DejaVu Sans Mono")[0.37]], [#text(font: "DejaVu Sans Mono")[0.13]], [#text(font: "DejaVu Sans Mono")[1.36]], [#text(font: "DejaVu Sans Mono")[0.42]], [#text(font: "DejaVu Sans Mono")[1.37]], [#text(font: "DejaVu Sans Mono")[0.02]], [#text(font: "DejaVu Sans Mono")[0.44]], [#text(font: "DejaVu Sans Mono")[-0.16]],
)
#text(size: 7pt, fill: gray)[Author column mapping: HM→`HM8`, DAM→`DAM6`, IRM→`IRM9`, HLA→`HLA7`, CRM→`CRM5`, RM→`RM10`, prolif→`proliferating2`, DC→`dendritic1`, macro→`macrophage3`]


=== Author label cross-tab
_Cell counts: rows = starcat Identity\_Label, columns = author's `seurat\_clusters`. Concentration along rows = agreement._

#table(
    columns: (auto, auto, auto, auto, auto, auto, auto, auto, auto, auto),
    inset: 3pt,
    align: (left, right, right, right, right, right, right, right, right, right),
    stroke: 0.5pt + gray,
    table.header([*starcat label*], [*0*], [*1*], [*2*], [*3*], [*4*], [*5*], [*6*], [*7*], [*8*]),
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[31]], [#text(font: "DejaVu Sans Mono")[32]], [#text(font: "DejaVu Sans Mono")[79]], [#text(font: "DejaVu Sans Mono")[57]], [#text(font: "DejaVu Sans Mono")[21]], [#text(font: "DejaVu Sans Mono")[28]], [#text(font: "DejaVu Sans Mono")[27]], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono")[17]],
      [Cycling], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[3]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono", weight: "bold")[700]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[1]],
      [DAM], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono")[7]], [#text(font: "DejaVu Sans Mono")[4]], [#text(font: "DejaVu Sans Mono")[25]], [#text(font: "DejaVu Sans Mono")[5]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[2]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[1]],
      [DC\_Mature], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_cDC1], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[4]], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[11]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[90]], [#text(font: "DejaVu Sans Mono")[57]], [#text(font: "DejaVu Sans Mono")[43]], [#text(font: "DejaVu Sans Mono")[71]], [#text(font: "DejaVu Sans Mono")[29]], [#text(font: "DejaVu Sans Mono", weight: "bold")[296]], [#text(font: "DejaVu Sans Mono")[8]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[7]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[91]], [#text(font: "DejaVu Sans Mono", weight: "bold")[227]], [#text(font: "DejaVu Sans Mono")[98]], [#text(font: "DejaVu Sans Mono")[47]], [#text(font: "DejaVu Sans Mono")[34]], [#text(font: "DejaVu Sans Mono", weight: "bold")[548]], [#text(font: "DejaVu Sans Mono")[55]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[25]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono", weight: "bold")[186]], [#text(font: "DejaVu Sans Mono", weight: "bold")[350]], [#text(font: "DejaVu Sans Mono", weight: "bold")[281]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,277]], [#text(font: "DejaVu Sans Mono", weight: "bold")[118]], [#text(font: "DejaVu Sans Mono", weight: "bold")[165]], [#text(font: "DejaVu Sans Mono")[9]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[66]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono", weight: "bold")[2,547]], [#text(font: "DejaVu Sans Mono", weight: "bold")[2,169]], [#text(font: "DejaVu Sans Mono", weight: "bold")[824]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,720]], [#text(font: "DejaVu Sans Mono", weight: "bold")[559]], [#text(font: "DejaVu Sans Mono", weight: "bold")[322]], [#text(font: "DejaVu Sans Mono", weight: "bold")[166]], [#text(font: "DejaVu Sans Mono", weight: "bold")[357]], [#text(font: "DejaVu Sans Mono")[98]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono", weight: "bold")[20,536]], [#text(font: "DejaVu Sans Mono", weight: "bold")[13,083]], [#text(font: "DejaVu Sans Mono", weight: "bold")[8,113]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,882]], [#text(font: "DejaVu Sans Mono", weight: "bold")[3,740]], [#text(font: "DejaVu Sans Mono", weight: "bold")[2,433]], [#text(font: "DejaVu Sans Mono", weight: "bold")[938]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,493]], [#text(font: "DejaVu Sans Mono", weight: "bold")[592]],
      [IRM], [#text(font: "DejaVu Sans Mono")[7]], [#text(font: "DejaVu Sans Mono")[67]], [#text(font: "DejaVu Sans Mono")[45]], [#text(font: "DejaVu Sans Mono", weight: "bold")[113]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,064]], [#text(font: "DejaVu Sans Mono")[62]], [#text(font: "DejaVu Sans Mono")[15]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[6]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono", weight: "bold")[123]], [#text(font: "DejaVu Sans Mono", weight: "bold")[242]], [#text(font: "DejaVu Sans Mono", weight: "bold")[607]], [#text(font: "DejaVu Sans Mono", weight: "bold")[4,178]], [#text(font: "DejaVu Sans Mono")[77]], [#text(font: "DejaVu Sans Mono")[75]], [#text(font: "DejaVu Sans Mono")[12]], [#text(font: "DejaVu Sans Mono")[11]], [#text(font: "DejaVu Sans Mono")[93]],
)
#text(size: 7pt, fill: gray)[Author column: `seurat\_clusters`]


#pagebreak()

== Borrie2025 / LOF.sc.teset.counts

_Tech: 10x\_chromium, n\_cells: 77,684_
#text(fill: orange)[⚠ 43 pending columns not merged]

#grid(columns: (1fr, 1fr), gutter: 12pt,
[
=== QA flags
#table(
    columns: (auto, auto),
    inset: 4pt,
    align: (left, left),
    stroke: 0.5pt + gray,
      [Cells], [#text(font: "DejaVu Sans Mono")[77,684]],
      [Gene overlap], [#text(font: "DejaVu Sans Mono")[2,058 / 2,727]],
      [Median residual], [#text(font: "DejaVu Sans Mono")[2.353]],
      [WholeCell% (high)], [#text(font: "DejaVu Sans Mono")[39.8% ⚠ likely whole-cell]],
      [DC% (\> 0.05)], [#text(font: "DejaVu Sans Mono")[54.8% ⚠ DC contamination]],
)

],
[
=== Identity\_Label distribution
#table(
    columns: (auto, auto, auto),
    inset: 3pt,
    align: (left, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*%*]),
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[40,891]], [#text(font: "DejaVu Sans Mono")[52.6%]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[12,580]], [#text(font: "DejaVu Sans Mono")[16.2%]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[5,729]], [#text(font: "DejaVu Sans Mono")[7.4%]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[5,142]], [#text(font: "DejaVu Sans Mono")[6.6%]],
      [IRM], [#text(font: "DejaVu Sans Mono")[4,912]], [#text(font: "DejaVu Sans Mono")[6.3%]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[3,089]], [#text(font: "DejaVu Sans Mono")[4.0%]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[1,785]], [#text(font: "DejaVu Sans Mono")[2.3%]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[1,732]], [#text(font: "DejaVu Sans Mono")[2.2%]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[587]], [#text(font: "DejaVu Sans Mono")[0.8%]],
      [DAM], [#text(font: "DejaVu Sans Mono")[467]], [#text(font: "DejaVu Sans Mono")[0.6%]],
      [_other (3)_], [#text(font: "DejaVu Sans Mono")[770]], [#text(font: "DejaVu Sans Mono")[1.0%]],
)

]
)

_No comparable author state-scores or label columns detected in this dataset; starcat annotation stands on its own._

#pagebreak()

== GreenFujita2024 / microglia

_Tech: 10x\_chromium, n\_cells: 86,612_
#text(fill: orange)[⚠ 43 pending columns not merged]

#grid(columns: (1fr, 1fr), gutter: 12pt,
[
=== QA flags
#table(
    columns: (auto, auto),
    inset: 4pt,
    align: (left, left),
    stroke: 0.5pt + gray,
      [Cells], [#text(font: "DejaVu Sans Mono")[86,612]],
      [Gene overlap], [#text(font: "DejaVu Sans Mono")[2,727 / 2,727]],
      [Median residual], [#text(font: "DejaVu Sans Mono")[1.229]],
      [WholeCell% (high)], [#text(font: "DejaVu Sans Mono")[0.0%]],
      [DC% (\> 0.05)], [#text(font: "DejaVu Sans Mono")[63.4% ⚠ DC contamination]],
)

],
[
=== Identity\_Label distribution
#table(
    columns: (auto, auto, auto),
    inset: 3pt,
    align: (left, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*%*]),
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[54,864]], [#text(font: "DejaVu Sans Mono")[63.3%]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[10,135]], [#text(font: "DejaVu Sans Mono")[11.7%]],
      [DAM], [#text(font: "DejaVu Sans Mono")[5,309]], [#text(font: "DejaVu Sans Mono")[6.1%]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[4,892]], [#text(font: "DejaVu Sans Mono")[5.6%]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[4,710]], [#text(font: "DejaVu Sans Mono")[5.4%]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[2,677]], [#text(font: "DejaVu Sans Mono")[3.1%]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono")[1,143]], [#text(font: "DejaVu Sans Mono")[1.3%]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[863]], [#text(font: "DejaVu Sans Mono")[1.0%]],
      [DC\_cDC1], [#text(font: "DejaVu Sans Mono")[666]], [#text(font: "DejaVu Sans Mono")[0.8%]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[545]], [#text(font: "DejaVu Sans Mono")[0.6%]],
      [_other (3)_], [#text(font: "DejaVu Sans Mono")[808]], [#text(font: "DejaVu Sans Mono")[0.9%]],
)

]
)

=== Author label cross-tab
_Cell counts: rows = starcat Identity\_Label, columns = author's `state`. Concentration along rows = agreement._

#table(
    columns: (auto, auto, auto, auto, auto, auto, auto, auto, auto, auto, auto),
    inset: 3pt,
    align: (left, right, right, right, right, right, right, right, right, right, right),
    stroke: 0.5pt + gray,
    table.header([*starcat label*], [*Mic.2*], [*Mic.7*], [*Mic.5*], [*Mic.3*], [*Mic.6*], [*Mic.8*], [*Mic.4*], [*Mic.12*], [*Mic.10*], [*Mic.9*]),
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[86]], [#text(font: "DejaVu Sans Mono", weight: "bold")[338]], [#text(font: "DejaVu Sans Mono", weight: "bold")[419]], [#text(font: "DejaVu Sans Mono")[43]], [#text(font: "DejaVu Sans Mono", weight: "bold")[688]], [#text(font: "DejaVu Sans Mono", weight: "bold")[107]], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono", weight: "bold")[943]], [#text(font: "DejaVu Sans Mono", weight: "bold")[129]], [#text(font: "DejaVu Sans Mono")[76]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[3]], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono")[5]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[11]], [#text(font: "DejaVu Sans Mono")[3]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[12]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DAM], [#text(font: "DejaVu Sans Mono")[55]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,044]], [#text(font: "DejaVu Sans Mono", weight: "bold")[374]], [#text(font: "DejaVu Sans Mono")[32]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,660]], [#text(font: "DejaVu Sans Mono", weight: "bold")[364]], [#text(font: "DejaVu Sans Mono")[11]], [#text(font: "DejaVu Sans Mono", weight: "bold")[825]], [#text(font: "DejaVu Sans Mono", weight: "bold")[473]], [#text(font: "DejaVu Sans Mono")[54]],
      [DC\_Mature], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[28]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[2]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_cDC1], [#text(font: "DejaVu Sans Mono")[30]], [#text(font: "DejaVu Sans Mono", weight: "bold")[157]], [#text(font: "DejaVu Sans Mono", weight: "bold")[139]], [#text(font: "DejaVu Sans Mono")[27]], [#text(font: "DejaVu Sans Mono", weight: "bold")[154]], [#text(font: "DejaVu Sans Mono")[30]], [#text(font: "DejaVu Sans Mono")[6]], [#text(font: "DejaVu Sans Mono")[12]], [#text(font: "DejaVu Sans Mono")[49]], [#text(font: "DejaVu Sans Mono")[16]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[23]], [#text(font: "DejaVu Sans Mono")[66]], [#text(font: "DejaVu Sans Mono")[42]], [#text(font: "DejaVu Sans Mono")[14]], [#text(font: "DejaVu Sans Mono")[80]], [#text(font: "DejaVu Sans Mono")[16]], [#text(font: "DejaVu Sans Mono")[4]], [#text(font: "DejaVu Sans Mono")[18]], [#text(font: "DejaVu Sans Mono")[43]], [#text(font: "DejaVu Sans Mono")[18]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono", weight: "bold")[147]], [#text(font: "DejaVu Sans Mono", weight: "bold")[480]], [#text(font: "DejaVu Sans Mono", weight: "bold")[828]], [#text(font: "DejaVu Sans Mono", weight: "bold")[132]], [#text(font: "DejaVu Sans Mono", weight: "bold")[475]], [#text(font: "DejaVu Sans Mono", weight: "bold")[108]], [#text(font: "DejaVu Sans Mono")[19]], [#text(font: "DejaVu Sans Mono")[70]], [#text(font: "DejaVu Sans Mono", weight: "bold")[125]], [#text(font: "DejaVu Sans Mono")[79]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[45]], [#text(font: "DejaVu Sans Mono", weight: "bold")[103]], [#text(font: "DejaVu Sans Mono")[40]], [#text(font: "DejaVu Sans Mono")[26]], [#text(font: "DejaVu Sans Mono", weight: "bold")[132]], [#text(font: "DejaVu Sans Mono")[66]], [#text(font: "DejaVu Sans Mono")[6]], [#text(font: "DejaVu Sans Mono")[20]], [#text(font: "DejaVu Sans Mono")[56]], [#text(font: "DejaVu Sans Mono")[33]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono", weight: "bold")[11,199]], [#text(font: "DejaVu Sans Mono", weight: "bold")[8,521]], [#text(font: "DejaVu Sans Mono", weight: "bold")[7,026]], [#text(font: "DejaVu Sans Mono", weight: "bold")[7,027]], [#text(font: "DejaVu Sans Mono", weight: "bold")[2,831]], [#text(font: "DejaVu Sans Mono", weight: "bold")[4,190]], [#text(font: "DejaVu Sans Mono", weight: "bold")[4,391]], [#text(font: "DejaVu Sans Mono", weight: "bold")[575]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,961]], [#text(font: "DejaVu Sans Mono", weight: "bold")[2,733]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,937]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,808]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,141]], [#text(font: "DejaVu Sans Mono", weight: "bold")[941]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,279]], [#text(font: "DejaVu Sans Mono", weight: "bold")[763]], [#text(font: "DejaVu Sans Mono", weight: "bold")[433]], [#text(font: "DejaVu Sans Mono", weight: "bold")[162]], [#text(font: "DejaVu Sans Mono", weight: "bold")[485]], [#text(font: "DejaVu Sans Mono", weight: "bold")[404]],
      [IRM], [#text(font: "DejaVu Sans Mono")[12]], [#text(font: "DejaVu Sans Mono")[25]], [#text(font: "DejaVu Sans Mono")[25]], [#text(font: "DejaVu Sans Mono")[16]], [#text(font: "DejaVu Sans Mono", weight: "bold")[109]], [#text(font: "DejaVu Sans Mono")[6]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[26]], [#text(font: "DejaVu Sans Mono")[46]], [#text(font: "DejaVu Sans Mono")[15]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono", weight: "bold")[179]], [#text(font: "DejaVu Sans Mono", weight: "bold")[284]], [#text(font: "DejaVu Sans Mono", weight: "bold")[388]], [#text(font: "DejaVu Sans Mono")[70]], [#text(font: "DejaVu Sans Mono", weight: "bold")[314]], [#text(font: "DejaVu Sans Mono")[85]], [#text(font: "DejaVu Sans Mono")[31]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,211]], [#text(font: "DejaVu Sans Mono", weight: "bold")[192]], [#text(font: "DejaVu Sans Mono", weight: "bold")[104]],
)
#text(size: 7pt, fill: gray)[Author column: `state` (showing top 10 of 18 levels)]


#pagebreak()

== Blackburn2026 / seurat\_obj\_trisomy\_initial

_Tech: 10x\_chromium, n\_cells: 88,582_
#text(fill: orange)[⚠ 43 pending columns not merged]

#grid(columns: (1fr, 1fr), gutter: 12pt,
[
=== QA flags
#table(
    columns: (auto, auto),
    inset: 4pt,
    align: (left, left),
    stroke: 0.5pt + gray,
      [Cells], [#text(font: "DejaVu Sans Mono")[88,582]],
      [Gene overlap], [#text(font: "DejaVu Sans Mono")[2,724 / 2,727]],
      [Median residual], [#text(font: "DejaVu Sans Mono")[2.647]],
      [WholeCell% (high)], [#text(font: "DejaVu Sans Mono")[14.2%]],
      [DC% (\> 0.05)], [#text(font: "DejaVu Sans Mono")[72.1% ⚠ DC contamination]],
)

],
[
=== Identity\_Label distribution
#table(
    columns: (auto, auto, auto),
    inset: 3pt,
    align: (left, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*%*]),
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[60,326]], [#text(font: "DejaVu Sans Mono")[68.1%]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[9,482]], [#text(font: "DejaVu Sans Mono")[10.7%]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[5,769]], [#text(font: "DejaVu Sans Mono")[6.5%]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[4,441]], [#text(font: "DejaVu Sans Mono")[5.0%]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[2,502]], [#text(font: "DejaVu Sans Mono")[2.8%]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[2,070]], [#text(font: "DejaVu Sans Mono")[2.3%]],
      [IRM], [#text(font: "DejaVu Sans Mono")[1,551]], [#text(font: "DejaVu Sans Mono")[1.8%]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[1,250]], [#text(font: "DejaVu Sans Mono")[1.4%]],
      [Cycling], [#text(font: "DejaVu Sans Mono")[770]], [#text(font: "DejaVu Sans Mono")[0.9%]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono")[272]], [#text(font: "DejaVu Sans Mono")[0.3%]],
      [_other (3)_], [#text(font: "DejaVu Sans Mono")[149]], [#text(font: "DejaVu Sans Mono")[0.2%]],
)

]
)

_No comparable author state-scores or label columns detected in this dataset; starcat annotation stands on its own._

#pagebreak()

== Mathys2024 / multiregion\_microglia

_Tech: 10x\_chromium, n\_cells: 92,800_
#text(fill: orange)[⚠ 43 pending columns not merged]

#grid(columns: (1fr, 1fr), gutter: 12pt,
[
=== QA flags
#table(
    columns: (auto, auto),
    inset: 4pt,
    align: (left, left),
    stroke: 0.5pt + gray,
      [Cells], [#text(font: "DejaVu Sans Mono")[92,800]],
      [Gene overlap], [#text(font: "DejaVu Sans Mono")[2,727 / 2,727]],
      [Median residual], [#text(font: "DejaVu Sans Mono")[1.280]],
      [WholeCell% (high)], [#text(font: "DejaVu Sans Mono")[0.0%]],
      [DC% (\> 0.05)], [#text(font: "DejaVu Sans Mono")[50.4% ⚠ DC contamination]],
)

],
[
=== Identity\_Label distribution
#table(
    columns: (auto, auto, auto),
    inset: 3pt,
    align: (left, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*%*]),
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[60,234]], [#text(font: "DejaVu Sans Mono")[64.9%]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[12,921]], [#text(font: "DejaVu Sans Mono")[13.9%]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[5,683]], [#text(font: "DejaVu Sans Mono")[6.1%]],
      [DAM], [#text(font: "DejaVu Sans Mono")[4,915]], [#text(font: "DejaVu Sans Mono")[5.3%]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[2,846]], [#text(font: "DejaVu Sans Mono")[3.1%]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[1,465]], [#text(font: "DejaVu Sans Mono")[1.6%]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[1,281]], [#text(font: "DejaVu Sans Mono")[1.4%]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono")[1,024]], [#text(font: "DejaVu Sans Mono")[1.1%]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[633]], [#text(font: "DejaVu Sans Mono")[0.7%]],
      [DC\_cDC1], [#text(font: "DejaVu Sans Mono")[595]], [#text(font: "DejaVu Sans Mono")[0.6%]],
      [_other (3)_], [#text(font: "DejaVu Sans Mono")[1,203]], [#text(font: "DejaVu Sans Mono")[1.3%]],
)

]
)

=== Author label cross-tab
_Cell counts: rows = starcat Identity\_Label, columns = author's `cell\_type\_high\_resolution`. Concentration along rows = agreement._

#table(
    columns: (auto, auto, auto, auto, auto, auto, auto),
    inset: 3pt,
    align: (left, right, right, right, right, right, right),
    stroke: 0.5pt + gray,
    table.header([*starcat label*], [*Mic P2RY12*], [*Mic TPT1*], [*Mic DUSP1*], [*CAMs*], [*Mic MKI67*], [*T cells*]),
      [Border\_CAM], [#text(font: "DejaVu Sans Mono", weight: "bold")[2,152]], [#text(font: "DejaVu Sans Mono", weight: "bold")[214]], [#text(font: "DejaVu Sans Mono", weight: "bold")[170]], [#text(font: "DejaVu Sans Mono", weight: "bold")[305]], [#text(font: "DejaVu Sans Mono")[2]], [#text(font: "DejaVu Sans Mono")[3]],
      [Cycling], [#text(font: "DejaVu Sans Mono", weight: "bold")[235]], [#text(font: "DejaVu Sans Mono")[27]], [#text(font: "DejaVu Sans Mono")[53]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono", weight: "bold")[238]], [#text(font: "DejaVu Sans Mono")[1]],
      [DAM], [#text(font: "DejaVu Sans Mono", weight: "bold")[4,123]], [#text(font: "DejaVu Sans Mono", weight: "bold")[322]], [#text(font: "DejaVu Sans Mono", weight: "bold")[317]], [#text(font: "DejaVu Sans Mono", weight: "bold")[153]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_Mature], [#text(font: "DejaVu Sans Mono")[31]], [#text(font: "DejaVu Sans Mono")[6]], [#text(font: "DejaVu Sans Mono")[59]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_cDC1], [#text(font: "DejaVu Sans Mono", weight: "bold")[495]], [#text(font: "DejaVu Sans Mono")[42]], [#text(font: "DejaVu Sans Mono")[57]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono", weight: "bold")[526]], [#text(font: "DejaVu Sans Mono")[46]], [#text(font: "DejaVu Sans Mono")[55]], [#text(font: "DejaVu Sans Mono")[5]], [#text(font: "DejaVu Sans Mono")[1]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,216]], [#text(font: "DejaVu Sans Mono")[84]], [#text(font: "DejaVu Sans Mono", weight: "bold")[153]], [#text(font: "DejaVu Sans Mono")[3]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(font: "DejaVu Sans Mono")[9]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,030]], [#text(font: "DejaVu Sans Mono", weight: "bold")[160]], [#text(font: "DejaVu Sans Mono")[83]], [#text(font: "DejaVu Sans Mono")[8]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono", weight: "bold")[55,207]], [#text(font: "DejaVu Sans Mono", weight: "bold")[3,154]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,751]], [#text(font: "DejaVu Sans Mono")[63]], [#text(font: "DejaVu Sans Mono")[53]], [#text(font: "DejaVu Sans Mono")[6]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono", weight: "bold")[11,696]], [#text(font: "DejaVu Sans Mono", weight: "bold")[892]], [#text(font: "DejaVu Sans Mono", weight: "bold")[309]], [#text(font: "DejaVu Sans Mono")[9]], [#text(font: "DejaVu Sans Mono")[12]], [#text(font: "DejaVu Sans Mono")[3]],
      [IRM], [#text(font: "DejaVu Sans Mono", weight: "bold")[442]], [#text(font: "DejaVu Sans Mono")[58]], [#text(font: "DejaVu Sans Mono")[46]], [#text(font: "DejaVu Sans Mono")[7]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono", weight: "bold")[4,914]], [#text(font: "DejaVu Sans Mono", weight: "bold")[374]], [#text(font: "DejaVu Sans Mono", weight: "bold")[345]], [#text(font: "DejaVu Sans Mono")[50]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]], [#text(fill: gray, font: "DejaVu Sans Mono")[·]],
)
#text(size: 7pt, fill: gray)[Author column: `cell\_type\_high\_resolution`]


#pagebreak()

== Mancuso\_2024 / Mancuso\_2023

_Tech: 10x\_chromium, n\_cells: 127,755_
#text(fill: orange)[⚠ 43 pending columns not merged]

#grid(columns: (1fr, 1fr), gutter: 12pt,
[
=== QA flags
#table(
    columns: (auto, auto),
    inset: 4pt,
    align: (left, left),
    stroke: 0.5pt + gray,
      [Cells], [#text(font: "DejaVu Sans Mono")[127,755]],
      [Gene overlap], [#text(font: "DejaVu Sans Mono")[2,723 / 2,727]],
      [Median residual], [#text(font: "DejaVu Sans Mono")[1.954]],
      [WholeCell% (high)], [#text(font: "DejaVu Sans Mono")[56.0% ⚠ likely whole-cell]],
      [DC% (\> 0.05)], [#text(font: "DejaVu Sans Mono")[60.8% ⚠ DC contamination]],
)

],
[
=== Identity\_Label distribution
#table(
    columns: (auto, auto, auto),
    inset: 3pt,
    align: (left, right, right),
    stroke: 0.5pt + gray,
    table.header([*Identity\_Label*], [*n*], [*%*]),
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono")[60,091]], [#text(font: "DejaVu Sans Mono")[47.0%]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono")[18,332]], [#text(font: "DejaVu Sans Mono")[14.3%]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono")[13,181]], [#text(font: "DejaVu Sans Mono")[10.3%]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono")[10,224]], [#text(font: "DejaVu Sans Mono")[8.0%]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono")[7,151]], [#text(font: "DejaVu Sans Mono")[5.6%]],
      [IRM], [#text(font: "DejaVu Sans Mono")[6,165]], [#text(font: "DejaVu Sans Mono")[4.8%]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono")[6,057]], [#text(font: "DejaVu Sans Mono")[4.7%]],
      [Monocyte\_Derived], [#text(font: "DejaVu Sans Mono")[2,019]], [#text(font: "DejaVu Sans Mono")[1.6%]],
      [Border\_CAM], [#text(font: "DejaVu Sans Mono")[2,014]], [#text(font: "DejaVu Sans Mono")[1.6%]],
      [DC\_cDC1], [#text(font: "DejaVu Sans Mono")[1,004]], [#text(font: "DejaVu Sans Mono")[0.8%]],
      [_other (3)_], [#text(font: "DejaVu Sans Mono")[1,517]], [#text(font: "DejaVu Sans Mono")[1.2%]],
)

]
)

=== Author label cross-tab
_Cell counts: rows = starcat Identity\_Label, columns = author's `cell.state\_v2.1`. Concentration along rows = agreement._

#table(
    columns: (auto, auto, auto, auto, auto, auto, auto, auto, auto),
    inset: 3pt,
    align: (left, right, right, right, right, right, right, right, right),
    stroke: 0.5pt + gray,
    table.header([*starcat label*], [*Homeostatic (HM)*], [*Transitioning CRM*], [*Disease associated (DAM)*], [*Translational response (TRM)*], [*Cytokines response 2 (CRM-2)*], [*Antigen-presenting response (HLA)*], [*Interferon response (IRM)*], [*Cytokines response 1 (CRM-1)*]),
      [Border\_CAM], [#text(font: "DejaVu Sans Mono", weight: "bold")[550]], [#text(font: "DejaVu Sans Mono", weight: "bold")[264]], [#text(font: "DejaVu Sans Mono", weight: "bold")[237]], [#text(font: "DejaVu Sans Mono", weight: "bold")[176]], [#text(font: "DejaVu Sans Mono", weight: "bold")[297]], [#text(font: "DejaVu Sans Mono", weight: "bold")[114]], [#text(font: "DejaVu Sans Mono", weight: "bold")[337]], [#text(font: "DejaVu Sans Mono")[39]],
      [Cycling], [#text(font: "DejaVu Sans Mono", weight: "bold")[276]], [#text(font: "DejaVu Sans Mono", weight: "bold")[169]], [#text(font: "DejaVu Sans Mono")[49]], [#text(font: "DejaVu Sans Mono", weight: "bold")[102]], [#text(font: "DejaVu Sans Mono", weight: "bold")[161]], [#text(font: "DejaVu Sans Mono")[20]], [#text(font: "DejaVu Sans Mono")[21]], [#text(font: "DejaVu Sans Mono")[41]],
      [DAM], [#text(font: "DejaVu Sans Mono", weight: "bold")[119]], [#text(font: "DejaVu Sans Mono")[59]], [#text(font: "DejaVu Sans Mono")[73]], [#text(font: "DejaVu Sans Mono")[30]], [#text(font: "DejaVu Sans Mono", weight: "bold")[106]], [#text(font: "DejaVu Sans Mono")[36]], [#text(font: "DejaVu Sans Mono")[22]], [#text(font: "DejaVu Sans Mono")[21]],
      [DC\_Mature], [#text(font: "DejaVu Sans Mono")[31]], [#text(font: "DejaVu Sans Mono")[25]], [#text(font: "DejaVu Sans Mono")[25]], [#text(font: "DejaVu Sans Mono")[5]], [#text(font: "DejaVu Sans Mono")[95]], [#text(font: "DejaVu Sans Mono")[11]], [#text(font: "DejaVu Sans Mono")[1]], [#text(font: "DejaVu Sans Mono")[19]],
      [DC\_cDC1], [#text(font: "DejaVu Sans Mono", weight: "bold")[262]], [#text(font: "DejaVu Sans Mono", weight: "bold")[197]], [#text(font: "DejaVu Sans Mono", weight: "bold")[101]], [#text(font: "DejaVu Sans Mono", weight: "bold")[159]], [#text(font: "DejaVu Sans Mono", weight: "bold")[187]], [#text(font: "DejaVu Sans Mono")[47]], [#text(font: "DejaVu Sans Mono")[11]], [#text(font: "DejaVu Sans Mono")[40]],
      [DC\_cDC2], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,884]], [#text(font: "DejaVu Sans Mono", weight: "bold")[698]], [#text(font: "DejaVu Sans Mono", weight: "bold")[693]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,258]], [#text(font: "DejaVu Sans Mono", weight: "bold")[856]], [#text(font: "DejaVu Sans Mono", weight: "bold")[399]], [#text(font: "DejaVu Sans Mono")[87]], [#text(font: "DejaVu Sans Mono", weight: "bold")[182]],
      [DC\_pDC], [#text(font: "DejaVu Sans Mono", weight: "bold")[2,345]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,218]], [#text(font: "DejaVu Sans Mono", weight: "bold")[609]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,134]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,091]], [#text(font: "DejaVu Sans Mono", weight: "bold")[285]], [#text(font: "DejaVu Sans Mono", weight: "bold")[145]], [#text(font: "DejaVu Sans Mono", weight: "bold")[324]],
      [HLA\_MHCII], [#text(font: "DejaVu Sans Mono", weight: "bold")[2,153]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,797]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,343]], [#text(font: "DejaVu Sans Mono", weight: "bold")[546]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,533]], [#text(font: "DejaVu Sans Mono", weight: "bold")[4,603]], [#text(font: "DejaVu Sans Mono", weight: "bold")[463]], [#text(font: "DejaVu Sans Mono", weight: "bold")[743]],
      [Homeostatic], [#text(font: "DejaVu Sans Mono", weight: "bold")[7,340]], [#text(font: "DejaVu Sans Mono", weight: "bold")[2,923]], [#text(font: "DejaVu Sans Mono", weight: "bold")[2,308]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,345]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,661]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,377]], [#text(font: "DejaVu Sans Mono", weight: "bold")[544]], [#text(font: "DejaVu Sans Mono", weight: "bold")[834]],
      [Homeostatic\_MERTK], [#text(font: "DejaVu Sans Mono", weight: "bold")[34,989]], [#text(font: "DejaVu Sans Mono", weight: "bold")[8,740]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,694]], [#text(font: "DejaVu Sans Mono", weight: "bold")[7,336]], [#text(font: "DejaVu Sans Mono", weight: "bold")[2,974]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,490]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,553]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,315]],
      [IRM], [#text(font: "DejaVu Sans Mono", weight: "bold")[353]], [#text(font: "DejaVu Sans Mono", weight: "bold")[386]], [#text(font: "DejaVu Sans Mono", weight: "bold")[400]], [#text(font: "DejaVu Sans Mono", weight: "bold")[118]], [#text(font: "DejaVu Sans Mono", weight: "bold")[1,034]], [#text(font: "DejaVu Sans Mono", weight: "bold")[183]], [#text(font: "DejaVu Sans Mono", weight: "bold")[3,470]], [#text(font: "DejaVu Sans Mono", weight: "bold")[221]],
      [Lipid\_LAM], [#text(font: "DejaVu Sans Mono", weight: "bold")[575]], [#text(font: "DejaVu Sans Mono", weight: "bold")[337]], [#text(font: "DejaVu Sans Mono", weight: "bold")[6,408]], [#text(font: "DejaVu Sans Mono", weight: "bold")[553]], [#text(font: "DejaVu Sans Mono", weight: "bold")[966]], [#text(font: "DejaVu Sans Mono", weight: "bold")[661]], [#text(font: "DejaVu Sans Mono", weight: "bold")[163]], [#text(font: "DejaVu Sans Mono", weight: "bold")[561]],
)
#text(size: 7pt, fill: gray)[Author column: `cell.state\_v2.1`]


#pagebreak()



= Caveats

+ *DAM vs Lipid_LAM naming.* The catalog's `Microglia_DAM` is the
  Keren-Shaul/Krasemann APOE⁺CD163⁺ DAM; `Microglia_Lipid_LAM` is the
  Marschallinger SPP1⁺GPNMB⁺ lipid-LAM. Many recent papers use "DAM"
  for what the catalog calls `Lipid_LAM`. Cross-tabs typically show
  high author-DAM in starcat rows labeled `Lipid_LAM` rather than `DAM` —
  this is correct, not a disagreement.
+ *High DC% in soft signal.* `DC_total > 0.05` triggers in 30–100 % of cells
  in most datasets, but the discrete `Identity_Label = DC_*` argmax is
  typically only a few percent. Use the discrete label, not the soft threshold,
  for population estimates.
+ *WholeCell_Baseline_high* is high (≥ 25 %) for whole-cell datasets,
  low (≤ 5 %) for snRNA-seq. Useful as a tech detector independent of
  the `.md` `technology` field.
+ *Residual* values reported here use the variance-normalised L2 formulation
  from `example/annotate.py`; the §7.5 threshold of 0.7 is from that same
  formulation. Most datasets score 1–3, with a few (`Olah2020` 4.1) showing
  poor catalog coverage — likely due to non-myeloid contamination.
