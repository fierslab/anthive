"""Read-only view of the donor variability results of ``bin/annot/ant-donor-variability``.

Files per dataset under ``<facet results dir>/<group>/``: ``<stem>.donor_variability.parquet`` (genes),
``<stem>.donor_variability_modules.parquet`` (modules) and ``<stem>.donor_variability_manifest.json``. This module turns
them into the JSON the API, the MCP tools and the web views serve. It composes over ``FacetResults`` (shared defensive,
mtime keyed reads and the cohort table) and never writes.

Reading rules (the help topic ``donor-variability`` says the same in plain words)
---------------------------------------------------------------------------------
* A replicate is what the dataset's ``replicate_unit`` says (donor, or animal for a xenograft); scopes are never mixed.
* ``v_donor`` is the between-replicate variance of the log2 expression with the sampling noise of the cells removed;
  ``sd_donor`` its square root (log2 units); ``reliability`` the share of what a replicate-level measurement shows that
  is stable replicate difference. Technical differences between replicates are part of it: read ``v_unexplained`` at a
  rung that includes the technical block (D0 or deeper) for what is left after what we know.
* ``excess_z`` ranks genes inside one unit against the mean-variance trend; ``p_excess`` is that rank as a relative
  upper tail. Neither is a test. The cross-cohort statement asks whether the ranks of a gene agree across the cohort
  anchors (units with at least 30 replicates that reach the chosen rung): Fisher's combination of the rank p values of
  all cohorts that ranked the gene (a rank product), BH over genes. A rank p value is uniform over genes by construction
  and cannot be smaller than 1 / (number of genes), so the null is "ranks in different cohorts are independent"; a
  partial conjunction (as for the facet tests) could never call a gene, because its p value stays above the best rank.
  Exploratory; cohorts that share donors or states are not independent.
* Modules have no mean-variance trend and no ``excess_*``: the cross-cohort list is descriptive.
"""
from __future__ import annotations

import logging
import math
import threading
from collections import OrderedDict
from itertools import combinations
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from scipy.stats import chi2, spearmanr

import donor_variance
import facet_scopes
from facet_results import _file_key
import replication
from celltype_groups import UNMAPPED, group_of, groups as known_groups

log = logging.getLogger(__name__)

SUFFIX_GENES = '.donor_variability.parquet'
SUFFIX_MODULES = '.donor_variability_modules.parquet'
SUFFIX_MANIFEST = '.donor_variability_manifest.json'
FEATURE_TYPES = ('gene', 'module')
RUNGS = donor_variance.EXCESS_RUNGS                         # raw, D0 .. D4, F
DEFAULT_RUNG = 'D1'
RUNG_LABELS = {
    'raw': 'all replicate differences (nothing removed)',
    'D0': 'after the technical block (cells, UMI per cell, genes per cell, mitochondrial reads, ambient RNA, long genes, composition)',
    'D1': 'after technical block, sex, age, PMI and batch',
    'D2': 'after technical block, covariates and disease',
    'D3': 'after technical block, covariates, disease and APOE e4',
    'D4': 'after technical block, covariates, disease, APOE e4 and Braak',
    'F': 'after everything known and 3 to 5 latent factors',
}
TERMS = donor_variance.TERM_ORDER
#: Top lists and cohort cards count a cohort only with this many replicates.
MIN_COHORT_REPLICATES = donor_variance.CROSS_COHORT_MIN_REPLICATES
#: A gene is "high" in a cohort when it is in the top ``HIGH_TAIL`` of its excess ranking.
HIGH_TAIL = 0.05
HELP_TOPICS = ('donor-variability', 'facet-replicate-unit', 'donor-groups')
SORTS = ('excess', 'sd', 'reliability', 'feature')
_SOURCE_PRIORITY_FALLBACK = 3


class DonorVariabilityError(ValueError):
    """A bad query parameter (maps to HTTP 400)."""


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value: Any, digits: int = 4) -> Optional[float]:
    number = _finite(value)
    return None if number is None else round(number, digits)


def _sig(value: Any, digits: int = 4) -> Optional[float]:
    """``value`` rounded to ``digits`` significant digits (p values span many orders of magnitude)."""
    number = _finite(value)
    return None if number is None else float(f'{number:.{digits}g}')


def fold_range(sd_log2: Any) -> Optional[float]:
    """Typical two-SD fold range of a log2 SD: ``2 ** (2 sd)`` (donors about 2 SD apart differ by this factor)."""
    number = _finite(sd_log2)
    return None if number is None else round(2.0 ** (2.0 * number), 3)


def rank_product_p(p_matrix: np.ndarray) -> np.ndarray:
    """Fisher combination of the rank p values of the cohorts that rank a gene (a rank product); NaN below two cohorts.

    Args:
        p_matrix: (genes, cohorts) rank based upper tail p values, NaN where a cohort did not rank the gene.

    Returns:
        (genes,) p value of ``-2 sum(log p)`` on ``2 m`` degrees of freedom, m = cohorts with a value (>= 2).
    """
    matrix = np.asarray(p_matrix, dtype=float)
    n_values = np.isfinite(matrix).sum(axis=1)
    statistic = -2.0 * np.nansum(np.log(np.clip(np.where(np.isfinite(matrix), matrix, 1.0), 1e-300, 1.0)), axis=1)
    p_values = chi2.sf(statistic, 2 * np.maximum(n_values, 1))
    return np.where(n_values >= 2, p_values, np.nan)


