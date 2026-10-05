"""ant-serve middleware: ConnectionPool, restore invalidation, metadata-cache
resilience, plot-cache keying and the streamed /cells serialisers.

ant-serve is a single-file uv script with heavy module-level imports, so the
pieces under test are lifted out of its AST and executed in a small namespace
instead of importing the whole server.
"""
from __future__ import annotations

import ast
import json
import logging
import os
import subprocess
import sys
import threading
import time
import types
from collections import OrderedDict
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

import duckdb
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVE_PATH = REPO_ROOT / 'bin' / 'api' / 'ant-serve'

_WANTED = {
    '_parse_size_mb', '_auto_total_memory_budget_mb', '_sql_str', '_dir_prefix',
    'EvictedDatabaseError', '_DbInstance', 'ConnectionPool',
    '_cells_json_chunks', '_cells_csv_chunks', '_CELLS_STREAM_CHUNK_ROWS',
    '_plot_cache_key', '_plot_db_fingerprint', 'restore_database', 'DatabaseRegistry',
    '_METADATA_CACHE_VERSION', '_METADATA_CACHE_TTL_S3', '_json_serial',
}


def _load_serve_parts(**extra_globals) -> types.SimpleNamespace:
    """Compile the selected top-level definitions of ant-serve (decorators stripped)."""
    tree = ast.parse(SERVE_PATH.read_text())
    body = []
    for node in tree.body:
        name = getattr(node, 'name', None)
        if isinstance(node, ast.Assign):
            name = next((t.id for t in node.targets if isinstance(t, ast.Name)), None)
        if name in _WANTED:
            if hasattr(node, 'decorator_list'):
                node.decorator_list = []
            body.append(node)
    module = ast.Module(body=body, type_ignores=[])
    namespace: Dict[str, Any] = {
        'duckdb': duckdb, 'pd': pd, 'json': json, 'time': time, 'Lock': Lock,
        'OrderedDict': OrderedDict, 'Path': Path, 'Dict': Dict, 'List': List,
        'Optional': Optional, 'Any': Any, 'log': logging.getLogger('test-ant-serve'),
        '_plot_os': os, '_plot_hashlib': __import__('hashlib'),
    }
    namespace.update(extra_globals)
    exec(compile(module, str(SERVE_PATH), 'exec'), namespace)
    return types.SimpleNamespace(**namespace)


serve = _load_serve_parts()


# ── fixtures ────────────────────────────────────────────────────────────────

