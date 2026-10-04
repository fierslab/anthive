"""
Secure analytics backend for ant-serve.

Provides:
  - SQL query execution against a DuckDB connection (SELECT only, row-capped,
    time-limited).  Results are persisted as Parquet server-side so that no
    bulk data ever travels over the network/MCP boundary.
  - Python visualisation execution in a subprocess with a blocked-import AST
    scan, injected DATA_PATH / OUTPUT_PATH variables, and a wall-clock timeout.
  - Schema introspection helpers.

Configuration (env vars):
  ANTHIVE_ANALYTICS_TMP   directory for parquet + figure scratch files
                          (default: ./tmp/analytics)
  ANTHIVE_ANALYTICS_ROWS  max rows per query result (default: 500000)
  ANTHIVE_QUERY_TIMEOUT   SQL query timeout in seconds (default: 120)
  ANTHIVE_VIZ_TIMEOUT     Python script timeout in seconds (default: 60)
"""

import ast
import base64
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import pandas as pd

from anthive_db import portable_layer_views

log = logging.getLogger('anthive.analytics')


# ── Configuration ──────────────────────────────────────────────────────────────

def _get_tmp_dir() -> Path:
    override = os.environ.get('ANTHIVE_ANALYTICS_TMP', '').strip()
    if override:
        tmp = Path(override).resolve()
    else:
        from config import get_tmp_dir  # noqa: PLC0415
        tmp = get_tmp_dir() / 'analytics'
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp

MAX_ROWS:       int = int(os.environ.get('ANTHIVE_ANALYTICS_ROWS', 500_000))
QUERY_TIMEOUT:  int = int(os.environ.get('ANTHIVE_QUERY_TIMEOUT', 120))
VIZ_TIMEOUT:    int = int(os.environ.get('ANTHIVE_VIZ_TIMEOUT', 60))
PREVIEW_ROWS:   int = 10
FULL_TABLE_ROWS: int = 500   # return all rows in preview when result is this small
SESSION_MAX_AGE: int = int(os.environ.get('ANTHIVE_SESSION_MAX_AGE', 3600 * 6))  # 6 hours


def _cleanup_old_sessions() -> int:
    """Remove analytics Parquet and viz files older than SESSION_MAX_AGE. Returns count removed."""
    tmp = _get_tmp_dir()
    cutoff = time.time() - SESSION_MAX_AGE
    removed = 0
    for path in tmp.iterdir():
        if path.suffix in ('.parquet', '.png', '.svg', '.pdf') and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


# ── SQL security ───────────────────────────────────────────────────────────────
#
# Validation uses DuckDB's own parser — never a regex over the raw source.
# Two pure-parse checks, both grammar-aware (comments stripped, string literals
# correctly distinguished from keywords, no false positives or negatives):
#
#   1. extract_statements: every statement must be SELECT.
#      Catches multi-statement injection, SET / COPY / ATTACH / DETACH /
#      INSERT / UPDATE / DELETE / CREATE / DROP / ALTER / PRAGMA / EXEC /
#      LOAD etc. cleanly via the StatementType enum.
#
#   2. tokenize: scan for keyword=FROM followed by either a string literal
#      (`FROM 's3://...'` path-as-table) or a known table-producing
#      function identifier (`FROM read_parquet(...)`).  The tokenizer
#      operates on the raw source, recognises comments and string literals,
#      and emits typed tokens — no regex needed.
#
# Neither call talks to the database or the network, so they are safe to run
# on hostile input.  EXPLAIN-based validation is NOT used: DuckDB will
# perform schema inference for foreign sources (e.g. open the s3:// URL) at
# EXPLAIN time, which is the exact attack we are trying to prevent.

