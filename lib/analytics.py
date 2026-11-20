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
import concurrent.futures
import functools
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import pandas as pd

from anthive_db import portable_layer_views
from scores_parquet import install_score_views

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
# Longest query text accepted. Validation (DuckDB's parser plus the tree walk)
# runs before the time limit starts; at this size it takes under a second.
MAX_SQL_CHARS: int = 50_000


def _cleanup_old_sessions() -> int:
    """Remove analytics Parquet and viz files older than SESSION_MAX_AGE. Returns count removed."""
    tmp = _get_tmp_dir()
    cutoff = time.time() - SESSION_MAX_AGE
    removed = 0
    for path in tmp.iterdir():
        try:
            old = path.suffix in ('.parquet', '.png', '.svg', '.pdf') and path.stat().st_mtime < cutoff
        except OSError:
            continue  # removed meanwhile by another request
        if old:
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

# ── Allowlist over DuckDB's own syntax tree (json_serialize_sql) ──────────────
#
# The token scan above is a blocklist: it stops the names it knows. Two
# forms got past it (review 2026-11, issue #2): a file path used as a table
# in a position the scan did not look at, and SQL hidden in a string literal
# and run by json_execute_serialized_sql. So validate_sql also walks the
# parsed tree and accepts only what is listed here:
#
#   - table functions: only the generators below (no file, catalog or
#     engine readers);
#   - tables: a plain identifier, no catalog or schema other than the
#     dataset's own; `__`-prefixed tables only __cells / __genes; and, at
#     execution (check_relations), only relations the dataset publishes or
#     CTEs of the query itself;
#   - scalar functions: anything but the few that read engine state.
ALLOWED_TABLE_FUNCTIONS = frozenset({
    'range', 'generate_series', 'unnest', 'json_each', 'json_tree',
})
# Scalar, aggregate and window functions: the ones DuckDB itself defines,
# minus these and minus macros that read the catalog (allowed_scalar_functions).
# Names DuckDB does not know are refused (fail closed after an upgrade).
BLOCKED_SCALAR_FUNCTIONS = frozenset({
    'current_setting', 'current_query', 'getenv', 'getvariable',
    'json_execute_serialized_sql', 'json_deserialize_sql', 'json_serialize_sql',
    'json_serialize_plan',   # binds and optimises SQL from a string: settings, plans, min/max oracle
    'stats',                 # storage statistics below view masks and filters
    'write_log', 'which_secret', 'sleep_ms', 'pg_sleep', 'nextval', 'currval',
})
# A macro whose body names one of these reads the catalog or engine state
# (pg_get_viewdef, format_type, get_block_size, pg_get_constraintdef, ...).
_MACRO_BODY_MARKERS = ('from ', 'from\n', 'duckdb_', 'pragma_', 'current_setting', 'getvariable')


_WINDOW_FUNCTIONS = frozenset({
    'row_number', 'rank', 'dense_rank', 'percent_rank', 'cume_dist', 'ntile',
    'lag', 'lead', 'first_value', 'last_value', 'nth_value', 'rank_dense',
})


@functools.lru_cache(maxsize=1)
def allowed_scalar_functions() -> frozenset:
    """Lower-case names of the scalar, aggregate and window functions a query may call."""
    conn = duckdb.connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT function_name, function_type, coalesce(macro_definition, '') "
            "FROM duckdb_functions() WHERE function_type IN ('scalar', 'aggregate', 'macro')").fetchall()
    finally:
        conn.close()
    allowed = set()
    for name, function_type, body in rows:
        lowered = str(name).lower()
        if lowered in BLOCKED_SCALAR_FUNCTIONS:
            continue
        if function_type == 'macro' and any(marker in str(body).lower() for marker in _MACRO_BODY_MARKERS):
            continue
        allowed.add(lowered)
    # A macro of the same name as a scalar is listed twice; any blocked form blocks the name.
    blocked_macros = {str(name).lower() for name, function_type, body in rows
                      if function_type == 'macro'
                      and any(marker in str(body).lower() for marker in _MACRO_BODY_MARKERS)}
    # Window functions are not in duckdb_functions() before DuckDB 1.5.
    return frozenset((allowed | _WINDOW_FUNCTIONS) - blocked_macros)
# Base tables open to queries although `__`-prefixed: cell and gene ids and
# names only, no author columns.
_OPEN_RAW_TABLES = frozenset({'__cells', '__genes'})
# Catalog / schema qualifiers that name the attached dataset or the query's
# own temp views ('db' is the attached dataset, see _open_analytics_connection).
_OWN_QUALIFIERS = frozenset({'', 'db', 'main', 'temp', 'memory'})
_PLAIN_TABLE_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


@dataclass(frozen=True)
class SqlReferences:
    """Relations a validated query reads: base table names and its own CTE names (lower case)."""
    tables: frozenset
    ctes: frozenset


def _serialized_tree(sql: str) -> Dict[str, Any]:
    """DuckDB's parse tree of sql (pure parse, nothing is executed or opened)."""
    parse_conn = duckdb.connect()
    try:
        raw = parse_conn.execute('SELECT json_serialize_sql(?)', [sql]).fetchone()[0]
    except Exception as exc:
        raise ValueError(f"Could not parse SQL: {exc}") from exc
    finally:
        parse_conn.close()
    try:
        tree = json.loads(raw)
    except (RecursionError, ValueError) as exc:
        raise ValueError("Query is nested too deeply to analyse.") from exc
    if not isinstance(tree, dict) or tree.get('error'):
        detail = tree.get('error_message') or tree.get('error_type') if isinstance(tree, dict) else 'no parse tree'
        raise ValueError(f"Could not analyse SQL: {detail}")
    return tree


