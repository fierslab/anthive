# Deploying anthive4 on a production host

End-to-end recipe for deploying anthive on a host that ALREADY runs
nginx fronting other vhosts (alzmap.org, data.bdslab.org, …).  The
goal of this recipe is to add ONE new anthive vhost without touching
the unrelated nginx config.

If you're setting up a fresh box without any existing nginx, see
`doc/INSTALL.md` instead — that doc covers the from-scratch path.

## Topology — what we're building

```
client ──https──> nginx :443 (SSL term + Authelia auth_request)
                   │  proxy_pass http://127.0.0.1:8500
                   ▼
                  Caddy on 127.0.0.1:8500 (plain HTTP, localhost-only)
                   │
                   ├── /api/*           → ant-serve   :8080
                   ├── /browse/*        → ant-browse  :8081
                   ├── /mcp*            → ant-mcp     :8090
                   ├── /authelia/*      → authelia    :9091
                   ├── /jupyter/*       → static (webapp/jupyter/)
                   ├── /artifacts/*     → static (webapp/artifacts/)
                   └── /  (fallback)    → ant-browse  :8081
```

The nginx → Caddy hop is loopback (~1 ms). The benefit: ONE Caddyfile
defines what `/jupyter/`, `/api/`, `/artifacts/`, `/mcp/`, … look like,
identical on dev and prod.  Adding or renaming a path = editing
`config/Caddyfile` only.  Nginx never changes after this deploy.

## Prerequisites

- nginx already installed and running (managing other vhosts).
- DNS for the new anthive hostname (e.g. `intern.bdslab.org`) points at
  this host.
- A valid TLS cert in `/etc/nginx/cert/` (wildcard recommended).
- `/data/anthive/anthive4` checked out — or any path; adjust the unit.
- All anthive prerequisites from `doc/INSTALL.md` (uv, S3 creds, the
  initial `sync-pull` + `build-stale` etc.).

## Step-by-step

### 1. Pull the latest anthive4 code

```bash
cd /data/anthive/anthive4
git pull origin main
```

### 2. Verify Caddy is available

We ship a Caddy binary at `./envs/caddy/caddy` (built with the
`replace_response` module for the Authelia logo CSS injection).
Confirm it works:

```bash
./envs/caddy/caddy version
./envs/caddy/caddy validate --config config/Caddyfile
```

The supervisor uses this binary by default — no system-wide install
needed.

### 3. Set up the systemd unit for the anthive supervisor in prod mode

```ini
# /etc/systemd/system/anthive.service
[Unit]
Description=Anthive supervisor (prod — Caddy on 127.0.0.1:8500, nginx in front)
After=network.target

[Service]
Type=simple
User=anthive
WorkingDirectory=/data/anthive/anthive4
ExecStart=/data/anthive/anthive4/bin/ops/ant-supervisor --prod --caddy
ExecStop=/data/anthive/anthive4/bin/ops/ant-supervisor restart all
Restart=on-failure
RestartSec=5

# uv must be discoverable for the per-script PEP-723 shebangs:
Environment=PATH=/home/anthive/.local/bin:/usr/local/bin:/usr/bin:/bin

# Prod-mode env split — also encoded by `--prod` above, but stating it
# explicitly here means a manual `systemctl edit anthive.service` user
# sees the actual values that drive config/Caddyfile.
Environment=ANTHIVE_PROD=1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now anthive.service
sudo systemctl status anthive.service
journalctl -u anthive.service -f       # follow logs
```

### 4. Verify Caddy is up locally

```bash
curl -sv http://127.0.0.1:8500/api/health -H 'Remote-User: smoke-test'
# → expect HTTP/1.1 200 OK with a JSON body listing databases.

curl -s -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8500/streamlit
# → HTTP 410 (the gone-stub, proves Caddy's routing layer is alive).

curl -s -o /dev/null -w 'HTTP %{http_code}\n' \
    http://127.0.0.1:8500/.well-known/oauth-authorization-server
# → HTTP 404 (the MCP discovery JSON-404 stub).
```

If any of these fail, fix BEFORE wiring nginx — the failure mode is
much harder to diagnose through nginx.

### 5. Drop in the slim nginx vhost