# Functions that read files / URLs, run nested SQL, or expose engine config
# or secrets. validate_sql rejects any of these names when followed by `(`,
# anywhere in the statement (FROM, JOIN, LATERAL, comma joins, subqueries,
# scalar position), including schema-qualified (`main.read_text`) and quoted
# (`"read_text"`) spellings. This is defence in depth only: the real
# boundary is the engine lockdown in _lockdown_connection().
_BLOCKED_FROM_FUNCTIONS = frozenset({
    'read_csv', 'read_csv_auto', 'read_parquet', 'read_json', 'read_json_auto',
    'read_json_objects', 'read_json_objects_auto', 'read_ndjson',
    'read_ndjson_auto', 'read_ndjson_objects', 'read_text', 'read_arrow',
    'read_blob', 'read_xlsx', 'read_excel', 'read_duckdb',
    'scan_csv', 'scan_parquet', 'scan_arrow', 'scan_ndjson', 'scan_json',
    'csv_scan', 'parquet_scan', 'sniff_csv',
    'glob', 'httpfs_get', 'httpfs_post', 'parquet_metadata', 'parquet_schema',
    'parquet_file_metadata', 'parquet_kv_metadata', 'parquet_bloom_probe',
    'load_spatial', 'st_read', 'st_read_meta',
    'iceberg_scan', 'iceberg_metadata', 'iceberg_snapshots', 'delta_scan',
    'sqlite_scan', 'sqlite_attach', 'postgres_scan', 'postgres_attach',
    'mysql_scan',
    'query', 'query_table',
    'duckdb_settings', 'duckdb_secrets', 'which_secret', 'current_setting',
    'duckdb_databases', 'duckdb_extensions', 'getenv',
    'load_aws_credentials',
})

# Raw obs tables behind the public views: see validate_sql.
_HIDDEN_RAW_TABLES = frozenset({'__obscat', '__obsnum'})

_SESSION_ID_RE = re.compile(r'^[0-9a-f]{32}$')


def validate_session_id(session_id: Any) -> str:
    """Return session_id if it is a 32-char lowercase hex id, else raise ValueError.

    Session ids become file names under the analytics tmp dir and are placed
    inside SQL string literals, so nothing but uuid4().hex is accepted.
    """
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("Invalid session_id: expected 32 lowercase hex characters.")
    return session_id


_IDENT_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


def _token_text(sql: str, offset: int) -> str:
    """Best-effort: return the identifier/keyword text starting at offset."""
    m = _IDENT_RE.match(sql, offset)
    return m.group(0) if m else ''


def validate_sql(sql: str) -> None:
    """Raise ValueError if sql is not a single SELECT or attempts to read
    external files / URLs from the FROM clause.  Pure parse — no execution,
    no network."""
    # Step 1 — statement-type check via DuckDB's parser.
    parse_conn = duckdb.connect()  # transient in-memory connection
    try:
        stmts = parse_conn.extract_statements(sql)
    except Exception as exc:
        raise ValueError(f"Could not parse SQL: {exc}") from exc
    finally:
        parse_conn.close()

    if not stmts:
        raise ValueError("Empty SQL")
    for i, stmt in enumerate(stmts):
        if stmt.type != duckdb.StatementType.SELECT:
            raise ValueError(
                f"Statement {i+1} is {stmt.type.name}; "
                f"only SELECT / WITH … SELECT queries are permitted."
            )

    # Step 2 — token-stream check for FROM-clause attacks.
    try:
        tokens = duckdb.tokenize(sql)
    except Exception as exc:
        raise ValueError(f"Could not tokenize SQL: {exc}") from exc

    n = len(tokens)

    def _next_significant(idx: int) -> int:
        idx += 1
        while idx < n and tokens[idx][1] == duckdb.token_type.comment:
            idx += 1
        return idx

    def _raw_token(idx: int) -> str:
        start_off = tokens[idx][0]
        end_off = tokens[idx + 1][0] if idx + 1 < n else len(sql)
        return sql[start_off:end_off].strip()

    for i in range(n):
        offset, tt = tokens[i]
        if tt not in (duckdb.token_type.keyword, duckdb.token_type.identifier):
            continue
        raw = _raw_token(i)
        if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
            name = raw[1:-1].replace('""', '"')
        else:
            name = _token_text(sql, offset)
        name = name.lower()
        # PRAGMA slips past the Step-1 statement-type gate because DuckDB's
        # parser reports read-only pragmas (e.g. `PRAGMA database_list`) as
        # StatementType.SELECT.
        if tt == duckdb.token_type.keyword and name == 'pragma':
            raise ValueError(
                "PRAGMA statements are not allowed through analytics_query."
            )
        # The physical obs tables hold every author column and value,
        # including the ones a dataset's .md hides (api.obs_hidden,
        # obs_value_hidden). Queries go through the public obscat / obsnum
        # views, which apply those rules.
        if name.rsplit('.', 1)[-1] in _HIDDEN_RAW_TABLES:
            raise ValueError(
                f"Table '{name}' is internal; query the obscat / obsnum views instead."
            )
        j = _next_significant(i)
        if j >= n:
            continue
        # FROM / JOIN '<path>' (path-as-table replacement scan).
        if (tt == duckdb.token_type.keyword and name in ('from', 'join')
                and tokens[j][1] == duckdb.token_type.string_const):
            raise ValueError(
                "FROM '<path>' (path-as-table) is not allowed. "
                "External files / URLs cannot be read through analytics_query."
            )
        # Blocked function call anywhere: <name> ( ...
        if name in _BLOCKED_FROM_FUNCTIONS and _raw_token(j).startswith('('):
            raise ValueError(
                f"Function '{name}()' is not allowed in analytics queries."
            )