def _check_tree(sql: str) -> SqlReferences:
    """Walk the parse tree of sql; raise ValueError on anything outside the allowlist."""
    tree = _serialized_tree(sql)
    tables: set = set()
    ctes: set = set()
    table_function_nodes: set = set()
    stack: List[Any] = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        node_type = node.get('type')
        if isinstance(node.get('cte_map'), dict):
            for entry in node['cte_map'].get('map') or []:
                if isinstance(entry, dict) and isinstance(entry.get('key'), str):
                    ctes.add(entry['key'].lower())
        if node_type == 'TABLE_FUNCTION':
            function = node.get('function') or {}
            name = str(function.get('function_name') or '').lower()
            qualified = function.get('schema') or function.get('catalog')
            if qualified or name not in ALLOWED_TABLE_FUNCTIONS:
                raise ValueError(
                    f"Table function '{name}()' is not allowed; only "
                    f"{', '.join(sorted(ALLOWED_TABLE_FUNCTIONS))} are.")
            table_function_nodes.add(id(function))
        elif node_type == 'BASE_TABLE':
            name = str(node.get('table_name') or '')
            lowered = name.lower()
            if lowered in _HIDDEN_RAW_TABLES:
                raise ValueError(
                    f"Table '{name}' is internal; query the obscat / obsnum views instead.")
            catalog = str(node.get('catalog_name') or '').lower()
            schema = str(node.get('schema_name') or '').lower()
            if catalog not in _OWN_QUALIFIERS or schema not in _OWN_QUALIFIERS:
                raise ValueError(
                    f"Table '{'.'.join(p for p in (catalog, schema, name) if p)}' is not a table "
                    f"of this dataset; system catalogs and other databases cannot be queried "
                    f"(the tables and their columns: SELECT * FROM schema_columns, or "
                    f"GET /databases/<id>/analytics/schema).")
            if not _PLAIN_TABLE_RE.fullmatch(name):
                raise ValueError(
                    f"'{name}' is not a table name: files and URLs cannot be read as tables.")
            if lowered.startswith('__') and lowered not in _OPEN_RAW_TABLES:
                raise ValueError(
                    f"Table '{name}' is internal; query the published views (obscat, obsnum, "
                    f"layer__*, cells, genes) instead.")
            tables.add(lowered)
        elif node_type == 'SHOW_REF':
            raise ValueError("DESCRIBE / SHOW / SUMMARIZE are not available here; the tables and columns "
                             "are in the view schema_columns (SELECT * FROM schema_columns), or use "
                             "GET /databases/<id>/analytics/schema.")
        if ('function_name' in node and node.get('class') in ('FUNCTION', 'WINDOW', 'AGGREGATE')
                and id(node) not in table_function_nodes):
            name = str(node.get('function_name') or '').lower()
            # Checked by name whatever the qualifier: DuckDB itself writes
            # some built-ins qualified (a list literal is main.list_value).
            if name not in allowed_scalar_functions() and name not in ALLOWED_TABLE_FUNCTIONS:
                raise ValueError(f"Function '{name}()' is not allowed in analytics queries.")
        stack.extend(node.values())
    return SqlReferences(tables=frozenset(tables), ctes=frozenset(ctes))


def published_relations(conn: duckdb.DuckDBPyConnection) -> frozenset:
    """Lower-case names a query may read on an analytics connection: the dataset's
    tables and views (database ``db``) and the TEMP views made for this connection,
    without the internal `__` tables other than __cells / __genes."""
    rows = conn.execute(
        "SELECT table_name FROM duckdb_tables() WHERE database_name IN ('db', 'temp') "
        "UNION SELECT view_name FROM duckdb_views() "
        "WHERE database_name IN ('db', 'temp') AND NOT internal").fetchall()
    names = {str(name).lower() for (name,) in rows}
    return frozenset(name for name in names
                     if not name.startswith('__') or name in _OPEN_RAW_TABLES)


def existing_relations(conn: duckdb.DuckDBPyConnection) -> frozenset:
    """Lower-case names of every table and view an unqualified name can reach on the
    connection (the search path: main and pg_catalog of every attached database, and
    temp), system and internal ones included. information_schema names (columns,
    tables, views, ...) need a qualifier, which _check_tree refuses."""
    rows = conn.execute(
        "SELECT table_name FROM duckdb_tables() WHERE schema_name IN ('main', 'pg_catalog') "
        "OR database_name = 'temp' "
        "UNION SELECT view_name FROM duckdb_views() WHERE schema_name IN ('main', 'pg_catalog') "
        "OR database_name = 'temp'").fetchall()
    return frozenset(str(name).lower() for (name,) in rows)


