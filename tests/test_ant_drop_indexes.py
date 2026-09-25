"""Tests for bin/ingest/ant-drop-indexes.

Builds a synthetic duckdb in the legacy ant-convert layout (redundant ART
indexes plus one index that must survive), then checks the rewrite, the
live-writer refusals, stale-sentinel recovery and ant-serve coordination.
"""
from __future__ import annotations

import fcntl
import http.server
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'lib'))
sys.path.insert(0, str(REPO_ROOT / 'tests'))

from test_annotation_writer import build_synthetic_duckdb  # noqa: E402

TOOL = REPO_ROOT / 'bin' / 'ingest' / 'ant-drop-indexes'
_loader = importlib.machinery.SourceFileLoader('ant_drop_indexes', str(TOOL))
_spec = importlib.util.spec_from_loader('ant_drop_indexes', _loader)
drop_indexes = importlib.util.module_from_spec(_spec)
_loader.exec_module(drop_indexes)

LEGACY_TARGETS = {'idx_cells_name', 'idx_genes_name', 'idx_obscat_sample', 'idx_obscat_Braak_stage',
                  'idx_obsm_X_umap_cell'}
KEPT_INDEX = 'idx_gene_bins_gene'
FIXED_MTIME_NS = 1_780_000_000_000_000_000


def build_legacy_dataset(folder: Path) -> Path:
    """Synthetic dataset with sibling .md + index.md and every legacy index class."""
    folder.mkdir(parents=True, exist_ok=True)
    db_path = folder / 'demo.duckdb'
    build_synthetic_duckdb(db_path)
    con = duckdb.connect(str(db_path))
    con.execute('ALTER TABLE __obscat ADD COLUMN "Braak stage" VARCHAR')
    con.execute('UPDATE __obscat SET "Braak stage" = CAST(cell_id % 6 AS VARCHAR)')
    con.execute('CREATE TABLE __obsm__X_umap (cell_id INTEGER PRIMARY KEY, dim_0 DOUBLE, dim_1 DOUBLE)')
    con.execute('INSERT INTO __obsm__X_umap SELECT cell_id, cell_id * 0.5, cell_id * -1.0 FROM __cells')
    con.execute('CREATE TABLE __gene_bins (gene_id INTEGER, expr_bin INTEGER)')
    con.execute('INSERT INTO __gene_bins SELECT gene_id, gene_id % 2 FROM __genes')
    con.execute('CREATE TABLE __pending_cols (col_name VARCHAR, target_table VARCHAR, annotator VARCHAR)')
    con.execute("INSERT INTO __pending_cols VALUES ('Braak stage', '__obscat', 'test')")
    con.execute('CREATE INDEX idx_cells_name ON __cells(cell_name)')
    con.execute('CREATE INDEX idx_genes_name ON __genes(gene_name)')
    con.execute('CREATE INDEX idx_obscat_sample ON __obscat(sample)')
    con.execute('CREATE INDEX idx_obscat_Braak_stage ON __obscat("Braak stage")')
    con.execute('CREATE INDEX idx_obsm_X_umap_cell ON __obsm__X_umap(cell_id)')
    con.execute(f'CREATE INDEX {KEPT_INDEX} ON __gene_bins(gene_id)')
    # Mimics the persisted layer views that bake another host's absolute path:
    # the parquet exists at CREATE VIEW time, then disappears.
    other_host = folder.parent / 'other_host'
    other_host.mkdir(exist_ok=True)
    parquet = other_host / 'demo.layer.X.parquet'
    con.execute(f"COPY (SELECT 1 AS cell_id, 2.5 AS value) TO '{parquet}' (FORMAT parquet)")
    con.execute(f"CREATE VIEW layer__X AS SELECT l.value FROM read_parquet('{parquet}') l")
    con.execute('CREATE VIEW layer__X_by_gene AS SELECT * FROM layer__X')
    con.close()
    parquet.unlink()
    (folder / 'index.md').write_text('---\ngroup: Demo\ngroup_id: D99\n---\n')
    (folder / 'demo.md').write_text('---\nid: DMO\n---\n')
    os.utime(db_path, ns=(FIXED_MTIME_NS, FIXED_MTIME_NS))
    return db_path


