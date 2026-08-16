# anthive4 — upgrade procedure

This is the operational runbook for pulling the full stack up to head. It
covers three separable layers you can upgrade independently or together:

1. **Software** — the anthive4 repo (`lib/`, `bin/`, `web/`, `envs/`)
2. **h5adstore metadata** — the sibling git repo that holds every `.md` and
   `.h5ad.mus` sidecar (`data/h5adstore/`, symlinked to `/data/1/project/h5adstore`)
3. **h5adstore data files** — the `.h5ad`, `.duckdb`, and parquet sidecars
   that live in S3 (never in git; too large)

Plus a fourth section on how to **apply metadata changes that alter the DuckDB
view** — obs renames, obs_derive, caveats, etc. — without needing a full
reconvert.

Everything below assumes you are logged into a host that has `anthive.yaml`
configured (S3 credentials, api host+port) and can reach the running
`ant-serve` on `http://127.0.0.1:8080`.

---

## 0. Quick sanity check

```bash
cd /data/1/users/mark/anthive4
git status                                    # both dirty? clean start = good
cd data/h5adstore && git status && cd ../..
curl -sf http://127.0.0.1:8080/health | jq .  # is the API alive?
./bin/ops/ant-supervisor status               # are all services up?
```

If either git repo has uncommitted changes you don't recognize, **stop and
review** — someone else may have been editing metadata. `git log --oneline
-5` on both to see recent history.

---

## 1. Upgrade the software (`anthive4` repo)

```bash
cd /data/1/users/mark/anthive4
git fetch origin
git log --oneline HEAD..origin/main           # what's incoming?
git pull                                      # or rebase if you have local commits
```

**Restart the runtime services** so they pick up code changes:

```bash
./bin/ops/ant-supervisor restart api          # ant-serve (port 8080)
./bin/ops/ant-supervisor restart browse       # web UI (port 8081)
./bin/ops/ant-supervisor status               # confirm all UP
```

**`lib/` changes ripple to every `bin/<domain>/ant-*` tool AND the server**
via the `sys.path` shim — there is no version boundary. A pull that touches
`lib/` requires the service restarts above. Individual `ant-*` scripts don't
need "installing" — they re-import `lib/` on every invocation.

**Dependency changes** (`envs/anthive-data`): if `envs/anthive-data.yml`
changed:

```bash
micromamba env update -f envs/anthive-data.yml -p envs/anthive-data
```

For `uv run --script` shebangs in `bin/<domain>/ant-*`: their PEP-723 header
holds the dep list. `uv` re-resolves per invocation — no manual step.

---

## 2. Upgrade the h5adstore metadata (`.md` + `.h5ad.mus`)

