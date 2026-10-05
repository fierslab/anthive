# bin/meta/ — catalog metadata + validators

Tools that read, write, and validate the **YAML frontmatter** of `.md`
files (anthive-catalog fields), the **`.h5ad.mus` sidecars** (experimental
annotation), and the integrity of the underlying h5ad/duckdb/S3 state.

| Tool | Role |
| --- | --- |
| `ant-md` | Read/write `.md` frontmatter fields. Refuses to set annotation fields (those live in `.h5ad.mus`, managed by the `mus` CLI). |
| `ant-md-check` | Sanity-check `.md` files (group_id, id uniqueness, sha256 freshness, schema compliance) and lint the facet mapping and lineage (`lib/facet_lint.py`): closed `ant_*` namespace (`--strict-namespace` makes unregistered targets errors), mapping rules, `reanalysis_of` / `is_secondary`; `--with-data` reads each `.duckdb` read-only. Prints files per violated rule. Skips `_` folders. |
| `ant-migrate-donor-uid` | One-shot data migration (`--dry-run` first): value-map `ant_donor_uid` derives to the crosswalk join form (verified entry by entry), and the `{group_id}:{value}` template form for every other dataset that renames a column to `ant_donor`. Text edit of the front matter, verified by re-parsing; prints counts, never ids. See `doc/DESIGN_canonical_columns.md`. |
| `ant-check` | Wrapper that runs `ant-h5ad-check` + `ant-duckdb-check` + `ant-md-check` over a target. |
| `ant-api-check` | Probe a running `bin/api/ant-serve` for endpoint correctness + data sanity. |
| `ant-s3-check` | Verify S3 mirror parity vs local h5adstore (size + mtime). Read-only — push/pull lives in `bin/ops/ant-sync`. |
| `ant-cellxgene` | Import datasets from the CZ CELL×GENE Discover portal. Downloads, validates, runs ant-convert. |
| `ant-geneset` | Manage the **genesetstore** (`data/genesetstore/`): list, validate, register `.grp` / `.rnk` gene lists keyed per study. Consumed by `bin/annot/ant-score`. |
| `ant-lib-add` | Add a paper to the **library** (`library` in anthive.yaml, default `data/library`, its own git repo) by DOI, PMID, PDF or BibTeX; fetches CrossRef + OpenAlex. Spec: `doc/LIBRARY.md`. |
| `ant-lib-import` | Import a Better BibTeX export (e.g. one Zotero collection) into the library; idempotent by DOI / citekey. |
| `ant-lib-export` | Write the library as BibLaTeX (default) or classic BibTeX (`--classic`). |
| `ant-lib-check` | Validate library sidecars and every `papers: {citekey: role}` reference in the h5adstore. |

Paper records are `<citekey>.md` sidecars with BibLaTeX field names plus
`anthive_*` fields (`lib/library.py`). Datasets/collections point at them
with `papers:` in their own front matter; nothing in the library points
back at datasets.

## The two metadata stores

Since the 2026-06 migration there are two separate metadata files per
dataset, with a strict split:

| File | Contents | Editor |
| --- | --- | --- |
| `<file>.md` (anthive-catalog) | `id`, `sha256`, `n_obs`, `layer_stats`, `obscat_include`, `obsnum_include`, `default_layer`, `top_genes`, `top_cat`, `top_num`, etc. | `ant-md` |
| `<file>.h5ad.mus` (experimental annotation) | `organism`, `tissue`, `cell_type`, `technology`, `suspension_type`, `disease`, `created`, `updated`, … — the **mus joint vocabulary** | `mus` CLI ([codeberg.org/mfiers/mus](https://codeberg.org/mfiers/mus)) |

Plus a `.env` cascade — folder-level mus fields that apply to every file
in/below that folder. `lib/mus.py` resolves the cascade + sidecar into the
effective annotation served to clients.

## Validators

- `ant-md-check` checks frontmatter shape and cross-references.
- `ant-h5ad-check` (in `bin/ingest/`) checks layer contracts; `ant-duckdb-check`
  (also `bin/ingest/`) checks the DuckDB schema.
- `ant-check` is the umbrella that runs all three.
- `ant-api-check` is the integration check — hits a running ant-serve.
- `ant-s3-check` is read-only S3 parity verification.

## Genesets

`ant-geneset` operates on `data/genesetstore/` (a separate git repo, sibling
of `data/h5adstore/`). Each study has a folder with `.grp` (one gene per
line) and `.rnk` (gene\tscore) files. `bin/annot/ant-score` reads these and
computes per-cell module scores.
