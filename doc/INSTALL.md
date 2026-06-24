# Anthive deployment — install instructions

End-to-end recipe for getting an anthive stack up on a new host.
Two paths covered:

- **Bare-API quick start** (sections 1–4) — just the REST API + browse
  UI, served on `localhost` ports. Useful for a local dev box or a
  private machine on the LAN. ~15 minutes.
- **Production deploy** (sections 5–9) — adds the reverse proxy with
  basic auth, the supervisor as a systemd unit, S3 sync, JupyterLite
  static build, and report sites. ~1–2 hours, mostly waiting on data
  to land from S3.

Everything in this doc assumes Linux. macOS works for the dev path
(skip systemd; use `nohup` / `tmux` for supervision).

Sister docs:

- `CLAUDE.md` at the repo root — what's where in the codebase.
- `doc/ARCHITECTURE.md` — how the parts fit together.
- `doc/API_CHANGELOG.md` — REST API version history (relevant when
  pinning antclient).
- `doc/QUICKSTART.md` — covers the data-curation workflow once
  things are running.

---

## 1. Prerequisites on the host

Required:

- **Linux** (tested on Debian 12 / Ubuntu 22.04 — anything with glibc
  ≥ 2.31 is fine).
- **Python 3.10+** for system tooling. The per-script venvs use
  Python 3.11 via uv, so the system Python is only needed to run
  `uv` itself.
