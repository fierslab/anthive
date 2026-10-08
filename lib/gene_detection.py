"""How well a gene is detected in a cell type: the low-detection pill of the facet views.

A facet effect on a gene that few cells express is fragile (dropout, ambient RNA, cross-species read
misassignment in xenografts), so the views flag it. The number comes from the same per-dataset gene statistics as
the expression panel (``gene_stats_index.parquet``, ``pct_nonzero`` = percent of cells with a count): for one facet
scope and cell type group, the median over the datasets whose whole content is that cell type (the ``cell_type`` of
the ``.h5ad.mus`` maps to the group; mixed datasets are not summarised). Secondary datasets (reanalyses) are left
out, as are datasets below ``MIN_CELLS`` cells. Scopes are never mixed.

``LOW_DETECTION_PCT`` is 5: a gene detected in fewer than 5 percent of the cells of the cell type is flagged.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import pandas as pd

from celltype_groups import UNMAPPED, group_of
from modality import modality_of_metadata

#: Percent of cells expressing the gene below which the pill shows.
LOW_DETECTION_PCT = 5.0
#: Datasets with fewer cells are not summarised (a median over a handful of cells says nothing).
MIN_CELLS = 50
#: Scopes whose datasets are summarised.
SUMMARY_SCOPES = ('human_postmortem', 'xenograft_human_in_mouse', 'in_vitro')
NOT_SUMMARISED = 'mixed / other'
#: Expression statistics copied from the gene statistics into every dataset entry (the expanded rows of the panel).
EXPRESSION_COLUMNS = ('mean', 'median_nonzero', 'q10_nonzero', 'q90_nonzero', 'rank_mean')


def dataset_group(cell_type: Any) -> Optional[str]:
    """Cell type group of a dataset whose whole content is one cell type; None for mixed datasets.

    Args:
        cell_type: The ``.h5ad.mus`` ``cell_type`` (a label, ``mixed`` or a list of labels).
    """
    if cell_type is None or isinstance(cell_type, (list, tuple, set)):
        return None
    group = group_of(cell_type)
    return None if group == UNMAPPED else group


def candidate_datasets(metadata: Mapping[str, Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """The datasets that feed the detection numbers, keyed by db_id.

    Each entry has ``scope``, ``group`` (None for a mixed dataset), ``n_cells``, ``title``, ``modality`` (``nuclei`` /
    ``cells`` / ``unknown``), ``modality_note`` and ``cell_type``. Secondary datasets and datasets in no facet
    scope are not candidates.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for db_id, meta in metadata.items():
        scope = meta.get('facet_scope')
        if scope not in SUMMARY_SCOPES or meta.get('is_secondary') is True:
            continue
        tag = modality_of_metadata(meta)
        out[db_id] = {'scope': scope, 'group': dataset_group(meta.get('cell_type')), 'n_cells': meta.get('n_cells') or 0,
                      'title': meta.get('title') or db_id, 'cell_type': meta.get('cell_type'),
                      'modality': tag['modality'], 'modality_note': tag['note'],
                      'technology': meta.get('technology')}
    return out


def _round(value: Any, digits: int = 1) -> Optional[float]:
    return None if value is None or pd.isna(value) else round(float(value), digits)


