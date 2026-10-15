# Dataset ingestion: checks and the S3 watch route

Status: P1 of `doc/DESIGN_dataset_ingestion.md` (2026-10-05), the S3 watch route (2026-10-14, design
section 21) and the deposit API (2026-10-15, REST 2.46.0, design section 22, server side). `ant
ingest-check`, `ant ingest-index`, `ant ingest-watch` and the `/datasets/deposit` routes exist; the vark
commands and the skill for depositors come next, and there is no admin web page. The check ids are frozen:
the curator skill is written against them, so an id is never renamed.

The first part of this page is the how-to for depositors and operators (through the API, or with mus and S3
keys). The second part describes `ant ingest-check` and every check.

## Depositing through the API (signed in, no S3 keys)

<details>
<summary>In short: the server hands out presigned upload URLs for your files, the watcher checks and publishes them, and you follow the deposit with your Anthive login.</summary>

Who may deposit: signed-in members of `datasets_upload_groups` (default `admins` and `curators`; container
sites `datasets.upload_groups`). Deposits are off until an operator sets `datasets_deposit_enabled: true`
(container sites `services.dataset_deposit: true`). The vark client will do the steps below for you; the REST
shapes are in `doc/API_CHANGELOG.md` (2.46.0).

1. **Read the rules** with `GET /api/datasets/deposit/vocab`: the file names per kind, the limits (200 GB per
   deposit by default), the mus vocabulary the server checks against, the sidecar keys the route reads, and
   templates for the seed `.md` and `index.md`.
2. **Prepare the files** as for the watch route below: `<basename>.h5ad` (log-normalised X, raw counts in
   `layers['counts']`, an embedding), the collapsed sidecar `<basename>.h5ad.mus` with `sha256=` of the h5ad,
   `license=` and `data_use=`, the seed `<basename>.md` with front matter, and for a new group `index.md`. A
   new group is named like a paper (`Smith2024`); an existing group is spelled exactly as in the catalogue.
3. **Declare the deposit**: `POST /api/datasets/deposit` with the group, the basename and every file's
   name, size, sha256 and kind. The answer has one presigned URL per small file (send the listed headers
   with the PUT) and part URLs for large ones. They write only the deposit's own staging folder: nothing
   reaches the catalogue's incoming folder before you complete, and an old URL can never change a deposit
   afterwards.
4. **Upload**, then `POST /api/datasets/deposit/<id>/complete`. The server checks every part and the sidecar
   and says what will hold the deposit (no `license`, no `data_use`, `access=public`).
5. **Follow it**: `GET /api/datasets/deposit/<id>` shows the state, the dataset id and version once it is
   live, held reasons, and the failed checks with their fix; `.../report` has the full check report without
   other datasets' details or server paths. With SMTP configured you also get an e-mail when it goes live,
   is held or fails.
6. **Fix and retry**: deposit again. A corrected sidecar or seed for an h5ad of yours that still waits in
   incoming needs no new h5ad upload (leave the h5ad out of the file list). A new deposit of byte-identical
   files is checked again.
7. **Withdraw** with `POST /api/datasets/deposit/<id>/withdraw` while it is not being processed and not yet
   live: the uploads are aborted and the deposit's own files are deleted. A correction is withdrawn together
   with its original, not alone.

To change only the metadata of a published dataset (sidecar keys, new caveats in the seed `.md`), deposit
the sidecar and/or the seed `.md` with `update_of` and no h5ad: the published h5ad is reused and the dataset
gets a patch version.

A deposit larger than 5 GB completes in the background: complete answers at once and the status shows how
many files and bytes are copied. A wrong sidecar can be declared again in the same deposit (`POST .../urls`
with its new size and sha256). DE tables for a dataset that is already published are deposited alone, with
`update_of` and only DE files whose sidecars name the dataset (`de_dataset_id=` the file id such as `SMI`, or
the db id `ST6/SMI`); they are imported and the dataset gets a new patch version. Every DE sidecar needs
`de_format`, and the formats with one comparison per file (deseq2, edger, scanpy, seurat, glmmseq, glmmtmb)
also `de_term` and `de_contrast`; complete refuses a table without them.

