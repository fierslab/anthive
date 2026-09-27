# Deploying anthive with containers

This is the supported way to run an anthive site. One file (`site.yaml`)
describes the deployment; `bin/ops/ant-stack` turns it into a docker compose
stack. Data never goes into an image. The stores are bind-mounted from the
host, the rendered config and all secrets live in a state directory you
back up, and the images come from a registry (or a local build).

For command-line ingest on a cluster, see [HPC.md](HPC.md). For image
internals (lock file, entrypoint roles, the uv shim), see
[DOCKER.md](DOCKER.md).

## What runs

```
                 ┌──────────────── host ────────────────┐
 browser ──TLS──▶│ caddy ──┬── /api/*   ──▶ api   (ant-serve)   ── h5adstore (rw, same path)
 AI client       │  :443   ├── /mcp*    ──▶ mcp   (ant-mcp)
                 │         ├── /authelia ─▶ authelia (optional) ── state_dir/authelia
                 │         ├── /jupyter, 404 ── webapp volume (from the image)
                 │         └── everything else ─▶ web (ant-browse) ── h5adstore (ro)
                 │  ingest (one-off `ant-stack ingest -- ...`) ── h5adstore (rw), scratch
                 └───────────────────────────────────────┘
```

Every service is its own container with one process, a restart policy and a
healthcheck. `static` is a one-shot container that copies the image's
JupyterLite bundle and Caddy config into shared volumes on every `up`, so an
upgraded image always brings its own static files along.

## Quick start

```sh
git clone https://codeberg.org/mfiers/anthive4 && cd anthive4
bin/ops/ant-stack init       # writes ./site.yaml from site.example.yaml
$EDITOR site.yaml            # paths, public_url, auth mode, tls
bin/ops/ant-stack init       # creates state_dir, secrets, user accounts
bin/ops/ant-stack pull       # or: bin/ops/ant-stack build
bin/ops/ant-stack check      # validates and prints the plan
bin/ops/ant-stack up         # renders, starts, waits until healthy
```

`init` prints a generated password for every user in `auth.users` once (it
is also kept in `<state_dir>/secrets/initial-passwords.txt`, mode 0600).
Change one with `bin/ops/ant-stack passwd <user>`.

The repo checkout is only needed for `ant-stack`, `docker/compose.yaml` and
`site.example.yaml`. The containers run the code inside the images, so
checking out a newer commit changes nothing until you pull newer images.

## site.yaml

`site.example.yaml` documents every key. The ones that decide how a site
behaves:

