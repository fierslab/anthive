"""Replicate unit of a dataset and the analysis level rule.

A dataset's *replicate unit* says what an independent biological replicate is:
``donor`` (human post-mortem or biopsy), ``animal`` (mouse, xenograft host),
``sample`` (independent samples without a person or animal id) or ``none``
(no replicate structure; nothing can be tested between replicates).
``replicate_column`` names the obs column that identifies the replicate
(``ant_donor`` for the human cohorts, ``hash.ID`` or a mouse id for
xenografts). Both are catalog keys of the dataset ``.md``; when they are not
declared they are inferred (``infer_replicate``).

``analysis_level`` is the shared rule that decides, per dataset x stratum x
facet, whether a statistical test is allowed: replicate-level (pseudobulk by
``replicate_column``) when there are enough replicates, otherwise the numbers
are cell-level and descriptive only. The facet tools, the per-cell variability
tools and ``lib/facet_results.py`` all use it so the rule has one definition.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

import facet_rules

REPLICATE_UNITS = ('donor', 'animal', 'sample', 'none')
DEFAULT_COLUMN_BY_UNIT = {'donor': 'ant_donor'}

#: replicates per level of a two-group facet needed for a replicate-level test (lib/facet_rules.py)
MIN_REPLICATES_PER_LEVEL = facet_rules.MIN_REPLICATES_PER_LEVEL
#: replicates of a numeric facet (Braak, age) needed for a replicate-level test ...
MIN_REPLICATES_NUMERIC = facet_rules.NUMERIC_MIN_REPLICATES
#: ... and the distinct values among them
MIN_DISTINCT_NUMERIC = facet_rules.NUMERIC_MIN_DISTINCT

LEVEL_REPLICATE = 'replicate'
LEVEL_CELL = 'cell'
CELL_LEVEL_LABEL = 'cell-level, descriptive only'

#: how ``replicate_unit`` was obtained
BASIS_DECLARED = 'declared'
BASIS_INFERRED = 'inferred'

#: marker of the numbers of the per-cell variability tools: cells are not replicates, so these stay
#: descriptive whether a replicate-level analysis exists or is impossible
PER_CELL_LABEL = 'per-cell (descriptive)'
REPLICATE_LABEL = 'replicate level'

_ANIMAL_COLUMN_RE = re.compile(r'(?i)^(hash([._]?id)?|mouse([._ ]?id)?|animal([._ ]?id)?|mice|subject([._]?id)?)$')
_ANIMAL_SYSTEMS = {'xenograft', 'chimera', 'in_vivo', 'mouse_model'}


class ReplicateError(ValueError):
    """An invalid ``replicate_unit`` / ``replicate_column`` declaration."""


def _as_text_list(value: Any) -> Sequence[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip().lower() for v in value if v is not None]
    return [str(value).strip().lower()]


def declared_replicate(metadata: Mapping[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """``(replicate_unit, replicate_column)`` as written in the ``.md`` (None when absent).

    Raises:
        ReplicateError: the unit is not one of ``REPLICATE_UNITS`` or the
            column is not a non-empty string.
    """
    unit = metadata.get('replicate_unit')
    column = metadata.get('replicate_column')
    if unit is not None:
        if not isinstance(unit, str) or unit not in REPLICATE_UNITS:
            raise ReplicateError(f'replicate_unit {unit!r} is not one of {", ".join(REPLICATE_UNITS)}')
    if column is not None and (not isinstance(column, str) or not column.strip()):
        raise ReplicateError(f'replicate_column {column!r} must be a non-empty string')
    return unit, column


def infer_replicate(annotation: Mapping[str, Any], obs_columns: Iterable[str]
                    ) -> Tuple[str, Optional[str]]:
    """Default ``(replicate_unit, replicate_column)`` of a dataset without a declaration.

    * mouse, xenograft or chimera: ``animal`` with the first animal id column
      (hash.ID, Mouse_ID, mouse.id, animal, ...); ``ant_donor`` is NOT used there
      (in a xenograft it names the human donor line, not the replicate);
      without such a column ``none``;
    * everything else with ``ant_donor`` (human post-mortem, biopsy, ...):
      ``donor`` / ``ant_donor``;
    * otherwise ``none``.

    Args:
        annotation: Resolved ``.h5ad.mus`` annotation (``organism``,
            ``experimental_system``).
        obs_columns: Names of the dataset's served obs columns.
    """
    columns = list(obs_columns)
    organisms = _as_text_list(annotation.get('organism'))
    systems = set(_as_text_list(annotation.get('experimental_system')))
    animal_like = bool(systems & _ANIMAL_SYSTEMS) or any('mouse' in o or 'mus' in o for o in organisms)
    if animal_like:
        for column in columns:
            if _ANIMAL_COLUMN_RE.match(column):
                return 'animal', column
        return 'none', None
    if 'ant_donor' in columns:
        return 'donor', 'ant_donor'
    return 'none', None


def resolve_replicate(metadata: Mapping[str, Any], annotation: Mapping[str, Any],
                      obs_columns: Iterable[str]) -> Dict[str, Optional[str]]:
    """Declared or inferred replicate unit of one dataset.

    Returns:
        ``{replicate_unit, replicate_column, basis}``; ``basis`` is
        ``declared`` when the ``.md`` states the unit, else ``inferred``. A
        declared unit without a column gets the default column of that unit
        (``ant_donor`` for ``donor``); ``none`` has no column.

    Raises:
        ReplicateError: invalid declaration.
    """
    unit, column = declared_replicate(metadata)
    if unit is None:
        unit, column = infer_replicate(annotation, obs_columns)
        return {'replicate_unit': unit, 'replicate_column': column, 'basis': BASIS_INFERRED}
    if unit == 'none':
        column = None
    elif column is None:
        column = DEFAULT_COLUMN_BY_UNIT.get(unit)
    return {'replicate_unit': unit, 'replicate_column': column, 'basis': BASIS_DECLARED}


def analysis_level(replicate_unit: Optional[str], numeric: bool,
                   n_per_level: Optional[Mapping[Any, int]] = None,
                   n_replicates: Optional[int] = None, n_distinct: Optional[int] = None,
                   min_per_level: Optional[int] = None,
                   min_numeric: Optional[int] = None,
                   min_distinct: int = MIN_DISTINCT_NUMERIC) -> Dict[str, Any]:
    """Level at which one (dataset, stratum, facet) may be analysed.

    Replicate level needs ``replicate_unit`` other than ``none`` and, for a
    two-group facet, at least ``min_per_level`` replicates in every level, or,
    for a numeric facet, at least ``min_numeric`` replicates with at least
    ``min_distinct`` distinct values (v3: 4 and 3, the statistical minimum of a
    trend with one covariate-free slope; v2 asked 6 and 2). Otherwise the numbers are cell-level and descriptive
    only: no p values, with an explicit reason.

    Args:
        replicate_unit: ``donor``, ``animal``, ``sample``, ``none`` or None
            (unknown, treated as ``none``).
        numeric: True for a numeric facet (Braak, age).
        n_per_level: Replicates per level (two-group facet).
        n_replicates: Replicates with a value (numeric facet).
        n_distinct: Distinct values among them (numeric facet).

    Returns:
        ``{analysis_level, label, reason}``; ``analysis_level`` is ``replicate``
        or ``cell``, ``reason`` is None for ``replicate``.
    """
    if min_per_level is None:
        min_per_level = facet_rules.min_per_level(replicate_unit)
    if min_numeric is None:
        min_numeric = facet_rules.min_numeric(replicate_unit)

    def cell(reason: str) -> Dict[str, Any]:
        return {'analysis_level': LEVEL_CELL, 'label': CELL_LEVEL_LABEL, 'reason': reason}

    if replicate_unit in (None, 'none'):
        return cell('no replicate unit (replicate_unit is none): cells are not independent replicates')
    if numeric:
        have = int(n_replicates or 0)
        if have < min_numeric or int(n_distinct or 0) < min_distinct:
            return cell(f'{have} {replicate_unit}s with a value spanning {int(n_distinct or 0)} distinct '
                        f'values (need {min_numeric} with at least {min_distinct} distinct values)')
    else:
        counts = {k: int(v) for k, v in (n_per_level or {}).items()}
        short = {k: v for k, v in counts.items() if v < min_per_level}
        if len(counts) < 2 or short:
            detail = ', '.join(f'{k}={v}' for k, v in counts.items()) or 'no levels'
            return cell(f'fewer than {min_per_level} {replicate_unit}s in a level ({detail})')
    return {'analysis_level': LEVEL_REPLICATE, 'label': REPLICATE_LABEL, 'reason': None}


def per_cell_basis(replicate_unit: Optional[str]) -> Dict[str, Any]:
    """Marker block of the per-cell variability numbers of one dataset.

    The numbers of ``ant-module-variability`` and ``ant-gene-variance-precompute``
    treat cells as units, so they stay descriptive: a donor-level facet is
    constant within a donor and mostly measures donor identity. They are marked
    ``per-cell (descriptive)`` whether a replicate-level analysis exists
    (``/databases/{id}/facet-tests``) or is impossible (``replicate_unit`` none).

    Returns:
        ``{basis, replicate_unit, replicate_level_analysis}`` where the last
        is ``possible`` (a replicate unit is declared or inferred) or ``impossible``.
    """
    return {'basis': PER_CELL_LABEL, 'replicate_unit': replicate_unit,
            'replicate_level_analysis': 'impossible' if replicate_unit in (None, 'none') else 'possible'}


def dataset_replicate_unit(md_path: Any, duckdb_path: Any, store_root: Any = None) -> str:
    """Declared or inferred ``replicate_unit`` of a dataset on disk (never raises).

    Used by the per-cell variability tools, which have no obs view at hand: the
    columns are the stored obs columns plus the ``ant_*`` columns the ``.md``
    ``api:`` section writes.

    Args:
        md_path: The dataset ``.md``.
        duckdb_path: The dataset ``.duckdb`` (None or missing: only the api section is used).
        store_root: h5adstore root for the ``.h5ad.mus`` annotation cascade.

    Returns:
        ``donor``, ``animal``, ``sample``, ``none``, or ``unresolved`` when the
        declaration is invalid or the files cannot be read.
    """
    from pathlib import Path  # noqa: PLC0415

    import metadata  # noqa: PLC0415
    import mus  # noqa: PLC0415
    if not Path(md_path).exists():
        return 'unresolved'
    try:
        md_yaml, _ = metadata.parse_md_file(Path(md_path))
        api = metadata.get_api_config(md_yaml) or {}
        columns = set((api.get('obs_rename') or {}).values()) | set(api.get('obs_derive') or {}) \
            | set(api.get('obs_constant') or {})
        if duckdb_path is not None and Path(duckdb_path).exists():
            import duckdb  # noqa: PLC0415
            connection = duckdb.connect(str(duckdb_path), read_only=True)
            try:
                columns |= {row[0] for row in connection.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name IN ('__obscat', '__obsnum')").fetchall()}
            finally:
                connection.close()
        h5ad_path = Path(md_path).with_suffix('.h5ad')
        annotation = mus.resolve_annotation(h5ad_path, root=store_root)
        return resolve_replicate(md_yaml, annotation, columns)['replicate_unit'] or 'none'
    except Exception:
        return 'unresolved'
