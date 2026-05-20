# QA Findings — Critical Pass

**Date:** 2026-05-19
**Scope of this document:** issues found in the anthive4 software stack.

## Scope and provenance — read this first

This document mixes findings from three sources. Each item is tagged:

- **[critic]** — from the ruthless-qa-critic agent that covered the
  API + MCP surface: `bin/ant-serve`, `bin/ant-mcp`, `lib/anthive_db.py`,
  `lib/analytics.py`. These have been read line by line.
- **[session]** — observed during the work that produced this branch
  (the geneset / scoring / ingestion arc). Reproduced, not speculative.
- **[unverified]** — plausible but not confirmed by a code read. Treat as
  "investigate" rather than "fix immediately." These are listed at the
  end of each section in italics so they can be checked first.

Components **not yet pass-reviewed** by a critic:
`bin/ant-sync`, `bin/ant_s3`, `bin/ant-s3-check`,
`bin/ant-convert`, `bin/ant-h5ad-check` (beyond the wipe bug),
`bin/ant-md`, `bin/ant-md-check`, `bin/ant-score` (beyond what
`ant-serve`'s critic flagged), `bin/ant-supervisor`,
`bin/ant-build-reports`,
`lib/layer_stats.py`, `lib/metadata.py`, `lib/config.py`,
`lib/checksum.py`, `lib/h5ad_utils.py`, `lib/h5adstore.py`,
`lib/geneset.py`, `lib/scoring.py`,
`webapp-browser/*`.
Re-run with `ruthless-qa-critic` per component before treating those
areas as audited.

---

## 🔴 Critical

### 1. SQL injection via column names in `/databases/{db_id}/module_scores` **[critic]**

- Location: `bin/ant-serve:2682-2691`
- Problem: column names from `information_schema.columns` are interpolated
  raw into f-strings (`MIN("{col}")`). `is_score_column()` is a regex
  match, not a validator. If anyone can write a DuckDB file with a
  crafted column name (`"); DROP …`), the closing quote escapes and
  arbitrary SQL runs in the request thread.
- Same trust-the-column-name pattern in `lib/anthive_db.py:580-593`
  (the `layer__{layer}` interpolation), `:357`.
- Fix: strict identifier regex `^[A-Za-z0-9_:]+$` before interpolation,
  or look up the column with a parameterized check then escape `"` as
  `""`. Centralize as a single `_quote_ident()` helper.
- [ ] Fixed

### 2. `analytics_query` write-blocking is bypassable **[critic]**

- Location: `lib/analytics.py:69-95`
- Problems:
  - DuckDB's bare-path-as-table syntax `SELECT * FROM 's3://attacker/payload.parquet'`
    is not blocked even though `read_parquet` is. Reads arbitrary URLs
    with the server's S3 credentials attached.
  - `SET memory_limit='100TB'` / `SET threads=…` are accepted — single
    query DoS / config drift.
  - String literals containing keywords trigger false positives:
    `SELECT 'INSERT INTO …' AS msg` is rejected. Correctness bug.
  - The block-comment stripper is not parse-aware: `SELECT '/*' AS a, '*/' AS b`
    is silently mangled.
- Fix: use DuckDB's own parser (`EXPLAIN` plan walk) to enforce that the
  FROM list contains only known catalog tables, and that no `SET`/`COPY`
  ever appears. Replace regex stripper with parse-aware tokenizer.
- [ ] Fixed

### 3. S3 credentials in plaintext on shared FS **[session]**

- Location: `anthive.yaml:4-5`
- Problem: live Hetzner access key + secret committed to a world-readable
  file (`-rw-r--r--`) on `/data/1/users/mark/...` — shared box.
- Fix:
  - `chmod 600 anthive.yaml`
  - Rotate keys (they're now in Claude Code transcripts and have been
    visible to any local user since the file was created).
  - Move to `~/.anthive/credentials.yaml` (already supported by `ant_s3`)
    or a sealed secret store; remove from the repo-tracked file.
- [ ] Rotated
- [ ] Moved out of repo
- [ ] `chmod 600`

### 4. `ant-h5ad-check` wipes user-curated annotations **[session]**

- Location: `bin/ant-h5ad-check` annotation-merge logic (verified by
  reproduction on Mancuso_2023.md during this session)
- Problem: re-running the check on a curated `.md` nulls out
  `annotation.organism`, `annotation.doi`, `annotation.accession`,
  `default_layer`, `top_genes`, `top_cat`, `top_num`. The
  preserve-on-rewrite logic only knows about a hard-coded handful of
  fields.
- Fix: invert the policy. Start from the existing `.md`'s YAML, overwrite
  *only* fields the tool can derive (layer_stats, n_obs, n_vars,
  sha256, date). Everything else is sticky.
- [ ] Fixed

### 5. Score-index globals mutated without a lock **[critic]**

- Location: `bin/ant-serve:2488-2541`
- Problems:
  - `_build_score_index()` publishes three module globals
    (`_score_index_by_db`, `_score_index_by_geneset`, `_score_columns`)
    via three separate assignments. A reader landing between them sees a
    stale dict paired with a fresh one.
  - The scan runs synchronously inside an `async def` endpoint — blocks
    the entire event loop while it walks every DuckDB.
  - `_score_columns` is populated and then never read anywhere. Dead
    state.
- Fix:
  - Build into local dicts (already done), then publish as a single
    `namedtuple` snapshot via one assignment under a `threading.Lock`.
  - Wrap the scan in `asyncio.to_thread(...)`.
  - Either delete `_score_columns` or actually use it where
    `SCORE_COLUMN_TEMPLATE.format(...)` is used today (it exists to
    handle naming-convention drift).
- [ ] Fixed (locking)
- [ ] Fixed (to_thread)
- [ ] Removed/used `_score_columns`

### 6. Score-index rebuild bypasses connection-pool cap **[critic]**

- Location: `bin/ant-serve:2502-2527`, `connection_pool.get_connection`
  at `~:231`
- Problem: the pool transparently exceeds `max_connections_per_db` by
  minting a fresh connection when full. An admin rescan during traffic
  doubles connection count, blowing past the configured DuckDB memory
  budget. OOM risk on a small server.
- Fix: honor the cap with a bounded wait, OR pass a single connection
  through the scan (reuse the one already opened by `_cache_all_metadata`).
- [ ] Fixed

### 7. `attach_sessions` session_id is f-string-interpolated into SQL **[critic]**

- Location: `lib/analytics.py:331-338`
- Problem: `read_parquet('{pq_path}')` is built from `pq_path = tmp / f"{sid}.parquet"`
  where `sid` comes from the request body with no hex-UUID validation. A
  `sid` containing a single quote terminates the SQL string literal.
- Fix: `re.fullmatch(r'[0-9a-f]{32}', sid)` (or full UUID regex) before
  building any path or SQL fragment from it.
- [ ] Fixed

### 8. `ant-sync` compares files by size only **[session]**

- Location: `bin/ant-sync` push/pull comparison logic (verified by
  reading the file earlier this session — push checks `s3_size == local_size`,
  pull checks `local_size == s3_size`).
- Problem: `.h5ad` and `.duckdb` re-writes routinely produce identical
  sizes despite different content. Push silently skips modified files;
  pull silently overwrites newer local copies. No mtime check.
- Fix: add mtime comparison (`LastModified` from `list_objects_v2`) and
  refuse to overwrite a newer destination unless `--force`. Log the
  size+mtime pair in `[SKIP]` lines so the user sees what was decided.
- [ ] Fixed

---

## 🟠 Serious

### 9. Daily metadata refresh doesn't rebuild the score index **[critic]**

- Location: `bin/ant-serve:980` (`_daily_metadata_refresh`)
- Problem: after 24h of adding/removing datasets, `scored_in` in
  `/genesets/*` responses returns stale `db_ids`. `get_geneset` advertises
  scores in databases that no longer exist.
- Fix: call `_build_score_index()` at the end of the daily refresh.
- [ ] Fixed

### 10. Provenance overwrites itself **[critic]**

- Location: `bin/ant-score:299-301` (the
  `adata.uns['module_scores'][safe_key] = json.dumps(record)` line)
- Problem: re-scoring an experiment replaces the prior record — no
  history, no drift detection (the genesetstore SHA is recorded but
  never compared to the previous one).
- Fix: append rather than replace. Either store a list of records under
  one key, or include the genesetstore SHA in the key so different
  versions coexist.
- [ ] Fixed

### 11. `_organism_compatible` heuristic is unsound **[critic]**

- Location: `bin/ant-score:522-545`
- Problems:
  - Counts hyphenated names as uppercase (`H2-Aa` matches because of `'-' in g`)
    even though `H2-Aa` is mouse case.
  - Thresholds overlap: human requires `≥0.4` and mouse requires `≤0.7`.
    Anything between is approved silently for both.
- Fix: this is a fallback when `.md annotation.organism` is missing. The
  cleanest correct version is to refuse to score and tell the user to
  set `annotation.organism` explicitly. The heuristic exists as a "best
  effort" — make it warn-only and require explicit override.
- [ ] Fixed

### 12. Ensembl autodetect false-negatives **[critic]**

- Location: `bin/ant-score:486-519`
- Problem: only matches `ENSG`/`ENSMUS`. Misses rat (`ENSRNO`),
  zebrafish (`ENSDAR`), mixed-species xenograft files, anything with a
  prefix.
- Fix: extend the pattern to all Ensembl species prefixes
  (`ENS[A-Z]{0,3}G\d+`), and consider matching by `ENS.*\d{11}` shape
  instead of by prefix.
- [ ] Fixed

### 13. `.md` mutation in `ant-score` is racy and non-atomic with the h5ad **[critic-implied]**

- Location: `bin/ant-score` — h5ad atomic replace, then `.md` update.
- Problem: two concurrent runs on the same dataset can lose each other's
  `obsnum_include` additions. A crash between the h5ad replace and the
  `.md` update leaves orphan obs columns invisible to `ant-convert`.
- Fix: `flock` the `.md` for the entire update; write both the h5ad and
  the `.md` then atomically rename a marker file last.
- [ ] Fixed

### 14. `_swap_var_names` keeps first occurrence on dedup **[critic-implied]**

- Location: `bin/ant-score:459-483`
- Problem: when two Ensembl IDs map to the same gene symbol,
  `keep='first'` picks file-order, not most-expressed. The picked row
  may be the zero-expression duplicate of an expressed gene.
- Fix: collapse by summing the expression rows for duplicate symbols
  rather than dropping. Match how scanpy's standard ingestion handles
  this in `var_names_make_unique` with `keep_largest` semantics.
- [ ] Fixed

### 15. `module_score` 500 leaks internals **[critic]**

- Location: `bin/ant-serve:2407-2414`
- Problem: exception text including paths and gene names is returned in
  the response body. Fine internally; problematic if the endpoint is
  ever exposed publicly.
- Fix: log full exception server-side, return a generic message + a
  correlation id.
- [ ] Fixed

### 16. `module_score` parquet files accumulate **[critic]**

- Location: `bin/ant-serve:2417-2420`
- Problem: cleanup is triggered only by `analytics_query`, not by ad-hoc
  module scoring. Disk fills over time on a long-running server.
- Fix: call `_cleanup_old_sessions()` here too, or schedule it from the
  daily refresh.
- [ ] Fixed

### 17. `module_score` accepts unbounded `n_control` **[critic]**

- Location: `bin/ant-serve:140-145` (request model) → SQL building in
  `anthive_db.py:679, 687, 698, 708`
- Problem: `n_control = 10_000_000` constructs an enormous CTE. No upper
  bound enforced.
- Fix: Pydantic `Field(50, ge=1, le=1000)` on the request model.
- [ ] Fixed

### 18. `get_database_module_scores` is unbounded **[critic]**

- Location: `bin/ant-serve:2632-2716`
- Problem: per-column full-table aggregation, no timeout, no pagination.
  One curl request can pin a worker for many minutes on a 50M-cell
  dataset with many score columns.
- Fix: collapse stats into a single SELECT (UNPIVOT or one big SELECT
  list), or cache results and return cached stats with a `cached_at`
  timestamp.
- [ ] Fixed

### 19. MCP tool descriptions steer the LLM the wrong way **[critic]**

- Location: `bin/ant-mcp:1101-1121` (`module_score`),
  `bin/ant-mcp:1014-1026` (`list_module_scores`),
  `bin/ant-mcp:765` (`analytics_query`)
- Problem: `module_score`'s docstring describes how to compute fresh
  scores and never says "check pre-computed first." `list_module_scores`
  isn't positioned as the preferred preflight. `analytics_query`'s
  description doesn't mention that pre-computed scores live as plain
  columns in `obsnum`.
- Fix: rewrite the first paragraph of each:
  - `module_score`: "Use ONLY when the geneset is not pre-computed
    (check with `list_module_scores` first)."
  - `list_module_scores`: "Always call this before `module_score` …"
  - `analytics_query`: "Pre-computed module scores live as obs columns
    named `<study>::<experiment>::<name>` — query `obsnum` directly."
- [ ] Fixed

### 20. Connection-pool decrement after close **[critic]**

- Location: `bin/ant-serve:240-242` (`return_connection`)
- Problem: if `conn.close()` raises (S3 hiccup), `in_use` is never
  decremented. The pool permanently believes a slot is occupied.
- Fix: try/finally around the decrement.
- [ ] Fixed

### 21. `_pooled_db` does not handle `KeyError` from `get_database_path` **[critic]**

- Location: `bin/ant-serve:1146-1153`
- Problem: callers that forget the `db_id in registry.databases` check
  get a 500 with the raw KeyError repr.
- Fix: raise `HTTPException(status_code=404)` from `_pooled_db` directly.
- [ ] Fixed

### 22. `validate_python` sandbox holes **[critic]**

- Location: `lib/analytics.py:100-157`
- Problems:
  - Dynamic dispatch via `getattr(pd, "read_" + "csv")` bypasses the
    name-based blocklist.
  - The subprocess inherits parent env (S3 creds, etc.) since no `env=`
    is passed to `subprocess.run`.
- Fix: this is fundamentally a defense-in-depth scanner, not a sandbox.
  Document that. Add subprocess-level controls (firejail/seccomp/no
  network); explicitly pass a minimal `env=` to `subprocess.run`.
- [ ] Hardened (env=)
- [ ] Documented limits

### 23. `_strip_sql_comments` is not parse-aware **[critic]**

- Location: `lib/analytics.py` (block-comment regex)
- Problem: `SELECT '/*' AS a, '*/' AS b` becomes `SELECT ' AS b` —
  silently mangles valid SQL. Combined with the keyword-in-string false
  positive (#2), this means valid user queries can be rejected or
  rewritten.
- Fix: tokenize via DuckDB's own parser (`EXPLAIN`) rather than regex.
- [ ] Fixed

### 24. analytics_query thread cannot be cancelled cleanly **[critic]**

- Location: `lib/analytics.py:349-374`
- Problem: on timeout, the main thread closes the DuckDB connection while
  the worker thread is still executing. Best-effort `join(2)`; if the
  worker is wedged in C code the daemon thread leaks indefinitely along
  with the partially-torn-down PyConnection.
- Fix: call DuckDB's `interrupt()` before close (1.1+); treat
  join-timeout as a critical event and poison the slot.
- [ ] Fixed

### 25. Route ambiguity / `:path` param swallow **[critic]**

- Location: `bin/ant-serve:2574, 2615, 2632` (`/genesets/rescan` vs
  `/genesets/{study}/{exp}`; `{db_id:path}` swallow)
- Problems:
  - `GET /genesets/rescan/foo` produces a confusing "Experiment
    rescan/foo not found" rather than the obvious 404.
  - `{db_id:path}` swallows arbitrary trailing segments — three-part
    db_ids form silently if a YAML produces a `group_id` containing `/`.
- Fix:
  - Validate `db_id` shape (`^[A-Za-z0-9_]+/[A-Za-z0-9_]+$`) on registry
    insertion.
  - Move `rescan` under `/genesets/-/rescan` or `/admin/genesets-rescan`.
- [ ] Fixed

### 26. `{study}/{experiment}/{name}` rejects names with `/` silently **[critic]**

- Location: `bin/ant-serve:2593`
- Problem: a geneset name containing `/` (registry doesn't forbid this
  today) matches no route, returns 404 with no useful hint.
- Fix: reject `/` in geneset names at registry load (preferred), or
  accept `{name:path}` and split server-side.
- [ ] Fixed

---

## 🟡 Concern

These are smaller bugs / footguns — fix when you're already in the file.

- **Stats helper NaN propagation** — single NaN in a layer turns every
  stat into NaN; check this in `lib/layer_stats.py` and `ant-score`'s
  `_score_one` summary path.
- **`safe_key.replace('/', '::')` would collide** if anyone ever uses
  `::` inside a name. Document or enforce.
- **`find_h5ad` is O(N) walk per call** — would benefit from a cache when
  called from a hot path.
- **`_score_columns` global** — populated, never read (cf. #5).
- **`cwd`-relative paths in `ant-supervisor`** — surveys needed; not
  audited here.
- **Two separate config-loading systems** — `lib/config.py` (used by
  `ant-sync`, `ant-s3-check`) and `ant_s3`'s own loader read different
  precedence. `ant_s3 show` ≠ what `ant-sync` will use. Verify.
- **`ant_s3 status` and `ant-sync status`** are likely to disagree
  because of the above. Verify.
- **`ant_s3 upload` extension-check is warn-only** — a typo can push
  `~/.ssh/id_rsa` if the user passes it. Verify.
- **`ant-s3-check --require-s3`** apparently returns "ok (local only)"
  on connection failure (silent pass). Verify.
- **`/skill` reads file on every request** — `bin/ant-serve:2729-2733`.
  Cache to memory.
- **`_cleanup_old_sessions` race** — concurrent calls + `stat()` between
  `iterdir` and mtime check can throw. Wrap in try/except.

---

## What's actually fine (do not "fix")

These were audited and look good:

- Geneset store design and the `.grp`/`.rnk` strict parsing.
- Geneset registry index.md inheritance walk.
- Atomic write of `~/.anthive/credentials.yaml`.
- `.md` excluded from S3 sync.
- Presigned URL expiry cap.
- That credential masking exists in `ant_s3 show` at all (though the
  masking implementation needs verifying).

---

## Suggested order of attack

If you only do five things, do these in this order:

1. **#3 S3 credentials** — rotate + `chmod 600` + move out of the repo file.
   Single biggest exposure.
2. **#4 ant-h5ad-check annotation wipe** — bit you once, will bite anyone
   re-checking a fixed h5ad.
3. **#8 ant-sync size-only diff** — biggest data-integrity risk if you
   pull from a stale S3.
4. **#1 + #2 SQL injection / FROM '…' bypass** — security; before any
   external exposure.
5. **#5 score-index race + sync-in-async** — first user complaint will be
   "rescan made the API hang."

Then sweep through the Serious tier as time allows.

---

## How to re-run the critics for unaudited components

```
# in this repo:
# Spawn a ruthless-qa-critic per logical scope, e.g.
#  bin/ant-convert + bin/ant-h5ad-check + lib/metadata.py
#  bin/ant-sync + bin/ant_s3 + bin/ant-s3-check
#  bin/ant-supervisor + webapp-browser/*
#  lib/layer_stats.py + lib/checksum.py + lib/h5ad_utils.py
```

Each should ideally take < 30 min of agent compute. I'd suggest running
them in parallel before relying on the [unverified] / [critic-implied]
items in this doc.
