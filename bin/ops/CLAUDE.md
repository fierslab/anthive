# bin/ops/ — operations, sync, and infra utilities

Process supervision, S3 sync, build helpers, and developer wrappers.

| Tool | Role |
| --- | --- |
| `ant-stack` | **The deployment tool.** Renders `site.yaml` into a docker compose stack under `<state_dir>/stack/` and drives it (`init`, `check`, `render`, `up`, `down`, `logs`, `ps`, `pull`, `build`, `passwd`, `ingest -- ...`, `publish-library`). Renders three configs (`anthive.yaml` for web+mcp, `anthive-api.yaml`, `anthive-ingest.yaml`) so S3 keys only reach ingest unless `s3.api_access` says otherwise; api/mcp see only their own secret files. Tests: `tests/test_deploy_config.py`, `tests/test_ant_stack_hardening.py`. See `doc/DEPLOY_CONTAINERS.md`. 2.44.0: `data.extra_mounts` (read-only same-path binds into api/web/mcp/ingest for symlinked stores; `_check_extra_mounts`, `_check_symlinks`), `library.index_pull` (rendered as `library_index_pull`, off without S3), `_check_uv`. Tests: `tests/test_library_site_modes.py`. 2.45.1: api/web/mcp get `ANTHIVE_CONFIG_SHA` (sha256 of their mounted config + secret files, `config_digest`, `CONFIG_HASHED_SERVICES`) so `up` recreates them when only the rendered config changed; `restart` takes several services; `check` always prints the internal dataset count (`internal_access_summary`) and the published host address; renders `log_stderr_level: info` (ant-serve INFO to stderr, default stays warning for the dev supervisor). Tests at the end of `tests/test_ant_stack_hardening.py`. 2.46.1: every render goes to `stack/staging/` (`staging_dir`, `staged_changes`, `promote`); only `init`, `render`, `up`, `passwd` apply it (`render(apply=True)`), `pull`/`check`/`ingest`/`logs`/`restart`/`publish-library` only stage, so old containers never read a new release's config; the staged compose digest is computed over the staged files (`config_digest(read_from=...)`). Renders are validated with `ANTHIVE_CONFIG_STRICT=1`; host tools are strict anyway, only ant-serve, ant-browse and ant-mcp call `config.set_unknown_keys_lenient()`, and `config.CRITICAL_KEYS` are strict everywhere. `up` stages, runs `check`, and promotes only when nothing blocks; `passwd` promotes only `caddy/auth.caddy`; render/promote hold an flock on `stack/.lock` and write temp + rename (`_atomic_write`). The watcher config is rendered only with deposits on (the live copy is removed by promote, not by a stage-only render). Caddy snippets are 0644 (`CADDY_FILE_MODE`: Caddy's root may be a remapped uid; `stack/` 0700 keeps them private), `_check_caddy_readable` warns otherwise; `up` holds the stack lock from stage to promote. Fourth config role `watcher` (`anthive-watcher.yaml`: host paths, real state_dir, full S3 keys, `api_url` = Caddy's tools listener `127.0.0.1:<ports.tools>`, `PORT_TOOLS` 8079, `render_caddy_tools`, imported through `ANTHIVE_HTTP_REDIRECT_SNIPPET`: identity headers dropped, `X-Anthive-Internal` required), `ant-stack watch` (`watcher_env`, `watcher_command`, printed by `check`). `check` counts datasets with `lib/h5adstore.catalog_dataset_mds` (the catalog scan's walker). `publish-library --s3-host-config PATH` runs `ant-sync library --no-git` on the host. Tests at the end of `tests/test_ant_stack_hardening.py`. 2.46.2: `load_site` keeps `site_key_findings(user, example)` in `_key_problems` / `_key_warnings` (unknown keys: PROBLEM in `STRICT_SECTIONS`, WARNING elsewhere, `EXTRA_SITE_KEYS` / `FREE_FORM` for keys the example only comments); `_validate_rendered_config` reports a ConfigError as one line and `check` prints render errors as PROBLEM; `watch` prints its command; container configs carry `mcp.mcp_url`. Tests: `tests/test_scratch_site_2462.py`. Next release: `ui:` block (`facets`, `caveats`, `api`, `mcp`, booleans, strict section, `UI_SWITCHES`, `ui_settings`) rendered into every role as `ui`; `mcp_enabled(site)` = `services.mcp` and `ui.mcp` drives the mcp profile, `mcp.enabled`, the plan and `up`'s cleanup; `render_caddy_mcp` writes `stack/caddy/mcp.caddy` from `config/caddy-mcp.on|off` (the off snippet answers a JSON 404 on `/mcp`; rendered from the checkout so it does not depend on the image's Caddy config) and the caddy service gets `ANTHIVE_MCP_SNIPPET` pointing at it; `_check_ui` prints the parts that are off and warns about caveats. ant-supervisor skips `mcp` and sets the off snippet when `ui.mcp` is false. Tests: `tests/test_ant_stack_ui.py`. |
| `ant-cron-run` | POSIX sh wrapper for cron/systemd: adds `~/.local/bin` etc. to PATH, exits 127 with a clear message when uv is missing, then execs the command. Test: `tests/test_library_site_modes.py`. |
| `ant-supervisor` | Runs the full stack as host processes (dev boxes, the `all` single-container mode) (api+web+mcp+caddy+authelia), multiplexes their stdout/stderr with colored labels, auto-restarts crashed children. `restart <name>` / `status` / `stop` subcommands. |
| `ant-sync` | Push/pull `.h5ad` and `.duckdb` files to/from S3 (boto3, mtime-aware, `_`-folder-aware). Never touches `.md` files. Also runs the two-way paper library sync (`ant sync library`: records via git, PDFs via S3, literature index) via `lib/library_sync.py`; `--no-library` / `--library-only`. |
| `ant_s3` | Lower-level S3 toolkit (credentials, list, status). Mostly invoked via `ant-sync`. |
| `ant-build-reports` | Pull static report sites from the git repos listed in `anthive.yaml` → `data/reports/`. Served by Caddy at `/report/<slug>/`. |
| `ant-inactive` | Mark/unmark a dataset as inactive (drops a `.inactive` sidecar; hides it from `ant-serve`). |
| `ant-db` | DuckDB inspection / one-off queries against an h5adstore database. |
| `dev-jupyter` | Start a local JupyterLab against `frontend/jupyter/jupyter` for developing the JupyterLite notebooks. |

