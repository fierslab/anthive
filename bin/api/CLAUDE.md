# bin/api/ — data and protocol servers

The two **runtime daemons** that expose anthive's data over network protocols.

| Tool | Role |
| --- | --- |
| `ant-serve` | FastAPI REST API (port 8080) — `/databases`, `/cells`, `/plot`, `/analytics`, `/admin`. Mounted under `root_path="/api"` so Caddy can proxy `:8501/api/...` to it. |
| `ant-mcp` | MCP server (port 8090) — exposes search / plot / analytics as MCP tools for LLM clients via the streamable-http transport. |

Both are long-running uvicorn/FastMCP processes, started under `ant-supervisor`.
Both depend on `lib/anthive_db.py` (DuckDB query layer), `lib/plotting.py`
(matplotlib/seaborn rendering), `lib/analytics.py` (SELECT-only SQL sandbox),
and `lib/config.py` (anthive.yaml loader).

## The bits that bite (ant-serve)

- **One private DuckDB instance per dataset, one cursor per request.**
  `ConnectionPool` opens an in-memory instance, ATTACHes the `.duckdb`
  read-only as `db`, then hardens it (`allowed_directories` = data folder,
  the dataset's own resolved directory, spill dir; `enable_external_access
  = false`; `lock_configuration = true`). Handlers get `cursor()` + `USE db`
  and must hand it back via `return_connection` (`_pooled_db` does). No
  `SET` works on a pooled cursor, and a pandas DataFrame can't be queried
  by variable name (replacement scans are external access); use
  `cursor.register()`. Open instances are LRU-capped
  (`duckdb_max_open_databases`, default 48) because each starts
  `threads - 1` OS threads. Server knobs are `duckdb_server_threads`
  (default 8) and `duckdb_server_memory_limit`; `duckdb_threads` /
  `duckdb_memory_limit` belong to ant-convert.
- **Handlers that touch DuckDB, S3 or files are plain `def`** (Starlette
  threadpool). An `async def` handler must push every blocking call through
  `asyncio.to_thread`, or one slow query stalls every request. Large pandas
  serialisation holds the GIL, so big responses are streamed in chunks
  (see `_cells_json_chunks`).
- **Rescan never empties the registry.** `_scan_databases` /
  `_cache_all_metadata` build new maps and swap them in; a dataset whose
  metadata load fails transiently keeps its previous entry. Rescan retires
  pool instances: in-flight queries finish on the old one.
- **DuckDB blocks cross-process read-write.** Annotation writers (`ant-score`,
  `ant-starcat`, `ant-annotate` from `bin/annot/`) can't open a duckdb r/w
  while ant-serve holds it r/o. Coordination via `POST /admin/release/{db_id}`
  → write → `POST /admin/restore/{db_id}`.
- **Field-type detection** (gene / obs-num / obs-cat / embedding) is automatic
  from the name; plot and query endpoints depend on it.
- **Plot cache** is in-memory LRU keyed on `(db_id, query, format, duckdb +
  scores-sidecar mtime)`; captures the X-Plot-Caption + bytes. `?cache=miss`
  skips the lookup, re-renders and refreshes the entry (`X-Plot-Cache:
  BYPASS`); it is not part of the key.
- `analytics_query` runs sandboxed SELECT-only SQL via `lib/analytics.py` —
  see `tests/test_analytics_security.py` before touching the sandbox.

## Personal MCP links (ant-serve side)

- Token store: `lib/mcp_tokens.py` (`McpLinkStore`), SQLite at
  `<state_dir>/mcp_tokens.db`. Tokens are HMAC-derived
  (`amcp_<public_id>_<mac>`), never stored; rotate = generation + 1.
- Endpoints: `GET /me/mcp`, `POST /me/mcp/rotate`, `DELETE /me/mcp`
  (Remote-User required), `GET/DELETE /admin/mcp-links[/{user}]` (admins),
  `POST /internal/mcp/verify` (internal secret only; Caddy 403s
  `/api/internal/*`). The web UI's `/account/mcp` page consumes `/me/mcp`.
- `_InternalSecretGuard`: a wrong `X-Anthive-Internal` is a 401 everywhere.
- `/admin/rescan|release|restore|refresh-views` use
  `_require_admin_or_local`: loopback tool calls without proxy headers or
  Remote-User (ant-sync, AnnotationWriter, ant-refresh-views, ant-score),
  internal-secret calls without a user (tools in another container), or
  admins. Through the proxy a normal user gets 403.
- Secrets: `internal_secret` / `mcp.token_secret` in config, else env
  `ANTHIVE_INTERNAL_SECRET` / `ANTHIVE_MCP_TOKEN_SECRET`, else auto-created
  0600 files in `<state_dir>/secrets/` (`config.get_or_create_secret`).

- **Paper library + literature search** (2.39): `lib/library_service.py`
  holds the records, the resolved `papers:` references and the
  `LibraryIndex`; loaded in a thread at startup and on `/admin/rescan`.
  Handlers stay thin. Access: `restricted` papers admin-only; calls from
  ant-mcp (internal secret + Remote-User) count as `for_llm` and honour
  `anthive_llm`. Query model: fastembed, cache from `get_model_cache_dir()`.

## ant-mcp specifics

- FastMCP `streamable-http` transport on `/mcp`, bound to loopback.
- **Personal MCP links** (`doc/MCP_LINKS.md`): `/mcp/u/<token>` (rewritten to
  `/mcp`) or `Authorization: Bearer amcp_...`. `_McpLinkAuthMiddleware`
  verifies via ant-serve `POST /internal/mcp/verify` (header
  `X-Anthive-Internal: <internal_secret>`), caches `mcp.verify_cache_seconds`,
  rate-limits per token, and puts the owner in the ASGI scope. `AnthiveMCP`
  (FastMCP subclass) reads it per message, hides `ADMIN_ONLY_TOOLS` from
  non-admins, and every ant-serve call carries `Remote-User`/`Remote-Groups`
  + the internal secret, so tools run as that user.
- `mcp.require_token: false` restores the old unauthenticated `/mcp` (only
  for localhost-only setups). With tokens on, FastMCP's DNS-rebinding check is
  disabled, because behind Caddy the Host header is the public name.
- Pinned to `mcp>=1.10,<2`: the 2.x SDK renamed FastMCP.
- OAuth-discovery probes (`/.well-known/oauth-*`, `/register`) are
  short-circuited by Caddy with valid JSON 404s (see `config/Caddyfile`).
- Plot output saved to `webapp/plots/` for URL-based serving; falls back to
  data: URIs if `plots_dir` is null.

## Hosts / ports

Default bind: `127.0.0.1`. Override via `anthive.yaml`:
```yaml
rest_api_host: 0.0.0.0   # bind on all interfaces (skip Caddy)
rest_api_port: 8080
```
MCP defaults to `127.0.0.1:8090` (`mcp.host` / `mcp.port` in anthive.yaml).

## Server-selection signal — `/health` carries the "how fast is this server"

A client (e.g. `vark`) configured with multiple anthive endpoints should
prefer the lowest `mean_response_ms` / `p50_response_ms` exposed by
`GET /api/health`.

The middleware (`RequestLoggingMiddleware` in `ant-serve`) records every
non-probe request's wall-clock into a 1024-slot ring buffer.  `/health`
and `/metrics` then expose:

| field | from | use |
| --- | --- | --- |
| `mean_response_ms` | `/health` + `/metrics.latency_ms.mean` | quick comparison; skewed by plot renders |
| `p50_response_ms`  | `/health` + `/metrics.latency_ms.p50` | recommended — outlier-resistant |
| `latency_ms.{p95,p99,max}` | `/metrics` only | diagnostics |
| `n_samples` | both | how many real requests fed the stats; `null` until first hit |

**Excluded** from the stats (so probes don't make a server look fast):
`/health`, `/metrics`, `/admin/plot-cache/stats` (matched by suffix).

### Recipe for vark (or any client picking between servers)

```python
def pick(endpoints, auth=None):
    import time, requests
    best, best_score = None, float('inf')
    for url in endpoints:
        try:
            t0 = time.perf_counter()
            r = requests.get(f"{url}/api/health", auth=auth, timeout=2)
            rtt_ms = (time.perf_counter() - t0) * 1000
            body = r.json() if r.status_code == 200 else {}
        except (requests.RequestException, ValueError):
            continue
        # Use server-side p50 once it has enough samples; otherwise fall
        # back to the network RTT we just observed.
        if body.get('n_samples', 0) >= 10:
            score = body.get('p50_response_ms') or rtt_ms
        else:
            score = rtt_ms
        if score < best_score:
            best, best_score = url, score
    return best
```

### Caveats worth knowing

- **Server-side wall-clock only.** Excludes network RTT between client
  and server — that's why the recipe above also tracks the probe's RTT.
- **Cold-start = `null`.** A freshly-restarted server has `n_samples=0`
  until traffic warms it up; clients should fall back to probe RTT.
- **Workload skew.** A server that mostly served `/info` looks fast even
  if its `/plot` is slow.  For honest comparisons across servers, hit
  representative endpoints, not just `/health`.
- **Ring buffer is per-process and in-memory.** Not persisted across
  restarts; not aggregated across replicas.
