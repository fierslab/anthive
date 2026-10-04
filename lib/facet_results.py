"""Read the per-dataset donor-level facet test results for the API.

``bin/annot/ant-facet-test`` (genes, limma-voom on donor pseudobulk) and
``bin/annot/ant-facet-test-modules`` (module scores, OLS plus permutations)
write their output under ``<h5adstore>/_facet_tests/<group>/``:

- ``<stem>.facet_tests.parquet``         one row per (stratum, facet, gene)
- ``<stem>.facet_module_tests.parquet``  one row per (stratum, facet, method,
  source, catalog, module)
- ``<stem>.facet_composition.parquet``   per-donor cell type proportions
- ``<stem>.facet_run_manifest.json`` and
  ``<stem>.facet_module_run_manifest.json``  provenance and per-unit status
- ``run_*.log`` in the folder root: ``[skip] <db_id>: <reason>`` lines for
  datasets that failed the metadata gate (those get no manifest).

The underscore folder is excluded from the catalogue scan; this module only
reads it. The test tools may rewrite a file while the server runs, so every
read is defensive: a missing, partial or unreadable file counts as "not
available" and never raises. Parsed frames are cached by (path, mtime,
size), so a rewritten file is picked up on the next request.

The design and the statistics are described in ``doc/DESIGN_facet_testing.md``.
Everything here is exploratory output.

Nothing here talks to HTTP; ant-serve wraps it (``/databases/{id}/facet-tests``).
"""
from __future__ import annotations

import io
import json
import logging
import math
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

log = logging.getLogger(__name__)

RESULTS_DIRNAME = '_facet_tests'

SUFFIXES = {
    'genes': '.facet_tests.parquet',
    'modules': '.facet_module_tests.parquet',
    'composition': '.facet_composition.parquet',
    'gene_manifest': '.facet_run_manifest.json',
    'module_manifest': '.facet_module_run_manifest.json',
}

FACET_ORDER = ('sex', 'disease', 'apoe_carrier', 'braak')
METHOD_ORDER = (('cell_mean', 'geneset'), ('cell_mean', 'ucell'), ('pseudobulk', 'gene_list'))

# Columns returned per gene row and per module row (only those present in the
# file are used, so a schema change in the writer degrades gracefully).
GENE_COLUMNS = (
    'facet', 'cell_type', 'celltype_source', 'contrast', 'level_ref', 'level_test',
    'gene', 'logFC', 'se', 'ci_low', 'ci_high', 't', 'p_value', 'q_value', 'ave_expr',
    'n_donors', 'n_donors_ref', 'n_donors_test', 'descriptive_only', 'provisional',
)
GENE_UNIT_COLUMNS = (
    'facet', 'cell_type', 'celltype_source', 'contrast', 'level_ref', 'level_test',
    'n_donors', 'n_donors_ref', 'n_donors_test', 'covariates', 'descriptive_only',
    'n_lt5', 'provisional', 'composition_p', 'composition_flag', 'batch_used',
    'batch_confounded', 'multi_tissue_donors',
)
MODULE_COLUMNS = (
    'facet', 'cell_type', 'celltype_source', 'contrast', 'level_ref', 'level_test',
    'method', 'source', 'catalog', 'module', 'estimate', 'se', 'ci_low', 'ci_high',
    'std_effect', 't', 'df', 'p_param', 'q_param', 'p_perm', 'q_perm', 'n_perm_effective',
    'n_genes_requested', 'n_genes_found', 'n_donors', 'n_donors_ref', 'n_donors_test',
    'descriptive_only', 'provisional', 'no_counts',
)

GENE_SORTS = ('q', 'effect', 'p', 'gene')
MODULE_SORTS = ('q', 'q_perm', 'effect', 'module')
MODULE_Q_BY = ('param', 'perm', 'either')

Q_THRESHOLD = 0.05

# Cohorts: which datasets share donors. The table is a DRAFT written next to
# the results (audit section 8.3). A cohort counts once; roles matching this
# pattern do not count (the same rule as ant-facet-test-modules summarize).
COHORT_TABLE_NAME = 'cohort_selection.DRAFT.tsv'
EXCLUDED_ROLE_PATTERN = re.compile(r'duplicate|supplement|do not|never|alternative B|T20 only', re.I)

REPLICATION_DEFINITION = (
    'replicates in k of m independent cohorts: q < 0.05 with the same sign in k cohorts out of the m '
    'cohorts in which this gene was tested in this cell type and facet. A cohort is a row group of the '
    'DRAFT cohort selection table (roles that are duplicates, supplements, "do not pool", "never use '
    'alongside sources" or alternatives are not counted) and counts at most once, however many of its '
    'datasets were tested. Descriptive-only units and datasets missing from the table are not counted. '
    'Cell type labels are matched exactly.'
)

APOE_LOCUS_GENES = frozenset({'APOE', 'APOC1', 'APOC1P1', 'TOMM40', 'NECTIN2', 'PVRL2'})
APOE_LOCUS_NOTE = ('APOE locus gene: allele-linked expression and read-mapping differences can produce '
                   'an e4 effect here; it need not be downstream biology.')

# Sex positive control: XIST down and Y-linked genes up in males.
SEX_CONTROL_XIST = 'XIST'
SEX_CONTROL_Y_GENES = ('RPS4Y1', 'DDX3Y', 'UTY', 'KDM5D', 'USP9Y', 'EIF1AY', 'ZFY', 'NLGN4Y', 'TXLNGY')
SEX_CONTROL_RULE = ('passed when XIST is lower in males and RPS4Y1 higher in males, both at q < 0.05; '
                    'also counted: Y-linked genes higher in males at q < 0.01')

FACET_INFO = {
    'sex': {'contrast': 'm vs f', 'effect_unit': 'log2 fold change, male minus female', 'provisional': False},
    'disease': {'contrast': 'AD vs control', 'effect_unit': 'log2 fold change, AD minus control',
                'provisional': True},
    'apoe_carrier': {'contrast': 'any e4 allele vs none (e2 carriers pooled with e3/e3)',
                     'effect_unit': 'log2 fold change, e4 carrier minus non-carrier', 'provisional': False},
    'braak': {'contrast': 'linear trend per Braak stage (not AD vs control)',
              'effect_unit': 'log2 fold change per Braak stage', 'provisional': False},
}
PROVISIONAL_NOTE = ('AD vs control with the current vocabulary; controls differ between studies; '
                    'results will change when the disease vocabulary is settled.')

_SKIP_LINE = re.compile(r'^\[skip\]\s+(\S+):\s*(.+?)\s*$')


class FacetResultsError(ValueError):
    """A bad query parameter (maps to HTTP 400)."""


def default_results_dir(store_root: Path) -> Path:
    """``<h5adstore>/_facet_tests``, where the test tools write."""
    return Path(store_root) / RESULTS_DIRNAME


def result_paths(results_dir: Path, group_folder: str, stem: str) -> Dict[str, Path]:
    """Paths of every result file of one dataset (they may not exist)."""
    folder = Path(results_dir) / group_folder
    return {kind: folder / f'{stem}{suffix}' for kind, suffix in SUFFIXES.items()}


def _file_key(path: Path) -> Optional[Tuple[str, int, int]]:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path), stat.st_mtime_ns, stat.st_size)


def _finite(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _as_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    try:
        return bool(value)
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> Optional[float]:
    number = _finite(value)
    if number is None:
        return None
    return int(number) if number.is_integer() else number


def _as_int(value: Any) -> Optional[int]:
    number = _finite(value)
    return int(round(number)) if number is not None else None


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value)
    return text if text else None


def _split_covariates(value: Any) -> List[str]:
    text = _clean_text(value)
    if not text:
        return []
    return [part.strip() for part in text.split(',') if part.strip()]


def _unit_key(facet: Any, cell_type: Any, celltype_source: Any) -> Tuple[str, str, str]:
    return (str(facet), str(cell_type), str(celltype_source))


def _facet_rank(facet: str) -> int:
    return FACET_ORDER.index(facet) if facet in FACET_ORDER else len(FACET_ORDER)


def records(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    """DataFrame rows as JSON-safe dicts (NaN and inf become None)."""
    out: List[Dict[str, Any]] = []
    for row in frame.to_dict(orient='records'):
        clean: Dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, float):
                clean[key] = value if math.isfinite(value) else None
            elif value is pd.NA or value is pd.NaT:
                clean[key] = None
            elif hasattr(value, 'item'):  # numpy scalar
                item = value.item()
                clean[key] = None if isinstance(item, float) and not math.isfinite(item) else item
            else:
                clean[key] = value
        out.append(clean)
    return out