Updates: a deposit for a dataset that is already published must say `"update_of": "<db_id>"`, and by default
only the user whose deposit published the dataset (or an admin) may deposit a new version. For datasets
published before deposits existed an admin names the owner (`POST /api/datasets/deposit/owners` or
`ant ingest-watch owner set <db_id> <user> --by NAME`); a caveat on the dataset records it. A dataset may not
be called `index` (or end in `.index`). The sidecar's
`depositor=` lines are ignored on this route: the login is recorded as the depositor (the `.md` `ingest:`
block, the store sidecar and the author of the store commit).

| State | Meaning |
| --- | --- |
| `uploading` | URLs handed out, not completed; expires 24 hours after the last URL refresh (at most 7 days) |
| `uploaded` | complete, waiting for the watcher's next listing |
| `seen`, `stable`, `checking` | the watcher has it (`checking` = pulled, converted, checked) |
| `live`, `held`, `failed`, `needs_decision`, `rejected` | as for the watch route below |
| `withdrawn`, `expired`, `superseded` | withdrawn by you or an admin; not completed in time; replaced before the watcher saw it |

Admins approve or reject a held deposit with `POST /api/datasets/deposit/<id>/approve` or `.../reject`
(body `{"note": "..."}`); it is the watcher's own approve, recorded with the admin's login. `GET
/api/datasets/deposit?all=true` lists every deposit. MCP clients see `my_deposits` and `deposit_status`
(read-only); data never goes through MCP.

Operators: uploads that no API deposit accounts for (lab members' `mus s3 upload`) are still published while
`ingest_watch.accept_unlinked` is true (the default for now); set it to false once depositors use vark.
Objects that carry a deposit's mark but were not completed by it are always refused (`needs_decision`).
Admins end an open deposit with `POST /api/datasets/deposit/<id>/expire`. The api presigns with `datasets_deposit_s3` (container sites: `secrets/s3_incoming_access_key_id`
and `s3_incoming_secret_access_key`), else with the `s3` keys. The api and the watcher must share
`ingest_state_dir`: on a container site run the watcher on the host with
`ANTHIVE_INGEST_STATE_DIR=<state_dir>/ingest` as the containers' uid (`ant-stack check` prints the line).

</details>

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
  dataset). Upload it with `--static index.md` (or `--static <basename>.index.md` when several datasets of the
  project are deposited together), or the route builds one from the sidecar keys `doi`, `pmid`, `year`,
  `journal`, `paper_title` and `authors`. For an existing group the store's `index.md` is used.
- **File and folder names** may hold letters, digits, `.`, `_` and `-` and must not start with `.` or `_`.
  Anything else is reported as malformed and left alone.
- **Access.** `access=internal` (login only) is the default for a new dataset and is set, with a caveat,
  when the sidecar says nothing. `access=public` is always held for an admin, also for updates of public data.
  Data with ROSMAP-derived values stays internal.
- **Licence and consent.** Set `license=` (an SPDX id such as `CC-BY-4.0`, or `in-house`) and `data_use=open`
  or `data_use=controlled`. Without them the deposit is held (`licence_unknown`, `data_use_unknown`).
- **Who deposited.** Optional `depositor=` and `depositor_email=` in the sidecar become the author of the
  store commit (a plain name and a valid e-mail address; anything else is recorded in the changelog instead).
- **DE tables** that come with the dataset (`mus s3 upload` of a `de/` folder whose sidecars name the dataset
  by `de_dataset_id` or `de_dataset_h5ad`) are imported after the dataset is live. DE tables for a dataset
  that is already published still go through `ant-ingest` by hand.

### What happens

