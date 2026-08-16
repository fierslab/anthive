# Ontology mapping — tissue and disease

This note explains how the anthive catalog constrains `tissue` and `disease`
values, how the parent-child hierarchy lets you query broadly, and what
mapping/normalisation was applied on 2026-08-16.

The vocabulary lists live at `lib/_mus_vocab.yaml` (canonical labels only)
and the parent-child relations at `lib/_ontology_hierarchy.yaml` (used for
`filter-by-parent` queries in the UI and MCP layer).

## Where values are stored

Every dataset's experimental annotation is written as flat `KEY=VALUE`
lines in two places (see `data/h5adstore/CONVENTIONS.md`):

| File | Scope |
| --- | --- |
| `<group>/.env` | Folder cascade default — inherited by every sidecar below |
| `<group>/<file>.h5ad.mus` | Per-file authority — replaces cascade for any key it sets |

Multi-value fields (`tissue`, `disease`, `condition`, `contributor`, `doi`,
…) are comma-separated. Items containing literal commas are double-quoted
(`disease="leukoencephalopathy, diffuse hereditary, with spheroids 1"`).

Matching is case-insensitive with `_` ≡ ` ` ≡ `-`, so
`prefrontal_cortex`, `Prefrontal-Cortex`, and `prefrontal cortex` all match
the same vocab entry.

## Vocabulary — tissue

UBERON-style human-readable labels. Grouped here by anatomical family. The
authoritative list is `lib/_mus_vocab.yaml → vocabulary.tissue`.

- **Brain, root**: `brain`, `brainstem`, `basal ganglia`, `olfactory bulb`,
  `gray matter`
- **Cortex**: `cortex`, `frontal cortex`, `prefrontal cortex`,
  `dorsolateral prefrontal cortex`, `motor cortex`, `occipital cortex`,
  `parietal cortex`, `temporal cortex`
- **Cortical subregions**: `middle temporal gyrus`, `inferior temporal gyrus`,
  `entorhinal cortex`, `angular gyrus`, `occipitotemporal cortex`,
  `cortical layer 2/3`
- **Deep brain**: `hippocampus`, `striatum`, `caudate nucleus`, `thalamus`,
  `hypothalamus`, `cerebellum`, `midbrain`, `substantia nigra`,
  `facial nucleus`, `subventricular zone`
- **White matter**: `white matter`, `corpus callosum`,
  `white matter of frontal/occipital/parietal/temporal lobe`,
  `white matter of cerebellum`, `cervical spinal cord white matter`
- **Spinal**: `spinal cord`
- **Brodmann**: `Brodmann (1909) area 4/7/9/10/17/18/46`
- **Non-brain**: `blood`, `bone marrow`, `lung`, `liver`, `kidney`, `heart`,
  `retina`, `skin`, `csf`

## Vocabulary — disease

MONDO-style labels. Grouped by broad category (see `lib/_mus_vocab.yaml →
vocabulary.disease` for the full list).

- **Reference**: `normal`
- **Neurodegenerative**: `Alzheimer disease`, `Parkinson disease`,
  `amyotrophic lateral sclerosis`, `Huntington disease`,
  `frontotemporal dementia`,
  `"leukoencephalopathy, diffuse hereditary, with spheroids 1"`
- **Neuroinflammatory**: `multiple sclerosis`,
  `experimental autoimmune encephalomyelitis`, `viral encephalitis`,
  `Aicardi-Goutières syndrome`
- **Neurodevelopmental / psychiatric**: `Down syndrome`,
  `autism spectrum disorder`, `schizophrenia`
- **Structural / lesional**: `focal cortical dysplasia`,
  `hippocampal sclerosis`, `epilepsy`, `temporal lobe epilepsy`,
  `stroke disorder`
- **Neoplastic**: `glioma`, `glioblastoma`
- **Other**: `COVID-19`, `graft-versus-host disease`

## Parent-child hierarchy

`lib/_ontology_hierarchy.yaml` maps each term to its DIRECT parent, so
transitive closure yields the full set of descendants.

Example (tissue):

```
brain
├── cortex
│   ├── frontal cortex
│   │   ├── prefrontal cortex
│   │   │   ├── dorsolateral prefrontal cortex
│   │   │   │   ├── Brodmann (1909) area 9
│   │   │   │   └── Brodmann (1909) area 46
│   │   │   └── Brodmann (1909) area 10
│   │   └── motor cortex
│   │       └── Brodmann (1909) area 4
│   ├── temporal cortex
│   │   ├── middle temporal gyrus
│   │   ├── inferior temporal gyrus
│   │   └── entorhinal cortex
│   ├── parietal cortex
│   │   ├── angular gyrus
│   │   └── Brodmann (1909) area 7
│   └── occipital cortex
│       ├── Brodmann (1909) area 17
│       └── Brodmann (1909) area 18
├── hippocampus
├── basal ganglia
│   └── striatum
│       └── caudate nucleus
├── midbrain
│   └── substantia nigra
├── brainstem
│   └── facial nucleus
├── white matter
│   ├── corpus callosum
│   └── white matter of {frontal, occipital, parietal, temporal} lobe
└── cerebellum
```