# ── Python security ────────────────────────────────────────────────────────────

_BLOCKED_MODULES: frozenset = frozenset({
    'os', 'sys', 'subprocess', 'socket', 'urllib', 'requests', 'httpx',
    'ftplib', 'smtplib', 'shutil', 'glob', 'pathlib', 'io', 'pickle',
    'importlib', 'ctypes', 'cffi', 'multiprocessing', 'threading',
    'signal', 'gc', 'weakref', 'resource', 'pty', 'tty',
    'builtins', '__builtin__', 'inspect', 'ast', 'dis',
    'code', 'codeop', 'tokenize', 'compileall', 'zipimport',
    'zipfile', 'tarfile', 'gzip', 'bz2', 'lzma', 'zlib',
    'struct', 'mmap', 'tempfile',
})

_BLOCKED_BUILTINS: frozenset = frozenset({
    'eval', 'exec', 'compile', '__import__', 'open', 'input',
    'breakpoint', 'memoryview',
})

# Attribute-style calls that load data from disk or network regardless of
# what module they live on — blocks pd.read_csv, pl.read_csv, etc.
_BLOCKED_DATA_LOADERS: frozenset = frozenset({
    'read_csv', 'read_table', 'read_fwf', 'read_excel', 'read_html',
    'read_json', 'read_parquet', 'read_feather', 'read_orc',
    'read_pickle', 'read_hdf', 'read_sas', 'read_spss', 'read_stata',
    'read_clipboard', 'read_xml', 'read_sql', 'read_sql_query',
    'load', 'loadtxt', 'load_npz', 'fromfile',  # numpy
    'urlopen', 'urlretrieve',                   # network (modules already import-blocked)
})


def validate_python(code: str) -> None:
    """Raise ValueError if code contains blocked imports or dangerous built-in calls."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ValueError(f"Syntax error in visualization code: {exc}") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_module = alias.name.split('.')[0]
                if top_module in _BLOCKED_MODULES:
                    raise ValueError(f"Import '{alias.name}' is not permitted.")
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top_module = node.module.split('.')[0]
                if top_module in _BLOCKED_MODULES:
                    raise ValueError(f"'from {node.module} import …' is not permitted.")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _BLOCKED_BUILTINS:
                raise ValueError(f"Call to '{func.id}()' is not permitted.")
            if isinstance(func, ast.Attribute):
                if func.attr in _BLOCKED_BUILTINS:
                    raise ValueError(f"Call to '.{func.attr}()' is not permitted.")
                if func.attr in _BLOCKED_DATA_LOADERS:
                    raise ValueError(
                        f"Call to '.{func.attr}()' is not permitted "
                        "(file/network I/O must go through the analytics_query SQL step)."
                    )


_VIZ_PREAMBLE = """\
# ── injected by analytics server — do not modify ──────────────────────────────
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import seaborn as sns
import numpy as np
DATA_PATH = {data_path!r}
OUTPUT_PATH = {output_path!r}
df = pd.read_parquet(DATA_PATH)
# ──────────────────────────────────────────────────────────────────────────────