`ant ingest-watch` lists `__incoming/` every 5 minutes. An upload counts as complete when the h5ad, its
sidecar and the project's static and DE files were identical in two consecutive listings and the newest is
at least 2 minutes old. Then, one dataset at a time and at low priority:

1. pull into the work dir and compare the h5ad with the sidecar's `sha256`;
2. write the catalogue `.md` (new dataset: version 1.0.0 and a first changelog entry; existing dataset: see
   "Updating a dataset");
3. `ant-convert`, then `ant-ingest-check` (the checks below);
4. errors stop here (`failed`), held reasons stop here (`held`), warnings pass;
5. place the files in the store (crash-safe: staged next to their targets, then renamed with the `.md`
   last), rescan the local ant-serve and check that it serves the new version, push the h5ad, sidecar,
   duckdb and layer files to the catalog on S3;
6. post steps: geneset scores, gene stats, module variability, facet tests (uncounted until an admin adds
   the dataset to the cohort table), the overlap index, the catalog manifest; DE tables; a git commit of
   the `.md`, `.h5ad.mus` and `index.md` in the store repository (not pushed unless
   `ingest_watch.store_git_push: true`);
7. delete the objects this dataset used from `__incoming/`, after checking that the new version is served and
   that its catalog copy on S3 has the right size and sha256 (`ingest_watch.delete_incoming_after_ingest`).
   Files that were not used stay. Static files of the project stay as long as another dataset of the project
   is still waiting in `__incoming/` (held, failed, needs_decision or not yet processed).

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
| `held` | clean, but an admin must decide (`public_access`, `licence_unknown`, `data_use_unknown`, `controlled_access`, `donor_overlap`) | read the report; fix the sidecar and upload it again, or approve it (below) |
| `failed` | errors in the checks, a sha256 mismatch, a convert failure | read the report, fix, upload again |
| `needs_decision` | the store, S3 or the update conflicts in a way the route does not resolve | see below |
| `rejected` | an admin rejected it | remove the deposit from S3 by hand |

Interrupted work (S3 errors, a killed tool, too little disk space, the server restarting, a stop of the
service) is not a state of its own: the item stays `stable` and is retried after 2, 4, 8 ... minutes.

### Approving or rejecting a held deposit

```bash
ant ingest-watch approve __incoming/<Group>/<basename>.h5ad --by "Your Name" --note "licence checked"
ant ingest-watch reject  __incoming/<Group>/<basename>.h5ad --by "Your Name" --note "wrong cohort"
```

`approve` lets the item through the held reasons it has now (errors still block) and the next poll publishes
it; the dataset gets a caveat naming who approved it and why, and the OS user who ran the command is recorded.
Any change to the deposit (the data, the sidecar, a seed or a static file) voids the approval. `reject` marks
it final; nothing is published or deleted. Both run only on the primary site, on the watcher's host: whoever
can run them there is trusted as an admin of the route. Deposits made through the API can also be approved or
rejected by a signed-in admin (`POST /api/datasets/deposit/<id>/approve` or `.../reject`).

Updates of a dataset that is published with `access=public` are always held, so a person approves every new
version of public data.

A deposit in a final state is looked at again only when its h5ad or sidecar changes (new ETag). Uploading
a corrected sidecar is enough to retry a held or failed deposit.

### Resolving needs_decision

The route stops when the store has an h5ad without its `.md`, a `.md` without its h5ad (a dataset that only
lives on S3, or a retired one), archived versions without a current `.md`, staged files without a journal, a
catalog h5ad on S3 for a dataset that is not in the local store, a static file that differs from the store
copy, an update that changes `access`, `data_use` or `license`, an update marker that does not match, or a
`.md` it cannot edit with a verified minimal change. Read the status note and decide by hand:

- the store files are leftovers: move them to an underscore folder (`<Group>/_backup_<date>/`) and upload the
  sidecar again (a new ETag) so the watcher retries;