def index_names(db_path: Path) -> set:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return {row[0] for row in con.execute('SELECT index_name FROM duckdb_indexes()').fetchall()}
    finally:
        con.close()


def snapshot(db_path: Path) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        database = con.execute('SELECT current_database()').fetchone()[0]
        return drop_indexes.catalog_fingerprint(con, database)
    finally:
        con.close()


def run_tool(*args: str) -> int:
    return drop_indexes.main(list(args))


def test_rewrite_drops_only_targets_and_preserves_everything_else(tmp_path):
    db_path = build_legacy_dataset(tmp_path / 'Demo')
    assert LEGACY_TARGETS | {KEPT_INDEX} <= index_names(db_path)
    before = snapshot(db_path)

    assert run_tool('--no-coordinate', str(db_path)) == 0

    remaining = index_names(db_path)
    assert not (remaining & LEGACY_TARGETS)
    assert KEPT_INDEX in remaining
    after = snapshot(db_path)
    assert after == before  # fingerprint excludes only the dropped indexes
    assert any(view[1] == 'layer__X' and '/other_host/demo.layer.X.parquet' in view[2] for view in after['views'])
    assert os.stat(db_path).st_mtime_ns == FIXED_MTIME_NS
    leftovers = sorted(p.name for p in db_path.parent.iterdir())
    assert leftovers == ['demo.duckdb', 'demo.md', 'index.md']
    con = duckdb.connect(str(db_path))
    try:
        assert con.execute('SELECT * FROM __pending_cols').fetchall() == [('Braak stage', '__obscat', 'test')]
        # UNIQUE / PRIMARY KEY constraints survive the rewrite.
        with pytest.raises(duckdb.ConstraintException):
            con.execute("INSERT INTO __cells VALUES (999, 'cell_0000')")
        with pytest.raises(duckdb.ConstraintException):
            con.execute('INSERT INTO __obsm__X_umap VALUES (0, 1.0, 1.0)')
    finally:
        con.close()


def test_dry_run_writes_nothing(tmp_path, capsys):
    db_path = build_legacy_dataset(tmp_path / 'Demo')
    raw = db_path.read_bytes()
    assert run_tool('--dry-run', '--no-coordinate', str(db_path)) == 0
    assert db_path.read_bytes() == raw
    assert 'drop 5 indexes' in capsys.readouterr().out


def test_second_run_is_a_noop(tmp_path, capsys):
    db_path = build_legacy_dataset(tmp_path / 'Demo')
    assert run_tool('--no-coordinate', str(db_path)) == 0
    raw = db_path.read_bytes()
    assert run_tool('--no-coordinate', str(db_path)) == 0
    assert db_path.read_bytes() == raw
    assert 'no redundant indexes' in capsys.readouterr().out


def test_refuses_when_writer_holds_flock(tmp_path):
    db_path = build_legacy_dataset(tmp_path / 'Demo')
    raw = db_path.read_bytes()
    lock_path = db_path.with_name(db_path.name + '.lock')
    with open(lock_path, 'w') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        assert run_tool('--no-coordinate', str(db_path)) == 1
    assert db_path.read_bytes() == raw
    assert not db_path.with_name(db_path.name + '.rebuilding').exists()


