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

- **Connections are read-only.** `ConnectionPool` opens each duckdb with
  `read_only=True` and pools up to N handles per db. Metadata is cached;
  `POST /admin/rescan` clears and reloads.
- **DuckDB blocks cross-process read-write.** Annotation writers (`ant-score`,
  `ant-starcat`, `ant-annotate` from `bin/annot/`) can't open a duckdb r/w
  while ant-serve holds it r/o. Coordination via `POST /admin/release/{db_id}`
  → write → `POST /admin/restore/{db_id}`.
- **Field-type detection** (gene / obs-num / obs-cat / embedding) is automatic
  from the name; plot and query endpoints depend on it.
- **Plot cache** is in-memory LRU keyed on `(db_id, query, format, mtime)`;
  captures the X-Plot-Caption + bytes; bypassed by `?cache=miss`.
- `analytics_query` runs sandboxed SELECT-only SQL via `lib/analytics.py` —
  see `tests/test_analytics_security.py` before touching the sandbox.

## ant-mcp specifics

- FastMCP `streamable-http` transport on `/mcp`.
- Public path through Caddy at `:8501/mcp` (no auth — MCP clients use their own).
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
MCP defaults to `0.0.0.0:8090` (`mcp.host` / `mcp.port` in anthive.yaml).

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