Copy `config/nginx-intern-anthive.conf` into nginx's sites directory
and enable it. The file is a ~150-line server{} block containing:

- SSL termination
- Authelia `auth_request` gate
- `/authelia/*` portal pass-through
- A single `location /` that forwards EVERYTHING to Caddy on 127.0.0.1:8500

```bash
sudo cp config/nginx-intern-anthive.conf \
        /etc/nginx/sites-available/intern-bdslab.conf
sudo ln -sf /etc/nginx/sites-available/intern-bdslab.conf \
            /etc/nginx/sites-enabled/intern-bdslab.conf

# If the host previously had a sprawling intern.bdslab.org config with
# /api/, /browse/, /jupyter/, … location blocks, REMOVE that old file
# now — the new slim vhost replaces it entirely.
# Example:
# sudo rm /etc/nginx/sites-enabled/intern-bdslab-OLD.conf
```

Adjust the cert paths in the new file if this host uses different ones.

### 6. Test + reload nginx

```bash
sudo nginx -t
sudo systemctl reload nginx
```

`nginx -t` MUST pass. A typo here doesn't break running connections
(reload is graceful) but a failing config blocks future reloads.

### 7. Smoke test from a browser

Visit `https://intern.bdslab.org/` and verify:

- [ ] Landing page loads (browse UI, owned by ant-browse via Caddy).
- [ ] Click into a dataset → plot UI works.
- [ ] `https://intern.bdslab.org/jupyter/` → JupyterLite loads with
      COOP/COEP headers (Pyodide kernel boots).
- [ ] Generate a plot, then a notebook link → the "Open in JupyterLite"
      button on the plot page opens the notebook deep-link.
- [ ] Trigger a pseudobulk artifact download from the API → file lands
      in `/artifacts/…` and downloads cleanly (auth flows through).
- [ ] `https://intern.bdslab.org/mcp` → MCP endpoint responds (try
      `curl -H 'Accept: text/event-stream' https://intern.bdslab.org/mcp`
      while authenticated — should produce a non-empty SSE stream).
- [ ] `https://intern.bdslab.org/api/version` → JSON `{ "version": ... }`.
- [ ] Other vhosts on this nginx (alzmap.org, data.bdslab.org) still
      work — they live in their own server{} blocks and we didn't touch
      them.

## Rollback

If something is wrong:

```bash
# 1. Roll nginx back to whatever was there before:
sudo rm /etc/nginx/sites-enabled/intern-bdslab.conf
sudo ln -sf /etc/nginx/sites-available/intern-bdslab-OLD.conf \
            /etc/nginx/sites-enabled/intern-bdslab.conf
sudo nginx -t && sudo systemctl reload nginx

# 2. Stop the new prod-mode supervisor:
sudo systemctl stop anthive.service
```

You should be back to the pre-deploy state.  Diagnose the Caddy issue
locally (steps 4 above), fix, and retry.

## What changes vs. the old topology

Before:

```
client ──https──> nginx :443  ──/api/      → ant-serve  :8080
                                ──/browse/   → ant-browse :8081
                                ──/jupyter/  → static
                                ──/mcp/      → ant-mcp    :8090
                                ──/.well-known/ → (custom 404s)
                                ──/streamlit  → 410
                                ──/artifacts/ → static
                                ──/           → ant-browse :8081
```

After:

```
client ──https──> nginx :443  ──/  → Caddy 127.0.0.1:8500 ── (everything above)
```

`config/nginx-intern-anthive.conf` shrinks from ~200 lines to ~50 of
actual routing.  Adding a new path = `config/Caddyfile` only — touch
nothing on prod nginx after this deploy.

## Reference

- `config/Caddyfile`                   — single source of truth, dev + prod
- `config/caddy-{tls,auth,http-redirect}.{dev,prod}` — the env-toggled snippets
- `config/Caddyfile-prod-snippet`      — annotated example of the env vars
- `config/nginx-intern-anthive.conf`   — drop-in slim vhost
- `config/nginx.conf`                  — local dev/test variant of the shim
- `doc/INSTALL.md` §5                  — the dev-mode (TLS-internal) Caddy path
- `doc/HANDOFF_authelia.md`            — Authelia setup details (still current)
