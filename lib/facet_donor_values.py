"""Donor-level values behind a facet test: what the dots of a donor plot are.

Every facet result (gene or module x facet x cell type group x scope) comes from one value per replicate (donor,
animal or sample) in each counted unit. This module rebuilds exactly those values from the per-sample sidecars that
``bin/annot/ant-facet-test`` and ``bin/annot/ant-facet-test-modules`` write next to their results:

* gene sidecars ``<stem>.facet_samples.parquet`` (one row per unit and replicate: facet value, fitted covariates,
  cell count, library size after ``filterByExpr`` and TMM factor) and ``<stem>.facet_sample_cells.parquet`` (the cells
  summed into every sample). One gene's value is the pseudobulk count of the sample on the voom input scale,
  ``log2((count + 0.5) / (library size * TMM factor + 1) * 1e6)``, summed on demand for that gene only;
* module sidecars ``<stem>.facet_module_samples.parquet`` and ``<stem>.facet_module_sample_scores.parquet``: the
  replicate scores the module test fitted (cell mean of the per-cell score, or the pseudobulk variant).

Replicate ids are never written to the sidecars and never returned: replicates get sequential labels (``D01`` ...)
ordered by facet value and library size, assigned per unit. Per-replicate covariate values stay on the server
(they are only used for the optional covariate-adjusted values); the answer names the covariates of the model.

Results from before the sidecars existed have no donor values (``available`` false with a reason) until
``ant-facet-test run --samples-only`` (and the modules counterpart) backfill them.
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from facet_rules import AGE_UNIT_YEARS

log = logging.getLogger(__name__)

GENE_SAMPLES_SUFFIX = '.facet_samples.parquet'
GENE_SAMPLE_CELLS_SUFFIX = '.facet_sample_cells.parquet'
MODULE_SAMPLES_SUFFIX = '.facet_module_samples.parquet'
MODULE_SCORES_SUFFIX = '.facet_module_sample_scores.parquet'

NUMERIC_FACETS = ('braak', 'age', 'age_adjusted', 'apoe4_dose')
AGE_FACETS = ('age', 'age_adjusted')
COVARIATE_COLUMNS = {'cov_sex': 'sex', 'cov_age': 'age', 'cov_pmi': 'pmi', 'cov_path': 'pathology'}
GENE_SCALE = 'log2 CPM (TMM normalised library size, the scale voom fits)'
CACHE_ENTRIES = 256
BACKFILL_HINT = ('no donor values stored for this unit yet: the sidecar is written by the next run of the test '
                 '(ant-facet-test run --samples-only backfills existing results)')

#: Axis texts of the facets: ``x_label`` of a numeric facet and the unit of its slope.
NUMERIC_AXIS = {
    'braak': ('Braak stage', 'per Braak stage'),
    'age': ('age (years)', 'per decade of age'),
    'age_adjusted': ('age (years)', 'per decade of age'),
    'apoe4_dose': ('APOE e4 copies', 'per e4 copy'),
}


def voom_log2_cpm(counts: np.ndarray, library_size: np.ndarray, norm_factor: np.ndarray) -> np.ndarray:
    """The voom input of a gene: ``log2((count + 0.5) / (library size * norm factor + 1) * 1e6)``.

    Args:
        counts: Raw pseudobulk counts of the gene, one per sample.
        library_size: Library size per sample after ``filterByExpr`` (``calcNormFactors`` input).
        norm_factor: TMM factor per sample.

    Returns:
        Log2 counts per million on the scale ``limma::voom`` fits.
    """
    effective_size = np.asarray(library_size, dtype=float) * np.asarray(norm_factor, dtype=float)
    return np.log2((np.asarray(counts, dtype=float) + 0.5) / (effective_size + 1.0) * 1e6)


def adjust_for_covariates(values: np.ndarray, facet_value: np.ndarray, covariates: np.ndarray) -> np.ndarray:
    """Values with the covariate part of an OLS fit removed (centred, so the scale stays that of the values).

    Fits ``value ~ 1 + facet + covariates`` by unweighted least squares and subtracts ``(C - mean(C)) @ beta_C``.
    The test itself uses voom weights, so this is an approximation of the model the p value comes from: the
    drawn trend can differ a little from the reported estimate.

    Args:
        values: One value per replicate.
        facet_value: Facet column of the model (0/1 or numeric).
        covariates: Matrix (replicates x covariates); empty (no columns) returns ``values`` unchanged.

    Returns:
        The adjusted values (a new array).
    """
    values = np.asarray(values, dtype=float)
    if covariates.size == 0 or covariates.shape[1] == 0:
        return values.copy()
    design = np.column_stack([np.ones(len(values)), np.asarray(facet_value, dtype=float), covariates])
    beta, *_ = np.linalg.lstsq(design, values, rcond=None)
    covariate_beta = beta[2:]
    centred = covariates - covariates.mean(axis=0)
    return values - centred @ covariate_beta


def _covariate_matrix(frame: pd.DataFrame) -> Tuple[np.ndarray, List[str]]:
    """Covariate columns of the model of a unit (numeric ones as stored, batch as dummy columns) and their names."""
    columns: List[np.ndarray] = []
    names: List[str] = []
    for column, name in COVARIATE_COLUMNS.items():
        if column in frame and frame[column].notna().any():
            columns.append(frame[column].astype(float).to_numpy())
            names.append(name)
    if 'batch_code' in frame and (frame['batch_code'] >= 0).all() and frame['batch_code'].nunique() > 1:
        codes = frame['batch_code'].to_numpy()
        for code in sorted(set(codes))[1:]:
            columns.append((codes == code).astype(float))
        names.append('batch')
    matrix = np.column_stack(columns) if columns else np.zeros((len(frame), 0))
    return matrix, names


def facet_kind(facet: str) -> str:
    """``numeric`` (a trend per unit) or ``two_group`` for a facet name."""
    return 'numeric' if facet in NUMERIC_FACETS else 'two_group'


def _labels(count: int) -> List[str]:
    width = max(2, len(str(count)))
    return [f'D{index:0{width}d}' for index in range(1, count + 1)]


def _file_key(path: Path) -> Optional[Tuple[str, int, int]]:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path), stat.st_mtime_ns, stat.st_size)


class DonorValues:
    """Reader and calculator of the donor-level values of facet test units.

    Thread safe. Sidecar tables are cached by file mtime, per-unit answers per (unit, subject, file stamp).

    Args:
        results_dir: The facet results folder (``<h5adstore>/_facet_tests``).
    """

    def __init__(self, results_dir: Path):
        self.results_dir = Path(results_dir)
        self._lock = threading.Lock()
        self._tables: "OrderedDict[Tuple, pd.DataFrame]" = OrderedDict()
        self._answers: "OrderedDict[Tuple, Dict[str, Any]]" = OrderedDict()

    # ── sidecar access ───────────────────────────────────────────────────────

    def _path(self, group_folder: str, stem: str, suffix: str) -> Path:
        return self.results_dir / group_folder / f'{stem}{suffix}'

    def _table(self, path: Path) -> Optional[pd.DataFrame]:
        """A sidecar parquet (cached while the file is unchanged); None when missing or unreadable."""
        key = _file_key(path)
        if key is None:
            return None
        with self._lock:
            cached = self._tables.get(key)
            if cached is not None:
                self._tables.move_to_end(key)
                return cached
        try:
            table = pd.read_parquet(path)
        except Exception as exc:  # a file being rewritten is just not available yet
            log.warning('donor values: cannot read %s: %s', path, exc)
            return None
        with self._lock:
            self._tables[key] = table
            while len(self._tables) > 24:
                self._tables.popitem(last=False)
        return table

    def unit_samples(self, group_folder: str, stem: str, facet: str, cell_type: str, celltype_source: str,
                     kind: str = 'genes') -> Optional[pd.DataFrame]:
        """The replicate rows of one unit from the samples sidecar (``kind`` genes or modules); None when absent."""
        suffix = GENE_SAMPLES_SUFFIX if kind == 'genes' else MODULE_SAMPLES_SUFFIX
        table = self._table(self._path(group_folder, stem, suffix))
        if table is None:
            return None
        rows = table[(table['facet'] == facet) & (table['cell_type'] == cell_type)
                     & (table['celltype_source'] == celltype_source)]
        return rows if len(rows) else None

    def has_unit(self, group_folder: str, stem: str, facet: str, cell_type: str, celltype_source: str,
                 kind: str = 'genes') -> bool:
        """Whether the sidecar holds the replicate rows of this unit."""
        return self.unit_samples(group_folder, stem, facet, cell_type, celltype_source, kind) is not None

    # ── answers ──────────────────────────────────────────────────────────────

    def gene_unit(self, group_folder: str, stem: str, facet: str, cell_type: str, celltype_source: str, gene: str,
                  fetch_counts: Callable[[int, str], Optional[pd.DataFrame]],
                  level_labels: Optional[Tuple[Optional[str], Optional[str]]] = None) -> Dict[str, Any]:
        """The donor values of one gene in one unit.

        Args:
            group_folder: Results folder of the dataset (``_pooled`` for a donor-pooled unit).
            stem: File stem (pool id for a pooled unit).
            facet: Tested facet name.
            cell_type: Raw stratum label of the unit.
            celltype_source: Stratifier of the unit.
            gene: Gene symbol.
            fetch_counts: ``(file_index, gene) -> DataFrame(cell_id, value)`` of raw counts of the gene in member file
                ``file_index`` (0 for a single dataset), or None when the file has no such gene.
            level_labels: ``(reference, tested)`` level names of a two-group facet.

        Returns:
            ``{available, reason, facet_kind, covariates, n_donors, donors[]}``; each donor has ``label``,
            ``facet_value`` (stage, years, 0/1), ``group`` (level name, two-group), ``value``, ``value_adjusted``,
            ``n_cells``. Without a sidecar or counts ``available`` is false and ``reason`` says why.
        """
        samples_path = self._path(group_folder, stem, GENE_SAMPLES_SUFFIX)
        cells_path = self._path(group_folder, stem, GENE_SAMPLE_CELLS_SUFFIX)
        stamp = (_file_key(samples_path), _file_key(cells_path))
        cache_key = ('gene', group_folder, stem, facet, cell_type, celltype_source, gene.upper(), level_labels, stamp)
        with self._lock:
            hit = self._answers.get(cache_key)
            if hit is not None:
                self._answers.move_to_end(cache_key)
                return hit
        frame = self.unit_samples(group_folder, stem, facet, cell_type, celltype_source, 'genes')
        cell_table = self._table(cells_path)
        if frame is None or cell_table is None:
            return self._unavailable(facet, BACKFILL_HINT)
        frame = frame.reset_index(drop=True)
        member_cells = cell_table[cell_table['sample_idx'].isin(set(frame['sample_idx']))]
        count_by_sample = pd.Series(0.0, index=frame['sample_idx'].to_numpy())
        found_any = False
        for file_index, part in member_cells.groupby('file_index'):
            gene_counts = fetch_counts(int(file_index), gene)
            if gene_counts is None:
                continue
            found_any = True
            joined = part.merge(gene_counts, on='cell_id', how='inner')
            summed = joined.groupby('sample_idx')['value'].sum()
            count_by_sample = count_by_sample.add(summed, fill_value=0.0)
        if not found_any:
            return self._unavailable(facet, f'gene {gene} is not in the counts of this dataset')
        counts = count_by_sample.reindex(frame['sample_idx']).fillna(0.0).to_numpy()
        values = voom_log2_cpm(counts, frame['lib_size'].to_numpy(), frame['norm_factor'].to_numpy())
        answer = self._assemble(frame, values, facet, level_labels, GENE_SCALE)
        answer['counts_total'] = int(counts.sum())
        with self._lock:
            self._answers[cache_key] = answer
            while len(self._answers) > CACHE_ENTRIES:
                self._answers.popitem(last=False)
        return answer

    def module_unit(self, group_folder: str, stem: str, facet: str, cell_type: str, celltype_source: str,
                    catalog: str, module: str, method: str, source: str,
                    level_labels: Optional[Tuple[Optional[str], Optional[str]]] = None) -> Dict[str, Any]:
        """The replicate scores of one module variant in one unit (what the module test fitted).

        ``method`` / ``source`` select the variant (``cell_mean`` with ``geneset`` or ``ucell``; ``pseudobulk`` with
        ``gene_list``). Same answer shape as ``gene_unit``; ``scale`` names the score.
        """
        samples_path = self._path(group_folder, stem, MODULE_SAMPLES_SUFFIX)
        scores_path = self._path(group_folder, stem, MODULE_SCORES_SUFFIX)
        stamp = (_file_key(samples_path), _file_key(scores_path))
        cache_key = ('module', group_folder, stem, facet, cell_type, celltype_source, catalog, module, method, source,
                     level_labels, stamp)
        with self._lock:
            hit = self._answers.get(cache_key)
            if hit is not None:
                self._answers.move_to_end(cache_key)
                return hit
        frame = self.unit_samples(group_folder, stem, facet, cell_type, celltype_source, 'modules')
        scores = self._table(scores_path)
        if frame is None or scores is None:
            return self._unavailable(facet, BACKFILL_HINT)
        frame = frame.reset_index(drop=True)
        wanted = scores[(scores['catalog'] == catalog) & (scores['module'] == module) & (scores['method'] == method)
                        & (scores['source'] == source)
                        & (scores['sample_idx'].isin(set(frame['sample_idx'])))]
        by_sample = wanted.drop_duplicates('sample_idx').set_index('sample_idx')['score']
        if by_sample.empty:
            return self._unavailable(facet, f'no replicate scores of {catalog}/{module} ({method}, {source}) stored')
        keep = frame['sample_idx'].isin(by_sample.index).to_numpy()
        frame = frame[keep].reset_index(drop=True)
        values = by_sample.reindex(frame['sample_idx']).to_numpy(dtype=float)
        answer = self._assemble(frame, values, facet, level_labels, module_scale(method, source))
        with self._lock:
            self._answers[cache_key] = answer
            while len(self._answers) > CACHE_ENTRIES:
                self._answers.popitem(last=False)
        return answer

    @staticmethod
    def _unavailable(facet: str, reason: str) -> Dict[str, Any]:
        return {'available': False, 'reason': reason, 'facet_kind': facet_kind(facet), 'covariates': [],
                'n_donors': 0, 'donors': []}

    @staticmethod
    def _assemble(frame: pd.DataFrame, values: np.ndarray, facet: str,
                  level_labels: Optional[Tuple[Optional[str], Optional[str]]], scale: str) -> Dict[str, Any]:
        """The answer of one unit from its replicate rows and values (labels, display facet values, adjusted values)."""
        kind = facet_kind(facet)
        covariates, names = _covariate_matrix(frame)
        adjusted = adjust_for_covariates(values, frame['facet_value'].to_numpy(), covariates)
        facet_value = frame['facet_value'].to_numpy(dtype=float)
        shown = facet_value * AGE_UNIT_YEARS if facet in AGE_FACETS else facet_value
        order = np.lexsort((frame['lib_size'].to_numpy() if 'lib_size' in frame else frame['n_cells'].to_numpy(),
                            facet_value))
        labels = _labels(len(frame))
        reference, tested = level_labels or (None, None)
        donors: List[Dict[str, Any]] = []
        for rank, position in enumerate(order):
            entry: Dict[str, Any] = {
                'label': labels[rank], 'facet_value': float(shown[position]),
                'value': float(values[position]), 'value_adjusted': float(adjusted[position]),
                'n_cells': int(frame['n_cells'].iloc[position])}
            if kind == 'two_group':
                entry['group'] = (tested if facet_value[position] >= 0.5 else reference) or (
                    'tested' if facet_value[position] >= 0.5 else 'reference')
            donors.append(entry)
        return {'available': True, 'reason': None, 'facet_kind': kind, 'scale': scale, 'covariates': names,
                'n_donors': len(donors), 'donors': donors}


def module_scale(method: str, source: str) -> str:
    """Plain name of the score scale of a module variant."""
    if method == 'pseudobulk':
        return 'module score of the pseudobulk (log2 CPM, minus a matched control gene set)'
    if source == 'ucell':
        return 'mean UCell score of the replicate\'s cells (0 to 1)'
    return 'mean gene set score of the replicate\'s cells (scanpy score_genes units)'


def fitted_line(donors: Sequence[Dict[str, Any]], facet: str, estimate: Optional[float],
                adjusted: bool = False) -> Optional[Dict[str, float]]:
    """The straight line of a numeric facet panel: slope = the reported estimate, through the centroid of the dots.

    Args:
        donors: ``donors`` of one unit (``facet_value`` in display units: stage, years).
        facet: Facet name.
        estimate: The unit's reported effect per stage, per decade of age or per copy.
        adjusted: Draw through the centroid of the adjusted values.

    Returns:
        ``{slope, intercept}`` in display units (slope per year for age) or None without an estimate or dots.
    """
    if estimate is None or not donors:
        return None
    x_values = np.array([d['facet_value'] for d in donors])
    y_values = np.array([d['value_adjusted' if adjusted else 'value'] for d in donors])
    slope = float(estimate) / (AGE_UNIT_YEARS if facet in AGE_FACETS else 1.0)
    return {'slope': slope, 'intercept': float(y_values.mean() - slope * x_values.mean())}
