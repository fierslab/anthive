# Design: the artefact store

Status: design 2026-10-16, approved by Mark with the section 16 defaults on 2026-10-18. Phase A1 (read
side), A2 (deposits, publication, versions, purge) and A3 (de_table, 2026-10-19) built for REST 2.47.0,
see sections 17 to 19. Section 23 of `doc/DESIGN_dataset_ingestion.md`
points here.
It builds on the S3 watch route (that document's section 21) and the deposit API (section 22, REST 2.46.0
and 2.46.1, `lib/dataset_deposit.py`, `vark datasets ...`). Nothing in this document changes those routes
until the phases of section 15 are built.

Why a separate document. `DESIGN_dataset_ingestion.md` is 1,200 lines about getting an h5ad into the
catalogue. Artefacts reuse its deposit machinery, but most of the design is elsewhere: serving files from S3,
collections without datasets, the web UI, MCP, ant-sync and a separate security surface. A document of its own
keeps each review readable, and the ingestion design gets a short section 23 that says what is shared.

Mark's decisions (2026-10-16), binding for everything below:

1. Custom DE tables and every other generated file (figures, tables, notebooks, reports, models, other)
   come in through the same prepare, QC and upload route as datasets: skills, vark and the deposit API.
   Uploaders never hold S3 keys.
2. Raw files are kept after ingestion in the static area of their dataset or collection. DE tables are in
   addition imported into the dataset's DE tables. The DE-only deposit of 2.46.0 becomes "an artefact of
   kind `de_table`, plus import".
3. S3 is the source of truth and serves the files. The API checks access and answers with a redirect to a
   short-lived signed GET. Objects are never public. Small inline files (figures, previews) may be synced
   locally. A site without S3 read access falls back to a local copy when there is one. ant-sync never pulls
   large artefacts by default.
4. Artefacts are versioned and a published version is immutable. A change is a new version. Old versions
   stay until purged. Each version records its sha256, its provenance (code and commit, parameters, software
   versions, author, date) and the dataset version it was made from. The latest version is shown by default.
5. A group `analysts` may upload artefacts. The groups are configurable, like `datasets_upload_groups`.
6. Analysts may create collections without single-cell datasets. They pick an explicit group name (the
   `data_project` naming rule) and give at least a title and authors.
7. At most 100 GB per file, configurable.
8. Purge is manual for now, by the uploader or an admin.

---

## 1. What exists and is reused

| Piece | Where | Use here |
| --- | --- | --- |
| Deposit records, key locks, rate limiter, per-user limits | `lib/dataset_deposit.py` (`DepositStore.reserve` in `BEGIN IMMEDIATE`, `open_keys`, `RateLimiter`) | an artefact deposit is a deposit with `deposit_kind: "artefact"`; same tables, same limits, same states |
| Staging prefix | `datasets_deposit_staging_prefix` (`__deposits/<site_id>/<deposit_id>/`) | presigned URLs write only there; complete copies into incoming |
| Verified copy | `complete`: staging ETag recorded at verification, `CopySourceIfMatch` (also through `UploadPartCopy` above 5 GB), MD5 of bytes read and the copy's ETag compared because the lab bucket ignores conditional headers | unchanged for artefact files; the same check guards the copy from incoming to the final key |
| Guarded deletes | `lib/ingest_pipeline.delete_consumed`: one key at a time, HEAD-compared with the expected ETag, `IfMatch` only after the self-test, every delete logged | incoming cleanup and purge |
| Key validation | `ingest_pipeline.SEGMENT_RE` (`^[A-Za-z0-9][A-Za-z0-9._-]*$`), `ensure_inside` | every artefact name and every local write |
| Watcher | `lib/ingest_watch.py` states, settle signature, resume markers, notifications, approve / reject / retry | a new item type `artefact` keyed by deposit id |
| DE import | the DE-only deposit (design 22.10, third review R1): import into a copy of the duckdb under the writer lock, release / restore only when every table went in, patch version, `.de.parquet` push, `.md` commit | step 9 of the pipeline; sources move from `<group>/de_sources/` into the artefact |
| Redaction | `sanitize_text`, `redacted_report` | QC reports shown to uploaders |
| Access | `lib/dataset_access.py` (`caller_sees_internal`, `internal_requires_auth`, hidden and `.inactive` datasets) | artefacts inherit their scope's visibility |
| Download redirect | `/databases/{db_id}/download/h5ad` answers 307 to a 5-minute presigned URL; `/download-static/{filename}` streams a local file | the artefact download route follows the first pattern |
| Static area | `<Group>/static/`, listed by `Registry.list_artifacts` (local files only), always mirrored by `ant-sync` (`_is_static_path`, any extension, pushed and pulled) | artefacts live under `static/artefacts/`, which ant-sync must stop mirroring wholesale (section 11) |
| Groups | `/groups/{group_id}` resolves a group only through its datasets (404 without one); the catalog scan ignores folders without datasets | collections without datasets need a collection registry (section 2.5) |

One naming hazard. Caddy already serves pseudobulk files at `/artifacts/*` (American spelling, `config/Caddyfile`).
The REST routes below live under `/api/artefacts`, so they do not collide, but the web UI must not use
`/artifacts` for its pages.

---

## 2. Data model

### 2.1 Kinds

| Kind | Typical files | Inline preview | Kind-specific QC (section 6) |
| --- | --- | --- | --- |
| `de_table` | `.tsv`, `.csv`, `.tsv.gz`, `.csv.gz`, each with its `.mus` | no | DE rules, then import into the dataset |
| `figure` | `.png`, `.jpg`, `.webp`, `.svg`, `.pdf`, `.tif` | yes (raster thumbnails; SVG and PDF in a later phase) | image decode, pixel limit |
| `table` | `.tsv`, `.csv`, `.gz` of those, `.xlsx`, `.parquet` | first rows | parse, donor-identifier scan |
| `notebook` | `.ipynb`, `.qmd`, `.Rmd`, `.html` export | no | nbformat parse, secret scan |
| `report` | `.pdf`, `.html`, `.md`, `.docx` | no | secret scan |
| `model` | `.pt`, `.onnx`, `.pkl`, `.rds`, `.h5`, `.joblib`, `.zip` | no | size, type sniff; never loaded |
| `other` | anything on the allowlist of section 6 | no | generic only |

A kind is chosen by the uploader and fixed for the life of the artefact (a new version cannot change it).

### 2.2 Identifiers

- **Scope** is the dataset or the collection an artefact belongs to. A dataset scope is the dataset's file id
  (`SLM` of `AZ5/SLM`), because the file id is what `db_id` and `de_dataset_id` use and it survives a rename of
  the basename. A collection scope is the literal `_group`. The leading underscore cannot clash with a file id,
  but it means every tool that skips `_` folders (ant-sync, `md_check`, the catalog scan) needs an explicit
  rule for artefacts; section 11 has the one for ant-sync. `_group` never appears in an incoming key: the
  pipeline builds the final path itself.
- **Artefact id** is a slug chosen by the uploader, `^[a-z0-9][a-z0-9-]{1,63}$`, unique within its scope across
  all kinds, so a reference needs no kind. vark proposes one from the main file name.
- **Version** is `v1`, `v2`, ... assigned by the watcher at publication: one more than the highest number the
  artefact ever had, purged versions included, so a number is never reused. Integers rather than semver,
  because an immutable file has no meaningful patch or minor change.
- **Reference** is `<group_id>/<scope>/<artefact_id>[@vN]`, for example `S26/SLM/volcano-tvsc@v2` or
  `F26/_group/umap-atlas`. Without `@vN` it means the latest live version.

### 2.3 Layout

```
<Group>/static/artefacts/<FILEID|_group>/<kind>/<artefact_id>/
    artefact.md                head record: mutable, written only by the pipeline, git-tracked
    v1/manifest.json           immutable version manifest, git-tracked
    v1/files/<name>            the uploaded files, byte for byte (S3; local only when inline, section 3.3)
    v1/preview/<name>.webp     generated thumbnails (figures, first rows of a table as .txt)
    v2/...
```

The S3 keys are the same relative paths under the catalog prefix (`ingest_watch.catalog_prefix`, default
`s3.prefix`). `.gitignore` of the h5adstore gets `static/artefacts/**/files/` and `static/artefacts/**/preview/`;
the head record and the manifests are small text and are tracked like the dataset `.md` files. A file name
inside `files/` may have one subfolder level (`files/panels/a.png`) so that a notebook can keep its relative
links; deeper nesting is refused.

### 2.4 The version manifest (`vN/manifest.json`, schema `anthive.artefact/1`)

The manifest separates what the server recorded from what the uploader declared. The web UI, MCP and vark
show declared fields as "declared by the uploader".

