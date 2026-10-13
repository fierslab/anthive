# docker/: anthive images and the compose stack

Deploying a site: read [doc/DEPLOY_CONTAINERS.md](../doc/DEPLOY_CONTAINERS.md)
and use `bin/ops/ant-stack`. Ingest on a cluster:
[doc/HPC.md](../doc/HPC.md). Image internals: [doc/DOCKER.md](../doc/DOCKER.md).

| File | Role |
| --- | --- |
| `Dockerfile` | `anthive-app`: API, web UI, MCP, one-off tools; one pinned venv |
| `Dockerfile.ingest` | `anthive-ingest`: app image plus the R/Seurat conda env |
| `compose.yaml` | the stack (static, api, web, mcp, caddy, authelia, ingest) |
| `entrypoint.sh` | role dispatcher: `api`, `web`, `mcp`, `static`, `all`, or a command |
| `standalone.sh` | `all` role: api + web + caddy in one container (`docker run`) |
| `uv-shim` | stands in for `uv` so the scripts' `uv run --script` shebangs use the venv |
| `healthcheck` | HTTP healthcheck helper used by compose |
| `gen-lock` | builds `requirements-app.lock` from the scripts' PEP-723 headers |
| `lock-overrides.txt` | the justified exceptions to the headers (pins, exclusions) |
| `requirements-app.in` / `.lock` | generated; commit both |
| `anthive-data.linux-64.explicit.txt` | exact conda export of the R env |
| `anthive-ingest.def` | Apptainer definition for HPC, built from the ingest image |
| `hpc-anthive.yaml` | default config inside the .sif (all writes to `$TMPDIR`) |
| `slurm-ingest.example.sh` | example Slurm job around the .sif |
| `smoketest.sh` | boots `anthive-app all` and checks the API |

## Build

```sh
make lock                                   # after changing any script's dependencies
make images REGISTRY=anthive TAG=dev        # anthive/anthive-app:dev + anthive/anthive-ingest:dev
make sif    REGISTRY=anthive TAG=dev        # anthive-ingest-dev.sif
```

## One container, no compose

For a quick look or a paper companion, the app image still runs everything
in one container (api + web + caddy, optional basic auth, no Authelia, no
MCP):

```sh
docker run --rm -p 8501:8501 \
    -v /path/to/h5adstore:/data/h5adstore \
    -v /path/to/genesetstore:/data/genesetstore:ro \
    -e ANTHIVE_BASIC_AUTH_USER=demo -e ANTHIVE_BASIC_AUTH_PASSWORD='a long password' \
    registry.gitlab.kuleuven.be/bdslab/anthive/anthive4/anthive-app:TAG all
```

Optional variables: `ANTHIVEPORT` (default 8501), `STARCATSTORE`,
`ANTHIVE_CHAT_API_KEY` / `ANTHIVE_CHAT_MODEL`, and `ANTHIVE_S3_ENDPOINT`,
`_BUCKET`, `_REGION`, `_ACCESS_KEY_ID`, `_SECRET_ACCESS_KEY`. Without basic
auth the container runs in auth mode `none` (public, no admin features, and only
datasets annotated `access: public` are served; `ANTHIVE_INTERNAL_REQUIRES_AUTH=false`
serves the internal ones too, only for a store that may be public as a whole).
