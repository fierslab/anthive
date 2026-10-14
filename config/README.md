# Configuration

New sites run as containers (`doc/DEPLOY_CONTAINERS.md`, `bin/ops/ant-stack`),
which render their own Caddy config from `site.yaml`. The files here are
the mechanism behind that and the reference for existing host installs
(`doc/DEPLOY.md`).

Caddy is the **single source of path routing** on dev and prod. The same
`Caddyfile` runs everywhere; a handful of env vars flip it between dev
(Caddy terminates TLS itself) and prod (an upstream nginx terminates TLS and
forwards every path to Caddy on a loopback port).

```
dev :   browser ─https─▶ Caddy :8501 (tls internal) ─▶ api / browse / jupyter / …
prod:   browser ─https─▶ nginx :443 (TLS [+ auth]) ─http─▶ Caddy 127.0.0.1:<port> ─▶ …
```

## What lives where

| Path | Tracked? | What |
| --- | --- | --- |
| `Caddyfile` | ✅ | The shared, env-driven router. All path routing lives here. |
| `caddy-{tls,auth,http-redirect,servers}.{dev,prod}` | ✅ | Snippets imported by the Caddyfile; pick dev vs prod behaviour. |
| `authelia/` | partial | `configuration.example.yml` is tracked; the real `configuration.yml`, `secrets/`, `users_database.yml` are gitignored. |
| `examples/` | ✅ | Templates for the per-instance files of a host install (nginx vhosts, systemd unit, sync and library cron/timer jobs). Placeholders only. |
| `nginx.conf` | ✅ | Dev-only nginx shim for `make serve-nginx` (no login; drops every identity header). |
| `server/` | ❌ gitignored | **Real** per-instance/-server configs (hostnames, ports, paths, cert locations, units). Lives in a separate private repo. |

There is no basic-auth file to drop in here: basic auth exists only in the
container stack, where `bin/ops/ant-stack` renders it from `site.yaml`
(`auth.mode: basic`, see `doc/DEPLOY_CONTAINERS.md`, "Login modes").

The generic mechanism (Caddyfile + snippets + authelia template) is tracked
because the supervisor references it by relative path at runtime. Everything
host-specific is **not** in this repo.

## The dev/prod env split

`bin/ops/ant-supervisor --prod --caddy` (or `ANTHIVE_PROD=1`) sets the env
vars the `Caddyfile` reads. Defaults (no `--prod`) reproduce dev behaviour.

| env var | dev default | prod |
| --- | --- | --- |
| `ANTHIVE_LISTEN` | `:8501` (+ local hostnames) | `http://:<port>` — **host-agnostic** (see below) |
| `ANTHIVE_TLS_SNIPPET` | `caddy-tls.dev` (`tls internal`) | `caddy-tls.prod` (empty — nginx does TLS) |
| `ANTHIVE_AUTH_SNIPPET` | `caddy-auth.dev` | `caddy-auth.prod` (empty — nginx auths, Caddy trusts headers) |
| `ANTHIVE_HTTP_REDIRECT_SNIPPET` | `caddy-http-redirect.dev` | `caddy-http-redirect.prod` (empty) |
| `ANTHIVE_SERVERS_SNIPPET` | `caddy-servers.dev` (empty) | `caddy-servers.prod` (trust nginx on loopback for `X-Forwarded-*`) |
| `ANTHIVE_CADDY_BIND` | `0.0.0.0 ::` | `127.0.0.1` |
| `ANTHIVE_API_PORT` / `ANTHIVE_BROWSE_PORT` | from `anthive.yaml` | from `anthive.yaml` |

## Adding an instance (prod)

1. **Ports** in `anthive.yaml`: `rest_api_port` / `browse_port`. Pick a free
   Caddy port too. **Every port must be unique** if more than one instance
   shares a host.
2. **Supervisor env** (in the systemd unit): `ANTHIVE_LISTEN=http://:<caddyport>`.
   For a public (no-auth) instance also set `ANTHIVE_SERVICES=api,browse` to
   skip Authelia.
3. **nginx vhost**: copy `examples/nginx-vhost.{public,authelia}.conf.example`,
   set `server_name`, cert paths, and the Caddy port. Deploy to
   `/etc/nginx/conf.d/`.
4. **systemd unit**: copy `examples/anthive-instance.service.example`, set the
   repo path, user, and env. `enable --now`.
5. Keep the filled-in copies in the private `server/` repo, not here.

## Critical gotchas

- **nginx owns the identity.** `caddy-auth.prod` trusts the `Remote-User` /
  `Remote-Groups` / `Remote-Email` / `Remote-Name` headers nginx sends. Every
  location must set them only from Authelia's `auth_request` response (or
  drop them on a public vhost) and drop `X-Remote-*`, exactly as the
  examples do. See `doc/DEPLOY.md`, "Upgrading a host install to 2.45".

- **`ANTHIVE_LISTEN` must be host-agnostic in prod** (`http://:<port>`, not
  `http://127.0.0.1:<port>`). nginx forwards the real public Host; a
  host-bound Caddy address matches no site and returns **empty 200s (blank
  pages)**. `ANTHIVE_CADDY_BIND=127.0.0.1` keeps it on loopback regardless.
- **Never set an unconditional `proxy_set_header Connection "upgrade";`** in
  the nginx vhost — on a normal GET nginx drops the body (blank pages). The
  examples omit it; add a `map $http_upgrade $connection_upgrade` only if you
  introduce a real WebSocket upstream.
- **nginx ≥ 1.25** uses `http2 on;`; **1.24 and earlier** need the combined
  `listen 443 ssl http2;`. The examples use the combined form (works on both).

## Launch

```bash
make serve-debug          # dev: supervisor + Caddy (tls internal) on :8501
make serve-prod           # prod: supervisor + Caddy on a loopback port; nginx in front
```
