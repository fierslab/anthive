#!/usr/bin/env bash
# smoketest.sh — boot the image, hit /api/health, tear down.
#
# Usage: bash docker/smoketest.sh <image:tag> <port>
#
# Uses the repo's own data/h5adstore + data/genesetstore as the fixture.
set -euo pipefail

IMAGE="${1:-anthive:latest}"
PORT="${2:-8501}"
CONTAINER_NAME="anthive-smoketest-$$"

# Any data/h5adstore + data/genesetstore is enough — even empty dirs
# would let the image boot; but if the actual repo checkout has them,
# use those so /api/databases returns non-empty.
H5AD="${H5ADSTORE:-$PWD/data/h5adstore}"
GS="${GENESETSTORE:-$PWD/data/genesetstore}"

_log() { printf '\033[36m[smoketest]\033[0m %s\n' "$*" >&2; }
_die() { printf '\033[31m[smoketest]\033[0m %s\n' "$*" >&2; docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true; exit 1; }

trap '_log "tearing down"; docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true' EXIT

for d in "$H5AD" "$GS"; do
    [[ -d "$d" ]] || _die "not a directory: $d"
done

_log "starting $IMAGE as $CONTAINER_NAME on :$PORT"
docker run -d --rm \
    --name "$CONTAINER_NAME" \
    -p "$PORT:$PORT" \
    -e ANTHIVEPORT="$PORT" \
    -v "$H5AD:/data/h5adstore" \
    -v "$GS:/data/genesetstore" \
    "$IMAGE" all >/dev/null

_log "waiting for /api/health (up to 180s — ant-serve scans + .md validation take ~45s for a large store)"
for i in $(seq 1 180); do
    if curl -fsS "http://localhost:$PORT/api/health" >/dev/null 2>&1; then
        _log "health OK after ${i}s"
        break
    fi
    sleep 1
    if [[ $i -eq 180 ]]; then
        docker logs "$CONTAINER_NAME" | tail -50 >&2
        _die "container never became healthy in 180s"
    fi
done

_log "checking /api/databases"
DATABASES_JSON=$(curl -fsS "http://localhost:$PORT/api/databases" 2>&1) \
    || _die "/api/databases failed"

DB_COUNT=$(printf '%s' "$DATABASES_JSON" | python3 -c 'import sys,json;print(len(json.load(sys.stdin).get("databases",[])))' 2>/dev/null || echo "?")
_log "/api/databases returned $DB_COUNT datasets"

_log "checking /api/catalogs"
curl -fsS "http://localhost:$PORT/api/catalogs" >/dev/null \
    || _die "/api/catalogs failed"

_log "checking browse root"
curl -fsS "http://localhost:$PORT/" >/dev/null \
    || _die "root path failed (ant-browse not reachable via caddy?)"

_log "PASS — $IMAGE served health + databases + catalogs + browse root"
