# anthive4 — Docker image

Single-container image for reproducible paper deployments. Runs
`ant-serve`, `ant-browse`, and `caddy` inside one container under a
supervisor (PID 1 is `tini` → `ant-supervisor`).

## Quick start

```bash
docker build -t anthive:latest -f docker/Dockerfile .

docker run --rm -p 8501:8501 \
    -e ANTHIVEPORT=8501 \
    -v /path/to/your/h5adstore:/data/h5adstore \
    -v /path/to/your/genesetstore:/data/genesetstore \
    anthive:latest
```

Open [http://localhost:8501/](http://localhost:8501/) — the web UI
lands on `/browse` and the REST API is at `/api`.

## Required env vars

| Var | Purpose |
| --- | --- |
| `ANTHIVEPORT` | Public port. Also the container-side port Caddy binds to. Default `8501`. |
| `H5ADSTORE` | Path *inside the container* to the h5adstore mount. Default `/data/h5adstore`. Pass the flag AND bind-mount the directory. |
| `GENESETSTORE` | Same, for the geneset store. Default `/data/genesetstore`. |

## Optional env vars

| Var | Purpose |
| --- | --- |
| `STARCATSTORE` | Path to a starcatstore mount (starCAT catalogs). No default — feature stays disabled if unset. |
| `ANTHIVE_CHAT_API_KEY` | LLM API key for the chat panel. Unset → chat disabled. |
| `ANTHIVE_CHAT_MODEL` | Chat model id. Default `anthropic/claude-opus-4`. |
| `ANTHIVE_BASIC_AUTH_USER` + `ANTHIVE_BASIC_AUTH_PASSWORD` | Both must be set to enable Caddy basic-auth. Unset → open. |
| `ANTHIVE_S3_ENDPOINT` / `_BUCKET` / `_REGION` / `_ACCESS_KEY_ID` / `_SECRET_ACCESS_KEY` | Only needed if datasets should be fetched from S3 at request time. |

## Volume layout

The image expects two mandatory mounts (bind or named volume):

- **`/data/h5adstore`** — the h5ad + duckdb + .md store. Read-write
  (score sidecars and `__gene_bins` may be written on demand).
- **`/data/genesetstore`** — the geneset store.

Optional third mount:

- **`/data/starcatstore`** — a starCAT catalog root, if `STARCATSTORE` is set.

## Full example

```bash
docker run --rm -d \
    --name anthive \
    -p 8501:8501 \
    -e ANTHIVEPORT=8501 \
    -e ANTHIVE_BASIC_AUTH_USER=reviewer \
    -e ANTHIVE_BASIC_AUTH_PASSWORD='hunter2' \
    -e ANTHIVE_CHAT_API_KEY="$OPENROUTER_KEY" \
    -e STARCATSTORE=/data/starcatstore \
    -v /srv/anthive/h5adstore:/data/h5adstore \
    -v /srv/anthive/genesetstore:/data/genesetstore \
    -v /srv/anthive/starcatstore:/data/starcatstore \
    anthive:latest
```

## Health / lifecycle

- **Health check**: `GET /api/health` runs every 15 s (Docker
  `HEALTHCHECK`). First-boot grace period is 60 s.
- **Shutdown**: `docker stop <container>` sends SIGTERM via tini →
  supervisor → children. All children get a 3 s SIGTERM window, then
  SIGKILL.
- **Logs**: everything is line-multiplexed by the supervisor with
  colored per-service prefixes.

## What the image does NOT do

- Does not run Authelia (auth is via Caddy basic-auth, opt-in).
- Does not run the MCP server (use vark CLI on the host).
- Does not include any h5adstore or starcatstore data — those must be
  mounted from the host.
- Does not open ports other than `ANTHIVEPORT`.

## Rebuilding after a repo change

The uv cache is warmed at build time. If you bump a dep in a script's
`# dependencies = [...]` header, `docker build` will re-warm on the
next run. If nothing changed, the `RUN uv run --script … --help` layer
hits the cache and the build finishes in seconds.

## Smoke test

```bash
make docker-build
make docker-smoketest    # boots the image, hits /api/health + /api/databases, tears down
```

## Troubleshooting

- **`H5ADSTORE: directory does not exist`** — you forgot the `-v`
  flag or spelled the container-side path wrong. Match the value of
  `H5ADSTORE` with the `-v ...:/data/h5adstore` right-hand side.
- **`H5ADSTORE: directory is not readable`** — a host-side permissions
  problem. The container runs as UID 1000. Either `chown -R 1000:1000`
  the host directory, or bind-mount with `:ro` and confirm your host
  files are world-readable.
- **`basic-auth is half-configured`** — you set the user but not the
  password (or vice versa). Set both or neither.
- **`/api/health` returns 401** — basic-auth is on. The healthcheck
  exempts `/api/health` from auth, so this usually means the auth
  snippet failed to render. Check container logs for
  `[anthive] basic auth ENABLED`.