The h5adstore is a **separate git repo** (github.com/badslab/h5adstore or
whatever's `origin`) symlinked into anthive4 as `data/h5adstore/`.

```bash
cd /data/1/users/mark/anthive4/data/h5adstore
git fetch origin
git log --oneline HEAD..origin/main
git pull
```

**After a metadata pull, tell ant-serve to reload** — the pull may have
introduced new datasets or changed api-config blocks:

```bash
curl -s -X POST http://127.0.0.1:8080/admin/rescan | jq .
```

`/admin/rescan` re-walks the filesystem for new datasets and re-parses every
`.md`. It does NOT re-open DuckDB files (fast). For datasets whose
`.md`'s `api:` block changed (obs_rename, obs_derive, obs_hidden, caveats):

```bash
curl -sS -X POST http://127.0.0.1:8080/admin/refresh-views/<group>/<file_id> \
  -o /dev/null -w "%{http_code}\n"
```

This DROPs and re-CREATEs the `obscat` / `obsnum` views over the underlying
`__obscat` / `__obsnum` tables. No filesystem write, in-memory only. See
§4 for the fuller how-to.

---

## 3. Upgrade the h5adstore data files (S3 sync)

Data files are gitignored and live on S3 (Hetzner storagebox by default).
Two directions:

### Pull data files from S3 to the local host

```bash
cd /data/1/users/mark/anthive4
./bin/ops/ant-sync pull '<pattern>'
# examples:
./bin/ops/ant-sync pull 'Lee2026/*'                    # a whole group
./bin/ops/ant-sync pull 'SP4/*_Micro_*'                # subset by pattern
./bin/ops/ant-sync pull ''                             # everything (~600 GB)
```

`ant-sync` reads S3 credentials from `anthive.yaml`. Files touched:
`.h5ad`, `.duckdb`, and `layer.*.parquet` sidecars. It NEVER touches
`.md` or `.h5ad.mus` — those live in git.

### Push local data files to S3

After a `ant convert` produced new `.duckdb`/parquet or you edited a `.h5ad`:

```bash
./bin/ops/ant-sync push '<pattern>'
# examples:
./bin/ops/ant-sync push 'Zhou2020/microglia.*'
./bin/ops/ant-sync push 'Prinz2026/*'
./bin/ops/ant-sync push ''                             # everything, careful
```

The tool skips files whose local + remote hashes match, so a full `push ''`
after most files are up-to-date is safe (just slow to enumerate). Uploads
are chunked; a broken sync resumes.

---

## 4. Applying metadata changes that alter DuckDB views

The most common day-to-day operation: you edit a `.md` `api:` block to
rename / hide / derive an obs column, and you want it live NOW.

### The `api:` block schema

Full spec in `lib/metadata.py::get_api_config()`. Summary:

```yaml
api:
  obs_rename:              # rename internal → public name
    individualID: donor_id
    Fur.color: Fur color   # spaces allowed on the public side
  obs_hidden:              # drop entirely (never surfaces)
    - hash.ID
    - vcf_id
  obs_value_rename:        # rewrite specific values in a categorical col
    condition:
      "cognitively normal": normal
      Control: normal
  obs_value_hidden:        # NULL specific values (applied BEFORE value_rename)
    condition:
      - unknown
  obs_derive:              # publish a new column from a value map
    astro_contaminated:
      source: microglia_subcluster
      dtype: categorical   # or numerical
      map:
        "4": "yes"
      default: "no"
```

### The `caveats:` block (separate from `api:`)

Top-level field, machine-readable warnings for the web UI:

```yaml
caveats:
  - level: medium              # low | medium | high
    kind: contamination        # see lib/metadata.py::CAVEAT_KINDS
    scope: microglia_subcluster=4
    obs_flag: astro_contaminated
    message: >
      5,827 cells (7.7%) in microglia_subcluster=4 co-express GFAP,
      SLC1A2, AQP4 above ambient baseline — likely astrocyte doublets.
    detected_at: '2026-08-16'
    detected_by: catalog audit v6
```

Rendered as a warning card on the dataset page; group index shows a ⚠
marker; `/databases/{id}/info` exposes it to CLI/MCP clients.

### The workflow — edit → apply → verify

```bash
# 1. Edit the .md
vi data/h5adstore/SP4/microglia_PFC.md
#    ... add api.obs_rename: individualID: donor_id ...

# 2. Ask ant-serve to reload the api config + rebuild the views
curl -s -X POST http://127.0.0.1:8080/admin/api-config/SP4/MPF \
  -H "Content-Type: application/json" -d @- <<EOF
{"obs_rename": {"individualID": "donor_id"}}
EOF
# OR (if you already edited the .md):
curl -sS -X POST http://127.0.0.1:8080/admin/refresh-views/SP4/MPF \
  -o /dev/null -w "%{http_code}\n"

# 3. Verify
curl -s http://127.0.0.1:8080/databases/SP4/MPF/metadata/fields | jq
curl -s http://127.0.0.1:8080/databases/SP4/MPF/info | jq .caveats
```

If the API returns HTTP 404 on refresh-views: check `data/h5adstore/<group>/`
for a `.inactive` sidecar next to the h5ad — inactive datasets are excluded
from the catalog on purpose.

### When a `.md` change requires a full re-convert (not just a view refresh)

Any change to the **column set** stored in the underlying `__obscat` /
`__obsnum` tables:

- Adding a column to `obscat_include` that wasn't there before
- Removing a column from `obs_exclude`
- Changing `layers_include` / `layers_exclude`
- Any change to which layer holds counts vs log-norm

Then you need a full re-convert:

```bash
./bin/ant convert data/h5adstore/Lee2026/DataS2_PsychAD_final.h5ad
./bin/ops/ant-sync push 'Lee2026/DataS2_PsychAD_final*'
curl -s -X POST http://127.0.0.1:8080/admin/rescan
curl -sS -X POST http://127.0.0.1:8080/admin/refresh-views/L26/PSY \
  -o /dev/null -w "%{http_code}\n"
```

Reconvert times: proportional to `n_obs * n_vars`. Rule of thumb: ~1 min per
100k cells at ~30k genes. For half-million-cell objects like Lee2026, plan
~10-15 min.

### Gotcha: pending obs cols block reconvert

If `ant score` or `ant starcat` wrote annotation columns to the DuckDB and
they haven't been merged back to the h5ad, `ant convert` will refuse to
rebuild (would silently lose them). Fix:

```bash
./bin/ant merge-h5ad data/h5adstore/<group>/<file>.md
# then retry the convert
```

`ant merge-h5ad` reads `__pending_cols` from the DuckDB, writes those obs
columns into the h5ad, and clears the audit trail. Runs against a
released copy of the DuckDB — no ant-serve outage.

### Gotcha: `ant check` sometimes drops the `id:` line

Known bug: `ant check` regenerates the `.md` frontmatter and occasionally
strips top-level fields it doesn't recognize (notably `id:`, `year:`,
`caveats:`). Symptoms: dataset disappears from the catalog after a
rescan, or shows the wrong file_id.

