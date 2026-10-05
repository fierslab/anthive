"""Scope rules shared by the per-cell statistics tools.

``ant-module-variability`` and ``ant-gene-variance-precompute`` describe how a
module score or a gene differs between groups of *cells*. Two kinds of
dataset make that meaningless:

* bulk data (technology bulk, or about one observation per donor or
  sample): there are no cells to spread over, so the tool skips the dataset;
* donor-like columns (``ant_donor``, ``ant_donor_uid``, ...): identifiers,
  never continuous. They must always be treated as categorical, never
  binned into quantiles.

The tools record every skipped dataset and its reason in a small JSON file
next to their aggregate parquet (``<stem>.skipped.json``). ant-serve reads it
to explain the absence of rows.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

#: A dataset with at most this many observations per donor counts as bulk.
ONE_PER_UNIT_RATIO = 1.5

#: View columns holding the donor identifier, in order of preference.
DONOR_COLUMNS = ('ant_donor_uid', 'ant_donor')

_DONOR_LIKE = re.compile(
    r'^(?:ant_)?(?:author_)?(?:donor|patient|subject|individual|participant)(?:_?(?:id|uid))?$',
    re.IGNORECASE,
)


def is_donor_like(column: str) -> bool:
    """Whether ``column`` names a donor (patient, subject, ...) identifier.

    Args:
        column: Obs column name, with or without the ``ant_`` prefix.

    Returns:
        True for names such as ``ant_donor``, ``ant_donor_uid``,
        ``patient_id``; False for attributes of a donor such as
        ``ant_donor_age``.
    """
    return bool(_DONOR_LIKE.match(column or ''))


def _annotation_texts(annotation: Mapping[str, Any], field: str) -> list[str]:
    """Values of one annotation field as a list of strings."""
    value = annotation.get(field)
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple, set)) else [value]
    return [str(item) for item in items if item is not None]


def bulk_technology(annotation: Mapping[str, Any]) -> Optional[str]:
    """The technology value that marks the dataset as bulk, if any.

    Args:
        annotation: Resolved ``.mus`` annotation (``technology``,
            ``technology_category``).

    Returns:
        The first technology or category text containing the word ``bulk``,
        else None.
    """
    for field in ('technology_category', 'technology'):
        for text in _annotation_texts(annotation, field):
            if re.search(r'\bbulk\b|bulk[_ -]', text, re.IGNORECASE):
                return text
    return None


def scope_skip_reason(annotation: Mapping[str, Any], n_obs: Optional[int],
                      n_donors: Optional[int]) -> Optional[str]:
    """Why a dataset is out of scope for per-cell statistics, or None.

    Args:
        annotation: Resolved ``.mus`` annotation.
        n_obs: Number of observations (cells) in the dataset.
        n_donors: Number of distinct donors; None when no donor column exists.

    Returns:
        A short reason, or None when the dataset is in scope.
    """
    technology = bulk_technology(annotation)
    if technology:
        return f'bulk data (technology {technology}): there are no cells to describe'
    if n_obs and n_donors and n_obs <= n_donors * ONE_PER_UNIT_RATIO:
        return (f'about one observation per donor ({n_obs} observations, {n_donors} donors): '
                'bulk-like, there are no cells to describe')
    return None


def count_obs_and_donors(conn: Any) -> tuple[Optional[int], Optional[int]]:
    """Observation and donor counts of one served ``.duckdb``.

    Args:
        conn: Open DuckDB connection with ``__cells`` and the ``obscat`` /
            ``obsnum`` views.

    Returns:
        ``(n_obs, n_donors)``; either is None when it cannot be determined.
    """
    try:
        n_obs = int(conn.execute('SELECT COUNT(*) FROM __cells').fetchone()[0])
    except Exception:  # noqa: BLE001 - a malformed dataset is simply not skipped
        return None, None
    for view in ('obscat', 'obsnum'):
        for column in DONOR_COLUMNS:
            try:
                present = conn.execute(
                    'SELECT COUNT(*) FROM information_schema.columns '
                    'WHERE table_name = ? AND column_name = ?', [view, column]).fetchone()[0]
                if not present:
                    continue
                quoted = '"' + column.replace('"', '""') + '"'
                n_donors = conn.execute(
                    f'SELECT COUNT(DISTINCT {quoted}) FROM {view}').fetchone()[0]
                if n_donors:
                    return n_obs, int(n_donors)
            except Exception:  # noqa: BLE001
                continue
    return n_obs, None


def skip_summary_path(parquet_path: Path) -> Path:
    """Path of the skip summary that belongs to an aggregate parquet."""
    return parquet_path.with_name(parquet_path.stem + '.skipped.json')


def read_skip_summary(path: Path) -> Dict[str, str]:
    """Read a skip summary; empty when missing or unreadable."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return {str(key): str(value) for key, value in data.items()} if isinstance(data, dict) else {}


def merge_skip_summary(path: Path, processed: Iterable[str],
                       reasons: Mapping[str, str]) -> Dict[str, str]:
    """Update the skip summary for the datasets a run looked at.

    Datasets the run did not process keep their earlier entry (a ``--only``
    run must not erase the others).

    Args:
        path: Summary file to read and atomically rewrite.
        processed: db_ids this run evaluated.
        reasons: db_id -> reason for those that were skipped.

    Returns:
        The merged mapping that was written.
    """
    merged = read_skip_summary(path)
    for db_id in processed:
        merged.pop(db_id, None)
    merged.update(reasons)
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(dict(sorted(merged.items())), indent=1) + '\n')
    os.replace(tmp, path)
    return merged
