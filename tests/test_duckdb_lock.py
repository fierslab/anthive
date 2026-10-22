"""The duckdb writer lock (lib/duckdb_lock.py): no lock file left behind, and removing it is race-free
(2.47.0 scratch-site check: a 0-byte <stem>.duckdb.lock stayed in the store after obs-columns writes)."""
from __future__ import annotations

import fcntl
import os

import pytest

import duckdb_lock


def test_release_removes_the_lock_file(tmp_path):
    lock = duckdb_lock.lock_path_for(tmp_path / 'x.duckdb')
    assert lock.name == 'x.duckdb.lock'
    handle = duckdb_lock.acquire(lock, timeout=1)
    assert lock.exists()
    assert duckdb_lock.try_acquire(lock) is None                       # held
    duckdb_lock.release(handle, lock)
    assert not lock.exists()
    duckdb_lock.release(duckdb_lock.acquire(lock, timeout=1), lock)    # free again
    assert not lock.exists()


def test_acquire_times_out_while_held(tmp_path):
    lock = tmp_path / 'x.duckdb.lock'
    handle = duckdb_lock.acquire(lock, timeout=1)
    with pytest.raises(duckdb_lock.LockBusy):
        duckdb_lock.acquire(lock, timeout=0.2, poll=0.05)
    duckdb_lock.release(handle, lock)


def test_a_waiter_on_the_removed_file_does_not_count_as_holding_the_lock(tmp_path):
    """A waiter that opened the old file gets its flock once the holder released, but on a file nobody else
    sees: the inode check makes it take the lock on the current file instead."""
    lock = tmp_path / 'x.duckdb.lock'
    holder = duckdb_lock.acquire(lock, timeout=1)
    stale = open(lock, 'a+')                                         # opened before the release
    duckdb_lock.release(holder, lock)
    fcntl.flock(stale.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)        # succeeds on the orphaned inode
    assert not duckdb_lock._same_file(lock, stale)
    newcomer = duckdb_lock.try_acquire(lock)
    assert newcomer is not None and duckdb_lock._same_file(lock, newcomer)
    assert duckdb_lock.try_acquire(lock) is None                      # one holder on the real file
    stale.close()
    duckdb_lock.release(newcomer, lock)


def test_try_acquire_retries_when_the_file_is_removed_between_open_and_flock(tmp_path, monkeypatch):
    lock = tmp_path / 'x.duckdb.lock'
    real_flock = fcntl.flock
    calls = []

    def racing_flock(fd, operation):
        real_flock(fd, operation)
        if operation & fcntl.LOCK_EX and not calls:
            calls.append(fd)
            os.unlink(lock)                                           # the previous holder's release
    monkeypatch.setattr(duckdb_lock.fcntl, 'flock', racing_flock)
    handle = duckdb_lock.try_acquire(lock)
    assert calls and handle is not None and duckdb_lock._same_file(lock, handle)
    duckdb_lock.release(handle, lock)
    assert not lock.exists()


def test_release_never_removes_a_file_it_does_not_hold(tmp_path):
    lock = tmp_path / 'x.duckdb.lock'
    first = duckdb_lock.acquire(lock, timeout=1)
    os.unlink(lock)
    other = duckdb_lock.acquire(lock, timeout=1)                      # someone else's file now
    duckdb_lock.release(first, lock)
    assert lock.exists() and duckdb_lock._same_file(lock, other)
    duckdb_lock.release(other, lock)
    assert not lock.exists()


def test_writer_lock_leaves_no_lock_file(tmp_path):
    from ingest_pipeline import writer_lock
    duckdb_path = tmp_path / 'x.duckdb'
    with writer_lock(duckdb_path, timeout=1):
        assert (tmp_path / 'x.duckdb.lock').exists()
    assert not (tmp_path / 'x.duckdb.lock').exists()


def test_every_writer_of_the_duckdb_lock_uses_duckdb_lock():
    """A taker without the inode check would race with the removal; keep them all on the helper."""
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    for rel in ('lib/anthive_db.py', 'lib/de_storage.py', 'lib/ingest_pipeline.py', 'bin/annot/ant-annotate',
                'bin/ingest/ant-merge-h5ad', 'bin/ingest/ant-drop-indexes'):
        source = (repo / rel).read_text()
        assert 'duckdb_lock' in source, rel
        assert 'fcntl.LOCK_EX' not in source, rel
