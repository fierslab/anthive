# DOCKER — maintainer notes for the anthive4 container image

Sister of `docker/README.md` (user-facing). This one documents the
image's internals: what the entrypoint does, how config is
synthesized, and how to iterate on the image.

## Image shape

Two-stage Debian slim (Python 3.11-bookworm).

```
builder stage:
  - apt: curl, git, build-essential, ca-certificates
  - install uv (pinned via UV_VERSION build-arg)
  - COPY . /opt/anthive4
  - Pre-warm uv cache: ant-serve, ant-browse, ant-score-ext, ant-supervisor
    all --help through uv so their PEP-723 deps land in /opt/uv-cache.

runtime stage:
  - apt: ca-certificates, curl, tini
  - non-root user `anthive` (uid 1000)
  - COPY uv binary + /opt/uv-cache + /opt/anthive4 from builder
  - HEALTHCHECK: curl /api/health
  - ENTRYPOINT: tini -> docker/entrypoint.sh
```

Why two stages: apt build-essential + git are ~200 MB. Dropping them
in the final layer keeps the shipped image lean.

Why tini: PID-1 signal semantics. Without tini, `docker stop` sends
SIGTERM to `python /opt/anthive4/bin/ops/ant-supervisor`, but python
on PID 1 without an explicit signal handler ignores SIGTERM, so docker
falls back to SIGKILL after `--stop-timeout` (10 s default). tini
correctly forwards signals and reaps zombies.

Why non-root: least-privilege. The container never needs root at
runtime. Mounts must be readable by uid 1000.

## Entrypoint responsibilities (`docker/entrypoint.sh`)

1. **Validate mounts** — `H5ADSTORE` and `GENESETSTORE` must be
   directories (readable). Empty directory → warn but continue.
   Non-existent → fatal.
2. **Synthesize `anthive.yaml`** — from env vars, into
   `/opt/anthive4/anthive.yaml`. Overwritten on every start; never
   read from the image.
3. **Symlink stores** — `/opt/anthive4/data/{h5adstore,genesetstore}`
   are symlinks to whatever `H5ADSTORE` / `GENESETSTORE` resolve to.
   This lets the codebase keep its `./data/h5adstore` relative paths
   without knowing anything about docker.
4. **Synthesize Caddy auth snippet** — writes
   `/tmp/anthive-caddy-auth.docker`. Empty if no basic-auth env
   vars; a `basic_auth` block if both `ANTHIVE_BASIC_AUTH_USER` and
   `ANTHIVE_BASIC_AUTH_PASSWORD` are set. Password is bcrypt-hashed
   via `caddy hash-password --plaintext`.
5. **Export supervisor env vars** — the docker-mode splice into
   `config/Caddyfile`:
   - `ANTHIVE_LISTEN=http://:${ANTHIVEPORT}`
   - `ANTHIVE_CADDY_BIND=0.0.0.0 ::`
   - `ANTHIVE_TLS_SNIPPET=caddy-tls.prod` (empty snippet — no TLS)
   - `ANTHIVE_HTTP_REDIRECT_SNIPPET=caddy-http-redirect.prod` (empty)
   - `ANTHIVE_AUTH_SNIPPET=/tmp/anthive-caddy-auth.docker`
   - `ANTHIVE_SERVICES=api,browse,caddy`
6. **`exec uv run --script bin/ops/ant-supervisor`** — the supervisor
   takes over PID 1's job, spawns the three services, and proxies
   SIGTERM to them.

## Why `caddy-tls.prod` and not a docker-specific snippet

The existing `config/caddy-tls.prod` is empty (nginx terminates
upstream). For the container image, nginx is upstream is the user's
problem — we just serve plain HTTP on `ANTHIVEPORT`. The empty
`.prod` snippet is exactly what we want; no need for a new
`caddy-tls.docker` file.

Same reasoning for `caddy-http-redirect.prod` (empty).

## Why NOT `caddy-auth.prod`

That file has a hardcoded `basic_auth` block for user `bds` — leftover
from the KU Leuven prod deployment. Reusing it inside the image would
require patching the file at build time or shipping a snippet with
credentials baked in. Instead, the entrypoint writes a fresh snippet
to `/tmp/` on every start (env → hash → file → env var), so the
image itself carries zero credentials.

## Iterating on the image

- **Change source code** → `make docker-build`. Layer cache reuses
  the apt + uv-install layers; the COPY + uv-warm layers rebuild.
- **Change a script's dep** → `make docker-build`. uv-warm layer
  re-runs, which downloads only the delta wheels via uv's own cache.
- **Change entrypoint.sh** → `make docker-build`. Cheap — only the
  final layer changes.
- **Change Dockerfile itself** → cache invalidates from the changed
  layer down.

## Testing

`make docker-smoketest` builds nothing — it assumes the image already
exists — and hits `/api/health`, `/api/databases`, `/api/catalogs`,
and `/`. Returns non-zero on any failure and dumps the last 50 log
lines for post-mortem.

## Known limitations

- **Architecture**: x86_64 only. The vendored Caddy binary at
  `envs/caddy/caddy` is statically linked ELF x86-64. To build for
  arm64, replace it with the matching binary before running
  `docker build`.
- **First boot is slow-ish**: 60 s startup grace covers a fresh uv
  cache miss on first invocation of the pre-warmed scripts (they
  still need to hydrate their venv from cache).
- **No persistent state volume**: score sidecars and `__gene_bins`
  land inside the mounted store, so they persist with the store.
  `tmp/` in the image is ephemeral.
