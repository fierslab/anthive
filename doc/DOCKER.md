# DOCKER: maintainer notes for the anthive images

How the images are built and why. Operators want
[DEPLOY_CONTAINERS.md](DEPLOY_CONTAINERS.md) (sites) or [HPC.md](HPC.md)
(clusters); `docker/README.md` lists the files.

## Two images

`anthive-app` (about 1.4 GB) runs every service: ant-serve, ant-browse,
ant-mcp, and all Python CLI tools for one-off runs. `anthive-ingest`
(about 4.1 GB) is the app image plus the R/Seurat conda env at `/opt/rdata`
(entered with `rdata <cmd>`), for converting Seurat objects. Both are
linux/amd64 only: the wheels lock is resolved for x86_64 manylinux and the
conda export is linux-64. An arm64 build would need both locks regenerated
on that platform.

Build stages of `docker/Dockerfile`:

1. `uv` and `caddy`: binaries copied from their official images (uv only
   at build time; caddy only for the single-container `all` role).
2. `venv`: `uv pip install --no-deps -r docker/requirements-app.lock` into
   `/opt/venv`. Nothing is resolved at build time, so a rebuild installs the
   same versions.
3. `jupyter`: builds the JupyterLite bundle with the pins from `make
   build-jupyter` (jupyterlite-core 0.7.6, notebook 7.5.6; the plugins are
   unpinned there and here).
4. `app`: python:3.11-slim, tini, the venv, the repo tree, the static
   bundle at `/opt/anthive4/webapp-dist`.

The repo tree in the image is read-only at runtime and the containers run
as the site's uid, not root. Everything that writes goes to a configured
directory (`state_dir`, `tmp_dir`, `log_dir`, `webapp_dir`, the DuckDB spill
dir, the ant-convert staging dir; see `anthive.default.yaml`). A new
hard-coded path under the repo shows up immediately as a PermissionError in
the container, which is how the remaining ones were found.

## One venv instead of per-script environments

Every script keeps its PEP-723 header; that stays the source of truth for
dependencies. `docker/gen-lock` merges the headers of all runtime and CLI
scripts, applies `docker/lock-overrides.txt`, and resolves one lock with
`uv pip compile`. The overrides record the conflicts between headers:

- pyscenic/ctxcore (ant-aucell) are left out: they pin old numpy/pandas and
  cannot share a venv with current scanpy.
- numpy is pinned to 2.x: ant-ucell's header caps it below 2, but the live
  server runs numpy 2.4 and ant-ucell only uses plain array operations.
- mcp stays on 1.x: ant-mcp uses the 1.x FastMCP API.
- duckdb is pinned to the tested 1.3 to 1.5 range.

`ant-starcat` and `ant-starcat-ext` are excluded entirely (torch-scale
dependencies; starCAT was purged from the catalog). `make images` runs
`gen-lock --check` and fails when a header changed without regenerating the
lock.

The scripts' shebang (`uv run --quiet --script`) is served by
`docker/uv-shim`, installed as `/usr/local/bin/uv`. It turns `uv run [flags]
SCRIPT ARGS` into `/opt/venv/bin/python SCRIPT ARGS` and refuses anything
else, so the `ant` dispatcher, subprocess calls between tools and the
shebangs all keep working without uv or network access in the image.

## Entrypoint roles

`docker/entrypoint.sh` (PID 1 is tini):

| Role | Does |
| --- | --- |
| `api` | `ant-serve --data-folder $ANTHIVE_H5ADSTORE --host 0.0.0.0 --port 8080` |
| `web` | `ant-browse --api-url $ANTHIVE_API_URL` on 8081 |
| `mcp` | `ant-mcp --api-url $ANTHIVE_API_URL` on 8090 |
| `static` | copies JupyterLite, the 404 page and `config/` into the shared volumes, exits |
| `all` | `docker/standalone.sh`: api + web + caddy under ant-supervisor |
| anything else | exec'd as a command (`ant convert ...`, `bash`) |

Before starting, it checks that `ANTHIVE_CONFIG` is readable and that the
store mounts exist and are readable by the container's uid, and links them
under `/tmp/anthive-links/` so the repo-relative defaults
(`data/h5adstore`) resolve too. A missing mount fails at once with a clear
message instead of looping through restarts.

## The ingest image's R environment

`docker/anthive-data.linux-64.explicit.txt` is `micromamba env export
--explicit --md5` of a working `envs/anthive-data` (R 4.5.3, Seurat 5.4.0,
reticulate, anndata, scanpy). The Dockerfile creates the env from exactly
those package URLs and hashes. To upgrade R or Seurat, update a real env
from `envs/anthive-data.yml`, check it, export it again, commit the file.

## Iterating

- Source change: `make images` (cached layers; only the repo copy and later
  layers rebuild, under a minute plus export).
- Dependency change in a script header: `make lock`, then `make images`.
- Entrypoint or Caddyfile change: rebuild; `ant-stack up` re-syncs the
  config volume and recreates Caddy on every run.
- Quick check without compose: `make docker-smoketest` (single-container).

## Data and secrets never enter an image

`.dockerignore` excludes `data/`, `var/`, `tmp/`, `anthive.yaml`, `site.yaml`,
the Authelia secrets, `webapp/*` except the 404 page, and all host tool envs.
A check over a built image found no `.duckdb`, `anthive.yaml`, `site.yaml`,
SQLite state or secrets; the only `.h5ad` inside is scanpy's bundled
public example dataset in site-packages.
