# Dataset ingestion: checks and the S3 watch route

Status: P1 of `doc/DESIGN_dataset_ingestion.md` (2026-10-05) plus the S3 watch route (2026-10-14, design
section 21). `ant ingest-check`, `ant ingest-index` and `ant ingest-watch` exist; the presigned-upload route,
its worker and the admin pages (P3 and later) do not. The check ids are frozen: the curator skill (P2) is
written against them, so an id is never renamed.

The first half of this page is the how-to for depositors and operators of the watch route. The second half
describes `ant ingest-check` and every check.

## Depositing with mus (the S3 watch route)

<details>
<summary>In short: <code>mus s3 upload</code> the h5ad with its sidecar; a watcher checks it and publishes it as an internal dataset, or records why not.</summary>

### Deposit

Prepare the folder as for any mus deposit (skill `mus-h5ad-deposit`): the `.h5ad` with log-normalised X and
raw counts in `layers['counts']`, the annotation in `mus.cfg` / `<basename>.h5ad.mus`. Then:

```bash
mus h5ad check microglia.h5ad
mus s3 upload -m "first deposit" microglia.h5ad \
    --static microglia.md --static index.md --static paper_supplement.pdf
```

This writes `__incoming/<data_project>/microglia.h5ad`, the collapsed sidecar `microglia.h5ad.mus` (with
`sha256=`) and `__incoming/<data_project>/static/...`.

- **The catalogue `.md`.** Upload your `<basename>.md` (title, description of 200 characters or more, facet
  mapping in `api:`, `replicate_unit`, caveats) with `--static`. It is used as the seed of the dataset `.md`
  and is not copied into `static/`. Leave out `version`, `changelog`, `sha256` and `n_obs`: the route fills
  them. Without a seed the route writes a minimal `.md` from the sidecar, which usually fails
  `meta.required_keys` (no description).
- **A new group** needs an `index.md` (group, `group_id`, doi or paper title, year, authors, default
  dataset). Upload it with `--static index.md`, or the route builds one from the sidecar keys `doi`, `pmid`,
  `year`, `journal`, `paper_title` and `authors`. For an existing group the store's `index.md` is used.
- **Access.** `access=internal` (login only) is the default and is set, with a caveat, when the sidecar
  says nothing. `access=public` is held for an admin. Data with ROSMAP-derived values stays internal.
- **Licence and consent.** Set `license=` (an SPDX id such as `CC-BY-4.0`, or `in-house`) and `data_use=open`
  or `data_use=controlled`. Without them the deposit is held (`licence_unknown`, `data_use_unknown`).
- **Who deposited.** Optional `depositor=` and `depositor_email=` in the sidecar become the author of the
  store commit.
- **DE tables** that come with the dataset (`mus s3 upload` of a `de/` folder whose sidecars name the dataset
  by `de_dataset_id` or `de_dataset_h5ad`) are imported after the dataset is live. DE tables for a dataset
  that is already published still go through `ant-ingest` by hand.

### What happens

`ant ingest-watch` lists `__incoming/` every 5 minutes. An upload counts as complete when the h5ad, its
sidecar and the project's static and DE files kept the same ETag and size over two polls (or are older than
15 minutes). Then, one dataset at a time and at low priority:

1. pull into the work dir and compare the h5ad with the sidecar's `sha256`;
2. write the catalogue `.md` (new dataset: version 1.0.0 and a first changelog entry; existing dataset: see
   "Updating a dataset");
3. `ant-convert`, then `ant-ingest-check` (the checks below);
4. errors stop here (`failed`), held reasons stop here (`held`), warnings pass;
5. place the files in the store, rescan the local ant-serve, push the h5ad, sidecar, duckdb and layer files
   to the catalog on S3;
6. post steps: geneset scores, gene stats, module variability, facet tests (uncounted until an admin adds
   the dataset to the cohort table), the overlap index, the catalog manifest; DE tables; a git commit of
   the `.md`, `.h5ad.mus` and `index.md` in the store repository (not pushed unless
   `ingest_watch.store_git_push: true`);
7. delete the objects this dataset consumed from `__incoming/`, after checking that the dataset is served and
   that its catalog copy on S3 has the right size and sha256 (`ingest_watch.delete_incoming_after_ingest`).

A failing post step is a warning: the dataset is already live. Other sites get the dataset the usual way,
from S3: `ant sync pull` (container sites: `ant-stack ingest -- ant sync pull --include-duckdb '<Group>/*'`),
then a rescan. The `.md`, `.h5ad.mus` and `index.md` reach them through the store repository once someone
pushes it.

### Status

```bash
ant ingest-watch status            # one line per deposit: state, db id, version, note
ant ingest-watch status --json     # plus the deletes and the versions
```

The same table is in `<ingest_state_dir>/ingest_watch_status.json` (default `var/ingest/`), rewritten after
every poll. Each deposit has its `ant-ingest-check` report (JSON and markdown) under
`<ingest_state_dir>/reports/` and the tool output in `<ingest_state_dir>/logs/`. With
`ingest_watch.notify.webhook_url` or `notify.email_to` + `notify.smtp_host` set, live, held, failed and
needs_decision send a JSON message.

