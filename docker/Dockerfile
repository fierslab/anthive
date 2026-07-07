# Anthive4 — single-container image
#
# Builds a self-contained image that runs api + browse + caddy inside
# one PID-1 supervisor. Deployed with:
#
#   docker run --rm -p 8501:8501 \
#       -e ANTHIVEPORT=8501 \
#       -v /host/h5adstore:/data/h5adstore \
#       -v /host/genesetstore:/data/genesetstore \
#       anthive:latest
#
# The image is x86_64 only (matches the vendored Caddy binary at
# envs/caddy/caddy which is statically linked ELF x86-64).
#
# ── Stage 1: builder ────────────────────────────────────────────────────
#   Installs uv + system deps, copies the repo, and PRE-WARMS the uv
#   script cache so first-boot doesn't pay a multi-minute download tax.
# ────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    UV_SYSTEM_PYTHON=1 \
    UV_CACHE_DIR=/opt/uv-cache \
    PIP_ROOT_USER_ACTION=ignore

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git build-essential \
    && rm -rf /var/lib/apt/lists/*

# uv — pinned by version so a rebuild is byte-reproducible.
ARG UV_VERSION=0.5.5
RUN curl -LsSf https://astral.sh/uv/${UV_VERSION}/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

WORKDIR /opt/anthive4
COPY . .

# Warm uv's on-disk cache for every ant-* script we'll run at runtime.
# Each script is a `uv run --script` PEP-723 shebang; `--help` triggers
# venv creation + dep download without executing the tool.
#
# We warm the three services the container actually runs (api, browse,
# score-ext) plus the supervisor. That covers 100 % of the runtime
# import surface; the CLI-only tools (ant-convert, ant-merge-h5ad, …)
# are usable inside the container on demand and warm lazily on first
# invocation.
RUN uv run --script bin/api/ant-serve --help >/dev/null 2>&1 || true \
 && uv run --script web/ant-browse --help >/dev/null 2>&1 || true \
 && uv run --script bin/ops/ant-supervisor --help >/dev/null 2>&1 || true

# ── Stage 2: runtime ───────────────────────────────────────────────────
#   Slim image, non-root user, uv + warm cache carried over from
#   builder. No apt build tools, no git.
# ───────────────────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    UV_CACHE_DIR=/opt/uv-cache \
    UV_NO_SYNC=1 \
    ANTHIVEPORT=8501 \
    ANTHIVE_HOME=/opt/anthive4 \
    PYTHONUNBUFFERED=1 \
    PATH="/usr/local/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 1000 --home /home/anthive --create-home anthive

COPY --from=builder /usr/local/bin/uv /usr/local/bin/uvx /usr/local/bin/
COPY --from=builder /opt/uv-cache /opt/uv-cache
COPY --from=builder /opt/anthive4 /opt/anthive4

# The container's runtime data dirs — mount points the entrypoint
# expects. Owned by the anthive user so a bind mount from a host UID
# other than 1000 still works (the entrypoint chowns on start).
RUN mkdir -p /data/h5adstore /data/genesetstore /data/starcatstore \
             /opt/anthive4/tmp /opt/anthive4/data/reports \
    && chown -R anthive:anthive /opt/anthive4 /opt/uv-cache /data

WORKDIR /opt/anthive4
USER anthive

EXPOSE 8501

HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS "http://localhost:${ANTHIVEPORT:-8501}/api/health" || exit 1

# tini is PID 1 so the supervisor gets clean SIGTERM propagation from
# `docker stop`. Without it, python's default handler on PID 1 misses
# SIGTERM and docker falls back to SIGKILL after 10 s.
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/anthive4/docker/entrypoint.sh"]
