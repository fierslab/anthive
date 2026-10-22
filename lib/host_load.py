"""Host load gates shared by background jobs (ant-score-fleet, the watcher's h5ad merge).

A polite background job starts a unit of work only while the 1 minute load average of other users is at most
a limit and enough memory is available. Linux only (``/proc``); elsewhere the gate reads as idle.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional


def load_average() -> float:
    """The 1 minute load average (0.0 where ``/proc/loadavg`` is missing)."""
    try:
        return float(Path('/proc/loadavg').read_text().split()[0])
    except (OSError, ValueError, IndexError):
        try:
            return float(os.getloadavg()[0])
        except (OSError, AttributeError):
            return 0.0


def mem_available_gb() -> float:
    """``MemAvailable`` of ``/proc/meminfo`` in GB (infinite where it cannot be read: no gate)."""
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) / 1024 / 1024
    except (OSError, ValueError, IndexError):
        pass
    return float('inf')


def busy_reason(max_load: float, min_free_gb: float, own_load: float = 0.0) -> Optional[str]:
    """Why the host is too busy for background work now, or None. ``own_load``: load the caller itself adds."""
    other = load_average() - own_load
    free = mem_available_gb()
    if other > max_load:
        return f'load {other:.1f} is above {max_load:g}'
    if free < min_free_gb:
        return f'free memory {free:.0f} GB is below {min_free_gb:g} GB'
    return None


def disk_free_bytes(path: Path) -> int:
    """Free bytes on the file system that holds ``path``."""
    return shutil.disk_usage(path).free