def check_relations(references: SqlReferences, allowed: frozenset,
                    existing: frozenset = frozenset()) -> None:
    """Raise ValueError when the query reads a relation the dataset does not publish.

    CTE names are collected over the whole query, not per scope, so a CTE may
    not take the name of a relation that exists but is not published (a CTE
    called duckdb_views would otherwise let `FROM duckdb_views` elsewhere in
    the query reach the system view).
    """
    shadowing = sorted(name for name in references.ctes if name in existing and name not in allowed)
    if shadowing:
        raise ValueError(
            f"CTE name {', '.join(repr(name) for name in shadowing)} is reserved; choose another name.")
    unknown = sorted(name for name in references.tables
                     if name not in references.ctes and name not in allowed)
    if unknown:
        shown = ', '.join(sorted(name for name in allowed if not name.startswith('layer__'))[:20])
        raise ValueError(
            f"Unknown table {', '.join(repr(name) for name in unknown)}. "
            f"This dataset publishes: {shown}, and layer__<layer> views "
            f"(columns and types: SELECT * FROM schema_columns, or "
            f"GET /databases/<id>/analytics/schema).")

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


def validate_sql(sql: str) -> SqlReferences:
    """Raise ValueError if sql is not a single SELECT, reads files, URLs,
    system catalogs or internal tables, or calls a function outside the
    allowlist. Pure parse: no execution, no network.

    Returns the base tables and CTE names the query reads; the executor
    checks the tables against what the dataset publishes (check_relations).
    """
    if not isinstance(sql, str):
        raise ValueError("SQL must be a string")
    if len(sql) > MAX_SQL_CHARS:
        raise ValueError(f"Query is {len(sql):,} characters long; the limit is {MAX_SQL_CHARS:,}.")
    _check_operator_runs(sql)
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
    if len(stmts) > 1:
        raise ValueError(
            f"Send one statement at a time ({len(stmts)} found). A PIVOT statement without an IN "
            f"list counts as two; name the values instead: FROM t PIVOT (count(*) FOR col IN ('a', 'b')).")
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

    # Step 3 — allowlist over DuckDB's parse tree (the real gate; the token
    # scan above is kept as a second, independent check).
    return _check_tree(sql)


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
        lowered = table_name.lower()
        if lowered.startswith('__') and lowered not in _OPEN_RAW_TABLES:
            continue  # internal tables (hidden author columns in __obscat / __obsnum); the views are the public surface
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
    # Only this dataset's own sidecars: <folder>/<stem>.*. A view baked by
    # another host or copied from another dataset may name a different file,
    # which a `db.`-qualified view name would otherwise read (the portable
    # TEMP views shadow only unqualified names).
    folder, _, base = db_url.rpartition('/')
    stem = base[:-len('.duckdb')] if base.endswith('.duckdb') else base
    try:
        for (view_sql,) in conn.execute(
            "SELECT sql FROM duckdb_views() WHERE database_name = 'db'"
        ).fetchall():
            for match in _VIEW_PARQUET_RE.finditer(view_sql or ''):
                path = match.group(1).replace("''", "'")
                path_folder, _, path_base = path.rpartition('/')
                if db_url.startswith('s3://'):
                    same_folder = path_folder == folder
                else:
                    same_folder = (Path(path_folder).resolve() == Path(folder).resolve()) if path_folder else False
                if same_folder and path_base.startswith(f'{stem}.'):
                    paths.append(path)
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
    # The Python client resolves unknown table names against Python variables
    # in the calling frames (replacement scans); a query must see only the
    # dataset's relations.
    conn.execute("SET python_enable_replacements = false")
    conn.execute("SET lock_configuration = true")


def _open_analytics_connection(
    db_url: str,
    s3_config: Optional[Dict[str, Any]] = None,
    attach_sessions: Optional[List[str]] = None,
    extra_parquet_views: Optional[Dict[str, str]] = None,
    extra_view_exclusions: Optional[Dict[str, Tuple[str, List[str]]]] = None,
    temp_directory: Optional[str] = None,
    max_temp_directory_size: str = '2GB',
) -> duckdb.DuckDBPyConnection:
    """Open a private, locked-down DuckDB instance with the dataset attached
    read-only as ``db`` (the default schema), plus TEMP VIEWs for attached
    session parquets and extra fleet-wide parquet views.

    ``extra_view_exclusions`` maps an extra view name to ``(column, values)``:
    that name becomes a TEMP TABLE without the rows whose column holds one of
    the values (the datasets hidden from the caller in a fleet-wide parquet,
    issue #4), and its file stays unreadable to the query.
    """
    mem_limit = os.environ.get('ANTHIVE_DUCKDB_MEMORY_LIMIT', '2GB')
    threads = int(os.environ.get('ANTHIVE_DUCKDB_THREADS', '2'))
    # Threads and memory at connect: set afterwards, DuckDB first starts one
    # thread per core.
    conn = duckdb.connect(config={'threads': threads, 'memory_limit': mem_limit})
    try:
        if temp_directory:
            # Spill under the configured tmp dir with a size cap (the default
            # is .tmp in the process cwd, up to 90% of the disk).
            conn.execute(f"SET temp_directory = {_sql_str(temp_directory)}")
            conn.execute(f"SET max_temp_directory_size = {_sql_str(max_temp_directory_size)}")
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

        # An extra view with exclusions becomes a TEMP TABLE of the kept rows,
        # filled before the lockdown, and its file is not readable afterwards:
        # hidden rows never enter this instance, so no plan, statistic or cast
        # error can reveal them (review finding F7).
        filtered = {name: spec for name, spec in (extra_view_exclusions or {}).items()
                    if name in {view_name for view_name, _ in extra_views}}
        for view_name, pq_path in extra_views:
            if view_name not in filtered:
                continue
            column, values = filtered[view_name]
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', str(column)):
                raise ValueError(f"Invalid exclusion column: {column!r}")
            where = ''
            if values:
                where = (f' WHERE "{column}" IS NULL OR "{column}" NOT IN '
                         f"({', '.join(_sql_str(value) for value in sorted(values))})")
            conn.execute(
                f"CREATE OR REPLACE TEMP TABLE {view_name} AS "
                f"SELECT * FROM read_parquet({_sql_str(pq_path)}){where}")
        lazy_views = session_views + [(name, path) for name, path in extra_views if name not in filtered]

        allowed = _dataset_sidecar_paths(conn, db_url)
        allowed += [path for _, path in lazy_views]
        allowed += [path for _, _, paths in layer_views for path in paths]
        _lockdown_connection(conn, allowed, allowed_directories)

        for view_name, create_sql, _paths in layer_views:
            try:
                conn.execute(create_sql)
            except duckdb.Error as exc:
                log.warning('analytics: portable view %s not installed: %s', view_name, exc)

        # Module scores kept in <stem>.scores.parquet are not in obsnum: expose
        # them as the lazy TEMP view score_sidecar (reading the sidecar is
        # allowed: it matches <stem>.*.parquet above).
        install_score_views(conn, db_url)

        for view_name, pq_path in lazy_views:
            conn.execute(
                f"CREATE OR REPLACE TEMP VIEW {view_name} AS "
                f"SELECT * FROM read_parquet({_sql_str(pq_path)})"
            )
        _install_schema_columns(conn)
        return conn
    except Exception:
        conn.close()
        raise