def summarise(entries: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Per (scope, group) summary of per-dataset detection entries.

    Args:
        entries: Dicts with ``scope``, ``group``, ``pct`` (percent of cells with a count) and ``n_cells``.

    Returns:
        One dict per scope and group, groups with one cell type only: ``scope``, ``celltype_group``,
        ``n_datasets``, ``pct_median``, ``pct_min``, ``pct_max``, ``n_cells`` and ``low_detection`` (median below
        ``LOW_DETECTION_PCT``), sorted by scope then datasets, most first.
    """
    frame = pd.DataFrame([e for e in entries if e.get('group') and e.get('pct') is not None
                          and (e.get('n_cells') or 0) >= MIN_CELLS])
    if frame.empty:
        return []
    rows = []
    for (scope, group), sub in frame.groupby(['scope', 'group'], sort=False):
        median = float(sub['pct'].median())
        rows.append({'scope': scope, 'celltype_group': group, 'n_datasets': int(len(sub)),
                     'pct_median': _round(median), 'pct_min': _round(sub['pct'].min()),
                     'pct_max': _round(sub['pct'].max()), 'n_cells': int(sub['n_cells'].sum()),
                     'low_detection': median < LOW_DETECTION_PCT})
    rows.sort(key=lambda r: (SUMMARY_SCOPES.index(r['scope']), -r['n_datasets'], r['celltype_group']))
    return rows


def gene_detection(gene: str, candidates: Mapping[str, Mapping[str, Any]], lookup: Any) -> Dict[str, Any]:
    """Detection of one gene across the candidate datasets.

    Args:
        gene: Gene symbol (any case).
        candidates: ``candidate_datasets`` output.
        lookup: ``lookup(db_id, gene) -> stats dict | None``; the stats dict carries ``pct_nonzero`` and
            ``n_cells``. Datasets for which it returns None are counted in ``n_missing``, not guessed.

    Returns:
        ``{gene, low_detection_pct, min_cells, n_candidates, n_missing, summary, datasets}``. ``datasets`` lists
        every dataset with a value (also mixed ones, ``celltype_group`` ``mixed / other``) with ``db_id``,
        ``title``, ``scope``, ``celltype_group``, ``pct``, ``n_cells``, ``modality``, ``modality_note`` and the
        expression statistics ``mean``, ``median_nonzero``, ``q10_nonzero``, ``q90_nonzero``, ``rank_mean`` (null when absent);
        ``summary`` is ``summarise`` of them.
    """
    datasets: List[Dict[str, Any]] = []
    missing = 0
    for db_id, info in candidates.items():
        stats = lookup(db_id, gene)
        if not stats or stats.get('pct_nonzero') is None:
            missing += 1
            continue
        datasets.append({'db_id': db_id, 'title': info['title'], 'scope': info['scope'],
                         'celltype_group': info['group'] or NOT_SUMMARISED, 'group': info['group'],
                         'cell_type': info['cell_type'] if not isinstance(info['cell_type'], (list, tuple)) else 'mixed',
                         'n_cells': int(stats.get('n_cells') or info['n_cells'] or 0),
                         'pct': _round(stats['pct_nonzero'], 2), 'modality': info['modality'],
                         'modality_note': info['modality_note'], 'technology': info.get('technology'),
                         **{column: _round(stats.get(column), 3) for column in EXPRESSION_COLUMNS}})
    summary = summarise(datasets)
    for entry in datasets:
        entry.pop('group')
    datasets.sort(key=lambda e: (e['scope'], e['celltype_group'], -(e['pct'] or 0)))
    return {'gene': gene, 'low_detection_pct': LOW_DETECTION_PCT, 'min_cells': MIN_CELLS,
            'n_candidates': len(candidates), 'n_missing': missing, 'summary': summary, 'datasets': datasets}


def detection_table(frame: pd.DataFrame, candidates: Mapping[str, Mapping[str, Any]],
                    fresh: Any = None) -> Dict[Tuple[str, str], pd.DataFrame]:
    """Detection of every gene in the index, per (scope, cell type group), vectorised.

    Args:
        frame: The gene statistics index (``dataset``, ``gene``, ``n_cells``, ``pct_nonzero``).
        candidates: ``candidate_datasets`` output.
        fresh: Optional ``fresh(db_id) -> bool``; datasets whose precomputed rows are stale are left out.

    Returns:
        ``(scope, group) -> DataFrame`` indexed by lower-case gene symbol with ``pct_median`` and ``n_datasets``.
        Only datasets whose content is a single cell type (``group`` not None, at least ``MIN_CELLS`` cells).
    """
    usable = {db_id: info for db_id, info in candidates.items()
              if info['group'] and info['n_cells'] >= MIN_CELLS and (fresh is None or fresh(db_id))}
    if frame is None or not usable:
        return {}
    sub = frame.loc[frame['dataset'].isin(usable), ['dataset', 'gene', 'pct_nonzero']].copy()
    sub['scope'] = sub['dataset'].map(lambda db_id: usable[db_id]['scope'])
    sub['group'] = sub['dataset'].map(lambda db_id: usable[db_id]['group'])
    sub['gene_key'] = sub['gene'].str.lower()
    tables: Dict[Tuple[str, str], pd.DataFrame] = {}
    for (scope, group), part in sub.groupby(['scope', 'group'], sort=False):
        grouped = part.groupby('gene_key')['pct_nonzero'].agg(['median', 'size'])
        tables[(scope, group)] = grouped.rename(columns={'median': 'pct_median', 'size': 'n_datasets'})
    return tables


def pill(table: Optional[pd.DataFrame], gene: str) -> Optional[Dict[str, Any]]:
    """The detection pill of one gene from a ``detection_table`` entry: ``{pct_median, n_datasets, low_detection}``,
    None when the gene has no value (not in the index for that scope and group)."""
    if table is None:
        return None
    key = gene.lower()
    if key not in table.index:
        return None
    row = table.loc[key]
    median = float(row['pct_median'])
    return {'pct_median': round(median, 1), 'n_datasets': int(row['n_datasets']),
            'low_detection': median < LOW_DETECTION_PCT}
