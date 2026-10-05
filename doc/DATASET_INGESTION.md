# Dataset ingestion: checks

Status: P1 of `doc/DESIGN_dataset_ingestion.md` (2026-10-05). `ant ingest-check` and `ant ingest-index`
exist; the upload route, the worker and the admin pages (P3 and later) do not. This page describes what
exists. The check ids are frozen: the curator skill (P2) is written against them, so an id is never renamed.

## Running it

```bash
ant ingest-index build                                   # once, then after the store changes
ant ingest-check /path/to/Group --dataset basename --json report.json
```

The folder holds `<basename>.h5ad`, `<basename>.md`, `<basename>.h5ad.mus` and, for a new group,
`index.md`. It can be a staging folder or a group that is already in the store (then the dataset counts
as an existing one: `version.initial` is skipped and some findings are warnings).

Read-only by construction. The scratch convert (when no `.duckdb` is given or found next to the h5ad)
hard-links the h5ad into `<tmp_dir>/ingest_check/convert/<key>/<Group>/` and runs `ant-convert` there; the
result is cached by the h5ad hash plus the `.md` keys that `ant-convert` reads. Output files inside the
submission folder or the store are refused. `ant duckdb-check` is not read-only, so it runs on a copy.

Options: `--audience curator|admin` (default admin), `--checks` and `--skip` (ids or families),
`--no-convert`, `--duckdb PATH`, `--sha256`, `--mode new|existing|auto`, `--profile-only`, `--list-checks`.

## The report

JSON with schema `anthive.ingest_check/1`, plus a markdown summary. `result` is `fail` when any check
failed. `publish` is `auto` only without errors and without held reasons, otherwise `held`.
Held reasons: `controlled_access`, `data_use_unknown`, `licence_unknown`, `public_access`, `donor_overlap`.

Controlled-access data never appears in a report. The donor crosswalk is read only to count how many donor
values resolve. Reports carry counts and 16-digit digests of donor keys, never identifiers, aliases or
canonical ids; a last scrub replaces any crosswalk token that slipped into a text. The curator view omits
`admin_detail` (matched dataset ids, cohort names, per-cohort match tables).

## Overlap indexes

`ant ingest-index build` reads every active dataset read-only and writes, under `ingest_index_dir`
(default `<state_dir>/ingest/index`, mode 0700/0600, refused inside the store):

- the donors of each dataset: digest of the donor key, sex, age, APOE genotype, Braak stage, PMI;
- the cells: sorted 64-bit hashes of (normalised barcode, donor digest) and of the barcode alone;
- the gene names, per organism (for `genes.ids`).

The build is incremental (`.duckdb` and `.md` size and mtime). Barcode and donor hashes use BLAKE2b
(8 bytes), not xxhash, to avoid a dependency.

## The checks

Status per check: `pass`, `warn`, `fail` (an error), `skip` (input absent). "Existing" means the folder
is a group in the store.