def check_rung(rung: Optional[str]) -> str:
    """Validate a ``rung`` query value (default ``D1``).

    Raises:
        DonorVariabilityError: not one of ``RUNGS``.
    """
    if not rung:
        return DEFAULT_RUNG
    if rung not in RUNGS:
        raise DonorVariabilityError(f'rung must be one of {", ".join(RUNGS)}')
    return rung


def check_feature_type(feature_type: Optional[str]) -> str:
    """Validate ``feature_type`` (``gene`` or ``module``; default gene)."""
    if not feature_type:
        return 'gene'
    if feature_type not in FEATURE_TYPES:
        raise DonorVariabilityError(f'feature_type must be one of {", ".join(FEATURE_TYPES)}')
    return feature_type


def _source_priority(celltype_source: Any) -> int:
    """Lower is a more direct stratum (0 dataset constant, 1 group pool, 2 per-cell type, 3 author label, 4 clusters)."""
    from facet_results import source_priority  # noqa: PLC0415
    try:
        return int(source_priority(celltype_source))
    except Exception:  # noqa: BLE001
        return _SOURCE_PRIORITY_FALLBACK


def rung_record(row: Dict[str, Any], rung: str, unit_rungs: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The numbers of one rung of one feature row (None when the rung was not computed for the unit)."""
    info = unit_rungs.get(rung)
    if rung == 'raw':
        sd = _finite(row.get('sd_donor'))
        return {'n': None, 'v': _round(row.get('v_donor'), 5), 'sd': _round(sd), 'fold_range': fold_range(sd),
                'share_design': None, 'excess_z': _round(row.get('excess_z_raw'), 3),
                'p_excess': _round(row.get('p_excess_raw'), 5)}
    if info is None or not info.get('computed'):
        return None
    v_value = _finite(row.get(f'v_unexplained_{rung}'))
    sd = None if v_value is None else math.sqrt(max(v_value, 0.0))
    return {'n': info.get('n'), 'p': info.get('p'), 'terms': info.get('terms'), 'v': _round(v_value, 5),
            'sd': _round(sd), 'fold_range': fold_range(sd), 'share_design': _round(row.get(f'share_design_{rung}'), 3),
            'excess_z': _round(row.get(f'excess_z_{rung}'), 3), 'p_excess': _round(row.get(f'p_excess_{rung}'), 5)}


def feature_record(row: Dict[str, Any], unit: Dict[str, Any]) -> Dict[str, Any]:
    """One feature (gene or module) of one unit as JSON.

    Args:
        row: A parquet row as dict.
        unit: The unit record of the manifest (``rungs``, ``rung_deepest``, ``lmg_rung``).

    Returns:
        ``{n_replicates, reliability, reliability_ci, v_donor, v_donor_ci, sd_donor, fold_range, rungs: {rung: ...},
        deepest, share_blocks, lmg, ...}`` (see the API changelog for the field list).
    """
    unit_rungs = {info['name']: info for info in unit.get('rungs') or []}
    deepest = unit.get('rung_deepest') or None
    sd = _finite(row.get('sd_donor'))
    record: Dict[str, Any] = {
        'n_replicates': _finite(row.get('n_replicates')) and int(row['n_replicates']),
        'reliability': _round(row.get('reliability'), 3),
        'reliability_ci': [_round(row.get('reliability_lo'), 3), _round(row.get('reliability_hi'), 3)],
        'r_half': _round(row.get('r_half'), 3),
        'v_donor': _round(row.get('v_donor'), 5),
        'v_donor_ci': [_round(row.get('v_donor_lo'), 5), _round(row.get('v_donor_hi'), 5)],
        'sd_donor': _round(sd), 'fold_range_donor': fold_range(sd),
        'var_half': _round(row.get('var_half'), 5),
        'rungs': {rung: rung_record(row, rung, unit_rungs) for rung in RUNGS},
        'deepest_rung': deepest,
        'share_design': _round(row.get(f'share_design_{deepest}'), 3) if deepest else None,
        'share_design_ci': [_round(row.get('share_design_lo'), 3), _round(row.get('share_design_hi'), 3)] if deepest else None,
        'share_blocks': {name: _round(row.get(f'share_{name}'), 3)
                         for name in ('technical', 'covariates', 'facets', 'latent') if f'share_{name}' in row},
        'lmg': {term: _round(row.get(f'lmg_{term}'), 3) for term in TERMS if f'lmg_{term}' in row} or None,
        'lmg_rung': unit.get('lmg_rung') if any(f'lmg_{term}' in row for term in TERMS) else None,
        'r_composition_pc1': _round(row.get('r_composition_pc1'), 3),
    }
    if 'ave_expr' in row:
        record['ave_expr'] = _round(row.get('ave_expr'), 3)
        record['half_count_q70'] = _round(row.get('half_count_q70'), 1)
    return record


class DonorVariability:
    """Reader of the donor variability files, composed over a ``FacetResults``.

    Args:
        reader: The shared ``FacetResults`` (results folder, parquet and JSON caches, cohort table).
    """

    def __init__(self, reader: Any):
        self.reader = reader
        self.results_dir = Path(reader.results_dir)
        self._lock = threading.Lock()
        self._families: "OrderedDict[Tuple, Tuple[Tuple, pd.DataFrame]]" = OrderedDict()
        self._lookups: "OrderedDict[Tuple, pd.DataFrame]" = OrderedDict()

    # ---- files -------------------------------------------------------------------------------------------------

    def paths(self, group_folder: str, stem: str) -> Dict[str, Path]:
        """Result file paths of one dataset (they may not exist)."""
        folder = self.results_dir / group_folder
        return {'genes': folder / f'{stem}{SUFFIX_GENES}', 'modules': folder / f'{stem}{SUFFIX_MODULES}',
                'manifest': folder / f'{stem}{SUFFIX_MANIFEST}'}

    def manifest(self, group_folder: str, stem: str) -> Optional[Dict[str, Any]]:
        """The run manifest, or None when missing or unreadable."""
        return self.reader._read_json(self.paths(group_folder, stem)['manifest'])

    def available(self, group_folder: str, stem: str) -> bool:
        """True when the dataset has a finished run with at least one gene row."""
        manifest = self.manifest(group_folder, stem)
        return bool(manifest and manifest.get('status') == 'done' and self.paths(group_folder, stem)['genes'].exists())

    def _frame(self, group_folder: str, stem: str, kind: str, columns: Sequence[str]) -> Optional[pd.DataFrame]:
        return self.reader._read_parquet(self.paths(group_folder, stem)[kind], columns)

    def _lookup(self, group_folder: str, stem: str, kind: str, filters: List[Tuple[str, str, Any]]
                ) -> Optional[pd.DataFrame]:
        """Rows of one result file matching equality ``filters`` (row groups are pruned by the sorted gene or module
        column, so a single feature reads about one row group of each file). Cached per file version and filter."""
        path = self.paths(group_folder, stem)[kind]
        key = _file_key(path)
        if key is None:
            return None
        cache_key = (key, repr(filters))
        with self._lock:
            cached = self._lookups.get(cache_key)
            if cached is not None:
                self._lookups.move_to_end(cache_key)
                return cached
        try:
            import pyarrow.parquet as pq  # noqa: PLC0415
            names = set(pq.read_schema(path).names)
            frame = pq.read_table(path, columns=[c for c in ALL_COLUMNS if c in names], filters=filters).to_pandas()
        except Exception as exc:  # noqa: BLE001  partial write, corrupt file, schema change
            log.warning('donor variability: cannot read %s: %s', path, exc)
            return None
        with self._lock:
            self._lookups[cache_key] = frame
            while len(self._lookups) > 512:
                self._lookups.popitem(last=False)
        return frame

    @staticmethod
    def _unit(manifest: Dict[str, Any], cell_type: str, celltype_source: str) -> Optional[Dict[str, Any]]:
        for unit in manifest.get('units') or []:
            if unit.get('cell_type') == cell_type and unit.get('celltype_source') == celltype_source:
                return unit
        return None

    # ---- one dataset -------------------------------------------------------------------------------------------

    def dataset_summary(self, db_id: str, group_folder: str, stem: str, skip_reason: Optional[str] = None
                        ) -> Dict[str, Any]:
        """Summary of the units of one dataset.

        Returns:
            ``{db_id, status (analysed | not_analysed | not_run), reason, facet_scope, replicate_unit, run, parameters,
            units: [{cell_type, celltype_source, celltype_group, n_replicates, status, rung_deepest, rungs, terms,
            terms_omitted, technical_covariates, n_genes, n_modules, median_reliability, median_sd_donor, notes}]}``.
        """
        manifest = self.manifest(group_folder, stem)
        paths = self.paths(group_folder, stem)
        if manifest is None:
            return {'db_id': db_id, 'status': 'not_run', 'reason': skip_reason or 'donor variability has not been run for this dataset',
                    'units': []}
        if manifest.get('status') != 'done':
            return {'db_id': db_id, 'status': 'not_analysed', 'reason': manifest.get('reason') or skip_reason,
                    'facet_scope': manifest.get('facet_scope'), 'units': []}
        genes = self._frame(group_folder, stem, 'genes', ('cell_type', 'celltype_source', 'gene'))
        modules = self._frame(group_folder, stem, 'modules', ('cell_type', 'celltype_source', 'module'))
        n_genes = genes.groupby(['cell_type', 'celltype_source']).size().to_dict() if genes is not None else {}
        n_modules = modules.groupby(['cell_type', 'celltype_source']).size().to_dict() if modules is not None else {}
        units = []
        for unit in manifest.get('units') or []:
            key = (unit.get('cell_type'), unit.get('celltype_source'))
            record = {name: unit.get(name) for name in (
                'cell_type', 'celltype_source', 'celltype_group', 'n_replicates', 'status', 'reason', 'rung_deepest',
                'terms', 'terms_omitted', 'technical_covariates', 'composition_pcs', 'n_factors', 'n_genes_gate',
                'lmg_rung')}
            record['median_cells_per_replicate'] = _round(unit.get('median_cells_per_replicate'), 1)
            record['median_reliability'] = _round(unit.get('median_reliability'), 3)
            record['median_sd_donor'] = _round(math.sqrt(max(unit['median_v_donor'], 0.0))) \
                if _finite(unit.get('median_v_donor')) is not None else None
            record['rungs'] = [{k: info.get(k) for k in ('name', 'terms', 'n', 'p', 'residual_df', 'computed', 'reason')}
                               for info in unit.get('rungs') or []]
            record['n_genes'] = int(n_genes.get(key, 0))
            record['n_modules'] = int(n_modules.get(key, 0))
            record['counted_in_cross_cohort'] = int(unit.get('n_replicates') or 0) >= MIN_COHORT_REPLICATES
            units.append(record)
        return {'db_id': db_id, 'status': 'analysed', 'reason': None, 'facet_scope': manifest.get('facet_scope'),
                'replicate_unit': manifest.get('replicate_unit'), 'replicate_column': manifest.get('replicate_column'),
                'files': {'genes': paths['genes'].exists(), 'modules': paths['modules'].exists()},
                'run': {key: manifest.get(key) for key in ('tool', 'timestamp_utc', 'seed', 'versions', 'md_sha256',
                                                            'anthive4_git', 'h5adstore_git', 'counts_layer', 'batch_column',
                                                            'n_replicates_all', 'n_strata_analysed', 'runtime_seconds')},
                'parameters': manifest.get('parameters'), 'units': units}

    def dataset_rows(self, group_folder: str, stem: str, feature_type: str, cell_type: str,
                     celltype_source: Optional[str] = None, feature: Optional[str] = None, method: Optional[str] = None,
                     sort: str = 'excess', rung: str = DEFAULT_RUNG, limit: int = 100, min_reliability: float = 0.0
                     ) -> Dict[str, Any]:
        """Feature rows of one unit of one dataset, sorted and filtered.

        Args:
            feature_type: ``gene`` or ``module``.
            cell_type: Stratum label (exactly as in the summary).
            celltype_source: Stratifier; the first unit with the label when omitted.
            feature: A gene symbol (case insensitive), or ``Catalog/module`` for a module.
            method: Modules only: ``pseudobulk`` (primary) or ``cell_mean``.
            sort: ``excess`` (excess_z at ``rung``, descending), ``sd`` (SD of the unexplained variance at ``rung``),
                ``reliability`` or ``feature``.
            rung: Rung of the sort and of ``excess``.
            min_reliability: Keep rows with reliability at least this.

        Returns:
            ``{unit, rung, n_matching, n_returned, rows}``; ``unit`` None when the unit does not exist.
        """
        feature_type = check_feature_type(feature_type)
        rung = check_rung(rung)
        if sort not in SORTS:
            raise DonorVariabilityError(f'sort must be one of {", ".join(SORTS)}')
        manifest = self.manifest(group_folder, stem)
        if not manifest:
            return {'unit': None, 'rung': rung, 'n_matching': 0, 'n_returned': 0, 'rows': []}
        frame = self._frame(group_folder, stem, 'genes' if feature_type == 'gene' else 'modules', ALL_COLUMNS)
        if frame is None:
            return {'unit': None, 'rung': rung, 'n_matching': 0, 'n_returned': 0, 'rows': []}
        frame = frame[frame['cell_type'] == cell_type]
        if celltype_source:
            frame = frame[frame['celltype_source'] == celltype_source]
        elif len(frame):
            frame = frame[frame['celltype_source'] == frame['celltype_source'].iloc[0]]
        unit = self._unit(manifest, cell_type, str(frame['celltype_source'].iloc[0])) if len(frame) else None
        if unit is None:
            return {'unit': None, 'rung': rung, 'n_matching': 0, 'n_returned': 0, 'rows': []}
        if feature_type == 'module':
            if method:
                frame = frame[frame['method'] == method]
            if feature:
                catalog, _, name = feature.rpartition('/')
                frame = frame[(frame['catalog'] == catalog) & (frame['module'] == name)]
        elif feature:
            frame = frame[frame['gene'].str.upper() == feature.upper()]
        if min_reliability > 0:
            frame = frame[frame['reliability'] >= min_reliability]
        key = {'excess': f'excess_z_{rung}', 'sd': 'sd_donor' if rung == 'raw' else f'v_unexplained_{rung}',
               'reliability': 'reliability'}.get(sort)
        if key and key in frame:
            frame = frame.sort_values(key, ascending=False, na_position='last', kind='mergesort')
        elif sort == 'feature':
            frame = frame.sort_values('gene' if feature_type == 'gene' else ['catalog', 'module'])
        n_matching = len(frame)
        rows = []
        for row in frame.head(limit).to_dict(orient='records'):
            record = feature_record(row, unit)
            if feature_type == 'gene':
                record = {'gene': row['gene'], **record}
            else:
                record = {'module': f"{row['catalog']}/{row['module']}", 'method': row['method'], 'source': row['source'],
                          'n_genes_found': _finite(row.get('n_genes_found')) and int(row['n_genes_found']), **record}
            rows.append(record)
        return {'unit': {key_: unit.get(key_) for key_ in ('cell_type', 'celltype_source', 'celltype_group', 'n_replicates',
                                                           'rung_deepest', 'terms')},
                'rung': rung, 'n_matching': n_matching, 'n_returned': len(rows), 'rows': rows}

    # ---- anchors and cohorts -----------------------------------------------------------------------------------

    def units(self, datasets: Sequence[Dict[str, Any]], scope: str, exclude_secondary: bool = True
              ) -> List[Dict[str, Any]]:
        """Every analysed unit of the datasets in ``scope`` with its cohort and whether it is a cohort anchor.

        A unit counts as an anchor when its dataset is in the cohort table as a counted cohort, is not a secondary,
        has a mapped cell type group and at least ``MIN_COHORT_REPLICATES`` replicates; per (cohort, group) the unit
        with the most direct stratifier, then the most replicates, wins. ``rung_reached`` lists the rungs it computed.
        """
        cohorts = self.reader.cohorts()
        rows: List[Dict[str, Any]] = []
        for dataset in datasets:
            manifest = self.manifest(dataset['group_folder'], dataset['stem'])
            if not manifest or manifest.get('status') != 'done':
                continue
            if (manifest.get('facet_scope') or dataset.get('facet_scope') or facet_scopes.DEFAULT_SCOPE) != scope:
                continue
            cohort = cohorts.get(dataset['db_id']) or {}
            for unit in manifest.get('units') or []:
                if unit.get('status') != 'ok':
                    continue
                reached = ['raw'] + [info['name'] for info in unit.get('rungs') or [] if info.get('computed')]
                reason = None
                if not cohort.get('counted'):
                    reason = 'dataset is not a counted cohort'
                elif exclude_secondary and dataset.get('is_secondary'):
                    reason = 'secondary dataset'
                elif unit.get('celltype_group') in (None, UNMAPPED):
                    reason = 'cell type group not mapped'
                elif int(unit.get('n_replicates') or 0) < MIN_COHORT_REPLICATES:
                    reason = f'fewer than {MIN_COHORT_REPLICATES} replicates'
                rows.append({
                    'db_id': dataset['db_id'], 'title': dataset.get('title'), 'group_folder': dataset['group_folder'],
                    'stem': dataset['stem'], 'cohort': cohort.get('cohort'), 'cohort_label': cohort.get('cohort_label'),
                    'cell_type': unit['cell_type'], 'celltype_source': unit['celltype_source'],
                    'celltype_group': unit.get('celltype_group'), 'n_replicates': int(unit.get('n_replicates') or 0),
                    'replicate_unit': manifest.get('replicate_unit'), 'rung_deepest': unit.get('rung_deepest'),
                    'rung_reached': reached, 'not_counted_reason': reason, 'counted': reason is None,
                    'lmg_rung': unit.get('lmg_rung'), '_unit': unit})
        frame = pd.DataFrame(rows)
        if frame.empty:
            return rows
        frame['_priority'] = frame['celltype_source'].map(_source_priority)
        candidates = frame[frame['counted']].sort_values(['cohort', 'celltype_group', '_priority', 'n_replicates', 'db_id'],
                                                         ascending=[True, True, True, False, True])
        winners = candidates.drop_duplicates(['cohort', 'celltype_group'], keep='first').index
        for position, record in enumerate(rows):
            if record['counted'] and position not in winners:
                record['counted'] = False
                record['not_counted_reason'] = 'another unit is the anchor of this cohort and cell type group'
        return rows

    # ---- cross cohort ------------------------------------------------------------------------------------------

    def _family_key_signature(self, anchors: Sequence[Dict[str, Any]]) -> Tuple:
        signature = []
        for anchor in anchors:
            for kind in ('genes', 'modules'):
                path = self.paths(anchor['group_folder'], anchor['stem'])[kind]
                try:
                    stat = path.stat()
                    signature.append((str(path), stat.st_mtime_ns, stat.st_size))
                except OSError:
                    signature.append((str(path), 0, 0))
        return tuple(signature)

    def family(self, anchors: Sequence[Dict[str, Any]], feature_type: str, rung: str) -> pd.DataFrame:
        """Cross-cohort table of one (scope, cell type group, feature type, rung) from the cohort anchors.

        Genes: columns ``gene``, ``m`` (anchors that rank the gene), ``k`` (anchors with ``p_excess <= 0.05``),
        ``p_rank_product`` (Fisher over the ``p_excess`` of the ``m >= 2`` anchors that rank the gene), ``r_value`` (BH over
        genes), ``median_excess_z``, ``median_reliability``, ``median_sd``. Modules: ``module``, ``method``, ``source``,
        ``m``, ``k`` (anchors with reliability >= 0.3), ``median_sd``, ``median_reliability``, ``median_share_design``;
        no p values.
        """
        signature = self._family_key_signature(anchors)
        key = (feature_type, rung, tuple((a['db_id'], a['cell_type'], a['celltype_source']) for a in anchors))
        with self._lock:
            cached = self._families.get(key)
            if cached is not None and cached[0] == signature:
                self._families.move_to_end(key)
                return cached[1]
        if feature_type == 'gene':
            table = self._gene_family(anchors, rung)
        else:
            table = self._module_family(anchors, rung)
        with self._lock:
            self._families[key] = (signature, table)
            self._families.move_to_end(key)
            while len(self._families) > 32:
                self._families.popitem(last=False)
        return table

    def _anchor_frame(self, anchor: Dict[str, Any], kind: str, columns: Sequence[str]) -> Optional[pd.DataFrame]:
        frame = self._frame(anchor['group_folder'], anchor['stem'], kind, columns)
        if frame is None:
            return None
        return frame[(frame['cell_type'] == anchor['cell_type']) & (frame['celltype_source'] == anchor['celltype_source'])]

    def _gene_family(self, anchors: Sequence[Dict[str, Any]], rung: str) -> pd.DataFrame:
        columns = ['cell_type', 'celltype_source', 'gene', 'reliability', f'p_excess_{rung}', f'excess_z_{rung}',
                   'sd_donor', f'v_unexplained_{rung}']
        parts = []
        for index, anchor in enumerate(anchors):
            frame = self._anchor_frame(anchor, 'genes', columns)
            if frame is None or f'p_excess_{rung}' not in frame:
                continue
            frame = frame.dropna(subset=[f'p_excess_{rung}']).assign(cohort_index=index)
            parts.append(frame)
        if not parts:
            return pd.DataFrame(columns=['gene', 'm', 'k', 'p_rank_product', 'r_value'])
        long = pd.concat(parts, ignore_index=True)
        p_matrix = long.pivot_table(index='gene', columns='cohort_index', values=f'p_excess_{rung}', aggfunc='first')
        p_rank_product = rank_product_p(p_matrix.to_numpy())
        grouped = long.groupby('gene')
        table = pd.DataFrame({
            'm': grouped.size(), 'k': grouped[f'p_excess_{rung}'].apply(lambda s: int((s <= HIGH_TAIL).sum())),
            'median_excess_z': grouped[f'excess_z_{rung}'].median(), 'median_reliability': grouped['reliability'].median(),
            'median_sd': grouped[f'v_unexplained_{rung}'].apply(lambda s: float(np.sqrt(max(s.median(), 0.0))))
            if rung != 'raw' else grouped['sd_donor'].median()})
        table['p_rank_product'] = pd.Series(p_rank_product, index=p_matrix.index).reindex(table.index)
        table['r_value'] = replication.benjamini_hochberg(table['p_rank_product'].to_numpy())
        return table.reset_index()

    def _module_family(self, anchors: Sequence[Dict[str, Any]], rung: str) -> pd.DataFrame:
        columns = ['cell_type', 'celltype_source', 'method', 'source', 'catalog', 'module', 'reliability', 'sd_donor',
                   f'v_unexplained_{rung}', f'share_design_{rung}']
        parts = []
        for anchor in anchors:
            frame = self._anchor_frame(anchor, 'modules', columns)
            if frame is not None and len(frame) and f'v_unexplained_{rung}' in frame:
                parts.append(frame)
        if not parts:
            return pd.DataFrame(columns=['module', 'method', 'source', 'm', 'k'])
        long = pd.concat(parts, ignore_index=True)
        long['module_id'] = long['catalog'] + '/' + long['module']
        grouped = long.groupby(['module_id', 'method', 'source'])
        table = pd.DataFrame({
            'm': grouped.size(), 'k': grouped['reliability'].apply(lambda s: int((s >= donor_variance.RELIABILITY_FLOOR).sum())),
            'median_reliability': grouped['reliability'].median(),
            'median_sd': grouped[f'v_unexplained_{rung}'].apply(lambda s: float(np.sqrt(max(s.median(), 0.0)))),
            'median_share_design': grouped[f'share_design_{rung}'].median()}).reset_index()
        return table.rename(columns={'module_id': 'module'})

    def concordance(self, anchors: Sequence[Dict[str, Any]], rung: str) -> Dict[str, Any]:
        """Mean pairwise Spearman correlation of ``excess_z`` between the anchors over the genes both rank.

        The summary of whether the donor-level excess of genes reproduces across cohorts at all (0 means no
        reproducibility). ``{mean_spearman, n_pairs, pairs: [{cohorts, n_genes, spearman}]}``; None values without a pair.
        """
        columns = ['cell_type', 'celltype_source', 'gene', f'excess_z_{rung}']
        series = []
        for anchor in anchors:
            frame = self._anchor_frame(anchor, 'genes', columns)
            if frame is not None and f'excess_z_{rung}' in frame:
                series.append((anchor['cohort_label'] or anchor['db_id'],
                               frame.dropna(subset=[f'excess_z_{rung}']).set_index('gene')[f'excess_z_{rung}']))
        pairs = []
        for (name_a, values_a), (name_b, values_b) in combinations(series, 2):
            shared = values_a.index.intersection(values_b.index)
            if len(shared) >= 30:
                rho = float(spearmanr(values_a.loc[shared], values_b.loc[shared])[0])
                pairs.append({'cohorts': [name_a, name_b], 'n_genes': int(len(shared)), 'spearman': _round(rho, 3)})
        mean = float(np.mean([pair['spearman'] for pair in pairs])) if pairs else None
        return {'mean_spearman': _round(mean, 3), 'n_pairs': len(pairs), 'pairs': pairs}

    def top(self, datasets: Sequence[Dict[str, Any]], scope: str, cell_type: str, feature_type: str = 'gene',
            rung: str = DEFAULT_RUNG, limit: int = 50, min_cohorts: int = 2, exclude_secondary: bool = True
            ) -> Dict[str, Any]:
        """Cross-cohort list: features that are highly variable between replicates in several cohorts.

        Args:
            datasets: Served datasets (``db_id``, ``group_folder``, ``stem``, ``title``, ``is_secondary``).
            scope: Facet scope (``human_postmortem`` or ``xenograft_human_in_mouse``); scopes are never mixed.
            cell_type: Cell type group name, or a raw label (resolved to its group).
            feature_type: ``gene`` or ``module``.
            rung: Rung at which the unexplained variance is ranked (``raw`` for all replicate differences).
            limit: Rows returned.
            min_cohorts: Features need a result in at least this many cohorts (genes: also the partial conjunction ``u``).

        Returns:
            ``{scope, cell_type_group, feature_type, rung, rung_label, cohorts: [...], m_cohorts, concordance, rows,
            note}``; ``concordance`` is the mean Spearman correlation of ``excess_z`` between the anchors over their shared
            genes (``mean_spearman``, ``n_pairs``; genes only); gene rows ``{gene, k, m, p_rank_product, r_value,
            median_excess_z, median_reliability, median_sd, fold_range}`` sorted by ``r_value`` then ``median_excess_z``.
        """
        feature_type = check_feature_type(feature_type)
        rung = check_rung(rung)
        scope = facet_scopes.check_scope(scope)
        group = cell_type if cell_type in known_groups() else group_of(cell_type)
        units = self.units(datasets, scope, exclude_secondary)
        anchors = [u for u in units if u['counted'] and u['celltype_group'] == group and rung in u['rung_reached']]
        cohorts = [{'db_id': a['db_id'], 'cohort': a['cohort_label'], 'title': a['title'], 'cell_type': a['cell_type'],
                    'celltype_source': a['celltype_source'], 'n_replicates': a['n_replicates'],
                    'replicate_unit': a['replicate_unit'], 'rung_deepest': a['rung_deepest']} for a in anchors]
        out: Dict[str, Any] = {
            'scope': scope, 'cell_type_group': group, 'feature_type': feature_type, 'rung': rung,
            'rung_label': RUNG_LABELS[rung], 'cohorts': cohorts, 'm_cohorts': len(anchors), 'concordance': None,
            'min_replicates_per_cohort': MIN_COHORT_REPLICATES, 'high_tail': HIGH_TAIL,
            'not_enough_cohorts': len(anchors) < max(min_cohorts, 1), 'rows': [],
            'note': 'exploratory ranking inside each cohort, not a test; technical differences between replicates are only '
                    'removed at rungs D0 and deeper' if feature_type == 'gene' else
                    'descriptive: modules have no mean-variance trend, so there is no ranking test'}
        if len(anchors) < max(min_cohorts, 1):
            return out
        table = self.family(anchors, feature_type, rung)
        if feature_type == 'gene':
            out['concordance'] = self.concordance(anchors, rung)
        if table.empty:
            return out
        table = table[table['m'] >= max(min_cohorts, 1)]
        if feature_type == 'gene':
            table = table.sort_values(['r_value', 'median_excess_z'], ascending=[True, False], na_position='last')
        else:
            table = table.sort_values('median_sd', ascending=False)
        for row in table.head(limit).to_dict(orient='records'):
            record = {key: (None if isinstance(value, float) and not math.isfinite(value) else value)
                      for key, value in row.items()}
            for key in ('p_rank_product', 'r_value', 'median_excess_z', 'median_reliability', 'median_sd',
                        'median_share_design'):
                if key in record:
                    record[key] = _sig(record[key]) if key in ('p_rank_product', 'r_value') else _round(record[key], 3)
            record['fold_range'] = fold_range(row.get('median_sd'))
            out['rows'].append(record)
        return out

    # ---- one feature across datasets ---------------------------------------------------------------------------

    def feature_card(self, datasets: Sequence[Dict[str, Any]], scope: str, feature_type: str, name: str,
                     cell_type: Optional[str] = None, rung: str = DEFAULT_RUNG, method: Optional[str] = None,
                     source: Optional[str] = None, exclude_secondary: bool = True) -> Dict[str, Any]:
        """Every unit that has the gene or module, with the cross-cohort summary per cell type group.

        Args:
            name: Gene symbol, or ``Catalog/module`` (a module id may contain a slash: everything before the last one
                is the catalog).
            cell_type: Restrict to a cell type group or raw label.
            rung: Rung of the cross-cohort summary.
            method, source: Modules: variant (default the primary ``pseudobulk`` variant).

        Returns:
            ``{feature, feature_type, scope, rung, units: [...], by_group: [...]}`` where each unit carries the cohort,
            ``counted`` (cohort anchor), ``not_counted_reason`` and ``values`` (see ``feature_record``); ``by_group``
            has ``m`` (anchors), ``k`` (anchors in the top 5 percent of their ranking, genes) and the family row
            (``p_rank_product``, ``r_value``) of the gene.
        """
        feature_type = check_feature_type(feature_type)
        rung = check_rung(rung)
        scope = facet_scopes.check_scope(scope)
        group_filter = None
        if cell_type:
            group_filter = cell_type if cell_type in known_groups() else group_of(cell_type)
        units = self.units(datasets, scope, exclude_secondary)
        gene_spellings = sorted({name, name.upper(), name.capitalize()})
        unit_cards: List[Dict[str, Any]] = []
        for unit in units:
            if group_filter and unit['celltype_group'] != group_filter:
                continue
            if feature_type == 'gene':
                frame = self._lookup(unit['group_folder'], unit['stem'], 'genes', [('gene', 'in', gene_spellings)])
            else:
                catalog, _, module_name = name.rpartition('/')
                frame = self._lookup(unit['group_folder'], unit['stem'], 'modules',
                                     [('module', '==', module_name), ('catalog', '==', catalog)])
            if frame is None:
                continue
            frame = frame[(frame['cell_type'] == unit['cell_type']) & (frame['celltype_source'] == unit['celltype_source'])]
            if feature_type == 'gene':
                frame = frame[frame['gene'].str.upper() == name.upper()]
            else:
                wanted_method = method or 'pseudobulk'
                frame = frame[frame['method'] == wanted_method]
                if source:
                    frame = frame[frame['source'] == source]
            if frame.empty:
                continue
            row = frame.iloc[0].to_dict()
            unit_cards.append({key: unit[key] for key in (
                'db_id', 'title', 'cohort', 'cohort_label', 'cell_type', 'celltype_source', 'celltype_group',
                'n_replicates', 'replicate_unit', 'rung_deepest', 'counted', 'not_counted_reason')} | {
                'values': feature_record(row, unit['_unit']),
                **({'method': row['method'], 'source': row['source']} if feature_type == 'module' else {})})
        by_group: List[Dict[str, Any]] = []
        for group in sorted({card['celltype_group'] for card in unit_cards}):
            anchors = [u for u in units if u['counted'] and u['celltype_group'] == group and rung in u['rung_reached']]
            entry: Dict[str, Any] = {'celltype_group': group, 'm': len(anchors), 'rung': rung, 'k': None,
                                     'p_rank_product': None, 'r_value': None}
            if len(anchors) >= 2 and feature_type == 'gene':
                table = self.family(anchors, 'gene', rung)
                hit = table[table['gene'].str.upper() == name.upper()] if len(table) else table
                if len(hit):
                    entry.update(k=int(hit['k'].iloc[0]), m_with_gene=int(hit['m'].iloc[0]),
                                 p_rank_product=_sig(hit['p_rank_product'].iloc[0]), r_value=_sig(hit['r_value'].iloc[0]))
            by_group.append(entry)
        return {'feature': name, 'feature_type': feature_type, 'scope': scope, 'rung': rung, 'rung_label': RUNG_LABELS[rung],
                'units': unit_cards, 'by_group': by_group}


#: Every column the row readers may need (missing ones are ignored by the parquet reader).
ALL_COLUMNS: Tuple[str, ...] = tuple(
    ['db_id', 'dataset', 'cell_type', 'celltype_source', 'celltype_group', 'facet_scope', 'replicate_unit', 'n_replicates',
     'rung_deepest', 'gene', 'method', 'source', 'catalog', 'module', 'n_genes_found', 'n_control_genes', 'ave_expr',
     'half_count_q70', 'v_donor', 'v_donor_lo', 'v_donor_hi', 'sd_donor', 'var_half', 'r_half', 'reliability',
     'reliability_lo', 'reliability_hi', 'share_design_lo', 'share_design_hi', 'v_unexplained_lo', 'v_unexplained_hi',
     'share_technical', 'share_covariates', 'share_facets', 'share_latent', 'r_composition_pc1']
    + [f'{prefix}_{rung}' for rung in ('D0', 'D1', 'D2', 'D3', 'D4', 'F') for prefix in ('v_unexplained', 'share_design')]
    + [f'{prefix}_{rung}' for rung in RUNGS for prefix in ('excess_z', 'p_excess')]
    + [f'lmg_{term}' for term in TERMS])