Disease hierarchy (abbreviated):

```
neurodegenerative disease
├── Alzheimer disease
├── Parkinson disease
├── Huntington disease
├── amyotrophic lateral sclerosis
├── frontotemporal dementia
├── Lewy body dementia
└── dementia
    └── vascular dementia

neuroinflammatory disease
├── multiple sclerosis
├── experimental autoimmune encephalomyelitis
└── viral encephalitis

brain neoplasm
├── glioma
├── glioblastoma
├── oligodendroglioma
└── astrocytoma

epilepsy
└── temporal lobe epilepsy
```

## How parent-based filtering works

Given a query term `Q`, a dataset matches if any of its tissue values is
either `Q` itself OR a transitive descendant of `Q` in the hierarchy.

Concretely: expand `Q → descendants(Q) ∪ {Q}` into a set of normalised
labels, then test set-membership against each dataset's tissue list.

Examples (assumes the datasets currently in the catalog):

- **`tissue=cortex`** matches every dataset with any cortical value —
  Mathys2024 (via `prefrontal cortex`, `middle temporal gyrus`,
  `entorhinal cortex`, `angular gyrus`), Gabitto2024 (via
  `middle temporal gyrus` and `prefrontal cortex`), Olah2020 (via
  `prefrontal cortex`, `temporal cortex`,
  `dorsolateral prefrontal cortex`), Schirmer2019, Zielonka2025
  (via `cortical layer 2/3`), and every dataset that already tags `cortex`.
- **`tissue=prefrontal cortex`** matches Francken2026, Gazestani2023,
  Olah2020, OteroGarcia2022, Pan2024, SunMathys2023, Schirmer2019,
  Zhou2020, Lee2026, plus any dataset that specifies
  `dorsolateral prefrontal cortex` (a descendant) like GreenFujita2024.
- **`tissue=temporal cortex`** matches Mancuso2019 (Primary.Human),
  Thrupp2020, and — via descendants — Gabitto2024 (`middle temporal
  gyrus`), Mathys2024 (`middle temporal gyrus`, `entorhinal cortex`).
- **`tissue=basal ganglia`** matches Prinz2026 (via `striatum`) and
  TsaiWyssCoray2025 (which lists `basal ganglia` directly).
- **`disease=neurodegenerative disease`** matches every dataset tagged
  with `Alzheimer disease`, `Parkinson disease`, `Huntington disease`,
  `amyotrophic lateral sclerosis`, `frontotemporal dementia`, or
  `Lewy body dementia`.

## Example web-UI queries this enables

- `/datasets?tissue=cortex&disease=Alzheimer disease` — every Alzheimer
  dataset covering any cortical subregion (including Brodmann-labelled
  ones) without having to enumerate all subregion labels.
- `/datasets?tissue=basal ganglia` — Prinz2026 comes up even though it
  only tags `striatum`.
- `/datasets?disease=brain neoplasm` — Sankowski2019 (`glioblastoma`) and
  Prinz2026 (`glioma`) both surface via the neoplasm umbrella.

## Sweep summary (2026-08-16)

- 216 files scanned (163 `.h5ad.mus` + 53 `.env`)
- 413 tissue+disease value instances already canonical
- 4 files rewritten (line-preserving; only `tissue=` and `disease=` lines
  touched):
  - `TsaiWyssCoray2025/.env` and `.../cerebellar_microglia_aging.h5ad.mus`
    — `basal ganglion` → `basal ganglia` (typo fix)
  - `Zhou2020/all_nuclei.h5ad.mus` and `Zhou2020/microglia.h5ad.mus`
    — `Alzheimer disease | normal` → `Alzheimer disease,normal`
    (pipe-separator legacy → comma per mus spec)
- 1 caveat added: Sankowski2019 `tissue='tumor'` — flagged as a lesion
  state rather than an anatomical term.
- Vocabulary additions (17 new tissues, 0 diseases): see the git diff on
  `lib/_mus_vocab.yaml` for the exact list.
- Hierarchy additions: `entorhinal cortex`, `angular gyrus`,
  `occipitotemporal cortex`, `cortical layer 2/3`, `basal ganglia`
  (with `striatum` re-parented under it), `olfactory bulb`,
  `corpus callosum`, `gray matter`, Brodmann areas 10/18/46, plus
  `frontal cortex` re-parented so `prefrontal cortex → frontal cortex →
  cortex` reflects real anatomy.