def json_safe(value: Any) -> Any:
    """Recursively replace NaN / inf / pandas NA with None and numpy scalars
    with Python values, so a payload always serialises as strict JSON."""
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if value is pd.NA or value is pd.NaT:
        return None
    if hasattr(value, 'item') and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class FacetResults:
    """Cached, defensive reader for the facet results folder.

    Args:
        results_dir: The facet results folder (``config.get_facet_results_dir()``,
            by default ``<h5adstore>/_facet_tests``).
        max_frames: Parsed parquet files kept in memory (LRU).
    """

    def __init__(self, results_dir: Path, max_frames: int = 12):
        self.results_dir = Path(results_dir)
        self.max_frames = max_frames
        self._lock = threading.Lock()
        self._frames: "OrderedDict[Tuple[Tuple[str, int, int], Tuple[str, ...]], pd.DataFrame]" = OrderedDict()
        self._json: Dict[Tuple[str, int, int], Optional[dict]] = {}
        self._skip_cache: Optional[Tuple[Tuple, Dict[str, Tuple[str, str]]]] = None
        self._png: "OrderedDict[Tuple, bytes]" = OrderedDict()
        self._overview_cache: Dict[Tuple, Dict[str, Any]] = {}
        self._summary_cache: Dict[Tuple, Dict[str, Any]] = {}
        self._cohort_cache: Optional[Tuple[Any, Dict[str, Dict[str, Any]]]] = None
        self._control_cache: Dict[Tuple, Dict[Tuple[str, str], Dict[str, Any]]] = {}
        self._gene_tables: "OrderedDict[Tuple, Tuple[pd.DataFrame, pd.DataFrame]]" = OrderedDict()

    # ── low-level reads ────────────────────────────────────────────────────

    def _read_parquet(self, path: Path, columns: Sequence[str]) -> Optional[pd.DataFrame]:
        """The requested columns that exist, or None when the file is missing,
        partial or unreadable."""
        key = _file_key(path)
        if key is None:
            return None
        cache_key = (key, tuple(columns))
        with self._lock:
            frame = self._frames.get(cache_key)
            if frame is not None:
                self._frames.move_to_end(cache_key)
                return frame
        try:
            import pyarrow.parquet as pq
            names = set(pq.read_schema(path).names)
            wanted = [c for c in columns if c in names]
            if not wanted:
                return None
            frame = pd.read_parquet(path, columns=wanted)
        except Exception as exc:  # partial write, corrupt file, schema change
            log.warning('facet results: cannot read %s: %s', path, exc)
            return None
        if _file_key(path) != key:  # rewritten while reading: do not cache
            return frame
        with self._lock:
            self._frames[cache_key] = frame
            self._frames.move_to_end(cache_key)
            while len(self._frames) > self.max_frames:
                self._frames.popitem(last=False)
        return frame

    def _read_json(self, path: Path) -> Optional[dict]:
        key = _file_key(path)
        if key is None:
            return None
        with self._lock:
            if key in self._json:
                return self._json[key]
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                data = None
        except Exception as exc:
            log.warning('facet results: cannot read %s: %s', path, exc)
            return None
        with self._lock:
            if len(self._json) > 512:
                self._json.clear()
            self._json[key] = data
        return data

    def _skip_reasons(self) -> Dict[str, Tuple[str, str]]:
        """db_id -> (reason, log file name) from the ``run_*.log`` files in the
        results root; a later log (by mtime) overrides an earlier one."""
        root = self.results_dir
        try:
            logs = sorted((p for p in root.glob('*.log') if p.is_file()),
                          key=lambda p: p.stat().st_mtime)
        except OSError:
            return {}
        signature = tuple(_file_key(p) for p in logs)
        with self._lock:
            if self._skip_cache is not None and self._skip_cache[0] == signature:
                return self._skip_cache[1]
        reasons: Dict[str, Tuple[str, str]] = {}
        for log_path in logs:
            try:
                text = log_path.read_text(errors='replace')
            except OSError:
                continue
            for line in text.splitlines():
                match = _SKIP_LINE.match(line)
                if match:
                    reasons[match.group(1)] = (match.group(2), log_path.name)
        with self._lock:
            self._skip_cache = (signature, reasons)
        return reasons

    def fingerprint(self, group_folder: str, stem: str) -> Tuple:
        """Changes whenever any result file of the dataset changes."""
        return tuple(_file_key(p) for p in result_paths(self.results_dir, group_folder, stem).values())

    # ── summary ────────────────────────────────────────────────────────────

    def summary(self, db_id: str, group_folder: str, stem: str,
                current_sha256: Optional[str] = None) -> Dict[str, Any]:
        """Per (facet, cell type stratum) summary of one dataset's results,
        cached until a result file, the cohort table or the run logs change.
        See ``_compute_summary``; adds ``cohort`` and, on sex units,
        ``sex_control``."""
        key = (db_id, group_folder, stem, current_sha256, self.fingerprint(group_folder, stem),
               _file_key(self.results_dir / COHORT_TABLE_NAME))
        with self._lock:
            cached = self._summary_cache.get(key)
        if cached is not None:
            return cached
        result = json_safe(self._compute_summary(db_id, group_folder, stem, current_sha256))
        result['cohort'] = self.cohorts().get(db_id)
        controls = self.sex_controls(group_folder, stem)
        for unit in result['units']:
            if unit['facet'] == 'sex' and unit['tested']:
                unit['sex_control'] = controls.get((unit['cell_type'], unit['celltype_source']))
        with self._lock:
            if len(self._summary_cache) > 512:
                self._summary_cache.clear()
            self._summary_cache[key] = result
        return result

    def _compute_summary(self, db_id: str, group_folder: str, stem: str,
                         current_sha256: Optional[str] = None) -> Dict[str, Any]:
        """Per (facet, cell type stratum) summary of one dataset's results.

        Args:
            db_id: Composite dataset id (``G24/MMG``).
            group_folder: Collection folder name in the store (``Gabitto2024``).
            stem: Dataset file stem (``Seattle_MTG_MG``).
            current_sha256: The dataset's current ``sha256`` from its ``.md``;
                when given, ``dataset_changed`` says whether the results were
                computed on a different version of the data.

        Returns:
            A JSON-safe dict, see the ant-serve endpoint docstring.
        """
        paths = result_paths(self.results_dir, group_folder, stem)
        gene_manifest = self._read_json(paths['gene_manifest'])
        module_manifest = self._read_json(paths['module_manifest'])
        gene_frame = self._read_parquet(paths['genes'], GENE_UNIT_COLUMNS + ('q_value',))
        module_frame = self._read_parquet(
            paths['modules'], ('facet', 'cell_type', 'celltype_source', 'method', 'source',
                               'q_param', 'q_perm', 'contrast', 'level_ref', 'level_test',
                               'n_donors', 'n_donors_ref', 'n_donors_test', 'covariates',
                               'descriptive_only', 'n_lt5', 'provisional', 'composition_p',
                               'composition_flag', 'batch_used', 'batch_confounded', 'no_counts'))

        units: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        records_by_unit: Dict[Tuple[str, str, str], Dict[str, Dict[str, Any]]] = {}

        def unit_for(facet, cell_type, celltype_source) -> Dict[str, Any]:
            key = _unit_key(facet, cell_type, celltype_source)
            if key not in units:
                units[key] = {
                    'facet': key[0], 'cell_type': key[1], 'celltype_source': key[2],
                    'tested': False, 'status_genes': None, 'status_modules': None,
                    'contrast': None, 'level_ref': None, 'level_test': None,
                    'level_counts': None, 'n_donors': None, 'n_donors_ref': None,
                    'n_donors_test': None, 'n_donors_dropped_covariate': None,
                    'n_cells_in_stratum': None, 'covariates': [], 'covariates_omitted': [],
                    'flags': {'descriptive_only': None, 'n_lt5': None,
                              'provisional': key[0] == 'disease', 'composition_flag': None,
                              'composition_p': None, 'batch_used': None,
                              'batch_confounded': None},
                    'genes': None, 'modules': [],
                }
            return units[key]

        def apply_manifest_unit(unit: Dict[str, Any], record: Dict[str, Any], kind: str) -> None:
            status = record.get('status')
            unit['status_' + kind] = str(status) if status is not None else None
            if status == 'ok':
                unit['tested'] = True
            if unit['level_counts'] is None and isinstance(record.get('level_counts'), dict):
                unit['level_counts'] = {str(k): _number(v) for k, v in record['level_counts'].items()}
            for field in ('n_donors_dropped_covariate', 'n_cells_in_stratum'):
                if unit[field] is None:
                    unit[field] = _as_int(record.get(field))
            if unit['n_donors'] is None:
                unit['n_donors'] = _as_int(record.get('n_donors_analysed'))
            if not unit['covariates'] and isinstance(record.get('covariates'), list):
                unit['covariates'] = [str(c) for c in record['covariates']]
            if not unit['covariates_omitted'] and isinstance(record.get('covariates_omitted'), list):
                unit['covariates_omitted'] = [str(c) for c in record['covariates_omitted']]
            flags = unit['flags']
            for field in ('n_lt5', 'composition_flag', 'batch_used', 'batch_confounded'):
                if flags[field] is None and field in record:
                    flags[field] = _as_bool(record.get(field))
            if flags['composition_p'] is None:
                flags['composition_p'] = _finite(record.get('composition_p'))

        for manifest, kind in ((gene_manifest, 'genes'), (module_manifest, 'modules')):
            for record in (manifest or {}).get('units') or []:
                if not isinstance(record, dict) or record.get('facet') is None:
                    continue
                unit = unit_for(record.get('facet'), record.get('cell_type'),
                                record.get('celltype_source'))
                apply_manifest_unit(unit, record, kind)
                records_by_unit.setdefault(_unit_key(record.get('facet'), record.get('cell_type'),
                                                     record.get('celltype_source')), {})[kind] = record
                if kind == 'modules' and record.get('status') == 'ok':
                    for method, source in METHOD_ORDER:
                        tag = f'{method}_{source}'
                        if f'n_modules_{tag}' in record:
                            unit['modules'].append({
                                'method': method, 'source': source,
                                'n_modules': _as_int(record.get(f'n_modules_{tag}')),
                                'n_q05_param': _as_int(record.get(f'n_sig_q05_param_{tag}')),
                                'n_q05_perm': _as_int(record.get(f'n_sig_q05_perm_{tag}')),
                            })
                if kind == 'genes' and record.get('status') == 'ok':
                    unit['genes'] = {'n_tested': _as_int(record.get('n_genes_tested')),
                                     'n_q05': _as_int(record.get('n_sig_q05')),
                                     'source': 'manifest'}

        # The parquet rows are the results themselves: their counts win over
        # the manifest when both exist (they disagree only mid-rewrite).
        for frame, kind in ((gene_frame, 'genes'), (module_frame, 'modules')):
            if frame is None or frame.empty:
                continue
            keys = [c for c in ('facet', 'cell_type', 'celltype_source') if c in frame.columns]
            if len(keys) < 3:
                continue
            for (facet, cell_type, celltype_source), group in frame.groupby(keys, sort=False, dropna=False):
                unit = unit_for(facet, cell_type, celltype_source)
                unit['tested'] = True
                if unit['status_' + kind] is None:
                    unit['status_' + kind] = 'ok'
                first = group.iloc[0]
                for field in ('contrast', 'level_ref', 'level_test'):
                    if unit[field] is None and field in group.columns:
                        unit[field] = _clean_text(first[field])
                for field in ('n_donors', 'n_donors_ref', 'n_donors_test'):
                    if field in group.columns and _as_int(first[field]) is not None:
                        unit[field] = _as_int(first[field])
                if 'covariates' in group.columns:
                    covariates = _split_covariates(first['covariates'])
                    if covariates:
                        unit['covariates'] = covariates
                flags = unit['flags']
                for field in ('descriptive_only', 'n_lt5', 'provisional', 'composition_flag',
                              'batch_used', 'batch_confounded'):
                    if field in group.columns and _as_bool(first[field]) is not None:
                        flags[field] = _as_bool(first[field])
                if 'composition_p' in group.columns and _finite(first['composition_p']) is not None:
                    flags['composition_p'] = _finite(first['composition_p'])
                if kind == 'genes':
                    q_values = pd.to_numeric(group.get('q_value'), errors='coerce')
                    unit['genes'] = {'n_tested': int(len(group)),
                                     'n_q05': int((q_values < Q_THRESHOLD).sum()),
                                     'source': 'results'}
                else:
                    method_rows = []
                    if 'method' in group.columns and 'source' in group.columns:
                        for (method, source), sub in group.groupby(['method', 'source'], sort=False):
                            q_param = pd.to_numeric(sub.get('q_param'), errors='coerce')
                            q_perm = pd.to_numeric(sub.get('q_perm'), errors='coerce')
                            method_rows.append({
                                'method': str(method), 'source': str(source),
                                'n_modules': int(len(sub)),
                                'n_q05_param': int((q_param < Q_THRESHOLD).sum()),
                                'n_q05_perm': int((q_perm < Q_THRESHOLD).sum()),
                            })
                    order = {pair: i for i, pair in enumerate(METHOD_ORDER)}
                    method_rows.sort(key=lambda r: order.get((r['method'], r['source']), 99))
                    unit['modules'] = method_rows

        for unit in units.values():
            flags = unit['flags']
            if flags['descriptive_only'] is None and flags['n_lt5'] is not None:
                flags['descriptive_only'] = bool(flags['n_lt5'])
            if unit['level_ref'] is None and unit['level_counts'] and unit['facet'] != 'braak':
                levels = list(unit['level_counts'])
                if len(levels) == 2:
                    unit['level_ref'], unit['level_test'] = levels[0], levels[1]
            if unit['genes'] is not None:
                unit['genes'].pop('source', None)
            if not unit['tested']:
                unit['reason'] = unit['status_genes'] or unit['status_modules']
            key = (unit['facet'], unit['cell_type'], unit['celltype_source'])
            unit['model'] = build_model(unit, records_by_unit.get(key, {}),
                                        gene_manifest, module_manifest)

        ordered = sorted(units.values(), key=lambda u: (
            u['celltype_source'], u['cell_type'], _facet_rank(u['facet']), u['facet']))

        runs = {
            'genes': _run_info(gene_manifest, kind='genes'),
            'modules': _run_info(module_manifest, kind='modules'),
        }
        files = {
            'genes': gene_frame is not None,
            'modules': module_frame is not None,
            'composition': _file_key(paths['composition']) is not None,
        }
        n_tested = sum(1 for u in ordered if u['tested'])
        if n_tested:
            status, reason = 'tested', None
        else:
            status, reason = 'not_tested', None
            for manifest in (gene_manifest, module_manifest):
                if manifest and manifest.get('status') == 'skipped' and manifest.get('reason'):
                    reason = str(manifest['reason'])
                    break
            if reason is None and ordered:
                reason = 'no facet and cell type combination had enough donors per level'
            if reason is None:
                skip = self._skip_reasons().get(db_id)
                if skip is not None:
                    reason = skip[0]
            if reason is None:
                status = 'not_run'
                reason = 'no facet test run has recorded this dataset'

        dataset_changed = None
        run_sha = (gene_manifest or module_manifest or {}).get('dataset_sha256')
        if current_sha256 and run_sha:
            dataset_changed = str(current_sha256) != str(run_sha)

        return {
            'db_id': db_id,
            'status': status,
            'reason': reason,
            'n_units': len(ordered),
            'n_units_tested': n_tested,
            'q_threshold': Q_THRESHOLD,
            'dataset_changed': dataset_changed,
            'files': files,
            'runs': runs,
            'units': ordered,
        }

    # ── rows ───────────────────────────────────────────────────────────────

    def _filter_unit(self, frame: pd.DataFrame, facet: Optional[str], cell_type: Optional[str],
                     celltype_source: Optional[str]) -> pd.DataFrame:
        mask = pd.Series(True, index=frame.index)
        for column, value in (('facet', facet), ('cell_type', cell_type),
                              ('celltype_source', celltype_source)):
            if value is not None and value != '' and column in frame.columns:
                mask &= frame[column].astype(str) == str(value)
        return frame[mask]

    def gene_rows(self, group_folder: str, stem: str, facet: Optional[str] = None,
                  cell_type: Optional[str] = None, celltype_source: Optional[str] = None,
                  q: float = Q_THRESHOLD, sort: str = 'q', limit: int = 100,
                  gene: Optional[str] = None) -> Optional[Tuple[pd.DataFrame, int]]:
        """Gene rows of one dataset, filtered and sorted.

        ``q`` below 1 keeps rows with ``q_value < q`` (descriptive-only rows,
        whose q is NULL, drop out); ``q >= 1`` keeps every row. Sort ``q``
        orders by q, then by absolute log fold change (largest first).

        Returns:
            (rows, number of rows matching before the limit), or None when the
            gene results file is missing or unreadable.
        """
        if sort not in GENE_SORTS:
            raise FacetResultsError(f'sort must be one of {", ".join(GENE_SORTS)}')
        frame = self._read_parquet(result_paths(self.results_dir, group_folder, stem)['genes'],
                                   GENE_COLUMNS)
        if frame is None:
            return None
        frame = self._filter_unit(frame, facet, cell_type, celltype_source)
        if gene and 'gene' in frame.columns:
            frame = frame[frame['gene'].astype(str).str.upper() == gene.upper()]
        if q < 1 and 'q_value' in frame.columns:
            frame = frame[pd.to_numeric(frame['q_value'], errors='coerce') < q]
        n_matching = int(len(frame))
        frame = frame.assign(_abs=pd.to_numeric(frame.get('logFC'), errors='coerce').abs())
        if sort == 'q':
            frame = frame.sort_values(['q_value', '_abs'], ascending=[True, False], na_position='last')
        elif sort == 'p':
            frame = frame.sort_values(['p_value', '_abs'], ascending=[True, False], na_position='last')
        elif sort == 'effect':
            frame = frame.sort_values('_abs', ascending=False, na_position='last')
        else:
            frame = frame.sort_values('gene', na_position='last')
        frame = frame.drop(columns='_abs').head(limit)
        return frame.reset_index(drop=True), n_matching

    def module_rows(self, group_folder: str, stem: str, facet: Optional[str] = None,
                    cell_type: Optional[str] = None, celltype_source: Optional[str] = None,
                    method: Optional[str] = None, source: Optional[str] = None,
                    q: float = Q_THRESHOLD, q_by: str = 'either', sort: str = 'q',
                    limit: int = 100, module: Optional[str] = None
                    ) -> Optional[Tuple[pd.DataFrame, int]]:
        """Module rows of one dataset, filtered and sorted.

        ``q_by`` picks the q value the threshold applies to: ``param``
        (``q_param``), ``perm`` (``q_perm``) or ``either`` (a row passes when
        one of the two is below ``q``). ``q >= 1`` keeps every row. Sort ``q``
        orders by ``q_param``, then by absolute ``std_effect``; ``q_perm`` by
        ``q_perm``; ``effect`` by absolute ``std_effect``.
        """
        if sort not in MODULE_SORTS:
            raise FacetResultsError(f'sort must be one of {", ".join(MODULE_SORTS)}')
        if q_by not in MODULE_Q_BY:
            raise FacetResultsError(f'q_by must be one of {", ".join(MODULE_Q_BY)}')
        frame = self._read_parquet(result_paths(self.results_dir, group_folder, stem)['modules'],
                                   MODULE_COLUMNS)
        if frame is None:
            return None
        frame = self._filter_unit(frame, facet, cell_type, celltype_source)
        for column, value in (('method', method), ('source', source)):
            if value and column in frame.columns:
                frame = frame[frame[column].astype(str) == value]
        if module and 'module' in frame.columns:
            frame = frame[frame['module'].astype(str).str.lower() == module.lower()]
        if q < 1:
            q_param = pd.to_numeric(frame.get('q_param'), errors='coerce') if 'q_param' in frame.columns \
                else pd.Series(float('nan'), index=frame.index)
            q_perm = pd.to_numeric(frame.get('q_perm'), errors='coerce') if 'q_perm' in frame.columns \
                else pd.Series(float('nan'), index=frame.index)
            if q_by == 'param':
                keep = q_param < q
            elif q_by == 'perm':
                keep = q_perm < q
            else:
                keep = (q_param < q) | (q_perm < q)
            frame = frame[keep]
        n_matching = int(len(frame))
        frame = frame.assign(_abs=pd.to_numeric(frame.get('std_effect'), errors='coerce').abs())
        if sort == 'q':
            frame = frame.sort_values(['q_param', '_abs'], ascending=[True, False], na_position='last')
        elif sort == 'q_perm':
            frame = frame.sort_values(['q_perm', 'q_param', '_abs'], ascending=[True, True, False],
                                      na_position='last')
        elif sort == 'effect':
            frame = frame.sort_values('_abs', ascending=False, na_position='last')
        else:
            frame = frame.sort_values(['catalog', 'module'], na_position='last')
        frame = frame.drop(columns='_abs').head(limit)
        return frame.reset_index(drop=True), n_matching

    # ── cohorts, sex positive control ──────────────────────────────────────

    def cohorts(self) -> Dict[str, Dict[str, Any]]:
        """db_id -> cohort record from the DRAFT cohort selection table.

        Record: ``cohort`` (full label), ``cohort_label`` (before the first
        parenthesis), ``role``, ``cell_class``, ``counted`` (counts towards
        replication), ``status`` (DRAFT), ``note``. Empty when the table is
        missing or unreadable.
        """
        path = self.results_dir / COHORT_TABLE_NAME
        key = _file_key(path)
        with self._lock:
            if self._cohort_cache is not None and self._cohort_cache[0] == key:
                return self._cohort_cache[1]
        table: Dict[str, Dict[str, Any]] = {}
        if key is not None:
            try:
                frame = pd.read_csv(path, sep='\t', dtype=str).fillna('')
                for row in frame.to_dict(orient='records'):
                    db_id = row.get('db_id', '').strip()
                    if not db_id:
                        continue
                    cohort = row.get('cohort', '').strip()
                    role = row.get('role', '').strip()
                    table[db_id] = {
                        'cohort': cohort,
                        'cohort_label': cohort.split(' (')[0].strip() or cohort,
                        'role': role,
                        'cell_class': row.get('cell_class', '').strip(),
                        'counted': bool(cohort) and not EXCLUDED_ROLE_PATTERN.search(role),
                        'status': row.get('status', '').strip() or 'DRAFT',
                        'note': row.get('rule_from_audit_8_3', '').strip(),
                    }
            except Exception as exc:
                log.warning('facet results: cannot read %s: %s', path, exc)
                table = {}
        with self._lock:
            self._cohort_cache = (key, table)
        return table

    def sex_controls(self, group_folder: str, stem: str) -> Dict[Tuple[str, str], Dict[str, Any]]:
        """(cell_type, celltype_source) -> sex positive control of the sex unit.

        ``status`` is ``passed``, ``failed`` or ``not assessable`` (XIST or
        RPS4Y1 not tested, or a descriptive-only unit); the rule is in
        ``rule``.
        """
        path = result_paths(self.results_dir, group_folder, stem)['genes']
        key = _file_key(path)
        if key is None:
            return {}
        with self._lock:
            if key in self._control_cache:
                return self._control_cache[key]
        genes = [SEX_CONTROL_XIST, *SEX_CONTROL_Y_GENES]
        out: Dict[Tuple[str, str], Dict[str, Any]] = {}
        try:
            frame = pd.read_parquet(path, columns=['facet', 'cell_type', 'celltype_source', 'gene',
                                                   'logFC', 'q_value'],
                                    filters=[('facet', '==', 'sex'), ('gene', 'in', genes)])
        except Exception as exc:
            log.warning('facet results: cannot read %s: %s', path, exc)
            return {}
        for (cell_type, celltype_source), group in frame.groupby(['cell_type', 'celltype_source'], sort=False):
            by_gene = {str(r['gene']): r for r in group.to_dict(orient='records')}
            xist, rps4y1 = by_gene.get(SEX_CONTROL_XIST), by_gene.get('RPS4Y1')

            def _q(record):
                return _finite(record.get('q_value')) if record else None

            def _fc(record):
                return _finite(record.get('logFC')) if record else None

            y_tested = [g for g in SEX_CONTROL_Y_GENES if g in by_gene]
            y_correct = [g for g in y_tested if _q(by_gene[g]) is not None and _q(by_gene[g]) < 0.01
                         and (_fc(by_gene[g]) or 0) > 0]
            if _q(xist) is None or _q(rps4y1) is None:
                status = 'not assessable'
            elif _q(xist) < Q_THRESHOLD and (_fc(xist) or 0) < 0 and _q(rps4y1) < Q_THRESHOLD and (_fc(rps4y1) or 0) > 0:
                status = 'passed'
            else:
                status = 'failed'
            out[(str(cell_type), str(celltype_source))] = {
                'status': status,
                'xist_logFC': _fc(xist), 'xist_q': _q(xist),
                'rps4y1_logFC': _fc(rps4y1), 'rps4y1_q': _q(rps4y1),
                'n_y_tested': len(y_tested), 'n_y_up_q01': len(y_correct),
                'rule': SEX_CONTROL_RULE,
            }
        if _file_key(path) == key:
            with self._lock:
                if len(self._control_cache) > 512:
                    self._control_cache.clear()
                self._control_cache[key] = out
        return out

    # ── gene rows of one facet across datasets ─────────────────────────────

    def facet_gene_table(self, facet: str, datasets: Sequence[Dict[str, Any]]
                         ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """All gene rows of one facet over every tested dataset.

        Returns:
            (rows, units). ``rows`` has ``unit`` (index into ``units``),
            ``gene``, ``logFC``, ``ci_low``, ``ci_high``, ``q_value`` (compact
            dtypes). ``units`` has one row per tested (dataset, stratum) with
            ``db_id``, ``title``, ``cell_type``, ``celltype_source``,
            ``n_donors(_ref/_test)``, ``level_ref``, ``level_test``,
            ``descriptive_only``, ``cohort``, ``cohort_label``, ``role``,
            ``counted`` and ``summary_unit`` (the summary unit dict).
        """
        signature = (facet, _file_key(self.results_dir / COHORT_TABLE_NAME), tuple(
            (d['db_id'], self.fingerprint(d['group_folder'], d['stem'])) for d in datasets))
        with self._lock:
            cached = self._gene_tables.get(signature)
            if cached is not None:
                self._gene_tables.move_to_end(signature)
                return cached
        cohorts = self.cohorts()
        unit_rows: List[Dict[str, Any]] = []
        parts: List[pd.DataFrame] = []
        for dataset in datasets:
            paths = result_paths(self.results_dir, dataset['group_folder'], dataset['stem'])
            if _file_key(paths['genes']) is None:
                continue
            summary = self.summary(dataset['db_id'], dataset['group_folder'], dataset['stem'],
                                   current_sha256=dataset.get('sha256'))
            tested = {(u['cell_type'], u['celltype_source']): u for u in summary['units']
                      if u['facet'] == facet and u['tested'] and u.get('genes')}
            if not tested:
                continue
            try:
                frame = pd.read_parquet(paths['genes'], columns=['facet', 'cell_type', 'celltype_source',
                                                                 'gene', 'logFC', 'ci_low', 'ci_high',
                                                                 'q_value'],
                                        filters=[('facet', '==', facet)])
            except Exception as exc:
                log.warning('facet results: cannot read %s: %s', paths['genes'], exc)
                continue
            cohort = cohorts.get(dataset['db_id']) or {}
            for (cell_type, celltype_source), group in frame.groupby(['cell_type', 'celltype_source'], sort=False):
                unit = tested.get((str(cell_type), str(celltype_source)))
                if unit is None:
                    continue
                index = len(unit_rows)
                unit_rows.append({
                    'unit': index, 'db_id': dataset['db_id'], 'title': dataset.get('title') or dataset['db_id'],
                    'cell_type': str(cell_type), 'celltype_source': str(celltype_source),
                    'n_donors': unit.get('n_donors'), 'n_donors_ref': unit.get('n_donors_ref'),
                    'n_donors_test': unit.get('n_donors_test'), 'level_ref': unit.get('level_ref'),
                    'level_test': unit.get('level_test'),
                    'descriptive_only': bool((unit.get('flags') or {}).get('descriptive_only')),
                    'cohort': cohort.get('cohort') or None, 'cohort_label': cohort.get('cohort_label') or None,
                    'role': cohort.get('role') or None, 'counted': bool(cohort.get('counted')),
                    'summary_unit': unit,
                })
                parts.append(pd.DataFrame({
                    'unit': pd.Series(index, index=group.index, dtype='int32'),
                    'gene': group['gene'].astype(str),
                    'logFC': pd.to_numeric(group['logFC'], errors='coerce').astype('float32'),
                    'ci_low': pd.to_numeric(group['ci_low'], errors='coerce').astype('float32'),
                    'ci_high': pd.to_numeric(group['ci_high'], errors='coerce').astype('float32'),
                    'q_value': pd.to_numeric(group['q_value'], errors='coerce').astype('float64'),
                }))
        units = pd.DataFrame(unit_rows, columns=[
            'unit', 'db_id', 'title', 'cell_type', 'celltype_source', 'n_donors', 'n_donors_ref',
            'n_donors_test', 'level_ref', 'level_test', 'descriptive_only', 'cohort', 'cohort_label',
            'role', 'counted', 'summary_unit'])
        if parts:
            rows = pd.concat(parts, ignore_index=True)
            rows['gene_upper'] = rows['gene'].str.upper().astype('category')
            rows['gene'] = rows['gene'].astype('category')
        else:
            rows = pd.DataFrame({'unit': pd.Series(dtype='int32'), 'gene': pd.Series(dtype='category'),
                                 'logFC': pd.Series(dtype='float32'), 'ci_low': pd.Series(dtype='float32'),
                                 'ci_high': pd.Series(dtype='float32'), 'q_value': pd.Series(dtype='float64'),
                                 'gene_upper': pd.Series(dtype='category')})
        result = (rows, units)
        with self._lock:
            self._gene_tables[signature] = result
            self._gene_tables.move_to_end(signature)
            while len(self._gene_tables) > 4:
                self._gene_tables.popitem(last=False)
        return result

    @staticmethod
    def _replication(unit_rows: pd.DataFrame) -> Dict[str, Any]:
        """Replication badge over the rows of one gene in one cell type.

        ``unit_rows`` needs ``counted``, ``descriptive_only``, ``cohort``,
        ``q_value``, ``logFC``.
        """
        eligible = unit_rows[unit_rows['counted'] & ~unit_rows['descriptive_only']
                             & unit_rows['q_value'].notna() & unit_rows['cohort'].notna()]
        cohorts_tested = sorted(set(eligible['cohort']))
        significant = eligible[eligible['q_value'] < Q_THRESHOLD]
        up = sorted(set(significant.loc[significant['logFC'] > 0, 'cohort']))
        down = sorted(set(significant.loc[significant['logFC'] < 0, 'cohort']))
        if len(up) > len(down):
            direction, same_sign = 'up', up
        elif len(down) > len(up):
            direction, same_sign = 'down', down
        elif up:
            direction, same_sign = 'mixed', up
        else:
            direction, same_sign = None, []
        m, k = len(cohorts_tested), len(same_sign)
        if m == 0:
            badge = 'not tested in a counted cohort'
        elif m == 1:
            badge = ('one cohort, not replicated' + (f' (q < 0.05, {direction})' if k else ' (not significant)'))
        else:
            badge = f'replicates in {k} of {m} independent cohorts' + (f' ({direction})' if direction else '')
        return {
            'cohorts_tested': m,
            'cohorts_sig_same_sign': k,
            'direction': direction,
            'sign_discordant': bool(up) and bool(down),
            'cohorts_up': up,
            'cohorts_down': down,
            'badge': badge + ('; sign discordant' if up and down else ''),
        }

    def gene_effects(self, facet: str, gene: str, datasets: Sequence[Dict[str, Any]],
                     cell_type: Optional[str] = None) -> Dict[str, Any]:
        """The result card of one gene for one facet: every tested unit, grouped
        by cohort, with the replication badge per cell type.

        Returns:
            ``{query, facet, gene, units, replication, cell_types, notes,
            definition}``; ``units`` is empty when the gene was not tested.
        """
        rows, units = self.facet_gene_table(facet, datasets)
        info = FACET_INFO.get(facet, {'contrast': facet, 'effect_unit': 'log2 fold change', 'provisional': False})
        gene_rows = rows[rows['gene_upper'] == gene.upper()] if len(rows) else rows
        merged = gene_rows.merge(units, on='unit', how='left') if len(gene_rows) else None
        unit_cards: List[Dict[str, Any]] = []
        replication: List[Dict[str, Any]] = []
        cell_types: List[Dict[str, Any]] = []
        if merged is not None and len(merged):
            if cell_type:
                merged = merged[merged['cell_type'] == cell_type]
            for ct, group in merged.groupby('cell_type', sort=False):
                rep = self._replication(group)
                replication.append({'cell_type': ct, **rep})
                cell_types.append({'cell_type': ct, 'n_units': int(len(group)),
                                   'n_cohorts': rep['cohorts_tested']})
            order = {r['cell_type']: (-r['cohorts_tested'], -len(merged[merged['cell_type'] == r['cell_type']]))
                     for r in replication}
            merged = merged.assign(
                _ct=merged['cell_type'].map(lambda c: order.get(c, (0, 0))),
                _counted=~merged['counted'],
                _role=merged['role'].fillna('').str.lower().str.startswith('anchor').map(lambda a: 0 if a else 1),
                _cohort=merged['cohort_label'].fillna('~'),
                _donors=-merged['n_donors'].fillna(0).astype(float))
            merged = merged.sort_values(['_ct', 'cell_type', '_counted', '_cohort', '_role', '_donors'])
            for record in merged.to_dict(orient='records'):
                summary_unit = record.get('summary_unit') or {}
                model = (summary_unit.get('model') or {}).get('genes') or {}
                q_value = _finite(record.get('q_value'))
                unit_cards.append({
                    'db_id': record['db_id'], 'title': record.get('title'),
                    'gene': str(record['gene']),
                    'cell_type': record['cell_type'], 'celltype_source': record['celltype_source'],
                    'cohort': record.get('cohort'), 'cohort_label': record.get('cohort_label'),
                    'role': record.get('role'), 'counted': bool(record.get('counted')),
                    'logFC': _finite(record.get('logFC')),
                    'ci_low': _finite(record.get('ci_low')), 'ci_high': _finite(record.get('ci_high')),
                    'q_value': q_value,
                    'significant': q_value is not None and q_value < Q_THRESHOLD,
                    'n_donors': _as_int(record.get('n_donors')),
                    'n_donors_ref': _as_int(record.get('n_donors_ref')),
                    'n_donors_test': _as_int(record.get('n_donors_test')),
                    'level_ref': record.get('level_ref'), 'level_test': record.get('level_test'),
                    'level_counts': summary_unit.get('level_counts'),
                    'descriptive_only': bool(record.get('descriptive_only')),
                    'flags': summary_unit.get('flags') or {},
                    'model': model.get('formula') or NOT_RECORDED,
                    'method': 'limma-voom on donor pseudobulk (n = donors)',
                    'covariates_left_out': [f"{c['name']} ({c['reason']})" for c in model.get('covariates') or []
                                            if c.get('status') != 'included'],
                    'run_date': model.get('run_date', NOT_RECORDED),
                    'tool': model.get('tool', NOT_RECORDED),
                })
        notes = ['exploratory', 'no meta-analysis: datasets are tested separately and share donors',
                 'cohort table is a DRAFT']
        if info.get('provisional'):
            notes.append(PROVISIONAL_NOTE)
        if gene.upper() in APOE_LOCUS_GENES and facet == 'apoe_carrier':
            notes.append(APOE_LOCUS_NOTE)
        return json_safe({
            'query': {'gene': gene, 'facet': facet, 'cell_type': cell_type},
            'facet': {'name': facet, 'column': 'ant_' + facet, 'contrast': info['contrast'],
                      'effect_unit': info['effect_unit'], 'provisional': info['provisional'],
                      'status': 'exploratory'},
            'gene': unit_cards[0]['gene'] if unit_cards else gene,
            'n_units': len(unit_cards),
            'units': unit_cards,
            'replication': replication,
            'cell_types': cell_types,
            'notes': notes,
            'apoe_locus': gene.upper() in APOE_LOCUS_GENES,
            'definition': REPLICATION_DEFINITION,
        })

    def recurring_genes(self, facet: str, datasets: Sequence[Dict[str, Any]],
                        cell_type: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
        """Genes ranked by the number of independent cohorts with q < 0.05 and
        the same sign, within one cell type label.

        Without ``cell_type`` the label with the most counted cohorts is used.
        Rows: ``gene, cohorts_sig_same_sign, cohorts_tested, direction,
        sign_discordant, units_up, units_down, max_abs_logFC, cohorts``.
        """
        rows, units = self.facet_gene_table(facet, datasets)
        counted = units[units['counted'] & ~units['descriptive_only']] if len(units) else units
        options = []
        if len(counted):
            for ct, group in counted.groupby('cell_type'):
                options.append({'cell_type': ct, 'n_cohorts': int(group['cohort'].nunique()),
                                'n_units': int(len(group))})
        options.sort(key=lambda o: (-o['n_cohorts'], -o['n_units'], o['cell_type']))
        chosen = cell_type or (options[0]['cell_type'] if options else None)
        out_rows: List[Dict[str, Any]] = []
        n_cohorts = 0
        if chosen is not None and len(rows):
            unit_ids = counted.loc[counted['cell_type'] == chosen, 'unit']
            n_cohorts = int(counted.loc[counted['cell_type'] == chosen, 'cohort'].nunique())
            sub = rows[rows['unit'].isin(unit_ids) & rows['q_value'].notna()]
            if len(sub):
                sub = sub.merge(units[['unit', 'cohort', 'cohort_label']], on='unit', how='left')
                sub = sub.assign(gene=sub['gene'].astype(str),
                                 up=(sub['q_value'] < Q_THRESHOLD) & (sub['logFC'] > 0),
                                 down=(sub['q_value'] < Q_THRESHOLD) & (sub['logFC'] < 0))
                per_cohort = sub.groupby(['gene', 'cohort'], observed=True).agg(
                    up=('up', 'any'), down=('down', 'any')).reset_index()
                per_gene = per_cohort.groupby('gene').agg(
                    cohorts_tested=('cohort', 'nunique'),
                    k_up=('up', 'sum'), k_down=('down', 'sum')).reset_index()
                per_gene = per_gene[(per_gene['k_up'] > 0) | (per_gene['k_down'] > 0)]
                if len(per_gene):
                    sig = sub[sub['up'] | sub['down']]
                    unit_counts = sig.groupby('gene').agg(units_up=('up', 'sum'), units_down=('down', 'sum'),
                                                          max_abs_logFC=('logFC', lambda v: float(v.abs().max())))
                    per_gene = per_gene.merge(unit_counts, left_on='gene', right_index=True, how='left')
                    per_gene['k'] = per_gene[['k_up', 'k_down']].max(axis=1)
                    per_gene = per_gene.sort_values(['k', 'cohorts_tested', 'max_abs_logFC'],
                                                    ascending=[False, True, False]).head(limit)
                    sig_cohorts = per_cohort[per_cohort['up'] | per_cohort['down']]
                    labels = dict(zip(units['cohort'], units['cohort_label']))
                    for record in per_gene.to_dict(orient='records'):
                        k_up, k_down = int(record['k_up']), int(record['k_down'])
                        direction = 'up' if k_up > k_down else 'down' if k_down > k_up else 'mixed'
                        names = sig_cohorts.loc[sig_cohorts['gene'] == record['gene'], 'cohort']
                        out_rows.append({
                            'gene': record['gene'],
                            'cohorts_sig_same_sign': max(k_up, k_down),
                            'cohorts_tested': int(record['cohorts_tested']),
                            'direction': direction,
                            'sign_discordant': k_up > 0 and k_down > 0,
                            'units_up': int(record.get('units_up') or 0),
                            'units_down': int(record.get('units_down') or 0),
                            'max_abs_logFC': _finite(record.get('max_abs_logFC')),
                            'cohorts': sorted({labels.get(c) or c for c in names}),
                            'apoe_locus': str(record['gene']).upper() in APOE_LOCUS_GENES,
                        })
        return json_safe({
            'facet': facet,
            'cell_type': chosen,
            'cohorts_in_cell_type': n_cohorts,
            'cell_types': options,
            'rows': out_rows,
            'definition': REPLICATION_DEFINITION,
            'cohort_table_status': 'DRAFT',
        })

    # ── one facet across datasets ──────────────────────────────────────────

    def facet_overview(self, facet: str, datasets: Sequence[Dict[str, Any]],
                       top_genes: int = 5) -> Dict[str, Any]:
        """Per-dataset summaries of one facet: one row per tested (dataset,
        stratum), sorted by donors analysed (most first). No pooling.

        Args:
            facet: Test facet name (``sex``, ``disease``, ``apoe_carrier``, ``braak``).
            datasets: Dicts with ``db_id``, ``group_folder``, ``stem`` and
                optional ``title``, ``group``, ``sha256``.
            top_genes: Genes at q < 0.05 listed per row (smallest q first).

        Returns:
            ``{facet, rows, not_tested, n_datasets_tested}``; ``not_tested``
            lists datasets with result files whose units for this facet were
            all untested, with the reason.
        """
        signature = (facet, top_genes, tuple(
            (d['db_id'], self.fingerprint(d['group_folder'], d['stem'])) for d in datasets))
        with self._lock:
            cached = self._overview_cache.get(signature)
        if cached is not None:
            return cached
        rows: List[Dict[str, Any]] = []
        not_tested: List[Dict[str, Any]] = []
        for dataset in datasets:
            paths = result_paths(self.results_dir, dataset['group_folder'], dataset['stem'])
            if all(_file_key(paths[kind]) is None for kind in ('genes', 'modules', 'gene_manifest',
                                                                 'module_manifest')):
                continue
            summary = self.summary(dataset['db_id'], dataset['group_folder'], dataset['stem'],
                                   current_sha256=dataset.get('sha256'))
            units = [u for u in summary['units'] if u['facet'] == facet]
            tested = [u for u in units if u['tested']]
            if not tested:
                if units:
                    not_tested.append({'db_id': dataset['db_id'], 'title': dataset.get('title'),
                                       'reason': units[0].get('reason')})
                continue
            top_by_unit: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
            if top_genes and any(u.get('genes') and u['genes'].get('n_q05') for u in tested):
                top_by_unit = self._top_genes(paths['genes'], facet, top_genes)
            runs = summary.get('runs') or {}
            run_date = None
            for kind in ('genes', 'modules'):
                if runs.get(kind) and runs[kind].get('timestamp_utc'):
                    run_date = str(runs[kind]['timestamp_utc'])[:10]
                    break
            for unit in tested:
                rows.append({
                    'db_id': dataset['db_id'],
                    'title': dataset.get('title'),
                    'group': dataset.get('group'),
                    **{key: unit.get(key) for key in (
                        'cell_type', 'celltype_source', 'contrast', 'level_ref', 'level_test',
                        'level_counts', 'n_donors', 'n_donors_ref', 'n_donors_test', 'covariates',
                        'covariates_omitted', 'flags', 'genes', 'modules', 'n_cells_in_stratum')},
                    'top_genes': top_by_unit.get((unit['cell_type'], unit['celltype_source']), []),
                    'run_date': run_date,
                    'dataset_changed': summary.get('dataset_changed'),
                    'cohort': (summary.get('cohort') or {}).get('cohort'),
                    'cohort_label': (summary.get('cohort') or {}).get('cohort_label'),
                    'role': (summary.get('cohort') or {}).get('role'),
                    'counted': bool((summary.get('cohort') or {}).get('counted')),
                    'model': unit.get('model'),
                    'sex_control': unit.get('sex_control'),
                })
        rows.sort(key=lambda r: (-(r.get('n_donors') or 0), r['db_id'], r.get('cell_type') or ''))
        result = json_safe({
            'facet': facet,
            'n_rows': len(rows),
            'n_datasets_tested': len({r['db_id'] for r in rows}),
            'rows': rows,
            'not_tested': not_tested,
        })
        with self._lock:
            if len(self._overview_cache) > 16:
                self._overview_cache.clear()
            self._overview_cache[signature] = result
        return result

    def _top_genes(self, path: Path, facet: str, n_top: int) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
        """(cell_type, celltype_source) -> the n_top genes with smallest q < 0.05."""
        try:
            import pyarrow.parquet as pq
            names = set(pq.read_schema(path).names)
            wanted = [c for c in ('facet', 'cell_type', 'celltype_source', 'gene', 'logFC', 'q_value')
                      if c in names]
            if len(wanted) < 6:
                return {}
            frame = pd.read_parquet(path, columns=wanted, filters=[('facet', '==', facet)])
        except Exception as exc:
            log.warning('facet results: cannot read %s: %s', path, exc)
            return {}
        frame = frame[pd.to_numeric(frame['q_value'], errors='coerce') < Q_THRESHOLD]
        out: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        if frame.empty:
            return out
        frame = frame.assign(_abs=pd.to_numeric(frame['logFC'], errors='coerce').abs())
        frame = frame.sort_values(['q_value', '_abs'], ascending=[True, False])
        for (cell_type, celltype_source), group in frame.groupby(['cell_type', 'celltype_source'], sort=False):
            out[(str(cell_type), str(celltype_source))] = records(
                group.head(n_top)[['gene', 'logFC', 'q_value']])
        return out

    # ── volcano ────────────────────────────────────────────────────────────

    def volcano_png(self, group_folder: str, stem: str, facet: str, cell_type: str,
                    celltype_source: Optional[str] = None, n_labels: int = 6) -> Optional[bytes]:
        """A small volcano plot (log2 fold change against -log10 p) of one unit.

        Genes at q < 0.05 are drawn in colour and the ``n_labels`` with the
        smallest q are labelled. No caption is drawn into the image.

        Returns:
            PNG bytes, or None when the unit has no gene rows with a p value
            (missing file, unknown unit, descriptive-only unit).
        """
        path = result_paths(self.results_dir, group_folder, stem)['genes']
        cache_key = (_file_key(path), facet, cell_type, celltype_source, n_labels)
        if cache_key[0] is None:
            return None
        with self._lock:
            cached = self._png.get(cache_key)
            if cached is not None:
                self._png.move_to_end(cache_key)
                return cached
        frame = self._read_parquet(path, GENE_COLUMNS)
        if frame is None:
            return None
        frame = self._filter_unit(frame, facet, cell_type, celltype_source)
        if celltype_source is None and 'celltype_source' in frame.columns \
                and frame['celltype_source'].nunique() > 1:
            frame = frame[frame['celltype_source'] == frame['celltype_source'].iloc[0]]
        effect = pd.to_numeric(frame.get('logFC'), errors='coerce')
        p_values = pd.to_numeric(frame.get('p_value'), errors='coerce')
        q_values = pd.to_numeric(frame.get('q_value'), errors='coerce')
        usable = effect.notna() & p_values.notna() & (p_values > 0)
        if not usable.any():
            return None
        frame = frame[usable]
        effect, p_values, q_values = effect[usable], p_values[usable], q_values[usable]
        neg_log_p = -pd.Series([math.log10(v) for v in p_values], index=p_values.index)
        significant = q_values < Q_THRESHOLD

        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure

        figure = Figure(figsize=(4.2, 3.2), dpi=110)
        FigureCanvasAgg(figure)
        axes = figure.add_subplot(111)
        axes.scatter(effect[~significant], neg_log_p[~significant], s=3, c='#a8b3bf',
                     linewidths=0, rasterized=True)
        up = significant & (effect > 0)
        down = significant & (effect < 0)
        axes.scatter(effect[up], neg_log_p[up], s=6, c='#b2182b', linewidths=0, rasterized=True)
        axes.scatter(effect[down], neg_log_p[down], s=6, c='#2166ac', linewidths=0, rasterized=True)
        axes.axvline(0, color='#cbd5e1', linewidth=0.6, zorder=0)
        if significant.any() and 'gene' in frame.columns:
            top = q_values[significant].sort_values().index[:n_labels]
            for index in top:
                axes.annotate(str(frame.at[index, 'gene']), (effect[index], neg_log_p[index]),
                              fontsize=6, xytext=(2, 2), textcoords='offset points', color='#1e293b')
        level_ref = _clean_text(frame['level_ref'].iloc[0]) if 'level_ref' in frame.columns else None
        level_test = _clean_text(frame['level_test'].iloc[0]) if 'level_test' in frame.columns else None
        if facet == 'braak':
            xlabel = 'log2 fold change per Braak stage'
        elif level_ref and level_test:
            xlabel = f'log2 fold change ({level_test} vs {level_ref})'
        else:
            xlabel = 'log2 fold change'
        axes.set_xlabel(xlabel, fontsize=8)
        axes.set_ylabel('-log10 p', fontsize=8)
        axes.tick_params(labelsize=7)
        for side in ('top', 'right'):
            axes.spines[side].set_visible(False)
        figure.tight_layout(pad=0.4)
        buffer = io.BytesIO()
        figure.savefig(buffer, format='png')
        png = buffer.getvalue()
        if _file_key(path) == cache_key[0]:
            with self._lock:
                self._png[cache_key] = png
                while len(self._png) > 64:
                    self._png.popitem(last=False)
        return png


def _run_info(manifest: Optional[dict], kind: str) -> Optional[Dict[str, Any]]:
    """Provenance of one run from its manifest (None when there is none)."""
    if not manifest:
        return None
    info: Dict[str, Any] = {
        'tool': manifest.get('tool'),
        'status': manifest.get('status'),
        'reason': manifest.get('reason'),
        'timestamp_utc': manifest.get('timestamp_utc'),
        'seed': manifest.get('seed'),
        'dataset_sha256': manifest.get('dataset_sha256'),
        'anthive4_git': manifest.get('anthive4_git'),
        'h5adstore_git': manifest.get('h5adstore_git'),
        'counts_layer': manifest.get('counts_layer'),
        'batch_column': manifest.get('batch_column'),
        'n_cells': manifest.get('n_cells'),
        'n_donors': manifest.get('n_donors'),
        'multi_tissue_donors': manifest.get('multi_tissue_donors'),
        'celltype_note': manifest.get('celltype_note'),
    }
    if kind == 'genes':
        info['versions'] = manifest.get('r_versions')
        parameters = manifest.get('parameters') or {}
        info['method'] = parameters.get('normalisation')
    else:
        info['versions'] = manifest.get('versions')
        info['genesetstore_git'] = manifest.get('genesetstore_git')
        info['sources'] = manifest.get('sources')
        info['no_counts'] = manifest.get('no_counts')
        parameters = manifest.get('parameters') or {}
        info['n_permutations'] = parameters.get('n_permutations')
    return {key: value for key, value in info.items()}


def rows_to_csv(frame: pd.DataFrame) -> str:
    """CSV text of a result frame."""
    return frame.to_csv(index=False)



# ── the model behind each result unit ────────────────────────────────────────
#
# Everything is taken from the run manifests. A few facts are not written to
# the manifests by the 1.0 tools but are fixed by the tool version (the BH
# family of the gene tests, the reference levels); they are filled in only for
# the tool versions listed here, with ``source: tool_version``. For any other
# tool version they read "not recorded".

NOT_RECORDED = 'not recorded'

_TOOL_FACTS = {
    'ant-facet-test/1.0': {
        'bh_family': 'Benjamini-Hochberg over the genes of one dataset x stratum x facet',
        'gene_model': 'limma lmFit on voom weights, then eBayes',
    },
    'ant-facet-test-modules/1.0': {},
}

_STRATUM_SOURCES = {
    'constant_all_cells': 'all cells of the dataset (ant_cell_type is one constant label)',
    'ant_cell_type': 'per-cell ant_cell_type',
    'ant_author_cell_type': 'per-cell author cell type (ant_author_cell_type)',
    'ant_author_cluster': 'author clusters (ant_author_cluster), expression-defined groups',
    'none': 'all cells pooled (no cell type column), not cell type specific',
}

_FACET_TERMS = {
    'sex': ('sex', 'm vs f: coefficient = m minus f (f is the reference)'),
    'disease': ('disease', 'AD vs control: coefficient = AD minus control (control is the reference)'),
    'apoe_carrier': ('apoe_carrier', 'e4 carrier vs non-carrier: coefficient = carrier minus non-carrier'),
    'braak': ('braak', 'Braak stage as a number: coefficient = change per Braak stage (linear trend)'),
}


def _stratum_source_text(source: str) -> str:
    if source in _STRATUM_SOURCES:
        return _STRATUM_SOURCES[source]
    return f'per-cell author column {source}'


def _covariate_rows(facet: str, record: Optional[Dict[str, Any]], manifest: Optional[dict]
                    ) -> Optional[List[Dict[str, str]]]:
    """Included and left-out covariates of one unit with the reason, from the
    manifest unit record. None when there is no record."""
    if not record:
        return None
    parameters = (manifest or {}).get('parameters') or {}
    included = [str(c) for c in record.get('covariates') or []]
    omitted = [str(c) for c in record.get('covariates_omitted') or []]
    min_age_pmi = parameters.get('min_donors_for_age_pmi')
    coverage = parameters.get('min_covariate_coverage')
    coverage_text = f'{round(coverage * 100)}%' if isinstance(coverage, (int, float)) else NOT_RECORDED
    analysed = _as_int(record.get('n_donors_analysed'))
    dropped = _as_int(record.get('n_donors_dropped_covariate')) or 0
    total = analysed + dropped if analysed is not None else None
    rows: List[Dict[str, str]] = []
    if facet != 'sex':
        if 'sex' in included:
            rows.append({'name': 'sex', 'status': 'included', 'term': 'sex', 'reason': 'm vs f'})
        elif 'sex' in omitted:
            rows.append({'name': 'sex', 'status': 'dropped', 'term': '',
                         'reason': f'sex known for fewer than {coverage_text} of donors or only one sex present'})
        else:
            rows.append({'name': 'sex', 'status': 'not used', 'term': '', 'reason': NOT_RECORDED})
    for name in ('age', 'pmi'):
        if name in included:
            rows.append({'name': name, 'status': 'included', 'term': f'{name}_z',
                         'reason': 'z-scored within the unit'})
        elif name in omitted:
            rows.append({'name': name, 'status': 'dropped', 'term': '',
                         'reason': f'known for fewer than {coverage_text} of donors, or constant'})
        elif total is not None and isinstance(min_age_pmi, (int, float)) and total < min_age_pmi:
            rows.append({'name': name, 'status': 'not used', 'term': '',
                         'reason': f'fewer than {int(min_age_pmi)} donors in the unit'})
        else:
            rows.append({'name': name, 'status': 'not used', 'term': '',
                         'reason': 'no values in this dataset'})
    batch_column = (manifest or {}).get('batch_column') if manifest else None
    if 'batch' in included:
        rows.append({'name': 'batch', 'status': 'included', 'term': 'batch',
                     'reason': f'donor-level column {batch_column}' if batch_column else 'donor-level batch'})
    elif record.get('batch_confounded'):
        rows.append({'name': 'batch', 'status': 'dropped', 'term': '',
                     'reason': f'{batch_column or "batch"} is confounded with the facet'})
    elif any(c.startswith('batch') for c in omitted):
        rows.append({'name': 'batch', 'status': 'dropped', 'term': '',
                     'reason': f'{batch_column or "batch"} has more levels than donors / 4'})
    elif batch_column:
        rows.append({'name': 'batch', 'status': 'not used', 'term': '',
                     'reason': f'{batch_column} is missing for some donors or has one level'})
    else:
        rows.append({'name': 'batch', 'status': 'not used', 'term': '',
                     'reason': 'no donor-level batch column found'})
    return rows


def _formula(response: str, facet: str, covariates: Optional[List[Dict[str, str]]]) -> str:
    if covariates is None:
        return NOT_RECORDED
    terms = [_FACET_TERMS.get(facet, (facet, ''))[0]]
    terms += [row['term'] for row in covariates if row['status'] == 'included' and row['term']]
    return f'{response} ~ ' + ' + '.join(terms)


def _run_line(manifest: Optional[dict], versions_key: str) -> Dict[str, Any]:
    manifest = manifest or {}
    return {
        'tool': manifest.get('tool') or NOT_RECORDED,
        'seed': manifest.get('seed') if manifest.get('seed') is not None else NOT_RECORDED,
        'run_date': str(manifest['timestamp_utc'])[:10] if manifest.get('timestamp_utc') else NOT_RECORDED,
        'versions': manifest.get(versions_key) or NOT_RECORDED,
        'anthive4_git': manifest.get('anthive4_git') or NOT_RECORDED,
        'dataset_sha256': manifest.get('dataset_sha256') or NOT_RECORDED,
    }


def build_model(unit: Dict[str, Any], records_for_unit: Dict[str, Dict[str, Any]],
                gene_manifest: Optional[dict], module_manifest: Optional[dict]) -> Dict[str, Any]:
    """The exact model behind one (facet, stratum) unit, from the manifests.

    Args:
        unit: The summary unit (facet, cell_type, celltype_source, ...).
        records_for_unit: Manifest unit records keyed ``genes`` / ``modules``.
        gene_manifest: ``<stem>.facet_run_manifest.json`` (or None).
        module_manifest: ``<stem>.facet_module_run_manifest.json`` (or None).

    Returns:
        ``{stratum, facet_term, donors, descriptive_only_rule, genes, modules}``.
        ``genes`` / ``modules`` are None when that run has no record of the
        unit. Values missing from the manifests read "not recorded".
    """
    facet = unit['facet']
    gene_record = records_for_unit.get('genes')
    module_record = records_for_unit.get('modules')
    parameters = ((gene_manifest or {}).get('parameters') or (module_manifest or {}).get('parameters') or {})
    min_cells = parameters.get('min_cells_per_sample')
    n_lt5 = parameters.get('n_lt5_threshold')
    numeric_min = parameters.get('numeric_min_donors_for_p')
    if n_lt5 is not None and numeric_min is not None:
        rule = (f'descriptive only (no p, q or CI) when a level has fewer than {n_lt5} donors; '
                f'for Braak when fewer than {numeric_min} donors')
    else:
        rule = NOT_RECORDED
    donors = {
        'per_level': unit.get('level_counts') or NOT_RECORDED,
        'n_analysed': unit.get('n_donors') if unit.get('n_donors') is not None else NOT_RECORDED,
        'n_dropped_missing_covariate': (unit.get('n_donors_dropped_covariate')
                                        if unit.get('n_donors_dropped_covariate') is not None else NOT_RECORDED),
        'min_cells_per_donor': min_cells if min_cells is not None else NOT_RECORDED,
        'min_donors_per_level': parameters.get('min_donors_per_level', NOT_RECORDED),
    }
    model: Dict[str, Any] = {
        'stratum': {'cell_type': unit['cell_type'], 'source': unit['celltype_source'],
                    'description': _stratum_source_text(unit['celltype_source'])},
        'facet_term': _FACET_TERMS.get(facet, (facet, NOT_RECORDED))[1],
        'donors': donors,
        'descriptive_only_rule': rule,
        'genes': None,
        'modules': None,
    }
    if gene_record and gene_manifest:
        gene_parameters = gene_manifest.get('parameters') or {}
        facts = _TOOL_FACTS.get(str(gene_manifest.get('tool')), {})
        covariates = _covariate_rows(facet, gene_record, gene_manifest)
        counts_layer = gene_manifest.get('counts_layer')
        unit_text = (f'one pseudobulk per donor: raw counts{" (layer " + counts_layer + ")" if counts_layer else ""} '
                     f'summed over the donor\'s cells in the stratum; donors with fewer than '
                     f'{min_cells if min_cells is not None else NOT_RECORDED} cells there are left out')
        method_parts = [gene_parameters.get('gene_filter'), gene_parameters.get('normalisation'),
                        facts.get('gene_model'), gene_parameters.get('eBayes')]
        model['genes'] = {
            'status': gene_record.get('status'),
            'unit_of_analysis': unit_text,
            'formula': _formula('expression', facet, covariates),
            'covariates': covariates,
            'method': '; '.join(p for p in method_parts if p) or NOT_RECORDED,
            'n_genes_tested': _as_int(gene_record.get('n_genes_tested')),
            'n_genes_input': _as_int(gene_record.get('n_genes_input')),
            'multiple_testing': facts.get('bh_family', NOT_RECORDED),
            **_run_line(gene_manifest, 'r_versions'),
        }
    if module_record and module_manifest:
        module_parameters = module_manifest.get('parameters') or {}
        covariates = _covariate_rows(facet, module_record, module_manifest)
        n_perm = module_parameters.get('n_permutations')
        variants = {}
        if any(k.startswith('n_modules_cell_mean') for k in module_record) or module_record.get('status') == 'ok':
            variants['cell_mean'] = ('per donor, mean of the stored per-cell module score over the donor\'s cells '
                                     'in the stratum (source geneset: scanpy score_genes; source ucell: UCell ranks)')
        if not module_manifest.get('no_counts'):
            expression = module_parameters.get('pseudobulk_expression') or NOT_RECORDED
            ctrl = module_parameters.get('ctrl_size')
            bins = module_parameters.get('n_bins')
            variants['pseudobulk'] = (f'per donor pseudobulk, {expression}; score = mean over the module genes minus '
                                      f'mean over {ctrl if ctrl is not None else NOT_RECORDED} expression-matched '
                                      f'control genes per module gene ({bins if bins is not None else NOT_RECORDED} bins)')
        model['modules'] = {
            'status': module_record.get('status'),
            'unit_of_analysis': variants or NOT_RECORDED,
            'formula': _formula('score', facet, covariates),
            'covariates': covariates,
            'method': (f'OLS per module; permutation p from {n_perm:,} shuffles: '
                       f'{module_parameters.get("permutation") or NOT_RECORDED}'
                       if isinstance(n_perm, int) else NOT_RECORDED),
            'n_permutations': n_perm if n_perm is not None else NOT_RECORDED,
            'multiple_testing': module_parameters.get('bh_family') or NOT_RECORDED,
            'sources': module_manifest.get('sources') or NOT_RECORDED,
            'no_counts': bool(module_manifest.get('no_counts')),
            **_run_line(module_manifest, 'versions'),
        }
    return model
