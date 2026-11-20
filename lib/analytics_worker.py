"""Single-use worker process for caller SQL (spawned by analytics.execute_query).

Why a process: a query running in a thread of ant-serve cannot be killed and
shares the server's memory. Some DuckDB functions ignore interrupt() (an
edit-distance on long strings runs for hours), allocate outside DuckDB's
memory_limit (repeat, lpad, printf, list functions), or crash the process
(json_structure on very deep JSON segfaults). In a worker those end with the
worker: the parent kills it at the time limit, and an address-space limit
turns runaway allocations into a clean out-of-memory error.

Protocol (one job per process):
  1. The worker imports everything, then writes ``{"ready": true}`` and a
     newline to stdout, and waits.
  2. The parent writes one JSON line with the job to stdin (never argv: it
     may carry S3 credentials).
  3. The worker sets RLIMIT_AS (its current address space plus
     ``memory_budget_bytes``) and RLIMIT_CPU, runs the query on a locked-down
     instance (analytics._open_analytics_connection), fetches Arrow batches
     until ``row_limit``, ``max_cells`` or ``max_result_bytes`` is reached,
     writes the result as an Arrow IPC file to ``result_path``, writes one
     JSON status line and exits.

Output (``job["output"]``): ``arrow`` writes the result as an Arrow IPC file
to ``result_path``; ``json`` writes the whole /query/sql answer as JSON text
to ``result_path`` (capped at ``max_body_bytes``); ``parquet`` writes the
rows to ``parquet_path`` and the /analytics/query answer (columns, dtypes,
counts, a byte-capped preview) to ``result_path``. All encoding happens
here, so the server only passes bytes on.

Status line: ``{"ok": true, "truncated": bool, "rows": n}`` or
``{"ok": false, "kind": "refused" | "error", "message": str}`` (at most
4,000 characters, server paths replaced).
"""

import json
import resource
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import duckdb  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.compute  # noqa: E402,F401
import pyarrow.ipc  # noqa: E402,F401

import analytics  # noqa: E402


def _address_space() -> int:
    """Current virtual memory size of this process in bytes (0 when unknown)."""
    try:
        with open('/proc/self/status') as handle:
            for line in handle:
                if line.startswith('VmSize:'):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def _limit_resources(job: dict) -> None:
    budget = int(job.get('memory_budget_bytes') or 0)
    base = _address_space()
    if budget > 0 and base > 0:
        resource.setrlimit(resource.RLIMIT_AS, (base + budget, base + budget))
    cpu = int(job.get('cpu_seconds') or 0)
    if cpu > 0:
        # A backstop for a parent that died; the parent kills at the time limit.
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))


def _parent_death_signal() -> None:
    """Die with the parent (Linux): a worker must not outlive a killed ant-serve.

    The signal follows the thread that started the worker; ant-serve starts
    every worker from one long-lived spawner thread (analytics._start_worker).
    """
    import os  # noqa: PLC0415
    parent = os.getppid()
    try:
        import ctypes  # noqa: PLC0415
        import signal  # noqa: PLC0415
        ctypes.CDLL('libc.so.6', use_errno=True).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except Exception:
        pass  # not Linux; RLIMIT_CPU and the parent's kill still apply
    if os.getppid() != parent:
        os._exit(0)  # the parent died before the signal was set


def _json_default(value):
    """json.dumps fallback for the Python values Arrow gives (decimals, dates, bytes, numpy)."""
    import base64  # noqa: PLC0415
    import datetime  # noqa: PLC0415
    import decimal  # noqa: PLC0415
    import math  # noqa: PLC0415
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            return None
        # Whole numbers (HUGEINT, UHUGEINT, DECIMAL(p, 0)) stay exact integers.
        return int(value) if value.as_tuple().exponent >= 0 else float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode('ascii')
    if hasattr(value, 'item') and not isinstance(value, (list, dict)):
        item = value.item()
        return None if isinstance(item, float) and not math.isfinite(item) else item
    if isinstance(value, (set, frozenset)):
        return list(value)
    return str(value)