- the dataset exists on S3 only: `ant sync pull '<Group>/<basename>*'` first, then re-upload the sidecar;
- the `.md` is unusual: make the change by hand (version bump, changelog entry, caveat) and run
  `ant-ingest` for that project;
- a static file differs: replace one of the two copies, or rename the deposited file;
- access, data_use or license change: edit the store `.h5ad.mus` by hand (with a caveat and a version bump),
  then upload the sidecar again.

### Updating a dataset

Upload again under the same `<data_project>/<basename>`. To make sure the upload lands on the dataset you
mean, add `update_of=<db_id>` or `previous_sha256=<sha256 of the published h5ad>` to the sidecar: a mismatch
stops the item (`needs_decision`). With `ingest_watch.require_update_marker: true` an update without either
stops too; this protects against two depositors who pick the same group and file name. It is off by default
(updates publish as new versions without asking): the cost is that a name collision silently produces a new
version of someone else's dataset (the old one stays restorable). The same checks run.
When they are clean:

- a different h5ad (another sha256) becomes a new minor version (1.0.0 to 1.1.0), with a changelog entry
  naming the previous sha256 and what changed (cells, genes, obs columns, sidecar keys);
- the same h5ad with new sidecar keys, overridden keys or new caveats in the deposited `<basename>.md`
  becomes a patch version (1.0.0 to 1.0.1);
- an identical upload changes nothing (new static files are still placed).

Curated values win: a sidecar key that the store already has with another value is kept, and the changelog
says what the deposit wanted. To replace it, add `override_<key>=true` next to the key in the sidecar. Changes of
`access`, `data_use` and `license` are never applied automatically. From a deposited `<basename>.md` only new
caveats are applied; if it differs in other keys it stays in `__incoming/` with a warning.

Caveats, curation fields and the facet mapping of the old `.md` stay as they are; the route appends one caveat
saying which version was replaced. The served dataset is swapped under `/admin/release` and `/admin/restore`, through a journal: if the
process dies in the middle, the next start finishes or undoes the swap before anything else, and a finished
swap then still gets its catalog push, scores, facet tests and store commit. The same holds when the service
is stopped or restarted during the post steps (which can take hours): the steps that did not finish run on the
next start, and the incoming objects are deleted only after that. The previous
version is kept in `<Group>/_versions/<basename>/<version>/` (hard links to the h5ad, `.md`, `.h5ad.mus` and old
scores, so no extra space while they are unchanged) and on S3 under `<Group>/_versions/<basename>/<version>/`. To go back, copy those files over the
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
make ingest-watch-status
# state, work and tmp dirs go to INGEST_WATCH_DIR (default ../anthive4_state); the watcher refuses dirs
# inside the checkout unless ingest_watch.allow_repo_dirs is true
ant ingest-watch --once --store /scratch/store --prefix __incoming_test/ \
    --catalog-prefix __ingest_test_catalog/ --api-url http://127.0.0.1:18850   # a test against a scratch store
```

On a server it runs as its own systemd unit (`config/examples/anthive-ingest-watch.service.example`), never
under `ant-supervisor`. It reaches the local ant-serve over HTTP only, keeps working while ant-serve restarts
(rescans retry with backoff, an unserved item is rescanned on the next poll) and takes the per-dataset writer
lock only for the seconds of a swap. A file lock in the state dir stops two watchers, or a watcher and
`ant-ingest`, from running at once. Stopping the service kills running tools (their item is retried); the
watcher never starts a swap after the stop signal. Work dirs of live items are removed at once, those of held
and failed items after `work_ttl_days` (14). A pull waits while less than 2x the h5ad size plus `min_free_gb`
(50) is free.
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
| `files.size` | size against `datasets_upload_max_gb` (200 GB) | over the limit | split the dataset |
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

The vark and pyvark commands for deposits, the depositor skill and an admin page (design 22.8). For the
watch route: the compose service, DE-only deposits, size routing to HPC (design section 21.8).
