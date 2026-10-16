# Design: the artefact store

Status: design only, 2026-10-16, not built. Section 23 of `doc/DESIGN_dataset_ingestion.md` points here.
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

- `POST /api/artefacts/{group_id}/{scope}/{artefact_id}/v{N}/purge` with `{reason, confirm:
  "<ref>@v<N>"}`: the uploader of that version, the artefact's owner or an admin. Answers 202 `{ref, state:
  "purge_requested"}`; the watcher carries it out (section 5.3) and the tombstone appears. 409 `busy` while a
  deposit of a newer version of the same artefact is being processed.
- Admins: `GET /api/artefacts?all=true` includes artefacts of hidden and inactive datasets; `POST
  .../{artefact_id}/owner {user, note}` transfers ownership (recorded in the head record and the event log);
  purge of anyone's version; approve, reject and retry of artefact and collection deposits through the
  existing deposit routes.

---

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

The watcher executes purge requests recorded by the API (a `purges` table in the deposit store):

1. under the scope's writer lock, the head record marks the version `purging`;
2. for each file and preview listed in the version's manifest, HEAD the key, compare the `x-amz-meta-sha256`
   with the manifest, delete that one key, log it in `deletions`. No listing of the prefix decides what is
   deleted, and no batch delete is used;
3. the manifest goes last, then the local copies;
4. the head record marks the version `purged` with who, when and why, `latest` moves to the highest live
   version (or null), the head record is pushed and committed;
5. for a `de_table` version whose study is still the one in the dataset, the study is removed by the same
   all-or-nothing duckdb copy as the import, with a dataset patch version and a changelog line ("DE study
   removed: purged artefact ...") and a caveat, following the rule that every curation decision appears on the
   dataset page (open question 5).

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