| id | What it checks | Fails when | Typical fix |
| --- | --- | --- | --- |
| `files.sha256` | h5ad hash against `--sha256` and the `sha256` in the `.md` | mismatch (existing dataset: warn) | re-upload, or drop the stale `sha256` |
| `files.size` | size against `datasets_upload_max_gb` | over the limit | split the dataset |
| `files.filename` | basename and group folder names | spaces, special characters, over 50 characters, group starting with `_` or `.` | rename |
| `h5ad.structure` | h5ad opens, has X or layers, obs, var | unreadable or incomplete; warns without an `obsm` embedding | re-save with anndata |
| `duckdb.integrity` | `ant duckdb-check` on a copy of the (scratch) database | convert failed or tool exits non-zero | fix what `ant-convert` reads |
| `meta.md_parse` | the `.md` exists and parses | no or broken YAML | write the front matter |
| `meta.catalog` | id and group id valid and unique (`md_check`, fixing off; `scripts/validate_md.py`) | id not 3 characters, duplicate in the group or store; missing id is a warning with a proposal | set `id:` |
| `meta.required_keys` | title, version, changelog, description of 200+ characters (key or markdown body) | any missing (description only warns for existing datasets) | fill in |
| `meta.index_md` | group `index.md`: group equals the folder, 3-character unique `group_id`, doi or paper title, year, authors, default dataset | new group incomplete; existing group disagrees with the store | fix `index.md` |
| `mus.vocab` | `.h5ad.mus` plus `.env` cascade against the mus vocabulary | required field missing or value outside the vocabulary; missing soft fields warn | `mus vocab` |
| `version.initial` | new dataset: `1.0.0` and one changelog entry with date, h5ad sha256, changes | otherwise (skipped for existing datasets) | set version and changelog |
| `layers.counts` | a layer other than X holds non-negative integers (2,000 cells, seed 0) | none, unless `counts_unavailable: <reason>` (then warn) | add `layers['counts']` |
| `layers.x_lognorm` | X is not integers, not scaled, below 20, expm1 row sums constant | negative, integer or large values; warns when the normalisation cannot be confirmed (declare `normalisation:`) | put log1p(CP10k) in X |
| `layers.consistency` | median r of X against log1p(CP10k(counts)) on 1,000 cells | below 0.95 (0.95 to 0.99 warns); skipped with a declared `normalisation:` | write both from the same cells |
| `genes.ids` | symbols, unique, 90%+ known for the organism (needs the index) | duplicates, Ensembl ids, under 70% known | use symbols |
| `facets.namespace` | every `ant_*` target is a registered facet (strict) | unregistered; deprecated warns | plain column name |
| `facets.mapping_static` | `facet_lint.lint_api` without data | rename chains, dtype, range, derive form errors | see the rule in the message |
| `facets.mapping_data` | source columns exist, numeric facets hold numbers in range, `replicate_column` exists | missing source column | correct names |
| `facets.unmapped_values` | every author value is mapped, renamed, hidden or defaulted; values land in the facet vocabulary | any unresolved value (listed) | extend the map |
| `facets.braak_range` | `ant_braak` whole numbers 0 to 6 | otherwise | map to integers |
| `facets.core_present` | donor, sex, disease (human, mouse); Braak for Alzheimer data | `ant_donor` missing with `replicate_unit: donor`; other gaps warn | map the columns |
| `donor.uid_form` | `ant_donor_uid` is a template or join derive | missing for a human donor dataset, malformed, unknown source | template form |
| `donor.uid_resolves` | join form: cohort known, share of fallbacks | unknown cohort or table; over 20% fallback warns | right cohort |
| `donor.overlap_ids` | donors (uid digests) shared with other groups | shared without a designation | `is_secondary` / `reanalysis_of` / `donor_group` / `facet_donor_exclusions` |
| `donor.overlap_demographics` | unique demographic matches per stored cohort, minus a chance level from age-shifted copies | never fails: held (`donor_overlap`) at 50%+ with 20%+ beyond chance; warns at 20%+ or 15+ donors beyond chance | declare the relation |
| `cells.overlap` | hashed (barcode, donor) pairs against every stored dataset | same cells both ways (95%), or 50%+ inside one dataset without `is_secondary` + `reanalysis_of`; same barcodes with other donors warn | use the update route |
| `design.designation` | `reanalysis_of` exists and implies `is_secondary`; `donor_group` slug | broken lineage | fix the keys |
| `design.replicate_unit` | `replicate_unit` declared and valid, `replicate_column` exists, samples nest in donors | missing, invalid, column absent, sample in several donors | declare it |
| `design.replicates_per_group` | units in total (6) and per facet level (3), the facet test thresholds | never fails; warns below, info for `none` | none |
| `design.disease_basis` | `disease_basis` (and `disease_derivation` for `braak_cerad`) when `ant_disease` is published | missing or invalid | declare it |
| `access.level` | `access` internal or public | missing or invalid; public is held (`public_access`) unless `datasets_auto_publish_public` | `access=internal` |
| `access.controlled` | `data_use` open or controlled; a controlled cohort in the donor join | invalid value; controlled, unset or a controlled join cohort is held | state `data_use` |
| `access.licence` | `license` is an SPDX style id or `in-house` | not SPDX style; missing or unknown is held (`licence_unknown`) | set `license=` |

`data_use` is a new soft mus field (`open` or `controlled`), read from the `.h5ad.mus` or the `.md`
until the upstream vocabulary has it. The crosswalk may carry an optional `controlled` column per
cohort; `ant ingest-check` reads only which cohorts are flagged.

## Not done yet

The upload route, the submission store, the worker, the admin pages and the skill (P2 to P5 of the design).