def _json_safe(value):
    """NaN and infinities become None, recursively (JSON has neither)."""
    import math  # noqa: PLC0415
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _encode(value) -> str:
    return json.dumps(_json_safe(value), default=_json_default, allow_nan=False)


def _is_temporal(data_type) -> bool:
    return (pa.types.is_temporal(data_type)
            or (pa.types.is_list(data_type) or pa.types.is_large_list(data_type))
            and _is_temporal(data_type.value_type))


def _rows(batch):
    """batch.to_pylist(); dates and times Python cannot hold (year 0 or past 9999)
    become their text (Arrow's cast), instead of failing the query."""
    try:
        return batch.to_pylist()
    except (OverflowError, ValueError):
        columns = []
        for column in batch.columns:
            if pa.types.is_temporal(column.type):
                column = pa.compute.cast(column, pa.string())
            columns.append(column)
        return pa.RecordBatch.from_arrays(columns, names=batch.schema.names).to_pylist()


def _row_chunks(batch, size: int = 256):
    """Rows of an Arrow batch as Python dicts, a few hundred at a time (bounded Python memory)."""
    for offset in range(0, batch.num_rows, size):
        yield from _rows(batch.slice(offset, size))


def _no_parquet_type(data_type) -> bool:
    """Types Parquet cannot store (intervals, unions), also inside lists, structs and maps."""
    if pa.types.is_interval(data_type) or pa.types.is_union(data_type):
        return True
    return any(_no_parquet_type(data_type.field(i).type) for i in range(data_type.num_fields))


def _parquet_schema(schema):
    """schema with every column Parquet cannot store as a string column (its JSON text)."""
    return pa.schema([pa.field(field.name, pa.string()) if _no_parquet_type(field.type) else field
                      for field in schema])


def _for_parquet(batch, schema):
    """batch in schema (from _parquet_schema): unstorable columns become JSON text."""
    if batch.schema.equals(schema):
        return batch
    columns = []
    for column, field in zip(batch.columns, schema):
        if column.type != field.type:
            column = pa.array([None if value is None else _encode(value)
                               for value in column.to_pylist()], pa.string())
        columns.append(column)
    return pa.RecordBatch.from_arrays(columns, schema=schema)


class _TooLarge(ValueError):
    pass


def _dtypes(schema) -> dict:
    """pandas dtype names of the result columns, as /query/sql and /analytics/query always gave them
    (from an empty table of the result's schema; 'object' for a type pandas cannot hold, e.g. a union)."""
    try:
        return {name: str(dtype) for name, dtype in schema.empty_table().to_pandas().dtypes.items()}
    except (pa.ArrowNotImplementedError, NotImplementedError, TypeError, ValueError):
        dtypes = {}
        for field in schema:
            try:
                dtypes[field.name] = str(pa.schema([field]).empty_table().to_pandas().dtypes.iloc[0])
            except (pa.ArrowNotImplementedError, NotImplementedError, TypeError, ValueError):
                dtypes[field.name] = 'object'
        return dtypes


def _sanitize(message: str, job: dict) -> str:
    """Error text for the caller: no server paths, bounded length."""
    folder = str(Path(job['db_url']).parent) if not str(job['db_url']).startswith('s3://') \
        else job['db_url'].rsplit('/', 1)[0]
    replacements = [(folder, '<dataset folder>'), (str(Path(job['result_path']).parent), '<tmp>')]
    for view_name, view_path in (job.get('extra_parquet_views') or {}).items():
        replacements.append((str(Path(view_path).resolve()), f'<{view_name}>'))
        replacements.append((str(view_path), f'<{view_name}>'))
        replacements.append((str(Path(view_path).resolve().parent), '<data folder>'))
    # Longest first, so a folder never replaces part of a longer path.
    for path, label in sorted(replacements, key=lambda item: -len(item[0])):
        if path and path not in ('/', '.'):
            message = message.replace(path, label)
    # DuckDB suggests settings the caller cannot change.
    message = message.replace('(e.g., SET memory_limit=...)', '').replace('SET memory_limit', 'the memory limit')
    return message if len(message) <= 4000 else message[:4000] + ' ...'