Fix: `git diff data/h5adstore/<group>/<file>.md`, restore the stripped
fields, `git checkout` the parts you didn't intend to change.

---

## 5. End-to-end: full stack upgrade in one shell session

```bash
# software
cd /data/1/users/mark/anthive4 && git pull
./bin/ops/ant-supervisor restart api browse

# metadata
cd data/h5adstore && git pull && cd ../..

# data files (only if you're on a fresh host or files diverge)
./bin/ops/ant-sync pull ''

# reload the catalog
curl -s -X POST http://127.0.0.1:8080/admin/rescan | jq .previous_count,.current_count

# sanity — does the API respond and count as expected?
curl -s http://127.0.0.1:8080/databases | jq '.databases | length'
```

---

## 6. Publishing changes back

When you finish work that touched anything:

```bash
# code changes → anthive4 repo
cd /data/1/users/mark/anthive4
git status
git add -p                            # patch-review; NEVER `git add -A`
git commit -m "..."
git push

# metadata changes → h5adstore repo
cd data/h5adstore
git status
git add -p
git commit -m "..."
git push

# data file changes → S3
cd /data/1/users/mark/anthive4
./bin/ops/ant-sync push '<what-you-changed>'
```

Never commit large `.h5ad`, `.duckdb`, or `.parquet` files to the
h5adstore repo (they're gitignored) — those go to S3 only. If `git status`
shows one as untracked, you have a `.gitignore` gap somewhere.

---

## 7. Common recovery scenarios

**Symptom**: dataset disappeared from `/databases` after a rescan.
- Check for `.inactive` sidecar next to the `.h5ad`.
- Check for stripped `id:` line in the `.md` (see §4 gotcha).
- Confirm the `.h5ad`, `.duckdb`, and `.md` are all present locally.
- `journalctl -u ant-serve` or the supervisor log for parse errors.

**Symptom**: `ant convert` refuses with "pending obs columns".
- Run `ant merge-h5ad <path>.md` first, then re-convert.

**Symptom**: obs column renamed via `api.obs_rename` doesn't show up.
- The source column must exist in `__obscat` / `__obsnum` — an obs_rename
  can't summon a column that isn't already ingested. If the source is
  filtered out by `obscat_include`, add it to the include list and
  full-reconvert (not just refresh-views).

**Symptom**: refresh-views returns 404.
- Dataset isn't in the catalog. Either `.inactive`, or missing from disk,
  or a `.md` parse error kept it out. `rescan` first, then check.

**Symptom**: web UI shows stale content.
- Restart `browse` (`./bin/ops/ant-supervisor restart browse`) — it caches
  metadata via `/api/databases`.

**Symptom**: many datasets suddenly "look wrong" after a bulk metadata edit.
- Revert with `git checkout .` in `data/h5adstore/` — that undoes uncommitted
  `.md` / `.h5ad.mus` changes. Then rescan.

---

## 8. Where to look when stuck

- `lib/CLAUDE.md` — the shared-core contract
- `bin/CLAUDE.md`, `bin/api/CLAUDE.md`, etc. — per-domain guides
- `data/h5adstore/CONVENTIONS.md` — h5ad + `.md` + `.h5ad.mus` layout rules
- `doc/ARCHITECTURE.md`, `doc/DESIGN_annotation_updates.md` — deeper design
- `doc/ONTOLOGY_MAPPING.md` — tissue/disease vocab + parent-child hierarchy
- `lib/_mus_vocab.yaml` — canonical tissue/disease/cell_type value lists
- `lib/_ontology_hierarchy.yaml` — parent-child relations for tissue + disease