"""


def _build_viz_script(code: str, data_path: str, output_path: str) -> str:
    return _VIZ_PREAMBLE.format(data_path=data_path, output_path=output_path) + code


# ── Schema introspection ───────────────────────────────────────────────────────

def get_schema(conn: duckdb.DuckDBPyConnection) -> List[Dict[str, Any]]:
    """Return a list of table descriptions (name, columns, row_count, sample rows)."""
    tables_raw = conn.execute("SHOW TABLES").fetchall()
    schema = []
    for (table_name,) in tables_raw:
        if table_name.lower() in _HIDDEN_RAW_TABLES:
            continue  # hidden author columns live there; the views are the public surface
        columns_raw = conn.execute(f"DESCRIBE \"{table_name}\"").fetchall()
        columns = [{'name': row[0], 'type': row[1]} for row in columns_raw]
        try:
            row_count_row = conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()
            row_count = row_count_row[0] if row_count_row else None
        except Exception:
            row_count = None
        try:
            sample_df = conn.execute(f'SELECT * FROM "{table_name}" LIMIT 3').df()
            sample = sample_df.where(pd.notna(sample_df), None).to_dict(orient='records')
        except Exception:
            sample = []
        schema.append({
            'table': table_name,
            'row_count': row_count,
            'columns': columns,
            'sample': sample,
        })
    return schema


# ── Query execution ────────────────────────────────────────────────────────────

def _sql_str(value: str) -> str:
    """Single-quoted SQL string literal with embedded quotes doubled."""
    return "'" + str(value).replace("'", "''") + "'"


_VIEW_PARQUET_RE = re.compile(r"read_parquet\(\s*'((?:[^']|'')+)'")


def _dataset_sidecar_paths(conn: duckdb.DuckDBPyConnection, db_url: str) -> List[str]:
    """Files the attached dataset legitimately reads: parquet paths named in
    its views, plus <stem>.*.parquet siblings of a local .duckdb."""
    paths: List[str] = []
    try:
        for (view_sql,) in conn.execute(
            "SELECT sql FROM duckdb_views() WHERE database_name = 'db'"
        ).fetchall():
            for match in _VIEW_PARQUET_RE.finditer(view_sql or ''):
                paths.append(match.group(1).replace("''", "'"))
    except Exception as exc:  # views are optional; lockdown still applies
        log.debug('analytics: could not list view parquet paths: %s', exc)
    if not db_url.startswith('s3://'):
        db_path = Path(db_url).resolve()
        paths.append(str(db_path))
        paths.extend(str(p) for p in db_path.parent.glob(f"{db_path.stem}.*.parquet"))
    return paths


def _lockdown_connection(
    conn: duckdb.DuckDBPyConnection,
    allowed_paths: List[str],
    allowed_directories: Optional[List[str]] = None,
) -> None:
    """Disable all external file / network / extension access except the
    listed paths, then freeze the configuration so SQL cannot undo it.

    Must run on a connection that owns its own DuckDB instance (in-memory
    connect + ATTACH): settings are per-instance, so locking a pooled file
    connection would lock every other connection on that file too.
    """
    unique_paths = sorted({str(p) for p in allowed_paths if p})
    conn.execute(f"SET allowed_paths = [{', '.join(_sql_str(p) for p in unique_paths)}]")
    if allowed_directories:
        dirs = sorted({str(d) for d in allowed_directories if d})
        conn.execute(f"SET allowed_directories = [{', '.join(_sql_str(d) for d in dirs)}]")
    conn.execute("SET enable_external_access = false")
    conn.execute("SET lock_configuration = true")


def _open_analytics_connection(
    db_url: str,
    s3_config: Optional[Dict[str, Any]] = None,
    attach_sessions: Optional[List[str]] = None,
    extra_parquet_views: Optional[Dict[str, str]] = None,
) -> duckdb.DuckDBPyConnection:
    """Open a private, locked-down DuckDB instance with the dataset attached
    read-only as ``db`` (the default schema), plus TEMP VIEWs for attached
    session parquets and extra fleet-wide parquet views."""
    conn = duckdb.connect()  # private in-memory instance, never shared
    try:
        mem_limit = os.environ.get('ANTHIVE_DUCKDB_MEMORY_LIMIT', '2GB')
        threads = int(os.environ.get('ANTHIVE_DUCKDB_THREADS', '2'))
        conn.execute(f"SET memory_limit = {_sql_str(mem_limit)}")
        conn.execute(f"SET threads = {threads}")
        allowed_directories: List[str] = []
        if db_url.startswith('s3://'):
            # httpfs is installed once by the connection pool; LOAD is idempotent.
            try:
                conn.execute("LOAD httpfs;")
            except Exception:
                conn.execute("INSTALL httpfs; LOAD httpfs;")
            s3 = s3_config or {}
            raw_ep = (s3.get('endpoint') or '').strip()
            ep_host = raw_ep.removeprefix('https://').removeprefix('http://')
            use_ssl = not raw_ep.startswith('http://')
            use_path_style = s3.get('use_path_style') or bool(ep_host)
            fields = [
                "TYPE S3",
                f"KEY_ID {_sql_str(s3.get('access_key_id') or '')}",
                f"SECRET {_sql_str(s3.get('secret_access_key') or '')}",
            ]
            if ep_host:
                fields.append(f"ENDPOINT {_sql_str(ep_host)}")
            if s3.get('region'):
                fields.append(f"REGION {_sql_str(s3['region'])}")
            fields.append(f"USE_SSL {'true' if use_ssl else 'false'}")
            if use_path_style:
                fields.append("URL_STYLE 'path'")
            conn.execute(f"CREATE OR REPLACE SECRET _s3 ({', '.join(fields)})")
            # The dataset's own folder in the bucket (its parquet sidecars).
            allowed_directories.append(db_url.rsplit('/', 1)[0] + '/')
        conn.execute(f"ATTACH {_sql_str(db_url)} AS db (READ_ONLY)")
        conn.execute("USE db")

        session_views: List[Tuple[str, str]] = []
        tmp = _get_tmp_dir()
        for i, sid in enumerate(attach_sessions or []):
            pq_path = tmp / f"{validate_session_id(sid)}.parquet"
            if pq_path.exists():
                session_views.append((f"session_{i}" if i > 0 else "scores", str(pq_path)))
        extra_views: List[Tuple[str, str]] = []
        for view_name, pq_path in (extra_parquet_views or {}).items():
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', view_name):
                raise ValueError(f"Invalid extra view name: {view_name!r}")
            if Path(pq_path).exists():
                extra_views.append((view_name, str(Path(pq_path).resolve())))

        # TEMP views shadowing the persisted layer__* views: same SQL, but
        # read_parquet points at the sidecars next to THIS host's duckdb
        # instead of the path baked in by whichever host converted it.
        layer_views = portable_layer_views(conn, db_url)

        allowed = _dataset_sidecar_paths(conn, db_url)
        allowed += [path for _, path in session_views + extra_views]
        allowed += [path for _, _, paths in layer_views for path in paths]
        _lockdown_connection(conn, allowed, allowed_directories)

        for view_name, create_sql, _paths in layer_views:
            try:
                conn.execute(create_sql)
            except duckdb.Error as exc:
                log.warning('analytics: portable view %s not installed: %s', view_name, exc)

        for view_name, pq_path in session_views + extra_views:
            conn.execute(
                f"CREATE OR REPLACE TEMP VIEW {view_name} AS "
                f"SELECT * FROM read_parquet({_sql_str(pq_path)})"
            )
        return conn
    except Exception:
        conn.close()
        raise


def run_query(
    db_url: str,
    sql: str,
    session_id: Optional[str] = None,
    s3_config: Optional[Dict[str, Any]] = None,
    timeout: Optional[int] = None,
    attach_sessions: Optional[List[str]] = None,
    extra_parquet_views: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Execute a validated SELECT query, persist results as Parquet, and return metadata.

    The caller receives column names, dtypes, row count, and a small preview
    (PREVIEW_ROWS rows).  The full result set lives at tmp/{session_id}.parquet
    and is never returned in this response; the session_id is the only handle.

    The query runs on a private in-memory DuckDB instance with the dataset
    ATTACHed read-only and external access disabled at the engine level
    (only the dataset's own files and the attached parquets are readable).
    On timeout the query is interrupted, then the connection closed.

    Args:
        db_url:     Path or S3 URL of the DuckDB file.
        sql:        SELECT query to execute.
        session_id: Optional caller-supplied ID (32 hex chars); generated if omitted.
        s3_config:  S3 credentials dict (required for s3:// URLs).
        timeout:    Query timeout in seconds, clamped to (0, QUERY_TIMEOUT].
        attach_sessions: Prior session ids to expose as TEMP VIEWs
                    (`scores`, `session_1`, ...).
        extra_parquet_views: {view_name: parquet_path} fleet-wide views.

    Returns:
        Dict with keys: session_id, columns, dtypes, row_count, preview, truncated.
    """
    if timeout is None or timeout <= 0:
        effective_timeout = QUERY_TIMEOUT
    else:
        effective_timeout = min(int(timeout), QUERY_TIMEOUT)
    validate_sql(sql)
    if session_id is not None:
        validate_session_id(session_id)
    for sid in attach_sessions or []:
        validate_session_id(sid)
    _cleanup_old_sessions()  # prune stale Parquet files to prevent disk fill
    session_id = session_id or uuid.uuid4().hex

    limited_sql = f"SELECT * FROM (\n{sql}\n) __q LIMIT {MAX_ROWS}"

    result_holder: Dict[str, Any] = {}
    error_holder:  Dict[str, Any] = {}
    conn_holder:   Dict[str, Any] = {}

    def _exec() -> None:
        try:
            query_conn = _open_analytics_connection(
                db_url, s3_config,
                attach_sessions=attach_sessions,
                extra_parquet_views=extra_parquet_views,
            )
            conn_holder['conn'] = query_conn
            result_holder['df'] = query_conn.execute(limited_sql).df()
        except Exception as exc:
            error_holder['exc'] = exc

    log.info('analytics_query session=%s timeout=%ds sql=%.100s', session_id, effective_timeout, sql.replace('\n', ' '))
    thread = threading.Thread(target=_exec, daemon=True)
    thread.start()
    thread.join(effective_timeout)

    if thread.is_alive():
        log.warning('analytics_query TIMEOUT session=%s after %ds', session_id, effective_timeout)
        dedicated_conn = conn_holder.get('conn')
        if dedicated_conn is not None:
            try:
                dedicated_conn.interrupt()
            except Exception:
                pass
            thread.join(5)
            try:
                dedicated_conn.close()
            except Exception:
                pass
        thread.join(2)
        raise TimeoutError(f"Query exceeded the {effective_timeout}s time limit and was cancelled.")

    dedicated_conn = conn_holder.get('conn')
    if dedicated_conn is not None:
        try:
            dedicated_conn.close()
        except Exception:
            pass

    if 'exc' in error_holder:
        log.error('analytics_query FAILED session=%s: %s', session_id, error_holder['exc'])
        raise RuntimeError(str(error_holder['exc'])) from error_holder['exc']

    result_df: pd.DataFrame = result_holder['df']
    parquet_path = _get_tmp_dir() / f"{session_id}.parquet"
    result_df.to_parquet(parquet_path, index=False)

    full_table = len(result_df) <= FULL_TABLE_ROWS
    preview_df = result_df if full_table else result_df.head(PREVIEW_ROWS)
    preview = preview_df.where(pd.notna(preview_df), None).to_dict(orient='records')
    return {
        'session_id':  session_id,
        'columns':     list(result_df.columns),
        'dtypes':      [str(dtype) for dtype in result_df.dtypes],
        'row_count':   len(result_df),
        'truncated':   len(result_df) == MAX_ROWS,
        'full_table':  full_table,
        'preview':     preview,
    }


