# Deploying anthive4

End-to-end deployment guide covering two shapes.

1. Fresh single-host install on a clean Ubuntu-ish box (sections 1 through 7).
2. Multi-copy topology where a **main** install serves the full dataset
   catalogue and one or more **derivatives** serve subsets via symlinks
   (sections 8 through 12).

Sister docs:

- `doc/INSTALL.md` covers the same ground with an alternative angle
  (bare-API-first, then reverse proxy). Some passages there are older; where
  the two disagree, this file wins.
- `doc/NEW_INSTANCE.md` is the legacy per-instance recipe for hosts that
  already run system nginx. It stays as a reference for that pattern.
- `doc/HANDOFF_authelia.md` is the self-contained Authelia walkthrough.
  Read that before wiring auth.

---

## 1. Prerequisites on the host

Required.

- Linux x86_64. Tested on Debian 12 and Ubuntu 22.04 or newer.
- Python 3.10 or later for the system tooling. The per-script venvs use
  Python 3.11 via uv, so the system Python is only needed to run `uv` itself.
- **[uv](https://docs.astral.sh/uv/)**. Every `ant-*` script is a
  `uv run --script` PEP-723 shebang. Install once:

  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

- **git** with SSH access to the data repos (`badslab/h5adstore` and any
  geneset repo you use).
- **micromamba** for the `anthive-data` env. Only needed if you plan to
  run ingest, scoring, or DE tools on this host. Not needed for a
  serve-only install. Install per
  <https://mamba.readthedocs.io/en/latest/installation/micromamba-installation.html>.
- Disk. The repo itself is small (<200 MB). The h5adstore is the heavy
  bit. Full catalog as of 2026-09 is roughly **500 GB** of `.h5ad` plus
  `.duckdb` and the parquet sidecars. Budget accordingly. A serve-only
  derivative that symlinks the main's files needs a few GB of headroom
  for the `.md` files and DuckDB spill.
- A non-root user that owns the install. `anthive` on most deployments.
  All services run as this user, no root at runtime.

Recommended.

- **Caddy 2.x**. The repo ships a working `config/Caddyfile` plus a
  bundled binary at `envs/caddy/caddy` (built with the `replace_response`
  module for a small piece of Authelia CSS injection).
- **nginx** if the host already fronts other vhosts. In that case anthive
  runs as a thin loopback shim (see section 5).
- **Authelia** for auth. The supervisor already knows how to launch it
  from `envs/authelia/authelia`. Setup detail is in
  `doc/HANDOFF_authelia.md`.

Optional secrets and configs on the host.

- `~/.synapseConfig` if any dataset comes from a Synapse-gated study.
  Only ingest tools need this; a derivative that symlinks does not.
- `~/.pypirc` if you plan to release pyvark from this host. Not needed for
  serving.
- S3 credentials for Hetzner (or wherever your object storage lives) go
  into `anthive.yaml`. See section 3.

---

## 2. Clone the three repos

anthive4 hosts the code. The h5adstore and (optionally) the genesetstore
are separate git repos because their heavy files are too big for git. The
`.md` files in those repos ARE tracked; the `.h5ad`, `.duckdb`, and
parquet sidecars are synced to S3 via `bin/ops/ant-sync` (never committed).

```bash
# Pick a base path. Convention: /data/anthive or /opt/anthive.
BASE=/data/anthive
sudo mkdir -p "$BASE" && sudo chown "$USER" "$BASE"
cd "$BASE"

# Code.
git clone git@github.com:badslab/anthive4.git

# Data. The h5adstore repo's `.gitignore` is `*` at the leaves, so the
# clone is fast (only .md + index files land locally).
git clone git@github.com:badslab/h5adstore.git

# Genesets. Optional if you don't use gene-set scoring.
git clone git@github.com:badslab/genesetstore.git

# Wire the data stores into the anthive tree.
cd anthive4
ln -s ../h5adstore    data/h5adstore
ln -s ../genesetstore data/genesetstore    # optional
```

After the clone, `data/h5adstore/` contains one folder per collection with
`.md` files and often an `index.md` at the folder root. The heavy files
come later via `ant-sync pull`.

---

## 3. Configure `anthive.yaml`

```bash
cd "$BASE/anthive4"
cp anthive.default.yaml anthive.yaml
$EDITOR anthive.yaml
```

`anthive.yaml` is gitignored (instance-local). Minimum keys to set for a
fresh install:

```yaml
# Bind loopback when a reverse proxy on this host fronts public access.
# Set to 0.0.0.0 only if you want direct LAN access without Caddy or nginx.
rest_api_host: localhost
rest_api_port: 8080
browse_host:   localhost
browse_port:   8081

# DuckDB memory. Leave 'auto' on a fresh box; revisit if RSS climbs.
duckdb_memory_limit: auto

# S3 for the data layer. Required if you'll be running ant-sync.
# AWS: leave endpoint null. Hetzner / MinIO / R2 / B2: set endpoint.
s3:
  endpoint: fsn1.your-objectstorage.com
  region: eu-central
  bucket: anthive4a
  access_key_id: AKIA...
  secret_access_key: ...
  prefix: ""
  use_path_style: true    # true for Hetzner and most non-AWS backends
```

For derivatives, add the `sync_policy` block from
`anthive.default.yaml` and set `role: slave` so annotation writers and
`sync push` refuse to run against the shared store. More on this in
section 8.

The chat assistant, geneset store path, and the report-site list all have
sensible defaults in `anthive.default.yaml`. Copy over the ones that apply
to your instance.

---

## 4. Pull the data

The h5adstore is canonical on S3. `ant-sync pull` copies the files this
host actually needs.

```bash
cd "$BASE/anthive4"

# Full catalog. Watch disk: current fleet is ~500 GB.
./bin/ops/ant-sync pull

# One collection only.
./bin/ops/ant-sync pull 'Sierksma2025/**'

# See what pull would touch without transferring.
./bin/ops/ant-sync status
```

Defaults pull `.h5ad`, `.de.parquet`, `.scores.parquet`, plus the
fleet-wide `module_variability.parquet`, `scores_index.parquet`,
`gene_stats_index.parquet`, and `gene_variance_index.parquet` (each
with its per-dataset cache prefix so incremental updates can reuse
cached rows: `_gene_stats_cache/`, `_gene_variance_cache/`,
`_module_variability_cache/`).
`.duckdb` and the `.layer.*.parquet` expression sidecars are treated as
derived; rebuild them locally via `make build-stale` (or pull them for
disaster recovery with `ant-sync pull --include-duckdb`).

For a derivative that symlinks the main's files, skip this step entirely
and go to section 8.

---

## 5. Build the `anthive-data` micromamba env (only if ingesting)

The serve-only path does NOT need this. It's for tools that touch h5ad or
run scoring: `ant-convert`, `ant-merge-h5ad`, `ant-score`, `ant-starcat`,
`ant-module-variability`, the DE ingest scripts, and anything that runs
R + Seurat for `.rds` import.

```bash
cd "$BASE/anthive4"
micromamba create -y -p envs/anthive-data -f envs/anthive-data.yml
```

The env definition ships in the repo at `envs/anthive-data.yml` and
includes R + Seurat, anndata, scanpy, and the numeric stack. It builds
once, then per-script uv shebangs pick it up when needed.

Derivatives that only serve don't need any of this. `ant-serve` reads
DuckDB directly and has no heavy scientific dependencies.

---

## 6. Reverse proxy

Two shapes. Pick one.

### 6a. Caddy alone (single-host dev box or no other vhosts)

Caddy terminates TLS on `:8501` with `tls internal` (self-signed by
Caddy's local CA). Users accept the cert warning once, or run
`caddy trust` on the server to install the CA into the OS trust store.

The Caddyfile is already correct out of the box. To reach the box over a
LAN address, add the address to the site block header in
`config/Caddyfile`, or export `ANTHIVE_CADDY_BIND` before start.

Start under the supervisor:

```bash
cd "$BASE/anthive4"
./bin/ops/ant-supervisor --caddy
# or nohup for a persistent run:
nohup ./bin/ops/ant-supervisor --caddy > /tmp/ant-supervisor.log 2>&1 &
```

Smoke test:

```bash
curl -k https://localhost:8501/api/version | python3 -m json.tool
```

### 6b. Nginx in front of Caddy (multi-vhost host)

If the box already runs nginx for other vhosts (alzmap.org, data.badslab
sites, and so on), do NOT duplicate the anthive routing in nginx. Use
this pattern:

```
client ──https──> nginx :443 (SSL term + Authelia auth_request)
                    │  proxy_pass http://127.0.0.1:8500
                    ▼
                Caddy on 127.0.0.1:8500 (plain HTTP, loopback only)
                    │
                    ├── /api/*        → ant-serve   :8080
                    ├── /browse/*     → ant-browse  :8081
                    ├── /mcp*         → ant-mcp     :8090
                    ├── /authelia/*   → authelia    :9091
                    ├── /jupyter/*    → static
                    ├── /artifacts/*  → static
                    └── / fallback    → ant-browse  :8081
```

The nginx to Caddy hop is loopback (~1 ms). The benefit is that
`config/Caddyfile` is the single source of path routing, identical on dev
and prod. Adding or renaming a path means editing `config/Caddyfile`
only. Nginx never changes after this deploy.

The prod-mode env-var split that drives `config/Caddyfile`:

| Env var                          | Dev default                 | Prod value                  |
| -------------------------------- | --------------------------- | --------------------------- |
| `ANTHIVE_LISTEN`                 | `:8501`                     | `http://127.0.0.1:8500`     |
| `ANTHIVE_TLS_SNIPPET`            | `caddy-tls.dev`             | `caddy-tls.prod` (empty)    |
| `ANTHIVE_AUTH_SNIPPET`           | `caddy-auth.dev`            | `caddy-auth.prod` (empty)   |
| `ANTHIVE_HTTP_REDIRECT_SNIPPET`  | `caddy-http-redirect.dev`   | `caddy-http-redirect.prod` (empty) |
| `ANTHIVE_CADDY_BIND`             | `0.0.0.0 ::`                | `127.0.0.1`                 |

`ant-supervisor --prod` sets all of them. Without `--prod` the dev
defaults apply and behaviour is unchanged.

Copy the slim nginx vhost:

```bash
sudo cp config/nginx-intern-anthive.conf \
        /etc/nginx/sites-available/anthive.conf
sudo ln -sf /etc/nginx/sites-available/anthive.conf \
            /etc/nginx/sites-enabled/anthive.conf
```

Adjust cert paths and the `server_name` at the top of the file. If a
sprawling per-path anthive vhost existed before this deploy, remove it
now. The new slim vhost replaces it entirely.

Test and reload:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

Start the supervisor in prod mode:

```bash
cd "$BASE/anthive4"
nohup ./bin/ops/ant-supervisor --prod --caddy > /tmp/ant-supervisor.log 2>&1 &
```

Verify:

```bash
# Direct to Caddy on loopback.
curl -sv http://127.0.0.1:8500/api/health -H 'Remote-User: smoke-test'
# → HTTP/1.1 200 with a JSON body listing databases.

# The MCP discovery stub (proves Caddy's routing layer is alive).
curl -s -o /dev/null -w 'HTTP %{http_code}\n' \
    http://127.0.0.1:8500/.well-known/oauth-authorization-server
# → HTTP 404 with a JSON body.

# Through nginx.
curl -sv https://anthive.example.org/api/health
```

If the loopback check fails, fix BEFORE wiring nginx. The failure mode is
much harder to diagnose through nginx.

---

## 7. Authelia (auth)

`doc/HANDOFF_authelia.md` is the canonical Authelia walkthrough. It
covers binary install, per-host secret generation, `configuration.yml`
placeholders, first-user seeding via `bin/ops/ant-adduser`, both the
Caddy and nginx wiring, and the common gotchas (the `localhost` cookie
domain trap, the basic-auth ban trap, the TLS-required cookie URL).

Key headers the app depends on, set by the proxy after Authelia says
"allow" on a `forward_auth` (Caddy) or `auth_request` (nginx) call:

- `Remote-User` (login, no spaces)
- `Remote-Name` (display name)
- `Remote-Email`
- `Remote-Groups` (comma-separated; `admins` gates the admin editors)

The web UI's topbar reads these to render the signed-in user and the
sign-out action. Un-proxied direct access to `:8081` shows nothing there,
which is correct.

Never add HTTP basic auth to browser routes. Browsers resend stale creds
and trip Authelia's rate-limiter and ban. The API path in
`config/caddy-auth.dev` and the equivalent nginx location have a
cookie-first-then-basic split that keeps CLI clients happy without
banning users.

---

## 7a. Personal MCP links through the proxy

Users connect AI clients with a personal link
(`/mcp/u/amcp_<id>_<mac>`, or `Authorization: Bearer amcp_...` on `/mcp`).
ant-mcp verifies the token itself (against ant-serve's
`/internal/mcp/verify`, using the shared internal secret), so the proxy
must let these requests through **without** the Authelia gate. User docs:
`doc/MCP_LINKS.md`.

`config/Caddyfile` already does this on every host:

- `/mcp/u/*` and `/mcp` with a `Bearer amcp_` header skip the cookie gate
  (`@browser` matcher in `config/caddy-auth.dev`); other `/mcp` requests
  stay gated;
- `/api/internal/*` answers 403 from outside;
- client-supplied `X-Anthive-Internal` is stripped; `Remote-*` headers are
  stripped on `/mcp*` (on gated routes `forward_auth copy_headers` already
  replaces client values);
- the access log (`ANTHIVE_CADDY_LOG`, default `./tmp/caddy.log`) redacts
  `amcp_` tokens, `Authorization` and `Cookie`.

**Prod with nginx in front** (6b): nginx runs `auth_request` itself, so it
needs one extra location that proxies `/mcp` to Caddy without the gate
and without logging the token:

```nginx
# Mask personal MCP link tokens in the access log.
map $request_uri $anthive_log_uri {
    ~^(?<mcp_prefix>/mcp/u/)amcp_  "${mcp_prefix}amcp_REDACTED";
    default                        $request_uri;
}
log_format anthive_masked '$remote_addr - $remote_user [$time_local] '
                          '"$request_method $anthive_log_uri $server_protocol" '
                          '$status $body_bytes_sent "$http_referer" "$http_user_agent"';

server {
    # ... existing server_name / TLS / auth_request setup ...
    access_log /var/log/nginx/anthive.access.log anthive_masked;

    # Personal MCP links: ant-mcp authenticates; no auth_request here.
    location /mcp {
        proxy_pass http://127.0.0.1:8500;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        # never forward client-supplied identity or internal headers
        proxy_set_header Remote-User "";
        proxy_set_header Remote-Groups "";
        proxy_set_header Remote-Name "";
        proxy_set_header Remote-Email "";
        proxy_set_header X-Anthive-Internal "";
        # MCP streamable HTTP uses long-lived SSE responses
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }
    location /api/internal/ { return 403; }
}
```

Also make sure every gated nginx location **overwrites** `Remote-*` from
the `auth_request` response (`auth_request_set` + `proxy_set_header`) so a
client can never supply its own; `caddy-auth.prod` trusts whatever nginx
sends.

Set `mcp.public_base_url` in `anthive.yaml` to the public origin (for
example `https://anthive.example.org`) so the web UI shows the right link
even if a hop rewrites `Host`.

Smoke test after deploy (replace the token with one from the UI):

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://anthive.example.org/mcp
# -> 302 (dev Caddy) or 401 (prod nginx path): no token, no entry
curl -s -X POST https://anthive.example.org/mcp/u/amcp_... \
     -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
# -> 200 with serverInfo "anthive"
```

---

## 8. Systemd

The supervisor should run under systemd on production hosts so it
survives reboots and restarts cleanly on crash.

```ini
# /etc/systemd/system/anthive.service
[Unit]
Description=Anthive supervisor (prod, Caddy on 127.0.0.1:8500, nginx in front)
After=network.target

[Service]
Type=simple
User=anthive
WorkingDirectory=/data/anthive/anthive4
ExecStart=/data/anthive/anthive4/bin/ops/ant-supervisor --prod --caddy
ExecStop=/data/anthive/anthive4/bin/ops/ant-supervisor restart all
Restart=on-failure
RestartSec=5

# uv must be on PATH for the per-script PEP-723 shebangs.
Environment=PATH=/home/anthive/.local/bin:/usr/local/bin:/usr/bin:/bin

# Prod-mode env split.
Environment=ANTHIVE_PROD=1

# Optional: pin Caddy to specific interfaces on multi-homed boxes.
# Environment=ANTHIVE_CADDY_BIND=127.0.0.1 10.0.0.10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now anthive.service
sudo systemctl status anthive.service
journalctl -u anthive.service -f
```

For the "Caddy alone, no nginx" shape, drop `--prod` from `ExecStart`.
Caddy then listens on `:8501` with `tls internal` and its own auth.

Nginx has its own systemd unit already. Just make sure the anthive vhost
is in `/etc/nginx/sites-enabled/`.

For hosts that need per-instance nginx routing (the legacy shape rather
than the thin shim), `doc/NEW_INSTANCE.md` remains the reference. Don't
mix the two on the same vhost.

Rollback if something goes wrong:

```bash
sudo systemctl stop anthive.service
sudo rm /etc/nginx/sites-enabled/anthive.conf
sudo nginx -t && sudo systemctl reload nginx
```

You should be back to the pre-deploy state.

---

## 9. Multi-copy topology (main plus derivatives)

The "main" install is the full replica. Every dataset lives there, on
disk, backed by S3. It's the source of truth for the code repo, the data
store, and the fleet-wide summary parquets.

A "derivative" is a **second (or third) anthive install** that serves a
**subset** of the main's datasets. Each derivative has:

- Its own anthive4 checkout.
- Its own `data/h5adstore/` folder.
- Its own `ant-serve` and `ant-browse` on their own ports and vhost.
- Its own Authelia, since Authelia is per-instance by design (see
  `doc/HANDOFF_authelia.md`).
- Its own `module_variability.parquet` and `scores_index.parquet`,
  filtered from the main's copies.

The derivative's `data/h5adstore/<Group>/` folders contain **symlinks**
back to the main install's files. The `.h5ad` file is deliberately NOT
symlinked. `ant-serve` reads only the DuckDB, so a serve-only derivative
never touches the h5ad and doesn't need it on local disk.

```
main:      /data/anthive/anthive4/data/h5adstore/Group/foo.duckdb   (real file)
                                                    foo.h5ad        (real file)
                                                    foo.scores.parquet
                                                    foo.md
                                                    static/
                                                    .env

derivative: /data/anthive-microglia/anthive4/data/h5adstore/Group/foo.duckdb
              → symlink to /data/anthive/.../Group/foo.duckdb
            foo.scores.parquet
              → symlink
            foo.md
              → symlink
            static/
              → symlink
            .env
              → symlink
            (foo.h5ad intentionally absent)
```

Anything that requires the h5ad (mainly `ant-merge-h5ad`, `ant-convert`,
and full re-scoring) MUST NOT run on the derivative. Set
`sync_policy.role: slave` in the derivative's `anthive.yaml` and the
annotation writers will refuse.

---

## 10. Symlinking a dataset into a derivative

For each dataset the derivative wants to include, symlink these files
from the main install into the derivative's tree:

- `<basename>.duckdb`
- `<basename>.layer.*.by_cell.parquet`
- `<basename>.layer.*.by_gene.parquet`
- `<basename>.scores.parquet`
- `<basename>.md`
- `<basename>.h5ad.mus`

Then, once per group folder:

- `index.md`
- `.env` (folder-level mus cascade entry point)
- `static/` (the whole folder; paper PDFs and supplementary tables)

DO NOT symlink `<basename>.h5ad`. The derivative doesn't need it.

A small bash loop that operators can adapt:

```bash
MAIN=/data/anthive/anthive4/data/h5adstore
DERIV=/data/anthive-microglia/anthive4/data/h5adstore
GROUP=Sierksma2025

# Datasets in this group that we want on the derivative. Empty = all.
BASENAMES=(Village.6M Village.12M Grafts.Adult)

mkdir -p "$DERIV/$GROUP"

# Group-level files (once per group).
for name in index.md .env static; do
    src="$MAIN/$GROUP/$name"
    dst="$DERIV/$GROUP/$name"
    [ -e "$src" ] && ln -sfn "$src" "$dst"
done

# Per-dataset files.
for base in "${BASENAMES[@]}"; do
    for ext in .duckdb .scores.parquet .md .h5ad.mus; do
        src="$MAIN/$GROUP/${base}${ext}"
        dst="$DERIV/$GROUP/${base}${ext}"
        [ -e "$src" ] && ln -sfn "$src" "$dst"
    done
    # Layer parquets (there are usually two per layer: by_cell and by_gene).
    for f in "$MAIN/$GROUP/${base}".layer.*.parquet; do
        [ -e "$f" ] && ln -sfn "$f" "$DERIV/$GROUP/$(basename "$f")"
    done
done
```

Two warnings.

Absolute paths keep the derivative working across moves within the host,
but they break if the main install ever migrates to a new path. Pick one
convention (absolute or relative) and use it consistently. Relative
symlinks (`ln -sr` on GNU coreutils, or hand-compute the `..` traversal)
survive host-path renames as long as main and derivative move together.

Symlinks make eviction on the main install invisible to derivatives. If
you `ant-sync pull` on the main and it evicts a low-priority duckdb, the
derivative's symlink is now dangling. Prefer per-file symlinks over
`ln -s <group>/` at the folder level so a newly-added file on the main
does NOT auto-appear on the derivative unless you re-run the loop.

---

## 11. Per-derivative genesetstore

Each derivative has its own `data/genesetstore/`. Two options.

**Symlink the whole store to the main** (recommended default). Every
derivative sees every geneset the main knows about.

```bash
ln -sfn /data/anthive/genesetstore \
        /data/anthive-microglia/anthive4/data/genesetstore
```

**Copy or symlink a subset** if you want a derivative to expose only
certain catalogs. Create a fresh folder and symlink the specific
catalog subfolders you want. `ant-serve` walks the store and picks up
whatever's there.

---

## 12. Per-derivative summary parquets

Two fleet-wide summary parquets live at the root of `data/h5adstore/`.
They're computed across **whatever datasets are in this install's
h5adstore**, so a derivative needs its own subset. Options for each:

- Rerun the tool on the derivative. Correct, slow (hours for the full
  walk on a large catalog), and requires the anthive-data env.
- Filter the main's parquet down to the derivative's dataset list. Fast
  (seconds). Correct because rows are keyed on `dataset` (the `db_id`
  string, e.g. `S25/PhM`).

Filter is the default for both.

### 12a. module_variability.parquet

Schema is `dataset, source, catalog, module, tag_column, tag, statistic,
value, n_cells, n_categories`. `dataset` is the compact `db_id` string
that `ant-serve` uses (like `AZ5/SLM`, `S25/PhM`).

Get the derivative's dataset list. One reliable source is the running
main's REST API:

```bash
# On the main.
curl -s http://127.0.0.1:8080/databases | python3 -c \
    'import json, sys; [print(r["id"]) for r in json.load(sys.stdin)["databases"]]'
```

The subset the derivative wants is a subset of that list. Save it as a
plain text file (one `db_id` per line):

```bash
cat > /tmp/derivative_datasets.txt <<'EOF'
S25/PhM
S25/Vlg
AZ5/SLM
...
EOF
```

Filter the main's MV parquet into the derivative's spot:

```bash
duckdb -c "
COPY (
  SELECT * FROM read_parquet('/data/anthive/anthive4/data/h5adstore/module_variability.parquet')
  WHERE dataset IN (
    SELECT column0 FROM read_csv('/tmp/derivative_datasets.txt',
                                 columns={'column0': 'VARCHAR'}, header=false)
  )
) TO '/data/anthive-microglia/anthive4/data/h5adstore/module_variability.parquet' (FORMAT parquet);
"
```

To rebuild from scratch on the derivative instead (only if you don't
trust the main's copy):

```bash
cd /data/anthive-microglia/anthive4
./bin/annot/ant-module-variability --h5adstore-root ./data/h5adstore \
    --out data/h5adstore/module_variability.parquet
```

That walks the derivative's h5adstore (following the symlinks), reads
each `.scores.parquet`, and writes the aggregate. Same tool, same
schema.

### 12b. scores_index.parquet

Same filter approach. Schema is `dataset, group, basename, source,
catalog, module, n_cells, mean, std, min, p50, max`. `dataset` is the
same `db_id` string.

```bash
duckdb -c "
COPY (
  SELECT * FROM read_parquet('/data/anthive/anthive4/data/h5adstore/scores_index.parquet')
  WHERE dataset IN (
    SELECT column0 FROM read_csv('/tmp/derivative_datasets.txt',
                                 columns={'column0': 'VARCHAR'}, header=false)
  )
) TO '/data/anthive-microglia/anthive4/data/h5adstore/scores_index.parquet' (FORMAT parquet);
"
```

### 12c. gene_stats_index.parquet

Precomputed per-(dataset, gene) statistics for a "hot" gene set (union
of top-N variable genes per dataset, plus any starred/favorite genes).
Serves `/api/gene_stats_all` in sub-second time; falls back to a live
`__genes` scan for any gene not in the parquet. Schema is
`dataset, gene, layer, n_cells, n_nonzero, pct_nonzero, mean, std, cv,
mean_nonzero, std_nonzero, median_nonzero, q10_nonzero, q90_nonzero,
max, mean_expr_layer, rank_mean, scaled_mean`.

Same filter approach for a derivative:

```bash
duckdb -c "
COPY (
  SELECT * FROM read_parquet('/data/anthive/anthive4/data/h5adstore/gene_stats_index.parquet')
  WHERE dataset IN (
    SELECT column0 FROM read_csv('/tmp/derivative_datasets.txt',
                                 columns={'column0': 'VARCHAR'}, header=false)
  )
) TO '/data/anthive-microglia/anthive4/data/h5adstore/gene_stats_index.parquet' (FORMAT parquet);
"
```

Or rebuild from the derivative's h5adstore:

```bash
cd /data/anthive-microglia/anthive4
./bin/annot/ant-gene-stats-precompute --h5adstore-root ./data/h5adstore \
    --out data/h5adstore/gene_stats_index.parquet
```

The tool caches per-dataset stats in `_gene_stats_cache/<db_id>/` keyed
on the max mtime of the duckdb, layer parquet, and .md, so subsequent
runs after a single-dataset ingest only recompute for that dataset.

### 12d. gene_variance_index.parquet

Precomputed per-(dataset, gene, facet) between-group variance for the
same hot gene set as 12c. Serves `/api/databases/{db_id}/gene-variance-by-facet`
in sub-second time; falls back to a live DuckDB scan for any
(dataset, facet) triple not represented in the parquet. Schema is
`dataset, gene, facet, facet_type, layer, n_cells, n_groups,
eta_squared, F_stat, signal, total_var, group_means_json`. `signal`
is `VAR_SAMP` over group means (the endpoint's ranking metric);
`eta_squared` is SS_between / SS_total; `F_stat` is one-way F.
Numerical facets are NTILE(5)-binned into `q1..q5`; categorical facets
with > 50 distinct values are skipped as uninformative for
group-variance (donor ids, cell barcodes).

Same filter approach for a derivative:

```bash
duckdb -c "
COPY (
  SELECT * FROM read_parquet('/data/anthive/anthive4/data/h5adstore/gene_variance_index.parquet')
  WHERE dataset IN (
    SELECT column0 FROM read_csv('/tmp/derivative_datasets.txt',
                                 columns={'column0': 'VARCHAR'}, header=false)
  )
) TO '/data/anthive-microglia/anthive4/data/h5adstore/gene_variance_index.parquet' (FORMAT parquet);
"
```

Or rebuild from the derivative's h5adstore (needs `gene_stats_index.parquet`
already in place — the hot set is sourced from there):

```bash
cd /data/anthive-microglia/anthive4
./bin/annot/ant-gene-variance-precompute --h5adstore-root ./data/h5adstore \
    --out data/h5adstore/gene_variance_index.parquet
```

Caches land under `_gene_variance_cache/<db_id>/genevar.parquet` keyed
on the max mtime of the duckdb, layer parquet, and .md — subsequent
runs after a single-dataset ingest only recompute for that dataset.

After any of these four (12a/12b/12c/12d) filters, restart the
derivative's `ant-serve` so it picks up the new parquets:

```bash
./bin/ops/ant-supervisor restart api
```

---

## 13. Day-2 ops

### Update code on a derivative

Same as the main. Pull, restart.

```bash
cd /data/anthive-microglia/anthive4
git pull origin main
./bin/ops/ant-supervisor restart all
```

The derivative doesn't own an h5adstore commit history separate from the
main's; only its `anthive.yaml` and Authelia config are instance-local.

### Add a new dataset to a derivative

Once the dataset is ingested on the main (via the usual `ant-convert`
plus `ant-sync push` cycle), the derivative catches up in two steps.

1. Add the symlinks. Re-run the loop from section 10 with the new
   basename.
2. Rebuild the two summary parquets by rerunning the filter from
   section 12 with the extended dataset list.
3. Restart the derivative's `ant-serve` so it rescans and picks up the
   new folder entries:

   ```bash
   ./bin/ops/ant-supervisor restart api
   # or trigger a rescan without restart:
   curl -X POST http://127.0.0.1:8080/admin/rescan
   ```

### Remove a dataset from a derivative

Delete the derivative's symlinks for that basename, refresh the two
summary parquets (rerun the filter with the shrunken list), then rescan
`ant-serve`. The main is untouched.

```bash
cd /data/anthive-microglia/anthive4/data/h5adstore/Sierksma2025
rm -f Village.6M.duckdb Village.6M.scores.parquet Village.6M.md \
      Village.6M.h5ad.mus Village.6M.layer.*.parquet
# then rebuild module_variability and scores_index parquets, and rescan.
```

### When the main evicts a dataset

If the main's disk fills and `ant-sync pull --priority` evicts a duckdb
that a derivative still symlinks, the derivative's symlink is now
dangling and `ant-serve` on the derivative will log a load failure for
that entry.

Either pin the priority higher on the main (`bin/ops/ant-priority set`),
or remove the symlink on the derivative and rebuild the summaries.
There's no automatic reconciliation across installs; the two catalogs
are coupled only by convention.

### Add a user

Both main and derivatives have their own Authelia and their own users
database.

```bash
./bin/ops/ant-adduser you@example.org -g admins
```

Prints a random password once. The user changes it in the portal's
Settings page after first login.

### Restart one service

```bash
./bin/ops/ant-supervisor restart api          # ant-serve
./bin/ops/ant-supervisor restart browse       # ant-browse (web UI)
./bin/ops/ant-supervisor restart caddy
./bin/ops/ant-supervisor restart authelia
./bin/ops/ant-supervisor restart all
```

Under systemd, do NOT use `ant-supervisor stop` for a permanent stop.
The unit's `Restart=on-failure` respawns the supervisor immediately. Use
`sudo systemctl stop anthive` to actually take it down.

---

## 14. Verify

```bash
curl -s http://localhost:8080/api/version | python3 -m json.tool
curl -s http://localhost:8080/api/health
curl -s http://localhost:8080/databases | python3 -c \
    'import json, sys; d = json.load(sys.stdin); print(f"{d[\"total\"]} datasets")'
```

Through the public vhost:

```bash
curl -sv https://anthive.example.org/api/version
```

If `/api/version` returns the current `API_VERSION` and `/api/health`
reports `databases > 0`, the install works. Then open the URL in a
browser and step through:

- [ ] Landing page loads. Datasets appear in the collection list.
- [ ] Click into a dataset. Plot UI works.
- [ ] `/jupyter/` loads JupyterLite with COOP/COEP headers so Pyodide
      boots.
- [ ] Generate a plot, click "notebook" on the plot toolbar, verify the
      JupyterLite deep-link opens.
- [ ] Pull a pseudobulk artifact from the API. File lands under
      `/artifacts/` and downloads cleanly.
- [ ] `/mcp` responds. Try
      `curl -H 'Accept: text/event-stream' https://anthive.example.org/mcp`
      while authenticated; expect a non-empty SSE stream.

For derivatives, run the same list on the derivative's hostname. It
should list the subset of datasets you symlinked and no more.

---

## 15. Common pitfalls

**Empty `data/h5adstore/`.** After a fresh clone the `.md` files are
there but the heavy files aren't. Run `ant-sync pull` (main) or add
symlinks (derivative). Until one of those runs, the API returns an empty
database list.

**DuckDB lock conflicts.** `ant-serve` holds duckdbs read-only via a
connection pool. Writers (`ant-convert`, `ant-score`, `ant-de-ingest`)
coordinate via `POST /admin/release/{db_id}` then
`POST /admin/restore/{db_id}`. A hung writer leaves the duckdb evicted
for 5 minutes. Kill the writer and call `restore` manually to recover.
On a derivative, none of this should happen because writers refuse under
`role: slave`.

**Dangling symlinks on a derivative.** If a main-side eviction wipes a
duckdb, the derivative's symlink is now broken. Pin priority on the main
or remove the derivative-side symlink and rebuild the summaries.

**`uv` not on PATH under systemd.** The unit above sets
`Environment=PATH=...` to include `~/.local/bin`. Verify with
`sudo -u anthive env | grep PATH`.

**Per-script PEP-723 deps cache cold.** The first time each `ant-*`
script runs, uv resolves and downloads its inline deps. This can take
60 to 90 seconds per script. Subsequent runs hit uv's cache and start in
under a second.

**`localhost` rejected as Authelia cookie domain.** Use
`localhost.localdomain` or an IP. See `doc/HANDOFF_authelia.md` for the
full list of Authelia gotchas.

**Basic-auth ban trap.** Never put HTTP basic auth in front of a browser
route while cookie SSO is also active. Browsers cache and resend basic
creds; Authelia treats each retry as a failed login and eventually bans
the user. `caddy-auth.dev` splits the API path (cookie-then-basic) from
the browser path (cookie only) for exactly this reason.

---

## Reference

- `config/Caddyfile` is the single source of path routing on dev and prod.
- `config/caddy-{tls,auth,http-redirect}.{dev,prod}` are env-toggled snippets.
- `config/nginx-intern-anthive.conf` is the drop-in slim prod vhost.
- `config/nginx.conf` is the local dev variant of the nginx shim.
- `doc/HANDOFF_authelia.md` covers Authelia end-to-end.
- `doc/NEW_INSTANCE.md` is the legacy per-instance recipe (nginx holds
  the routing; kept for hosts already on that pattern).
- `bin/ops/ant-supervisor` runs the stack, restarts crashed children,
  multiplexes stdout with colored labels.
- `bin/ops/ant-sync` pushes and pulls the heavy files.
- `bin/ops/ant-adduser` seeds and adds Authelia users.
- `bin/annot/ant-module-variability` rebuilds the MV parquet from
  scratch (slow, correct) as an alternative to filtering.