```json
{
  "schema": "anthive.artefact/1",
  "ref": "S26/SLM/volcano-tvsc@v2",
  "artefact_id": "volcano-tvsc", "version": 2, "kind": "figure",
  "group": "Sun2026", "group_id": "S26", "scope": "SLM", "db_id": "S26/SLM",
  "title": "Volcano plot, treated vs control",
  "description": "Markdown, at most 20 kB.",
  "change_note": "Relabelled the axes.",
  "supersedes": 1,
  "access": "inherit",
  "files": [
    {"name": "volcano.pdf", "size": 182331, "sha256": "...", "content_type": "application/pdf", "role": "main"},
    {"name": "volcano.png", "size": 90211, "sha256": "...", "content_type": "image/png", "role": "supporting"}
  ],
  "made_from": [
    {"db_id": "S26/SLM", "dataset_version": "1.3.0", "h5ad_sha256": "..."}
  ],
  "declared": {
    "author": {"name": "A. Analyst", "orcid": "0000-0000-0000-0000"},
    "created": "2026-10-12",
    "code": {"repository": "https://gitlab.kuleuven.be/...", "commit": "3f2a...", "path": "src/03_figures/volcano.py", "dirty": false},
    "parameters": {"alpha": 0.05, "lfc_threshold": 1},
    "software": [{"name": "python", "version": "3.12.4"}, {"name": "scanpy", "version": "1.10.2"}],
    "environment_lock_sha256": "..."
  },
  "recorded": {
    "uploaded_by": "jdoe", "uploaded_by_name": "Jane Doe",
    "deposit_id": "dep_01J...", "published": "2026-10-16T10:12:00Z",
    "site_id": "intern", "server_version": "2.47.0",
    "checks": {"files.sha256": "pass", "figure.decode": "pass", "secrets.scan": "pass"}
  },
  "de": null
}
```

- `files[].sha256` is recomputed by the pipeline; the uploader's value is only compared with it.
- `made_from` is required for a dataset scope (at least the scope dataset). `h5ad_sha256` is filled by the
  server from the dataset's changelog entry of that version, never from the uploader. A collection scope may
  list several datasets, also of other collections, as long as the uploader can see each of them.
- `access` is `inherit` or `internal`. An artefact can narrow its scope's access, never widen it.
- `de` (only for `de_table`) holds the DE sidecar keys of each table (`de_format`, `de_term`, `de_contrast`,
  `de_method`, `de_study_id`, ...) and, after the import, `{study_id, rows, imported_at, dataset_version_after}`.

### 2.5 The head record (`artefact.md`)

YAML front matter, no body. It is what the listing reads; the manifests are read only for detail views.

```yaml
artefact_id: volcano-tvsc
kind: figure
scope: SLM
owner: jdoe              # first uploader; an admin may transfer it
created: 2026-10-14
title: Volcano plot, treated vs control   # title of the latest live version
latest: 2                # highest live version, null when every version is purged
versions:
  - {version: 1, state: purged, manifest_sha256: '...', published: 2026-10-14, purged: 2026-10-16, purged_by: jdoe, purge_reason: wrong contrast}
  - {version: 2, state: live, manifest_sha256: '...', published: 2026-10-16}
```

Only the pipeline writes it, under the dataset's (or collection's) writer lock, to a temp file renamed over
the old one, then pushes it to S3 and commits it. `manifest_sha256` ties each entry to its immutable manifest,
so an edited head record is detectable.

### 2.6 Collections without datasets

Today a collection exists for the API only through its datasets. Analysis collections need:

- a collection registry built at the catalog scan from every `<Group>/index.md` (not only those of groups
  with datasets), so `/groups/{group_id}` answers for a collection without datasets;
- `md_check` and `index_md.required_spec` to require `default_dataset` only when `collection_kind` is not
  `analysis`.

The minimal `index.md` of an analysis collection, written by the pipeline from the creation request:

```yaml
---
group: Fiers2026            # equals the folder; data_project rule ^[A-Z][A-Za-z0-9_]*\d{4}$
group_id: F26               # assigned by the server, unique in the store (3 characters)
collection_kind: analysis
title: Microglia state atlas, cross-study analyses
authors:
- Mark Fiers
year: 2026                  # number; taken from the name
access: internal            # internal (default) or public (held for an admin)
created_by: mfiers          # the login, recorded by the server
created: 2026-10-16
---
Optional markdown description.
```

Optional keys are the ones a dataset collection already uses (`description`, `doi`, `paper_title`, `papers`,
`license`). Every existing collection keeps working without `collection_kind` (it means `datasets`).

---

## 3. Access and delivery

### 3.1 Who sees what

| Scope | Visible to |
| --- | --- |
| a dataset | whoever sees the dataset: its resolved `access`, `internal_requires_auth`, not hidden, not `.inactive` (admins excepted) |
| `_group` of a collection with datasets | the most restrictive of its live datasets: public only when every one of them is public |
| `_group` of an analysis collection | the `access` key of its `index.md` |
| an artefact with `access: internal` | as above, and never anonymous callers |

The check runs on the artefact the server resolved, never on request parameters. The existing middleware
only recognises dataset query parameters (`DATASET_QUERY_PARAMS`), so every artefact route calls the access
check explicitly; a test enumerates the routes. An invisible artefact answers 404 `not_found`, the same as one
that does not exist.

If a dataset's access changes later (internal to public), its artefacts follow. A metadata update that changes
`access` already stops at `needs_decision`; the decision view lists the dataset's artefacts so the admin sees
what becomes public with it.

### 3.2 Downloads

`GET /api/artefacts/{group_id}/{scope}/{artefact_id}/v{N}/files/{name}` (or `latest` for `v{N}`):

1. resolves the artefact, the version and the file name from the manifest (the name is looked up, never joined
   into a path);
2. checks access and the per-user download rate;
3. when the site can read S3: 307 to a presigned GET for that one key, valid `artefacts_url_ttl_seconds`
   (300), signed with `ResponseContentDisposition: attachment; filename="<name>"` and `ResponseContentType`
   from the manifest, so the bucket never decides how a browser renders the bytes. The redirect carries
   `Cache-Control: no-store, private` and `Referrer-Policy: no-referrer`;
4. when it cannot (no S3 read access, or `artefacts_serve_local: prefer`) and a local copy exists with the
   manifest's sha256 (checked once and cached by mtime and size): streams it with `Content-Disposition:
   attachment`, `X-Content-Type-Options: nosniff` and `Content-Security-Policy: sandbox`;
5. otherwise 404 `not_on_this_site` with the reference, so vark can say where to get it.

Previews (`.../v{N}/preview/{name}`) are the one inline route: generated server-side WebP or text, always
local, `Content-Type` fixed by the server.

Each download is counted per user (metrics, and a line in the api log without the signed URL). A signed URL
cannot be revoked: after a purge or an access change it keeps working for at most the TTL.

### 3.3 Local copies

A site keeps locally the head records, the manifests, the previews and the inline files: files of kind
`figure`, and any file up to `artefacts_inline_max_mb` (2) of the other kinds. Everything else stays on S3
unless someone pulls it explicitly (section 11). The primary site that runs the watcher holds the same set:
the pipeline places large files by S3 server-side copy and never writes them to the store.

The server reads S3 with its own read key (`artefacts_s3`, `GetObject` and `HeadObject` on the catalog
prefix), separate from the presigning key of deposits. Section 22.7 of the ingestion design found that the
object store probably cannot scope a key to a prefix (about 70% confidence); `ant-stack check` warns when the
read key can also write.

---

## 4. API

New tag `artefacts`, REST 2.47.0 (minor: new routes and fields, nothing removed). Read routes are in the
public schema; deposit, collection and purge routes stay hidden like the deposit routes. Errors are the
deposit API's JSON shape `{"status": "<word>", "message": "..."}`.

### 4.1 Deposit an artefact

`POST /api/datasets/deposit` with `deposit_kind: "artefact"`. Allowed for signed-in members of
`artefacts_upload_groups` (default `admins`, `analysts`) or `datasets_upload_groups`; admins always.

```json
{
  "deposit_kind": "artefact",
  "data_project": "Sun2026",
  "scope": "SLM",
  "artefact": {
    "kind": "figure",
    "artefact_id": "volcano-tvsc",
    "new_version_of": 1,
    "title": "Volcano plot, treated vs control",
    "description": "...",
    "change_note": "Relabelled the axes.",
    "access": "inherit",
    "made_from": [{"db_id": "S26/SLM", "dataset_version": "1.3.0"}],
    "declared": {"author": {...}, "created": "2026-10-12", "code": {...}, "parameters": {...}, "software": [...]}
  },
  "files": [
    {"name": "volcano.pdf", "size": 182331, "sha256": "...", "kind": "artefact_file", "role": "main"},
    {"name": "volcano.png", "size": 90211, "sha256": "...", "kind": "artefact_file", "role": "supporting"}
  ]
}
```

- `new_version_of` is absent for a new artefact and must be the latest live version the client saw for a new
  version (optimistic concurrency). It is checked at create and again at publication.
- For `kind: de_table` the files are `kind: de` entries with their `.mus`, exactly as in the DE-only deposit;
  the sidecar must name the scope dataset (`de_dataset_id`).
- The artefact block (at most 256 kB) is stored in the deposit record. The server writes it to incoming itself
  as `request.json`, with the deposit mark; the uploader never uploads it.
- Target keys are `__incoming/<data_project>/artefacts/<deposit_id>/<name>`; upload keys are in the
  deposit's staging folder as today. Single PUT up to 64 MB, multipart above (a 100 GB file is about 1,600
  parts of 64 MiB, inside the 9,500-part limit).

Answer (201): the 2.46.0 create answer plus `deposit_kind: "artefact"` and `artefact: {ref_without_version,
kind, new_version_of}`. Status, list, withdraw, approve, reject, retry, expire and report work unchanged on
artefact deposits; when live the status adds `artefact: {ref, version}`.

New errors: 409 `artefact_exists` (a new artefact with a taken id), 409 `version_conflict` (with `latest`),
403 `not_owner` (a new version of someone else's artefact without admin rights; an artefact the caller
cannot see answers 404 instead), 422 `invalid_kind`,
`invalid_artefact_id`, `invalid_scope`, `made_from_unknown` (also for a dataset the caller cannot see, with
the same message), `provenance_incomplete` (with the missing keys), 413 `too_large` (per file
`artefacts_max_file_gb`, 100).

### 4.2 Create a collection

`POST /api/datasets/deposit` with `deposit_kind: "collection"` (members of `collections_create_groups`,
default `admins`, `analysts`):

```json
{"deposit_kind": "collection", "data_project": "Fiers2026",
 "collection": {"title": "...", "authors": ["Mark Fiers"], "description": "...", "access": "internal",
                "doi": null, "papers": {}}}
