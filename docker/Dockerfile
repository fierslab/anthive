# anthive-app: the image every anthive service container runs.
#
#   api    ant-serve (REST API)          docker/entrypoint.sh api
#   web    ant-browse (web UI)           docker/entrypoint.sh web
#   mcp    ant-mcp (MCP server)          docker/entrypoint.sh mcp
#   static copy the static bundles (JupyterLite, 404 page, Caddy config)
#          into the shared volumes, then exit
#   all    single-container mode (api + web + caddy under ant-supervisor)
#   <cmd>  anything else runs as a command, e.g. `ant convert x.h5ad`
#
# Deployed with docker/compose.yaml via bin/ops/ant-stack. The image never
# contains data, secrets or site config: stores are bind-mounted, the site
# config is mounted read-only and named by ANTHIVE_CONFIG.
#
# Python: ONE pinned venv at /opt/venv built from docker/requirements-app.lock
# (generated from the scripts' PEP-723 headers by docker/gen-lock). The
# scripts' `uv run --script` shebangs are served by docker/uv-shim, which
# execs /opt/venv/bin/python, so no per-script environments exist at runtime.
#
# Architecture: linux/amd64 only (the conda R env lock in the ingest image
# is linux-64 and the wheels lock is resolved for x86_64 manylinux).

ARG PYTHON_IMAGE=python:3.11-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.9.7
ARG CADDY_IMAGE=caddy:2.11-alpine

# ── uv binary (build-time only) ─────────────────────────────────────────
FROM ${UV_IMAGE} AS uv

# ── caddy binary (static Go build; only the `all` standalone mode uses it,
#    compose runs the official caddy image as its own container) ──────────
FROM ${CADDY_IMAGE} AS caddy

# ── venv: resolve nothing, install exactly the lock ─────────────────────
FROM ${PYTHON_IMAGE} AS venv
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
COPY docker/requirements-app.lock /tmp/requirements-app.lock
RUN uv venv --python /usr/local/bin/python3.11 /opt/venv \
 && uv pip install --python /opt/venv/bin/python --no-deps -r /tmp/requirements-app.lock \
 && /opt/venv/bin/python -c "import duckdb, fastapi, pandas, anndata, mcp; print('venv ok', duckdb.__version__)"

# ── JupyterLite bundle (same pins as `make build-jupyter`) ──────────────
FROM ${PYTHON_IMAGE} AS jupyter
COPY --from=uv /uv /usr/local/bin/uv
ARG BUILD_JUPYTER=1
COPY frontend/jupyter /src/frontend/jupyter
RUN mkdir -p /out/jupyter \
 && if [ "$BUILD_JUPYTER" = "1" ]; then \
      cd /src && DOIT_DB=/tmp/.jupyterlite.doit.db \
      uv tool run --from 'jupyterlite-core==0.7.6' \
        --with jupyterlite-pyodide-kernel --with jupyterlab-widgets \
        --with 'notebook==7.5.6' --with jupyter-server \
        --with jupyterlab-open-url-parameter \
        jupyter-lite build --force --contents frontend/jupyter/jupyter \
          --output-dir /out/jupyter ; \
    else echo "BUILD_JUPYTER=0: shipping without the JupyterLite bundle"; fi \
 && cp /src/frontend/jupyter/index.html /out/index.html

# ── runtime ─────────────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS app

LABEL org.opencontainers.image.title="anthive-app" \
      org.opencontainers.image.description="anthive single-cell browser: API, web UI, MCP" \
      org.opencontainers.image.source="https://codeberg.org/mfiers/anthive4"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ANTHIVE_HOME=/opt/anthive4 \
    PATH="/opt/anthive4/bin:/opt/venv/bin:${PATH}" \
    HOME=/tmp \
    MPLCONFIGDIR=/tmp/matplotlib \
    NUMBA_CACHE_DIR=/tmp/numba \
    XDG_CACHE_HOME=/tmp/cache

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl tini libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 1000 --home /tmp --no-create-home anthive

COPY --from=venv /opt/venv /opt/venv
COPY docker/uv-shim /usr/local/bin/uv
COPY docker/healthcheck /usr/local/bin/anthive-healthcheck
COPY . /opt/anthive4
COPY --from=jupyter /out /opt/anthive4/webapp-dist
COPY --from=caddy /usr/bin/caddy /opt/anthive4/envs/caddy/caddy

# Shared-volume mount points are world-writable so the containers can run
# as any site UID (compose sets user: to the site's uid:gid); a named volume
# inherits these permissions on first use. The repo tree itself stays
# read-only: every runtime write goes to a configured directory.
RUN chmod 0755 /usr/local/bin/uv /usr/local/bin/anthive-healthcheck \
 && cp /opt/anthive4/webapp/404.html /opt/anthive4/webapp/anthive404.png /opt/anthive4/webapp-dist/ \
 && rm -rf /opt/anthive4/webapp \
 && mkdir -p /srv/webapp /srv/caddy-config /var/lib/anthive /scratch \
 && chmod 0777 /srv/webapp /srv/caddy-config /var/lib/anthive /scratch \
 && mkdir -p /opt/anthive4/data \
 && ln -s /tmp/anthive-links/h5adstore    /opt/anthive4/data/h5adstore \
 && ln -s /tmp/anthive-links/genesetstore /opt/anthive4/data/genesetstore \
 && ln -s /tmp/anthive-links/starcatstore /opt/anthive4/data/starcatstore \
 && ln -s /srv/webapp /opt/anthive4/webapp \
 && /opt/venv/bin/python -m compileall -q /opt/anthive4/lib >/dev/null \
 && /opt/venv/bin/python -c "import sys; sys.path.insert(0, '/opt/anthive4/lib'); import config, anthive_db, analytics; print('lib ok')"

WORKDIR /opt/anthive4
USER anthive

EXPOSE 8080 8081 8090

ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/anthive4/docker/entrypoint.sh"]
CMD ["api"]
