# Plan: dataset ingestion route (skill-guided curator uploads that publish themselves once server-side checks pass)

Status: accepted 2026-10-05 (Mark), with the recommended defaults of section 19. **P1 is implemented**
(`ant ingest-check`, `ant ingest-index`; see section 20 for what differs from this plan). P2 to P5 are open.
The S3 watch route (`ant ingest-watch`, section 21, 2026-10-14) publishes `mus s3 upload` deposits without the
upload API; it shares the build pipeline the P3/P4 worker should reuse. The deposit API (section 22,
2026-10-15, REST 2.46.0) replaces sections 3 to 7 for depositors: they sign in to Anthive, get presigned
uploads into the watch route's incoming prefix and follow their deposit, and the watcher stays the backend.
The artefact store (section 23, design only, 2026-10-16) extends the deposit API to DE tables, figures and
other generated files and to collections without datasets; its design is `doc/DESIGN_artefacts.md`.
It includes Mark's decisions sent mid-task:
- The role is called `curators`.
- A submission whose server-side checks pass is published automatically, with a "new, unreviewed" badge. Admins can retire it or send it back afterwards.
- Anything marked controlled-access, or with an unknown licence, waits for an admin.
- Builds run on a capped ingest worker, and S3 is the hub where each file is written once.


---

## 0. What exists today (reused)

| Piece | Where | Note for this design |
| --- | --- | --- |
| Paper upload gating | `lib/config.py:get_library_upload_settings`, `bin/api/ant-serve:_require_uploader` (line ~14686), `bin/api/ant-mcp:_may_upload` / `UPLOAD_TOOLS`, `anthive.default.yaml:library_upload_groups`, site.yaml `library.upload_groups`, rendered by `bin/ops/ant-stack` line 417 | Copy this pattern exactly for `datasets_upload_groups`. Basic auth mode: `auth.admins` count as `admins`. |
| Admin gating | `_require_admin`, `_require_admin_or_local` (accepts local tools and `X-Anthive-Internal` with no `Remote-User`) | The worker calls `/admin/release`, `/admin/restore` and `/admin/rescan` with the internal secret or the new worker token. |
| Staged swap | `POST /admin/release/{db_id}`, `POST /admin/restore/{db_id}`, `POST /admin/rescan`, `lib/config.py:get_convert_staging_dir` (`ANTHIVE_CONVERT_STAGING_DIR`) | Already the writer protocol of ant-score and AnnotationWriter. |
| Facet lint | `lib/facet_lint.py` (`lint_api`, `lint_api_data`, `lint_lineage`, `duckdb_reader`), `lib/md_check.py:check_md_folder(fix=...)`, `bin/meta/ant-md-check --no-fix --strict-namespace --with-data` | `--with-data` needs a `.duckdb`. `ant-md-check` **fixes by default** (writes ids), so always use `fix=False`. `md_check` **skips any path with a `_` folder part below the scanned root** (line 216): scan the submission's own folder, never `_incoming` from above it. |
| Derive forms, donor keys | `lib/obs_derive_forms.py` (`validate_spec`, `crosswalk_for`, `translate_value`, `group_id_for_md`), `lib/donor_keys.py` (`donor_key_map`, `key_digest`, `distinct_count`) | The crosswalk is read only inside the worker. |
| Annotation | `lib/mus.py` (`resolve_annotation`, `validate_annotation`, `required_fields`), `lib/_mus_vocab.yaml` | `access` vocabulary is only `internal | public`. `license` is a soft field with no vocabulary. Controlled-access needs a new field (section 9). |
| Structure validator | `<store>/scripts/validate_md.py` (id / group_id rules) | Run as a subprocess when present. `md_check` covers the same rules. |
| DuckDB integrity | `bin/ingest/ant-duckdb-check` | Reuse as a subprocess, **on a private copy only**: it is not read-only (it builds `__gene_bins`, may write `default_layer` into the `.md`, logs into the working directory). Found in P1, see section 20. |
| **Never** | `bin/ingest/ant-h5ad-check`, and `bin/meta/ant-check`, which calls ant-h5ad-check as a subprocess | Both rewrite `.md` destructively. A test enforces this (section 14). |
| Versioning | Already in store `.md`: `version: X.Y.Z` and a `changelog:` list of `{version, date, previous_sha256, changes}` | New datasets start at `1.0.0` with a first changelog entry. |
| Store metadata | `/data/1/project/h5adstore` is a git repo (gitlab `bdslab/anthive/h5adstore`) | `.md`, `.h5ad.mus` and `index.md` are git-tracked. `ant-sync` never touches them and skips any path with a `_` folder part. |
| Facet test thresholds | `bin/annot/ant-facet-test`: `MIN_DONORS_DATASET=6`, `MIN_DONORS_PER_LEVEL=3`, `BRAAK_MIN_DONORS=6`, `NUMERIC_MIN_DONORS_FOR_P=10`, `MIN_DONORS_FOR_AGE_PMI=15` | Move these to `lib/facet_rules.py` so ingest-check and the facet tests share one definition. |
| Skills | `data/skills/skills/<name>/SKILL.md` (+ `reference/`), YAML `name` and `description`, numbered sections, "the server is right" rule, explicit 403/404/503 handling, no site addresses, no data | Follow `anthive-paper-upload` (structure and refusal handling) and `anthive-setup` (one step at a time, ask before changing anything). |

---

## 1. Goals and non-goals

**Goals**
1. A curator (group `curators`, or `admins`) gets a dataset from their laptop or HPC into Anthive without repo, shell or S3 credentials on the server.
2. One public skill, `anthive-dataset-ingest`, knows every step and drives the curator in order. It explains every failed check and how to fix it.
3. All checks run server-side in the ingest worker. A result reported by the client is never trusted. vark and the skill only drive the process.
4. A pass is published automatically, defaulting to `access=internal` (login only), with a "new, unreviewed" badge. Admins review afterwards: they mark it reviewed, send it back or retire it.
5. Exceptions wait for an admin: controlled-access data, an unknown licence, or admin-only findings.
6. Each file is written once. S3 is the hub. The serving host stays read-only for the api, and CPU work runs in a capped worker.
7. Versioning from day one: `version: 1.0.0`, and a changelog entry with `sha256`.
8. P1 alone gives Mark a usable `ant ingest-check` on a local folder.

**Non-goals**
- No dataset data through the api process or MCP. Only small text (metadata, reports) goes through them.
- No pre-publication review page. It was dropped in favour of the post-hoc list.
- Curator-side conversion is out (section 6).
- No editing of existing published datasets through this route in P1 to P4. New versions of existing datasets come in P5.
- No exposure of `donor_crosswalk.tsv` contents, cohort aliases or donor identifiers to curators, the skill text or MCP.
- No automatic entry into the counted facet-test cohort selection (`_facet_tests/cohort_selection*.tsv`). That stays an admin decision (section 16).

---

## 2. Architecture

```
 curator laptop / HPC                      S3 bucket (hub)                       production host
 ----------------------                    ---------------                       -----------------------------------------
 AI client + skill                          _incoming/<user>/<sid>/x.h5ad         caddy -> api (ant-serve)    [read-only tree]
  anthive-dataset-ingest  --MCP (text)-->   (presigned multipart PUT)              |  submissions DB (state_dir/ingest/)
  vark datasets ...       --REST (text)-->  ------------------------------->       |  presigns with scoped incoming key
     |                                      <Group>/x.h5ad (server-side copy)      |  job queue (SQLite)
     +--- multipart PUT parts --------------> (one write per artifact)             |
                                            <Group>/x.duckdb, layer parquet  <-+   v
                                            (pushed async by worker)           |  ingest-worker container (cgroup capped:
                                                                               |   cpus, memory, 1 job, nice 19, ionice idle)
                                                                               |   claim job  -> /internal/ingest/jobs/claim
                                                                               |   pull h5ad from S3 once
                                                                               |   ant ingest-check (profile, checks, scratch convert)
                                                                               |   build: move into store, /admin/rescan,
                                                                               |          release/restore for writers, scoring,
                                                                               |          indexes, facet tests, git commit .md
                                                                               +-- ant-sync push '<Group>/**' (throttled)
                                                                                   h5adstore/<Group>/... (served locally)

 optional overflow worker (HPC Apptainer, docker/anthive-ingest.def): same claim protocol, pulls from S3,
 pushes outputs to S3, then the prod worker runs a "pull + rescan" job.
```

What runs where:

| Side | Runs | Never runs |
| --- | --- | --- |
| Curator (skill + vark or pyvark) | prepare `.md` / `.h5ad.mus`, compute sha256 locally, multipart upload, read the obs profile and reports, propose facet mappings, literature lookup via the library MCP tools | any check that counts as authoritative; any access to the crosswalk |
| api (ant-serve) | auth, submission records, state machine, presigning, small text payloads, queue, redacting reports | reading or parsing h5ad, converting, reading the crosswalk |
| ingest worker | checksum verification, obs profiling, `ant ingest-check`, scratch and final convert, build steps, S3 copy and push, git commit | serving |
| admin (web UI / API) | post-hoc list, mark reviewed, send back, retire, approve held submissions | |

---

## 3. Data model

### 3.1 Staging layout

| Location | Content | Who writes |
| --- | --- | --- |
| S3 `_incoming/<user>/<sid>/<basename>.h5ad` | the uploaded data (multipart) | curator via presigned URLs |
| S3 `_incoming/<user>/<sid>/meta/` | not used: metadata lives in the submission record | |
| `<h5adstore>/_incoming/<user>/<sid>/` | worker staging: `<Group>/index.md` (new group only), `<Group>/<basename>.{h5ad,md,h5ad.mus}`, scratch `.duckdb` and layer parquet, `reports/` | worker only |
| `<scratch_dir>/ingest/<job_id>/` | DuckDB spill and `ANTHIVE_CONVERT_STAGING_DIR` | worker |
| `config.get_ingest_index_dir()` (default `<state_dir>/ingest/index`) | overlap indexes (changed in P1, they are not in the store; section 20) | `ant ingest-index` |

The underscore keeps everything out of the catalogue scan (`ant-serve` line 1150), `ant-sync` (line 170), `md_check` and `catalog_manifest`. `<user>` is the `Remote-User`, sanitised to `[a-z0-9._-]`. `<sid>` is a ULID.

### 3.2 Submission record

Stored in `<state_dir>/ingest/submissions.sqlite`. The api owns it, and api `state_dir` is already read-write. Resolve it with a new `config.get_ingest_state_dir()` (key `ingest_state_dir`, env `ANTHIVE_INGEST_STATE_DIR`, default `<state_dir>/ingest`). Workers never open the file. They go through `/internal/ingest/*`, so remote workers work the same way.

Tables:
- `submissions(sid PK, user, group_name, group_id, basename, title, state, access, licence, controlled, created, updated, h5ad_sha256, h5ad_size, s3_key, upload_id, md_text, mus_text, index_md_text, md_hash, last_check_id, last_check_md_hash, db_id, version, unreviewed BOOL, held_reasons JSON, admin_comments JSON)`
- `jobs(job_id PK, sid, kind ENUM(verify, profile, check, build, post, retire, pull_rescan), state ENUM(queued, running, done, failed, cancelled), worker_id, claimed_at, heartbeat_at, attempts, params JSON, result JSON, log_tail TEXT)`
- `events(id, sid, ts, actor, action, detail JSON)`: the audit log. The same lines are also written to `<state_dir>/ingest/audit.jsonl`.
- Reports: `<state_dir>/ingest/reports/<sid>/<check_id>.json` (full) and `<check_id>.curator.json` (redacted).

Module: `lib/ingest_store.py`, with `SubmissionStore` (SQLite WAL, a single writer lock), `transition(sid, to, actor, detail)` that validates edges, `enqueue(kind, sid, params)`, and `claim(worker_id, capabilities)` (an atomic `UPDATE ... WHERE state='queued' ... RETURNING`).

### 3.3 States

```
draft -> uploading -> verifying -> ready --(PUT metadata, POST check)--> checking
checking -> needs_changes (errors)          -> (curator edits) -> checking
checking -> passed
passed --(POST submit)--> held      (controlled / licence unknown / admin-only findings)
passed --(POST submit)--> building  (auto-publish allowed)
held --(admin approve)--> building ; held --(admin send-back)--> needs_changes ; held --(admin reject)--> rejected
building -> live (unreviewed=1) -> live (unreviewed=0, after admin "reviewed")
building -> build_failed --(admin rerun)--> building
live --(admin send-back)--> needs_changes (dataset set inactive) ; live --(admin retire)--> retired
any non-final --(curator withdraw)--> withdrawn
```

Rules:
- `submit` is refused unless `last_check_md_hash == md_hash`, i.e. the current metadata is the metadata that passed.
- Retention: `draft`, `withdrawn` and `rejected` submissions older than `datasets_incoming_retention_days` are cleaned up. The worker aborts their multipart uploads and deletes `_incoming/` objects. An S3 lifecycle rule on `_incoming/` is the backstop.

---

## 4. API endpoints (ant-serve, REST 2.4x, tag `datasets-ingest`)

Gate: `_require_curator(request)`, a copy of `_require_uploader` that reads `get_dataset_upload_settings()['upload_groups']`. Curators see only their own submissions; admins see all. When `services.dataset_upload` is off, every route answers 503 `{"status": "disabled"}`.