SCHEMA_COLUMNS_VIEW = 'schema_columns'


def _install_schema_columns(conn: duckdb.DuckDBPyConnection) -> None:
    """TEMP VIEW schema_columns (table_name, column_name, data_type, column_index):
    the columns of the relations this connection publishes, so a query can look
    them up in SQL (information_schema and DESCRIBE are refused). The list of
    relations is fixed here; the view reads only their catalog entries."""
    names = sorted(name for name in published_relations(conn) if name != SCHEMA_COLUMNS_VIEW)
    if not names:
        names = ['']
    in_list = ', '.join(_sql_str(name) for name in names)
    conn.execute(
        f"CREATE OR REPLACE TEMP VIEW {SCHEMA_COLUMNS_VIEW} AS "
        f"SELECT table_name, column_name, data_type, column_index FROM ("
        f"  SELECT table_name, column_name, data_type, column_index, "
        f"         row_number() OVER (PARTITION BY lower(table_name), column_index "
        f"                            ORDER BY database_name = 'temp' DESC) AS __pick "
        f"  FROM duckdb_columns() "
        f"  WHERE database_name IN ('db', 'temp') AND lower(table_name) IN ({in_list})"
        f") WHERE __pick = 1 ORDER BY table_name, column_index")


def _without_trailing_semicolons(sql: str) -> str:
    """sql up to its trailing `;` tokens (and any comment after them), so the
    statement can be wrapped as a subquery. Semicolons inside string literals
    or comments are not tokens and stay."""
    tokens = duckdb.tokenize(sql)
    cut = len(sql)
    for offset, token_type in reversed(tokens):
        if token_type == duckdb.token_type.operator and sql[offset:offset + 1] == ';':
            cut = offset
            continue
        break
    return sql[:cut]


class QueryBusy(RuntimeError):
    """All query workers are in use; the caller should retry later (HTTP 429)."""


def _bytes_of(size_text: str) -> int:
    """'2GB' / '512MB' / '1.5GiB' -> bytes (0 when unreadable)."""
    match = re.fullmatch(r'\s*([0-9.]+)\s*([KMGT]?)i?B?\s*', str(size_text), re.IGNORECASE)
    if not match:
        return 0
    return int(float(match.group(1)) * 1024 ** ' KMGT'.index(match.group(2).upper() or ' '))


MAX_CONCURRENT_QUERIES: int = int(os.environ.get('ANTHIVE_QUERY_CONCURRENCY', 4))
# Running queries per caller (a signed-in user, or all anonymous callers together).
MAX_QUERIES_PER_CALLER: int = int(os.environ.get('ANTHIVE_QUERY_PER_CALLER', 2))
SLOT_WAIT_SECONDS: float = 3.0
# Requests that may wait for a slot at once (each holds a server thread while it waits).
MAX_SLOT_WAITERS: int = int(os.environ.get('ANTHIVE_QUERY_MAX_WAITERS', 8))
# The tmp dir is swept at most this often (each sweep lists the whole folder).
SWEEP_INTERVAL_SECONDS: float = 60.0
SWEEP_MAX_ENTRIES: int = 20_000
# A streamed /query/sql answer that was never read releases its caller after this.
STREAM_RELEASE_SECONDS: float = 600.0
SPARE_WORKERS: int = int(os.environ.get('ANTHIVE_QUERY_SPARE_WORKERS', 1))
# Address space a worker may add after start-up (its imports take about 3 GB of
# mostly unused virtual memory); allocations past it fail cleanly. DuckDB keeps
# virtual ranges beyond its memory_limit while spilling, so the budget is three
# times that limit, at least 6 GB (3 GB broke aggregations DuckDB finishes).
WORKER_MEMORY_BUDGET: int = (int(os.environ['ANTHIVE_QUERY_MEMORY_BUDGET_MB']) * 1024 * 1024
                             if os.environ.get('ANTHIVE_QUERY_MEMORY_BUDGET_MB')
                             else max(6 * 1024 ** 3,
                                      3 * _bytes_of(os.environ.get('ANTHIVE_DUCKDB_MEMORY_LIMIT', '2GB'))))
