"""The writer lock of a served duckdb: an exclusive flock on ``<stem>.duckdb.lock``.

Every tool that writes a duckdb in the store takes this lock (``AnnotationWriter``, ``DEWriter``,
``ingest_pipeline.writer_lock``, ``ant-annotate``, ``ant-merge-h5ad``, ``ant-drop-indexes``). The lock file
is only an anchor and is removed on release, so no 0-byte ``.lock`` files stay in the store (2.47.0
scratch-site check).

Removing a lock file is safe only when every taker checks, after its flock succeeds, that the path still
names the file it locked (same device and inode). Otherwise a waiter that opened the old file would get its
lock after the holder removed it, while a newcomer creates and locks a new file, and both would write. So
``release`` unlinks while the lock is still held, and ``try_acquire`` retries when the path was removed or
replaced between its open and its flock.
"""
from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path
from typing import IO, Optional


class LockBusy(TimeoutError):
    """Another writer holds the lock."""


def lock_path_for(duckdb_path: Path) -> Path:
    """``<name>.lock`` next to the duckdb."""
    duckdb_path = Path(duckdb_path)
    return duckdb_path.with_name(duckdb_path.name + '.lock')


def _same_file(lock_path: Path, handle: IO) -> bool:
    try:
        on_disk = os.stat(lock_path)
    except FileNotFoundError:
        return False
    held = os.fstat(handle.fileno())
    return (on_disk.st_dev, on_disk.st_ino) == (held.st_dev, held.st_ino)


def try_acquire(lock_path: Path) -> Optional[IO]:
    """One attempt: the locked handle, or None when another writer holds the lock."""
    lock_path = Path(lock_path)
    while True:
        handle = open(lock_path, 'a+')
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
        except BaseException:
            handle.close()
            raise
        if _same_file(lock_path, handle):
            return handle
        # the holder before us removed (or someone replaced) the file between our open and our flock: the lock
        # we got protects nothing; take the lock on what the path names now
        handle.close()


def acquire(lock_path: Path, timeout: float, poll: float = 0.5) -> IO:
    """Wait up to ``timeout`` seconds for the lock; raises ``LockBusy`` after that."""
    deadline = time.monotonic() + timeout
    while True:
        handle = try_acquire(lock_path)
        if handle is not None:
            return handle
        if time.monotonic() >= deadline:
            raise LockBusy(f'{lock_path} is held by another writer')
        time.sleep(poll)


def release(handle: Optional[IO], lock_path: Path) -> None:
    """Remove the lock file while still holding it (only when it is still ours), then unlock and close."""
    if handle is None:
        return
    try:
        if _same_file(Path(lock_path), handle):
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                pass
    except OSError:
        pass                                   # a read-only folder: the anchor stays, the lock is still released
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()