| Method + path | Role | Payload -> answer |
| --- | --- | --- |
| `GET /datasets/ingest/requirements` | curator | -> `{uploads_enabled, max_upload_gb, part_mb, quota_gb_remaining, required_md_keys, required_mus_fields, facet_registry_url, replicate_units:[donor,animal,sample,none], disease_basis_values, access_values, check_ids:[...], min_skill_version, auto_publish, default_access}` |
| `POST /datasets/ingest/submissions` | curator | `{group, basename, title, new_group:bool, file:{name,size,sha256}}` -> 201 `{sid, state:'draft'}`. Checks size vs limit and quota, filename via `h5ad_utils.validate_filename`, and group existence. |
| `GET /datasets/ingest/submissions` | curator (own) / admin (`?user=&state=`) | -> list |
| `GET /datasets/ingest/submissions/{sid}` | owner / admin | -> record without the texts, plus `next_actions` |
| `POST /datasets/ingest/submissions/{sid}/upload` | owner | `{part_size}` -> `{upload_id, key, parts:[{n,url}], expires}`. Calls `CreateMultipartUpload` with `ChecksumAlgorithm=SHA256`, and presigns `UploadPart` URLs for the first N parts. State -> `uploading`. |
| `POST /datasets/ingest/submissions/{sid}/upload/parts` | owner | `{from,to}` -> fresh presigned URLs (resume or expiry) |
| `GET /datasets/ingest/submissions/{sid}/upload` | owner | -> `{parts_done:[{n,etag,size}]}` via `ListParts` (resume) |
| `POST /datasets/ingest/submissions/{sid}/upload/complete` | owner | `{parts:[{n,etag,checksum_sha256}]}` -> `CompleteMultipartUpload`, enqueue `verify`. State -> `verifying`. |
| `DELETE /datasets/ingest/submissions/{sid}/upload` | owner | abort the multipart upload |
| `GET /datasets/ingest/submissions/{sid}/obs-profile` | owner / admin | -> the worker's profile: columns, dtype, n_unique, NA fraction, the top 30 values with counts, numeric min/max/quantiles, `candidate_facets` (from `facet_mapping.candidate_columns`), layers summary, var name sample |
| `PUT /datasets/ingest/submissions/{sid}/metadata` | owner | `{md, mus, index_md?}` (text, 256 KB cap each) -> `{md_hash, static_findings}`. `static_findings` covers YAML parse, `facet_lint.lint_api` without data in strict namespace, and `mus.validate_annotation`. These are pure functions with no crosswalk and no data, so they are safe in the api. They are advisory only. |
| `POST /datasets/ingest/submissions/{sid}/check` | owner | -> 202 `{job_id}`. State -> `checking`. |
| `GET /datasets/ingest/submissions/{sid}/report` | owner (redacted) / admin (`?full=1`) | -> report JSON (section 8) |
| `GET /datasets/ingest/submissions/{sid}/report.md` | owner / admin | -> human summary |
| `POST /datasets/ingest/submissions/{sid}/submit` | owner | `{note}` -> `{state:'building'|'held', held_reasons}` |
| `POST /datasets/ingest/submissions/{sid}/withdraw` | owner | -> `withdrawn` |
| `GET /admin/ingest/submissions` | admin | `?state=&unreviewed=1&since=` -> list for the post-hoc page |
| `POST /admin/ingest/submissions/{sid}/approve` | admin | `{comment}`: held -> building |
| `POST /admin/ingest/submissions/{sid}/reviewed` | admin | `{comment}`: clears `unreviewed` |
| `POST /admin/ingest/submissions/{sid}/send-back` | admin | `{comments:[{check_id?, text}]}`: held/live -> needs_changes. If live, enqueue `retire` with `mode=inactive`. |
| `POST /admin/ingest/submissions/{sid}/retire` | admin | `{reason}` -> `retire` job (`ant inactive` semantics: drop from the API, keep files) |
| `POST /admin/ingest/submissions/{sid}/reject` | admin | `{reason}` |
| `POST /admin/ingest/submissions/{sid}/rerun` | admin | `{kind: check|build|post}` |
| `POST /internal/ingest/jobs/claim` | worker token | `{worker_id, capabilities:{local_store:bool, max_cells, max_gb, kinds:[...]}}` -> job or 204 |
| `POST /internal/ingest/jobs/{job_id}/heartbeat` | worker token | `{progress, log_tail}`. A job with no heartbeat for 10 min is requeued, at most 2 attempts. |
| `POST /internal/ingest/jobs/{job_id}/finish` | worker token | `{state, result, report?}`. The api applies the state transition. |
| `GET /internal/ingest/submissions/{sid}/payload` | worker token | -> md, mus and index_md texts, plus the S3 key |

Worker auth: a new secret `<state_dir>/secrets/ingest_worker_token`, checked by `_require_ingest_worker`, a constant-time compare on header `X-Anthive-Ingest-Token`. Extend `_require_admin_or_local` to accept it too, so the worker can call `/admin/release`, `/admin/restore`, `/admin/rescan` and `/admin/refresh-views`.

Presigning: `lib/ingest_s3.py` with `presign_parts()`, `complete()`, `list_parts()` and `abort()`, using boto3 and the **scoped incoming key** (section 11).

Size limits: enforced at `POST /submissions` (declared size), at complete (sum of part sizes), and again in `verify` (actual object size).

---

## 5. Clients

### vark (Go, `vark/internal/cli/datasets_ingest.go`, `vark/internal/api/ingest.go`)

| Command | Does |
| --- | --- |
| `vark datasets requirements [--server S] [-o json]` | GET requirements |
| `vark datasets upload FILE.h5ad --group G --basename B --title T [--new-group] [--part-mb 64] [--resume SID]` | local sha256 (streamed, progress), POST submission, multipart upload with 4 parallel parts, per-part retries, resume through `GET .../upload`, a state file `~/.cache/vark/uploads/<sid>.json`, then complete. Prints the sid. |
| `vark datasets status [SID] [-o json]` | list or one submission |
| `vark datasets profile SID [-o json]` | obs profile |
| `vark datasets metadata SID --md B.md --mus B.h5ad.mus [--index index.md]` | PUT metadata, prints static findings |
| `vark datasets check SID [--wait]` | trigger and poll until a report exists |
| `vark datasets report SID [-o json|md]` | report |
| `vark datasets submit SID [--note ...]` | submit |
| `vark datasets withdraw SID` | withdraw |

`vark server check` gains `curator: true|false` and `dataset_uploads_enabled`, mirroring `uploader` and `uploads_enabled` in `internal/cli/server_check.go`.

### pyvark (`vark/client.py`)

Methods: `get_dataset_ingest_requirements()`, `create_dataset_submission(...)`, `upload_dataset(path, group, basename, title, part_mb=64, resume=None, progress=True)`, `get_dataset_submission(sid)`, `get_obs_profile(sid)`, `put_dataset_metadata(sid, md, mus, index_md=None)`, `check_dataset(sid, wait=True, timeout=3600)`, `get_dataset_report(sid, fmt='json')`, `submit_dataset(sid, note=None)`. Tests go in `tests/test_ingest_offline.py` with mocked HTTP. vark and pyvark are read-only for this agent: hand both to their owners as a spec.

---

## 6. Where does the build run, and how much data moves

Measured: `ant-convert` takes 4m43s on M22/PFC (172k cells) and 7m53s on RX4/ALL (433k cells). RX4/ALL produces a 223 MB `.duckdb` plus 7.0 GB X parquet and 4.7 GB counts parquet.

Facet tests are measured from `_facet_tests/*/*manifest.json`: 124 runs, 2,220 s in total (median about 18 s), maximum 241 s (Lee2026 PsychAD).

Measured in P1 (2026-10-05, `taskset -c 4-11 nice -n 19 ionice -c3`, scratch convert of M22/PFC, 172k cells, 3.3 GB h5ad):
`ant ingest-check` end to end took 367 s wall with a peak of 39.7 GB resident memory. That is the scratch
`ant-convert` (about 5 min, 11 GB of scratch files: 7.2 GB of layer parquet, a 30 MB database) plus the hash and
all 33 checks (under a minute). Without a scratch convert (database already there) the checks take 6 to 51 s on
the five validation datasets (22k to 290k cells, peak 0.3 to 7.7 GB). The downstream build steps were **not**
measured: scoring, gene stats, module variability and facet tests write into the live store caches and the served
databases, so a clean measurement needs the throwaway store of the end to end test (section 15). The table below
stays an estimate.

Estimates for the downstream steps, to be measured on RX4/ALL in a throwaway store:

| Step | Estimate for 400k cells |
| --- | --- |
| ant-score over the default geneset collections | 10 to 40 min. scanpy `score_genes` per geneset; it scales with geneset count. |
| UCell (part of the default `score` step; AUCell is not used, see `bin/annot/CLAUDE.md`) | 20 to 60 min (the heaviest) |
| gene stats and variance precompute | 5 to 15 min |
| module variability | 5 to 20 min |
| facet tests (gene + modules) | 1 to 5 min |
| DE pseudobulk (optional) | 1 to 10 min |

Downstream steps are 2x to 6x the convert time, so they dominate. They must not run in the api and must be capped.

Let H be the h5ad size and D the derived files (duckdb + parquet, roughly 1x to 1.5x H).

| Option | Transfers | Host CPU used | Latency to visible | Notes |
| --- | --- | --- | --- | --- |
| A. Dev box today (manual) | curator -> dev (H), dev -> S3 (H + D), S3 -> prod (H + D): 3 hops, 2H + 2D through networks | dev (Mark dislikes) | hours, manual | data hops twice; human in the loop |
| **B. Worker on the prod host, cgroup capped (recommended)** | curator -> S3 (H, needed anyway as the archive), S3 -> prod (H once), outputs written in place in the serving store (0 hops), prod -> S3 push of D (once, async, throttled for backup and mirrors). The final h5ad key is an S3 server-side `CopyObject` from `_incoming/` (no bytes through a host). | prod, capped: `cpus` (for example 4 of N), `mem_limit` (for example 48g), one job at a time, `nice 19`, `ionice -c3`, `blkio_config.weight: 100` | convert minutes after submit; downstream after that | every artifact is written once; no dev box |
| C. HPC worker (Apptainer, `docker/anthive-ingest.def`) | curator -> S3 (H), S3 -> HPC (H), HPC -> S3 (D), S3 -> prod (D): 4 transfers, H + 2D more than B | HPC | queue wait plus transfers | good overflow for >1.5M cells or when the prod host is busy; same claim protocol |
| D. Curator-side conversion | curator -> S3 (H + D, and D is larger than H for big datasets) | curator | | rejected: checks must run server-side, tool version skew, curator needs the full anthive env, results not trustworthy |
| E. Serve directly from S3 (httpfs) instead of local copies | no S3 -> prod copy, but every query reads remote | none | | rejected for primary serving: the catalogue scan is known to be slow with remote objects (DEPLOY_CONTAINERS troubleshooting: "`up` waits ... scanning a large, mostly remote catalogue"), plus remote latency on every expression read. Acceptable only on mirror sites with `s3.api_access: read`. |

**Recommendation: B, with C as overflow through the same queue.** Jobs carry `n_obs` and the size from the profile. A worker claims a build only when they fit its `capabilities` (`datasets_worker_local_max_cells`, default 1,500,000; `..._local_max_gb`, default 60). Bigger jobs wait for a worker with `local_store: false` (HPC). That worker writes its outputs to S3. A small `pull_rescan` job then runs on the prod worker: `ant sync pull --include-duckdb '<Group>/<basename>*'`, then the rescan.

The api stays read-only on the tree. Only the ingest worker mounts `data_rw`, as the one-off ingest container does today.

Two-stage visibility on B, so curators are not kept waiting:
- Stage 1: convert, move into the store, rescan. The dataset becomes visible with the "new, unreviewed" badge and a "processing" note.
- Stage 2: scoring, indexes, facet tests and DE each refill the dataset under release/restore. When stage 2 is done, the note clears.

---

## 7. MCP tools (`bin/api/ant-mcp`)

`INGEST_TOOLS` are visible only to `curators` and `admins` while `dataset_uploads_enabled`, using the same visibility filter as `UPLOAD_TOOLS`. All are thin wrappers over REST; ant-serve enforces the rules again.

| Tool | Role | Notes |
| --- | --- | --- |
| `dataset_ingest_requirements()` | curator | |
| `list_dataset_submissions()` | curator (own) | |
| `dataset_submission_status(sid)` | curator | includes `next_actions` |
| `dataset_obs_profile(sid)` | curator | value counts of the curator's own dataset; never crosswalk content |
| `put_dataset_metadata(sid, md, mus, index_md=None)` | curator | text only, size-capped |
| `check_dataset_submission(sid)` | curator | enqueues; poll with status |
| `dataset_submission_report(sid)` | curator | redacted report |
| `submit_dataset(sid, note)` | curator | |

No upload tool: data never goes through MCP. The skill always uses vark or pyvark for the h5ad.

No admin tools (approve, reviewed, send-back, retire, reject). Admin actions are UI and API only, for an audit trail tied to a browser session. Literature help uses the existing library tools (the anthive-literature tool set). A source paper is added through the existing paper-upload flow, with `anthive_role: source`.

---

## 8. `ant ingest-check`

### 8.1 Tool and package

- CLI: `bin/ingest/ant-ingest-check` (a uv script like its neighbours), registered in `bin/ant` `command_descriptions` as `'ingest-check': 'Check a dataset submission before publication (read-only, JSON report)'`.
- Usage: `ant ingest-check <GROUP_DIR | file.h5ad> [--store PATH] [--json OUT] [--md-summary OUT] [--profile-only] [--no-convert] [--duckdb PATH] [--audience curator|admin] [--checks id,id] [--skip id]`
- Library: `lib/ingest_check/` with these modules:
  - `__init__.py`: registry of `Check(id, family, severity_default, fn)`
  - `context.py`: `CheckContext` with paths, the md and mus dicts, a lazy h5ad summary (`h5ad_view.H5adSummary`: h5py, obs and var decoded with `anndata.io.read_elem`, X, layers, obsm, obsp, varm and uns only listed; never `anndata.read_h5ad`, whose backed mode reads layers, raw, obsm, obsp and uns into memory in full), a lazy duckdb reader, store root, group_id, crosswalk access and `redact()`
  - `report.py`: JSON schema, md renderer, redaction
  - `profile.py`
  - `checks_files.py`, `checks_meta.py`, `checks_layers.py`, `checks_facets.py`, `checks_donors.py`, `checks_cells.py`, `checks_design.py`, `checks_access.py`
- Read-only on its input. If no `.duckdb` is given, it runs `ant-convert` on a **scratch copy**: it symlinks the h5ad into `<tmp>/<Group>/`, copies the `.md`, and runs with `ANTHIVE_CONVERT_STAGING_DIR=<scratch>`, so the curator's folder is never touched. The scratch convert is cached by `(h5ad_sha256, convert-relevant md keys)`, so metadata-only edits re-run the views (`ant refresh-views` logic) and not the convert.
- Guard: the package must never import or invoke `ant-h5ad-check`, `ant-check`, or `md_check` with `fix=True`.
- Exit code: 0 for pass (no errors), 1 for errors, 2 for a tool failure.

### 8.2 Report JSON (`schema: anthive.ingest_check/1`)

```json
{"schema":"anthive.ingest_check/1","tool_version":"<anthive __version__>","created":"...",
 "dataset":{"group":"...","basename":"...","sha256":"...","n_obs":0,"n_vars":0},
 "result":"pass|fail","publish":"auto|held","held_reasons":["public_access","licence_unknown"],
 "counts":{"error":0,"warning":0,"info":0},
 "checks":[{"id":"facets.unmapped_values","status":"pass|fail|warn|skip","severity":"error",
            "summary":"...","details":{...},"fix":"... how to fix ...","docs":"skill reference anchor"}],
 "admin_detail":{...}}
```