MAX_RESULT_BYTES: int = int(os.environ.get('ANTHIVE_QUERY_MAX_RESULT_MB', 256)) * 1_000_000
# Largest JSON answer of /query/sql, and the budget of the session files of /analytics/query.
MAX_BODY_BYTES: int = int(os.environ.get('ANTHIVE_QUERY_MAX_BODY_MB', 128)) * 1_000_000
SESSION_BUDGET_BYTES: int = int(os.environ.get('ANTHIVE_ANALYTICS_TMP_BUDGET_MB', 5000)) * 1_000_000
MAX_STATUS_LINE: int = 65536
WORKER_START_TIMEOUT: int = 60
_WORKER_SCRIPT = Path(__file__).resolve().parent / 'analytics_worker.py'
_WORKER_ENV_KEYS = ('PATH', 'LANG', 'LC_ALL', 'HOME', 'TMPDIR', 'SSL_CERT_FILE', 'SSL_CERT_DIR')

_query_slots = threading.BoundedSemaphore(MAX_CONCURRENT_QUERIES)
_caller_lock = threading.Lock()
_caller_running: Dict[str, int] = {}
_spare_lock = threading.Lock()
_spare_workers: List[subprocess.Popen] = []
_waiters = 0
_sweep_lock = threading.Lock()
_last_sweep = 0.0
_sessions_lock = threading.Lock()
_sessions_running: set = set()
_spawn_requests: "queue.Queue[Tuple[concurrent.futures.Future, Any]]" = queue.Queue()
_spawner_lock = threading.Lock()
_spawner_thread: Optional[threading.Thread] = None


def _worker_env() -> Dict[str, str]:
    """A small environment for workers: no inherited credentials beyond ANTHIVE_ settings."""
    env = {key: os.environ[key] for key in _WORKER_ENV_KEYS if key in os.environ}
    env.update({key: value for key, value in os.environ.items() if key.startswith('ANTHIVE_')})
    env.update({'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1',
                'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1'})
    return env


def _popen_worker() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, '-I', str(_WORKER_SCRIPT)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=_worker_env(), text=True, bufsize=1, start_new_session=True)


def _spawner_loop() -> None:
    while True:
        future, _ = _spawn_requests.get()
        if not future.set_running_or_notify_cancel():
            continue
        try:
            future.set_result(_popen_worker())
        except BaseException as exc:  # noqa: BLE001 - handed to the waiting caller
            future.set_exception(exc)


def _start_worker() -> subprocess.Popen:
    """Start a worker from the one long-lived spawner thread.

    The worker's parent-death signal (PR_SET_PDEATHSIG) fires when the THREAD
    that forked it ends, not the process: a worker started from a request or
    refill thread was killed as soon as that thread finished (spares never
    survived). The spawner thread lives as long as the server."""
    global _spawner_thread
    with _spawner_lock:
        if _spawner_thread is None or not _spawner_thread.is_alive():
            _spawner_thread = threading.Thread(target=_spawner_loop, name='analytics-spawner',
                                               daemon=True)
            _spawner_thread.start()
    future: concurrent.futures.Future = concurrent.futures.Future()
    _spawn_requests.put((future, None))
    return future.result(timeout=WORKER_START_TIMEOUT)


def _read_line(proc: subprocess.Popen, timeout: float) -> Optional[str]:
    """One line from the worker's stdout, or None at EOF or when timeout passes."""
    import selectors  # noqa: PLC0415
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        if not selector.select(timeout):
            return None
    line = proc.stdout.readline(MAX_STATUS_LINE)
    return line or None


def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        pass
    for stream in (proc.stdin, proc.stdout):
        try:
            stream.close()
        except Exception:
            pass


def _refill_spares() -> None:
    with _spare_lock:
        _spare_workers[:] = [proc for proc in _spare_workers if proc.poll() is None]
        missing = SPARE_WORKERS - len(_spare_workers)
        for _ in range(max(0, missing)):
            try:
                _spare_workers.append(_start_worker())
            except OSError as exc:
                log.warning('analytics: could not start a spare query worker: %s', exc)
                break


def prestart_workers() -> None:
    """Start the spare query workers (called once at ant-serve start-up)."""
    try:
        _refill_spares()
    except Exception as exc:  # a missing worker only costs the first query its start-up time
        log.warning('analytics: spare workers not started: %s', exc)


def _take_worker() -> subprocess.Popen:
    """A started worker (a spare one when available); a replacement spare starts in the background."""
    with _spare_lock:
        proc = None
        while _spare_workers:
            candidate = _spare_workers.pop(0)
            if candidate.poll() is None:
                proc = candidate
                break
    if proc is None:
        proc = _start_worker()
    threading.Thread(target=_refill_spares, daemon=True).start()
    return proc


def _check_operator_runs(sql: str) -> None:
    """Refuse long runs of operator characters before any DuckDB call: DuckDB's
    lexer rescans a run containing `/*` from each comment start (quadratic),
    holding the GIL (25,000 characters froze the server for seconds)."""
    if _OPERATOR_RUN_RE.search(sql):
        raise ValueError("Query has a run of more than 256 operator characters.")


_OPERATOR_RUN_RE = re.compile(r'[-+*/<>=~!@#%^&|`?]{257,}')