The Streamlit dashboard was removed 2026-06-22 along with
`dev-serve` / `streamlit-serve` / `sync-to-stlite`. The interactive
plot UI now lives in `web/templates/plot.html` (`./web/ant-browse`).

## ant-supervisor — the bits that bite

- **Hosts/ports come from `anthive.yaml`** (`rest_api_host`, `rest_api_port`,
  `browse_host`, `browse_port`). Default is `localhost`; flip to `0.0.0.0`
  if you need direct LAN access without Caddy.
- **`restart <name>`** SIGTERMs the child; waits 3s; SIGKILLs if it
  hasn't exited. Caddy in particular doesn't honour SIGTERM (eternal
  grace period); the SIGKILL fallback handles that case.
- **Respawn loop always brings daemons back**, regardless of exit code. To
  stop a service permanently, stop the whole supervisor (`make serve-stop`).
- **Caddy authentication** comes from the `ANTHIVE_AUTH_SNIPPET` the
  Caddyfile imports (`caddy-auth.dev` = Authelia, `caddy-auth.prod` =
  trust the upstream proxy, `caddy-auth.none`). Basic auth exists only in
  the container stack, rendered from `site.yaml` by `ant-stack`
  (`auth.mode: basic`, `doc/DEPLOY_CONTAINERS.md` "Login modes").
- **`--prod` / `ANTHIVE_PROD=1`**: switches Caddy to listen on
  `http://127.0.0.1:8500` (no TLS) so an upstream nginx can terminate
  TLS + Authelia gate. Sets the env vars `ANTHIVE_LISTEN`,
  `ANTHIVE_TLS_SNIPPET`, `ANTHIVE_AUTH_SNIPPET`, `ANTHIVE_HTTP_REDIRECT_SNIPPET`,
  and `ANTHIVE_CADDY_BIND=127.0.0.1` consumed by `config/Caddyfile`.
  Default (no flag) preserves dev behaviour byte-for-byte: TLS-internal
  on :8501, local Authelia forward_auth, :8500 → :8501 redirect block on.
  See `doc/DEPLOY.md` for the full prod recipe.

