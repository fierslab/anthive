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
  ANTHIVE_QUERY_TIMEOUT   SQL query timeout in seconds (default: 30)
  ANTHIVE_VIZ_TIMEOUT     Python script timeout in seconds (default: 60)
"""

import ast
import base64
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import pandas as pd


# ── Configuration ──────────────────────────────────────────────────────────────

def _get_tmp_dir() -> Path:
    tmp = Path(os.environ.get('ANTHIVE_ANALYTICS_TMP', './tmp/analytics'))
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp

MAX_ROWS:      int = int(os.environ.get('ANTHIVE_ANALYTICS_ROWS', 500_000))
QUERY_TIMEOUT: int = int(os.environ.get('ANTHIVE_QUERY_TIMEOUT', 30))
VIZ_TIMEOUT:   int = int(os.environ.get('ANTHIVE_VIZ_TIMEOUT', 60))
PREVIEW_ROWS:  int = 10


# ── SQL security ───────────────────────────────────────────────────────────────

_WRITE_PATTERN = re.compile(
    r'\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|REPLACE|MERGE'
    r'|ATTACH|DETACH|COPY|EXPORT|IMPORT|PRAGMA|CALL)\b',
    re.IGNORECASE,
)


def _strip_sql_comments(sql: str) -> str:
    sql = re.sub(r'--[^\n]*', '', sql)
    sql = re.sub(r'/\*.*?\*/', '', sql, flags=re.DOTALL)
    return sql


def validate_sql(sql: str) -> None:
    """Raise ValueError if sql contains write, DDL, or admin statements."""
    stripped = _strip_sql_comments(sql)
    match = _WRITE_PATTERN.search(stripped)
    if match:
        raise ValueError(
            f"Statement not allowed: '{match.group()}'. "
            "Only SELECT / WITH … SELECT queries are permitted."
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
            if isinstance(func, ast.Attribute) and func.attr in _BLOCKED_BUILTINS:
                raise ValueError(f"Call to '.{func.attr}()' is not permitted.")


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
        columns_raw = conn.execute(f"DESCRIBE \"{table_name}\"").fetchall()
        columns = [{'name': row[0], 'type': row[1]} for row in columns_raw]
        try:
            row_count_row = conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()
            row_count = row_count_row[0] if row_count_row else None
        except Exception:
            row_count = None
        try:
            sample_df = conn.execute(f'SELECT * FROM "{table_name}" LIMIT 3').df()
            sample = sample_df.to_dict(orient='records')
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

def run_query(
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a validated SELECT query, persist results as Parquet, and return metadata.

    The caller receives column names, dtypes, row count, and a small preview
    (PREVIEW_ROWS rows).  The full result set lives at tmp/{session_id}.parquet
    and is never returned in this response — the session_id is the only handle.

    Args:
        conn:       Read-only DuckDB connection.
        sql:        SELECT query to execute.
        session_id: Optional caller-supplied ID; one is generated if omitted.

    Returns:
        Dict with keys: session_id, columns, dtypes, row_count, preview.
    """
    validate_sql(sql)
    session_id = session_id or uuid.uuid4().hex

    limited_sql = f"SELECT * FROM ({sql}) __q LIMIT {MAX_ROWS}"

    result_holder: Dict[str, Any] = {}
    error_holder:  Dict[str, Any] = {}

    def _exec() -> None:
        try:
            result_holder['df'] = conn.execute(limited_sql).df()
        except Exception as exc:
            error_holder['exc'] = exc

    thread = threading.Thread(target=_exec, daemon=True)
    thread.start()
    thread.join(QUERY_TIMEOUT)

    if thread.is_alive():
        raise TimeoutError(f"Query exceeded the {QUERY_TIMEOUT}s time limit.")
    if 'exc' in error_holder:
        raise RuntimeError(str(error_holder['exc'])) from error_holder['exc']

    result_df: pd.DataFrame = result_holder['df']
    parquet_path = _get_tmp_dir() / f"{session_id}.parquet"
    result_df.to_parquet(parquet_path, index=False)

    preview = result_df.head(PREVIEW_ROWS).where(pd.notna(result_df.head(PREVIEW_ROWS)), None).to_dict(orient='records')
    return {
        'session_id':  session_id,
        'columns':     list(result_df.columns),
        'dtypes':      [str(dtype) for dtype in result_df.dtypes],
        'row_count':   len(result_df),
        'truncated':   len(result_df) == MAX_ROWS,
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

    tmp = _get_tmp_dir()
    parquet_path = tmp / f"{session_id}.parquet"
    if not parquet_path.exists():
        raise FileNotFoundError(
            f"No query result found for session '{session_id}'. "
            "Run analytics_query first."
        )

    output_path = tmp / f"{session_id}_viz.{output_format}"
    validate_python(code)
    script_source = _build_viz_script(code, str(parquet_path), str(output_path))

    with tempfile.NamedTemporaryFile(
        mode='w', suffix='.py', delete=False, dir=tmp, prefix='_viz_',
    ) as script_file:
        script_file.write(script_source)
        script_path = Path(script_file.name)

    try:
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            timeout=VIZ_TIMEOUT,
            cwd=str(tmp),
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError(
            f"Visualization script exceeded the {VIZ_TIMEOUT}s time limit."
        )
    finally:
        script_path.unlink(missing_ok=True)

    if proc.returncode != 0:
        stderr_snippet = (proc.stderr or '(no stderr)')[-1000:]
        raise RuntimeError(f"Visualization script failed:\n{stderr_snippet}")

    if not output_path.exists():
        raise RuntimeError(
            "Script exited successfully but produced no output file. "
            "Make sure your script saves the figure to OUTPUT_PATH, e.g.:\n"
            "    plt.savefig(OUTPUT_PATH, bbox_inches='tight', dpi=150)"
        )

    figure_bytes = output_path.read_bytes()
    output_path.unlink(missing_ok=True)
    return base64.b64encode(figure_bytes).decode(), output_format