def _make_dataset(folder: Path, name: str = 'ds', n_cells: int = 50) -> Path:
    """A minimal anthive-shaped duckdb with an obsnum view and a parquet layer sidecar."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'{name}.duckdb'
    sidecar = folder / f'{name}.layer.X.by_gene.parquet'
    conn = duckdb.connect(str(path))
    conn.execute(f"CREATE TABLE __cells AS SELECT range AS cell_id, 'c' || range AS cell_name FROM range({n_cells})")
    conn.execute("CREATE TABLE __obsnum AS SELECT cell_id, cell_id * 1.0 AS score FROM __cells")
    conn.execute("CREATE VIEW obsnum AS SELECT c.cell_name, o.score FROM __obsnum o JOIN __cells c USING (cell_id)")
    conn.execute(f"COPY (SELECT range AS gene_id, range % 7 AS cell_id, 1.0 AS value FROM range(20)) TO '{sidecar}'")
    conn.execute(f"CREATE VIEW layer__X AS SELECT * FROM read_parquet('{sidecar.resolve()}')")
    conn.close()
    return path


@pytest.fixture
def store(tmp_path):
    data = tmp_path / 'data'
    return data, _make_dataset(data / 'G1', 'ds')


def _pool(data: Path, **kw):
    return serve.ConnectionPool(threads=2, memory_limit='512MB', temp_directory=None,
                                allowed_directories=[str(data)], **kw)


def _writer_can_lock(path: Path) -> bool:
    """A separate process must be able to take the r/w lock (DuckDB file locks are per process)."""
    code = f"import duckdb; duckdb.connect({str(path)!r}).close()"
    return subprocess.run([sys.executable, '-c', code], capture_output=True).returncode == 0


# ── pool: cursors, hardening ────────────────────────────────────────────────

def test_cursor_sees_dataset_and_parquet_sidecar(store):
    data, path = store
    pool = _pool(data)
    cur = pool.get_connection(str(path))
    try:
        assert cur.execute("SELECT count(*) FROM obsnum").fetchone()[0] == 50
        assert cur.execute("SELECT count(*) FROM layer__X").fetchone()[0] == 20
        assert cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'obsnum'").fetchone()[0] == 1
    finally:
        pool.return_connection(str(path), cur)
    assert pool.in_use[str(path)] == 0


def test_hardening_blocks_external_files_and_setting_changes(store, tmp_path):
    data, path = store
    secret = tmp_path / 'secret.txt'
    secret.write_text('TOPSECRET')
    pool = _pool(data)
    cur = pool.get_connection(str(path))
    try:
        with pytest.raises(duckdb.PermissionException):
            cur.execute(f"SELECT * FROM obsnum, read_text('{secret}')").fetchall()
        for stmt in ("SET enable_external_access = true", "SET threads = 1",
                     "SET allowed_directories = ['/']", "SET lock_configuration = false"):
            with pytest.raises(duckdb.Error):
                cur.execute(stmt)
        assert cur.execute("SELECT current_setting('threads')").fetchone()[0] == 2
    finally:
        pool.return_connection(str(path), cur)


def test_symlinked_dataset_reads_sidecar_via_resolved_path(tmp_path):
    other_store = tmp_path / 'elsewhere'
    real_path = _make_dataset(other_store / 'G2', 'linked')
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'G2').symlink_to(other_store / 'G2')
    pool = _pool(data)
    db_key = str((data / 'G2' / 'linked.duckdb').resolve())  # the registry stores resolved paths
    assert db_key == str(real_path.resolve())
    cur = pool.get_connection(db_key)
    try:
        assert cur.execute("SELECT count(*) FROM layer__X").fetchone()[0] == 20
    finally:
        pool.return_connection(db_key, cur)


def test_concurrent_cursors_are_independent_and_counted(store):
    data, path = store
    pool = _pool(data)
    errors, results = [], []

    def work():
        try:
            cur = pool.get_connection(str(path))
            try:
                for _ in range(20):
                    results.append(cur.execute("SELECT sum(score) FROM obsnum").fetchone()[0])
            finally:
                pool.return_connection(str(path), cur)
        except Exception as exc:  # collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(12)]  # well past the old 3-per-db cap
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert set(results) == {sum(range(50)) * 1.0}
    assert pool.in_use[str(path)] == 0
    assert pool.get_stats()['in_use'] == 0


def test_first_touch_race_opens_one_instance(store):
    data, path = store
    pool = _pool(data)
    barrier = threading.Barrier(16)
    cursors = []

    def grab():
        barrier.wait()
        cursors.append(pool.get_connection(str(path)))

    threads = [threading.Thread(target=grab) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert pool.total_connections == 1
    assert pool.in_use[str(path)] == 16
    for cur in cursors:
        pool.return_connection(str(path), cur)
    assert pool.in_use[str(path)] == 0


# ── pool: release / restore / rescan ────────────────────────────────────────

def test_release_waits_for_in_flight_cursor_then_writer_gets_lock(store):
    data, path = store
    pool = _pool(data)
    cur = pool.get_connection(str(path))
    returned_at = {}

    def finish_later():
        time.sleep(0.5)
        returned_at['t'] = time.monotonic()
        pool.return_connection(str(path), cur)

    threading.Thread(target=finish_later).start()
    result = pool.release(str(path), duration_seconds=60, drain_timeout_seconds=10)
    assert result['ok'] and result['still_in_use'] == 0
    assert time.monotonic() >= returned_at['t']
    with pytest.raises(serve.EvictedDatabaseError):
        pool.get_connection(str(path))
    assert _writer_can_lock(path)

    writer = duckdb.connect(str(path))
    writer.execute("ALTER TABLE __obsnum ADD COLUMN extra DOUBLE")
    writer.close()
    assert pool.restore(str(path)) == {'was_evicted': True}
    cur = pool.get_connection(str(path))
    try:
        cols = [r[0] for r in cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = '__obsnum'").fetchall()]
        assert 'extra' in cols
    finally:
        pool.return_connection(str(path), cur)


def test_release_timeout_closes_instance_on_late_return(store):
    data, path = store
    pool = _pool(data)
    cur = pool.get_connection(str(path))
    result = pool.release(str(path), duration_seconds=60, drain_timeout_seconds=0.2)
    assert result['ok'] is False and result['still_in_use'] == 1
    assert not _writer_can_lock(path)          # the query still holds the file
    pool.return_connection(str(path), cur)
    assert _writer_can_lock(path)              # last cursor back -> instance closed


def test_close_all_keeps_in_flight_query_alive(store):
    data, path = store
    pool = _pool(data)
    old = pool.get_connection(str(path))
    pool.close_all()
    assert old.execute("SELECT count(*) FROM obsnum").fetchone()[0] == 50
    assert pool.get_stats()['retiring_instances'] == 1
    new = pool.get_connection(str(path))
    assert pool.total_connections == 2
    pool.return_connection(str(path), old)
    assert pool.get_stats()['retiring_instances'] == 0
    assert new.execute("SELECT count(*) FROM obsnum").fetchone()[0] == 50
    pool.return_connection(str(path), new)
    assert pool.in_use[str(path)] == 0


def test_lru_cap_closes_idle_instances_only(tmp_path):
    data = tmp_path / 'data'
    paths = [str(_make_dataset(data / f'G{i}', f'ds{i}')) for i in range(3)]
    pool = _pool(data, max_open_databases=2)
    busy = pool.get_connection(paths[0])
    for key in paths[1:]:
        pool.return_connection(key, pool.get_connection(key))
    stats = pool.get_stats()
    assert stats['active_databases'] == 2
    assert stats['lru_closes'] == 1
    assert busy.execute("SELECT count(*) FROM obsnum").fetchone()[0] == 50  # busy one survived
    pool.return_connection(paths[0], busy)


# ── restore_database reloads metadata ──────────────────────────────────────

def test_restore_endpoint_reloads_metadata_and_list_cache():
    from fastapi import HTTPException

    class FakePool:
        def restore(self, db_url):
            return {'was_evicted': True}

    class FakeRegistry:
        def __init__(self):
            self._db_extra = {'G/ds': {'group_id': 'G'}}
            self.metadata_cache = {'G/ds': {'obs': {'numerical': ['old']}}}
            self.gene_list_cache = {'G/ds': ['APOE']}
            self._databases_list_cache = [{'id': 'G/ds'}]

        def get_database_path(self, db_id):
            if db_id != 'G/ds':
                raise KeyError(db_id)
            return '/x/ds.duckdb'

        def _load_database_metadata(self, db_id, extra):
            return {'obs': {'numerical': ['old', 'new_score']}}

    registry = FakeRegistry()
    parts = _load_serve_parts(registry=registry, connection_pool=FakePool(), HTTPException=HTTPException)
    result = parts.restore_database('G/ds')
    assert result == {'was_evicted': True, 'metadata_reloaded': True}
    assert registry.metadata_cache['G/ds']['obs']['numerical'] == ['old', 'new_score']
    assert registry._databases_list_cache is None
    assert 'G/ds' not in registry.gene_list_cache
    with pytest.raises(HTTPException) as err:
        parts.restore_database('nope')
    assert err.value.status_code == 404


# ── _cache_all_metadata: transient errors keep the previous entry ───────────

def test_metadata_scan_keeps_previous_entry_on_transient_error(tmp_path):
    present = tmp_path / 'present.duckdb'
    present.write_bytes(b'')
    md = tmp_path / 'present.md'
    md.write_text('---\n---\n')
    registry = serve.DatabaseRegistry.__new__(serve.DatabaseRegistry)
    registry.databases = {'locked': str(present), 'gone': str(tmp_path / 'gone.duckdb'), 'fine': str(present)}
    registry._db_md_bases = {'locked': md, 'gone': tmp_path / 'gone.md', 'fine': md}
    registry._db_extra = {k: {} for k in registry.databases}
    registry.metadata_cache = {'locked': {'title': 'previous'}, 'gone': {'title': 'old'}}
    registry._databases_list_cache = ['stale']
    saved = {}
    registry._load_persisted_cache = lambda: {}
    registry._save_persisted_cache = lambda entries: saved.update(entries)
    registry._get_entry_fingerprint = lambda db_id: {'duckdb_mtime': 1.0}
    registry._is_cache_entry_valid = lambda *a: False

    def load(db_id, extra):
        if db_id == 'fine':
            return {'title': 'fresh'}
        raise duckdb.IOException('Could not set lock on file: Conflicting lock is held')

    registry._load_database_metadata = load
    registry._cache_all_metadata(force_reload=True)
    assert registry.metadata_cache == {'locked': {'title': 'previous'}, 'fine': {'title': 'fresh'}}
    assert set(registry.databases) == {'locked', 'fine'}      # only the dataset whose files are gone drops
    assert registry._databases_list_cache is None
    assert set(saved) == {'fine'}                             # the stale entry is not persisted as fresh


def test_s3_duckdb_keys_uses_one_paginated_listing():
    calls = []

    class Paginator:
        def paginate(self, Bucket, Prefix):
            calls.append((Bucket, Prefix))
            yield {'Contents': [{'Key': 'store/G/a.duckdb'}, {'Key': 'store/G/a.h5ad'}]}
            yield {'Contents': [{'Key': 'store/H/b.duckdb'}]}

    class Client:
        def get_paginator(self, name):
            assert name == 'list_objects_v2'
            return Paginator()

    registry = serve.DatabaseRegistry.__new__(serve.DatabaseRegistry)
    registry._s3_config = {'bucket': 'bkt', 'prefix': '/store/'}
    assert registry._s3_duckdb_keys(Client()) == {'G/a.duckdb', 'H/b.duckdb'}
    assert calls == [('bkt', 'store/')]


# ── plot cache ──────────────────────────────────────────────────────────────

def test_plot_cache_key_ignores_cache_switch():
    base = serve._plot_cache_key('G/ds', 'violin', 'y=APOE&x=ant_disease', 'png', 'fp')
    assert serve._plot_cache_key('G/ds', 'violin', 'x=ant_disease&y=APOE&cache=miss', 'png', 'fp') == base
    assert serve._plot_cache_key('G/ds', 'violin', 'y=TREM2&x=ant_disease', 'png', 'fp') != base


def test_plot_fingerprint_tracks_scores_sidecar(tmp_path):
    db = tmp_path / 'ds.duckdb'
    db.write_bytes(b'')
    os.utime(db, (1_000_000, 1_000_000))
    before = serve._plot_db_fingerprint(str(db))
    scores = tmp_path / 'ds.scores.parquet'
    scores.write_bytes(b'')
    os.utime(scores, (2_000_000, 2_000_000))
    assert serve._plot_db_fingerprint(str(db)) != before


# ── streamed /cells serialisers ─────────────────────────────────────────────

@pytest.mark.parametrize('n_rows', [0, 1, serve._CELLS_STREAM_CHUNK_ROWS, serve._CELLS_STREAM_CHUNK_ROWS * 2 + 3])
def test_cells_json_chunks_match_one_shot_serialisation(n_rows):
    df = pd.DataFrame({'cell_name': [f'c{i}' for i in range(n_rows)],
                       'APOE': [i / 3 for i in range(n_rows)],
                       'grp': ['a' if i % 2 else None for i in range(n_rows)]})
    streamed = b''.join(serve._cells_json_chunks(df)).decode()
    # Exactly what the pre-streaming handler produced in one piece.
    expected = (f'{{"n_cells":{n_rows},"n_columns":3,'
                f'"columns":{json.dumps(["cell_name", "APOE", "grp"])},'
                f'"data":{df.to_json(orient="records", double_precision=6)}}}')
    assert streamed == expected
    assert len(json.loads(streamed)['data']) == n_rows


@pytest.mark.parametrize('n_rows', [0, 7, serve._CELLS_STREAM_CHUNK_ROWS + 1])
def test_cells_csv_chunks_match_to_csv(n_rows):
    df = pd.DataFrame({'cell_name': [f'c{i}' for i in range(n_rows)], 'APOE': [float(i) for i in range(n_rows)]})
    assert b''.join(serve._cells_csv_chunks(df)).decode() == df.to_csv(index=False)