| Key | Decides |
| --- | --- |
| `site.public_url` | what users type; cookie domain, redirects, MCP links |
| `data.h5adstore` | the store (absolute host path, mounted at the same path) |
| `data.readonly` | mount the store read-only (mirrors that never annotate) |
| `state_dir` | where secrets, caches, tokens and the rendered config live |
| `scratch_dir` | host disk for DuckDB spill and ant-convert staging (default: a volume) |
| `auth.mode` | authelia, basic, trusted-proxy or none (below) |
| `tls.mode` | internal (own CA), acme (Let's Encrypt) or off (behind a proxy) |
| `run_as` | uid/gid the containers run as (default: whoever runs ant-stack) |
| `images` | registry, names, tag |
| `services.mcp`, `services.chat` | optional services |
| `resources` | DuckDB threads and memory, open-dataset cap, plot cache, catalog scan |
| `anthive` | any extra anthive.yaml key, copied verbatim (wins last) |

Why the same-path mounts: each dataset's `.duckdb` stores layer views with
the absolute path of its parquet sidecars. Mounting the store at the path it
has on the host keeps those views valid inside the containers, so a store
can be shared between the host tools and the containers without rewriting
anything.

`bin/ops/ant-stack render` writes everything derived from `site.yaml` into
`<state_dir>/stack/`:

- `.env` holds the compose variables (images, uid, paths, listen addresses)
- `compose.site.yaml` has the bind mounts, ports and profiles for this site
- `anthive.yaml` is the runtime config the containers read (0600; it can
  hold the S3 and chat keys)
- `caddy/auth.caddy` and `caddy/tls.caddy` are the proxy snippets
- `../authelia/configuration.yml` is the Authelia config, in authelia mode

Do not edit these by hand; they are regenerated on every `up`.

## Authentication modes

Caddy is the only thing reachable from outside, and it decides identity.
The backend trusts the `Remote-User` / `Remote-Groups` headers, so every mode
makes sure those headers can only come from the proxy itself.

**authelia.** A login portal (Authelia container, only started in this mode)
with SSO cookies. Browser routes redirect to `/authelia/`; `/api/*` answers
401 without a session and also accepts HTTP basic credentials of an Authelia
user, which is how pyvark, vark and scripts authenticate. Users live in
`<state_dir>/authelia/users_database.yml`, seeded from `auth.users` at `init`;
admins are the users in the `admins` group. Authelia refuses plain-HTTP
cookies, so `public_url` must be https. Password reset mails need
`auth.smtp`; without it they are written to
`<state_dir>/authelia/notification.txt`.

**basic.** HTTP basic auth at Caddy for the users in `auth.users` (bcrypt
hashes in `<state_dir>/secrets/users.yaml`). Caddy sets `Remote-User` to the
authenticated user and `Remote-Groups: admins` for users in `auth.admins`
(or in an `admins` group in `auth.users`). Good for small groups and
automation. Browsers cache basic credentials, so logging out means closing
the browser.

**trusted-proxy.** An upstream proxy (for example nginx with the university
SSO) has already authenticated the user and sets the Remote-* headers.
Caddy passes them on untouched, so the published port must only be
reachable from that proxy: `check` refuses anything but `ports.bind:
127.0.0.1`. Use `tls.mode: off` and let the upstream terminate TLS.

**none.** No login at all, for a public read-only mirror or a box that only
listens on loopback. Caddy strips any identity header a client sends, and
ant-serve refuses every admin action in this mode (two independent locks).
ant-serve prints a warning banner at start.

In every mode, the server-control endpoints under `/api/admin/*` (release,
restore, rescan, cache and index rebuilds) are refused by the proxy unless
the caller is in the admins group. Tools inside the stack reach them
directly on `http://api:8080`, and host tools use the loopback port.

## TLS

- `internal` issues certificates from Caddy's own CA for the host of
  `public_url` and every name in `site.hostnames`. Browsers warn until the
  CA is trusted: copy `/data/caddy/pki/authorities/local/root.crt` out of the
  caddy container (`docker cp <site>-caddy-1:/data/caddy/pki/authorities/local/root.crt .`)
  and install it on the clients. The CA lives in the `caddy-data` volume and
  survives restarts and `down`.
- `acme` gets Let's Encrypt certificates for `public_url`'s host. The name
  must resolve publicly to this machine and ports 80 and 443 must reach it.
  Set `tls.acme_email`.
- `off` serves plain HTTP on `ports.http`, for sites behind a TLS proxy.

## Day-2 operations

| Task | Command |
| --- | --- |
| status | `ant-stack ps` |
| logs | `ant-stack logs [service] [-f]` |
| restart one service | `ant-stack restart api` |
| upgrade | set `images.tag`, then `ant-stack pull && ant-stack up` |
| stop | `ant-stack down` (keeps volumes and state) |
| wipe volumes too | `ant-stack down --volumes` (JupyterLite copy, certs, scratch) |
| new user (authelia) | add to `auth.users`, `ant-stack init`, `ant-stack passwd <user>` |
| convert a dataset | `ant-stack ingest -- ant convert /path/in/store/x.h5ad` |
| score a dataset | `ant-stack ingest -- ant score S25/PhM Sierksma2025/WGCNA` |

`ant-stack ingest` runs the command in the ingest image with the same mounts
as the API, `ANTHIVE_COORDINATE=auto` and `http://api:8080` as the API
address, so writers that need the server to release a dataset can ask it.
The h5ad path must be inside a mounted store.

Every failed container restarts on its own (`restart: unless-stopped`), and
`up` waits for all healthchecks before it returns.

## Backups

Back up `state_dir`. It holds everything that is not in the images or the
data stores: secrets, the Authelia user database and its SQLite store, the
gene-stats cache, click history, chat history and MCP link tokens. It is
small and changes slowly. Take the backup with the stack stopped, or copy
the SQLite files with `sqlite3 <db> ".backup <file>"`.

The data stores are backed up as today (S3 via `ant sync`, the `.md` files
in git). Docker volumes only hold rebuildable things.

## Registry

The images are published by `.forgejo/workflows/images.yml` on version tags
(and on demand), or by hand with `make images push-images REGISTRY=... TAG=...`.

| Registry | For | Against |
| --- | --- | --- |
| Codeberg (Forgejo packages) | next to the source and its CI, OCI images and oras artifacts (the .sif), same account and tokens, free | non-profit capacity; quotas and fair use for multi-GB images are not published in a way I could confirm; occasional outages |
| GitHub Container Registry | fast, reliable, free for public images, no pull limits worth noting | needs a GitHub mirror or cross-forge credentials |
| Quay.io | free public repositories without pull rate limits | another account; Red Hat terms |
| Docker Hub | the default everywhere | anonymous pulls are rate limited per IP, which hits clusters where many nodes share one address |
| self-hosted (zot or CNCF Distribution behind Caddy) | private images, full control, can live next to the data | you run, secure and back it up |

Recommendation: publish tagged releases to the Codeberg registry, because
CI and code already live there, and mirror them to GHCR if Codeberg
capacity or availability becomes a problem. Keep Docker Hub out of the pull
path of HPC jobs. If images must stay private (unpublished data in
notebooks, site branding), run zot next to the stack; it is a single binary
and speaks OCI, including the oras artifacts Apptainer pulls. I am fairly
confident in the trade-offs above but have not measured Codeberg's limits.

## Troubleshooting

- `check` says a store is not readable by the uid: set `run_as` to a user
  with access, or fix group permissions on the store.
- The API stays unhealthy: `ant-stack logs api`. A config problem
  (unknown key, bad path) shows up there on the first line.
- A page 502s right after `up`: the web container waits for the API, which
  scans the catalog first. `up` waits too; if you used `--no-wait`, give it
  a minute.
- Admin pages say 403 in basic mode: the user is missing from `auth.admins`.
- `tls.mode: off` in site.yaml is read by YAML as a boolean; ant-stack
  handles that, quoting it (`"off"`) is fine too.