`admin_detail` holds cohort names, matched dataset ids and demographic match tables. It is stripped in the curator view. Identifiers never appear in either view: donor keys appear only as `key_digest`.

### 8.3 Checks

| id | Input | Method (reuse) | Fail rule (error) / warn |
| --- | --- | --- | --- |
| `files.sha256` | h5ad, declared sha256 | `checksum.calculate_sha256` (the worker already did this in `verify`) | mismatch = error |
| `files.size` | object size | config limits | over the limit = error |
| `files.filename` | basename | `h5ad_utils.validate_filename` | invalid = error |
| `h5ad.structure` | h5ad | `h5ad_utils.validate_structure`, `get_h5ad_info` | unreadable or missing X/obs/var = error |
| `meta.md_parse` | `.md` | `metadata.parse_md_file` | parse error = error |
| `meta.catalog` | staging folder | `md_check.check_md_folder(folder, fix=False, store=<store>)` and `<store>/scripts/validate_md.py` as a subprocess when present | id or group_id invalid or duplicate = error |
| `meta.required_keys` | `.md` | key list from requirements (title, description of at least 200 characters, version, changelog, replicate_unit, disease_basis, access-related keys) | missing = error |
| `meta.index_md` | `<Group>/index.md` | new group: `group:`, `group_id` (3 chars, unique in the store via `md_check.collect_db_ids`), doi or `paper_title`, year, authors, `default_dataset`; existing group: group matches | error |
| `mus.vocab` | `.h5ad.mus` and cascade | `mus.resolve_annotation`, `mus.validate_annotation`, `mus.required_fields()` | a required field missing or not in the vocabulary = error; soft field missing = warn |
| `layers.counts` | h5ad layers | sample 2,000 cells (`default_rng(0).choice`, sorted; the same cells as vark's local inspector), read with h5py (`sampling.read_rows`); when those cells would decompress more than 1.5 GB of one compressed chunked matrix (X or the first counts candidate, e.g. dense gzip in chunks of ~1,000 cells), 16 runs of 125 consecutive cells instead (`sampling.choose_rows`, `sample_mode: sampled_runs` in the details and the summary, as in vark); a layer is counts if all sampled values are non-negative integers | no counts layer = error unless `.md` has `counts_unavailable: <reason>` (then warn) |
| `layers.x_lognorm` | X | sampled: non-integer, max below 20, `expm1(X)` row sums with CV below 0.1, or a declared `normalisation:` | X looks raw or scaled (negative values, z-scores) = error |
| `layers.consistency` | X vs counts | `log1p(counts/rowsum*1e4)` vs X on 1,000 cells: median r at least 0.99 (the method of the Aldinger changelog) | below 0.95 = error; 0.95 to 0.99 = warn |
| `genes.ids` | var_names, var columns | share of var_names found in the gene universe of `gene_stats_index.parquet` for the organism; Ensembl-like names (`ENSG`, `ENSMUSG`) as the index = error (symbols required, Ensembl in `ensg_id`); duplicated var_names = error | below 70% known = error; 70% to 90% = warn |
| `duckdb.integrity` | scratch duckdb | `bin/ingest/ant-duckdb-check <db>` (no `--clean-wal`) | non-zero exit = error |
| `facets.namespace` | `.md api` | `facet_lint.lint_api(api, namespace_mode='strict')` | unregistered `ant_*` = error; deprecated = warn |
| `facets.mapping_static` | `.md api` | `lint_api`: rename chains, dtype vs kind, vocabulary, `ant_braak_group` rule | error findings = error |
| `facets.mapping_data` | api + duckdb | `facet_lint.lint_api_data` via `duckdb_reader` | missing source column or unmapped non-NULL values = error |
| `facets.unmapped_values` | obs profile + api | every value of every source column of `obs_rename`/`obs_derive` is mapped, renamed, hidden or default-covered (`facet_mapping.translate_observed`) | any unmapped value = error, listing the values |
| `facets.braak_range` | `ant_braak` values | whole numbers 0 to 6 (`facet_lint.parse_braak_label`) | outside = error |
| `facets.core_present` | api | human or mouse tissue: `ant_donor` (or `ant_sample`), `ant_sex`, `ant_disease` mapped or `obs_constant`; AD-related: `ant_braak` or an explicit `braak_unavailable` | missing = warn (error for `ant_donor` when replicate_unit is donor) |
| `donor.uid_form` | `api.obs_derive.ant_donor_uid` | `obs_derive_forms.validate_spec`; template or join | missing = error for human donor datasets; malformed = error |
| `donor.uid_resolves` | join form + crosswalk (worker only) | `crosswalk_for` + `translate_value` over the distinct donor values | cohort unknown = error; more than 20% of values falling to the fallback = warn (admin_detail lists which, the curator view only counts) |
| `donor.overlap_ids` | new donor keys vs `_ingest_index/donors.parquet` | `donor_keys.donor_key_map` -> `key_digest` set; intersect per existing dataset | 1 or more shared uids without `is_secondary`/`reanalysis_of`/`donor_group`/`facet_donor_exclusions` = error; with a designation = info |
| `donor.overlap_demographics` | per-donor (sex, age, APOE genotype, Braak, PMI) vs index | candidate if sex, APOE and Braak are equal, age within 1 year (90+ treated as censored-equal) and PMI within 1 h; per existing cohort, the fraction of new donors with exactly one candidate | at least 50% matched to one cohort without a designation = **held** (admin), not a curator error; 20% to 50% = warn. Counts only to the curator; the matched cohort and dataset go to admin_detail. |
| `cells.overlap` | hashed `(normalised barcode, donor key)` vs `_ingest_index/cells/*.parquet` | barcode normalisation strips `-1`, sample prefixes and suffixes; xxhash64; per existing dataset, the fraction of new cells found | at least 95% of new and existing cells match = "same cells": error with fix "use the merge path (new version of <db_id>)"; at least 50% of new cells inside one dataset = reanalysis: error unless `is_secondary: true` and `reanalysis_of: <db_id>` |
| `design.designation` | `is_secondary`, `reanalysis_of`, `donor_group` | `facet_lint.lint_lineage` | error findings = error |
| `design.replicate_unit` | `replicate_unit`, `replicate_column` | the value is in `donor|animal|sample|none`; the column exists, and its values nest within donor (or animal) | error |
| `design.replicates_per_group` | replicate column × each core facet | `lib/facet_rules.py` thresholds: at least 6 units in total, at least 3 per facet level | below = warn (dataset still useful, facet tests skip it); `replicate_unit: none` = info |
| `design.disease_basis` | `disease_basis` | present and in the vocabulary (set by the other agent's work) | missing = error |
| `version.initial` | `version`, `changelog` | new dataset: `version == 1.0.0`, one changelog entry with `version`, `date`, `sha256 == h5ad sha256`, `changes` | error |
| `access.level` | mus `access` | `internal` or `public` | missing = error; `public` = held unless `datasets_auto_publish_public: true` |
| `access.controlled` | new mus/md field `data_use: open|controlled` (section 9), plus cohort hints from the crosswalk (worker) | | invalid value = error; `controlled`, missing, or a join-form cohort flagged controlled in the crosswalk = warn, never held (D7 revised 2026-10-20) |
| `access.licence` | mus `license` | SPDX-like id (CC-BY-4.0, CC0-1.0, ...) or `in-house` | missing or `unknown` = held |
| `scores.modules_complete` | duckdb, scores sidecar, geneset store | facet-eligible datasets only (the metadata gate of `ant-facet-test`); `celltype_scope.applicable_experiments` lists the catalogs with at least one cell in the scope of one of their modules, and the sidecar must hold `geneset` (scanpy) and `ucell` rows for each | missing sidecar or score sets = warn (scoring is an automatic build step after the conversion, not a curator input; the fix is `ant-score-fleet run --only <db_id>`); not facet-eligible or no applicable catalog = skip |

`publish` is `auto` only when there are no errors and no held reasons.

### 8.4 Overlap indexes: `bin/ingest/ant-ingest-index`

`ant ingest-index build [--store PATH] [--only DB]` writes:
- `donors.parquet`: `db_key, group_id, key_digest, key_kind, sex, age, age_censored, apoe, braak, pmi, n_cells`. Digests only, never raw uids.
- `cells/<db_key>.{strict,loose}.npy`: 64-bit hashes of (normalised barcode, donor digest) and of the barcode alone (BLAKE2b, 8 bytes; section 20).
- `genes.parquet`: the gene names per organism, for `genes.ids`.
- Location: `config.get_ingest_index_dir()`, not `<store>/_ingest_index/` (section 20).

The build is incremental, keyed on the duckdb mtime and sha. It reads the `ant_*` views read-only. The worker refreshes the index after every build. The files never go to S3 or the API, but they contain quasi-identifiers: mode 0700 / 0600, like the crosswalk.

---

## 9. Metadata additions (coordinate with the agents doing replicate_unit and disease_basis)

- `.md`: `replicate_unit`, `replicate_column` (other agent), `disease_basis` (other agent), `counts_unavailable` (optional reason), `normalisation` (optional), `ingest: {sid, curator, submitted, check_report_sha256}` (written by the worker).
- `.h5ad.mus`: `data_use=open|controlled`, a new soft field. It needs a mus vocabulary change upstream (codeberg mfiers/mus) and a refresh of `lib/_mus_vocab.yaml`. `license` stays free text but is checked by `access.licence`.
- `lib/obscol_tags_vocab.yaml`: no change.
- The crosswalk gets an optional column `controlled` per cohort. This is private: the file is controlled-access, so the change is invisible to the public skill.

---

## 10. The skill: `data/skills/skills/anthive-dataset-ingest/`

Files:
- `SKILL.md`
- `reference/md-template.md` (annotated dataset `.md` with api block, designation, replicate keys, version 1.0.0, changelog)
- `reference/mus-template.md`
- `reference/facet-mapping.md` (the forms, with worked examples of `obs_rename`, `obs_value_rename`, `obs_value_hidden`, `obs_derive` map/template/join, `obs_constant`, `obs_hidden`)
- `reference/checks.md` (one entry per check id: what it means, typical causes, how to fix, when to ask an admin)
- `reference/designation.md` (primary vs secondary, reanalysis_of, donor_group, merge path)

Update `data/skills/README.md`, add the zip to `scripts/build_zips.py`, and update the "anthive-basics -> next skill" pointer.

### SKILL.md outline

Front matter:
- `name: anthive-dataset-ingest`
- description: "Prepare, check and submit a single-cell dataset (.h5ad) for Anthive: upload with vark, describe it (.md catalogue entry, .h5ad.mus annotation, facet mapping, donor ids, design), run the server checks, fix what fails, and submit. For Anthive admins and curators; for everyone else it prepares the files to send to an admin. Requires access to an Anthive server and the vark CLI."

Sections:

0. **Rights and server.** `dataset_ingest_requirements` / `vark datasets requirements`, and `vark server check` (`curator`, `dataset_uploads_enabled`).
   - 403: only curators may add datasets here. Offer to prepare the files for an admin, with no workarounds.
   - 404: the server is too old.
   - 503 `disabled`: ask the operator.
   - "When the server differs from this skill, the server is right."
1. **Ground rules (always).**
   - Never print, guess or ask for donor identifiers of other cohorts or crosswalk contents.
   - Never upload through MCP.
   - Never edit server files.
   - Never skip a failed check or argue it away. Fix it, or explain why an admin must decide.
   - Never mark data `open` or `public` without the user confirming the licence and consent.
   - Ask before each upload or submit.
   - Visibility: `internal` means everyone who can log in to this site.
2. **Gather.** Paper (DOI), file location, organism and tissue, who generated it, licence and data use agreement, whether it reuses donors or cells of a published cohort, and the target group (existing collection or new).
3. **Upload.** `vark datasets upload ... --resume`. Explain the sid, how to resume, and the quota. Wait for the `ready` state (verify + profile).
4. **Read the profile and the paper.** `dataset_obs_profile`; literature search for the methods (library tools; if the paper is missing, propose `anthive-paper-upload` with role `source`).
5. **Describe: catalogue `.md`.** Title, description from the paper (no invention), `version: 1.0.0`, changelog with sha256, `replicate_unit` / `replicate_column`, `disease_basis`.
6. **Describe: `.h5ad.mus`.** The seven required fields, soft fields, `access` (default internal), `license`, `data_use`.
7. **Facet mapping.**
   - Propose `obs_rename` / `obs_derive` per column from the profile's `candidate_facets` and the paper.
   - Show a table of column -> facet -> value map and get confirmation.
   - Every value must be mapped, hidden or defaulted.
   - Braak as integers 0 to 6.
   - `ant_donor_uid`: the template form `'{group_id}:{value}'` by default. When the paper says the donors come from a named cohort, say that an admin-held crosswalk may join them, use the join form with the cohort name the server's requirements list, and let the check report resolution counts.
8. **Designation.** Primary vs `is_secondary`, `reanalysis_of`, `donor_group`. If the curator says it is the same cells as an existing dataset, explain the merge path.
9. **Stage and check.**
   - `put_dataset_metadata`, then read the `static_findings`.
   - `check_dataset_submission`; poll `dataset_submission_status` every 30 to 60 s, with the expected duration from requirements.
   - `dataset_submission_report`.
10. **Fix loop.**
    - For each failing check id: look up `reference/checks.md`, explain it in plain words, propose a concrete edit, apply it after an OK, and re-check.
    - At most 5 rounds, then suggest contacting an admin with the report link.
    - Data-level failures cannot be fixed by metadata. Say that a new h5ad upload is needed (new submission) and how to produce it, for example adding raw counts.
11. **Submit.** Summarise what will happen:
    - auto-publish as internal with a "new, unreviewed" badge, or held, and why;
    - the curator can withdraw before the build;
    - admins may send it back later.
    Then `submit_dataset`.
12. **After.** Report the sid, state and dataset id once live. Track stage 2 (scoring and facet tests) via status. Post-publication fixes go as a new version (P5).

Failure handling table: one row per HTTP answer (401, 403, 404, 409 duplicate group or basename, 413 size or quota, 422 invalid metadata, 503 disabled, 5xx retry with back-off at most 3 times). Plus vark exit codes (resume instructions).

Must never: anything that bypasses section 1, contacting S3 directly, putting identifiers in the `.md` description, claiming the dataset is checked before the server report says pass, or editing an existing published dataset through this route.

---

## 11. Security

- **Roles:**
  - `curators` (Authelia group, set in `datasets.upload_groups`): own submissions only.
  - `admins`: everything.
  - The worker token covers `/internal/ingest/*` and the admin release, restore and rescan endpoints only.
  - Basic auth mode: `auth.admins` count as admins.
  - `auth.mode: none` disables the feature.
- **S3:**
  - The api gets a **scoped incoming key**: `secrets/s3_incoming_access_key_id` and `secrets/s3_incoming_secret_access_key`. Its policy allows only `PutObject`, `CreateMultipartUpload`, `UploadPart`, `CompleteMultipartUpload`, `AbortMultipartUpload` and `ListMultipartUploadParts` on `<bucket>/_incoming/*`. It is mounted into the api only when `services.dataset_upload: true`.
  - The presigned URL TTL is `datasets_upload_url_ttl_minutes`, default 60.
  - The key prefix comes from the server (`_incoming/<user>/<sid>/`), never from the client.
  - Fallback when the provider cannot scope keys: `s3.api_access: write`, with an `ant-stack check` warning.
- **No data through the api or MCP:** only text up to 256 KB per field, and reports. Enforced by request size limits on those routes.
- **Controlled access:**
  - The crosswalk and overlap indexes (`config.get_ingest_index_dir()`) are read only by the worker and `ant ingest-check`.
  - Reports carry digests or counts in the curator view and names in `admin_detail` only.
  - Controlled or licence-unknown data is never auto-published.
  - The public skill holds no cohort aliases, no site addresses and no file paths of private files.
- **Quotas:** `datasets_upload_quota_gb_per_user` (sum of live and pending incoming bytes), `datasets_upload_max_open_per_user`, and a per-site `datasets_upload_max_gb`.
- **Audit:** every transition, presign, metadata PUT, admin action and worker job goes to `events` and to `audit.jsonl`, with actor, ip (from Caddy), sid and the sha256 of payloads. Git commits of `.md` name the curator as author and anthive as committer (the library pattern).
- **Untrusted inputs:**
  - The h5ad is opened in the worker only, under cgroup limits with no network except the api and S3.
  - YAML is loaded with `safe_load`.
  - The md/mus texts are written to disk by the worker under fixed names.
  - The basename and group are validated against `[A-Za-z0-9._-]` (path traversal).

---

## 12. Config keys

`anthive.default.yaml` (flat, like `library_*`), read by `lib/config.py:get_dataset_upload_settings()`:

```yaml
# Dataset ingestion by curators (doc/DESIGN_dataset_ingestion.md).
datasets_upload_groups: [admins]
datasets_upload_max_gb: 50
datasets_upload_part_mb: 64
datasets_upload_url_ttl_minutes: 60
datasets_upload_quota_gb_per_user: 200
datasets_upload_max_open_per_user: 3
datasets_incoming_prefix: _incoming
datasets_incoming_retention_days: 30
datasets_auto_publish: true          # pass -> live with an unreviewed badge
datasets_auto_publish_public: false  # access=public always waits for an admin
datasets_default_access: internal
datasets_post_steps: [score, gene_stats, gene_variance, module_variability, facet_test, facet_test_modules]
datasets_score_collections: []       # genesetstore experiments scored on ingest (empty: the site defaults)
datasets_de_pseudobulk: false
datasets_store_git_commit: true
datasets_store_git_push: false
datasets_worker_local_max_cells: 1500000
datasets_worker_local_max_gb: 60
ingest_state_dir: ''                 # default <state_dir>/ingest; env ANTHIVE_INGEST_STATE_DIR
```

`site.example.yaml` (rendered by `ant-stack` into the three configs):

```yaml
services:
  dataset_upload: false   # curators upload datasets (needs s3 + the incoming key)
  ingest_worker: false    # long-running worker container on this host
datasets:
  upload_groups: [admins, curators]
  max_upload_gb: 50
  quota_gb_per_user: 200
  auto_publish: true
  retention_days: 30
  post_steps: [score, gene_stats, gene_variance, module_variability, facet_test, facet_test_modules]
ingest_worker:
  cpus: 4
  memory: 48g
  io_weight: 100          # blkio weight 10..1000
  nice: 19
  local_max_cells: 1500000
```

`ant-stack` changes (`bin/ops/ant-stack`):
- Render the `ingest-worker` service: ingest image, `command: ant ingest-worker --api http://api:8080`, `cpus`, `mem_limit`, `blkio_config.weight`, `restart: unless-stopped`, volumes `data_rw + scratch + config_mount('ingest') + secrets/ingest_worker_token:ro`.
- Mount the incoming key into the api only when `dataset_upload` is on.
- `ant-stack init` generates `ingest_worker_token`.
- New `_check_datasets(site)`:
  - PROBLEM: `dataset_upload` without `s3.enabled` or without the incoming key files.
  - PROBLEM: `ingest_worker` with `data.readonly`.
  - WARNING: `dataset_upload` without `ingest_worker` (nothing will run the checks unless a remote worker is set up).
  - WARNING: `cpus` at or above the host cores minus 2, or `memory` above half the host RAM.
  - WARNING: `upload_groups` naming a group absent from the Authelia users file.
  - WARNING: `auto_publish_public: true`.
- Add `_check_secret_permissions` coverage for the new secrets.

`doc/DEPLOY_CONTAINERS.md`:
- New "Let curators add datasets" under Everyday tasks.
- Rows in the site.yaml keys table and the "Who sees which secret" table (api: incoming key; ingest-worker: worker token + full S3 keys).
- The diagram gets the worker.
- A "What changed" release row.

`doc/CONFIG.md`: the flat keys. `doc/HPC.md`: running `ant ingest-worker --remote` under Slurm (`docker/slurm-ingest.example.sh` variant).

Rule kept: nothing hard-codes repo paths for runtime writes. Every path comes from config or env (`get_ingest_state_dir`, `get_convert_staging_dir`, scratch).

---

## 13. Build worker: `bin/ingest/ant-ingest-worker`

- Loop: `claim` -> run the job in a subprocess with `nice`/`ionice` -> heartbeat every 30 s -> `finish`. `--once` for tests and Slurm, `--remote` (no local store: writes to S3), and `--kinds`.
- Job `verify`: stream the S3 object -> sha256 -> compare -> size. Then enqueue `profile`.
- Job `profile`: download to `<store>/_incoming/<user>/<sid>/<Group>/` (local) or scratch (remote) -> `ant ingest-check --profile-only`. The api stores the profile. State -> `ready`.
- Job `check`: write the md/mus/index texts into staging -> `ant ingest-check --json --md-summary --store <store>` (scratch convert cached) -> finish with the report.
- Job `build`, local stage 1:
  1. Fill the catalogue fields non-destructively with `lib/ingest_md_fill.py:fill_catalog_fields(md, h5ad)`. It sets sha256, n_obs, layer_stats and obsnum/obscat defaults using `h5ad_utils` and `layer_stats`, only where the curator left them unset. It writes the `ingest:` block.
  2. Final convert, or reuse the cached scratch convert when the h5ad sha and the convert-relevant keys match, then refresh views.
  3. `ant-duckdb-check`.
  4. Atomic rename of the set into `<store>/<Group>/`. New dataset: `POST /admin/rescan`. Existing db_id (P5): `/admin/release` -> swap -> `/admin/restore`.
  5. S3 `CopyObject` from `_incoming/...h5ad` to `<Group>/<basename>.h5ad`, then delete the incoming object.
  6. `git commit` of `.md` / `.mus` / `index.md` in the store checkout, author = curator.
  7. State -> `live` (unreviewed).
- Job `post`, stage 2: for each `datasets_post_steps` entry, wrap writers in release/restore as ant-score does today: `ant-score-fleet run --only <db>` (the `score` step: scanpy `score_genes` via `ant-score-ext` plus UCell via `ant-ucell` for every genesetstore catalog whose cell type scope contains a cell of the dataset, automatic after the conversion; AUCell is not used), `ant-gene-stats-precompute`, `ant-gene-variance-precompute`, `ant-module-variability`, `ant-facet-test <db>`, `ant-facet-test-modules <db>` (results uncounted until an admin adds the dataset to the cohort selection), and optionally `ant-de-pseudobulk`. Then `ant-ingest-index build --only <db>`, `ant-sync push '<Group>/<basename>*'` throttled, and a final `/admin/rescan`. A failure in one step is recorded and the rest continue; it is shown as a warning on the admin list.
- Job `rescore` (genesetstore hook): the worker remembers the genesetstore git HEAD of the last full scoring. When it differs (an experiment was added or a `celltypes` / `role` / gene list changed), `git diff --name-only <old> <new>` names the touched experiments and the worker runs `ant-score-fleet --catalogs <experiment,...> run` over the datasets they apply to (the fleet plan lists them), then `ant-facet-test-modules run --all --force` for the affected datasets, `ant-module-variability --workers 2 --no-prune`, the scores index rebuild, a rescan and a throttled `ant-sync push`. A changed `celltypes` can also remove a catalog from a dataset: run the fleet without `--catalogs` for that case (it deletes stale catalogs). The `scores.modules_complete` check is the audit of this hook.
- Job `retire`: `ant inactive <db_id>` semantics, then rescan, then a git commit.
- Job `pull_rescan` (after a remote build): `ant sync pull --include-duckdb '<Group>/<basename>*'`, then rescan.
- Antomaton vs a simple queue: use the api-owned SQLite queue with a polling worker. It has no new dependency and works for HPC through HTTP, and the job vocabulary is small. Antomaton (codeberg.org/mfiers/antomaton) can later replace the worker as a client of the same `/internal/ingest/jobs` protocol. Its design note was not found in Basic Memory, so it was not evaluated. Open decision D3.

---

## 14. Web UI and admin page

- `web/templates/admin_ingest.html` + `web/browser.py:admin_ingest_view` (pattern of `admin_feedback_view` / `admin_mcp_links_view`, `_is_admin`), route `/admin/ingest`. It shows:
  - recent auto-ingests and held submissions: user, dataset link, state, check summary, build and post-step status, age;
  - actions: Mark reviewed, Send back (comment box, prefilled per failing check), Retire, Approve (held), Reject, Rerun;
  - a full report viewer with `admin_detail`.
- `dataset.html` and the catalogue list get a "new, unreviewed" badge (`unreviewed` from the submission joined through the `ingest.sid` in `.md`), plus a "processing" note during stage 2.
- `web/templates/account_ingest.html` at `/account/ingest`: the curator's own submissions, state, report (redacted) and a withdraw button. A link from the user menu, shown to curators only.
- Help: a `help.html` section "Adding a dataset" pointing to the skill and vark. `api_guide.html` gets the vark datasets commands.

---

## 15. Tests

- `tests/test_ingest_check_layers.py`: synthetic small AnnData. Counts present or absent; X raw, scaled or lognorm; consistency.
- `tests/test_ingest_check_facets.py`: unmapped values, Braak out of range, strict namespace, mapping_data against a tiny duckdb built with ant-convert in tmp.
- `tests/test_ingest_check_donors.py`: template vs join forms with a fixture crosswalk in tmp (never the real one), digest-only overlap, demographic matcher thresholds, redaction (no raw uid in the curator JSON, enforced with a regex scan).
- `tests/test_ingest_check_cells.py`: barcode normalisation, same-cells / subset / disjoint classification.
- `tests/test_ingest_check_no_destructive.py`: greps `lib/ingest_check/` and `bin/ingest/ant-ingest-check` for `h5ad-check`, `ant-check` and `fix=True`; runs the check on a folder and asserts every file mtime and hash is unchanged.
- `tests/test_ingest_store.py`: every legal and illegal state transition, the claim race (two threads), heartbeat requeue, retention.
- `tests/test_ingest_api.py`: FastAPI TestClient as in `test_library_upload.py` / `test_mcp_links.py`. Group gating (curator, admin, other: 403; basic mode), own-only listing, size and quota 413, submit refused on a stale md_hash, held reasons, worker token. S3 is mocked with `moto` (add as a test dependency) for multipart presign, complete and abort.
- `tests/test_ingest_worker.py`: `--once` with a fake api and local store. Build stage 1 moves files and calls rescan; release/restore are called around writers.
- `tests/test_deploy_config.py` / `tests/test_ant_stack_hardening.py`: worker service rendering, cgroup keys, secret mounts (the incoming key only in the api, only when enabled), the new `check` problems and warnings.
- `tests/test_mcp_ingest_tools.py`: visibility of `INGEST_TOOLS` by group and enabled flag.
- `tests/test_ingest_md_fill.py`: never overwrites curator keys.
- Skills repo: a lint in `scripts/build_zips.py` that fails on site URLs, `donor_crosswalk` content patterns, and em-dashes.
- End to end (manual, documented in `doc/CHECK_PROCESS.md`): ingest RX4/ALL into a throwaway store with `bin/ops/ant-stack` on a test site; time each step and update the section 6 table.

---

## 16. Docs

- New: `doc/DESIGN_dataset_ingestion.md` (this file), and `doc/DATASET_INGESTION.md` (operator and curator guide: states, what each check means, mirroring the skill's `reference/checks.md`).
- Updated:
  - `doc/REST_API_SPEC.md` and `doc/API_CHANGELOG.md` (new endpoints)
  - `doc/MCP_CLIENTS.md` (tools)
  - `doc/DEPLOY_CONTAINERS.md`, `doc/CONFIG.md`, `doc/HPC.md`
  - `doc/CHECK_TOOLS.md` (ant ingest-check and a warning that ant-check calls ant-h5ad-check)
  - `doc/H5AD_INGESTION_SPEC.md` (link)
  - `bin/ingest/CLAUDE.md`, `bin/ops/CLAUDE.md`, `lib/CLAUDE.md`
  - the web help page
  - the skills `README.md` table

---

## 17. Phases

| Phase | Content | Size | Depends on |
| --- | --- | --- | --- |
| **P1: ant ingest-check, manual** (done 2026-10-05) | `lib/ingest_check/*`, `bin/ingest/ant-ingest-check`, `bin/ingest/ant-ingest-index`, `lib/facet_rules.py` (shared thresholds), `lib/ingest_md_fill.py`, scratch convert cache, JSON + md report, tests in section 15 (checks, no-destructive). Mark runs `ant ingest-check /path/Group --store /data/1/project/h5adstore --json r.json`. Measure the downstream step times. | L | replicate_unit and disease_basis keys landing (stub the checks as `skip` until then) |
| **P2: skill** | `anthive-dataset-ingest` SKILL.md + references, written against P1 check ids. Usable at first in "prepare files for an admin" mode (curator sends the folder, Mark runs P1). | M | P1 check ids frozen |
| **P3: upload + roles + checks server-side** | config keys, `get_dataset_upload_settings`, `_require_curator`, `lib/ingest_store.py`, `lib/ingest_s3.py`, curator endpoints, `ingest-worker` with `verify`/`profile`/`check`, MCP `INGEST_TOOLS`, ant-stack render + check, incoming key, vark and pyvark commands (spec handed to their owners), `/account/ingest`, DEPLOY docs | L | P1; scoped S3 key from the bucket owner |
| **P4: auto-publish + build + post-hoc admin** | `build`/`post`/`retire`/`pull_rescan` jobs, release/restore integration, git commit, S3 copy and push, `/admin/ingest` page, badges, admin endpoints, held flow, HPC remote worker mode | L | P3 |
| **P5: versions** | Submissions against an existing db_id: semver bump rules (patch for metadata, minor for new layers or annotation, major for different cells), a changelog entry with `previous_sha256`, the merge path for "same cells", release/swap/restore, and version history shown on the dataset page | M | P4 |

---

## 18. Risks

- **Prod host contention:** the cgroup caps bound CPU and memory, but IO on a shared disk can still hurt serving. Mitigations: `ionice -c3`, blkio weight, a `quiet_hours` option for stage 2, the HPC overflow, and monitoring with `/metrics`.
- **anndata memory:** `read_h5ad` of a large file can exceed the cap. The OOM kill stays inside the worker. Size routing goes to HPC; use backed mode where the code allows.
- **Auto-published bad biology:** checks are structural, not semantic. Mitigations: the unreviewed badge, internal-only by default, quick retire, and no counted facet-test cohort entry without an admin.
- **Crosswalk leakage** through reports or the profile: the redaction tests, digest-only indexes, and an admin_detail split.
- **Overlap false positives and negatives:** demographic matching is heuristic, so it holds rather than fails. Barcode normalisation varies by pipeline. Keep thresholds in config and log the outcomes to tune them.
- **ant-check confusion:** someone reuses `ant-check` and the `.md` is rewritten. The guard test and the docs cover this.
- **md_check underscore skip:** checking from above `_incoming` silently checks nothing. Always pass the group folder as the root, and test it.
- **S3 provider** may not support scoped keys or `x-amz-checksum-sha256`. The fallback is worker-side sha256 (always done anyway) and `api_access: write` with a warning.
- **Git conflicts** in the store checkout when Mark edits the same files by hand: the worker commits only its own files and `pull --rebase`s before committing. On conflict the state becomes `build_failed` with a clear message.
- **Two agents adding keys in parallel** (replicate_unit, disease_basis): P1 stubs them; sync the vocabularies before P2.

---

## 19. Decisions (all accepted by Mark, 2026-10-05, with the recommended defaults)

- **D1 Role name:** `curators`.
- **D2 Auto-approval:** a pass is published automatically for `access=internal`. `access=public` **always waits for an admin** (`datasets_auto_publish_public: false`).
- **D3 Build runner:** a simple api-owned queue (SQLite) with a polling worker. Not antomaton. Antomaton can later become a client of the same `/internal/ingest/jobs` protocol.
- **D4 Quotas:** 200 GB per user, 50 GB per upload, 3 open submissions per user, 30 days incoming retention (`datasets_upload_quota_gb_per_user`, `datasets_upload_max_gb`, `datasets_upload_max_open_per_user`, `datasets_incoming_retention_days`).
- **D5 Post steps on ingest:** standard geneset scores, gene stats and facet tests. The `score` step is scanpy `score_genes` plus UCell, per cell type scope (decision 2026-10-08, replaces "ucell / aucell off"; AUCell is not part of the defaults). DE pseudobulk is **off** by default (`datasets_de_pseudobulk: false`).
- **D6 Missing raw counts:** allowed with a stated reason (`counts_unavailable: <reason>` in the `.md`), shown on the dataset page. Without the reason it is an error (check `layers.counts`).
- **D7 Controlled flag source:** both. A new mus field `data_use` (`open` or `controlled`) and the optional `controlled` column of `donor_crosswalk.tsv`.
  **Revised 2026-10-20 (Mark, 2.47.0):** data use agreements and the internal/external split apply to a whole server, with no per-dataset access control by agreement, because everybody on a server may see all its data. So `data_use` (controlled, missing, or a controlled cohort in the crosswalk) only warns the depositor that everyone on the server sees the dataset; it never holds a deposit. The held reasons `controlled_access` and `data_use_unknown` are retired (older held records keep them and are approved as before), and a `data_use` change in an update is plain metadata: a patch version with a changelog line and a caveat. `public_access`, `licence_unknown`, `donor_overlap` and the artefact holds are unchanged.
- **D8 Store git push:** the worker commits locally and Mark pushes (`datasets_store_git_push: false`).
- **D9 Send-back of a live dataset:** the dataset is set inactive immediately.

### Critical Files for Implementation
- /data/1/users/mark/anthive4/bin/api/ant-serve (`_require_uploader` ~14686 as the gating template, `_require_admin_or_local`, `/admin/release` / `/admin/restore` / `/admin/rescan` ~11878-12139)
- /data/1/users/mark/anthive4/lib/md_check.py and /data/1/users/mark/anthive4/lib/facet_lint.py (check reuse; `fix=False`; underscore skip at line 216)
- /data/1/users/mark/anthive4/lib/config.py (`get_library_upload_settings` pattern, `get_state_dir`, `get_convert_staging_dir`)
- /data/1/users/mark/anthive4/bin/ops/ant-stack (site.yaml rendering line ~417, container roles and volumes ~646-652, `_check_s3`, secret mounts)
- /data/1/users/mark/anthive4/data/skills/skills/anthive-paper-upload/SKILL.md (skill structure and refusal conventions)
- Also: /data/1/users/mark/anthive4/bin/api/ant-mcp (`UPLOAD_TOOLS` visibility ~109-127), /data/1/users/mark/anthive4/lib/obs_derive_forms.py, /data/1/users/mark/anthive4/lib/donor_keys.py, /data/1/users/mark/anthive4/bin/annot/ant-facet-test (thresholds lines 93-99), /data/1/users/mark/anthive4/doc/DEPLOY_CONTAINERS.md, /data/1/users/mark/anthive4/vark/internal/cli/papers_upload.go and /data/1/users/mark/pyvark/vark/client.py (`upload_paper` ~2379) for the client patterns.

### Implementation notes
- Three gotchas found while reading the code:
  - `bin/meta/ant-check` calls `ant-h5ad-check` as a subprocess, so it is destructive too and must never be used, not just ant-h5ad-check.
  - `ant-md-check` writes ids unless you pass `--no-fix`.
  - `lib/md_check.py` silently skips folders whose path below the scan root starts with `_`.
- The mus `access` vocabulary only has `internal` and `public`. Controlled access needs a new field (D7).
- The api has no S3 keys by default (`s3.api_access: none`, 2.41.0), so presigning needs the scoped incoming key in section 11.
- I could not find the antomaton design in Basic Memory.
- The downstream build-step times in section 6 are estimates, except convert and facet tests, which are measured. P1 should measure the rest.


---

## 20. P1 as implemented (2026-10-05)

Delivered: `bin/ingest/ant-ingest-check`, `bin/ingest/ant-ingest-index`, `lib/ingest_check/` (34 checks, frozen ids
in `lib/ingest_check/checks_*.py`, listed by `ant ingest-check --list-checks`), `lib/facet_rules.py` (the facet test
thresholds, now imported by `bin/annot/ant-facet-test`), `config.get_ingest_state_dir / get_ingest_index_dir /
get_ingest_scratch_dir / get_dataset_ingest_settings`, `doc/DATASET_INGESTION.md` (what each check means), tests
`tests/test_ingest_*.py`. `replicate_unit` / `replicate_column` / `disease_basis` were already on main
(`lib/facet_lint.py`, `lib/replicates.py`), so `design.replicate_unit` and `design.disease_basis` are real checks, not stubs.

What differs from sections 3 to 8:

- **Index location.** The overlap indexes live in `config.get_ingest_index_dir()` (default `<state_dir>/ingest/index`,
  env `ANTHIVE_INGEST_INDEX_DIR`), not in `<store>/_ingest_index/`. They hold quasi-identifiers; the state dir is
  where site-private data goes, and the store is a git repo with sync tooling next to it. `ant-ingest-index` refuses a
  directory inside the store.
- **Hashes.** BLAKE2b with 8 bytes instead of xxhash64 (no new dependency). Two hashes per cell: strict
  (normalised barcode + donor digest) and loose (barcode alone). The loose one catches the same cells under other donor
  keys (a fallback key contains the group id, so a re-labelled copy of a dataset has different strict hashes).
- **`ant-duckdb-check` is not read-only** (section 0 said it was). Without `--clean-wal` it still opens the database
  read-write to build a missing `__gene_bins` table, may write `default_layer` into the `.md`, and appends to
  `./ant-<date>.log` in the working directory. `ant ingest-check` runs it only on a private copy in the scratch dir
  (reflink copy of the database, copy of the `.md`, symlinks to the parquet files). Its "numerical obs column missing"
  findings for score columns are downgraded to a warning when the `.scores.parquet` sidecar holds them.
- **Scratch convert.** `ant-convert` resolves symlinks, so the scratch copy of the h5ad is a hard link (reflink copy
  across devices), never a symlink. Scratch builds older than 7 days are pruned.
- **Gene universe.** `genes.ids` uses the gene names of the datasets of the same organism in the overlap index
  (`genes.parquet`), not `gene_stats_index.parquet`, which only holds a subset of genes per dataset. Without 3 datasets
  of the organism the share of known genes is not judged.
- **Demographic overlap.** Besides the rule of section 8.3 (sex, age within a year, PMI within an hour, Braak, APOE; 90+
  treated as equal), a new donor counts as matched when it has exactly one candidate or exactly one *close* candidate
  (distance under 0.25 tolerances). The chance level of each cohort is estimated by repeating the match with the new
  ages shifted by 6, 9 and 12 years either way. `donor_overlap` is held when at least 50% of the donors match uniquely
  and at least 20% beyond chance; 20% beyond chance, or 15 donors, is a warning. On GF4/MIC against M23 (247 shared
  persons by id out of 449) the match is 62% with a chance level of 32%. It cannot replace the id based check.
- **Held reasons.** Added `data_use_unknown`: a dataset without `data_use` is held, not passed (D7: an undeclared
  consent status is exactly what an admin should look at). Retired 2026-10-20 with `controlled_access` (D7 revised):
  both are warnings now.
- **Description.** `meta.required_keys` accepts the `description` key or the markdown body of the `.md`, and only warns
  for datasets already in the store (none of them has a `description` key).
- **`disease_basis`** is required only when `ant_disease` is published. **`layers.consistency`** is skipped when the
  `.md` declares `normalisation:`; bulk data whose X is another normalisation (vst) must declare it.
- **Existing datasets.** A folder that is a group in the store runs in `existing` mode: `version.initial` is skipped, a stale
  `sha256` in the `.md` and a short description are warnings. Overlap checks leave out the dataset itself, and donor id
  overlap ignores the other datasets of the same group.
- **`lib/ingest_md_fill.py`** is not part of P1. It is only used by the worker, so it moves to P4 with its test.

Validation on five existing datasets (`taskset -c 4-11 nice -n 19 ionice -c3`, overlap index of 152 datasets; 7 to 68 s
each, peak 0.3 to 7.7 GB resident). All run in `existing` mode against the live store, which is why most findings are
warnings about missing optional fields (`license`, `data_use`, `description`):

| dataset | kind | errors | what it shows |
| --- | --- | --- | --- |
| GF4/MIC | ROSMAP microglia | 1 | `donor.overlap_ids` fails: 255 persons shared with 9 datasets (M23/AST, IMM, VAS: 247 to 248; F26/*: 222 to 244; L26/PSY 3). Demographic match 62% against a chance level of 32%: held as `donor_overlap` |
| L26/PSY | Lee2026 PsychAD | 1 | `donor.overlap_ids` note: 8 persons in 7 datasets of M23, F26 and GF4, declared through `facet_donor_exclusions`; 39 donors beyond chance by demographics. The one error is `design.disease_basis` (`braak_cerad` without `disease_derivation`) |
| AZ5/SLM | xenograft | 0 | animal replicate unit, donor checks skipped, `access=public` held, stale `sha256` in the `.md`, no `ant_donor` / `ant_sex` / `ant_disease` |
| H26/EBP | bulk | 1 | `layers.consistency` fails (r 0.81: X is not log1p CP10k of the counts and the `.md` does not declare a `normalisation`) |
| G24/MMG | clean primary | 0 | warnings only: deprecated `ant_region`, stale `sha256`, short description |

---

## 21. S3 watch route (2026-10-14)

A second way in, built before the presigned-upload route of sections 3 to 7 (P3, not built). The curator
deposits with `mus s3 upload` exactly as for `ant-ingest`; a long-running watcher on the dev box finds the
upload, checks it and publishes it. Decisions by Mark, 2026-10-14:

- S3 `__incoming/` is the source. A watcher polls it every few minutes; complete uploads are pulled,
  ingested and pushed back to the catalog on S3.
- Publication is automatic unless there are errors. Held results of `ant-ingest-check` (`donor_overlap`,
  `licence_unknown`, `public_access`; until 2.47.0 also `data_use_unknown` and `controlled_access`, see D7)
  block like errors and are recorded for a person. Warnings do not block. The watch route holds `access=public` itself, whatever
  `datasets_auto_publish_public` says.
- After a verified publication the watcher deletes the incoming objects that item consumed, and only those
  (21.6). `ant-ingest prune` stays for leftovers.
- An upload for a dataset that is already published is a new version, never a silent replacement. The previous
  version is kept until someone purges it.
- It runs as its own process (systemd unit or `make ingest-watch`), never as a child of the serving stack. A
  compose service comes later; everything already resolves through `lib/config.py`.

An independent review on the same day found a non-atomic update swap, path traversal through S3 keys, and
deletes of files that had not been placed; 21.9 lists what changed.

### 21.1 Pieces

| Piece | Role |
| --- | --- |
| `bin/ingest/ant-ingest-watch` | the loop (`--once`, `status`, `approve`, `reject`), lock, low priority, SIGTERM handling, refusal of runtime dirs inside the checkout, CLI overrides (`--store`, `--prefix`, `--catalog-prefix`, `--api-url`, `--post-steps`, `--no-delete`, `--no-git`) |
| `lib/ingest_watch.py` | state database, settle logic, the poll (`Watcher`), sibling-aware deletes after verification, retries of unverified items, work-dir TTL, approve / reject, status file, notifications |
| `lib/ingest_pipeline.py` | one item from S3 to the served store (`Pipeline`), shared with `ant-ingest`: key validation, `SwapJournal` and `recover_swaps`, `delete_consumed` (the only S3 delete of the route) |
| `lib/ingest_md_fill.py` | h5py-only profile, non-destructive `.md` fill, index.md from the sidecar, store-wins sidecar merge, seed `.md` delta, version plan and minimal `.md` edits for updates, depositor validation |
| `lib/ingest_versions.py` | archive plan (hard links), S3 copy of the replaced version, list versions, plan and run a purge |
| `lib/md_edit.py` | `bump_version(part='minor', previous_sha256=...)` and `append_top` |
| `config.get_ingest_watch_settings`, `config.get_ingest_work_dir` | the `ingest_watch:` block and the work dir, env > anthive.yaml > default |
| `config/examples/anthive-ingest-watch.service.example` | systemd unit (Nice 19, idle IO class, CPU and memory caps, runtime dirs outside the checkout, `TimeoutStopSec=600`) |

### 21.2 What counts as an item, and when it is complete

The keys `mus s3 upload` writes (mus `internal/cli/s3_upload.go`) are
`__incoming/<data_project>/[<subfolder>/]<basename>.h5ad`, the collapsed sidecar `<basename>.h5ad.mus` beside it
(with `sha256=`), `__incoming/<data_project>/static/<file>` and `__incoming/<data_project>/de/<folder>/<file>`
with a `.mus` per DE table. Every segment of every key must match `^[A-Za-z0-9][A-Za-z0-9._-]*$` (no `..`, no
leading `.` or `_`, ASCII only, NFC); any other key is reported as malformed and never pulled, placed or
deleted. Every write into the store is additionally checked to stay inside `<store>/<data_project>` (DE sources
inside `<group>/de_sources`).

One item is one h5ad with its sidecar. It uses the static files of its project and the DE tables whose sidecar
names it (`de_dataset_id`, else `de_dataset_h5ad`). Catalogue seeds travel as `--static` files:
`static/<basename>.md` (the dataset `.md`), and for a new group `static/<basename>.index.md` or, when no other
item of the project has its own, `static/index.md`.

An item is processed when its h5ad and sidecar exist, two consecutive listings showed the same ETag and size for
every object it may consume (the project's static, seed and DE keys included), and its newest object is at least
`settle_seconds` (default 120) old. The first listing of an item never starts processing. One item at a time,
oldest first, under nice 19 and the idle IO class.

### 21.3 States

```
seen -> stable -> pulled -> checked -> live | held | failed | needs_decision | rejected
held --(ant-ingest-watch approve --by NAME)--> stable -> ... -> live
held | failed | needs_decision --(ant-ingest-watch reject --by NAME)--> rejected
needs_decision | failed --(ant-ingest-watch retry --by NAME)--> stable -> ... (after the cause was fixed)
```

One row per h5ad key in `<ingest_state_dir>/ingest_watch.sqlite` (tables `items`, `events`, `deletions`,
`versions`). A row in a final state is processed again only when the ETag or size of its h5ad or sidecar
changes (an approval is void then). Transient problems leave the item `stable` with a retry after 2, 4, 8 ...
minutes (at most an hour; after 8 attempts `failed`): S3 errors, an object that changed during the pull, too
little free space, a failed release, a failed S3 copy of the old version, a killed or timed-out child (exit
below 0, 124, 127, 137, 143, or no report after a signal), a stop request.

`needs_decision` is for states the route cannot reconcile: an h5ad in the store without its `.md` or the other
way round, `_versions/<basename>/` without a current `.md`, staged files without a journal, a catalog h5ad on S3
for a dataset that does not exist locally, a static file that differs from the store copy, a change of
`access` or `license` in an update (a `data_use` change is applied since 2.47.0, D7), an update marker that does not match (21.5), and a store `.md`
that `md_edit` cannot change with a verified minimal edit.

### 21.4 The pipeline of one item

| Step | Blocks publication when it fails | Notes |
| --- | --- | --- |
| store state | `needs_decision` | 21.3, before any download |
| pull | retry later | GETs pinned to the listed ETag (HEAD before and after); kept in `<work>/_pull/` with a manifest, so a retry with unchanged ETags does not download again (and a large download resumes); free space at least 2x the h5ad + `min_free_gb` |
| sha256 | `failed` | the sidecar must declare `sha256=` and it must match; `data_project=` must match the folder |
| catalogue `.md` | yes | new: seed + `fill_catalog_fields`, `access=internal` with a caveat when the sidecar has none; update: 21.5 |
| `ant-convert` | `failed` (retry when killed) | in the work dir; skipped for a metadata-only update |
| `ant-ingest-check` | errors -> `failed`, held reasons -> `held` (retry when killed) | public data is always held; an approval lets the held reasons it names through |
| placement | | through the swap journal (21.5); a new dataset is staged the same way |
| rescan + served | no (warning) | `POST /admin/rescan` with backoff right after the restore (a rescan retires every pooled instance, so nothing opened during the swap survives), then `GET /databases/{db_id}/info` must report the new sha256 and version |
| catalog push | no (warning, retried next poll) | h5ad (metadata `sha256`), sidecar (`s3_*` always re-stamped from the catalog object; the deposit's own `s3_*` keys are dropped), duckdb, layer parquets, DE sidecar; verified by HEAD: size plus the `sha256` metadata, or for a legacy object without it an ETag recomputed from the store file (single-part MD5, or a multipart ETag for parts of 5 MiB (AWS Go SDK, rclone, mus), 8 MiB (boto3), 16, 64 or 256 MiB, each enlarged the way boto3 does for more than 10,000 parts (doubled until they fit), plus `ceil(size / 10000)` rounded up to a MiB). Only a real match counts, and only then is the metadata backfilled (a failed backfill, e.g. over 5 GB, keeps the verdict). A retry first checks that the store h5ad still has the published sha256, else `needs_decision` |
| post steps | no (warning) | as before; skipped after a stop request; `post_step_timeout_seconds` |
| git commit | no (warning) | only the `.md`, `.h5ad.mus` and `index.md` the route itself wrote, and only while each still has the sha256 the route wrote (a file edited meanwhile: no commit, a warning); skipped when the curator has staged changes of those paths, so the index is never touched; `git commit --only`, a failed commit resets just those paths; an `unchanged` deposit commits nothing; author from a validated `depositor` / `depositor_email`, else the service identity (an invalid depositor is recorded in the changelog); `store_git_push` pushes only when the branch has no other unpushed commit |
| work dir | | removed after live; held, failed and needs_decision work dirs are kept `work_ttl_days` (14) |
| delete incoming | | 21.6 |

### 21.5 Updates, versions and the swap journal

An upload for `<group>/<basename>` that already has an h5ad and a `.md` in the store goes through the same
checks (`--mode existing`). A sidecar may name the dataset it updates: `update_of=<db_id>` or
`previous_sha256=<sha256 of the published h5ad>`. A mismatch is `needs_decision`; with
`ingest_watch.require_update_marker: true` an update without either is `needs_decision` too (protection
against two depositors choosing the same `<group>/<basename>`; off by default, as Mark decided that updates
publish as new versions). The trade-off: with the marker off, a second lab that happens to deposit
`<group>/<basename>` replaces the first lab's dataset by a new version without anyone being asked (the old
version stays in `_versions/` and on S3, and the checks still run); with it on, every update needs the extra
sidecar line. Data updates of a dataset with `access=public` are always held for an admin. Clean, the upload
becomes a new version:

- the h5ad sha256 differs: minor bump, changelog `{version, date, previous_sha256, changes}`;
- same h5ad, other sidecar keys or new caveats from the deposited `.md` seed: patch bump;
- nothing changed: `live` with kind `unchanged` (new static files are still placed).

The sidecar merge protects curated values: a key in both keeps the store value unless the deposit also says
`override_<key>=true` (the conflict is recorded in the changelog); new keys are adopted; `access`
and `license` changes stop the item for a person. A `data_use` change follows the deposit (patch version,
changelog line, caveat with topic `data_use`; D7 revised 2026-10-20). A deposited `.md` seed adds its new caveats (by message). Since 2.47.0 (Mark, 2026-10-20: the update itself
is the explicit act) the depositor-editable keys (`ingest_md_fill.DEPOSITOR_EDITABLE_KEYS`) are merged into the
store `.md` with one changelog line each and a `seed_md_update` caveat; `api` / include-list changes rebuild the
views (a patch that converts, `Outcome.rebuilt`). The seed is consumed only when no curated key differs;
otherwise it stays in incoming with a warning.

The `.md` edit is minimal and verified (`md_edit`); caveats, curation fields, the `api:` mapping and the body
stay byte for byte. A `.md` without `version` is treated as 0.0.0 (archived as 0.0.0, bumped from there).

Placement order for an update:
1. the S3 copy of the current catalog objects to `<group>/_versions/<basename>/<old>/` (a failure stops here,
   nothing changed);
2. under the per-dataset writer flock, a journal in `<ingest_state_dir>/swaps/`: hard links of the current
   source files into `_versions/<basename>/<old>/` (nothing moves), the new files and new static files staged
   as hidden siblings (`.<name>.ingest-new`, a cross-device copy as `.<name>.ingest-partial`, both tracked),
   the files to remove afterwards (old scores, stale layer parquets);
3. `POST /admin/release/<db_id>` for a data update, only now (a failure rolls the journal back and retries);
4. commit: journal phase `committing`, renames over the targets with the `.md` last, removals, journal deleted;
5. `POST /admin/restore/<db_id>`, then at once the rescan and the served check.

`recover_swaps` runs before every poll (and before `ant-ingest`): a journal still `staged` is rolled back
(staged and partial files and links removed, the published version untouched), one `committing` is rolled
forward and a dataset the swap had released is restored in ant-serve.

Every commit (also one finished by recovery) writes a resume marker `swaps/resume/<item>.json` before the
journal goes away. It records whether the catalog push happened and which post steps (score, gene stats,
module variability, facet tests, index, manifest, DE import, derived push, store commit) are done; each step
updates it when it ends, and the marker is removed only after the last one. A stop request during the post
steps (`systemctl restart` is a SIGTERM to the whole unit: the running tool dies and the watcher sees the stop)
leaves the step undone and returns the item to `stable`; a SIGKILL leaves the marker as it was. The incoming
objects are not deleted while a marker exists. The next pass that touches the item, whatever it is (the same
deposit, now `unchanged`, or a newer upload that becomes a patch or minor version), first finishes the
pending publication: served check, push if it had not happened, the remaining post steps, its versions rows.
A marker whose version is no longer the store's (someone changed the dataset by hand) is removed with a
warning. A post step that fails on its own (not a stop) is a warning; with
`ingest_watch.delete_despite_post_warnings: true` (default) the delete still happens and the acceptance is
recorded as an event; with `false` the incoming objects stay. A stop
request is honoured between steps and never after staging has started a commit; an I/O error while staging
rolls back and retries later.

`ant-ingest purge <db_id> [--keep N]` and `ant-ingest purge --all --keep N` remove archived versions locally and
on S3, with `--dry-run`, a typed confirmation (or `--yes`) and `--local-only`; on S3 only keys under the
version's own `_versions/<basename>/<version>/` prefix, one at a time. The watcher never purges.

### 21.6 Deletes

`delete_incoming_after_ingest` (default true). Only after the item is `live`, served with the new sha256 and
version, its catalog h5ad verified and the store copy present. Only the keys that were actually placed or
applied count as consumed: the h5ad and sidecar, static files that were placed or are identical in the store,
seeds that were used, DE tables that were ingested. Static files of the project (and `static/index.md`) are
shared: they stay while any other item of the project is still in incoming and not live (held, failed,
needs_decision, waiting); the last item to go live deletes them. Before each delete the key is HEAD-compared with the pulled ETag; a changed ETag means a newer upload and the
key stays. `IfMatch` is sent on top of that only when the backend is known to enforce it: at the first delete
a run makes, the watcher writes a scratch object `<prefix>_selftest/<random>` twice, tries to delete it with
the first ETag and looks whether it survived (`enforced`, `ignored`, `unsupported:<code>`); the result is
logged, recorded as an event and cached for a week in `<ingest_state_dir>/ifmatch_selftest.json`. Without
`IfMatch` there is a window of milliseconds between the HEAD and the delete in which a new upload of the same
key would be deleted; the depositor's next upload then simply arrives again (mus re-uploads the whole item).
On the lab bucket the self-test of 2026-10-14 returned `ignored`: the object store accepted the header and
deleted the object anyway, so there the HEAD comparison is the only protection. An S3 error on one key is recorded as `skipped:error:<code>`, never stops the
other keys or the poll, and the next poll retries the remaining keys (a live row whose sidecar is already gone
but whose h5ad is still listed is recognised as the same deposit, not a new upload). Prefix-wide and batch
deletes do not exist in the route; every key is logged with its ETag, outcome and time in `deletions`. When the rescan, the served check or
the catalog push failed, the next poll retries them (one rescan per poll) and deletes afterwards.

### 21.6a Trust model of approve and reject

`ant-ingest-watch approve` and `reject` run on the watcher's host as a user who can write its state dir, and
only on a primary site (`require_primary`). Anyone with that access is trusted as an admin of the route: the
`--by` name is what they type, and the OS user is recorded next to it in the event log and the caveat. The
approval is bound to the exact deposit (ETag and size of the h5ad, sidecar, seeds, static and DE files); any
change voids it. Since 2.46.0 an admin can also approve or reject a deposit made through the API
(`POST /datasets/deposit/{id}/approve|reject`, section 22.6); the approval then records the API login
(`channel: api`) instead of the OS user.

### 21.7 How it relates to the presigned-upload route

Both end in the same store and use the same checks, the same `.md` fill and the same post steps. The watch
route trusts whoever can write to `__incoming/` on S3 (lab members with mus and the S3 keys); the presigned
route (P3) is for curators without S3 keys and adds the submission database, roles and the admin page. When
P3 is built, its worker should call `lib/ingest_pipeline.Pipeline` for the build, and `__incoming/` can stay as
the lab's own route. `ant-ingest` and the watcher share one lock (`ingest_watch.lock`), so they never run at the
same time.

### 21.8 Not done yet

- The compose service (`site.yaml` key, `ant-stack` rendering and checks); `doc/DEPLOY_CONTAINERS.md` says so.
- DE-only deposits (a DE table for a dataset that is already published, without an h5ad) still go through
  `ant-ingest`; the watcher only ingests DE tables that come with their dataset.
- A deposit under a subfolder (`<group>/<sub>/x.h5ad`) is checked in a flat staging folder, so a `.env` at the
  subfolder level is not part of the check.
- No size routing to an HPC worker, no admin page (status, the JSON file, notifications, approve and reject
  are the surface).

### 21.9 Review of 2026-10-14

| Finding | Change |
| --- | --- |
| C1 the update moved the old files before the new ones were in place; a crash left no `.md` and the retry took the new-dataset path | swap journal with hard links, hidden staging, `.md` renamed last, recovery before each poll; `_versions` without a current `.md` is `needs_decision`; stop requests honoured between steps; systemd `TimeoutStopSec=600` |
| C2 `..` in S3 keys escaped the group folder | key segment validation in `plan_items`; `ensure_inside` on every store write |
| S1, S2 static files and seeds were deleted although unused, also while a sibling still needed them | consumption only of placed or applied files; shared files kept while a sibling is pending; a differing static file is `needs_decision` |
| S3 updates injected `access=internal` and the deposit overwrote curated keys | `access` default only for new datasets; store-wins merge with `override_<key>=true`; access and license changes need a person (data_use since 2.47.0: applied with a caveat) |
| S4 a killed child marked the dataset failed | transient exit codes retry |
| S5 `ant-ingest` could run beside the watcher | shared lock |
| S6 "served" only checked the id | `/info` must show the new sha256 and version |
| S7 a failed S3 copy of the old version was a warning | it runs first and stops the item |
| S8 name collisions | `update_of` / `previous_sha256`, `require_update_marker` |
| S9 release failure swapped anyway | retry, no swap |
| S10 work dirs, retries, disk space | removed after live, TTL for the rest, pulls reused, free-space check |
| S11 legacy catalog objects without `sha256` metadata never verified | MD5 ETag or size + local sha256, metadata backfilled |
| C-a to C-j, nits | two identical listings, public always held, `IfMatch`, prune skips `_versions/` and checks ETags, one rescan per poll, catalog object without local dataset, runtime dirs outside the checkout, unstage on a failed commit, approve and reject, 0.0.0, case-insensitive ids, depositor validation |

Second review (same day):

| Finding | Change |
| --- | --- |
| N1 a swap finished by recovery was reprocessed as `unchanged`: no push, post steps, DE, manifest or versions row | resume marker from recovery; the pipeline finishes the publication when the item comes back |
| N2 `IfMatch` was trusted although a backend may ignore it | HEAD comparison before every delete; `IfMatch` only on top, after a self-test on a scratch key |
| N3 a multipart legacy object was accepted on size and its sha256 backfilled unverified | recomputed multipart ETags for common part sizes; backfill only after a match |
| N4 a retried push could publish a changed store file | the store h5ad must still have the published sha256, else `needs_decision` |
| N5 the deposit's `s3_*` stamps could end up in the store sidecar | dropped; always re-stamped from the catalog object |
| N6 the dataset was released while staging ran | staging first, release just before the commit, rescan right after the restore |
| N7 an approval survived changes of seeds or static files | bound to the full settle signature |
| N8 approve / reject | primary sites only; OS user recorded; trust model in 21.6a |
| N9 commits of files the route did not write, or edited meanwhile; the curator's staged changes | commit only the route's own files with the sha256 it wrote; leave staged changes alone |
| N10 | public data updates are held: documented |
| N11 partial copies were not tracked | in the journal, removed by rollback and recovery, matched by the hidden-file check |
| N12 uv, download and network failures of a tool counted as dataset errors | recognised in the output, retried |
| nits | hidden-file check on the exact basename; a conflicting seed `.md` applies nothing |

Third review (same day):

| Finding | Change |
| --- | --- |
| a stop or kill during the post steps lost them, and the incoming objects were deleted anyway | resume marker written at commit, updated per post step, removed only at the end; a stop in the post steps retries the item; no delete while a marker exists; `delete_despite_post_warnings` for steps that fail on their own |
| a delete error could stop the poll | per key `skipped:error:<code>`, retried next poll |
| multipart ETags of 5 MiB parts and of large objects were not recognised | 5 MiB added; boto3's doubling and `ceil(size / 10000)` part sizes |
| a pending marker was ignored when the next pass was an update | the next pass finishes the pending publication first; stale markers removed with a warning |

---

## 22. Deposit API (2026-10-15, REST 2.46.0, phase 1: the server)

Mark's decision of 2026-10-15: dataset preparation and upload move out of mus into vark; depositors use their
Anthive login and never hold S3 keys; the server presigns uploads into `__incoming/` and serves the deposit
status; the watcher of section 21 stays the backend. Phase 1 is the server side only (this section); vark and
the skills come next. It answers the review of the same day (no route for a depositor, the catalogue's
bucket-wide key handed to depositors, no status a depositor can reach, self-declared identity).

### 22.1 Pieces

| Piece | Role |
| --- | --- |
| `lib/dataset_deposit.py` | `DepositStore` (SQLite `<ingest_state_dir>/deposits.sqlite`: `deposits`, `files`, `events`), `DepositService` (the rules behind the routes, raises `DepositError(status, body)`), redaction (`sanitize_text`, `redacted_report`, `report_summary`), `public_state` / `watch_row_matches` |
| `bin/api/ant-serve` | thin routes under `/datasets/deposit` (tag `datasets-deposit`, hidden from the public schema like the paper upload), `_require_dataset_uploader`, `_deposit_service` (the presigning boto3 client, SigV4) |
| `lib/ingest_watch.py` | `Watcher.link_deposits` (attach a deposit record to its item, hold back projects with an open deposit), `Watcher.sync_deposit` (outcome into the record, redacted report copy, depositor e-mail), `notify_depositor`, `approve` / `reject` with `channel='api'` |
| `lib/ingest_pipeline.py` | `Item.deposit`; identity and `update_of` from the record; the deposit id is part of `Item.content_signature` |
| `bin/api/ant-mcp` | `my_deposits`, `deposit_status` (read-only, `DEPOSIT_TOOLS`, depositors only) |
| `lib/config.py` | `get_dataset_deposit_settings` (`datasets_upload_groups`, `datasets_deposit_*`) |
| `bin/ops/ant-stack` | `services.dataset_deposit`, `datasets:` block, the presigning key pair, `_check_datasets` |

### 22.2 Flow

1. `GET /datasets/deposit/vocab`: what a deposit needs (file kinds, names, limits, the mus vocabulary from
   `lib/_mus_vocab.yaml`, the sidecar keys the route reads, the seed `.md` and `index.md` rules with
   templates, held reasons, states).
2. `POST /datasets/deposit` with `{data_project, basename, files: [{name, size, sha256, kind}], update_of?}`.
   The server checks names, the group rule, sizes and the update rules, then the rate limit (before any call
   to the object store), then in one SQLite transaction (`BEGIN IMMEDIATE`) the per-user limits, the record
   and a lock on every target key (`open_keys`, primary key = the key). Each file has a target key in the
   watched prefix, the layout `mus s3 upload` writes (`<prefix><data_project>/<basename>.h5ad` and `.h5ad.mus`,
   `.../static/<name>` for the seed `.md`, `index.md` and other static files, `.../de/<folder>/<file>`), and an
   upload key in the deposit's own staging folder, `<staging_prefix><deposit_id>/<the same path>`
   (`datasets_deposit_staging_prefix`, default `__deposits/`, never inside the watched prefix). The presigned
   URLs point at the upload keys only.
3. The client uploads. Files up to `datasets_deposit_single_put_max_mb` (64) are one `PutObject`, larger ones
   multipart (part size at least 64 MiB, whole MiB, at most about 9,500 parts). Every URL signs the exact
   `Content-Length`; a single PUT also signs `x-amz-meta-sha256` and `x-amz-meta-deposit`; multipart objects
   get the same metadata from `CreateMultipartUpload` on the server. `x-amz-meta-sha256` is the depositor's
   attestation only: the pipeline recomputes the hash. A different size, a missing header or another key is a
   403 from S3 (checked on the lab bucket). No URL lives longer than the deposit's window.
   `POST /datasets/deposit/{id}/urls` refreshes URLs of one file and lists the parts S3 already has (resume;
   at most `max_refreshes` per deposit).
4. `POST /datasets/deposit/{id}/complete` moves the deposit to `completing` (a compare-and-set; withdraw and
   expiry wait), lists the parts (no ETags from the client), checks part numbers and sizes, completes the
   uploads in staging, HEADs every staging object (size, `sha256` and `deposit` metadata), reads the sidecar
   back (at most 1 MB: `sha256=` must be the deposited h5ad's, or for a corrected sidecar the waiting h5ad's
   recorded hash; `data_project=` the deposit's), then copies every file to its target key (`CopyObject`,
   managed `UploadPartCopy` above 5 GB) with the deposit metadata, records the ETags of the copies, deletes
   the staging objects, releases the key locks and sets `uploaded`. A refusal at any step deletes the copies
   already made and returns the deposit to `uploading`.
5. The watcher lists the objects, links them to the deposit (22.4) and runs the pipeline of section 21.
6. `GET /datasets/deposit[/{id}]` and `/report` follow it; `withdraw`, `approve`, `reject`, `retry`, `expire` act
   on it. `retry` (admins) runs a `needs_decision` or `failed` deposit again; `reject` is final and deletes the
   incoming objects of the deposit and of the deposits it continues (guarded: `x-amz-meta-deposit` and the
   completed ETag), withdraws open corrections, and a create never continues a rejected deposit (409
   `rejected` if an object of it is left). A reject on the host is recorded in the deposit store and the next
   poll does the same cleanup (`DepositService.pending_rejected` / `clean_rejected`). `/report` of a deposit
   that stopped before the checks answers 404 `no_report` with `state` and `reason`.

### 22.3 Rules

| Rule | Answer |
| --- | --- |
| Caller not signed in, not in `datasets_upload_groups`, or `auth.mode: none` | 403 `forbidden` on every route (also `vocab`); `X-Remote-*` is never read. Admin routes (approve, reject, expire, owners, `?all=true`) also let in admins outside the upload groups |
| Another user's deposit id, a malformed id | 404 `not_found` (admins see every deposit) |
| `data_project`: a valid segment; an existing group (a folder with `index.md`) spelled exactly, or a new group named like `Smith2024` (`^[A-Z][A-Za-z0-9_]*\d{4}$`, mus' rule); a folder without `index.md` is not available; a case-insensitive clash is refused without naming the existing group (admins see it) | 422 `invalid_data_project` |
| `basename`: `[A-Za-z0-9._-]`, at most 50 characters, not `index` and not `*.index` (group seeds) | 422 `invalid_name` |
| File names: one segment of `[A-Za-z0-9._-]` not starting with `.` or `_` (DE: `<folder>/<file>`), the fixed names per kind, no seed names as static files | 422 `invalid_name` |
| One `mus` always; one h5ad unless a corrected sidecar is deposited for an h5ad of the same user still in incoming (`continues`) | 422 `invalid`, `h5ad_required` |
| Sizes: the deposit at most `datasets_deposit_max_gb` (200, equal to `datasets_upload_max_gb` of the check `files.size`); text kinds at most 1 MB | 413 `too_large` |
| A dataset that is in the store and the caller may not update it (`update_policy: own`: only its owner, see 22.6, or an admin): one answer whether or not `update_of` was given, without the dataset id | 409 `name_taken` |
| A dataset the caller may update: `update_of` must name it | 409 `update_of_required`, `update_of_mismatch`; `update_of` for a dataset that does not exist is 422 `update_of_unknown` |
| A key locked by an open deposit (for `index.md` the answer says to use `<basename>.index.md`); a key in incoming written by another user's deposit or by a lab member's upload | 409 `key_in_open_deposit`, `key_taken` (admins may replace) |
| Open deposits per user (`max_open_per_user`, 5); deposits started per user in 24 hours (`max_per_day`, 20); bytes waiting per user (`max_pending_gb_per_user`, 400); presign calls per minute (`presign_per_minute`, 30); URL refreshes per deposit (`max_refreshes`, 500) | 429 `too_many_open_deposits`, `too_many_deposits_today`, 413 `quota_exceeded`, 429 `rate_limited` with `Retry-After`, 429 `too_many_refreshes` |
| Deposits off (`datasets_deposit_enabled` false or no S3 key pair) | 503 `disabled` for create and the upload routes; `vocab` says `uploads_enabled: false` |
| URLs live `url_ttl_hours` (24) and never past the deposit's window; an open deposit expires that long after its last refresh, at the latest `max_open_days` (7) after it was created; an expired deposit's uploads are aborted and its staging folder emptied (by the next API call or the watcher's next poll) | 410 `expired` |

### 22.4 Watcher integration

- **Link.** An item belongs to a deposit when its h5ad and sidecar keys and ETags are exactly the copies the
  deposit completed (`DepositStore.find_for_item`, newest first).
- **Hold back.** Items whose h5ad or sidecar key is locked by a deposit that is `uploading` or `completing`
  wait; nothing else waits (an open deposit no longer freezes its project). Uploads never reach the watched
  prefix before complete, so only the seconds of the copy matter.
- **Static and DE files belong to their deposit.** A linked item uses only the static and DE objects its own
  deposit completed (and those of the deposits a corrected sidecar continues); an unlinked item never uses an
  object a waiting deposit completed. The seeds (`<basename>.md`, `index.md`, `<basename>.index.md`) follow
  the same rule.
- **Refusals.** An unlinked item whose h5ad or sidecar carries `x-amz-meta-deposit` is `needs_decision`: only
  the server writes that mark, so it is a replayed or left-over object. With `ingest_watch.accept_unlinked:
  false` every unlinked item is `needs_decision`; it is `true` for now because lab members still deposit with
  `mus s3 upload`, and becomes `false` once vark ships (Mark, 2026-10-15: preparation and upload move to
  vark).
- **Fail closed.** A deposit store that exists but cannot be opened or read stops the poll (TransientError,
  backoff); nothing is processed without the deposit information.
- **Housekeeping.** Each poll expires stale deposits (and interrupted completes, `completing` for 12 hours),
  removes staging objects of deposits that are not open (a late PUT to an old URL lands there and is swept),
  and writes pending owner caveats (22.6).
- **Identity.** For a linked item the depositor is the deposit's login (Remote-User, with Remote-Name and
  Remote-Email when the proxy sent them): the `.md` `ingest:` block gets `depositor` and `deposit: {id,
  user}`, the store sidecar gets `depositor=` / `depositor_email=` from the record (deposited lines are
  dropped, also on updates), the store commit has that author, and a `depositor` stated in the sidecar is
  only mentioned in the changelog.
- **Updates.** The record's `update_of` is the update marker. A linked deposit without it for a dataset that
  exists by the time the watcher runs is `needs_decision`.
- **Same bytes again.** The deposit id is part of the item's content signature, so a new deposit of
  byte-identical files (the usual fix of a failed check: a corrected seed with the same sidecar) is processed,
  and after a dataset went live a new deposit of the same bytes is processed as `unchanged` instead of
  staying in incoming.
- **Outcome.** After each final state the watcher copies state, db id, version, kind, held reasons and a
  path-free error into the record, writes the redacted report to
  `<ingest_state_dir>/deposit_reports/<id>.json` and e-mails the depositor on `live`, `held` and `failed`
  (`ingest_watch.notify` SMTP settings, once per outcome).

The API shows the watcher's row while it is about this deposit (`watch_row_matches`: ETags, and the deposit id
in the row's signature), so `seen`, `stable` and `checking` (`pulled`, `checked`) are live; otherwise the
copy in the record. A deposit whose objects were replaced before the watcher saw them is `superseded`.

### 22.5 Redaction

What a depositor sees is the curator view: `admin_detail` (other datasets' ids, cohort names, demographic
match tables) is dropped as in `ant ingest-check --audience curator`, and every string loses its server
paths (the store, state, work and tmp roots become `<store>`, `<state>`, ...; any other absolute path
`<path>/<file name>`), `s3://` addresses and the bucket name. Admin actions show as "an admin" to the
depositor. Donor identifiers and crosswalk content never reach the report in the first place (section 8.2).
The e-mail carries only the state, the dataset id and version, held reasons and the failed checks with their
fix.

### 22.6 Withdraw, expire, approve, reject, owners

- `withdraw` (the depositor or an admin): a compare-and-set to `withdrawing` (refused with 409 `busy` while a
  complete runs), then aborts the deposit's multipart uploads, empties its staging folder and deletes its own
  incoming copies through `delete_consumed` (HEAD-compared, one key at a time), but only objects whose
  `x-amz-meta-deposit` is this deposit and whose ETag is the completed one; anything else is reported as
  kept. Refused while the watcher processes the deposit (409 `busy`), once it is live (409 `live`) and while a
  corrected sidecar deposit uses its h5ad (409 `in_use`, naming that deposit).
- `expire` (admins): ends an open deposit at once, as the expiry does.
- `approve` and `reject` (admins): the watcher's own `approve` / `reject` on the watcher's row of this
  deposit, with `channel='api'` and the Remote-User as `by`. They need the row to be about this deposit
  (409 `not_reviewable`) and, for approve, `held` (409 `not_held`). No lock is taken: the watcher writes a
  final-state row only when its content changes, so the approval is not lost; it is bound to the settle
  signature as before.
- Owners (Mark, 2026-10-15: `update_policy` stays `own`): datasets published before deposits existed have no
  depositor, so an admin names one with `POST /datasets/deposit/owners {db_id, user, note?}` or
  `ant-ingest-watch owner set <db_id> <user> --by NAME` on the watcher's host. The assignment is kept in the
  deposit store (`owners`, it wins over the deposit history) and written into the dataset's `.md` as a caveat
  (topic `dataset_owner`) by the watcher's next poll (the CLI writes it at once); the store commit of that
  `.md` is left to a person.

### 22.7 Security notes

- The server's S3 credentials never leave it: `datasets_deposit_s3` (container sites: the key pair in
  `secrets/s3_incoming_access_key_id` and `s3_incoming_secret_access_key`), else the `s3` block. It needs
  `PutObject`, the multipart calls, `CopyObject`, `HeadObject`, `GetObject`, `DeleteObject` and `ListBucket`
  on the staging prefix and the incoming prefix. The first review found that the object store probably
  cannot scope keys to a prefix (about 70% confidence); then the api holds a bucket-wide key, which
  `ant-stack check` warns about.
- A presigned URL is bound to one staging key, one size and (single PUT) the metadata; it cannot list, read,
  delete or write a key the watcher reads, and it dies with the deposit's window. Depositors cannot see other
  deposits; the key locks and `key_taken` stop one depositor from overwriting another's waiting files.
- The api and the watcher must share `ingest_state_dir` (the deposit store, the watcher's state database,
  the report copies). On a container site the watcher runs on the host with
  `ANTHIVE_INGEST_STATE_DIR=<state_dir>/ingest` and the same uid as the containers.

### 22.8 Not done in phase 1

- vark (`vark datasets vocab | deposit | status | withdraw`) and pyvark, the `anthive-dataset-deposit` skill:
  their owners build on the shapes in `doc/API_CHANGELOG.md` 2.46.0. When vark ships, set
  `ingest_watch.accept_unlinked: false`.
- No web page for deposits (no help topic either: the web UI does not mention them yet).
- The watcher has no compose service yet (21.8), so container sites run it on the host.

### 22.9 Security review (2026-10-15)

| Finding | Change |
| --- | --- |
| C1 presigned PUT URLs outlived their deposit: after complete, withdraw or expiry a re-PUT or replay wrote the watched keys, and the object was ingested unlinked with the sidecar's identity, past the checks, quotas, withdraw and expiry | uploads go to `<staging_prefix><deposit_id>/`, outside the watched prefix; complete copies the verified files in; withdraw, expiry and a sweep each poll empty the staging folders; URLs never outlive the deposit window; the watcher refuses unlinked objects carrying a deposit mark; `ingest_watch.accept_unlinked` (true until vark ships) |
| S1 an open deposit froze its whole project | only items whose keys a deposit in `uploading` / `completing` locks wait; URL refreshes capped (`max_refreshes`); admin `POST .../expire` |
| S2 a linked item consumed every static and DE file of the project, also another user's; unlinked items consumed deposit files | per-deposit static, DE and seeds (`deposit_objects`, continuation chain); unlinked items skip deposit files |
| S3 an unreadable deposit store let the poll run without deposit information | fail closed: TransientError, no poll |
| S4 a dataset named `index` (or `*.index`) collided with the group seed names | refused by the API and reported as malformed by `plan_items` |
| create checked limits and keys outside a transaction | `BEGIN IMMEDIATE` around the checks and the insert, key locks with a primary key |
| withdraw or expiry could run while a complete copied | per-deposit compare-and-set states (`completing`, `withdrawing`, `expiring`); an interrupted complete is cleaned up after 12 hours |
| withdraw of a deposit deleted the h5ad a corrected-sidecar deposit used | 409 `in_use` |
| no daily limit; the rate limiter ran after S3 calls | `max_per_day` (20); the limiter runs before any S3 call |
| admins outside the upload groups could not approve; MCP gate differed from REST in basic mode | admin routes admit admins; `_may_deposit` treats basic-mode `auth.admins` like REST |
| existence oracle: different answers (with the dataset id) for "exists, update_of required" and "exists, not yours"; case clash named the group | one `name_taken` answer without the id for callers who may not update; the clash message names nothing (admins excepted) |
| folders without `index.md` (scripts and the like) counted as groups | not a group; refused as a group name |
| `s3://` addresses and the bucket name, admin names in depositor views | redacted; "an admin" |
| malformed `refresh_urls` body or a broken `.md` gave a 500 | 422; a broken `.md` counts as an existing dataset |
| a continuation sidecar was not checked against the waiting h5ad | its `sha256=` must equal the waiting h5ad's recorded hash |
| `x-amz-meta-sha256` could be mistaken for verification | documented as an attestation; the pipeline recomputes the hash |

Second review (same day):

| Finding | Change |
| --- | --- |
| N1 a re-PUT of a staging key while the deposit was `completing` replaced a checked file by an unchecked one | the staging ETag is recorded at verification; the sidecar is read with `IfMatch` on it and the copy is `CopySourceIfMatch` (also forwarded to UploadPartCopy by the managed copy); because a store may ignore both, the bytes read must have that MD5 and a single-PUT file's copy must carry that ETag; any mismatch is 409 `staging_changed`, nothing stays in incoming and the single-PUT files are checked again on the next complete |
| N2 the staging sweep deleted staging objects of deposits another install (shared bucket, a test watcher with its own state dir) did not know | the sweep empties only folders of this install's terminal deposits (`uploaded`, `withdrawn`, `expired`), and folders of unknown ids only when every object is older than `max_open_days`; the staging prefix carries a site id (`__deposits/<site_id>/`, `datasets_deposit_site_id`, default the host name, container sites `site.name`); `ant-stack check` warns that another site on the bucket needs another site id and no watcher on the same incoming prefix |
| N3 a failed final `completing -> uploaded` still reported success and released the key locks; recovery could fire on a running complete | 409 `not_completed`, its copies removed, no locks released; `updated` is refreshed after each copy |
| N4 an owner note could break the `.md` front matter; the assignment was neither versioned nor committed | notes are one line without `---`; caveat plus patch version bump and changelog entry through `md_edit`, written to a temp file renamed over the `.md` under the dataset's writer lock, then a git commit of that `.md` alone (skipped when someone has staged it) |
| N5 unlinked items could use static files locked by an open deposit or carrying a deposit mark | skipped (key lock, deposit file of a `completing` or `uploaded` deposit, or `x-amz-meta-deposit`) |
| N6 a HEAD error while checking an unlinked item for a deposit mark let it through | the item waits for the next poll |

### 22.10 Changes for the vark client (2026-10-15)

| Asked by vark | Done |
| --- | --- |
| the whole facet registry and machine-readable requirements in the vocab | `facets` (every registered and deprecated facet with kind, controlled values, range, hint keywords, scopes), `seed_md.required_spec`, `index_md.required_spec` |
| a corrected sidecar could never pass in the same deposit (URLs sign size and sha256) | `POST .../urls {name, size, sha256}` declares the deposit's own sidecar again while uploading |
| DE tables for a published dataset | DE-only deposits (`update_of`, only `de` files with sidecars naming the dataset); the watcher imports them, bumps the patch version with a changelog entry, pushes the `.de.parquet`, rescans and commits the `.md` |
| a group lookup | `GET /datasets/deposit/groups/{data_project}[?basename=]` (db id only for who may update) |
| withdrawing a correction left the original h5ad without a sidecar | a correction is withdrawn with its original (cascade), not alone (409 `continuation`); the watcher marks a deposit h5ad without sidecar `needs_decision` |
| large completes block the request | above `datasets_deposit_async_complete_gb` (5) complete answers 202 and copies in a background thread with progress in the status; a restart resumes (worker id per process) |

Third review (same day):

| Finding | Change |
| --- | --- |
| R1 a DE-only deposit could overwrite an existing study, wrote sources over another dataset's, and a failed table left the others imported without a version | a study id the dataset has is refused unless an admin deposited it (then archived under `_versions/<basename>/de/<study>/`, "replaced" in the changelog); sources under `<group>/de_sources/<basename>/`; the import runs on a copy of the duckdb under the writer lock and replaces the served files (release / restore) only when every table went in; version and changelog only on success |
| R2 a restarted ant-serve could take over a complete that was still running; files were marked complete before the final state | resume only after `datasets_deposit_complete_lease_minutes` (10) without a lease refresh (each copy and a 30 s heartbeat during long copies refresh it); the final state, the files and the key locks change in one transaction, staging is deleted after it |
| R3 a failing correction in the cascade withdraw stayed `withdrawing` | rolled back with 502, nothing else withdrawn |
| R4 a DE-only deposit could be withdrawn while the watcher imported it | the watcher claims it (`importing`, compare-and-set); withdraw answers 409 `busy`; a claim of a dead run is retried |
| R5 a sidecar could be declared again after the deposit left `uploading` | in one transaction, only while `uploading` |
| nits | the group lookup never offers retired datasets to non-admins and is rate-limited; the API checks DE sidecars with the import's rule; an `AbortIncompleteMultipartUpload` lifecycle rule is recommended and `ant-stack check` says so |

End to end with the real `ant-de-ingest` (2026-10-15, a scratch copy of AZ5/SLM with its study
`lec_vs_igg1_pseudobulk`, throwaway ant-serve, test prefixes): a new study by the recorded owner went live
(version bump, listed by `GET /databases/AZ5/SLM/de`, watcher run 3.9 s); the existing study id by the owner
failed with the duckdb and `.de.parquet` byte-identical; an admin replacement went live with only the replaced
study archived (`de_storage.write_sidecar(only_study=...)`, 12,962 rows) and "replaced" in the changelog; a
deposit of a good and a broken table failed with the served duckdb, `.de.parquet` and `.md` byte-identical
although the good table had been imported into the work copy.

---

## 23. Artefact store (2026-10-16, design only)

Mark decided on 2026-10-16 that custom DE tables and every other generated file (figures, tables, notebooks,
reports, models) go through the deposit route of section 22, are kept as versioned, immutable artefacts in the
static area of their dataset or collection on S3, and are served by a redirect to a short-lived signed GET.
Analysts may also create collections without datasets. The design is in its own document,
`doc/DESIGN_artefacts.md`, because most of it is outside ingestion (serving from S3, collections without
datasets, web UI, MCP, ant-sync, its own security checklist) and this document is already long.

What it takes from this document:

- the deposit API unchanged, with two new values of `deposit_kind` (`artefact`, `collection`): the staging
  prefix, presigned uploads bound to size and metadata, `complete` with `CopySourceIfMatch` and the MD5
  check, key locks in `BEGIN IMMEDIATE`, the per-user limits and the rate limiter (22.2, 22.3, 22.9);
- the watcher as the only writer, with a new item type keyed by deposit id, the same states, settle rule,
  resume markers and approve / reject / retry (21.3, 21.5, 22.4); unlinked artefact objects are always
  refused;
- `delete_consumed` for incoming cleanup and for purges, one key at a time and HEAD-compared (21.6);
- the DE-only import of 22.10 as the import step of artefacts of kind `de_table`; the DE-only deposit becomes
  an artefact deposit, and `<group>/de_sources/` is migrated into artefacts.
