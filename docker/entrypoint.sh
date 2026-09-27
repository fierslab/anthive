#!/usr/bin/env bash
# entrypoint.sh: role dispatcher for the anthive-app / anthive-ingest images.
#
#   api | web | mcp        run one service in the foreground (compose)
#   static                 copy the image's static bundles into the shared
#                          volumes (/srv/webapp, /srv/caddy-config), exit 0
#   all                    legacy single-container mode: api + web + caddy
#                          under ant-supervisor, config synthesised from env
#   <anything else>        exec it (e.g. `ant convert x.h5ad`, `bash`)
#
# Inputs (environment, set by compose from the site's rendered .env):
#   ANTHIVE_CONFIG        rendered anthive.yaml, mounted read-only (required
#                         for api/web/mcp and commands)
#   ANTHIVE_H5ADSTORE     h5adstore path (bind-mounted at the SAME absolute
#                         path as on the host)
#   ANTHIVE_GENESETSTORE  genesetstore path (same-path mount)
#   ANTHIVE_STARCATSTORE  optional
#   ANTHIVE_API_URL       how web/mcp/tools reach ant-serve (http://api:8080)
#
# Failures are loud and immediate: a broken mount must not become a restart
# loop that looks like a flaky service.
set -euo pipefail

_log()  { printf '[anthive:%s] %s\n' "${ROLE:-init}" "$*" >&2; }
_die()  { printf '[anthive:%s] FATAL: %s\n' "${ROLE:-init}" "$*" >&2; exit 64; }

ROLE="${1:-api}"
HOME_DIR=/opt/anthive4
cd "$HOME_DIR"

mkdir -p /tmp/anthive-links "${MPLCONFIGDIR:-/tmp/matplotlib}" "${NUMBA_CACHE_DIR:-/tmp/numba}"

# Link a mounted store under /tmp/anthive-links/<link> so the repo-relative
# defaults (data/h5adstore, data/genesetstore) resolve inside the image.
_link_store() {
    local name="$1" path="${2:-}" required="$3" link="$4"
    if [[ -z "$path" ]]; then
        [[ "$required" == yes ]] && _die "$name is not set (ant-stack renders it from site.yaml)"
        return 0
    fi
    [[ -d "$path" ]] || _die "$name=$path is not a directory inside the container (missing bind mount?)"
    [[ -r "$path" ]] || _die "$name=$path is not readable by uid $(id -u); fix host permissions or site.yaml uid/gid"
    ln -sfn "$path" "/tmp/anthive-links/$link"
}

_require_config() {
    [[ -n "${ANTHIVE_CONFIG:-}" ]] || _die "ANTHIVE_CONFIG is not set"
    [[ -r "$ANTHIVE_CONFIG" ]] || _die "ANTHIVE_CONFIG=$ANTHIVE_CONFIG is not readable (is the site config mounted?)"
}

_stores() {
    local need_data="$1"
    _link_store ANTHIVE_H5ADSTORE "${ANTHIVE_H5ADSTORE:-}" "$need_data" h5adstore
    _link_store ANTHIVE_GENESETSTORE "${ANTHIVE_GENESETSTORE:-}" no genesetstore
    _link_store ANTHIVE_STARCATSTORE "${ANTHIVE_STARCATSTORE:-}" no starcatstore
}

API_URL="${ANTHIVE_API_URL:-http://api:8080}"

case "$ROLE" in
    api)
        _require_config; _stores yes
        _log "ant-serve on :8080, data $ANTHIVE_H5ADSTORE"
        exec python bin/api/ant-serve --data-folder "$ANTHIVE_H5ADSTORE" --host 0.0.0.0 --port 8080
        ;;
    web)
        _require_config; _stores no
        _log "ant-browse on :8081 -> $API_URL"
        exec python web/ant-browse --api-url "$API_URL" --host 0.0.0.0 --port 8081
        ;;
    mcp)
        _require_config
        _log "ant-mcp on :8090 -> $API_URL"
        exec python bin/api/ant-mcp --api-url "$API_URL" --host 0.0.0.0 --port 8090
        ;;
    static)
        # Refresh the image-owned parts of the shared volumes on every `up`
        # so an upgraded image brings its new JupyterLite bundle and Caddy
        # config along. plots/ and artifacts/ (written at runtime) are kept.
        _log "syncing static bundles into /srv/webapp and /srv/caddy-config"
        # Plain cp -R (no -a): the volume roots belong to root and the
        # container runs as the site uid, so ownership/times of `.` cannot
        # be preserved (and need not be).
        rm -rf /srv/webapp/jupyter /srv/webapp/index.html /srv/webapp/404.html /srv/webapp/anthive404.png
        cp -R "$HOME_DIR/webapp-dist/." /srv/webapp/
        mkdir -p /srv/webapp/plots /srv/webapp/artifacts
        chmod 0777 /srv/webapp/plots /srv/webapp/artifacts 2>/dev/null || true
        rm -rf /srv/caddy-config/config
        cp -R "$HOME_DIR/config" /srv/caddy-config/config
        _log "done"
        exit 0
        ;;
    all)
        exec "$HOME_DIR/docker/standalone.sh"
        ;;
    *)
        # One-off command (ingest, maintenance, shell). Stores are optional.
        if [[ -n "${ANTHIVE_CONFIG:-}" ]]; then _require_config; fi
        _stores no
        exec "$@"
        ;;
esac