def _acquire_slot(caller: str) -> None:
    """One query slot for caller, or QueryBusy: the per-caller limit first, then a
    short wait for a global slot, with at most MAX_SLOT_WAITERS requests waiting."""
    global _waiters
    with _caller_lock:
        if _caller_running.get(caller, 0) >= MAX_QUERIES_PER_CALLER:
            raise QueryBusy(f"You already have {MAX_QUERIES_PER_CALLER} queries running; wait for one to finish.")
        _caller_running[caller] = _caller_running.get(caller, 0) + 1
    if _query_slots.acquire(blocking=False):
        return
    with _caller_lock:
        if _waiters >= MAX_SLOT_WAITERS:
            waiting_full = True
        else:
            waiting_full = False
            _waiters += 1
    if waiting_full:
        _release_caller(caller)
        raise QueryBusy(f"All {MAX_CONCURRENT_QUERIES} query slots are in use; try again in a few seconds.")
    try:
        got = _query_slots.acquire(timeout=SLOT_WAIT_SECONDS)
    finally:
        with _caller_lock:
            _waiters -= 1
    if not got:
        _release_caller(caller)
        raise QueryBusy(f"All {MAX_CONCURRENT_QUERIES} query slots are in use; try again in a few seconds.")


def _release_caller(caller: str) -> None:
    with _caller_lock:
        left = _caller_running.get(caller, 0) - 1
        if left > 0:
            _caller_running[caller] = left
        else:
            _caller_running.pop(caller, None)


def _maybe_sweep(tmp: Path) -> None:
    """_sweep_tmp, at most once per SWEEP_INTERVAL_SECONDS
    (each lists the whole tmp folder) and never two at once."""
    global _last_sweep
    now = time.monotonic()
    if now - _last_sweep < SWEEP_INTERVAL_SECONDS or not _sweep_lock.acquire(blocking=False):
        return
    try:
        if now - _last_sweep < SWEEP_INTERVAL_SECONDS:
            return
        _last_sweep = now
        _sweep_tmp(tmp)
    except OSError as exc:
        log.warning('analytics: tmp sweep failed: %s', exc)
    finally:
        _sweep_lock.release()


def _sweep_tmp(tmp: Path) -> None:
    """Remove worker leftovers (a killed server leaves them), session files and
    figures older than SESSION_MAX_AGE, and keep session parquets within budget.
    One pass over at most SWEEP_MAX_ENTRIES entries."""
    cutoff = time.time() - (QUERY_TIMEOUT + STREAM_RELEASE_SECONDS + 60)
    session_cutoff = time.time() - SESSION_MAX_AGE
    sessions = []
    with os.scandir(tmp) as entries:
        for count, entry in enumerate(entries):
            if count >= SWEEP_MAX_ENTRIES:
                log.warning('analytics: tmp folder %s has more than %d entries; swept the first ones',
                            tmp, SWEEP_MAX_ENTRIES)
                break
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            path = Path(entry.path)
            if entry.name.startswith(('_result_', '_spill_', '_viz_', '_send_')):
                if stat.st_mtime < cutoff:
                    if entry.is_dir(follow_symlinks=False):
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        path.unlink(missing_ok=True)
                continue
            if path.suffix in ('.parquet', '.png', '.svg', '.pdf', '.owner') and stat.st_mtime < session_cutoff:
                path.unlink(missing_ok=True)
                continue
            if path.suffix == '.parquet' and _SESSION_ID_RE.fullmatch(path.stem):
                sessions.append((stat.st_mtime, stat.st_size, path))
    total = sum(size for _, size, _ in sessions)
    for _, size, path in sorted(sessions):
        if total <= SESSION_BUDGET_BYTES:
            break
        path.unlink(missing_ok=True)
        path.with_suffix('.owner').unlink(missing_ok=True)
        total -= size


def _run_worker(job: Dict[str, Any], *, timeout: Optional[int], caller: str, label: str,
                reader, keep_caller: bool = False) -> Any:
    """Validate, take a slot, run job in a worker, return reader(status, result_path).

    reader runs while the slot is held and before the result file is removed.
    With keep_caller, a successful run leaves the caller's count taken: the
    reader's result then owns it and must call _release_caller (the streamed
    /query/sql answer counts against its caller until it is sent).
    """
    if timeout is None or timeout <= 0:
        effective_timeout = QUERY_TIMEOUT
    else:
        effective_timeout = min(int(timeout), QUERY_TIMEOUT)
    validate_sql(job['sql'])
    for sid in job.get('attach_sessions') or []:
        validate_session_id(sid)
    tmp = _get_tmp_dir()
    _maybe_sweep(tmp)
    token = uuid.uuid4().hex
    result_path = tmp / f"_result_{token}"
    spill_dir = tmp / f"_spill_{token}"
    threads = int(os.environ.get('ANTHIVE_DUCKDB_THREADS', '2'))
    job = dict(job, result_path=str(result_path), spill_dir=str(spill_dir),
               max_result_bytes=MAX_RESULT_BYTES, memory_budget_bytes=WORKER_MEMORY_BUDGET,
               # CPU time counts every thread; the wall-clock limit is the parent's kill.
               cpu_seconds=effective_timeout * max(1, threads) + 30)
    _acquire_slot(caller)
    proc: Optional[subprocess.Popen] = None
    caller_handed_over = False
    try:
        log.info('%s caller=%s timeout=%ds sql=%.100s', label, caller, effective_timeout,
                 job['sql'].replace('\n', ' '))
        proc = _take_worker()
        if not _read_line(proc, WORKER_START_TIMEOUT):
            raise RuntimeError('The query worker did not start.')
        started = time.monotonic()
        try:
            proc.stdin.write(json.dumps(job) + '\n')
            proc.stdin.flush()
        except OSError as exc:
            raise RuntimeError(f'The query worker went away before the query started ({exc}).') from exc
        line = _read_line(proc, effective_timeout)
        if line is None or not line.endswith('\n'):
            if proc.poll() is None and time.monotonic() - started >= effective_timeout - 0.5:
                log.warning('%s TIMEOUT after %ds; worker killed', label, effective_timeout)
                raise TimeoutError(f"Query exceeded the {effective_timeout}s time limit and was cancelled.")
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                pass
            code = proc.returncode
            if code is not None and code < 0 and -code == getattr(__import__('signal'), 'SIGXCPU', 24):
                raise TimeoutError(f"Query used more CPU time than the {effective_timeout}s limit allows.")
            reason = (f'signal {-code}' if code is not None and code < 0 else f'exit code {code}')
            log.error('%s worker died (%s)', label, reason)
            raise RuntimeError(
                f"The query stopped its worker process ({reason}): it needed more memory or "
                f"stack than a query may use, or hit a crash in DuckDB.")
        status = json.loads(line)
        if not status.get('ok'):
            message = str(status.get('message') or 'query failed')[:4000]
            log.error('%s FAILED: %s', label, message[:300])
            if status.get('kind') == 'refused':
                raise ValueError(message)
            raise RuntimeError(message)
        result = reader(status, result_path)
        caller_handed_over = keep_caller
        return result
    finally:
        try:
            if proc is not None:
                _kill(proc)
        finally:
            _query_slots.release()
            if not caller_handed_over:
                _release_caller(caller)
            result_path.unlink(missing_ok=True)
            shutil.rmtree(spill_dir, ignore_errors=True)


