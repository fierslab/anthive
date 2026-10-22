"""Version class of a new h5ad against the published one (2.47.0, Mark 2026-10-22): ``minor`` or ``major``.

Semantics of dataset versions (design section 25, ``doc/DESIGN_dataset_ingestion.md``):

* **patch**: metadata only (``.md`` keys, caveats, mapping); the data files are unchanged.
* **minor**: additive. Nothing that existed is changed or removed: the same cells (``obs_names``) in the same
  order, ``X`` and every existing layer identical, every existing ``obs``, ``var``, ``obsm``, ``varm`` and
  ``obsp`` entry identical; new obs columns, embeddings, layers or var columns may be added. The predecessor
  of a minor version can be removed without losing data.
* **major**: anything else (X or a layer changed, cells or genes added, removed or reordered, an existing obs
  value changed, a column removed, ...).

:func:`classify` reads both files read-only with h5py and compares every dataset of the old file with the
same path in the new one in bounded blocks (``BLOCK_ELEMENTS`` values at a time), so memory stays flat for
multi-GB files and the comparison stops at the first difference. ``uns`` is metadata and not compared. A
depositor cannot ask for a lower class: the comparison decides.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple

import numpy as np

#: values read per block when two datasets are compared
BLOCK_ELEMENTS = 8 * 1024 * 1024
#: top-level entries whose existing content must not change (``uns`` is free metadata)
COMPARED = ('X', 'layers', 'obs', 'var', 'obsm', 'varm', 'obsp', 'varp', 'raw')
#: where an added key is an addition (a new obs column, embedding, layer, ...) rather than a change
ADDITIVE = ('layers', 'obs', 'var', 'obsm', 'varm', 'obsp', 'varp')


@dataclass
class Classification:
    """``part`` (``minor`` or ``major``), the reason in one sentence, what was added and the seconds taken."""
    part: str
    reason: str
    added: List[str] = field(default_factory=list)
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return {'class': self.part, 'reason': self.reason, 'added': list(self.added), 'seconds': self.seconds}


class _Different(Exception):
    """The first difference found (its message is the reason)."""


def _equal_block(old: np.ndarray, new: np.ndarray) -> bool:
    if old.shape != new.shape:
        return False
    if old.dtype.kind in 'fc' and new.dtype.kind in 'fc':
        return bool(np.array_equal(old, new, equal_nan=True))
    return bool(np.array_equal(old, new))


def _compare_dataset(old: Any, new: Any, path: str) -> None:
    if old.shape != new.shape:
        raise _Different(f'{path} changed shape {old.shape} -> {new.shape}')
    if old.dtype != new.dtype and not (old.dtype.kind in 'OSU' and new.dtype.kind in 'OSU'):
        raise _Different(f'{path} changed type {old.dtype} -> {new.dtype}')
    if old.shape == ():
        if not _equal_block(np.asarray(old[()]), np.asarray(new[()])):
            raise _Different(f'{path} differs')
        return
    rows = old.shape[0]
    per_row = int(np.prod(old.shape[1:])) if len(old.shape) > 1 else 1
    step = max(1, BLOCK_ELEMENTS // max(1, per_row))
    for start in range(0, rows, step):
        stop = min(rows, start + step)
        if not _equal_block(np.asarray(old[start:stop]), np.asarray(new[start:stop])):
            raise _Different(f'{path} differs (rows {start} to {stop - 1})')


def _compare(old: Any, new: Any, path: str, added: List[str], additive: bool) -> None:
    import h5py  # noqa: PLC0415

    if isinstance(old, h5py.Dataset):
        if not isinstance(new, h5py.Dataset):
            raise _Different(f'{path} changed from an array to a group')
        _compare_dataset(old, new, path)
        return
    if not isinstance(new, h5py.Group):
        raise _Different(f'{path} changed from a group to an array')
    for attr in ('encoding-type', 'shape'):         # a sparse matrix group: same encoding, same dimensions
        if attr in old.attrs and not np.array_equal(np.asarray(old.attrs[attr]), np.asarray(new.attrs.get(attr))):
            raise _Different(f'{path} changed {attr} {old.attrs[attr]} -> {new.attrs.get(attr)}')
    for key in old.keys():
        if key not in new:
            raise _Different(f'{path}/{key} was removed')
        _compare(old[key], new[key], f'{path}/{key}', added, additive=False)
    if additive:
        added.extend(f'{path}/{key}' for key in new.keys() if key not in old)


def _obs_names(handle: Any) -> Optional[Any]:
    obs = handle.get('obs')
    if obs is None:
        return None
    index = obs.attrs.get('_index', '_index')
    if isinstance(index, bytes):
        index = index.decode()
    return obs.get(index)


def classify(old_path: Path, new_path: Path) -> Classification:
    """``minor`` when ``new_path`` only adds to ``old_path``, else ``major`` with the first difference."""
    import h5py  # noqa: PLC0415

    started = time.monotonic()
    added: List[str] = []
    try:
        with h5py.File(old_path, 'r') as old, h5py.File(new_path, 'r') as new:
            old_names, new_names = _obs_names(old), _obs_names(new)
            if old_names is None or new_names is None:
                raise _Different('obs names cannot be read')
            try:
                _compare(old_names, new_names, 'obs_names', [], additive=False)   # an array or an encoded group
            except _Different as exc:
                raise _Different(f'the cells differ: {exc}') from None
            for key in COMPARED:
                if key not in old:
                    if key in new and key != 'raw':
                        added.append(key)
                    continue
                if key not in new:
                    raise _Different(f'{key} was removed')
                _compare(old[key], new[key], key, added, additive=key in ADDITIVE)
    except _Different as exc:
        return Classification('major', f'data altered: {exc}', [], round(time.monotonic() - started, 2))
    seconds = round(time.monotonic() - started, 2)
    if added:
        shown = ', '.join(added[:10]) + (f' and {len(added) - 10} more' if len(added) > 10 else '')
        return Classification('minor', f'additive: existing cells, X, layers and annotations identical; added '
                                       f'{shown}', added, seconds)
    return Classification('minor', 'additive: existing cells, X, layers and annotations identical (no data '
                                   'change)', [], seconds)


def classify_or_major(old_path: Optional[Path], new_path: Path) -> Tuple[str, str, dict]:
    """``(part, reason, details)``; ``major`` when the old file is not available to compare with."""
    if old_path is None or not Path(old_path).is_file():
        return 'major', 'data altered: the published h5ad is not on this host to compare with', {}
    try:
        result = classify(Path(old_path), Path(new_path))
    except Exception as exc:  # noqa: BLE001 - an unreadable file never yields a lower class
        return 'major', f'data altered: the files could not be compared ({type(exc).__name__}: {exc})', {}
    return result.part, result.reason, result.as_dict()