- **[uv](https://docs.astral.sh/uv/)** — all `ant-*` scripts are
  `uv run --script` PEP-723 shebangs; no shared venv, every script
  carries its own dependency list. Install once:

    ```sh
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```

- **git** with SSH access to the relevant data repositories
  (`badslab/anthive4`, `badslab/h5adstore`, and your geneset store).
- **A few hundred MB of free RAM** for the empty stack; **2–8 GB
  per active DuckDB connection** under load (the per-connection
  memory_limit is auto-derived as ~25% of system RAM divided across
  the pool — set explicitly in `anthive.yaml` if the default is too
  greedy or too tight).
- **Disk** — depends on the data set. The repo itself is small
  (<200 MB), the h5adstore repo is mostly `.md` files (a few MB),
  but the `.h5ad` + `.duckdb` data files sync from S3 are typically
  **hundreds of GB to a few TB**.

Recommended:

- **Caddy v2** (apt / official binary) — fronts the stack with basic
  auth and TLS. The repo ships a working `config/Caddyfile`.
- **Nginx** — alternative reverse proxy if you prefer it; the repo
  ships a working `config/nginx.conf` too.
- A non-root user that owns the install (`anthive` on most deployments).
  All services run as this user; no root needed at runtime.

Optional:

- **R + Bioconductor** — only needed if you want the (currently
  unimplemented) `ant-de-pseudobulk --engine deseq2-r` path. Default
  pydeseq2 engine is pure Python.
- **micromamba** — only if you maintain heavy data-management envs
  (e.g. for scanpy + scvi). Not needed for the served stack.

---

## 2. Clone + checkout

```sh
cd /opt
sudo mkdir anthive && sudo chown $USER anthive
cd anthive
git clone git@github.com:badslab/anthive4.git
cd anthive4
```

The repo conventionally lives at `/opt/anthive/anthive4` or
`/data/<user>/anthive4`. Anywhere works — the only path assumption
is the relative `./data/h5adstore` symlink set up in section 3.

---

## 3. Wire up the data layer

The data lives in **separate git repositories**, kept outside this
repo because the `.h5ad` / `.duckdb` files are too big for git. The
`.md` files in those repos are git-tracked; the heavy files come
from S3 via `ant-sync`.

```sh
cd /opt/anthive
git clone git@github.com:badslab/h5adstore.git
# If your project also maintains a geneset store:
git clone git@github.com:badslab/genesetstore.git    # adjust to your repo

# Symlink them into the anthive4 working tree at the expected paths:
cd anthive4
ln -s ../h5adstore     data/h5adstore
ln -s ../genesetstore  data/genesetstore     # optional
```

After this:

- `data/h5adstore/` contains `<Study>/<file>.md` files (tracked by
  git) plus possibly stale `.h5ad` / `.duckdb` / `.de.parquet` files
  (untracked, populated by `ant-sync pull`).
- `data/genesetstore/` similarly holds `.grp` / `.rnk` gene-list
  files. Optional — set `geneset_store: null` in `anthive.yaml` to
  disable the geneset endpoints if you don't have one.

---

## 4. Configure + first boot

Copy the template and fill in the bits that matter for this host:

```sh
cp anthive.default.yaml anthive.yaml
$EDITOR anthive.yaml
```

Minimum keys to set:

```yaml
# Where ant-serve and ant-browse bind. localhost is correct when the
# reverse proxy on this host fronts public access.  Set to 0.0.0.0
# only when you want direct LAN access without Caddy/nginx.
rest_api_host: localhost
rest_api_port: 8080
browse_host:   localhost
browse_port:   8081

# DuckDB memory: leave 'auto' on a fresh box, then revisit if RSS
# climbs uncomfortably. See doc/PERFORMANCE.md for the throttling model.
duckdb_memory_limit: auto

# S3 for the data layer (required if you'll be running ant-sync pull/push).
# AWS: leave endpoint null. MinIO / Cloudflare R2 / Backblaze B2 / Hetzner:
#   endpoint: https://your-endpoint
#   use_path_style: true   # for MinIO and many S3-compatibles
s3:
  endpoint: null
  region: us-east-1
  bucket: anthive
  access_key_id: AKIA...
  secret_access_key: ...
  prefix: ""
```

Then pull the data:

```sh
make sync-pull          # downloads .h5ad files (the source of truth)
make build-stale        # rebuilds .duckdb files from any h5ad that's newer
```

`make sync-pull` defaults to fetching `.h5ad` + `.de.parquet` (the
DE-result sidecar). The `.duckdb` files are derived and rebuilt
locally by `ant-convert` — that's what `build-stale` does. To pull
duckdbs from S3 instead of rebuilding (faster for disaster recovery
of a large store):

```sh
make sync-pull-all      # pulls .h5ad + .duckdb + .de.parquet
```

After this `data/h5adstore/` should contain the served files. Smoke
test by listing:

```sh
ls data/h5adstore/*/      # a list of studies, each with .md + .h5ad + .duckdb
```

Now start the stack in the foreground (no reverse proxy yet):

```sh
make serve-api &        # binds 127.0.0.1:8080
make serve-browse &     # binds 127.0.0.1:8081
sleep 5
curl -s http://localhost:8080/api/version | python3 -m json.tool
# {
#   "version": "2.3.0",
#   "released": "2026-06-09",
#   "plot_geoms": [...13 entries...],
#   ...
# }
curl -s -o /dev/null -w "browse: HTTP %{http_code}\n" http://localhost:8081/
```

If both respond, you have a working bare-API install. Kill the
background processes (`kill %1 %2`) and continue with section 5 for
production wiring.

---

## 5. Reverse proxy: Caddy (recommended)

Caddy handles TLS automatically and the repo's `config/Caddyfile`
already wires `/`, `/api/`, `/jupyter/`, `/streamlit*`, the MCP
discovery probes, and the static fallback. By default it listens on
port 8501 (the project convention) — adjust to 80/443 with TLS in
front of a public domain.

**Local-only first** (Caddy in front of the supervisor on `:8501`,
no TLS):

```sh
make serve-debug        # starts the supervisor with --caddy
                        # → api + browse + caddy under one process
                        # → kill with `make serve-stop`
                        # → smoke: curl http://localhost:8501/api/version
```

**Public deployment**:

1. Point your public hostname (DNS `A` / `AAAA`) at the host.
2. Edit `config/Caddyfile` — change the listen line at the top from
   `:8501` to your hostname (e.g. `anthive.example.org`). Caddy will
   auto-provision a Let's Encrypt cert.
3. Run Caddy under systemd separately (don't use `make serve-debug`
   for production — see section 7).

**Add basic auth** (recommended for any non-public-data deploy):

```sh
make caddy-passwd       # interactive: pick a user + password
# → writes config/caddy-auth-basic.conf (gitignored).
# Caddy picks it up automatically; api + browse + the entire UI are
# now behind HTTP basic auth.

make caddy-passwd-clear # remove the auth and re-open the host
```

**Restrict which interfaces Caddy binds to** (multi-homed hosts —
Nebula / Tailscale / Wireguard + LAN):

By default Caddy accepts connections on *every* network interface. On
a multi-homed host (Nebula peer + office LAN + Docker bridges all on
the same machine), that often exposes more than you want even with
basic-auth in front. Restrict by setting ``ANTHIVE_CADDY_BIND``
before starting the supervisor — values are space-separated IPs or
hostnames:

```sh
# Localhost + a Nebula peer only:
export ANTHIVE_CADDY_BIND="127.0.0.1 10.0.0.10"

# Localhost + Tailscale only (substitute your TS IP):
export ANTHIVE_CADDY_BIND="127.0.0.1 100.x.y.z"

# Then restart the whole supervisor so the env var propagates to caddy:
make serve-stop
make serve-debug      # or: make serve-all
```

The env var is read by `config/Caddyfile` on startup (consumed by
Caddy's `bind` directive). Children of the supervisor inherit its
environment, so setting the var in the systemd unit's `Environment=`
directive makes this persistent across restarts. Leaving it unset
preserves the default `0.0.0.0 ::` (all v4 + v6 interfaces) for
development laptops.

---

## 5b. Production topology — nginx-thin + Caddy-fat

If your host already runs nginx for other vhosts (alzmap.org,
data.example.org, …), DON'T duplicate the anthive routing in nginx.
Instead use this pattern:

```
client ──https──> nginx :443 (SSL term + Authelia auth_request)
                                       │
                                       │  proxy_pass http://127.0.0.1:8500
                                       ▼
                                  Caddy on 127.0.0.1:8500
                                       │
                                       ├ /api/*       → ant-serve  :8080
                                       ├ /browse/*    → ant-browse :8081
                                       ├ /mcp*        → ant-mcp    :8090
                                       ├ /authelia/*  → authelia   :9091
                                       ├ /jupyter/*   → static
                                       ├ /artifacts/* → static
                                       └ / (fallback) → ant-browse :8081
```

The nginx → Caddy hop is loopback (~1 ms).  The benefit is one Caddyfile
defining what `/jupyter/`, `/api/`, `/artifacts/`, `/mcp/` look like —
identical on dev and prod, and nginx never changes after this deploy.

**Caddy in prod mode** (started by ant-supervisor with `--prod`):

- Listens on `http://127.0.0.1:8500` (plain HTTP, no TLS — nginx does TLS).
- `ANTHIVE_CADDY_BIND=127.0.0.1` locks it to loopback.
- The `caddy-auth.prod` snippet is empty — Caddy trusts the `Remote-User /
  Remote-Groups / Remote-Email` headers nginx already set via `auth_request`.
- The `:8500` HTTPS-redirect block is omitted (would collide with the
  prod listener on the same port).

**Nginx** is a thin shim — see `config/nginx-intern-anthive.conf` for
the drop-in slim vhost (~150 lines, of which only ~10 are anthive-
specific routing). Step-by-step deploy recipe:

→ **`doc/DEPLOY.md`** — has the systemd unit, the smoke-test commands,
and a rollback procedure.

Quick reference for the env-var split that drives `config/Caddyfile`:

| Env var                          | Dev default                 | Prod value                  |
| -------------------------------- | --------------------------- | --------------------------- |
| `ANTHIVE_LISTEN`                 | `:8501`                     | `http://127.0.0.1:8500`     |
| `ANTHIVE_TLS_SNIPPET`            | `caddy-tls.dev`             | `caddy-tls.prod` (empty)    |
| `ANTHIVE_AUTH_SNIPPET`           | `caddy-auth.dev`            | `caddy-auth.prod` (empty)   |
| `ANTHIVE_HTTP_REDIRECT_SNIPPET`  | `caddy-http-redirect.dev`   | `caddy-http-redirect.prod` (empty) |
| `ANTHIVE_CADDY_BIND`             | `0.0.0.0 ::`                | `127.0.0.1`                 |

These pair 1:1 with snippet files in `config/`. `ant-supervisor --prod`
sets all of them; `ant-supervisor` (no flag) uses dev defaults so
local behaviour is unchanged.

### Standalone nginx (alternative — single-host dev box)

If your host has no other vhosts and you'd rather not run two
processes, you can still use nginx alone:

```sh
make serve-nginx        # foreground nginx; reads config/nginx.conf
```

This still routes everything through a local Caddy at 127.0.0.1:8500
(nginx is the SSL listener on :8501, Caddy does all path routing).
Start them in two terminals:

```sh
# Terminal 1
make serve-prod         # supervisor + Caddy in prod mode

# Terminal 2
make serve-nginx        # the thin shim
```

---

## 6. JupyterLite static build (optional)

The in-browser Pyodide notebooks served at `/jupyter/` are a static
build under `webapp/jupyter/`. Build once with:

```sh
make build-webapp       # builds JupyterLite + the landing page
                        # → outputs to webapp/    (gitignored)
                        # → ~5-10 minutes the first time
                        # → Caddy/nginx serves it as static files
```

Re-run after pulling significant changes to `frontend/jupyter/` or
`antclient/`. The build is incremental — subsequent runs are fast.

---

## 7. Process supervision

The supervisor (`bin/ops/ant-supervisor`) runs all the services
under one parent, multiplexes their stdout with colored prefixes,
and restarts crashed children automatically.

**Foreground** (development / debug):

```sh
make serve-debug        # all services + Caddy (debug mode, port 8501)
# or:
make serve-all          # all services WITHOUT Caddy (production, assumes
                        # nginx or external Caddy is running)
```

**Subcommands**:

```sh
make serve-status       # show what's running and pid uptimes
make serve-restart SVC=api               # restart one service
make serve-restart SVC='api browse'      # several
make serve-restart SVC=all               # restart everything
make serve-stop         # stop the whole supervisor + all children
```

**Production: run under systemd**

There are two production shapes — pick one:

1. **Nginx in front (recommended for multi-vhost hosts)** — Caddy on
   127.0.0.1:8500, nginx terminates TLS + Authelia gate. Use
   `--prod --caddy`. See `doc/DEPLOY.md` for the full recipe.
2. **Caddy alone (single-host dev box / no other vhosts)** — Caddy on
   :8501 with `tls internal`. Use bare `ant-supervisor` (no flags), or
   `--caddy` to keep Caddy under the supervisor. No nginx involved.

```ini
# /etc/systemd/system/anthive.service
[Unit]
Description=Anthive supervisor
After=network.target

[Service]
Type=simple
User=anthive
WorkingDirectory=/opt/anthive/anthive4
# Shape 1 — prod, nginx in front:
ExecStart=/opt/anthive/anthive4/bin/ops/ant-supervisor --prod --caddy
# Shape 2 — Caddy alone, no nginx (uncomment instead of the line above):
# ExecStart=/opt/anthive/anthive4/bin/ops/ant-supervisor --caddy
ExecStop=/opt/anthive/anthive4/bin/ops/ant-supervisor restart all
Restart=on-failure
RestartSec=5
Environment=PATH=/home/anthive/.local/bin:/usr/local/bin:/usr/bin:/bin
# Optional: restrict Caddy to specific interfaces (see section 5 above).
# Without this, Caddy binds to every interface (dev) or 127.0.0.1 (prod).
Environment=ANTHIVE_CADDY_BIND=127.0.0.1 10.0.0.10

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now anthive.service
sudo systemctl status anthive.service
journalctl -u anthive.service -f      # follow logs
```

If you're terminating TLS with Caddy on this host, you'll typically
run Caddy under its own systemd unit (provided by the `caddy` apt
package or written by hand against `caddy run --config <path>`).

If you're using nginx, treat nginx as the gate — its own systemd unit
already exists; just make sure `/etc/nginx/sites-enabled/` includes
the anthive config.

**Default service set**: api + browse only (see
`bin/ops/ant-supervisor` `DEFAULT_SERVICES`). MCP and Streamlit are
**off by default** (vark and ant-browse cover their use cases). To
re-enable MCP, edit `DEFAULT_SERVICES` or run `make serve-mcp`
standalone.

---

## 8. S3 sync — maintenance workflow

Once running, you have a few daily / weekly operations:

```sh
make sync-pull       # pull h5ad + .de.parquet from S3 (cheap)
make build-stale     # rebuild any duckdb whose h5ad is newer than it
make sync-push       # push h5ad + duckdb + .de.parquet back to S3
make sync-update     # all three above, in order — the full cycle
make sync-status     # show local vs S3 drift (size + mtime)
```

The `.md` files in `data/h5adstore/` are tracked by git in that
sister repo; commit them there when you change annotations.

---

## 9. Verification

After everything is up:

```sh
# Versioning surface (single source of truth)
curl -s http://localhost:8501/api/version | python3 -m json.tool

# Health + speed signal
curl -s http://localhost:8501/api/health

# OpenAPI schema (the canonical contract)
curl -s http://localhost:8501/api/openapi.json | python3 -c \
    'import sys, json; s=json.load(sys.stdin); print(f"endpoints: {len(s[\"paths\"])}")'

# Swagger / ReDoc renderings
$BROWSER http://localhost:8501/api/docs
$BROWSER http://localhost:8501/api/redoc

# Web UI landing
$BROWSER http://localhost:8501/

# JupyterLite (if built)
$BROWSER http://localhost:8501/jupyter/
```

If `/api/version` returns the current `API_VERSION` and `/api/health`
reports `databases > 0`, you're done.

---

## Common pitfalls

- **DuckDB lock conflicts**: `ant-serve` holds duckdbs read-only via
  a connection pool. Writers (`ant-convert`, `ant-score`,
  `ant-de-ingest`) coordinate via `/admin/release/{db_id}` →
  `/admin/restore/{db_id}`. If a writer hangs, the duckdb stays
  evicted for 5 minutes (`EVICTION_DEFAULT_SECONDS`) — kill the
  writer and call `POST /admin/restore/{db_id}` manually to recover.

- **Empty `data/h5adstore/`**: `make sync-pull` is required after a
  fresh clone; until it runs, the `.md` files exist but the heavy
  files don't, and the API will return empty database lists.

- **memory_limit too generous on a small box**: `auto` reserves
  ~25% of system RAM divided across the connection pool. On a
  small VM (< 16 GB RAM), set
  `duckdb_memory_limit: "1GB"` explicitly.

- **Streamlit / MCP off by default**: this is intentional. Use
  `make serve-streamlit` or `make serve-mcp` if you actually want
  them, but ant-browse + vark cover the same surface in the
  default deployment.

- **`uv` not on `$PATH` for the systemd user**: the supervisor
  invokes `uv run --quiet --script` to start each child. The
  systemd unit above sets `Environment=PATH=...` to include
  `~/.local/bin` where uv installs by default. Verify with
  `sudo -u anthive env | grep PATH`.

- **Per-script PEP-723 deps cache cold**: the first time each
  `ant-*` script runs, uv resolves and downloads its inline
  dependencies (`requests`, `pandas`, `pydeseq2`, …). This can take
  60–90 s per script. Subsequent runs hit uv's cache and start in
  <1 s. Don't be alarmed if the first `ant-de-pseudobulk` invocation
  on a new host takes a couple of minutes.

---

## Putting it all together — minimum command sequence

```sh
# Prerequisites (once per host)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone + data
cd /opt/anthive
git clone git@github.com:badslab/anthive4.git
git clone git@github.com:badslab/h5adstore.git
cd anthive4
ln -s ../h5adstore data/h5adstore

# Configure
cp anthive.default.yaml anthive.yaml
$EDITOR anthive.yaml      # set s3.* at minimum

# Data
make sync-pull            # h5ad from S3
make build-stale          # duckdb from h5ad

# Webapp (optional but recommended)
make build-webapp

# systemd (production)
# Copy the unit-file snippet from section 7 above into:
#   /etc/systemd/system/anthive.service
# (adjust User, WorkingDirectory, and ExecStart paths first)
sudo systemctl daemon-reload && sudo systemctl enable --now anthive

# Verify
curl -s http://localhost:8501/api/version
```

Total elapsed time on a fresh host with empty caches and ~10 GB of
data: ~30 minutes. The data sync usually dominates.