def _base_job(db_url: str, sql: str, row_limit: int, s3_config, attach_sessions,
              extra_parquet_views, extra_view_exclusions, max_cells: int) -> Dict[str, Any]:
    if not isinstance(sql, str):
        raise ValueError("SQL must be a string")
    return {
        'db_url': db_url, 'sql': sql, 'row_limit': max(1, int(row_limit)), 's3_config': s3_config,
        'attach_sessions': attach_sessions or [],
        'extra_parquet_views': extra_parquet_views or {},
        'extra_view_exclusions': {name: [column, list(values)] for name, (column, values)
                                  in (extra_view_exclusions or {}).items()},
        'max_cells': int(max_cells or 0),
    }


def execute_query(
    db_url: str,
    sql: str,
    *,
    row_limit: int,
    s3_config: Optional[Dict[str, Any]] = None,
    timeout: Optional[int] = None,
    attach_sessions: Optional[List[str]] = None,
    extra_parquet_views: Optional[Dict[str, str]] = None,
    extra_view_exclusions: Optional[Dict[str, Tuple[str, List[str]]]] = None,
    max_cells: int = 0,
    caller: str = 'internal',
    label: str = 'query',
):
    """Run caller-supplied SQL on one dataset and return (pyarrow.Table, truncated).

    Every user SQL query goes through _run_worker: validation here
    (validate_sql: one SELECT, allowlisted table and scalar functions, no
    files, system catalogs or internal tables), then a single-use worker
    process (lib/analytics_worker.py) on a private locked-down DuckDB instance
    that reads only the dataset's own files, every table checked against what
    the dataset publishes. The worker has an address-space and a CPU limit,
    spills to the tmp dir (at most 2 GB), does all encoding, and is killed at
    the time limit, so a runaway, uninterruptible or crashing query ends with
    the worker, not the server. At most MAX_CONCURRENT_QUERIES run at once and
    MAX_QUERIES_PER_CALLER per caller (QueryBusy). The row cap wraps the query;
    the result is also capped by MAX_RESULT_BYTES and, when given, max_cells.

    The routes use query_json (/query/sql) and run_query (/analytics/query),
    which keep the result encoding in the worker; this Arrow form is for
    tests and internal callers with small results.

    Raises:
        ValueError: the query is refused (validation, an unknown table, result too large).
        QueryBusy: no query slot for this caller.
        TimeoutError: the time limit was reached (the worker is killed).
        RuntimeError: DuckDB failed while running the query, or the worker died.
    """
    import pyarrow as pa  # noqa: PLC0415
    import pyarrow.ipc  # noqa: PLC0415,F401

    job = _base_job(db_url, sql, row_limit, s3_config, attach_sessions, extra_parquet_views,
                    extra_view_exclusions, max_cells)
    job['output'] = 'arrow'

    def _read(status, result_path):
        with pa.memory_map(str(result_path), 'r') as source:
            table = pa.ipc.open_file(source).read_all()
        return table, bool(status.get('truncated'))
    return _run_worker(job, timeout=timeout, caller=caller, label=label, reader=_read)