| State | Meaning | What to do |
| --- | --- | --- |
| `seen` | listed, upload may still be running | wait one or two polls |
| `stable` | complete, queued (or waiting for a retry after an S3 error) | wait |
| `pulled`, `checked` | being processed | wait |
| `live` | published | review the dataset page; `served` and `catalog_verified` in `--json` say whether the incoming objects could be deleted |
| `held` | clean, but an admin must decide (`public_access`, `licence_unknown`, `data_use_unknown`, `controlled_access`, `donor_overlap`) | read the report; fix the sidecar and upload it again, or publish by hand after review |
| `failed` | errors in the checks, a sha256 mismatch, a convert failure | read the report, fix, upload again |
| `needs_decision` | the store holds something the route cannot reconcile | see below |

A deposit in a final state is looked at again only when its h5ad or sidecar changes (new ETag). Uploading
a corrected sidecar is enough to retry a held or failed deposit.

### Resolving needs_decision

The route stops when the store has an h5ad without its `.md`, a `.md` without its h5ad (a dataset that only
lives on S3, or a retired one), or a `.md` it cannot edit with a verified minimal change. Look at the store
folder named in the status note and decide by hand:

- the store files are leftovers: move them to an underscore folder (`<Group>/_backup_<date>/`) and upload the
  sidecar again (a new ETag) so the watcher retries;
- the dataset exists on S3 only: `ant sync pull '<Group>/<basename>*'` first, then re-upload the sidecar;
- the `.md` is unusual: make the change by hand (version bump, changelog entry, caveat) and run
  `ant-ingest` for that project.

### Updating a dataset

Upload again under the same `<data_project>/<basename>`. The same checks run. When they are clean:

- a different h5ad (another sha256) becomes a new minor version (1.0.0 to 1.1.0), with a changelog entry
  naming the previous sha256 and what changed (cells, genes, obs columns, sidecar keys);
- the same h5ad with changed sidecar keys becomes a patch version (1.0.0 to 1.0.1); the uploaded sidecar wins
  for every key it sets, keys only in the store's sidecar stay;
- an identical upload changes nothing.

Caveats, curation fields and the facet mapping of the old `.md` stay as they are; the route appends one caveat
saying which version was replaced. The served dataset is swapped under `/admin/release` and `/admin/restore`.
The previous version is kept in `<Group>/_versions/<basename>/<version>/` (h5ad, `.md`, `.h5ad.mus`, the old
scores) and on S3 under `<Group>/_versions/<basename>/<version>/`. To go back, copy those files over the
current ones, bump the version with a changelog entry, convert and rescan.

### Purging old versions

```bash
ant ingest purge WT6/MIC --dry-run          # what would go (local folders and S3 keys)
ant ingest purge WT6/MIC                    # keep only the current version; asks for "yes"
ant ingest purge WT6/MIC --keep 2           # keep the current and the newest archived version
ant ingest purge --all --keep 1 --yes       # every dataset, no question
```

Purge only removes archived versions (`_versions/` folders and the matching S3 keys); `--local-only` leaves
S3 alone. The watcher never purges.

Removing a dataset itself is a different thing. To take it out of the catalogue but keep everything, mark it
inactive (`ant inactive <db_id>`, a `.inactive` sidecar) and rescan; to delete it, remove its files from the
store and its keys from S3 by hand, commit the removal in the store repository, and rescan.

### Cleaning up incoming objects

The watcher deletes what a live dataset consumed. Held, failed and needs_decision deposits stay in
`__incoming/` until you replace or remove them. Leftovers of the manual route are listed and removed with:

```bash
ant ingest prune --dry-run
ant ingest prune
```

### Running the watcher

```bash
make ingest-watch            # foreground, needs ingest_watch.enabled: true in anthive.yaml
make ingest-watch-once       # one poll
ant ingest-watch --once --store /scratch/store --prefix __incoming_test/ \
    --catalog-prefix __ingest_test_catalog/ --api-url http://127.0.0.1:18850   # a test against a scratch store
```

On a server it runs as its own systemd unit (`config/examples/anthive-ingest-watch.service.example`), never
under `ant-supervisor`. It reaches the local ant-serve over HTTP only, keeps working while ant-serve restarts
(rescans retry with backoff, an unserved item is rescanned on the next poll) and takes the per-dataset writer
lock only for the seconds of a swap. A file lock in the state dir stops two watchers from running at once.
Settings are in the `ingest_watch:` block of `anthive.default.yaml` (env `ANTHIVE_INGEST_WATCH_<KEY>`).
`ant-score-fleet` waits while the host is busy; `post_step_timeout_seconds` (default 6 hours) stops a step
that waits too long, and it is then a warning.

</details>

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

The presigned-upload route, the submission store, its worker, the admin pages and the skill (P2 to P5 of the
design). For the watch route: the compose service, DE-only deposits, size routing to HPC (design section 21.8).