def test_refuses_when_another_process_holds_read_write_connection(tmp_path):
    db_path = build_legacy_dataset(tmp_path / 'Demo')
    raw = db_path.read_bytes()
    holder = subprocess.Popen(
        [sys.executable, '-c', textwrap.dedent(f"""
            import duckdb, sys, time
            con = duckdb.connect({str(db_path)!r})
            print('ready', flush=True)
            sys.stdin.read()
        """)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == 'ready'
        assert run_tool('--no-coordinate', str(db_path)) == 1
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)
    assert db_path.read_bytes() == raw


def test_refuses_foreign_live_sentinel_and_wal(tmp_path):
    db_path = build_legacy_dataset(tmp_path / 'Demo')
    raw = db_path.read_bytes()
    sentinel = db_path.with_name(db_path.name + '.rebuilding')
    sentinel.write_text(f'ant-convert pid={os.getpid()}\n')
    assert run_tool('--no-coordinate', str(db_path)) == 1
    assert sentinel.read_text().startswith('ant-convert')
    sentinel.unlink()

    wal = db_path.with_name(db_path.name + '.wal')
    wal.write_bytes(b'')
    assert run_tool('--no-coordinate', str(db_path)) == 1
    wal.unlink()
    assert db_path.read_bytes() == raw


def test_recovers_from_own_stale_sentinel(tmp_path):
    db_path = build_legacy_dataset(tmp_path / 'Demo')
    dead = subprocess.Popen([sys.executable, '-c', 'pass'])
    dead.wait()
    db_path.with_name(db_path.name + '.rebuilding').write_text(f'ant-drop-indexes pid={dead.pid}\n')
    db_path.with_name(db_path.name + '.dropidx.tmp').write_bytes(b'half-written')
    assert run_tool('--no-coordinate', str(db_path)) == 0
    assert not (index_names(db_path) & LEGACY_TARGETS)
    assert sorted(p.name for p in db_path.parent.iterdir()) == ['demo.duckdb', 'demo.md', 'index.md']


def test_aborts_swap_when_original_changes_mid_build(tmp_path, monkeypatch):
    db_path = build_legacy_dataset(tmp_path / 'Demo')
    real_build = drop_indexes.DropIndexJob._build

    def build_then_touch(self):
        result = real_build(self)
        os.utime(self.path, ns=(FIXED_MTIME_NS + 10**9, FIXED_MTIME_NS + 10**9))
        return result

    monkeypatch.setattr(drop_indexes.DropIndexJob, '_build', build_then_touch)
    raw = db_path.read_bytes()
    assert run_tool('--no-coordinate', str(db_path)) == 1
    assert db_path.read_bytes() == raw
    assert sorted(p.name for p in db_path.parent.iterdir()) == ['demo.duckdb', 'demo.md', 'index.md']


class _FakeAntServe(http.server.BaseHTTPRequestHandler):
    calls: list = []
    release_response: tuple = (200, {'ok': True})

    def do_POST(self):  # noqa: N802
        type(self).calls.append(self.path)
        status, body = (type(self).release_response if '/release/' in self.path else (200, {'ok': True}))
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_ant_serve():
    _FakeAntServe.calls = []
    _FakeAntServe.release_response = (200, {'ok': True})
    server = http.server.HTTPServer(('127.0.0.1', 0), _FakeAntServe)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield _FakeAntServe, f'http://127.0.0.1:{server.server_port}/api/'
    server.shutdown()


def test_coordinates_release_and_restore(tmp_path, fake_ant_serve):
    handler, url = fake_ant_serve
    db_path = build_legacy_dataset(tmp_path / 'Demo')
    assert run_tool('--api-url', url, str(db_path)) == 0
    assert handler.calls == ['/api/admin/release/D99/DMO', '/api/admin/restore/D99/DMO']
    assert not (index_names(db_path) & LEGACY_TARGETS)


@pytest.mark.parametrize('response', [(200, {'ok': False, 'error': 'drain timeout'}), (404, {'detail': 'nope'})])
def test_failed_release_leaves_original_and_still_restores(tmp_path, fake_ant_serve, response):
    handler, url = fake_ant_serve
    handler.release_response = response
    db_path = build_legacy_dataset(tmp_path / 'Demo')
    raw = db_path.read_bytes()
    assert run_tool('--api-url', url, str(db_path)) == 1
    assert db_path.read_bytes() == raw
    assert handler.calls[0] == '/api/admin/release/D99/DMO'
    if response[0] == 200:
        assert handler.calls[-1] == '/api/admin/restore/D99/DMO'
    assert sorted(p.name for p in db_path.parent.iterdir()) == ['demo.duckdb', 'demo.md', 'index.md']


def test_unreachable_server_is_treated_as_stopped(tmp_path):
    db_path = build_legacy_dataset(tmp_path / 'Demo')
    assert run_tool('--api-url', 'http://127.0.0.1:9/api/', str(db_path)) == 0
    assert not (index_names(db_path) & LEGACY_TARGETS)


def test_glob_skips_underscore_staging_folders(tmp_path):
    build_legacy_dataset(tmp_path / 'Demo')
    build_legacy_dataset(tmp_path / '_staging' / 'Demo')
    found = drop_indexes.expand_inputs([str(tmp_path / '**' / '*.duckdb')])
    assert [p.relative_to(tmp_path).as_posix() for p in found] == ['Demo/demo.duckdb']