class JsonAnswer:
    """A /query/sql answer in a file, sent in chunks and removed afterwards.

    The caller's query count stays taken until the answer has been sent (or
    closed), so a caller cannot pile up unread answers; when nobody reads it,
    a timer releases it after STREAM_RELEASE_SECONDS.
    """

    CHUNK = 65536

    def __init__(self, path: Path, caller: str):
        self.path = path
        self.caller = caller
        self.size = path.stat().st_size
        self._lock = threading.Lock()
        self._closed = False
        self._timer = threading.Timer(STREAM_RELEASE_SECONDS, self.close)
        self._timer.daemon = True
        self._timer.start()

    def close(self) -> None:
        """Remove the file and release the caller (idempotent)."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._timer.cancel()
        try:
            self.path.unlink(missing_ok=True)
        finally:
            _release_caller(self.caller)

    def chunks(self):
        """The answer in CHUNK-sized pieces; closes when done or abandoned."""
        try:
            with open(self.path, 'rb') as handle:
                while True:
                    chunk = handle.read(self.CHUNK)
                    if not chunk:
                        break
                    yield chunk
        finally:
            self.close()

    def read_bytes(self) -> bytes:
        """The whole answer (tests and small internal callers)."""
        try:
            return self.path.read_bytes()
        finally:
            self.close()


def query_json_answer(db_url: str, sql: str, *, row_limit: int, max_cells: int = 0,
                      s3_config: Optional[Dict[str, Any]] = None, timeout: Optional[int] = None,
                      caller: str = 'internal', label: str = 'query_sql') -> JsonAnswer:
    """The /query/sql answer, encoded in the worker (at most MAX_BODY_BYTES), as a JsonAnswer.

    Keys: query, columns, dtypes, data (records), n_rows, n_columns, truncated.
    The server streams it from the file; it never holds the whole answer.
    Errors as in execute_query.
    """
    job = _base_job(db_url, sql, row_limit, s3_config, None, None, None, max_cells)
    job.update(output='json', max_body_bytes=MAX_BODY_BYTES)

    def _read(status, result_path):
        send_path = result_path.with_name('_send_' + result_path.name[len('_result_'):])
        os.replace(result_path, send_path)
        try:
            return JsonAnswer(send_path, caller)
        except OSError:
            send_path.unlink(missing_ok=True)
            raise
    return _run_worker(job, timeout=timeout, caller=caller, label=label, reader=_read,
                       keep_caller=True)


def query_json(db_url: str, sql: str, **kwargs) -> bytes:
    """query_json_answer, read whole (tests and internal callers with small results)."""
    return query_json_answer(db_url, sql, **kwargs).read_bytes()


def run_query_json(
    db_url: str,
    sql: str,
    session_id: Optional[str] = None,
    s3_config: Optional[Dict[str, Any]] = None,
    timeout: Optional[int] = None,
    attach_sessions: Optional[List[str]] = None,
    extra_parquet_views: Optional[Dict[str, str]] = None,
    extra_view_exclusions: Optional[Dict[str, Tuple[str, List[str]]]] = None,
    caller: str = 'internal',
) -> bytes:
    """Run a query for /analytics/query: the rows go to tmp/{session_id}.parquet, the answer comes back as JSON bytes.

    The worker writes the parquet and encodes the answer (session_id, columns,
    dtypes, row_count, truncated, full_table, preview_complete, preview): all
    rows when there are at most FULL_TABLE_ROWS, else the first PREVIEW_ROWS,
    cut to 2 MB. The full result is never returned; the session_id is the
    handle for /analytics/viz and attach_sessions. Session files are kept
    SESSION_MAX_AGE and within SESSION_BUDGET_BYTES (oldest removed first).
    Execution and errors as in execute_query.
    """
    if session_id is not None:
        validate_session_id(session_id)
    tmp = _get_tmp_dir()
    session_id = session_id or uuid.uuid4().hex
    parquet_path = tmp / f"{session_id}.parquet"
    part_path = tmp / f"_result_{uuid.uuid4().hex}.parquet.part"
    job = _base_job(db_url, sql, MAX_ROWS, s3_config, attach_sessions, extra_parquet_views,
                    extra_view_exclusions, 0)
    job.update(output='parquet', parquet_path=str(part_path), session_id=session_id,
               preview_rows=FULL_TABLE_ROWS, full_table_rows=FULL_TABLE_ROWS, preview_short=PREVIEW_ROWS,
               max_preview_bytes=2_000_000)

    def _read(status, result_path):
        body = result_path.read_bytes()
        owner_part = tmp / f"_result_{uuid.uuid4().hex}.owner.part"
        owner_part.write_text(owner)
        os.replace(owner_part, owner_path)
        os.replace(part_path, parquet_path)  # published only once the answer exists
        return body
    owner_path = tmp / f"{session_id}.owner"
    owner = _owner_digest(caller)
    with _sessions_lock:
        if session_id in _sessions_running:
            raise QueryBusy("A query with this session_id is still running; wait for it to finish.")
        if parquet_path.exists():
            try:
                existing_owner = owner_path.read_text().strip()
            except OSError:
                existing_owner = ''
            if existing_owner != owner:
                raise ValueError("This session_id belongs to another query or caller; "
                                 "leave session_id out to get a new one.")
        _sessions_running.add(session_id)
    try:
        return _run_worker(job, timeout=timeout, caller=caller,
                           label=f'analytics_query session={session_id}', reader=_read)
    finally:
        with _sessions_lock:
            _sessions_running.discard(session_id)
        part_path.unlink(missing_ok=True)


def _owner_digest(caller: str) -> str:
    """What a session's .owner file holds: a digest of the caller key (no user names on disk)."""
    import hashlib  # noqa: PLC0415
    return hashlib.sha256(f'anthive-session-owner:{caller}'.encode()).hexdigest()


def run_query(*args, **kwargs) -> Dict[str, Any]:
    """run_query_json, parsed (the answer is small: no rows beyond the preview)."""
    return json.loads(run_query_json(*args, **kwargs))


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
