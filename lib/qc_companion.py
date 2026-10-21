"""QC companion datasets (designation decided by Mark, 2026-10-21).

The QC round of a workflow keeps every cell with its per-cell QC statistics and a keep flag and is deposited
as ``<name>_qc``; the cleanup round deposits the kept cells as the final dataset. The QC dataset is the
*QC companion* of the final one:

* ``.md`` keys: ``qc: true`` and ``qc_of: <db_id of the final dataset>``. ``qc_of`` may stay empty while the
  final dataset is not deposited yet (it is filled later by a metadata-only update; both keys are
  depositor-editable).
* A QC companion is always secondary (``is_secondary: true``; the route sets it when absent and the checks
  refuse ``is_secondary: false``), so every counting tool leaves it out; it is never facet-tested
  (``facet_scopes.facet_exclusion_reason``) and gets the caveat :func:`caveat_message`.
* The overlap checks exempt the pair (either direction): the companion of a dataset is named by ``qc_of``, or,
  while ``qc_of`` is empty, by the file name rule ``<basename>_qc`` next to ``<basename>`` in the same group.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

QC_KEY = 'qc'
QC_OF_KEY = 'qc_of'
QC_SUFFIX = '_qc'
CAVEAT_TOPIC = 'qc_companion'


def is_qc(metadata: Optional[Mapping[str, Any]]) -> bool:
    """True when the ``.md`` says ``qc: true`` (or names ``qc_of``)."""
    metadata = metadata or {}
    return metadata.get(QC_KEY) is True or bool(str(metadata.get(QC_OF_KEY) or '').strip())


def qc_target(metadata: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The db id named by ``qc_of``, or None."""
    value = str((metadata or {}).get(QC_OF_KEY) or '').strip()
    return value or None


def caveat_message(target: Optional[str]) -> str:
    """The caveat every QC companion carries."""
    of = f'QC companion of {target}' if target else 'QC companion (its final dataset is not deposited yet)'
    return f'{of}: all cells before filtering; not counted in statistics.'


def is_pair(qc_meta: Mapping[str, Any], qc_db_id: str, qc_basename: str, other_db_id: str, other_basename: str,
            same_group: bool) -> bool:
    """True when ``qc_meta`` (a QC companion) belongs to the other dataset.

    ``qc_of`` decides when set; while it is empty, a dataset of the same group whose basename is the companion's
    without ``_qc`` is its final dataset.
    """
    if not is_qc(qc_meta) or qc_db_id == other_db_id:
        return False
    target = qc_target(qc_meta)
    if target:
        return target.upper() == str(other_db_id).upper()
    return same_group and qc_basename.endswith(QC_SUFFIX) and qc_basename[:-len(QC_SUFFIX)] == other_basename