# ── Visualisation execution ────────────────────────────────────────────────────

def run_viz(
    session_id: str,
    code: str,
    output_format: str = 'png',
) -> Tuple[str, str]:
    """Run a Python visualisation script and return (base64_bytes, output_format).

    The script is executed in a subprocess with a wall-clock timeout.  Before
    execution the code is scanned with the AST validator to block dangerous
    imports and built-in calls.  The preamble injects DATA_PATH (parquet from a
    previous analytics_query call) and OUTPUT_PATH (where the script must save
    its figure).

    The script MUST save its figure to OUTPUT_PATH.  Common pattern::

        fig.savefig(OUTPUT_PATH, bbox_inches='tight', dpi=150)

    Args:
        session_id:    ID returned by a prior run_query call.
        code:          Python source code for the visualisation.
        output_format: 'png' | 'svg' | 'pdf'  (default 'png').

    Returns:
        (base64_string, output_format)
    """
    if output_format not in {'png', 'svg', 'pdf'}:
        output_format = 'png'
    validate_session_id(session_id)

    tmp = _get_tmp_dir()
    parquet_path = tmp / f"{session_id}.parquet"
    if not parquet_path.exists():
        raise FileNotFoundError(
            f"No query result found for session '{session_id}'. "
            "Run analytics_query first."
        )

    validate_python(code)
    work_dir = Path(tempfile.mkdtemp(prefix='_viz_', dir=tmp))
    output_path = work_dir / f"figure.{output_format}"
    script_path = work_dir / 'script.py'
    script_path.write_text(_build_viz_script(code, str(parquet_path), str(output_path)))
    # No inherited environment (credentials, API keys, proxies); only what
    # the interpreter and matplotlib need. cwd is a private scratch dir.
    child_env = {
        'PATH': '/usr/local/bin:/usr/bin:/bin',
        'HOME': str(work_dir),
        'MPLCONFIGDIR': str(work_dir),
        'LANG': 'C.UTF-8',
        'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONNOUSERSITE': '1',
    }

    try:
        try:
            proc = subprocess.run(
                [sys.executable, '-I', str(script_path)],
                capture_output=True,
                text=True,
                timeout=VIZ_TIMEOUT,
                cwd=str(work_dir),
                env=child_env,
            )
        except subprocess.TimeoutExpired:
            log.warning('analytics_viz TIMEOUT session=%s after %ds', session_id, VIZ_TIMEOUT)
            raise TimeoutError(
                f"Visualization script exceeded the {VIZ_TIMEOUT}s time limit."
            )
        figure_bytes = output_path.read_bytes() if output_path.exists() else None
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    if proc.returncode != 0:
        stderr_snippet = (proc.stderr or '(no stderr)')[-1000:]
        log.error('analytics_viz FAILED session=%s: %s', session_id, stderr_snippet[:200])
        raise RuntimeError(f"Visualization script failed:\n{stderr_snippet}")

    if figure_bytes is None:
        raise RuntimeError(
            "Script exited successfully but produced no output file. "
            "Make sure your script saves the figure to OUTPUT_PATH, e.g.:\n"
            "    plt.savefig(OUTPUT_PATH, bbox_inches='tight', dpi=150)"
        )

    return base64.b64encode(figure_bytes).decode(), output_format