def _run(job: dict) -> dict:
    references = analytics.validate_sql(job['sql'])
    spill_dir = Path(job['spill_dir'])
    output = job.get('output') or 'arrow'
    conn = analytics._open_analytics_connection(
        job['db_url'], job.get('s3_config'),
        attach_sessions=job.get('attach_sessions'),
        extra_parquet_views=job.get('extra_parquet_views'),
        extra_view_exclusions={name: tuple(spec) for name, spec in
                               (job.get('extra_view_exclusions') or {}).items()},
        temp_directory=str(spill_dir),
        max_temp_directory_size=job.get('max_temp_directory_size') or '2GB',
    )
    writer = None
    try:
        analytics.check_relations(references, analytics.published_relations(conn),
                                  analytics.existing_relations(conn))
        row_limit = max(1, int(job['row_limit']))
        max_cells = int(job.get('max_cells') or 0)
        max_bytes = int(job.get('max_result_bytes') or 0)
        max_body = int(job.get('max_body_bytes') or 0)
        sql = (f"SELECT * FROM (\n{analytics._without_trailing_semicolons(job['sql'])}\n) "
               f"__q LIMIT {row_limit + 1}")
        reader = conn.execute(sql).fetch_record_batch(8192)
        schema = reader.schema
        n_columns = max(1, len(schema))
        if max_cells:
            row_limit = max(1, min(row_limit, max_cells // n_columns))

        body = None
        if output == 'json':
            body = open(job['result_path'], 'w', encoding='utf-8')
            body.write('{"query": ' + _encode(job['sql']) + ', "columns": ' + _encode(schema.names)
                       + ', "dtypes": ' + _encode(_dtypes(schema)) + ', "data": [')
        elif output == 'parquet':
            import pyarrow.parquet as pq  # noqa: PLC0415
            parquet_schema = _parquet_schema(schema)
            writer = pq.ParquetWriter(job['parquet_path'], parquet_schema)

        # preview_rows holds Arrow slices; they become Python values only in
        # _analytics_meta, row by row against the byte budget.
        batches, preview_rows, rows, size, body_size, truncated = [], [], 0, 0, 0, False
        preview_count = 0
        preview_limit = int(job.get('preview_rows') or 0)
        for batch in reader:
            if rows + batch.num_rows > row_limit:
                batch = batch.slice(0, row_limit - rows)
                truncated = True
            size += batch.nbytes
            if max_bytes and size > max_bytes:
                raise _TooLarge(
                    f"The result is larger than {max_bytes // 1_000_000} MB; select fewer rows or "
                    f"columns, or aggregate in the query.")
            if output == 'json':
                for row in _row_chunks(batch):
                    text = ('' if rows == 0 and body_size == 0 else ', ') + _encode(row)
                    body_size += len(text)
                    if max_body and body_size > max_body:
                        raise _TooLarge(
                            f"The answer is larger than {max_body // 1_000_000} MB of JSON; select fewer "
                            f"rows or columns, or aggregate in the query.")
                    body.write(text)
            elif output == 'parquet':
                writer.write_batch(_for_parquet(batch, parquet_schema))
                if preview_count < preview_limit:
                    piece = batch.slice(0, preview_limit - preview_count)
                    preview_rows.append(piece)
                    preview_count += piece.num_rows
            else:
                batches.append(batch)
            rows += batch.num_rows
            if truncated or rows >= row_limit:
                # One more row exists when the query returned row_limit + 1.
                truncated = truncated or bool(next(iter(reader), None) is not None)
                break
        if output == 'json':
            body.write('], "n_rows": ' + str(rows) + ', "n_columns": ' + str(len(schema))
                       + ', "truncated": ' + ('true' if truncated else 'false') + '}')
            body.close()
            return {'ok': True, 'truncated': truncated, 'rows': rows}
        if output == 'parquet':
            writer.close()
            writer = None
            return {'ok': True, 'truncated': truncated, 'rows': rows,
                    'meta': _analytics_meta(job, schema, rows, truncated, preview_rows)}
        table = pa.Table.from_batches(batches, schema=schema)
    finally:
        if writer is not None:
            writer.close()
        conn.close()
        shutil.rmtree(spill_dir, ignore_errors=True)
    with pa.OSFile(job['result_path'], 'wb') as sink:
        with pa.ipc.new_file(sink, table.schema) as ipc_writer:
            ipc_writer.write_table(table)
    return {'ok': True, 'truncated': truncated, 'rows': table.num_rows}


def _analytics_meta(job: dict, schema, rows: int, truncated: bool, preview_rows: list) -> bool:
    """Write the /analytics/query answer (without the data) to result_path.

    preview_rows are Arrow slices; each row becomes Python values and JSON one
    at a time and the preview stops at the byte budget, so a few huge rows cost
    at most one row of Python memory beyond the budget.
    """
    full_table = rows <= int(job.get('full_table_rows') or 500)
    wanted_count = rows if full_table else int(job.get('preview_short') or 10)
    wanted_count = min(wanted_count, sum(piece.num_rows for piece in preview_rows))
    budget = int(job.get('max_preview_bytes') or 2_000_000)
    encoded, used, seen, over = [], 0, 0, False
    for piece in preview_rows:
        for offset in range(piece.num_rows):
            if seen >= wanted_count or over:
                break
            seen += 1
            row = piece.slice(offset, 1)
            if row.nbytes > budget:
                over = True
                break
            text = _encode(_rows(row)[0])
            if used + len(text) > budget:
                over = True
                break
            encoded.append(text)
            used += len(text)
    with open(job['result_path'], 'w', encoding='utf-8') as handle:
        handle.write('{"session_id": ' + _encode(job['session_id']) + ', "columns": ' + _encode(schema.names)
                     + ', "dtypes": ' + _encode(list(_dtypes(schema).values()))
                     + ', "row_count": ' + str(rows) + ', "truncated": ' + ('true' if truncated else 'false')
                     + ', "full_table": ' + ('true' if full_table else 'false')
                     + ', "preview_complete": ' + ('true' if len(encoded) == wanted_count else 'false')
                     + ', "preview": [' + ', '.join(encoded) + ']}')
    return True


def main() -> int:
    _parent_death_signal()
    sys.stdout.write(json.dumps({'ready': True}) + '\n')
    sys.stdout.flush()
    line = sys.stdin.readline()
    if not line:
        return 0  # the parent went away before giving a job
    job = json.loads(line)
    _limit_resources(job)
    try:
        status = _run(job)
    except ValueError as exc:
        status = {'ok': False, 'kind': 'refused', 'message': _sanitize(str(exc), job)}
    except (MemoryError, duckdb.OutOfMemoryException):
        status = {'ok': False, 'kind': 'error',
                  'message': 'Out of memory: the query needs more memory than one query may use here; '
                             'filter or aggregate more.'}
    except Exception as exc:  # reported to the caller as a query error
        status = {'ok': False, 'kind': 'error', 'message': _sanitize(str(exc), job)}
    if not status.get('ok'):
        for key in ('result_path', 'parquet_path'):
            if job.get(key):
                Path(job[key]).unlink(missing_ok=True)
    sys.stdout.write(json.dumps(status) + '\n')
    sys.stdout.flush()
    return 0


if __name__ == '__main__':
    sys.exit(main())
