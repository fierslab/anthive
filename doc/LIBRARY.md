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
    datasets/
      lee2026plasticity.md    bibliographic record + notes
      lee2026plasticity.pdf   optional, git-ignored, synced via S3
    lab/
    methods/
    reading/
      2024/                   any depth is fine
  README.md
  REVIEW.md                   fields still awaiting human review
```

## Folders

Records can sit in sub-folders of `papers/`, at any depth. **Folders are for
people only and never carry meaning**: no tool reads a role, a topic or an
access rule from a folder name. Roles (`anthive_role`) and the `papers:`
references on datasets, collections and gene sets stay the source of truth.

- One paper lives in exactly one folder. Citekeys (and DOIs) are unique
  across the whole library; `ant lib-check` and the server report a clash
  with both paths.
- `file:` names the PDF relative to the record's own folder, normally the
  bare filename (`file: lee2026plasticity.pdf` next to the `.md`).
- Folders starting with `_` or `.` are skipped (staging, scratch), as are
  `README.md`, `REVIEW.md` and any `.md` without `entrytype` in its front
  matter (plain notes).
- Moving a paper is a `git mv` of its `.md` (and moving the PDF next to it).
  `ant sync library` copies the PDF to its new key on S3 and reports the old
  key; only `--propagate-deletes` removes it (see "Two-way sync" below).

`ant lib-add`, `ant lib-import` and the upload API put a NEW record in the first folder that
applies, unless `--folder` says otherwise:

1. `papers/lab/` when an author matches `library_lab_authors` in
   `anthive.yaml` (default `[De Strooper]`, case and accent insensitive);
2. `papers/datasets/` for role `source` or `reanalysis`;
3. `papers/methods/` for role `method`;
4. `papers/reading/` for everything else.

Updates keep a record where it is.

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
| `ant lib-add <doi\|pmid\|pdf\|bibfile> [--folder papers/reading/2024]` | fetches CrossRef and OpenAlex (authors with ORCID, journal, pages, abstract, cited DOIs), writes the sidecar, copies the PDF next to it |
| `ant lib-import <export.bib> [--folder ...]` | imports a Better BibTeX export (for example one Zotero collection); idempotent by DOI and citekey |
| `ant lib-export [-o library.bib]` | writes BibTeX for the whole library or a selection |
| `ant lib-check` | validates every record and every `papers:` reference in the h5adstore |

| `ant lib-index` | builds or updates the literature index (`doc/LIBRARY_INDEX.md`) |
| `ant sync library` | two-way sync of the records (git) and PDFs (S3), then the index (below) |
| `ant sync push` / `pull` | also run the library sync; `--no-library` skips it, `--library-only` does only it |

All of them read the library location from `library` in `anthive.yaml`
(environment `ANTHIVE_LIBRARY`) and the h5adstore from `h5adstore`.

## Storage and sync

The `.md` records are committed to the library repository. PDFs are
git-ignored and live in S3, under the same bucket and prefix as the
h5adstore, mirroring the folders:

```
<s3.prefix>/_library/papers/<folders>/<citekey>.pdf
<s3.prefix>/_library_index/library_chunks.parquet     (+ library_chunks.json)
```

The leading `_` keeps both out of the h5adstore sync and the S3 catalog scan.
The PDF sync works only inside an existing library checkout (PDFs mean
nothing without their records); the index folder is created if needed.

### Two-way sync (`ant sync library`)

Papers can be added on any site (by hand, with `ant lib-add`, or through the
upload API), so the library has no single direction. `ant sync library`
runs three steps, and `ant sync push` and `ant sync pull` run the same three
unless `--no-library` is given:

1. **Records** through git: `git pull --rebase --autostash` from the
   library remote, then `git push` when `library_git_push` is true. It never
   forces. A rebase conflict is aborted (the checkout is left as it was) and
   reported; resolve it by hand in the checkout. `--no-git` skips this step.
2. **PDFs** against S3, decided per file from a manifest of what both sides
   looked like at the last sync (kept in `.git/anthive-sync.json`, or
   `.anthive-sync.json` when the library is not a plain git checkout; put
   `.anthive-*` in the library's `.gitignore` in that case). Remote changes
   are seen from the ETag and the `sha256` stored in each object's metadata,
   so nothing is downloaded just to compare.
3. **Index**: when PDFs arrived or changed, `ant lib-index --add` indexes
   just those papers. Then the publisher rule below.

| Local | S3 | Seen at last sync | Result |
| --- | --- | --- | --- |
| new | absent | no | upload |
| absent | new | no | download |
| same content | same content | no | recorded, nothing copied |
| different | different | no | conflict |
| changed | unchanged | yes | upload |
| unchanged | changed | yes | download |
| changed | changed | yes | conflict (unless both now hold the same bytes) |
| unchanged | deleted | yes | reported; local file deleted only with `--propagate-deletes` |
| deleted | unchanged | yes | reported; S3 object deleted only with `--propagate-deletes` |
| moved (same bytes, new path, old path gone) | unchanged | yes | copied to the new key on S3; old key reported, removed with `--propagate-deletes` |
| unchanged | moved and old key gone | yes | moved locally |

A **conflict** keeps the local file untouched and saves the S3 version next
to it as `<name>.conflict-YYYYMMDD.pdf` (never synced itself). It stays a
conflict on every run until you decide: `--prefer local` uploads the local
file, `--prefer remote` replaces it with the S3 copy. Delete the conflict
copy afterwards.

`--dry-run` prints the plan and changes nothing (no manifest update either).
On a read-only site (`slave` mode) the sync downloads only: no git push, no
S3 writes, local changes are reported.

**Index publisher rule.** The literature index is derived data and is
published by one site only, the one with `library_index_publisher: true`
(normally the main site). That site pushes `library_chunks.parquet` to S3
after its index update. Every other site downloads the published index only
when it has none or the S3 copy is newer, then runs `ant lib-index --add` so
its own local papers are included again. Two publishers would overwrite each
other's index; do not configure more than one.

## Adding papers through the API

`POST /api/papers/upload` (and the MCP tools `check_paper` and
`upload_paper`) add a paper without a shell on the server. Only members of
`library_upload_groups` (default `[admins]`; in basic auth mode the users in
`auth.admins` count as `admins`) may call it; everyone else gets a 403.

`GET /api/papers/upload/requirements` describes what a record needs, with a
description and example for every field and the allowed vocabularies, so a
client (or the paper-preparation skill) can build a complete record first.

Required:

- `entrytype` (BibLaTeX: article, inproceedings, book, online, unpublished,
  misc, ...), `title`, `author` (a list of `"Last, First"` names, at least
  one), `date` or `year`;
- an identifier: `doi`, `pmid`, `eprint` with `eprinttype`, or `url`.
  Entry types `unpublished` and `misc` may give a `note` instead.

Recommended (listed back as `missing_recommended` when absent):
`journaltitle`, `abstract`, `keywords`, `anthive_role`, `anthive_celltypes`,
`anthive_tissues`, `anthive_diseases`, `anthive_methods`, `anthive_cohorts`,
`anthive_summary`, `anthive_access`, `anthive_llm` and `papers_for`.

The multipart form takes `metadata` (a JSON object or the text of a `.md`
record with front matter), an optional `file` (the PDF), `folder` (`lab`,
`datasets`, `methods` or `reading`; default by the folder rule above),
`enrich` (default true), `dry_run` and `update`.

- **Enrichment** looks the DOI or PMID up in CrossRef and OpenAlex and fills
  only empty fields; it never overwrites what was sent. Filled fields are
  listed in `filled_fields` and in the record's `anthive_draft`, so they show
  up for review. A failed lookup is a warning.
- **Duplicates** are refused with 409 and the matching citekey, checked by
  DOI, PMID, the PDF's sha256 and the citekey. A record without DOI or PMID
  whose first author, year and title match an existing paper counts as the
  same citekey (`matched_on: citekey`). `update=true` merges the sent
  fields into the existing record instead (it stays in its folder).
- **PDFs** are checked by their magic bytes (415 otherwise) and size
  (`library_upload_max_pdf_mb`, 413 above it).
- **`papers_for`** (dataset or collection ids, optionally `id:role`) is
  stored as `anthive_proposed_links` on the paper and never written into the
  h5adstore. A curator adds the real `papers:` reference on the dataset.
- `dry_run=true` validates, enriches and returns the record that would be
  written, without writing anything.

Responses: 201 `created`, 200 `updated` or `dry_run` (all with `citekey`,
`folder`, `path`, `pdf_sha256`, `record`, `filled_fields`, `warnings`,
`missing_recommended`), 409 `duplicate`, 422 `invalid` (`errors` per field
and `missing_required`), 413, 415, 403.

What happens on success, in order: the record (and PDF) are written under a
lock, the record is validated with the same rules as `ant lib-check`, and
both are committed to the library repo with the uploader as git author
(`Remote-Name`/`Remote-Email`, else the user name) and
`library_git_committer` as committer; with `library_git_push` the commit is
pushed. Then, without failing the request: the paper is added to the
literature index in the background (`/api/library/status` shows
`upload_index`), the library is reloaded, and with `library_s3_autopush`
(off by default since 2.41.0) the PDF goes to S3 at once. Any of those that
fails is returned as a warning; the paper itself is saved. Without autopush
the PDF reaches S3 with the next `ant sync library`; container sites run
`ant-stack publish-library` from a timer for that (`doc/DEPLOY_CONTAINERS.md`).

## How the server uses it

ant-serve loads the records, resolves every `papers:` reference to dataset
ids and loads the index at startup (in the background) and on
`/admin/rescan`. Endpoints: `/papers`, `/papers/{citekey}` (+ `.bib`,
`/related`, `/file`, `/pages/{n}`), `/library.bib`, `/library/status`,
`/search/literature`, `/databases/{id}/papers`, `/databases/{id}/context`
(see `doc/API_CHANGELOG.md`, 2.39.0). MCP clients get the same through the
tools `search_literature`, `get_paper`, `related_papers`,
`get_dataset_context` (`doc/MCP_LINKS.md`).

Access: `anthive_access: restricted` records are visible to admins only and
never appear in other records' `related` lists or exports for anyone else.
For callers that pass answers to an external LLM (ant-mcp), `anthive_llm:
never` hides the paper and `summary_only` keeps its metadata and summary but
no passages, notes, abstract or page text.