## ant-sync — what it pushes

`ant-sync push [glob]` — `.h5ad` + `.duckdb` only (never `.md`, never
`.h5ad.mus`). Uses S3 mtime metadata to detect drift on identically-sized
files. Every pushed `.h5ad` gets `x-amz-meta-sha256` (computed from the file), which the deposit API needs
to reuse a published h5ad for a metadata-only update.

`ant-sync stamp-sha256 [glob] [--dry-run]` backfills that metadata on objects pushed before it existed:
`stamp_plan` (stamp only when the local file has the object's size and ETag, `lib/s3_etag.py`, and agrees with
the sha256 in the `.md`/`.mus`), `stamp_apply` (in-place `copy_object` with `MetadataDirective: REPLACE` and
`CopySourceIfMatch`, managed copy above 5 GB, verified by a HEAD). Primary only, never deletes. Tests:
`tests/test_ant_sync_stamp.py`.

`ant-sync pull` — h5ad only by default (plus `_facet_tests/` results). `.duckdb` is treated as derived
data; rebuild it locally with `make build-stale`.

`--dry-run` is honoured on every path (2.46.1): pull (priority driver, `--only`, `--include-duckdb`, the
`duckdb`/`all` modes), push, evict, plan and the library; no download, upload, delete, mtime change, ant-serve
call or h5adstore `git pull` (`plan` is read-only too: it plans with the priorities of the current checkout). Under `sync_policy.mode: priority` the only
bypass is `--only`; `--include-duckdb` runs the priority driver. The driver (`_run_priority`) lists the whole
bucket once and splits it (`_split_priority_index`): datasets = `.duckdb` + sidecars (`BUNDLE_SUFFIXES`,
owner by longest stem, `_bundle_owner`; sizes count towards the budget; evicted together), always-mirrored
shared files (`_is_priority_global`: root files, `SYNC_PATH_PREFIXES`, `static/`), never `.h5ad`. Small
same-size files newer on S3 are re-pulled (`CHEAP_REPULL_BYTES`). Only an exact stem bundles a sidecar
(`_sidecar_stem`; `foo.sub.de.parquet` without `foo.sub.duckdb` is left alone). On a primary (`is_slave` false) the plan
(`_build_priority_plan(primary=True)`, `_global_plan(primary=True)`) turns every overwrite, mtime re-stamp and eviction
of a sidecar or shared file into KEEP (they may be unpushed work), leaves their bytes out of the budget, and keeps a
dataset with a KEEP sidecar whole in its local version (row status KEEP); duckdbs otherwise as before. min_free is
checked against the real free space before every download, `--evict` or not. Downloads run in try/finally so a released duckdb is always restored
and the catalog rescanned, also after a failed download. A generic pull that overwrites a `.duckdb`
releases it first and rescans after. The ant-serve URL is `config.get_api_url` (`ANTHIVE_API_URL` / `api_url`);
a compose-only host such as `http://api:8080` used outside a container is warned about once. Tests:
`tests/test_ant_sync_dry_run.py` (recording fake S3 that allows only list/HEAD).

`ant-sync library` runs the two-way paper library sync, also run by push and pull
unless `--no-library`: records via `git pull --rebase` (+ push with
`library_git_push`, never forced), PDFs against S3 through the manifest
state table in `lib/library_sync.py` (`plan_pdf_sync`, conflict copies,
deletes only with `--propagate-deletes`, `--prefer local|remote`,
`--dry-run`), then `ant lib-index --add` and the index publisher rule
(`library_index_publisher`). `library_index_pull: false` stops the download of the shared index; a library folder without its own `.git` never gets a git command (`git_sync` skips it). Read-only (slave) sites only download. Tests:
`tests/test_library_upload.py` (fake S3).

S3 credentials come from `anthive.yaml`:
```yaml
s3:
  endpoint: null            # null = AWS S3; else MinIO/R2/etc URL
  region: us-east-1
  bucket: anthive
  access_key_id: ...
  secret_access_key: ...
```

## Dev helpers (`dev-jupyter`)

Convenience script for developing the JupyterLite notebooks. Not part
of the production runtime path — for that, use `ant-supervisor`.
