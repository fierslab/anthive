"""Test AnnotationWriter's release-on-conflict retry path with a mock HTTP server.

Spins up a tiny in-process HTTP server that pretends to be ant-serve's
/admin/release/{db_id} and /admin/restore/{db_id} endpoints, then verifies
the writer hits them when it gets a DuckDB lock conflict.

We trigger a real lock conflict by opening the synthetic duckdb r/w in a
sidecar process first (DuckDB blocks cross-process opens). The mock server
closes that sidecar process when it gets the release call.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import duckdb
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'lib'))
sys.path.insert(0, str(REPO_ROOT / 'tests'))

from test_annotation_writer import build_synthetic_duckdb, CELL_NAMES  # noqa: E402
from anthive_db import (  # noqa: E402
    AnnotationWriter, AnnotationWriterError, _post_ant_serve,
)


class MockAntServe:
    """In-process HTTP server with a release endpoint that closes a sidecar
    DuckDB process holding the lock."""

    def __init__(self):
        self.release_calls = []
        self.restore_calls = []
        self.sidecar_proc: subprocess.Popen | None = None
        self.release_ok = True
        self.return_value = {'ok': True, 'closed_idle': 1, 'still_in_use': 0}

    def serve(self) -> tuple[str, ThreadingHTTPServer, Thread]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path.startswith('/admin/release/'):
                    db_id = self.path[len('/admin/release/'):]
                    outer.release_calls.append(db_id)
                    # Kill the sidecar to free the duckdb lock
                    if outer.sidecar_proc and outer.sidecar_proc.poll() is None:
                        outer.sidecar_proc.terminate()
                        try:
                            outer.sidecar_proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            outer.sidecar_proc.kill()
                    body = json.dumps(outer.return_value).encode()
                    self.send_response(200 if outer.release_ok else 500)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith('/admin/restore/'):
                    db_id = self.path[len('/admin/restore/'):]
                    outer.restore_calls.append(db_id)
                    body = json.dumps({'was_evicted': True}).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args, **kwargs):
                pass  # suppress request logging

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        port = server.server_address[1]
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return f'http://127.0.0.1:{port}', server, thread


def _spawn_sidecar_holding_lock(duckdb_path: Path) -> subprocess.Popen:
    """Spawn a python subprocess that opens the duckdb r/w and idles,
    holding the cross-process lock."""
    script = (
        "import duckdb, sys, time;"
        f"c = duckdb.connect({str(duckdb_path)!r}, read_only=False);"
        "print('locked', flush=True);"
        "time.sleep(60)"
    )
    proc = subprocess.Popen(
        [sys.executable, '-c', script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Wait until the sidecar has actually taken the lock
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        line = proc.stdout.readline().decode().strip() if proc.stdout else ''
        if line == 'locked':
            return proc
        if proc.poll() is not None:
            err = proc.stderr.read().decode() if proc.stderr else ''
            raise RuntimeError(f"sidecar died early: {err}")
        time.sleep(0.05)
    proc.terminate()
    raise RuntimeError("sidecar never took the lock")


# ---------------------------------------------------------------------------

def test_release_retry_on_lock_conflict(tmp_path):
    """When DuckDB raises 'Conflicting lock', AnnotationWriter should POST
    /admin/release/{db_id} and retry — succeeding once the sidecar dies."""
    db = tmp_path / "release_test.duckdb"
    build_synthetic_duckdb(db)

    sidecar = _spawn_sidecar_holding_lock(db)
    mock = MockAntServe()
    mock.sidecar_proc = sidecar
    base_url, server, _thread = mock.serve()

    try:
        values = pd.Series({name: float(i) for i, name in enumerate(CELL_NAMES)})
        with AnnotationWriter(
            db,
            annotator='test', run_id='r1',
            db_id='test_db', ant_serve_url=base_url,
        ) as writer:
            writer.add_numerical('after_release', values)
            writer.commit()
    finally:
        server.shutdown()
        if sidecar.poll() is None:
            sidecar.terminate()

    # Verify the writer hit release (and later restore)
    assert mock.release_calls == ['test_db'], mock.release_calls
    assert mock.restore_calls == ['test_db'], mock.restore_calls

    # Verify the column was actually written
    con = duckdb.connect(str(db), read_only=True)
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM obsnum WHERE after_release IS NOT NULL"
        ).fetchone()[0]
    finally:
        con.close()
    assert n == len(CELL_NAMES)


def test_release_failure_raises(tmp_path):
    """If ant-serve says release wasn't ok, the writer should fail clearly."""
    db = tmp_path / "fail.duckdb"
    build_synthetic_duckdb(db)

    sidecar = _spawn_sidecar_holding_lock(db)
    mock = MockAntServe()
    mock.sidecar_proc = sidecar
    mock.return_value = {'ok': False, 'still_in_use': 1}
    base_url, server, _thread = mock.serve()

    try:
        with pytest.raises(AnnotationWriterError, match="did not succeed"):
            with AnnotationWriter(
                db, annotator='test', run_id='r1',
                db_id='test_db', ant_serve_url=base_url,
            ) as writer:
                writer.add_numerical(
                    'never_written',
                    pd.Series({CELL_NAMES[0]: 1.0}),
                )
    finally:
        # Sidecar is still alive in this branch — kill it manually
        server.shutdown()
        if sidecar.poll() is None:
            sidecar.terminate()


def test_no_coordination_url_propagates_original_error(tmp_path):
    """Without db_id/ant_serve_url, the lock conflict propagates as
    AnnotationWriterError with a helpful message."""
    db = tmp_path / "no_coord.duckdb"
    build_synthetic_duckdb(db)

    sidecar = _spawn_sidecar_holding_lock(db)
    try:
        with pytest.raises(AnnotationWriterError, match="no \\(db_id, ant_serve_url\\)"):
            with AnnotationWriter(db, annotator='t', run_id='r1') as writer:
                writer.add_numerical('x', pd.Series({CELL_NAMES[0]: 1.0}))
    finally:
        if sidecar.poll() is None:
            sidecar.terminate()


def test_post_ant_serve_warn_on_connection_refused():
    """When ant-serve isn't running, on_failure='warn' should return None
    silently — used by __exit__'s restore call."""
    result = _post_ant_serve(
        'http://127.0.0.1:1',  # nothing listens here
        '/admin/restore/foo',
        on_failure='warn',
        timeout=1.0,
    )
    assert result is None
