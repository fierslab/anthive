# Standing up a new anthive4 instance (app + Authelia + nginx + systemd)

A step-by-step runbook for deploying a new public anthive4 instance on a
host, fronted by **system nginx** with **Authelia** auth. Written from the
live `anthive.example.org` and `anthive2.example.org` deployments.

This doc describes the **legacy** per-instance nginx pattern where nginx
holds the per-path routing. For new deploys (especially on a host running
other vhosts you don't want to touch), see [`DEPLOY.md`](DEPLOY.md) — that
recipe uses a thin nginx shim that forwards every anthive path to Caddy on
loopback, leaving the routing in one place (`config/Caddyfile`).

For Authelia internals and gotchas, see [`HANDOFF_authelia.md`](HANDOFF_authelia.md).

> **Not for new sites.** Containers are the preferred and supported way to
> deploy anthive ([DEPLOY_CONTAINERS.md](DEPLOY_CONTAINERS.md)). Hosts that
> still run this pattern must apply
> [DEPLOY.md, "Upgrading a host install to 2.45"](DEPLOY.md#upgrading-a-host-install-to-245)
> before running 2.45: every location must set `Remote-*` only from
> Authelia's response, drop `X-Remote-*`, and send `X-Forwarded-For` to
> ant-serve (a loopback request without it counts as a tool on the host).

---

## Model

```
browser ──https──▶ system nginx (:443, LE cert) ──auth_request──▶ authelia (127.0.0.1:PORT)
                          │                                            (allow 200 / redirect 401)
                          ├─ /api/  ─▶ ant-serve  (127.0.0.1:API_PORT)
                          └─ /     ─▶ ant-browse (127.0.0.1:BROWSE_PORT)
```

- **Per-instance, no SSO.** Each instance has its own users, secrets, ports.
- One `ant-supervisor` per instance runs `api` + `browse` + `authelia`;
  **systemd** keeps that supervisor alive. nginx is shared (system-wide).

### Pick ports — they MUST be unique per instance on a shared host

| Instance | api | browse | authelia |
| --- | --- | --- | --- |
| anthive.example.org | 8080 | 8081 | 9091 |
| anthive2.example.org | 8180 | 8181 | 9191 |
| *(new)* | 82xx | 82xx | 92xx |

---

## 0. Prerequisites

- DNS `A`/`AAAA` for the new hostname → this host.
- A Let's Encrypt cert for it: `sudo certbot --nginx -d NEWHOST` (needs an
  nginx http block reachable first; or `certbot certonly --webroot`).
- `uv` on PATH (`~/.local/bin/uv`), system nginx, `sudo`.

## 1. Get the code + app config

```bash
git clone git@github.com:badslab/anthive4.git /data/NEWHOST
cd /data/NEWHOST
cp anthive.default.yaml anthive.yaml      # then edit (see below)
```

`anthive.yaml` is **gitignored** (instance-local). Set the unique ports and,
if datasets come from S3, the `s3:` block (copy from another instance):

```yaml
rest_api_host: localhost
rest_api_port: 82A0          # unique
browse_host: localhost
browse_port: 82B0            # unique
s3:                          # only if datasets are served from S3
  endpoint: fsn1.your-objectstorage.com
  region: eu-central
  access_key_id: ...
  secret_access_key: ...
  bucket: anthive4a
  use_path_style: true
```

## 2. Datasets

`ant-serve` discovers datasets via `.md` files under `data/h5adstore/`
(opens the `.duckdb` locally, or from S3 if absent + `s3:` is set).

- **Share another instance's data**: symlink the dataset dirs you want, e.g.
  `ln -s /data/anthive/data/h5adstore/Sierksma2025/Village.6M \
        data/h5adstore/<Group>/Village.6M`
  (Only what's symlinked is served. `.inactive` sidecars hide a dataset.)
- **Pull from S3**: `make sync-pull` (h5ad) / `make sync-pull-all` (incl duckdb).
  ⚠ duckdbs are large (tens of GB); check `df -h` first. Serving entirely from
  S3 works but makes cold starts slow (metadata is read per-dataset on boot).

## 3. Authelia

```bash
cd /data/NEWHOST
mkdir -p envs/authelia config/authelia/secrets tmp

# 3a. binary
LATEST=$(curl -sS https://api.github.com/repos/authelia/authelia/releases/latest \
         | python3 -c 'import json,sys;print(json.load(sys.stdin)["tag_name"])')
curl -sSL "https://github.com/authelia/authelia/releases/download/${LATEST}/authelia-${LATEST}-linux-amd64.tar.gz" \
     | tar -xz -C envs/authelia/ authelia
chmod +x envs/authelia/authelia

# 3b. per-host secrets (UNIQUE per host — never share)
openssl rand -hex 64 > config/authelia/secrets/jwt
openssl rand -hex 64 > config/authelia/secrets/session
openssl rand -hex 32 > config/authelia/secrets/encryption
# SMTP password (shared Migadu mailbox) — copy from an existing instance:
cp /data/anthive/config/authelia/secrets/smtp config/authelia/secrets/smtp
chmod 600 config/authelia/secrets/*

# 3c. config from the template — fill PRIMARY_HOST and AUTHELIA_PORT
cp config/authelia/configuration.example.yml config/authelia/configuration.yml
$EDITOR config/authelia/configuration.yml
#   - server.address  -> tcp://127.0.0.1:92X0/authelia
#   - session.cookies.domain/authelia_url/default_redirection_url -> NEWHOST
# configuration.yml is GITIGNORED (instance-specific). Do not commit it.

# 3d. seed the first user (login auto-derived from the email)
printf 'users: {}\n' > config/authelia/users_database.yml   # only if new
./bin/ops/ant-adduser you@example.org -g admins             # prints a random pw once

# 3e. validate
AUTHELIA_SESSION_SECRET_FILE=$(pwd)/config/authelia/secrets/session \
AUTHELIA_STORAGE_ENCRYPTION_KEY_FILE=$(pwd)/config/authelia/secrets/encryption \
AUTHELIA_IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET_FILE=$(pwd)/config/authelia/secrets/jwt \
AUTHELIA_NOTIFIER_SMTP_PASSWORD_FILE=$(pwd)/config/authelia/secrets/smtp \
./envs/authelia/authelia validate-config --config config/authelia/configuration.yml
# one benign warning about default_policy is expected
```

The supervisor already injects the four `AUTHELIA_*_FILE` env vars (see the
`authelia` block in `bin/ops/ant-supervisor`) — no change needed there.

## 4. nginx

The per-instance nginx files of this pattern live in the private
server-config repo (`config/server/`, gitignored), not in anthive4. Start
from an existing instance's file there, or from
`config/examples/nginx-vhost.authelia.conf.example` (which forwards to
Caddy instead of the per-path ports), and adapt hostname / cert path /
ports:

```bash
cp config/examples/nginx-vhost.authelia.conf.example config/server/nginx-NEWHOST.conf
$EDITOR config/server/nginx-NEWHOST.conf
#   server_name        -> NEWHOST
#   ssl_certificate(_key) -> /etc/letsencrypt/live/NEWHOST/...
#   proxy_pass ports   -> Caddy (or api 82A0 / browse 82B0) / authelia 92X0
sudo cp config/server/nginx-NEWHOST.conf /etc/nginx/conf.d/NEWHOST.conf
sudo nginx -t && sudo systemctl reload nginx
```

The pattern (already in the example files): `/authelia/` portal open;
`/internal/authelia-verify` (cookie-only, browser) and
`/internal/authelia-verify-api` (cookie-then-basic, for `/api/`);
`/.well-known/acme-challenge/` and `/plots/` (if used) public; everything
else gated. The `X-Original-URL` header on the verify locations is REQUIRED.

## 5. systemd

Copy a `.service` template, adapt the paths, install:

```bash
cp config/examples/anthive-instance.service.example config/server/anthive-NEWHOST.service
$EDITOR config/server/anthive-NEWHOST.service   # WorkingDirectory + ExecStart -> /data/NEWHOST
sudo cp config/server/anthive-NEWHOST.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now anthive-NEWHOST.service
```

Runs as `mf`, `Restart=always`, default cgroup-kill (no orphaned children).

## 6. Verify

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:92X0/authelia/api/health   # 200
curl -s http://127.0.0.1:82A0/databases | python3 -c 'import json,sys;print(json.load(sys.stdin)["total"])'
curl -s --resolve NEWHOST:443:127.0.0.1 -o /dev/null -w '%{http_code}\n' https://NEWHOST/   # 302 → portal
```
Then open `https://NEWHOST/` in a browser → Authelia portal → log in.

---

## Day-2 operations

- **Add a user**: `./bin/ops/ant-adduser email@x` (random pw, hot-reloads).
  Chosen/shared password: `./envs/authelia/authelia crypto hash generate
  argon2` (interactive) → paste the digest into `users_database.yml`.
- **Restart one service** (supervisor stays up; systemd untouched):
  `./bin/ops/ant-supervisor restart {api,browse,authelia}`.
- **Rebuild the dataset catalog**: `curl -X POST http://127.0.0.1:82A0/admin/rescan`.
- **Stop/start the whole instance**: `sudo systemctl stop|start anthive-NEWHOST`
  (do NOT use `ant-supervisor stop` under systemd — `Restart=always` revives it).
- **Logs**: `journalctl -u anthive-NEWHOST -f`, or `./tmp/authelia.log`.

## Gotchas (learned the hard way)

- **Ports must be unique per instance.** Two supervisors on the same ports →
  "address already in use" crash-loop. After a systemd cutover, kill any
  leftover manual/`nohup` supervisor or it keeps owning the ports.
- **This host's IPv6 egress to external services is flaky.** SMTP (Migadu)
  and S3 are pinned to IPv4 in the configs for this reason (587 only; 465/25
  firewalled). If Migadu's or S3's IP changes, re-resolve and update.
- **`disable_startup_check: true`** on the notifier keeps a flaky SMTP from
  crashing auth at boot.
- **Cold start is slow when serving many datasets from S3** — metadata is read
  per-dataset on boot (minutes for ~100). Pull duckdbs local for speed.
- **Browser gate is cookie-only by design.** Don't add a header/basic strategy
  to it — browsers resend stale basic creds and Authelia will ban the user.
- **`configuration.yml`, `anthive.yaml`, `users_database.yml`, `secrets/`,
  `envs/authelia/` are all gitignored** (instance-local). Only the
  `.example` / template files are tracked.
