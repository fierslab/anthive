# The paper library

anthive keeps one library of papers: the papers its datasets come from,
papers that reanalyse them or describe their methods, and papers kept
because they are relevant. The library is its own git repository (default
location `data/library`, configurable as `library` in `anthive.yaml` or
`ANTHIVE_LIBRARY`). Each paper is one markdown sidecar; PDFs sit next to
them and are synced through S3 like the data, not committed.

```
library/
  papers/
    lee2026plasticity.md      bibliographic record + notes
    lee2026plasticity.pdf     optional, git-ignored, synced via S3
  README.md
  REVIEW.md                   fields still awaiting human review
```

## The sidecar format

A sidecar is YAML front matter followed by free-text notes in markdown.
Every front matter key is a BibLaTeX field name, so a record converts to a
standard BibTeX entry without translation (`ant lib-export`). anthive's own
fields carry an `anthive_` prefix; BibTeX tools ignore fields they do not
know.

```markdown
---
entrytype: article
citekey: lee2026plasticity
author:
- Lee, Donghoon
- Roussos, Panos
title: Plasticity of human microglia and brain perivascular macrophages in aging and Alzheimer's disease
journaltitle: Nature Genetics
date: '2026-08-11'
volume: '58'
number: '9'
pages: 2152-2163
doi: 10.1038/s41588-026-02716-6
pmid: '39677435'
file: lee2026plasticity.pdf
anthive_role: source
anthive_celltypes: [microglial cell, macrophage, myeloid cell]
anthive_tissues: [prefrontal cortex]
anthive_diseases: [Alzheimer disease, normal]
anthive_organisms: [human, mouse]
anthive_methods: [scRNA-seq (fresh myeloid cells), snRNA-seq, CITE-seq]
anthive_cohorts: [PsychAD, ROSMAP]
anthive_summary: Two or three sentences on what the paper shows.
anthive_access: public
anthive_llm: allowed
anthive_draft: [anthive_celltypes, anthive_summary]
---

## Notes

Why we keep it, caveats, how it relates to our data. Indexed for search.
```

The file is named `<citekey>.md` and the PDF, if any, `<citekey>.pdf`.

### Bibliographic fields (BibLaTeX)

| Field | Notes |
| --- | --- |
| `entrytype` | `article`, `online` (preprints), `unpublished`, `book`, `incollection`, `inproceedings`, `thesis`, `report`, `misc`, `dataset`, `software` |
| `citekey` | lowercase ascii; see "Citekeys" |
| `author`, `editor` | lists of `Last, First` |
| `title`, `subtitle` | |
| `journaltitle` | the journal (`journal` is accepted as the BibTeX alias) |
| `booktitle`, `publisher`, `institution` | |
| `date` | `YYYY`, `YYYY-MM` or `YYYY-MM-DD`; `year` is accepted too |
| `volume`, `number`, `pages`, `eid` | |
| `doi`, `pmid`, `pmcid`, `isbn`, `issn`, `url` | DOIs without the `https://doi.org/` prefix |
| `eprint`, `eprinttype` | e.g. a bioRxiv or arXiv id |
| `abstract`, `keywords` | `keywords` is a list |
| `file` | the PDF next to the sidecar |
| `related`, `relatedtype` | explicit links to other papers, see "Relations" |
| `note` | e.g. "Preprint, bioRxiv" or "Manuscript submitted to ..." |

### anthive fields

| Field | Meaning |
| --- | --- |
| `anthive_role` | why the paper is in the library: `source`, `reanalysis`, `method`, `background`, `related` |
| `anthive_celltypes` | cell types studied, as mus vocabulary labels |
| `anthive_tissues` | tissues or regions, as mus vocabulary labels |
| `anthive_diseases` | conditions, as mus vocabulary labels (`normal` for healthy) |
| `anthive_organisms` | `human`, `mouse`, ... |
| `anthive_methods` | assays and models in plain terms (`snRNA-seq`, `human microglia xenograft`, ...) |
| `anthive_cohorts` | donor cohorts and brain banks (`ROSMAP`, `SEA-AD`, `PsychAD`, ...) |
| `anthive_summary` | two or three sentences, used as context by search and AI clients |
| `anthive_orcids` | author name to ORCID, from OpenAlex |
| `anthive_cites` | DOIs this paper cites, from OpenAlex (used for citation relations) |
| `anthive_openalex` | the OpenAlex work id |
| `anthive_access` | `public`, `internal` or `restricted`; who may see the record and PDF |
| `anthive_llm` | `allowed`, `summary_only` or `never`: what may be sent to external LLMs |
| `anthive_draft` | fields written by a tool or LLM that still need a human check; remove a field from this list once checked |

The mus vocabulary (`lib/_mus_vocab.yaml`, the same one `.h5ad.mus` uses)
keeps papers and datasets comparable. Labels outside the vocabulary are
allowed when accurate; `ant lib-check` lists them so the vocabulary can grow.
`lib/library.py` maps the common labels to ontology ids (CL, UBERON, MONDO).

### Citekeys

Better BibTeX style, so Zotero exports match: first author's last name, year,
first significant word of the title, lowercase ascii
(`lee2026plasticity`). Collisions get `a`, `b`, ... appended. A citekey never
changes once other files point at it.

A preprint and its published version are one record when the published
version exists (attach the version you have as the PDF and say so in the
notes). A revised manuscript that differs from a preprint gets its own
`unpublished` record linked with `relatedtype: version`.

## Relations

**Datasets, collections and gene sets point at papers**, in their own front
matter, with a role:

```yaml
# data/h5adstore/Lee2026/index.md (all datasets of the collection)
papers:
  lee2026plasticity: source

# a dataset .md (just this dataset)
papers:
  lee2026plasticity: source
  mathys2023singlecell: related
```

Roles: `source` (the data comes from this paper), `reanalysis`, `method`,
`related`. Gene-set experiments in the genesetstore use the same `papers:`
mapping in their metadata.

**Papers point at papers** with BibLaTeX's `related` (a list of citekeys)
and `relatedtype` (one type per record): `samecohort`, `version`, `extends`,
`reanalysisof`, `companion`, `comment`.

Relations nobody types are derived when the library is indexed: shared
authors (by ORCID when available), shared cell types, tissues, diseases,
methods and cohorts, and citations (`anthive_cites`).

`ant lib-check` fails on a `papers:` reference or a `related` entry that
does not resolve to a record.

## Static-file sidecars

Other files of a study live in its `static/` folder (supplementary tables,
protocols, figures, code). They get the same kind of sidecar, `<file>.md`
next to the file, so the web UI can show them with context:

```yaml
---
title: Supplementary Table 3, cluster markers
description: Marker genes per microglial subtype (Wilcoxon, log2FC > 0.5)
kind: supplementary_table   # supplementary_table | figure | protocol | code | data | other
paper: lee2026plasticity    # the paper it belongs to
license: CC-BY-4.0
---
```

## Tools

| Command | Does |
| --- | --- |
| `ant lib-add <doi\|pmid\|pdf\|bibfile>` | fetches CrossRef and OpenAlex (authors with ORCID, journal, pages, abstract, cited DOIs), writes the sidecar, copies the PDF |
| `ant lib-import <export.bib>` | imports a Better BibTeX export (for example one Zotero collection); idempotent by DOI and citekey |
| `ant lib-export [-o library.bib]` | writes BibTeX for the whole library or a selection |
| `ant lib-check` | validates every record and every `papers:` reference in the h5adstore |

All of them read the library location from `library` in `anthive.yaml`
(environment `ANTHIVE_LIBRARY`) and the h5adstore from `h5adstore`.
