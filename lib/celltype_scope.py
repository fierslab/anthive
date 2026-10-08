"""Cell type scope of gene modules: which cells of a dataset a module may be scored on.

A geneset-store experiment declares ``celltypes`` (Cell Ontology labels, or the
explicit value ``all``) and optionally ``role: markers``; a per-geneset ``.md``
may override both (``lib/geneset.py``). This module turns that declaration into
a decision per cell:

* a label scope (``[microglial cell]``) matches the cells whose cell type
  belongs to the same group of ``lib/celltype_groups.yaml`` (``group_of`` of the
  cell's ``ant_cell_type`` equals ``group_of`` of one of the module's labels);
* ``all`` and every ``role: markers`` module match every cell;
* a module is scored on its matching cells only; the other cells get no row in
  the scores sidecar (NULL, never zero).

``cell_type_groups`` reads the per-cell cell type of a dataset the way the
facet test tools do (the current ``.md`` obs mapping applied to the stored obs
tables), with the dataset constant of the ``.h5ad.mus`` as fallback.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional

import duckdb
import pandas as pd

from anthive_db import build_obs_view_sql  # type: ignore
from celltype_groups import UNMAPPED, group_of  # type: ignore
from geneset import CELLTYPES_ALL, ROLE_MARKERS, Geneset  # type: ignore
from metadata import get_api_config, parse_md_file  # type: ignore
import mus  # type: ignore

CELL_TYPE_COLUMN = 'ant_cell_type'


@dataclass(frozen=True)
class ModuleScope:
    """Resolved cell type scope of one module.

    Attributes:
        celltypes: the declared labels (``('all',)`` for an unrestricted scope).
        role: ``markers`` for a QC marker panel, else None.
        groups: cell type groups the module applies to; None means every cell.
    """
    celltypes: tuple
    role: Optional[str]
    groups: Optional[FrozenSet[str]]

    @property
    def is_markers(self) -> bool:
        """True for a QC marker panel (never facet-tested)."""
        return self.role == ROLE_MARKERS

    @property
    def applies_everywhere(self) -> bool:
        """True when the module is scored on every cell (``all`` or markers)."""
        return self.groups is None

    def matches_group(self, group: Optional[str]) -> bool:
        """True when a cell (or stratum) of ``group`` is inside the scope."""
        return self.groups is None or (group is not None and group in self.groups)

    def matches_label(self, label: Any) -> bool:
        """True when a raw cell type label (``ant_cell_type``, a stratum label) is inside the scope."""
        return self.groups is None or group_of(label) in self.groups

    def cell_mask(self, cell_groups: pd.Series) -> pd.Series:
        """Boolean mask over a per-cell group series: the cells this module is scored on."""
        if self.groups is None:
            return pd.Series(True, index=cell_groups.index)
        return cell_groups.isin(self.groups)

    def key(self) -> tuple:
        """Hashable key: modules with equal keys are scored on the same cells."""
        return ('*',) if self.groups is None else tuple(sorted(self.groups))


def scope_from_labels(celltypes: Iterable[str], role: Optional[str] = None) -> ModuleScope:
    """Scope from declared labels and role.

    Args:
        celltypes: Cell Ontology labels or ``['all']``.
        role: ``markers`` or None.

    Returns:
        The resolved ModuleScope (markers and ``all`` scopes apply to every cell).
    """
    labels = tuple(celltypes)
    if role == ROLE_MARKERS or CELLTYPES_ALL in (label.lower() for label in labels):
        return ModuleScope(labels or (CELLTYPES_ALL,), role, None)
    return ModuleScope(labels, role, frozenset(group_of(label) for label in labels))


def module_scope(geneset: Geneset) -> ModuleScope:
    """Scope of a geneset (its own ``.md`` override, else its experiment's declaration)."""
    return scope_from_labels(geneset.celltypes, geneset.role)


def cell_type_labels(entry: Any, store_root: Optional[Path] = None) -> pd.DataFrame:
    """Per-cell cell type label of a dataset.

    Reads ``ant_cell_type`` through the obs views rebuilt from the current
    ``.md`` mapping (as ``ant-facet-test``). A dataset without that column falls
    back to the ``cell_type`` of its ``.h5ad.mus`` (one label for every cell
    when it names one cell type; no label when it names several).

    Args:
        entry: H5adEntry (``duckdb_path``, ``md_path``, ``h5ad_path``, ``db_id``, ``group_id``).
        store_root: h5adstore root (``.mus`` cascade, join tables); default the configured store.

    Returns:
        Frame with columns cell_id, cell_name, label (object, None where unknown),
        ordered by cell_id.
    """
    md_yaml, _ = parse_md_file(entry.md_path)
    api_config = get_api_config(md_yaml)
    conn = duckdb.connect(':memory:')
    try:
        conn.execute(f"ATTACH '{entry.duckdb_path}' AS ds (READ_ONLY)")
        conn.execute('USE ds')
        columns: Dict[str, List[str]] = {}
        for underscore in ('__obscat', '__obsnum'):
            columns[underscore] = [row[0] for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_catalog='ds' AND table_name = ? ORDER BY ordinal_position",
                [underscore]).fetchall()]
        cells = conn.execute('SELECT cell_id, cell_name FROM __cells ORDER BY cell_id').df()
        cells['label'] = None
        if columns['__obscat']:
            sql = build_obs_view_sql('obscat', '__obscat', columns['__obscat'], api_config,
                                     sibling_underscore_table='__obsnum',
                                     sibling_columns=columns['__obsnum'],
                                     db_label=entry.db_id, group_id=entry.group_id, store_root=store_root)
            conn.execute('CREATE OR REPLACE TEMP VIEW' + sql[len('CREATE VIEW'):])
            has_column = conn.execute("SELECT count(*) FROM (DESCRIBE obscat) WHERE column_name = ?",
                                      [CELL_TYPE_COLUMN]).fetchone()[0]
            if has_column:
                labelled = conn.execute(
                    f'SELECT c.cell_id, o."{CELL_TYPE_COLUMN}" AS label FROM __cells c '
                    f'JOIN obscat o ON o.cell_name = c.cell_name').df()
                cells = cells.drop(columns='label').merge(labelled, on='cell_id', how='left')
                cells['label'] = cells['label'].astype(object).where(cells['label'].notna(), None)
                cells = cells.sort_values('cell_id', ignore_index=True)
    finally:
        conn.close()
    if cells['label'].notna().any():
        return cells
    annotation = mus.resolve_annotation(entry.h5ad_path, root=store_root)
    raw = annotation.get('cell_type')
    values = raw if isinstance(raw, list) else ([raw] if raw else [])
    if len(values) == 1:
        cells['label'] = str(values[0])
    return cells