```

The server checks the name (`data_project` rule, not existing, no case-insensitive clash, the clash not
named to non-admins), assigns a unique `group_id`, renders the minimal `index.md` of section 2.6, writes it to
`__incoming/Fiers2026/static/index.md` with the deposit mark and answers 201 `{deposit_id, state:
"uploaded", data_project, group_id, index_md}`. There are no files to upload. The watcher places it, commits
it and rescans; `access: public` is held for an admin. An artefact deposit into a collection that is not live
yet answers 409 `collection_pending`. Errors: 409 `collection_exists`, 422 `invalid_data_project`,
`invalid_collection` (missing title or authors).

### 4.3 Read

| Route | Answer |
| --- | --- |
| `GET /api/artefacts?group_id=&db_id=&kind=&q=&owner=&include_purged=false&limit=50&cursor=` | `{artefacts: [summary], next_cursor}`; summary `{ref, artefact_id, kind, group_id, scope, db_id, title, latest, versions_live, owner, updated, size_bytes, main_file, preview}` |
| `GET /api/artefacts/{group_id}/{scope}/{artefact_id}` | head record plus the latest live manifest (`manifest`), `versions: [{version, state, published, purged?}]` |
| `GET .../{artefact_id}/versions` | every version, purged ones as tombstones `{version, state: "purged", purged, purged_by, purge_reason}` (`purged_by` is "an admin" for non-admins when an admin purged) |
| `GET .../{artefact_id}/v{N}` | the manifest; 410 `purged` with the tombstone |
| `GET .../{artefact_id}/v{N}/files/{name}` | section 3.2 |
| `GET .../{artefact_id}/v{N}/preview/{name}` | inline preview |
| `GET /api/databases/{db_id}` and `GET /api/groups/{group_id}` | gain `artefacts_count` |

### 4.4 Purge and admin overrides

Purge has two stages (Mark, 2026-10-22; section 5.3 for the watcher):

- `POST /api/artefacts/{group_id}/{scope}/{artefact_id}/v{N}/purge` `{reason}` (soft): the uploader of that
  version, the artefact's owner or an admin. The latest version is never purged (409 `latest_version`): a
  version becomes purgeable once a newer live version exists. The watcher marks the version `purged` in the
  head record with who, when and why; nothing is deleted, the version stays listed and downloadable and
  clients show it greyed out with "purged by X on D: reason". A `de_table` version keeps its study in the
  dataset.
- `POST .../v{N}/unpurge` `{reason?}`: the same people; the version is live again (`unpurged`, `unpurged_by`
  in the head record).
- `POST .../v{N}/delete` `{reason, confirm: "<ref>@v<N>"}` (admins only, the hard purge): only a purged (or
  failed) version; the watcher deletes its files from S3 and the store, removes a `de_table`'s studies (with
  the re-import of an older live version) and leaves the tombstone (`deleted`).
- Answers: 202 `{ref, version, action: "soft" | "unpurge" | "hard", state: "requested", changed: true,
  request_id, requested, next}` when recorded or already waiting; 200 `{ref, version, action, state: "done",
  changed: false, version_state, message}` when the version is in that state already (idempotent). 403
  `forbidden` (not the uploader, owner or admin; any non-admin on delete), 404 `not_found`, 409
  `latest_version`, `not_purged` (unpurge or delete of a version that is not purged), `deleted`,
  `failed_version` (a failed version is deleted, never soft-purged), `busy` (another action on the version waits,
  a publication runs), `central_watcher`; 422 `invalid` (reason under 5 characters), `confirm_mismatch`.
- `GET /api/artefacts/purges?state=&action=`: the requests (own; admins all) `{purges: [{id, ref, version,
  action, state, reason, requested, updated, requested_by, detail}]}`. Requests made before the two-stage purge
  read `action: "hard"` (the column's default), and head records written before it (no `states: 2`) read their
  `purged` versions as `deleted`: the purges done so far stay tombstones.
- Withdrawing a whole artefact (proposal, not built): when only one live version is left the artefact cannot
  be purged at all. A separate admin route `POST .../{artefact_id}/withdraw {reason}` could mark the head
  record `withdrawn` (listed with `include_purged`, downloads kept, no new versions), reversible, and an admin
  hard purge of a withdrawn artefact's last version would then be allowed. Until Mark decides, a wrong last
  version is replaced by depositing a corrected version and purging the old one.
- Admins: `GET /api/artefacts?all=true` includes artefacts of hidden and inactive datasets; `POST
  .../{artefact_id}/owner {user, note}` transfers ownership (recorded in the head record and the event log);
  purge of anyone's version; approve, reject and retry of artefact and collection deposits through the
  existing deposit routes.

---

### 4.5 Withdrawing a whole artefact (2.47.1, decided by Mark)

- `POST /api/artefacts/{group_id}/{scope}/{artefact_id}/withdraw` `{reason}` (admins): the watcher writes
  `withdrawn: {date, by, reason}` into the head record (`artefact.md`, committed, pushed). The artefact stays
  listed, every version stays downloadable, and every payload carries `withdrawn: {date, reason}` (plus `by` for
  admins). No new version is accepted: create answers 409 `withdrawn`, and a deposit created before the
  withdrawal fails at publication. Its versions may now be purged and deleted, the latest one too: the soft purge
  drops the "latest version" rule, and an admin may delete a live version directly (no purge stage first).
- `POST .../unwithdraw` `{reason?}` (admins) undoes it (`unwithdrawn: {date, by, withdrawn}` stays in the head
  record). Both are idempotent (200 `state: done, changed: false`), recorded in `artefact_purges` with version 0
  and action `withdraw` / `unwithdraw`, refused on a central-watcher site (409 `central_watcher`).
- Web: an admin-only "withdraw this artefact" form and "undo the withdrawal" button, a withdrawn mark on the
  artefact page, purge and delete buttons on the latest version of a withdrawn artefact. MCP: `get_artefact` and
  `list_artefacts` say WITHDRAWN.

## 5. Watcher and pipeline

### 5.1 Item

An artefact item is one completed artefact or collection deposit, keyed by deposit id, found under
`__incoming/<data_project>/artefacts/<deposit_id>/` (or the collection's `static/index.md`). It is linked only
when every object is exactly a copy the deposit completed (key, ETag, `x-amz-meta-deposit`). Unlike h5ad items
there is no unlinked path: `ingest_watch.accept_unlinked` does not apply, and any object under `artefacts/`
without its deposit is `needs_decision`. Settling, retries and the states are those of section 21.3.

### 5.2 Steps

| Step | On failure | Notes |
| --- | --- | --- |
| link and settle | wait | two identical listings, `settle_seconds` |
| scope | `failed` | the collection is live; the dataset exists, is not inactive and the depositor can still see it; each `made_from` version exists in the dataset's changelog |
| version check | `failed` (`version_conflict`) | `new_version_of` is still the latest live version, under the scope's writer lock until placement ends |
| hash | retry, then `failed` | streaming GET pinned to the listed ETag, sha256 computed on the stream; the bytes are written to the work dir only for inline files and for kinds whose QC reads content (tables up to `artefacts_content_check_max_gb`, 2; notebooks; images). A 100 GB model is hashed, never stored on the watcher's disk |
| QC | errors `failed`, held reasons `held` | section 6; report redacted as for datasets |
| assign version | | `max(all versions, purged included) + 1` |
| place files | retry | server-side `CopyObject` (managed `UploadPartCopy` above 5 GB) from incoming to `.../vN/files/<name>` with `CopySourceIfMatch` on the incoming ETag and `x-amz-meta-sha256` set; HEAD of the copy must show the size, the sha256 metadata and, for single-part sources, the source ETag. A key that already exists under `vN/` stops the item (`needs_decision`): a version is never overwritten |
| previews and inline copies | warning | thumbnails from the work-dir bytes (Pillow, `MAX_IMAGE_PIXELS` kept), written locally and to S3 under `vN/preview/` |
| manifest | retry | `vN/manifest.json` written last on S3 (HEAD first: it must not exist), then locally; its existence is what makes the version real |
| head record | retry | `artefact.md` updated and pushed (section 2.5) |
| DE import (`de_table`) | `failed`, nothing changed | the 2.46.0 DE-only import, with the artefact files as sources (no copy into `de_sources/`): duckdb copy under the writer lock, all tables or none, dataset patch version, changelog naming the artefact reference, `.de.parquet` push. A new version of the same `de_table` artefact replaces its own study (the artefact owns its study ids); a study id owned by another artefact or by a legacy import is refused unless an admin deposited it |
| rescan, served check | warning, retried | `POST /admin/rescan?what=artefacts` reloads the artefact index only; `GET /api/artefacts/...` must show the new version |
| git commit | warning | only `artefact.md`, the new `manifest.json` and a collection's `index.md`, under the existing rules (route-written files with the sha256 written, staged changes left alone) |
| delete incoming | | `delete_consumed`, one key at a time, HEAD-compared; `request.json` last |

A resume marker (section 21.5) is written when placement starts and lists the steps done, so a stop between
the manifest and the commit finishes the publication on the next pass instead of publishing a second
version. Recovery reads the marker first: files already copied with the right sha256 are not copied again.

### 5.3 Purge

The watcher executes the requests recorded by the API (the `artefact_purges` table of the deposit store, column
`action`), one at a time under the scope's artefact lock, and logs each as `artefacts: <purge | unpurge |
permanent deletion> of <ref>@v<N> by <login>: <state> <detail>`.

- `soft`: checks again that the version is live and a newer live version exists (`latest_version` otherwise),
  marks it `purged` with `purged`, `purged_by`, `purge_reason` (and `purged_by_admin`), writes the head record
  (local, S3), rescans, commits "purge <ref>@v<N> (files kept)". Nothing else changes.
- `unpurge`: a `purged` version becomes `live` (the purge fields go, `unpurged` and `unpurged_by` stay), the
  same writes, commit "unpurge <ref>@v<N>".
- `hard` (an admin's deletion of a purged or failed version):

1. under the scope's writer lock, the head record marks the version `deleting`;
2. for each file and preview listed in the version's manifest, HEAD the key, compare the `x-amz-meta-sha256`
   with the manifest, delete that one key, log it in `deletions`. No listing of the prefix decides what is
   deleted, and no batch delete is used;
3. the manifest goes last, then the local copies;
4. the head record marks the version `deleted` with `deleted`, `deleted_by`, `delete_reason` (the soft purge
   fields stay), `latest` stays the highest live version, the head record is pushed and committed ("delete
   <ref>@v<N> permanently");
5. for a `de_table` version whose study is still the one in the dataset, the study is removed by the same
   all-or-nothing duckdb copy as the import, with a dataset patch version and a changelog line ("DE study
   <id> removed: artefact <ref>@v<N> deleted permanently") and a caveat, following the rule that every curation
   decision appears on the dataset page (open question 5). A soft-purged `de_table` version still counts as the
   writer of its rows.

Head record states (`states: 2`): `live`, `purged` (soft, servable), `deleting`, `deleted` (tombstone),
`reserved`, `failed`. `ant sync pull` keeps the files of purged versions and deletes the local copies of
deleted ones.

A key whose sha256 metadata differs from the manifest is kept and the purge stops at `needs_decision`. If the
bucket has object versioning, a delete leaves a noncurrent copy; the purge is then not a deletion of the bytes
until a lifecycle rule expires noncurrent versions (open question 9).

---

## 6. QC rules

### 6.1 Generic (every kind)

| Check | Result |
| --- | --- |
| names: each segment matches `SEGMENT_RE`, at most 120 characters, ASCII, at most one subfolder, unique case-insensitively | error |
| declared size and sha256 equal the recomputed ones | error |
| size per file at most `artefacts_max_file_gb` (100), per deposit `artefacts_max_deposit_gb` (200), at most `artefacts_max_files` (50) files | error at create, again at QC |
| extension on the kind's allowlist, and magic bytes agree with it (PNG, PDF, ZIP, gzip, parquet, JSON) | error |
| title 5 to 200 characters; description at least 50 characters (or a markdown file in the deposit) | error |
| provenance: `declared.author`, `declared.created`, at least one `software` entry; `code.repository` with `commit`, or `code: {none: "<reason>"}` | error for `de_table`, `figure`, `table`, `notebook`, `report`, `model`; warning for `other` |
| `made_from` versions exist; a version older than the dataset's current one | error / warning |
| secret scan of text files and notebooks (private keys, AWS and GitLab tokens, `password=` lines, `.env` contents) | held `possible_secret` |
| donor identifiers: table columns whose values overlap with the crosswalk's internal ids (read inside the watcher only, as `ant ingest-check` does) | held `donor_identifiers` |
| scope or artefact public | held `public_access` (as for datasets) |
| ZIP files: central directory read only, never extracted; entry names without `..` or absolute paths; uncompressed total at most 20 times the archive and at most 1 TB | error |

### 6.2 DE tables (`de_table`)

Everything `complete` and the import check today, plus checks that the import currently leaves to the reader:

| Check | Result |
| --- | --- |
| known `de_format`; `de_term` and `de_contrast` for one-contrast formats (`deseq2`, `edger`, `scanpy`, `seurat`, `glmmseq`, `glmmtmb`), `de_method` for `long`; `auto` counts as missing | error |
| `de_dataset_id` (or `de_dataset_h5ad`) names the scope dataset | error |
| required columns of the format (`lib/de_presets.py`) present; at least one row | error |
| p-values and adjusted p-values within [0, 1]; log fold changes finite; adjusted p not smaller than raw p in more than 1% of rows | error / warning |
| at least 50% of the gene identifiers are in the dataset's var | error below 50%, warning below 90% |
| duplicated genes within one contrast | warning |
| `made_from.dataset_version` differs from the version the table is imported into | warning in the report and a line in the changelog |
| study id rules (section 5.2) | error |

---

## 7. vark

The commands reuse the uploader of `vark datasets upload` (`internal/cli/datasets_upload.go`: presigned
PUT, multipart with parallel parts, resume through `POST .../urls`, `--wait`). vark is built by its own
agent from this section and the 2.47.0 changelog.

| Command | Does |
| --- | --- |
| `vark artefacts add (--dataset DB_ID \| --collection GROUP) --kind K [--id ID] [--title T] [--description FILE] [--made-from DB_ID@VERSION]... [--param k=v]... [--params FILE] [--software auto\|NAME=VER]... [--code auto\|URL@COMMIT\|none:REASON] [--new-version] [--wait] FILE...` | local sha256, provenance capture (`--code auto` reads the git remote, commit and dirty flag of the first file's repository; `--software auto` records the Python packages of the active environment or `sessionInfo()` for R), the local QC that needs no server, deposit, upload, complete; `--new-version` reads the latest version first and sends it as `new_version_of` |
| `vark artefacts add --kind de_table ...` | the `vark datasets de upload` path (format detection, sidecar writing), deposited as an artefact; `vark datasets de upload` becomes an alias |
| `vark artefacts list [--dataset DB_ID] [--collection GROUP] [--kind K] [--mine] [-o json]` | `GET /artefacts` |
| `vark artefacts get REF[@vN] [--file NAME] [-O DIR]` | follows the redirect, verifies the sha256 of every downloaded file against the manifest, writes `manifest.json` beside the files |
| `vark artefacts versions REF` | versions and tombstones |
| `vark artefacts purge REF@vN --reason TEXT [--yes]` | typed confirmation of the reference unless `--yes` |
| `vark artefacts status [DEPOSIT_ID]` | `vark datasets status` filtered to artefact deposits |
| `vark collections create GROUP --title T --author NAME... [--description FILE] [--access internal\|public] [--doi DOI]` | collection deposit, prints the `group_id` once live with `--wait` |
| `vark collections show GROUP` | `GET /groups/{group_id}` with the artefact count |

`vark server check` gains `artefact_uploader` and `collection_creator`. pyvark gets the read side first
(`list_artefacts`, `get_artefact`, `artefact_versions`, `download_artefact(ref, dest, verify=True)`), the
upload later; it is also built by its own agent.

---

## 8. MCP (read only)

| Tool | Returns |
| --- | --- |
| `list_artefacts(db_id=None, group_id=None, kind=None, query=None)` | summaries as in 4.3 |
| `get_artefact(ref, version=None)` | the manifest with provenance, marked declared and recorded, and the API path of each file (never a signed URL: a signed URL in a tool result would reach the AI provider's logs) |
| `artefact_versions(ref)` | versions and tombstones |
| `read_artefact_table(ref, file=None, rows=50)` | the first rows of a `table` or `de_table` file of at most 2 MB, from the local inline copy |
| `get_artefact_preview(ref, file=None)` | the WebP preview of a figure as image content, at most 1 MB |

Visibility follows section 3.1. There are no upload, purge or collection tools: files never go through MCP,
and destructive actions stay on REST with a typed confirmation.

---

## 9. Web UI

Built by the web agent from `web/CLAUDE.md`. On the dataset page an **Artefacts** tab (next to Facets) and on
the collection page an Artefacts section:

- one line visible per artefact (kind, title, latest version, date, owner, size, download button); the
  description, the provenance (declared and recorded, made-from dataset version with a link) and the version
  list sit in a block that is closed by default;
- figures as a thumbnail grid from the local previews, opening a larger preview; the download button goes to
  the API route, which redirects to S3;
- older versions under "show older versions"; purged versions greyed with who, when and why;
- for the owner and admins a purge button that asks for the reference to be typed;
- a "made from an older dataset version" badge when `made_from` is behind the dataset;
- analysis collections in the collections list with a badge and their artefacts as content; the collection
  page does not ask for datasets.

The Downloads tab keeps its rule (h5ad and static files only); legacy static files stay there until they are
migrated (section 13). `help.html` gets an Artefacts topic in the same round.

---

## 10. Skills

A new skill `anthive-artefact-deposit` rather than a section in `anthive-dataset-deposit`: the audience
(analysts), the questions (which scope, which kind, which dataset version, how to capture provenance) and the
failure modes differ from an h5ad deposit, and the dataset skill is already long. It covers choosing scope
and kind, creating an analysis collection when needed, capturing provenance with `vark artefacts add --code
auto --software auto` (and linking to `sc-reproducible-research` for the freeze and the environment lock),
the QC failures with their fixes, new versions, and purging. The DE part of `anthive-dataset-deposit` moves
to the new skill and is replaced by a pointer. `anthive-dataset-analysis` gains one step: look for existing
artefacts of a dataset before recomputing them.

---

## 11. ant-sync

Today `_is_static_path` makes every file under `<Group>/static/` syncable in both directions, whatever its
size, and `_is_syncable` skips any path with a `_` folder part. Both are wrong for artefacts. New rules for
`<Group>/static/artefacts/`:

| Files | pull | push |
| --- | --- | --- |
| `artefact.md`, `vN/manifest.json`, `vN/preview/*` | always | never (the watcher writes S3) |
| inline files (`figure`, or at most `artefacts_inline_max_mb`) | always | never |
| other artefact files | only with `ant sync pull --artefacts REF\|GLOB` (honours `--dry-run`, checks free space, verifies the sha256) | never |
| purged versions (head record says `purged`) | local copies are deleted on pull | never |

The rule matches `static/artefacts/` before the `_` skip, so `_group` folders are covered. In
`sync_policy.mode: priority` the always-pulled files are counted in the budget like `static/`; explicitly
pulled large files are evictable. Legacy static files keep today's behaviour until migrated.

---

## 12. Security review checklist

| Concern | Design answer | Test |
| --- | --- | --- |
| signed URL scope | one key, GET only, TTL 300 s, fixed `Content-Disposition: attachment` and content type, issued only after the access check | presign parameters asserted; a URL for key A cannot read key B |
| signed URL leakage | `Cache-Control: no-store`, `Referrer-Policy: no-referrer`; MCP returns API paths only; Caddy's access log records response headers, so `Location` of artefact redirects is dropped in the log filter like the 2.46.1 filters (verify on the rendered config) | log line of a redirect has no signature |
| revocation | not possible within the TTL; documented; TTL kept short | |
| path validation | names checked at create and QC; the final key is built by the server from validated parts; the download route looks names up in the manifest; `ensure_inside` on every local write; at most one subfolder | traversal, Unicode, case-clash, `_`- and `.`-prefixed names refused |
| cross-collection access | access resolved from the artefact, not from request parameters; `made_from` to an invisible dataset answers like an unknown one; a deposit's scope must be in its `data_project` | analyst A cannot list, read or version an artefact of an internal dataset when anonymous; group-scope access is the most restrictive dataset |
| internal datasets need sign-in | every artefact route calls the access check explicitly | route enumeration test |
| ownership | new versions by the owner or an admin (`update_policy: own`, as for datasets); purge by the version's uploader, the owner or an admin | |
| immutability | a version key is never overwritten (HEAD before copy, manifest last, existing key is `needs_decision`); head record entries carry the manifest sha256 | re-deposit of the same version is refused |
| provenance spoofing | declared and recorded fields separated; `uploaded_by` only from `Remote-User`; sha256 and `made_from.h5ad_sha256` computed by the server; the commit is not verified (open question 7) and is labelled declared | a deposit that sets `recorded.*` has it dropped |
| quotas | the deposit limits (`max_open_per_user`, `max_per_day`, `max_pending_gb_per_user`) apply; new `artefacts_max_stored_gb_per_user` (1,000) and `artefacts_max_versions` (100 per artefact) | 413 `quota_exceeded` |
| rate limits | presign as today; downloads `artefacts_download_per_minute` (60); list and get share the group-lookup limiter | 429 with `Retry-After` |
| content served to browsers | HTML, SVG and notebooks only as attachments from the bucket's origin; previews generated server-side; Pillow pixel limit; ZIPs never extracted | |
| secrets and donor ids in uploads | held reasons, admin decides | |
| deletes | `delete_consumed` and the purge delete only listed keys, HEAD-compared, one at a time, logged; no prefix or batch deletes | a purge with a changed key keeps it |
| replay of URLs | uploads only to staging; complete copies with `CopySourceIfMatch` and the MD5 check; unlinked artefact objects are refused | the 2.46.0 replay tests run for artefact deposits |
| credentials | presign key and read key separate; `ant-stack check` warns about bucket-wide keys | |

---

## 13. Migration

On the dev store (2026-10-16, `/data/1/project/h5adstore`):

- **DE sources.** `Sun2026/de_sources/` (two tables, 3.1 MB) and `Sierksma2020/de_sources/` (two tables, 3.9
  MB). A one-off `ant artefacts migrate-de` makes each table a `de_table` artefact `v1` of its dataset, with
  `declared` filled from the `.mus` (`de_software_version`, `de_description`) and `code: {none: "imported
  before artefacts existed"}`, `recorded.uploaded_by: migration`, `made_from` the dataset version current at
  the import date in the changelog, and the study id the duckdb already has. It does not re-import. It copies
  to S3, writes the manifests and head records, commits, and removes `de_sources/` in a separate commit after
  the served check. Other sites and S3 must be checked for more `de_sources/` first.
- **Static files.** 20 files: 11 `paper_url.txt` (two `.bak` copies among them), 3 `.xlsx`, 2 `.csv`, a `.zip`
  (Olah2020, 22 MB) and a stray `separately_cleaned_harmonised_190626.h5ad.md` (Davis2025). The supplements
  become `_group` artefacts of kind `table` or `other` with `code: {none: "supplement of <doi>"}`. The `.bak`
  files are deleted. `paper_url.txt` belongs in `index.md` (`doi`, `papers:`), not in artefacts (open question
  11). The Davis2025 `.md` needs a person to look at it. `/download-static/` and `list_artifacts` keep working
  for legacy files during the transition.
- **DE-only deposits.** `deposit_kind: "de_only"` stays accepted for one minor release and is handled as an
  artefact deposit of kind `de_table` with `artefact_id` from the study id.

---

## 14. Config and deployment

New keys in `anthive.default.yaml` (each with a `get_*` helper in `lib/config.py`, env > yaml > default):
`artefacts_enabled` (false), `artefacts_upload_groups` ([admins, analysts]), `collections_create_groups`
([admins, analysts]), `artefacts_max_file_gb` (100), `artefacts_max_deposit_gb` (200), `artefacts_max_files`
(50), `artefacts_url_ttl_seconds` (300), `artefacts_inline_max_mb` (2), `artefacts_content_check_max_gb` (2),
`artefacts_download_per_minute` (60), `artefacts_max_stored_gb_per_user` (1000), `artefacts_max_versions`
(100), `artefacts_hold_public` (true), `artefacts_serve_local` (`fallback` | `prefer`), `artefacts_s3` (the
read key pair). `datasets_upload_groups` stays the strict-type key it is; `artefacts_upload_groups` and
`collections_create_groups` join the keys whose wrong type stops the start.

Container sites: `site.yaml` `artefacts:` block (`enabled`, `upload_groups`, `max_file_gb`), secrets
`s3_artefacts_read_access_key_id` and `_secret_access_key`, the `analysts` group in the Authelia users file,
`ant-stack check` for the read key and the log filter. `doc/DEPLOY_CONTAINERS.md`, `site.example.yaml` and a
scratch-site run are part of the phase that adds them.

---

## 15. Phases and effort

| Phase | Content | Effort (working days) |
| --- | --- | --- |
| A1 | `lib/artefacts.py` (manifest schema, validation, layout, head record), artefact index at the catalog scan, collection registry, read routes, access, redirect and local fallback, tests | 5 |
| A2 | `deposit_kind` `artefact` and `collection`, watcher item type, generic QC, placement, versions, purge, resume markers | 7 |
| A3 | `de_table` as an artefact, DE QC, `de_only` alias, migration of `de_sources` | 3 |
| A4 | ant-sync rules, config, ant-stack, deploy docs, scratch-site test | 3 |
| A5 | vark and pyvark (their agents) | 4 |
| A6 | web UI and help (web agent) | 3 |
| A7 | MCP tools and the skill | 2 |
| A8 | independent security review and fixes (two rounds, as for 2.46.0) | 3 |

About 30 working days in total; the estimate is uncertain by about a third, mostly in A2 (the watcher
integration took several review rounds for datasets). A1 and A4 alone give read access to migrated DE
sources and supplements; A2 and A5 together are the first point where analysts can upload.

---

## 16. Open questions for Mark

1. Dataset scope folder: the file id (`SLM`, proposed, stable) or the basename (readable, what `de_sources/`
   uses)?
2. Version numbers: plain integers `v1`, `v2` (proposed) or semver like datasets?
3. May any analyst add artefacts to a dataset or collection that is not theirs (proposed: yes, if they can see
   it; new versions of an artefact only by its owner or an admin)?
4. Hold artefacts of public datasets and public analysis collections for an admin (proposed: yes,
   `artefacts_hold_public`)?
5. Purging a `de_table` version: remove its study from the dataset as a dataset patch version with a caveat
   (proposed), or keep the imported study?
6. Collection-level artefacts of a collection with mixed access: most restrictive dataset (proposed) or an
   explicit `access` in `index.md`?
7. Should the declared commit be verified against the repository (only possible for reachable forges), or
   stay labelled as declared?
8. Storage quotas: 1,000 GB per user and 100 versions per artefact as defaults, and a quota per collection?
9. Does the lab bucket have object versioning? If so a purge leaves noncurrent copies until a lifecycle rule
   removes them; which retention do you want for those?
10. May analysts request public analysis collections at all (held for an admin), or internal only?
11. `paper_url.txt` files: move them into `index.md` (`doi`, `papers:`) and delete them, or keep them as legacy
    static files?
12. `group_id` of analysis collections: assigned by the server from the name (proposed, first letter plus the
    two year digits, then letters until unique), or chosen by the analyst?
13. How long should `deposit_kind: "de_only"` stay accepted (proposed: one minor release)?
14. Previews for SVG and PDF need cairosvg and poppler in the images; worth the image size, or raster previews
    only?
15. Is `analysts` a separate group from `curators`, and should curators automatically be analysts?

---

## 17. Phase A1 as built (2026-10-18)

`lib/artefacts.py`, the read routes in `bin/api/ant-serve`, the config keys of section 14 in
`lib/config.py` (`get_artefact_settings`, `may_upload_artefacts`, `may_create_collections`), the Caddy log
filter, `md_check` and `ingest-check` for analysis collections. Tests: `tests/test_artefacts_lib.py`,
`tests/test_artefacts_api.py`. Choices made where this document was silent or had to be read one way:

- **Index trust.** The index serves a version only when its `manifest.json` has the sha256 the head record
  lists and every identifying field (ref, group, group_id, scope, kind, artefact_id, version, db_id) agrees
  with the folder it was read from. A head record that disagrees with its folder, or whose `latest` is not
  the highest live version, leaves the artefact out. An artefact id found under two kinds of one scope is
  left out entirely. Head records and manifests above 1 MB are not read. Files reached through a symlink
  that leaves `static/artefacts/` are refused; a whole group folder symlinked from another store works.
- **Artefact-level access.** An artefact is internal when any of its versions says `access: internal` (the
  most restrictive version wins, so narrowing one version cannot be undone by a later one). "Never
  anonymous" is read as "needs a caller who sees internal data", the dataset rule: on a site with
  `internal_requires_auth: false` internal artefacts are as open as internal datasets.
- **Collection access.** A dataset collection's `_group` is public only when it has served datasets, all of
  them public, and its `index.md` does not set `access` to anything but `public` (an optional narrowing key;
  no collection has it today). Without served datasets it counts as internal. An analysis collection is
  public only when its `index.md` says `access: public` and any dataset placed in it is public too.
  Orphans (dataset inactive or gone, collection unknown) are visible to admins only: on the get routes, and
  in the listing with `all=true`.
- **Collection registry.** Built from every top-level folder with an `index.md` or with served datasets
  (`_` and `.` folders never). A dataset collection whose `index.md` has no `group_id` takes the group id of
  its datasets when they agree. Two folders with one group id are both left out of the registry (their
  datasets are served as before, but they get no collection artefacts). An analysis collection with an
  incomplete `index.md` (name rule, group = folder, 3-character group_id, title, authors, access value) is
  left out. `/groups/{id}` answers from the registry only for an analysis collection the caller may see; a
  dataset collection without visible datasets (hidden from the caller, or all inactive) stays a 404.
- **Downloads.** Before signing, a HEAD of the object must show the manifest's size and, when the object
  carries `x-amz-meta-sha256`, the manifest's sha256 (502 `integrity_error` otherwise; the URL is never
  signed). Objects without the metadata are accepted on size alone (A2 sets it on every copy; the A3
  migration must too). A transient S3 failure without a local copy is 503 `storage_unavailable`, not 404.
  `latest` works in file and preview paths too. Local copies are hashed once per inode, size and mtime
  (in-memory, bounded); a large explicitly pulled file is hashed on its first download request.
- **Rate limits.** Downloads per caller: the signed-in user, else the last `X-Forwarded-For` entry (the
  address Caddy saw), else the peer. List and get are not rate limited in A1 (the deposit lookup limiter the
  design names exists only when deposits are configured).
- **Tombstones.** The head record entry of a purged version may carry `purged_by_admin: true`; with it,
  non-admins see `purged_by: "an admin"`. A2 writes the flag.
- **artefacts_enabled** gates the deposits (A2). The read routes always serve what the store holds: a site
  without artefacts answers empty lists.
- **Catalog prefix.** Artefact keys are `<catalog prefix><Group>/static/artefacts/...` with the catalog
  prefix `ingest_watch.catalog_prefix`, else `s3.prefix` (`get_artefact_settings()['catalog_prefix']`). The
  watcher of A2 must place files with the same helper; today the dataset pipeline uses
  `ingest_watch.catalog_prefix` alone, which differs only on a site whose `s3.prefix` is not empty.
- **Caddy.** Verified with Caddy 2.10.2: the access log wrote the full `Location` of a 307 (signature and
  key id included) under `resp_headers`. The filter `resp_headers>Location regexp \?.*$ ?REDACTED` keeps
  host and path; the h5ad download and paper PDF redirects were leaking the same way and are covered too.
  The default logger does not log response headers.
- **Not in A1:** `index_md.required_spec` of the deposit vocabulary still lists `default_dataset` (dataset
  deposits always target dataset collections; A2 adds the collection deposit and its own spec). The
  `site.yaml` `artefacts:` block, the read-key secrets and `ant-stack check` stay in A4 as planned: every new
  key is optional and a site that sets none behaves as before. help.html and the web UI are A6.


---

## 18. Phase A2 as built (2026-10-18)

`lib/artefact_deposit.py` (server: `ArtefactDepositService`, a subclass of the 2.46 `DepositService` that
leaves dataset deposits as they were; the shared rules: kind allowlists, magic bytes, secret scan, provenance,
group id assignment, `index.md` rendering), `lib/artefact_pipeline.py` (watcher: `ArtefactPublisher`, called
by `ingest_watch.Watcher.poll` after the DE deposits), the deposit route dispatch and the purge route in
`bin/api/ant-serve`, the `deposits` columns `artefact_ref`, `artefact`, `group_id` and the table
`artefact_purges` in `deposits.sqlite`. Tests: `tests/test_artefact_deposit.py`, `test_artefact_pipeline.py`,
`test_artefact_deposit_api.py`, and `test_artefact_e2e_s3.py` (opt-in, `ANTHIVE_E2E_S3=1`, real bucket, test
prefixes only, every object it created deleted). Where this document was silent or was changed:

- **Items come from the deposit store**, as the DE-only deposits do: the watcher reads the completed artefact
  and collection deposits from `deposits.sqlite` and links them to the listing (exact key, ETag, size and
  `x-amz-meta-deposit` of every file). No `request.json` is written to incoming: the artefact block lives in
  the deposit record, which the watcher reads anyway, so there is one object less to verify and delete. An
  object under `<prefix><data_project>/artefacts/` that no completed deposit accounts for is recorded as
  `needs_decision` (`artefact-unlinked:<key>`) and never processed or deleted.
- **The collection's `index.md` goes to `<prefix><data_project>/artefacts/<deposit_id>/index.md`**, not to
  `<prefix><data_project>/static/index.md`: the dataset route reads `static/index.md` as a group seed, and a
  dataset deposit of the same name would have consumed it. `ingest_pipeline.plan_items` skips every key below
  `<prefix><data_project>/artefacts/`, so the name `artefacts` is reserved there (a `mus s3 upload` with a
  subfolder of that name is no longer a dataset item).
- **`de_table` is refused** with 422 `invalid_kind` until A3; DE tables keep the 2.46 DE route.
- **Rights.** The artefact and collection groups count only where `artefacts_enabled` is on; elsewhere the
  2.46 rules apply unchanged (a site that limited `datasets_upload_groups` keeps its limits). Where it is on,
  admins may always deposit artefacts and create collections, as section 4.1 says. The generic deposit routes
  (status, urls, complete, withdraw, list, vocab) accept any of the three kinds of depositor; the service only
  shows a caller their own deposits (another user's deposit is a 404, as in 2.46). A dataset collection's
  artefact needs a visible collection; a dataset artefact needs a visible dataset (a public dataset of a mixed
  collection is a valid scope).
- **One deposit per artefact.** `reserve` gained an `extra_check` that runs inside its `BEGIN IMMEDIATE`
  transaction: no other deposit of the same artefact (or collection name or group id) may be open or waiting for
  the watcher (409 `busy`, 409 `collection_exists`). The version check runs again at publication under the
  scope's writer lock (the dataset's `<stem>.duckdb.lock`, or `static/artefacts/_group.lock`).
- **Publication without a served gate.** The placement is verified on S3 (HEAD of every copy: size, sha256
  metadata, and the source ETag for single-part objects, since the lab bucket ignores `CopySourceIfMatch`),
  then the artefact index is rescanned and the version route fetched. A failed rescan or served check is a
  warning (the next rescan serves the version); the incoming objects are deleted once the placement is
  verified, because the S3 copies are the truth. The dataset route waits for its served check instead.
- **Resume.** A marker in `<ingest_state_dir>/artefact_resume/<deposit_id>.json` is written before the first
  copy and holds the version, the hashes, the made_from versions, the QC report and (once decided) the exact
  manifest text, so a restarted watcher writes the same manifest. Files already at their key with the right
  sha256 are not copied again; a key with other bytes stops the item. Tested by killing the watcher after the
  marker, after the first copy, after the manifest and after the head record.
- **Previews.** Table previews (first 20 lines as `.txt`) are written; raster previews need Pillow, which the
  watcher's dependencies and the images do not carry yet (A4 adds it with the image rebuild). Inline copies of
  figures and files up to `artefacts_inline_max_mb` are written locally.
- **Donor identifiers.** Every alias and canonical uid of `donor_crosswalk.tsv` of at least 4 characters,
  matched exactly (case-insensitive) against the cells of `.tsv` / `.csv` tables (gzip too) up to
  `artefacts_content_check_max_gb`; a hit holds the deposit (`donor_identifiers`). The secret scan names what it
  found (private key, AWS key id, GitLab or GitHub token, password assignment), never the value.
- **Approvals** of held reasons are recorded in the manifest (`recorded.approval: {by, reasons, date}`).
- **Quotas.** `artefacts_max_stored_gb_per_user` counts the live versions the user uploaded (from the index)
  plus the new deposit, at create; `artefacts_max_versions` is checked at create and at publication.
- **Purge** runs in the watcher (`artefact_purges`), under the scope's writer lock: the head record entry
  becomes `purging`, every file of the manifest is deleted after a HEAD that shows the manifest's sha256 (a
  preview needs the sha256 the pipeline wrote), the manifest last, then the local version folder, then the
  tombstone (`purged`, who, when, why, `purged_by_admin`), the rescan and a git commit of the head record and
  the removed manifest. A key whose sha256 differs is kept and the purge stops at `needs_decision` with the
  version `purging`. A killed purge is finished by the next poll.
- **Not built in A2:** the owner transfer of section 4.4 (`POST .../owner`); the DE import of `de_table`
  artefacts and their purge (A3); MCP, vark and pyvark (A5, A7).
- **Before artefacts are switched on at a site** (A4): `ant-sync` still mirrors `<Group>/static/` in both
  directions whatever the size, so another site's `ant sync pull` would download every artefact file the
  watcher placed locally (inline files only, so at most a few MB each, but every version), and it skips
  `_group` folders. The h5adstore repository needs `static/artefacts/**/files/` and
  `static/artefacts/**/preview/` in its `.gitignore` (the watcher commits only head records, manifests and
  `index.md`, by path, but a hand `git add -A` would pick the files up). The `site.yaml` `artefacts:` block,
  the read-key secrets and `ant-stack check` are A4 as planned.

---

## 19. Phase A3 as built (2026-10-19)

`lib/artefact_de.py` (the section 6.2 checks), `ingest_pipeline.import_de_all_or_nothing` (the DE-only
route's import, extracted unchanged and shared; it can also remove studies and append a caveat), the
`de_table` steps of `lib/artefact_pipeline.py`, the server rules and the `de_only` alias in
`lib/artefact_deposit.py`, `lib/artefact_migrate.py` with `bin/ingest/ant-artefacts-migrate-de`. Tests:
`tests/test_artefact_de.py` (with the real `ant-de-ingest`), the de_table part of `test_artefact_e2e_s3.py`.
Choices:

- **Order of the steps.** The import runs after the file copies and before the manifest, under the dataset's
  writer lock: a version whose import fails never becomes real. Its copies are deleted (only keys of that
  version carrying the sha256 this deposit placed), its head entry is marked `failed` and its marker dropped;
  the next deposit gets the next number (section 20). A kill after the import is recognised by the changelog line naming `artefact <ref>@v<N>
  (deposit <id>)`; the study is never imported twice.
- **Who may deposit a de_table.** A de_table changes its dataset (a patch version and DE studies), so it
  needs the right to update the dataset, the 2.46 rule of `_existing_dataset`: an admin,
  `datasets_deposit_update_policy: any`, or the dataset's owner (assigned by an admin, else the depositor of
  its newest live version). Checked at create (403) and again at publication. This is the security default
  (Mark may revise it); a member who may not update the dataset can still deposit the table as kind `table`.
- **Study ownership.** A study id belongs to the artefact whose live versions list it in `de.imported`. A
  new version of that artefact replaces it (archived, as an admin replacement in 2.46); any other clash needs
  an admin, as in 2.46. Which version wrote the rows a study serves now is decided by fingerprints
  (section 21), not by study id.
- **Sign convention.** The table cannot prove the direction of a comparison. The check reads the contrast:
  `A_vs_B` (or `A vs B`) is recorded as "positive log2 fold change = higher in A than in B"; any other contrast
  is a warning. Nothing compares the sign with the data.
- **Versions known for `made_from`.** A dataset published before it had a changelog loses its first version
  from the `.md` at the first patch bump; `changelog_versions` now also knows the version a first patch entry
  implies (`1.0.0` before `1.0.1`), and `h5ad_sha256_at` treats it as preceding every entry.
- **Sidecars.** API deposits keep the `.mus` as uploaded (a warning names keys such as `abspath`,
  `eln_*`, `s3_*`, which are served with the table); the migration copies only the DE keys.
- **`de_only` alias.** Only where artefacts are on, for one minor release; the rights of the 2.46 route
  (the dataset update rule above) apply to it. The artefact id comes from the first table's name, not from the
  sidecar's `de_study_id`, because the sidecar is not uploaded yet when the deposit is created.
- **Migration dry run on the dev store (2026-10-19, `/data/1/project/h5adstore`, read-only: no file in the
  store changed; superseded by the rerun in section 21).** Four tables found. `Sierksma2020/de_sources/de/DE_ARM_vs_HM1.tsv.gz` and
  `DE_HM1_vs_HM2.tsv.gz` would become `S20/SIE/de-arm-vs-hm1` and `S20/SIE/de-hm1-vs-hm2`, but the served duckdb
  holds no DE study at all (no `__de_study`, no `.de.parquet`): the imports were lost or never made, so these
  would be artefacts without an import record. The two `Sun2026/de_sources/*/res_TvsC_sh*.tsv` tables are
  skipped: their sidecars name `de_dataset_id=AQB`, and no Sun2026 dataset has that id (DUN, AQR, AQM). Both
  need a decision before `--apply`: deposit the Sierksma tables again as `de_table` artefacts (which imports
  them), and correct the Sun2026 sidecars to the dataset they belong to.

## 20. Security review of A1 + A2 (2026-10-19)

An independent review probed the A1 + A2 code (probes kept as `tests/test_artefact_review.py`). What changed:

- **made_from (S1).** The read side redacts `made_from` per viewer (`artefacts.redact_manifest`), so an
  internal dataset id never reaches a caller who cannot see the dataset. At publication a public artefact with
  an internal source is held (`made_from_internal`); an admin approves it or the depositor redeposits it as
  internal. The manifest on S3 and on disk keeps the full list; only the API redacts.
- **ant-sync (S2).** `*/static/artefacts/**` is never pushed or pulled: an `ant sync push` would re-upload
  version files without their `sha256` / `deposit` metadata, and a later purge would then refuse them (or,
  worse, a pull would bring back purged files).
- **Failed placements (S3).** The version number is reserved in the head record (`state: reserved`) right
  after the resume marker and before any copy. A stop during placement marks it `failed` and deletes the
  copies that carry this deposit's mark. Numbers are never reused, so a half-placed version can never be
  mistaken for a later deposit's. `_place` reuses an existing key only when its `sha256`, `deposit` and size
  all match. Admins clean up a failed version by purging it (`cleanup_failed_version`, listed keys with the
  deposit mark only). An index entry with only failed or reserved versions counts as not existing, so the
  depositor can try again without `new_version_of`; the owner is set at the first published version.
- **Memory (S4).** The QC never reads a whole file: the secret scan streams 1 MiB chunks with a 16 KiB
  overlap that starts at a line boundary (the patterns anchor on `^`), sha256 and MD5 come from the one pull,
  local copies stream through a temp file, the donor scan stops at 2,000,000 rows (a warning), and figures are
  kept in the work dir only up to the content check limit. DE tables stream through `artefact_de.open_table`.
- **Sticky internal (C5).** Each head entry records the access it was published with; `access_internal`
  is true when any entry, purged ones included, was internal.
- **Multipart copies (C6).** Some stores ignore `CopySourceIfMatch` on a managed (multipart) copy, and the
  copy gets its own ETag. After such a copy the source must still have the checked ETag, else the item stops.
- **Unscanned files and more types (C7).** A text or table file too large to scan is held (`not_scanned`).
  The donor scan reads parquet (DuckDB, streamed), xlsx (shared strings and sheets, iterparse inside the zip,
  nothing extracted), json, txt, yaml and md (token scan). A zip that is not kept locally has its central
  directory read with ranged GETs pinned to the ETag (`RangedObject`), never downloaded whole.
- **Active content (C8).** html, svg, js, xml and notebooks are stored as `application/octet-stream`; every
  object gets `Content-Disposition: attachment`. The presigned GET adds nothing the object does not carry.
- **Locks (C9).** Placement and purge take `<stem>.artefacts.lock` (or `static/artefacts/_group.lock`); the
  duckdb's writer lock is taken only around a DE import or study removal, so annotation writers are not
  blocked by a long copy.
- **Collections against new groups (C10).** A dataset deposit whose group is a pending collection waits
  (409); a collection whose name a pending dataset deposit is creating is refused; a dataset deposit into an
  analysis collection needs its creator or an admin.
- **Resume (C11).** `resume_interrupted` runs at startup also where only artefacts are on.
- **Logins (nits).** Callers without a sign-in see "a member" / "an admin" instead of logins and no purge
  reason. The quota counts pending deposits.

Left for A6: the head record's description and the change notes are markdown written by depositors. They are
served as text today; when the web UI renders them, it must sanitise (no raw HTML, no `javascript:` links, no
images from other hosts).

## 21. Second review of the artefact store (2026-10-19)

A second review checked the section 20 fixes and A3 (probes kept as `tests/test_artefact_review2.py`).

- **De_table rights (N1).** See section 19: the dataset update rule at create and at publication, for
  de_table artefacts and the `de_only` alias. The alias now also carries a server-set `alias_of` in the stored
  block, so the watcher applies the relaxed provenance it was accepted with (before, an alias deposit could
  never be published: the watcher held de_table provenance to the strict rule).
- **Who wrote a study (N2).** Every de_table version records, per imported study, a fingerprint of the rows
  it imported (`de.imported.fingerprints`: row count and the summed `hash()` of the `__de` columns). A purge
  compares each study with what the dataset serves now:
  - rows another de_table version in the scope recorded stay (another artefact replaced the study);
  - rows of the purged version are replaced by the newest other live version of the same artefact that
    lists the study (its table is read from the local copy or the catalog, imported with `--replace`, the
    purged rows archived), or removed when no such version exists;
  - rows no version recorded (a hand edit or import since, or a version without fingerprints) stop the purge
    for an admin; nothing is deleted.

  All of it is one all-or-nothing import with a patch version and a caveat. The fingerprint is computed by
  the DuckDB that compares it; an upgrade that changes `hash` makes the comparison stop for an admin, never
  remove the wrong rows.
- **Rollback (N3).** The study plan (`added`, `replaced`) is saved in the resume marker before the import. A
  version abandoned after its import is rolled back: added studies removed, replaced ones restored from their
  `_versions/<basename>/de/<study>/` archives (`import_de_all_or_nothing(restore=...)`), a patch version and
  the caveat `de_import_rolled_back`. The failure text says whether it worked; a rollback that fails says that
  an admin must remove the study.
- **Give-up and bugs (S3 residue).** A deposit that fails after 8 transient errors or through an
  unexpected exception abandons its reserved version like a stop does (rollback, copies removed, entry
  `failed`), so nothing is left `reserved`.
- **DE duplicate check (S4 residue).** 64-bit hashes of `(term, contrast, gene)`, at most 2,000,000; a larger
  table is held (`de_not_checked`).
- **Checks against a moving dataset.** The version and sha256 of the scope and `made_from` datasets are read
  before the checks and again under the scope lock; a change means the checks run again at the next attempt.
- **Purge of a de_table on an inactive dataset.** The files go, the study removal is recorded as
  `de_removal_deferred` on the tombstone; an admin removes the study after the dataset is active again.
- **Migration.** `apply` checks the head record locally and on S3 first; files are written only when absent
  (an existing file with the same sha256 is a resumed run); a manifest is never overwritten; only a 404 counts
  as absent. The script uses the `s3` block's write key. Study ids match changelog lines as whole ids. Tables
  of retired datasets (a `_`-prefixed folder such as `_deleted_2026-10-05/`) are skipped. Studies already in
  the duckdb get their fingerprint.
- **Dev store dry run again (2026-10-19, read-only, no store file changed).**
  `Sierksma2020/de_sources/de/DE_ARM_vs_HM1.tsv.gz` (79,582 rows) and `DE_HM1_vs_HM2.tsv.gz` (16,148 rows) now
  link to the studies Mark imported by hand into S20/SIE 0.3.0 (made_from 0.3.0, from the changelog entry
  naming each study). Both `Sun2026` tables are skipped: their targets AQN and AQB are retired datasets
  (`Sun2026/_deleted_2026-10-05/`).

## 22. Phase A4 as built (2026-10-19)

`lib/artefact_sync.py` with `ant-sync pull`, the `artefacts:` block and `_check_artefacts` in `ant-stack`,
`doc/DEPLOY_CONTAINERS.md` ("Let analysts deposit artefacts"), `site.example.yaml`. Tests:
`tests/test_artefact_sync.py`, `tests/test_ant_stack_artefacts.py`. Choices:

- **Head records and manifests are not pulled by ant-sync** (section 11 said "always"). The watcher commits
  them to the h5adstore repository, so they arrive with `git pull`; writing them from S3 into the checkout
  would make the next `git pull` refuse to overwrite untracked files. ant-sync reads them locally to decide
  what to fetch, and notes a live version whose manifest is not in the checkout yet.
- **Inline files and previews** are pulled for every live version, verified against the manifest's sha256
  (previews against the object's `sha256` metadata; a preview without it is not pulled). They are not counted
  in the `sync_policy.mode: priority` budget: they are at most `artefacts_inline_max_mb` each.
- **`--artefacts PATTERN`** pulls the other files of the artefacts it names; a FILTER glob without it skips
  the artefact part of the pull, `--no-artefacts` always does.
- **An artefact-only site** (`artefacts.enabled`, `services.dataset_deposit` off) gets the same watcher,
  tools listener and presigning pair as a deposit site: one watcher publishes both.
- **Scratch-site check (2026-10-19).** Built the app image from the branch (`ant-stack build --app-only`; the
  ingest image was the app image retagged, since nothing here needs R), then followed DEPLOY_CONTAINERS.md on a
  scratch site with a fresh `state_dir`: `init` twice, key files, `check` (OK), `up`; basic login, `tls.mode:
  off`, ports 18580 / 18579, S3 on the lab bucket under the test prefixes only (`__ingest_test_catalog/`,
  `__incoming_test/`, `__deposits_test/`). Through the running stack: an artefact deposit into an internal
  analysis collection (create, presigned PUT, complete), `ant-stack watch -- --once` published it (live v1, the
  inline copy in the store), the listing and detail answered, the download was a 307 to a signed link with
  `Cache-Control: no-store, private` and `Referrer-Policy: no-referrer`, the signed GET returned the same bytes
  as `attachment`, and the Caddy access log held the `Location` with `?REDACTED` instead of the signature.
  Changing `artefacts.max_file_gb` and running `up` changed the vocabulary at once. `ant-stack ingest -- ant
  sync pull --no-library` (dry run, then for real) brought back a deleted inline copy. A purge through the API
  was carried out by the next `watch --once`. Found and fixed: `check` did not warn that an artefact-only site
  presigns with the read-write pair, nor remind of the lifecycle rule. Afterwards `down`, the volumes, images
  and the scratch folder were removed, and the one S3 object left (the tombstone head record) was deleted; no
  object remains under the test prefixes. The published image (anonymous pull) could not be used: no image of
  this branch is published.

## 23. Security review round 3 (2026-10-19)

- **Stored XSS through an analysis collection (serious).** A collection deposit wrote the depositor's description
  into `index.md`, and once the collection listed a dataset the folder page rendered it with python-markdown and
  `|safe`. Fixed at both ends: `render_analysis_index_md` writes `<` as `&lt;`, and the web UI renders every
  markdown body (dataset `.md`, collection `index.md`, gene set and experiment bodies, paper notes) through
  `safe_markdown` (`_store_markdown`). 2.46 dataset deposits write seed `.md` and `index.md` files too, so no body
  is trusted; the store uses no raw HTML today.
- **safe_markdown** parses with markdown-it-py (CommonMark, tables, strikethrough, raw HTML off) and keeps the
  allowlist re-serialisation; hrefs with a backslash or a control character are refused (`/\host` is another
  site to a browser); relative links are kept; plot thumbnails survive for store bodies. The 31 review payloads
  are tests.
- **ant-sync and symbolic links.** `artefact_sync.checked_path` refuses `<group>/static`, `<group>/static/artefacts`
  and any component below them when it is a symbolic link, and any path resolving outside that tree, for planning,
  deletes and downloads. The group folder itself may be a link (a mirror site's store links whole groups); its
  `static/` tree is then the target's own and is checked from there.
- **Manifests without previews.** Manifests written before previews were recorded have no `previews` list;
  `ant sync pull` notes that per version and pulls no preview for it (no backfill: the watcher only writes new
  versions, and a preview is a convenience). The migration writes `previews: []`.
- **MCP.** Depositor text is a labelled JSON string on one line, after the server facts.
- **CSRF.** `_CrossSiteGuard` in ant-serve (see the changelog); ant-browse has no state-changing routes. The
  FastAPI floor of ant-serve and ant-browse is 0.141.1 (the pinned version). Round 4: `Origin` is compared with
  `Host` (port included) or the configured public URL; `X-Forwarded-Host` is not believed (a client can send it,
  and Caddy passes `Host` on unchanged). nginx in front must forward `Host $http_host` (the dev shim and the
  vhost examples do); a refusal logs `Origin`, `Host`, `X-Forwarded-Host` and the public URL at warning. ant-mcp
  posts paper uploads as multipart also without a PDF (a urlencoded form is a 415).
- **Content types, the system owner, previews.** Active types are `application/octet-stream` in manifests and
  downloads; `migration` is reserved (ant-adduser, ant-stack, ant-serve, ownership checks); previews are recorded
  in the manifest and checked against it by `ant sync pull`.