def cell_type_groups(entry: Any, store_root: Optional[Path] = None) -> pd.DataFrame:
    """``cell_type_labels`` plus the group of every label.

    Returns:
        Frame with columns cell_id, cell_name, label, group; ``group`` is
        ``unmapped`` for a label outside the table and None for a cell without
        any cell type information.
    """
    cells = cell_type_labels(entry, store_root)
    cells['group'] = [None if label is None else group_of(label) for label in cells['label']]
    return cells


def scope_cell_ids(scope: ModuleScope, cells: pd.DataFrame) -> pd.Series:
    """cell_id of the cells a scope applies to (all cells for an unrestricted scope).

    A cell without any cell type information is inside an unrestricted scope only.
    """
    if scope.groups is None:
        return cells['cell_id']
    return cells.loc[cells['group'].isin(scope.groups), 'cell_id']


def group_summary(cells: pd.DataFrame) -> Dict[str, int]:
    """Cells per group (``none`` for cells without cell type information)."""
    return {('none' if key is None or (isinstance(key, float) and pd.isna(key)) else str(key)): int(count)
            for key, count in cells['group'].value_counts(dropna=False).items()}


__all__ = ['ModuleScope', 'UNMAPPED', 'cell_type_groups', 'cell_type_labels', 'group_summary',
           'module_scope', 'scope_cell_ids', 'scope_from_labels']
