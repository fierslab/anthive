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
Everything here is exploratory output. Since v3 (2026-10-06) every cross-dataset view is per facet scope
(``lib/facet_scopes.py``: human post-mortem, human cells xenografted into mouse): the ``scope``
argument selects one, the default is ``FacetResults.default_scope(facet)``, and genes are never matched across scopes.

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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from pandas.api.types import union_categoricals

import facet_rules
import facet_scopes
import gene_classes
import replicates
import replication
from celltype_groups import UNMAPPED, group_info, group_of
from donor_keys import distinct_count
from facet_aliases import DEPRECATED_FACET_TAGS, canonical_facet

log = logging.getLogger(__name__)

RESULTS_DIRNAME = '_facet_tests'

SUFFIXES = {
    'genes': '.facet_tests.parquet',
    'modules': '.facet_module_tests.parquet',
    'composition': '.facet_composition.parquet',
    'gene_manifest': '.facet_run_manifest.json',
    'module_manifest': '.facet_module_run_manifest.json',
}

#: Folder (inside a group folder) of the full-donor runs of datasets that have a
#: ``facet_donor_exclusions`` rule: supplementary, never counted, not read by the API.
SUPPLEMENTARY_DIRNAME = 'supplementary_all_donors'

#: Folder of the donor-pooled units (``ant-facet-test run --pool``): files ``<pool_id>.*`` with
#: db_id ``pool:<pool_id>``. They are datasets of the results reader, not of the catalogue.
POOLED_DIRNAME = '_pooled'
POOL_PREFIX = 'pool:'

#: Threads that read result files in parallel when the gene tables are built (pyarrow releases the GIL).
READ_WORKERS = 6
#: Replication families computed at the same time (numpy/pandas hold the GIL for part of the work).
FAMILY_WORKERS = 4
UNIT_TABLE_COLUMNS = [
    'unit', 'db_id', 'title', 'cell_type', 'celltype_source', 'n_donors', 'n_donors_ref',
    'n_donors_test', 'level_ref', 'level_test', 'descriptive_only', 'cohort', 'cohort_label',
    'role', 'counted', 'independence', 'overlap_unknown', 'pooled', 'disease_basis',
    'analysis_level', 'tau_log2', 'exclusion_missing', 'is_secondary', 'celltype_group', 'facet_scope',
    'summary_unit', 'donor_digests', 'modality', 'modality_note']

FACET_ORDER = ('sex', 'disease', 'apoe4_carrier', 'apoe4_dose', 'braak', 'braak_threshold', 'age', 'age_adjusted',
               'app_nlgf')
#: Replication-family cache size: every (facet, scope, exclude_secondary) fits, so no request evicts another's
#: families (the 2.44 facets x scopes multiplied the keys past the old LRU of 16, so each call recomputed them).
FAMILY_CACHE_ENTRIES = len(FACET_ORDER) * len(facet_scopes.SCOPES) * 2
#: Gene cards kept (one per facet, scope and gene): a gene page asks for every facet and scope of one gene.
CARD_CACHE_ENTRIES = 256
METHOD_ORDER = (('cell_mean', 'geneset'), ('cell_mean', 'ucell'), ('pseudobulk', 'gene_list'))

# Columns returned per gene row and per module row (only those present in the
# file are used, so a schema change in the writer degrades gracefully).
GENE_COLUMNS = (
    'facet', 'cell_type', 'celltype_source', 'contrast', 'level_ref', 'level_test',
    'gene', 'logFC', 'se', 'ci_low', 'ci_high', 't', 'p_value', 'q_value', 'p_treat', 'q_treat',
    'tau_log2', 'ave_expr', 'n_donors', 'n_donors_ref', 'n_donors_test', 'descriptive_only', 'provisional',
    'small_sample', 'facet_scope',
)
GENE_UNIT_COLUMNS = (
    'facet', 'cell_type', 'celltype_source', 'contrast', 'level_ref', 'level_test',
    'n_donors', 'n_donors_ref', 'n_donors_test', 'covariates', 'descriptive_only',
    'n_lt5', 'provisional', 'composition_p', 'composition_flag', 'batch_used',
    'batch_confounded', 'multi_tissue_donors', 'small_sample', 'facet_scope',
)
MODULE_COLUMNS = (
    'facet', 'cell_type', 'celltype_source', 'contrast', 'level_ref', 'level_test',
    'method', 'source', 'catalog', 'module', 'estimate', 'se', 'ci_low', 'ci_high',
    'std_effect', 't', 'df', 'p_param', 'q_param', 'p_perm', 'q_perm', 'n_perm_effective',
    'n_genes_requested', 'n_genes_found', 'n_donors', 'n_donors_ref', 'n_donors_test',
    'descriptive_only', 'provisional', 'no_counts', 'passes_effect_gate', 'facet_scope',
)

GENE_SORTS = ('q', 'effect', 'p', 'gene')
MODULE_SORTS = ('q', 'q_perm', 'effect', 'module')
MODULE_Q_BY = ('param', 'perm', 'either')

Q_THRESHOLD = 0.05

#: Combination of the m - u + 1 largest cohort p values in the partial conjunction (``lib/replication.py``):
#: ``simes`` (default: valid under positive dependence, which cohorts that share pathology, cell states and
#: some persons have) or ``fisher`` (independent cohorts; on the 2026-10-05 results it calls 29 instead of 2
#: Braak microglia genes replicated, 23 of them with at most one cohort individually significant).
#: Since v3 (2026-10-06) the cohort p values are the ordinary one-sided limma p values (``PC_P_COLUMN``).
PC_METHOD = 'simes'

#: Column of the cohort p values of the partial conjunction: ``p_value`` (ordinary limma, two-sided, halved to
#: one-sided per direction; v3) or ``p_treat`` (limma treat; v2). The effect gate takes over the role of tau.
PC_P_COLUMN = 'p_value'

#: Disease definitions that are pathology based can be combined across cohorts; clinical or author
#: diagnoses cannot be combined with them (``disease_basis`` key of the dataset .md).
PATHOLOGY_BASES = frozenset({'nia_reagan', 'braak_cerad', 'adnc'})
CLINICAL_BASES = frozenset({'clinical_dx', 'author_dx'})
DISEASE_BASES = tuple(sorted(PATHOLOGY_BASES | CLINICAL_BASES))


def basis_class(basis: Any) -> str:
    """``pathology``, ``clinical`` or ``unknown`` for a ``disease_basis`` value."""
    text = str(basis) if basis is not None else ''
    if text in PATHOLOGY_BASES:
        return 'pathology'
    if text in CLINICAL_BASES:
        return 'clinical'
    return 'unknown'

#: y label of the facet volcano: raw p, unlike the padj axis of the regular DE volcano.
VOLCANO_YLABEL = f'-log10 p (raw); coloured = q < {Q_THRESHOLD:g}'

# Cohorts: which datasets share donors. The table is a DRAFT written next to
# the results (audit section 8.3). A cohort counts once; roles matching this
# pattern do not count (the same rule as ant-facet-test-modules summarize).
COHORT_TABLE_NAME = 'cohort_selection.DRAFT.tsv'
# Optional column ``donor_exclusion`` of the cohort table: free text naming the donors that
# must be absent from the counted run of that dataset (they are persons of another counted
# cohort). The dataset .md carries the machine-readable rule (lib/donor_exclusions.py); a unit
# counts only when its run manifest records that the rule was applied (``donor_exclusion.counted``).
#: Parenthesised curation notes that carry a date (``(... merged into it 2026-10-05)``).
_DATED_NOTE = re.compile(r'\s*\([^()]*\d{4}-\d{2}-\d{2}[^()]*\)')


def public_role(role: str) -> str:
    """The cohort role as shown to users: ``role`` without parenthesised notes that
    contain a date (internal curation history), whitespace tidied."""
    return _DATED_NOTE.sub('', role or '').strip()


EXCLUDED_ROLE_PATTERN = re.compile(r'duplicate|supplement|do not|never|alternative B|T20 only', re.I)

REPLICATION_DEFINITION = (
    'replicated = partial-conjunction r-value < 0.05 (Benjamini and Heller 2008) for "the effect is present in at '
    'least 2 cohorts, same direction". Each cohort contributes exactly one anchor unit per cell type group (a '
    'donor-pooled unit for multi-region cohorts, the whole-cell-type-group stratum otherwise; other units are '
    'supplementary and listed with counted: false). The anchor\'s p value is the ordinary one-sided limma p value of '
    'the tested direction (the two-sided p halved when the observed effect has that sign, 1 minus the half '
    'otherwise). For a gene, the Simes combination of the m - 1 largest of the m cohort p values gives the partial '
    'conjunction p value (x 2 for the two directions); Benjamini-Hochberg over all genes tested in at least 2 cohorts '
    'of the same facet scope and cell type group gives r_value. r_cohorts names the two cohorts with the smallest '
    'p values, the ones that carry it. The effect gate is a separate descriptive condition, not part of the error '
    'control: effect_gate_passed means at least 2 counted cohorts each have a log2 fold change of at least tau in the '
    'replicated direction (tau = log2(1.1), a 10 percent change; Braak log2(1.1)/3 per stage, 10 percent over three '
    'stages; age log2(1.1) per decade); effect_gate_cohorts names them. r_treat is the second tier: the same partial '
    'conjunction on the one-sided limma treat p values (H0: |log2 fold change| <= tau) with its own BH; '
    'replicated_effect = r_treat < 0.05, replicated with at least a 10 percent change at FDR 5 percent. Every tested '
    'cohort counts (m); the unit needs at least 3 replicates per level, or 4 replicates with 3 distinct values for a '
    'numeric facet. k is a descriptive count: cohorts whose anchor has q_value < 0.05 in the same direction. '
    'Descriptive-only units, secondary datasets (reanalyses) and datasets missing from the cohort table are not '
    'counted. The disease facet only combines cohorts whose disease definition is pathology based (disease_basis '
    'nia_reagan, braak_cerad or adnc); clinical or author diagnoses are listed but not counted. Cohorts whose donor '
    'independence was never checked (independence: unknown) are counted and flagged (overlap_unknown). Cell type '
    'labels are counted by their group (lib/celltype_groups.yaml; unmapped labels never count). Facets are tested and '
    'counted per facet scope (human post-mortem, human cells xenografted into mouse); scopes and '
    'their genes are never merged.'
)

DONOR_COUNT_DEFINITION = (
    'n_donors_sum adds the donors analysed in each counted unit (a donor tested in two datasets counts '
    'twice); n_donors_distinct counts each person once across those units, keyed on ant_donor_uid where '
    'the dataset publishes it and on <group_id>:<ant_donor> otherwise (the run manifests record a digest '
    'of each analysed donor key). The fallback cannot join one person across collections, so '
    'n_donors_distinct is an upper bound where uids are missing. n_donors_distinct is null when a counted '
    'unit comes from a run that predates the donor keys (re-run ant-facet-test to fill it).'
)

RANKING_DEFINITION = (
    'genes with r_value < 0.05, ranked by (1) r_value, smallest first; (2) k, the number of cohorts with '
    'q_value < 0.05 in the same direction, highest first; (3) agreement, the fraction of the m tested cohorts whose '
    'effect has that sign (significant or not); (4) the median absolute log2 fold change over the m tested cohorts. '
    'effect_gate_passed and replicated_effect are shown, not used for the ranking.'
)

#: Genes named in a facet headline (the strict-tier list is short on purpose).
STRICT_GENES_LISTED = 10

#: Order in which a stratum's cell type source stands for its group when one
#: cohort has several units in the same group (lower is more direct).
_SOURCE_PRIORITY = {'constant_all_cells': 0, 'group_pool': 1, 'ant_cell_type': 2, 'ant_author_cluster': 4, 'none': 5}
_AUTHOR_SOURCE_PRIORITY = 3


def _donor_counts(unit_rows: pd.DataFrame) -> Dict[str, Optional[int]]:
    """``n_donors_sum`` and ``n_donors_distinct`` over counted unit rows
    (``DONOR_COUNT_DEFINITION``). One row per unit is enough; duplicates are
    dropped by the ``unit`` column when present."""
    rows = unit_rows.drop_duplicates('unit') if 'unit' in unit_rows.columns else unit_rows
    if not len(rows):
        return {'n_donors_sum': None, 'n_donors_distinct': None}
    n_sum = pd.to_numeric(rows['n_donors'], errors='coerce') if 'n_donors' in rows.columns else None
    digests = list(rows['donor_digests']) if 'donor_digests' in rows.columns else [None] * len(rows)
    digests = [d if isinstance(d, (set, frozenset)) else None for d in digests]
    return {
        'n_donors_sum': int(n_sum.sum()) if n_sum is not None and n_sum.notna().all() else None,
        'n_donors_distinct': distinct_count(digests),
    }


def gate_words(facet: Optional[str]) -> str:
    """How the effect-gate threshold reads for a facet: a 10 percent change, over three stages for Braak, per decade for age."""
    if facet == 'braak':
        return '10% over three stages'
    if facet in ('age', 'age_adjusted'):
        return '10% per decade'
    return '10%'


def source_priority(celltype_source: Any) -> int:
    """0 dataset constant, 1 derived group pool (all labels of a cell type group),
    2 per-cell ant_cell_type, 3 author cell type column, 4 author clusters, 5 all cells pooled."""
    source = str(celltype_source).split('@')[0]
    if source.endswith('_author_cluster'):
        return _SOURCE_PRIORITY['ant_author_cluster']
    return _SOURCE_PRIORITY.get(source, _AUTHOR_SOURCE_PRIORITY)


def _known_group_names() -> set:
    from celltype_groups import groups  # noqa: PLC0415
    return set(groups())


def _not_counted_reason(record: Dict[str, Any]) -> Optional[str]:
    """Why a unit does not count towards replication (None when it counts)."""
    if record.get('counted_effective'):
        return None
    if record.get('excluded_secondary'):
        return 'secondary dataset (reanalysis)'
    if record.get('descriptive_only'):
        return 'descriptive only (below the p-value floor of its replicate unit)'
    if not record.get('cohort') or (isinstance(record.get('cohort'), float) and math.isnan(record['cohort'])):
        return 'dataset not in the cohort table'
    if record.get('exclusion_missing'):
        return ('this dataset shares persons with another counted cohort and its run does not record the '
                'donor exclusion (re-run ant-facet-test)')
    if not record.get('counted'):
        return 'cohort role not counted (duplicate, supplement or alternative)'
    if record.get('celltype_group') == UNMAPPED:
        return 'cell type label not mapped to a group'
    if record.get('basis_excluded'):
        return (f'disease definition is {_clean_text(record.get("disease_basis")) or "not recorded"}; replication only '
                f'combines pathology-based definitions (nia_reagan, braak_cerad, adnc)')
    anchor = record.get('anchor_db_id')
    if anchor and anchor != record.get('db_id'):
        return f'supplementary: {anchor} is the anchor of this cohort for this cell type group'
    return 'another unit of this dataset and cell type group has a more direct cell type source'


APOE_LOCUS_GENES = frozenset({'APOE', 'APOC1', 'APOC1P1', 'TOMM40', 'NECTIN2', 'PVRL2'})
APOE_LOCUS_NOTE = ('APOE locus gene: allele-linked expression and read-mapping differences can produce '
                   'an e4 effect here; it need not be downstream biology.')

# Sex positive control: XIST down and Y-linked genes up in males.
SEX_CONTROL_XIST = 'XIST'
SEX_CONTROL_Y_GENES = ('RPS4Y1', 'DDX3Y', 'UTY', 'KDM5D', 'USP9Y', 'EIF1AY', 'ZFY', 'NLGN4Y', 'TXLNGY')
SEX_CONTROL_RULE = ('passed when XIST is lower in males and RPS4Y1 higher in males, both at q < 0.05; '
                    'also counted: Y-linked genes higher in males at q < 0.01')

#: The effect gate (v3b) is a separate served condition, not part of ``replicated``: at least 2 counted cohorts each
#: with a log2 fold change of at least ``facet_rules.gate_tau(facet)`` in the replicated direction.

FACET_INFO = {
    'sex': {'contrast': 'm vs f', 'effect_unit': 'log2 fold change, male minus female', 'provisional': False},
    'disease': {'contrast': 'AD vs control', 'effect_unit': 'log2 fold change, AD minus control',
                'provisional': True},
    'apoe4_carrier': {'contrast': 'any e4 allele vs none (e2 carriers pooled with e3/e3)',
                     'effect_unit': 'log2 fold change, e4 carrier minus non-carrier', 'provisional': False},
    'braak': {'contrast': 'linear trend per Braak stage (not AD vs control)',
              'effect_unit': 'log2 fold change per Braak stage', 'provisional': False},
    'age': {'contrast': 'age, not adjusted for pathology: linear trend per decade (re-detects AD pathology: read '
                        'next to the pathology-adjusted model)',
            'effect_unit': 'log2 fold change per decade of age', 'provisional': False},
    'apoe4_dose': {'contrast': 'linear trend per APOE e4 allele (0, 1, 2 alleles; a sensitivity analysis of the carrier contrast)',
                   'effect_unit': 'log2 fold change per e4 allele', 'provisional': False},
    'braak_threshold': {'contrast': 'Braak stage III or higher vs II or lower (a sensitivity analysis of the linear trend)',
                        'effect_unit': 'log2 fold change, high minus low Braak', 'provisional': False},
    'age_adjusted': {'contrast': 'age, adjusted for pathology (Braak, else pathologic AD): linear trend per decade',
                     'effect_unit': 'log2 fold change per decade of age', 'provisional': False},
    'app_nlgf': {'contrast': 'App NL-G-F knock-in vs control',
                 'effect_unit': 'log2 fold change, nlgf minus control', 'provisional': False},
}
PROVISIONAL_NOTE = ('AD vs control with the current vocabulary; controls differ between studies; '
                    'results will change when the disease vocabulary is settled.')

_SKIP_LINE = re.compile(r'^\[skip\]\s+(\S+):\s*(.+?)\s*$')


#: ``summary(facet_scope=...)`` default: the caller names no catalogue scope.
_UNSET: Any = object()


class FacetResultsError(ValueError):
    """A bad query parameter (maps to HTTP 400)."""


def default_results_dir(store_root: Path) -> Path:
    """``<h5adstore>/_facet_tests``, where the test tools write."""
    return Path(store_root) / RESULTS_DIRNAME


def pool_definitions(results_dir: Path) -> Dict[str, List[str]]:
    """``pool_id -> member db_ids`` of the donor-pooled units in the cohort table.

    A pool is a row whose ``db_id`` is ``pool:<pool_id>`` and whose
    ``pool_members`` column lists the member datasets (comma separated).
    Empty when the table is missing or has no such rows.
    """
    path = Path(results_dir) / COHORT_TABLE_NAME
    if not path.exists():
        return {}
    try:
        frame = pd.read_csv(path, sep='\t', dtype=str).fillna('')
    except Exception as exc:
        log.warning('facet results: cannot read %s: %s', path, exc)
        return {}
    pools: Dict[str, List[str]] = {}
    for row in frame.to_dict(orient='records'):
        db_id = row.get('db_id', '').strip()
        members = [m.strip() for m in row.get('pool_members', '').split(',') if m.strip()]
        if db_id.startswith(POOL_PREFIX) and members:
            pools[db_id[len(POOL_PREFIX):]] = members
    return pools


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


def _numeric(column: pd.Series, dtype: str) -> np.ndarray:
    """``column`` as a numpy array of ``dtype`` (unparseable values become NaN); no index alignment."""
    if column.dtype.kind in 'fiu':
        return column.to_numpy(dtype=dtype)
    return pd.to_numeric(column, errors='coerce').to_numpy(dtype=dtype, na_value=np.nan)


def _split_covariates(value: Any) -> List[str]:
    text = _clean_text(value)
    if not text:
        return []
    return [part.strip() for part in text.split(',') if part.strip()]


def _rename_deprecated_facets(frame: pd.DataFrame) -> pd.DataFrame:
    """Result frame with deprecated facet names (``apoe_carrier``) in its
    ``facet`` column replaced by the current name; results written before the
    rename keep working. Returns the input when nothing needs renaming."""
    if 'facet' in frame.columns and frame['facet'].isin(list(DEPRECATED_FACET_TAGS)).any():
        frame = frame.assign(facet=frame['facet'].replace(DEPRECATED_FACET_TAGS))
    return frame


def _stored_facet_names(facet: str) -> List[str]:
    """The names ``facet`` may carry in result files: the current one plus
    its deprecated predecessors."""
    return [facet] + [old for old, current in DEPRECATED_FACET_TAGS.items() if current == facet]


def _rename_deprecated_in_json(value: Any) -> Any:
    """Manifest data with every string equal to a deprecated facet name (as a
    value or a key) replaced by the current name."""
    if isinstance(value, dict):
        return {DEPRECATED_FACET_TAGS.get(key, key) if isinstance(key, str) else key:
                _rename_deprecated_in_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_rename_deprecated_in_json(item) for item in value]
    if isinstance(value, str):
        return DEPRECATED_FACET_TAGS.get(value, value)
    return value


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


class PreparedDatasets:
    """Datasets resolved by ``FacetResults.prepare``: the resolved dicts and the signature of their result files."""

    def __init__(self, datasets: List[Dict[str, Any]], signature: Tuple):
        self.datasets = datasets
        self.signature = signature


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
        # key -> (signature, value): one entry per key, replaced when its files change (``_cached_build``)
        self._tables: "OrderedDict[Tuple, Tuple[Tuple, Dict[Tuple[str, str], Tuple[pd.DataFrame, pd.DataFrame]]]]" = OrderedDict()
        self._tables_unscoped: Dict[str, Tuple[Any, Tuple[pd.DataFrame, pd.DataFrame]]] = {}
        self._families: "OrderedDict[Tuple, Tuple[Tuple, Dict[str, Dict[str, Any]]]]" = OrderedDict()
        self._cards: "OrderedDict[Tuple, Tuple[Tuple, Dict[str, Any]]]" = OrderedDict()
        self._counting: Dict[Tuple, Tuple[pd.DataFrame, pd.DataFrame]] = {}
        self._key_locks: Dict[Tuple, threading.Lock] = {}
        self._scopes_memo: Optional[Tuple[Tuple, Dict[str, List[str]]]] = None
        self._family_slots = threading.BoundedSemaphore(FAMILY_WORKERS)

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
            frame = _rename_deprecated_facets(pd.read_parquet(path, columns=wanted))
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
            data = _rename_deprecated_in_json(data) if isinstance(data, dict) else None
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

    def pooled_datasets(self) -> List[Dict[str, Any]]:
        """Dataset dicts of the donor-pooled units with a run manifest in ``_pooled/``.

        They look like catalogue datasets to the cross-dataset views: ``db_id``
        ``pool:<pool_id>``, ``group_folder`` ``_pooled``, ``stem`` the pool id.
        """
        folder = self.results_dir / POOLED_DIRNAME
        try:
            names = sorted(p.name for p in folder.glob('*' + SUFFIXES['gene_manifest']))
        except OSError:
            return []
        out = []
        for name in names:
            pool_id = name[:-len(SUFFIXES['gene_manifest'])]
            out.append({'db_id': POOL_PREFIX + pool_id, 'group_folder': POOLED_DIRNAME, 'stem': pool_id,
                        'title': f'{pool_id} (donor-pooled)', 'group': 'pooled', 'is_secondary': False,
                        'sha256': None})
        return out

    def dataset_scope(self, dataset: Dict[str, Any]) -> str:
        """Facet scope of a dataset dict: the scope the run manifest recorded (what was tested), else the
        scope the caller supplied (``facet_scope``, from the catalogue metadata), else human post-mortem
        (runs before 2026-10-06 were human only)."""
        paths = result_paths(self.results_dir, dataset['group_folder'], dataset['stem'])
        manifest = self._read_json(paths['gene_manifest']) or self._read_json(paths['module_manifest']) or {}
        return manifest.get('facet_scope') or dataset.get('facet_scope') or facet_scopes.DEFAULT_SCOPE

    def _with_scope(self, datasets: Sequence[Dict[str, Any]], scope: Optional[str] = None) -> List[Dict[str, Any]]:
        """``datasets`` with ``facet_scope`` resolved (``dataset_scope``); only those of ``scope`` when given."""
        out = []
        for dataset in datasets:
            resolved = self.dataset_scope(dataset)
            if scope is None or resolved == scope:
                out.append({**dataset, 'facet_scope': resolved})
        return out

    def prepare(self, datasets: "Sequence[Dict[str, Any]] | PreparedDatasets") -> "PreparedDatasets":
        """``datasets`` resolved once (donor-pooled units added, facet scope set) with the cache signature.

        Every cross-dataset method accepts the result in place of a list, so a request that asks for several
        facets and scopes stats the result files once instead of once per call.
        """
        if isinstance(datasets, PreparedDatasets):
            return datasets
        resolved = self._with_scope(self._with_pooled(datasets))
        return PreparedDatasets(resolved, self._table_signature(resolved))

    def scopes_with_results(self, facet: str, datasets: "Sequence[Dict[str, Any]] | PreparedDatasets") -> List[str]:
        """Scopes (in ``facet_scopes.SCOPES`` order) in which ``facet`` has at least one tested unit."""
        prepared = self.prepare(datasets)
        with self._lock:
            if self._scopes_memo is not None and self._scopes_memo[0] == prepared.signature:
                by_facet = self._scopes_memo[1]
            else:
                by_facet = None
        if by_facet is None:
            found: Dict[str, set] = {}
            for dataset in prepared.datasets:
                paths = result_paths(self.results_dir, dataset['group_folder'], dataset['stem'])
                if _file_key(paths['genes']) is None:
                    continue
                summary = self.summary(dataset['db_id'], dataset['group_folder'], dataset['stem'],
                                       current_sha256=dataset.get('sha256'), disease_basis=dataset.get('disease_basis'))
                for unit in summary['units']:
                    if unit['tested'] and unit.get('genes'):
                        found.setdefault(unit['facet'], set()).add(dataset['facet_scope'])
            by_facet = {name: [scope for scope in facet_scopes.SCOPES if scope in scopes]
                        for name, scopes in found.items()}
            with self._lock:
                self._scopes_memo = (prepared.signature, by_facet)
        return list(by_facet.get(canonical_facet(facet), []))

    @staticmethod
    def default_scope(facet: str) -> str:
        """Scope answered when a caller names none: human post-mortem (backwards compatible) when the
        facet is allowed there, else the first scope the facet registry allows (``app_nlgf``: mouse)."""
        allowed = facet_scopes.facet_allowed_scopes(facet)
        return facet_scopes.DEFAULT_SCOPE if facet_scopes.DEFAULT_SCOPE in allowed else allowed[0]

    def _with_pooled(self, datasets: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """``datasets`` plus the donor-pooled units; a pool takes the ``disease_basis`` its
        member datasets agree on (None when they differ or declare none)."""
        known = {d['db_id'] for d in datasets}
        basis_of = {d['db_id']: d.get('disease_basis') for d in datasets}
        cohorts = self.cohorts()
        pools = []
        for pool in self.pooled_datasets():
            if pool['db_id'] in known:
                continue
            members = (cohorts.get(pool['db_id']) or {}).get('pool_members') or []
            bases = {basis_of.get(member) for member in members}
            pools.append({**pool, 'disease_basis': bases.pop() if len(bases) == 1 else None})
        return list(datasets) + pools

    def fingerprint(self, group_folder: str, stem: str) -> Tuple:
        """Changes whenever any result file of the dataset changes."""
        return tuple(_file_key(p) for p in result_paths(self.results_dir, group_folder, stem).values())

    # ── summary ────────────────────────────────────────────────────────────

    def summary(self, db_id: str, group_folder: str, stem: str,
                current_sha256: Optional[str] = None, disease_basis: Optional[str] = None,
                facet_scope: Any = _UNSET, facet_exclude_reason: Optional[str] = None,
                scope_reason: Optional[str] = None) -> Dict[str, Any]:
        """Per (facet, cell type stratum) summary of one dataset's results,
        cached until a result file, the cohort table or the run logs change.
        See ``_compute_summary``; adds ``cohort`` and, on sex units,
        ``sex_control``.

        ``disease_basis`` is the dataset's current ``.md`` value; it overrides
        the one recorded in the run manifest (runs before the key existed
        record none) on the summary and on every unit.

        ``facet_scope`` is the dataset's catalogue scope (None: outside every scope). When given, a dataset
        without a tested unit (no run manifest, or a skipped one) reports it instead of the human
        post-mortem default; when omitted the manifest or default scope stays. A tested
        dataset reports the scope its run manifest recorded. ``facet_exclude_reason`` (catalogue,
        ``lib/facet_scopes.facet_exclusion_reason``) is carried on the payload as ``facet_exclude`` /
        ``facet_exclude_reason`` and, for a dataset without results, becomes the ``reason``.
        """
        key = (db_id, group_folder, stem, current_sha256, disease_basis, facet_scope, facet_exclude_reason, scope_reason,
               self.fingerprint(group_folder, stem), _file_key(self.results_dir / COHORT_TABLE_NAME))
        with self._lock:
            cached = self._summary_cache.get(key)
        if cached is not None:
            return cached
        result = json_safe(self._compute_summary(db_id, group_folder, stem, current_sha256))
        result['disease_basis'] = disease_basis or result.get('disease_basis')
        for unit in result['units']:
            unit['disease_basis'] = result['disease_basis']
        if result['status'] != 'tested' and facet_scope is not _UNSET:
            result['facet_scope'] = facet_scope
            for unit in result['units']:
                unit['facet_scope'] = facet_scope
        result['facet_exclude'] = facet_exclude_reason is not None
        result['facet_exclude_reason'] = facet_exclude_reason
        current_reason = facet_exclude_reason or scope_reason
        if current_reason and result['status'] != 'tested':
            result['reason'] = current_reason
        cohort = self.cohorts().get(db_id)
        if cohort:
            cohort = {key: value for key, value in cohort.items() if key != 'role_internal'}
        if cohort and cohort.get('donor_exclusion_required'):
            applied = ((result.get('donor_exclusion') or {}).get('genes') or {}).get('counted') is True
            cohort = {**cohort, 'donor_exclusion_applied': applied}
            if not applied and cohort.get('counted'):
                cohort['counted'] = False
                cohort['exclusion_missing'] = True
        result['cohort'] = cohort
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
                    'flags': {'descriptive_only': None, 'n_lt5': None, 'small_sample': None,
                              'provisional': key[0] == 'disease', 'composition_flag': None,
                              'composition_p': None, 'batch_used': None,
                              'batch_confounded': None},
                    'genes': None, 'modules': [],
                    'analysis_level': None, 'analysis_level_label': None, 'analysis_level_reason': None,
                    'replicate_unit': None, 'replicate_column': None, 'tau_log2': None,
                    'facet_scope': None,
                    'lambda_gc': None, 'inflated': None, 'df_total_median': None, 'covariates_dropped': [],
                    'n_sex_checked': None, 'n_sex_discordant': None, 'age_top_coded': None,
                    'control_basis': None, 'design_subset': None, 'notes': [],
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
            for field in ('n_lt5', 'composition_flag', 'batch_used', 'batch_confounded', 'descriptive_only',
                          'small_sample'):
                if flags.get(field) is None and field in record:
                    flags[field] = _as_bool(record.get(field))
            if flags['composition_p'] is None:
                flags['composition_p'] = _finite(record.get('composition_p'))
            for field in ('analysis_level', 'analysis_level_label', 'analysis_level_reason',
                          'replicate_unit', 'replicate_column', 'facet_scope'):
                if unit[field] is None and record.get(field) is not None:
                    unit[field] = str(record[field])
            if unit['tau_log2'] is None:
                unit['tau_log2'] = _finite(record.get('tau_log2'))
            for field in ('lambda_gc', 'df_total_median'):
                if unit[field] is None:
                    unit[field] = _finite(record.get(field))
            if unit['inflated'] is None and record.get('inflated') is not None:
                unit['inflated'] = bool(record['inflated'])
            for field in ('n_sex_checked', 'n_sex_discordant'):
                if unit[field] is None:
                    unit[field] = _as_int(record.get(field))
            for field in ('age_top_coded',):
                if unit[field] is None:
                    unit[field] = _finite(record.get(field))
            for field in ('control_basis', 'design_subset'):
                if unit[field] is None and record.get(field):
                    unit[field] = str(record[field])
            if not unit['covariates_dropped'] and isinstance(record.get('covariates_dropped'), list):
                unit['covariates_dropped'] = [str(c) for c in record['covariates_dropped']]

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
                for field in ('descriptive_only', 'n_lt5', 'small_sample', 'provisional', 'composition_flag',
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

        run_scope = ((gene_manifest or module_manifest or {}).get('facet_scope')) or facet_scopes.DEFAULT_SCOPE
        for unit in units.values():
            unit['facet_scope'] = unit['facet_scope'] or run_scope
            for field in ('replicate_unit', 'replicate_column'):
                unit[field] = unit[field] or (gene_manifest or module_manifest or {}).get(field)
            flags = unit['flags']
            if flags['descriptive_only'] is None and flags['n_lt5'] is not None:
                # runs before v3 (2026-10-06): n_lt5 meant descriptive only; v3 records both
                flags['descriptive_only'] = bool(flags['n_lt5'])
            if unit['level_ref'] is None and unit['level_counts'] and unit['facet'] != 'braak':
                levels = list(unit['level_counts'])
                if len(levels) == 2:
                    unit['level_ref'], unit['level_test'] = levels[0], levels[1]
            if unit['genes'] is not None:
                unit['genes'].pop('source', None)
            if not unit['tested']:
                unit['reason'] = unit['status_genes'] or unit['status_modules']
            if unit['analysis_level'] is None:
                # runs before the replicate rule: tested units were analysed per donor
                unit['analysis_level'] = replicates.LEVEL_REPLICATE if unit['tested'] else replicates.LEVEL_CELL
                unit['analysis_level_label'] = (replicates.REPLICATE_LABEL if unit['tested']
                                                else replicates.CELL_LEVEL_LABEL)
                unit['analysis_level_reason'] = None if unit['tested'] else unit.get('reason')
            key = (unit['facet'], unit['cell_type'], unit['celltype_source'])
            unit['model'] = build_model(unit, records_by_unit.get(key, {}),
                                        gene_manifest, module_manifest)
            genes_model = (unit['model'] or {}).get('genes') or {}
            unit['covariates_dropped'] = [f"{c['name']}: {c['reason']}" for c in genes_model.get('covariates') or []
                                          if c.get('status') == 'dropped']
            unit['notes'] = unit_notes(unit)

        ordered = sorted(units.values(), key=lambda u: (
            u['celltype_source'], u['cell_type'], _facet_rank(u['facet']), u['facet']))

        runs = {
            'genes': _run_info(gene_manifest, kind='genes'),
            'modules': _run_info(module_manifest, kind='modules'),
        }
        donor_exclusion = {
            'genes': (gene_manifest or {}).get('donor_exclusion'),
            'modules': (module_manifest or {}).get('donor_exclusion'),
        }
        supplementary = self._supplementary_run(group_folder, stem)
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

        run_manifest = gene_manifest or module_manifest or {}
        return {
            'db_id': db_id,
            'status': status,
            'reason': reason,
            'replicate_unit': run_manifest.get('replicate_unit'),
            'replicate_column': run_manifest.get('replicate_column'),
            'replicate_basis': run_manifest.get('replicate_basis'),
            'disease_basis': run_manifest.get('disease_basis'),
            'facet_scope': run_scope,
            'pool': run_manifest.get('pool'),
            'n_units': len(ordered),
            'n_units_tested': n_tested,
            'q_threshold': Q_THRESHOLD,
            'dataset_changed': dataset_changed,
            'files': files,
            'runs': runs,
            'donor_exclusion': donor_exclusion,
            'supplementary_all_donors': supplementary,
            'units': ordered,
        }

    def run_manifest(self, group_folder: str, stem: str, kind: str = 'genes') -> Optional[dict]:
        """The run manifest of a dataset (``kind`` ``genes`` or ``modules``); None when missing or unreadable."""
        paths = result_paths(self.results_dir, group_folder, stem)
        return self._read_json(paths['gene_manifest' if kind == 'genes' else 'module_manifest'])

    def _supplementary_run(self, group_folder: str, stem: str) -> Optional[Dict[str, Any]]:
        """Facts of the full-donor (non-counted) run of a dataset with a donor
        exclusion rule: ``{counted: False, n_donors, n_cells, timestamp_utc}``;
        None when there is none. The results themselves are not served."""
        path = (Path(self.results_dir) / group_folder / SUPPLEMENTARY_DIRNAME
                / f'{stem}{SUFFIXES["gene_manifest"]}')
        manifest = self._read_json(path)
        if not manifest or manifest.get('status') != 'done':
            return None
        return {'counted': False, 'n_donors': manifest.get('n_donors'), 'n_cells': manifest.get('n_cells'),
                'timestamp_utc': manifest.get('timestamp_utc'),
                'path': f'{group_folder}/{SUPPLEMENTARY_DIRNAME}/{stem}{SUFFIXES["gene_manifest"]}'}

    # ── rows ───────────────────────────────────────────────────────────────

    def _filter_unit(self, frame: pd.DataFrame, facet: Optional[str], cell_type: Optional[str],
                     celltype_source: Optional[str]) -> pd.DataFrame:
        facet = canonical_facet(facet) if facet else facet
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
        facet = canonical_facet(facet) if facet else facet
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
        facet = canonical_facet(facet) if facet else facet
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
        parenthesis), ``role`` (public text: the ``role_public`` column, else the
        table role without parenthesised dated curation notes; the raw text is
        ``role_internal``, never served to users), ``cell_class``, ``counted`` (counts towards
        replication), ``status`` (DRAFT), ``note``, ``independence``
        (``checked`` or ``unknown``: whether donor overlap with the other
        cohorts was ever checked; unknown cohorts count but are flagged,
        ``overlap_unknown``) and ``pool_members`` (db_ids pooled per donor
        into this row's unit; only on ``pool:`` rows). Empty when the table
        is missing or unreadable.
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
                    role_public = row.get('role_public', '').strip() or public_role(role)
                    independence = row.get('independence', '').strip().lower() or 'checked'
                    table[db_id] = {
                        'independence': independence,
                        'overlap_unknown': independence == 'unknown',
                        'pool_members': [m.strip() for m in row.get('pool_members', '').split(',') if m.strip()],
                        'cohort': cohort,
                        'cohort_label': cohort.split(' (')[0].strip() or cohort,
                        'role': role_public,
                        'role_internal': role,
                        'cell_class': row.get('cell_class', '').strip(),
                        'counted': bool(cohort) and not EXCLUDED_ROLE_PATTERN.search(role),
                        'status': row.get('status', '').strip() or 'DRAFT',
                        'donor_exclusion_required': row.get('donor_exclusion', '').strip(),
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

    # ── donor keys of tested units ─────────────────────────────────────────

    def unit_donor_digests(self, group_folder: str, stem: str, kind: str = 'genes'
                           ) -> Dict[Tuple[str, str, str], frozenset]:
        """``(facet, cell_type, celltype_source) -> digests`` of the donors
        analysed in each tested unit, from the run manifest (``kind`` genes
        or modules). Units from runs that predate the donor keys are absent.
        The digests stay internal: no endpoint returns them."""
        paths = result_paths(self.results_dir, group_folder, stem)
        manifest = self._read_json(paths['gene_manifest' if kind == 'genes' else 'module_manifest'])
        out: Dict[Tuple[str, str, str], frozenset] = {}
        for record in (manifest or {}).get('units') or []:
            if not isinstance(record, dict) or record.get('status') != 'ok':
                continue
            digests = record.get('donor_key_digests')
            if isinstance(digests, list):
                out[_unit_key(record.get('facet'), record.get('cell_type'),
                              record.get('celltype_source'))] = frozenset(str(d) for d in digests)
        return out

    # ── gene rows of one facet across datasets ─────────────────────────────

    def facet_gene_table(self, facet: str, datasets: Sequence[Dict[str, Any]], scope: Optional[str] = None
                         ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """All gene rows of one facet over every tested dataset (and donor-pooled unit) of one facet scope.

        ``scope`` (``lib/facet_scopes.py``) keeps the datasets of that scope; None keeps every scope (only the
        offline summaries do that: replication is always computed per scope, genes are never merged across scopes).

        The tables of every facet and scope are built in one pass over the result files (each parquet file is
        read once) and cached together; the cache is rebuilt when a result file, the cohort table or a dataset
        attribute changes. Concurrent callers of a cold cache share one build; while a rebuild after a file
        change runs, other callers get the previous tables.

        Returns:
            (rows, units). ``rows`` has ``unit`` (index into ``units``),
            ``gene``, ``logFC``, ``ci_low``, ``ci_high``, ``q_value``,
            ``p_treat`` and ``q_treat`` (NaN for runs before the treat
            columns existed) (compact dtypes). ``units`` has one row per
            tested (dataset, stratum) with ``db_id``, ``title``,
            ``cell_type``, ``celltype_source``, ``n_donors(_ref/_test)``,
            ``level_ref``, ``level_test``, ``descriptive_only``, ``cohort``,
            ``cohort_label``, ``role``, ``counted``, ``independence``,
            ``overlap_unknown``, ``pooled``, ``disease_basis``,
            ``analysis_level``, ``tau_log2``, ``facet_scope`` and ``summary_unit`` (the summary
            unit dict). ``rows`` also carries ``p_value`` (the ordinary two-sided limma p).
        """
        facet = canonical_facet(facet) if facet else facet
        prepared = self.prepare(datasets)
        tables = self._cached_build(self._tables, ('tables',), prepared.signature,
                                    lambda: self._build_gene_tables(prepared.datasets), max_entries=1)
        if scope is not None:
            return tables.get((facet, scope)) or self._empty_gene_table()
        with self._lock:
            merged = self._tables_unscoped.get(facet)
            if merged is not None and merged[0] is tables:
                return merged[1]
        parts = [tables[key] for key in tables if key[0] == facet]
        if len(parts) == 1:
            result = parts[0]
        elif parts:
            result = (pd.concat([rows for rows, _ in parts], ignore_index=True).astype(
                          {'gene': 'category', 'gene_upper': 'category'}),
                      pd.concat([units for _, units in parts], ignore_index=True))
        else:
            result = self._empty_gene_table()
        with self._lock:
            self._tables_unscoped[facet] = (tables, result)
        return result

    def _table_signature(self, datasets: Sequence[Dict[str, Any]]) -> Tuple:
        """Cache key of the gene tables: changes when a result file, the cohort table or a dataset attribute
        that enters the tables changes. ``datasets`` are already resolved (``_with_scope(_with_pooled(...))``)."""
        return (_file_key(self.results_dir / COHORT_TABLE_NAME), tuple(
            (d['db_id'], bool(d.get('is_secondary')), d.get('disease_basis'), d['facet_scope'], d.get('modality'),
             self.fingerprint(d['group_folder'], d['stem'])) for d in datasets))

    def _cached_build(self, store: "OrderedDict[Tuple, Tuple[Tuple, Any]]", key: Tuple, signature: Tuple,
                      build: Any, max_entries: int) -> Any:
        """Value of ``key`` in ``store`` for ``signature``; built once however many threads ask.

        ``store`` maps ``key -> (signature, value)``, so an entry is replaced (not accumulated) when its
        files change. A cold key is built by the first caller while the others wait on a per-key lock (no
        global lock is held during the build, so unrelated requests keep running). When a rebuild after a file
        change is already running, callers get the previous value instead of queueing behind it.
        """
        lock_key = (id(store), key)
        with self._lock:
            entry = store.get(key)
            if entry is not None and entry[0] == signature:
                store.move_to_end(key)
                return entry[1]
            key_lock = self._key_locks.setdefault(lock_key, threading.Lock())
        if entry is not None:
            if not key_lock.acquire(blocking=False):
                return entry[1]
        else:
            key_lock.acquire()
        try:
            with self._lock:
                entry = store.get(key)
                if entry is not None and entry[0] == signature:
                    return entry[1]
            value = build()
            with self._lock:
                store[key] = (signature, value)
                store.move_to_end(key)
                while len(store) > max_entries:
                    store.popitem(last=False)
            return value
        finally:
            key_lock.release()

    @staticmethod
    def _empty_gene_table() -> Tuple[pd.DataFrame, pd.DataFrame]:
        rows = pd.DataFrame({'unit': pd.Series(dtype='int32'), 'gene': pd.Series(dtype='category'),
                             'logFC': pd.Series(dtype='float32'), 'ci_low': pd.Series(dtype='float32'),
                             'ci_high': pd.Series(dtype='float32'), 'p_value': pd.Series(dtype='float64'),
                             'p_treat_strong': pd.Series(dtype='float64'),
                             'persists_after_pathology_adjustment': pd.Series(dtype='float32'),
                             'q_value': pd.Series(dtype='float64'),
                             'p_treat': pd.Series(dtype='float64'), 'q_treat': pd.Series(dtype='float64'),
                             'gene_upper': pd.Series(dtype='category')})
        units = pd.DataFrame([], columns=UNIT_TABLE_COLUMNS)
        return rows, units

    def _read_dataset_tables(self, dataset: Dict[str, Any], cohorts: Dict[str, Dict[str, Any]]
                             ) -> Dict[str, Tuple[List[Dict[str, Any]], List[pd.DataFrame]]]:
        """Gene rows of every tested facet of one dataset: ``facet -> (unit dicts, row frames)``.
        The parquet file is read once, whatever the number of facets."""
        paths = result_paths(self.results_dir, dataset['group_folder'], dataset['stem'])
        if _file_key(paths['genes']) is None:
            return {}
        summary = self.summary(dataset['db_id'], dataset['group_folder'], dataset['stem'],
                               current_sha256=dataset.get('sha256'), disease_basis=dataset.get('disease_basis'))
        tested: Dict[str, Dict[Tuple[str, str], Dict[str, Any]]] = {}
        for unit in summary['units']:
            if unit['tested'] and unit.get('genes'):
                tested.setdefault(unit['facet'], {})[(unit['cell_type'], unit['celltype_source'])] = unit
        if not tested:
            return {}
        try:
            import pyarrow.parquet as pq
            available = set(pq.read_schema(paths['genes']).names)
            wanted = [c for c in ('facet', 'cell_type', 'celltype_source', 'gene', 'logFC', 'ci_low',
                                  'ci_high', 'p_value', 'q_value', 'p_treat', 'q_treat',
                                  'persists_after_pathology_adjustment', 'p_treat_strong') if c in available]
            stored = {name for facet in tested for name in _stored_facet_names(facet)}
            # dictionary-encoded strings: grouping and gene lookup work on integer codes
            frame = pq.read_table(
                paths['genes'], columns=wanted, filters=[('facet', 'in', sorted(stored))],
                read_dictionary=[c for c in ('facet', 'cell_type', 'celltype_source', 'gene') if c in wanted]
            ).to_pandas()
            for column in ('facet', 'cell_type', 'celltype_source', 'gene'):
                if column in frame.columns and not isinstance(frame[column].dtype, pd.CategoricalDtype):
                    frame[column] = frame[column].astype('category')
            if frame['facet'].cat.categories.isin(list(DEPRECATED_FACET_TAGS)).any():
                frame['facet'] = pd.Categorical(frame['facet'].astype(object).replace(DEPRECATED_FACET_TAGS))
        except Exception as exc:
            log.warning('facet results: cannot read %s: %s', paths['genes'], exc)
            return {}
        for column in ('p_value', 'p_treat', 'q_treat', 'persists_after_pathology_adjustment', 'p_treat_strong'):
            if column not in frame.columns:
                frame[column] = np.nan
        cohort = summary.get('cohort') or cohorts.get(dataset['db_id']) or {}
        digests = self.unit_donor_digests(dataset['group_folder'], dataset['stem'])
        out: Dict[str, Tuple[List[Dict[str, Any]], List[pd.DataFrame]]] = {}
        for facet, facet_frame in frame.groupby('facet', sort=False, observed=True):
            facet = str(facet)
            units_of_facet = tested.get(facet)
            if not units_of_facet:
                continue
            unit_dicts: List[Dict[str, Any]] = []
            parts: List[pd.DataFrame] = []
            for (cell_type, celltype_source), group in facet_frame.groupby(['cell_type', 'celltype_source'], sort=False, observed=True):
                unit = units_of_facet.get((str(cell_type), str(celltype_source)))
                if unit is None:
                    continue
                unit_dicts.append({
                    'db_id': dataset['db_id'], 'title': dataset.get('title') or dataset['db_id'],
                    'cell_type': str(cell_type), 'celltype_source': str(celltype_source),
                    'n_donors': unit.get('n_donors'), 'n_donors_ref': unit.get('n_donors_ref'),
                    'n_donors_test': unit.get('n_donors_test'), 'level_ref': unit.get('level_ref'),
                    'level_test': unit.get('level_test'),
                    'descriptive_only': bool((unit.get('flags') or {}).get('descriptive_only')),
                    'cohort': cohort.get('cohort') or None, 'cohort_label': cohort.get('cohort_label') or None,
                    'role': cohort.get('role') or None, 'counted': bool(cohort.get('counted')),
                    'independence': cohort.get('independence') or 'checked',
                    'overlap_unknown': bool(cohort.get('overlap_unknown')),
                    'pooled': str(dataset['db_id']).startswith(POOL_PREFIX),
                    'disease_basis': summary.get('disease_basis'),
                    'analysis_level': unit.get('analysis_level'),
                    'tau_log2': unit.get('tau_log2'),
                    'exclusion_missing': bool(cohort.get('exclusion_missing')),
                    'is_secondary': bool(dataset.get('is_secondary')),
                    'celltype_group': group_of(cell_type),
                    'facet_scope': dataset['facet_scope'],
                    'summary_unit': unit,
                    'donor_digests': digests.get(_unit_key(facet, cell_type, celltype_source)),
                    'modality': dataset.get('modality'), 'modality_note': dataset.get('modality_note'),
                })
                parts.append(pd.DataFrame({
                    'gene': group['gene'].array,
                    'logFC': _numeric(group['logFC'], 'float32'),
                    'ci_low': _numeric(group['ci_low'], 'float32'),
                    'ci_high': _numeric(group['ci_high'], 'float32'),
                    'p_value': _numeric(group['p_value'], 'float64'),
                    'q_value': _numeric(group['q_value'], 'float64'),
                    'p_treat_strong': _numeric(group['p_treat_strong'], 'float64'),
                    'persists_after_pathology_adjustment': _numeric(
                        group['persists_after_pathology_adjustment'], 'float32'),
                    'p_treat': _numeric(group['p_treat'], 'float64'),
                    'q_treat': _numeric(group['q_treat'], 'float64'),
                }))
            if unit_dicts:
                out[facet] = (unit_dicts, parts)
        return out

    @staticmethod
    def _gene_vocabulary(gene_arrays: Sequence[pd.Categorical]) -> Tuple[pd.Index, Dict[int, np.ndarray]]:
        """One gene vocabulary for all result files, and per distinct category list the map from its codes to it.

        The parts of one file share their category list, so the union is taken once per file, not once
        per (facet, cell type) part; the maps are keyed by ``id`` of the category ``Index``.
        """
        position: Dict[str, int] = {}
        names: List[str] = []
        code_maps: Dict[int, np.ndarray] = {}
        previous: Optional[Tuple[np.ndarray, np.ndarray]] = None
        for array in gene_arrays:
            categories = array.dtype.categories
            if id(categories) in code_maps:
                continue
            symbols = categories.to_numpy(dtype=object)
            if previous is not None and len(symbols) == len(previous[0]) and (symbols == previous[0]).all():
                code_maps[id(categories)] = previous[1]
                continue
            codes = np.empty(len(symbols), dtype='int64')
            for index, symbol in enumerate(symbols):
                code = position.get(symbol)
                if code is None:
                    code = position[symbol] = len(names)
                    names.append(symbol)
                codes[index] = code
            code_maps[id(categories)] = codes
            previous = (symbols, codes)
        return pd.Index(names, dtype=object), code_maps

    def _build_gene_tables(self, datasets: Sequence[Dict[str, Any]]
                           ) -> Dict[Tuple[str, str], Tuple[pd.DataFrame, pd.DataFrame]]:
        """Gene tables of every (facet, scope): every result file is read once, in parallel."""
        cohorts = self.cohorts()
        with ThreadPoolExecutor(max_workers=READ_WORKERS) as pool:
            per_dataset = list(pool.map(lambda dataset: self._read_dataset_tables(dataset, cohorts), datasets))
        unit_rows: Dict[str, List[Dict[str, Any]]] = {}
        parts: Dict[str, List[pd.DataFrame]] = {}
        for facet_tables in per_dataset:
            for facet, (unit_dicts, frames) in facet_tables.items():
                numbered = unit_rows.setdefault(facet, [])
                for unit_dict, frame in zip(unit_dicts, frames):
                    frame.insert(0, 'unit', np.full(len(frame), len(numbered), dtype='int32'))
                    numbered.append({'unit': len(numbered), **unit_dict})
                    parts.setdefault(facet, []).append(frame)
        vocabulary, code_maps = self._gene_vocabulary([part['gene'].array for frames in parts.values() for part in frames])
        upper_codes, upper_names = pd.factorize(vocabulary.str.upper())
        gene_dtype = pd.CategoricalDtype(vocabulary)
        upper_dtype = pd.CategoricalDtype(upper_names)
        tables: Dict[Tuple[str, str], Tuple[pd.DataFrame, pd.DataFrame]] = {}
        for facet, facet_units in unit_rows.items():
            units = pd.DataFrame(facet_units, columns=UNIT_TABLE_COLUMNS)
            gene_codes = np.concatenate([code_maps[id(part['gene'].array.dtype.categories)][part['gene'].array.codes]
                                         for part in parts[facet]])
            rows = pd.concat([part.drop(columns='gene') for part in parts[facet]], ignore_index=True)
            rows['gene'] = pd.Categorical.from_codes(gene_codes, dtype=gene_dtype)
            rows['gene_upper'] = pd.Categorical.from_codes(upper_codes[gene_codes], dtype=upper_dtype)
            for scope in sorted(set(units['facet_scope'])):
                scope_units = units[units['facet_scope'] == scope]
                tables[(facet, scope)] = (rows if len(scope_units) == len(units)
                                          else rows[rows['unit'].isin(scope_units['unit'])], scope_units)
        return tables

    def _counted_units(self, units: pd.DataFrame, exclude_secondary: bool, facet: Optional[str]) -> pd.DataFrame:
        """``counting_units`` of a cached unit table, remembered (the table object is stable until its files change)."""
        key = (id(units), bool(exclude_secondary), facet)
        with self._lock:
            hit = self._counting.get(key)
        if hit is not None and hit[0] is units:
            return hit[1]
        result = self.counting_units(units, exclude_secondary, facet)
        with self._lock:
            if len(self._counting) >= 4 * FAMILY_CACHE_ENTRIES:
                self._counting.clear()
            self._counting[key] = (units, result)
        return result

    def warm(self, datasets: Sequence[Dict[str, Any]], exclude_secondary: bool = True) -> int:
        """Build the gene tables and every replication family so the first request is a cache hit.

        Meant for a background thread at server start; safe to call concurrently with requests (they
        share the builds). Returns the number of families built.
        """
        prepared = self.prepare(datasets)
        gene_classes.flags('TREM2')  # parses the GENCODE file once
        self.facet_gene_table(FACET_ORDER[0], prepared, facet_scopes.DEFAULT_SCOPE)
        wanted = [(facet, scope) for facet in FACET_ORDER for scope in self.scopes_with_results(facet, prepared)]
        with ThreadPoolExecutor(max_workers=FAMILY_WORKERS) as pool:
            list(pool.map(lambda pair: self.replication_families(pair[0], prepared, exclude_secondary, pair[1]), wanted))
        return len(wanted)

    @staticmethod
    def counting_units(units: pd.DataFrame, exclude_secondary: bool = True,
                       facet: Optional[str] = None) -> pd.DataFrame:
        """``units`` with the replication bookkeeping added.

        Exactly one dataset is the anchor of a cohort for a cell type group:
        a donor-pooled unit beats a single file, then a role starting with
        ``anchor``, then the most direct cell type source, then the most
        donors. Only the units of that dataset with the most direct source
        count (several equal strata of one dataset share the cohort's one
        vote: their p values are Bonferroni adjusted by ``n_siblings``).

        Columns added: ``excluded_secondary`` (a secondary dataset left out
        because ``exclude_secondary``), ``basis_excluded`` (facet ``disease``
        only: the dataset's ``disease_basis`` is not pathology based),
        ``eligible`` (counted cohort, not descriptive only, not an excluded
        secondary, group not unmapped, basis compatible), ``anchor_db_id``
        (the cohort's anchor dataset for the group), ``representative``
        (eligible and of the anchor dataset with its most direct source),
        ``n_siblings`` and ``counted_effective`` (= representative).
        """
        units = units.copy()
        if 'is_secondary' not in units.columns:
            units['is_secondary'] = False
        if 'celltype_group' not in units.columns:
            units['celltype_group'] = units['cell_type'].map(group_of) if 'cell_type' in units.columns else UNMAPPED
        if 'celltype_source' not in units.columns:
            units['celltype_source'] = 'ant_cell_type'
        if 'db_id' not in units.columns:
            units['db_id'] = units.index.astype(str)
        if 'pooled' not in units.columns:
            units['pooled'] = units['db_id'].astype(str).str.startswith(POOL_PREFIX)
        if 'role' not in units.columns:
            units['role'] = None
        if 'n_donors' not in units.columns:
            units['n_donors'] = 0
        if 'disease_basis' not in units.columns:
            units['disease_basis'] = None
        units['excluded_secondary'] = units['is_secondary'].astype(bool) & bool(exclude_secondary)
        if facet == 'disease':
            units['basis_excluded'] = units['disease_basis'].map(basis_class) != 'pathology'
        else:
            units['basis_excluded'] = False
        units['eligible'] = (units['counted'].astype(bool) & ~units['descriptive_only'].astype(bool)
                             & units['cohort'].notna() & ~units['excluded_secondary']
                             & (units['celltype_group'] != UNMAPPED) & ~units['basis_excluded'])
        units['_priority'] = units['celltype_source'].map(source_priority).where(units['eligible'])
        units['anchor_db_id'] = None
        units['representative'] = False
        units['n_siblings'] = 1
        eligible = units[units['eligible']]
        if len(eligible):
            datasets = eligible.assign(
                _not_pooled=~eligible['pooled'].astype(bool),
                _not_anchor_role=~eligible['role'].fillna('').str.lower().str.startswith('anchor'),
                _donors=-pd.to_numeric(eligible['n_donors'], errors='coerce').fillna(0)
            ).groupby(['cohort', 'celltype_group', 'db_id'], dropna=False).agg(
                _not_pooled=('_not_pooled', 'min'), _not_anchor_role=('_not_anchor_role', 'min'),
                _best=('_priority', 'min'), _donors=('_donors', 'min')).reset_index()
            datasets = datasets.sort_values(['cohort', 'celltype_group', '_not_pooled', '_not_anchor_role',
                                             '_best', '_donors', 'db_id'])
            anchors = datasets.drop_duplicates(['cohort', 'celltype_group'], keep='first')
            best_priority = {(c, g): (d, b) for c, g, d, b in zip(
                anchors['cohort'], anchors['celltype_group'], anchors['db_id'], anchors['_best'])}
            for position, (cohort, group, db_id, priority, is_eligible) in enumerate(zip(
                    units['cohort'], units['celltype_group'], units['db_id'], units['_priority'], units['eligible'])):
                if not is_eligible:
                    continue
                anchor_db_id, anchor_priority = best_priority[(cohort, group)]
                units.iat[position, units.columns.get_loc('anchor_db_id')] = anchor_db_id
                if db_id == anchor_db_id and priority == anchor_priority:
                    units.iat[position, units.columns.get_loc('representative')] = True
            siblings = units[units['representative']].groupby(['db_id', 'celltype_group']).size()
            units['n_siblings'] = [int(siblings.get((d, g), 1)) if r else 1 for d, g, r in
                                   zip(units['db_id'], units['celltype_group'], units['representative'])]
        units['counted_effective'] = units['representative'].astype(bool)
        return units.drop(columns='_priority')

    @staticmethod
    def _replication(unit_rows: pd.DataFrame, family: Optional[pd.Series] = None,
                     facet: Optional[str] = None) -> Dict[str, Any]:
        """Replication entry over the rows of one gene in one cell type group.

        ``unit_rows`` needs ``counted``, ``descriptive_only``, ``cohort``,
        ``q_value``, ``logFC``; ``representative`` and ``n_siblings`` (from
        ``counting_units``) are used when present. ``family`` is the gene's
        row of the replicability table (``replication_families``); without it
        only the legacy count (q_value < 0.05, same sign) is filled.

        The legacy fields (``cohorts_tested``, ``cohorts_sig_same_sign``,
        ``direction``, ``cohorts_up``, ``cohorts_down``, ``sign_discordant``,
        ``legacy_badge``) keep their meaning for one release: a cohort counts
        when its anchor has q_value < 0.05. The statistic is ``r_value``.
        """
        rows = unit_rows
        if 'representative' not in rows.columns:
            rows = rows.assign(representative=rows['counted'].astype(bool) & ~rows['descriptive_only'].astype(bool)
                               & rows['cohort'].notna())
        if 'n_siblings' not in rows.columns:
            rows = rows.assign(n_siblings=1)
        eligible = rows[rows['representative'].astype(bool) & rows['q_value'].notna()]
        eligible = eligible.assign(q_adj=(eligible['q_value'] * eligible['n_siblings']).clip(upper=1.0))
        cohorts_tested = sorted(set(eligible['cohort']))
        donors = _donor_counts(eligible)
        significant = eligible[eligible['q_adj'] < Q_THRESHOLD]
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
        m_legacy, k_legacy = len(cohorts_tested), len(same_sign)
        if m_legacy == 0:
            legacy = 'not tested in a counted cohort'
        elif m_legacy == 1:
            legacy = ('one cohort, not replicated' + (f' (q < 0.05, {direction})' if k_legacy else ' (not significant)'))
        else:
            legacy = f'replicates in {k_legacy} of {m_legacy} independent cohorts' + (f' ({direction})' if direction else '')
        legacy += '; sign discordant' if up and down else ''
        entry: Dict[str, Any] = {
            'cohorts_tested': m_legacy,
            'cohorts_sig_same_sign': k_legacy,
            'direction': direction,
            'sign_discordant': bool(up) and bool(down),
            'cohorts_up': up,
            'cohorts_down': down,
            'legacy_badge': legacy,
            **donors,
        }
        entry.update(FacetResults._replicability_fields(family, m_legacy, eligible, facet))
        entry.update(FacetResults._call_fields(entry))
        entry['badge'] = FacetResults._badge(entry)
        return entry

    @staticmethod
    def _call_fields(entry: Dict[str, Any]) -> Dict[str, Any]:
        """The call a view draws from a replication entry (2.45).

        ``call``: ``replicated`` (r < 0.05 over at least two cohorts, direction-consistent), ``not_replicated``
        (two or more cohorts, r >= 0.05) or ``not_tested`` (fewer than two cohorts; ``nt_label`` says ``1 cohort`` or
        ``not tested``). ``gate``: the effect gate passed (the "at least 10% in 2 cohorts" pill); ``strict``: also
        passes the strict minimum-effect test (the star). ``k_over_m`` reads ``1/4``; ``cohorts_label`` reads
        ``3/4 cohorts, 1 small``. There is no "small effect" call: the word is never used for effects.
        """
        m = entry.get('m') or 0
        k = entry.get('k')
        r_value = entry.get('r_value')
        if m >= 2 and r_value is not None:
            call = 'replicated' if r_value < replication.R_THRESHOLD else 'not_replicated'
        else:
            call = 'not_tested'
        tested = max(m, entry.get('cohorts_tested') or 0)
        nt_label = None if call != 'not_tested' else ('1 cohort' if tested == 1 else 'not tested')
        small = entry.get('n_small') or 0
        shown = m if m else tested
        label = f'{k}/{m} cohorts' if k is not None and m else (f'{shown} cohort' + ('s' if shown != 1 else ''))
        if small:
            label += f', {small} small'
        gate = bool(call == 'replicated' and entry.get('effect_gate_passed'))
        return {'call': call, 'nt_label': nt_label, 'gate': gate,
                'strict': bool(gate and entry.get('replicated_effect')),
                'k_over_m': f'{k}/{m}' if k is not None and m else None, 'cohorts_label': label}

    @staticmethod
    def _replicability_fields(family: Optional[pd.Series], m_legacy: int, eligible: pd.DataFrame,
                              facet: Optional[str] = None) -> Dict[str, Any]:
        """r_value, k, m, the effect gate, the second tier and flags of one gene from its family row."""
        overlap = sorted({str(label) for label, flag in zip(
            eligible.get('cohort_label', eligible['cohort']), eligible.get('overlap_unknown', [False] * len(eligible)))
            if flag}) if len(eligible) else []
        label_of = dict(zip(eligible['cohort'], eligible.get('cohort_label', eligible['cohort']))) if len(eligible) else {}
        n_small = int((pd.to_numeric(eligible['n_donors'], errors='coerce')
                       < facet_rules.SMALL_COHORT_REPLICATES).sum()) if len(eligible) and 'n_donors' in eligible else 0
        small_cohorts = sorted({str(label_of.get(cohort) or cohort) for cohort, donors in zip(
            eligible['cohort'], pd.to_numeric(eligible['n_donors'], errors='coerce'))
            if donors < facet_rules.SMALL_COHORT_REPLICATES}) if len(eligible) and 'n_donors' in eligible else []

        def labels(cohorts: Any) -> List[str]:
            return [str(label_of.get(c) or c) for c in (cohorts if isinstance(cohorts, list) else [])]

        base = {'overlap_unknown': bool(overlap), 'cohorts_overlap_unknown': overlap,
                'replication_u': replication.DEFAULT_U, 'replication_method': PC_METHOD, 'replication_p': PC_P_COLUMN,
                'effect_gate_unit': gate_words(facet), 'small_cohort_replicates': facet_rules.SMALL_COHORT_REPLICATES}
        if family is None:
            return {'r_value': None, 'p_pc': None, 'k': None, 'k_treat': None, 'm': 0, 'replicated': None,
                    'r_significant': None, 'effect_gate_passed': None, 'effect_gate_cohorts': [],
                    'n_cohorts_ge_tau': None, 'median_logFC': None, 'effect_gate_tau': None, 'r_treat': None,
                    'replicated_effect': None, 'r_strong': None, 'replicated_strong': None, 'n_small': n_small,
                    'small_cohorts': small_cohorts, 'r_cohorts': [], 'r_direction': None, **base}
        has_r = _finite(family.get('r_value')) is not None
        has_treat = _finite(family.get('r_treat')) is not None
        return {
            'r_value': _finite(family.get('r_value')),
            'p_pc': _finite(family.get('p_pc')),
            'k': _as_int(family.get('k')),
            'k_treat': _as_int(family.get('k_treat')),
            'm': _as_int(family.get('m')) or 0,
            'replicated': bool(family.get('replicated')) if has_r else None,
            'r_significant': bool(family.get('r_significant')) if has_r else None,
            'effect_gate_passed': bool(family.get('effect_gate_passed')) if has_r else None,
            'effect_gate_cohorts': labels(family.get('effect_gate_cohorts')),
            'n_cohorts_ge_tau': _as_int(family.get('n_cohorts_ge_tau')),
            'median_logFC': _finite(family.get('median_logFC')),
            'effect_gate_tau': _finite(family.get('effect_gate_tau')),
            'r_treat': _finite(family.get('r_treat')),
            'replicated_effect': bool(family.get('replicated_effect')) if has_treat else None,
            'r_strong': _finite(family.get('r_strong')),
            'replicated_strong': bool(family.get('replicated_strong')) if has_treat else None,
            'n_small': n_small,
            'small_cohorts': small_cohorts,
            'r_cohorts': labels(family.get('r_cohorts')),
            'r_direction': family.get('r_direction') if isinstance(family.get('r_direction'), str) else None,
            **base,
        }

    @staticmethod
    def _badge(entry: Dict[str, Any]) -> str:
        """Badge text of one replication entry."""
        m, k, r_value = entry.get('m') or 0, entry.get('k'), entry.get('r_value')
        if not m and entry.get('cohorts_tested'):
            return entry['legacy_badge'] + ' (no treat statistics in this run: re-run ant-facet-test)'
        if m == 0:
            return 'not tested in a counted cohort'
        k_text = f'{k} of {m} cohorts individually q < 0.05' if k is not None else 'k not available'
        if m == 1:
            return f'one cohort, not replicated ({k_text})'
        direction = f' ({entry["r_direction"]})' if entry.get('r_direction') else ''
        if r_value is None:
            return f'replication not assessable ({k_text})'
        if r_value >= replication.R_THRESHOLD:
            text = f'not replicated: r = {r_value:.2g}; {k_text}'
        else:
            carriers = ' and '.join(entry.get('r_cohorts') or [])
            carried = f', carried by {carriers}' if carriers else ''
            unit = entry.get('effect_gate_unit') or '10%'
            if entry.get('effect_gate_passed'):
                gate_names = ' and '.join((entry.get('effect_gate_cohorts') or [])[:2])
                text = (f'replicated in at least 2 of {m} cohorts{direction}: r = {r_value:.2g}{carried}; {k_text}; '
                        f'effect of at least {unit} in {gate_names}')
                if entry.get('replicated_effect'):
                    text += f'; also replicated with at least {unit} (r_treat = {entry["r_treat"]:.2g})'
                    if entry.get('replicated_strong'):
                        text += f'; and with at least 25% (r_strong = {entry["r_strong"]:.2g})'
            else:
                text = (f'direction replicates{direction} (r = {r_value:.2g}{carried}); effect below {unit} in fewer '
                        f'than 2 cohorts; {k_text}')
        if entry.get('n_small'):
            text += f'; m includes {entry["n_small"]} cohorts below {entry.get("small_cohort_replicates", 20)} donors'
        if entry.get('overlap_unknown'):
            text += '; donor overlap unknown for ' + ', '.join(entry['cohorts_overlap_unknown'])
        return text

    def replication_families(self, facet: str, datasets: Sequence[Dict[str, Any]],
                             exclude_secondary: bool = True, scope: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """Replicability of every gene of one facet in one facet scope, per cell type group.

        Per group the anchor units of the counted cohorts are combined
        (``lib/replication.py``): the ordinary one-sided limma p of each cohort's anchor in
        both directions, partial conjunction (u = 2, Simes) and BH over the
        genes tested in at least two cohorts; ``replicated`` also needs the effect gate.
        ``scope`` defaults to ``default_scope(facet)``.

        Returns:
            ``group -> {frame, per_cohort, units}``. ``frame`` is indexed by
            upper-case gene symbol with ``gene``, ``m`` (cohorts with a treat
            p), ``k`` (cohorts with q_treat < 0.05 in the majority direction),
            ``k_up``, ``k_down``, ``p_pc``, ``r_value``, ``r_direction``,
            ``agreement``, ``median_abs_logFC``, ``median_logFC`` (signed median effect over the
            tested cohorts), ``effect_gate_passed``, ``effect_gate_tau``, ``r_significant`` (r < 0.05) and
            ``replicated`` (r < 0.05 and the effect gate); ``per_cohort`` has one row
            per (gene, cohort); ``units`` the counted units of the group.
        """
        facet = canonical_facet(facet) if facet else facet
        scope = scope or self.default_scope(facet)
        prepared = self.prepare(datasets)

        def build() -> Dict[str, Dict[str, Any]]:
            rows, units = self.facet_gene_table(facet, prepared, scope)
            families: Dict[str, Dict[str, Any]] = {}
            if len(units):
                units = self._counted_units(units, exclude_secondary, facet)
                counted = units[units['counted_effective']]
                with self._family_slots:
                    for group, group_units in counted.groupby('celltype_group'):
                        sub = rows[rows['unit'].isin(group_units['unit'])]
                        families[group] = self._family(sub, group_units, facet)
            return families

        return self._cached_build(self._families, (facet, scope, bool(exclude_secondary)), prepared.signature, build,
                                  max_entries=FAMILY_CACHE_ENTRIES)

    @staticmethod
    def _family(sub: pd.DataFrame, group_units: pd.DataFrame, facet: Optional[str] = None) -> Dict[str, Any]:
        """Replicability table of one cell type group (see ``replication_families``).

        ``r_value``: partial conjunction (u = 2, Simes) of the ordinary one-sided limma p values, BH over the
        genes of the family (the cohort p of a direction is the two-sided p halved when the observed effect has that
        sign, 1 minus the half otherwise). ``replicated`` is r < 0.05: the effect is present, in the same
        direction, in at least 2 cohorts. ``r_cohorts`` are the two cohorts with the smallest one-sided p in that
        direction, the ones that carry the r-value.

        The effect gate is a separate, descriptive condition (it is not part of the error control): at least 2
        cohorts each with a log2 fold change of at least tau (``facet_rules.gate_tau``) in the replicated direction
        (signed), ``effect_gate_passed`` with ``effect_gate_cohorts``. ``r_treat`` is the second tier: the same
        partial conjunction on the one-sided limma treat p values (H0: |log2FC| <= tau) with its own BH;
        ``replicated_effect`` = r_treat < 0.05, replicated with at least a tau-sized change at FDR 5 percent;
        ``r_strong`` / ``replicated_strong`` the same with a 25 percent change (``facet_rules.strong_tau``).
        ``k`` counts cohorts with q_value < 0.05 in the direction (``k_treat``: q_treat).
        """
        columns = ['unit', 'cohort', 'n_siblings']
        sub = sub.merge(group_units[columns], on='unit', how='left')
        # group on integer gene codes (string keys make every groupby and pivot below several times slower);
        # the symbols are put back at the end
        upper = pd.Categorical(sub['gene_upper'])
        display = pd.Categorical(sub['gene'])
        sub = sub.assign(gene_key=upper.codes, gene=display.codes)
        log_fc = sub['logFC'].astype(float)
        p_two = sub['p_value']
        half = p_two / 2.0
        sub['p_up'] = np.where(p_two.isna(), np.nan, np.where(log_fc >= 0, half, 1.0 - half))
        sub['p_down'] = np.where(p_two.isna(), np.nan, np.where(log_fc <= 0, half, 1.0 - half))
        p_treat = sub['p_treat']
        p_strong = sub['p_treat_strong'] if 'p_treat_strong' in sub else pd.Series(np.nan, index=sub.index)
        sub['ps_up'] = np.where(p_strong.isna(), np.nan, np.where(log_fc > 0, p_strong, 1.0))
        sub['ps_down'] = np.where(p_strong.isna(), np.nan, np.where(log_fc < 0, p_strong, 1.0))
        sub['pt_up'] = np.where(p_treat.isna(), np.nan, np.where(log_fc > 0, p_treat, 1.0))
        sub['pt_down'] = np.where(p_treat.isna(), np.nan, np.where(log_fc < 0, p_treat, 1.0))
        q_adjusted = (sub['q_value'] * sub['n_siblings']).clip(upper=1.0)
        sub['up'] = (q_adjusted < Q_THRESHOLD) & (log_fc > 0)
        sub['down'] = (q_adjusted < Q_THRESHOLD) & (log_fc < 0)
        q_treat_adjusted = (sub['q_treat'] * sub['n_siblings']).clip(upper=1.0)
        sub['up_treat'] = (q_treat_adjusted < Q_THRESHOLD) & (log_fc > 0)
        sub['down_treat'] = (q_treat_adjusted < Q_THRESHOLD) & (log_fc < 0)
        per_cohort = sub.groupby(['gene_key', 'cohort'], observed=True).agg(
            gene=('gene', 'first'), p_up=('p_up', 'min'), p_down=('p_down', 'min'),
            pt_up=('pt_up', 'min'), pt_down=('pt_down', 'min'), ps_up=('ps_up', 'min'), ps_down=('ps_down', 'min'),
            n_siblings=('n_siblings', 'max'), up=('up', 'any'), down=('down', 'any'),
            up_treat=('up_treat', 'any'), down_treat=('down_treat', 'any'),
            effect=('logFC', 'mean')).reset_index()
        for column in ('p_up', 'p_down', 'pt_up', 'pt_down', 'ps_up', 'ps_down'):
            per_cohort[column] = (per_cohort[column] * per_cohort['n_siblings']).clip(upper=1.0)
        p_up = per_cohort.pivot(index='gene_key', columns='cohort', values='p_up')
        p_down = per_cohort.pivot(index='gene_key', columns='cohort', values='p_down')
        stats_ = replication.replicability(p_up.to_numpy(), p_down.to_numpy(), method=PC_METHOD)
        frame = pd.DataFrame({'p_pc': stats_['p_pc'], 'r_value': stats_['r_value'],
                              'r_direction': stats_['direction']}, index=p_up.index)
        treat_up = per_cohort.pivot(index='gene_key', columns='cohort', values='pt_up').reindex(
            index=p_up.index, columns=p_up.columns)
        treat_down = per_cohort.pivot(index='gene_key', columns='cohort', values='pt_down').reindex(
            index=p_up.index, columns=p_up.columns)
        treat_stats = replication.replicability(treat_up.to_numpy(), treat_down.to_numpy(), method=PC_METHOD)
        strong_up = per_cohort.pivot(index='gene_key', columns='cohort', values='ps_up').reindex(
            index=p_up.index, columns=p_up.columns)
        strong_down = per_cohort.pivot(index='gene_key', columns='cohort', values='ps_down').reindex(
            index=p_up.index, columns=p_up.columns)
        strong_stats = replication.replicability(strong_up.to_numpy(), strong_down.to_numpy(), method=PC_METHOD)
        frame['r_strong'] = strong_stats['r_value']
        frame['p_pc_treat'] = treat_stats['p_pc']
        frame['r_treat'] = treat_stats['r_value']
        frame['m'] = p_up.notna().sum(axis=1).astype(int)
        for source, target in (('up', 'k_up'), ('down', 'k_down'), ('up_treat', 'k_treat_up'),
                               ('down_treat', 'k_treat_down')):
            frame[target] = per_cohort.groupby('gene_key')[source].sum().reindex(frame.index).fillna(0).astype(int)
        frame['k'] = frame[['k_up', 'k_down']].max(axis=1)
        frame['k_treat'] = frame[['k_treat_up', 'k_treat_down']].max(axis=1)
        sign = np.sign(per_cohort['effect'])
        direction_sign = frame['r_direction'].map({'up': 1.0, 'down': -1.0})
        agree = (sign.values == direction_sign.reindex(per_cohort['gene_key']).values)
        n_tested = per_cohort.groupby('gene_key')['effect'].size()
        frame['agreement'] = (pd.Series(agree, index=per_cohort.index).groupby(per_cohort['gene_key']).sum()
                              / n_tested).reindex(frame.index)
        frame['median_abs_logFC'] = per_cohort.assign(_abs=per_cohort['effect'].abs()).groupby(
            'gene_key')['_abs'].median().reindex(frame.index)
        frame['median_logFC'] = per_cohort.groupby('gene_key')['effect'].median().reindex(frame.index)
        # the effect gate: at least 2 cohorts with a signed effect of at least tau in the replicated direction
        tau = facet_rules.gate_tau(facet or '')
        effects = per_cohort.pivot(index='gene_key', columns='cohort', values='effect').reindex(
            index=p_up.index, columns=p_up.columns)
        signed = effects.mul(direction_sign.reindex(p_up.index), axis=0)
        carries = signed >= tau
        frame['effect_gate_tau'] = tau
        frame['n_cohorts_ge_tau'] = carries.sum(axis=1).astype(int)
        frame['effect_gate_passed'] = frame['n_cohorts_ge_tau'] >= replication.DEFAULT_U
        order = np.argsort(-np.nan_to_num(signed.to_numpy(), nan=-np.inf), axis=1)
        names = np.asarray(p_up.columns, dtype=object)
        carried = carries.to_numpy()
        any_carries = carried.any(axis=1)
        frame['effect_gate_cohorts'] = [
            [names[i] for i in row_order if row_carries[i]] if has_carriers else []
            for row_order, row_carries, has_carriers in zip(order, carried, any_carries)]
        # the two cohorts that carry the r-value: the smallest one-sided p in the replicated direction
        best = np.where((frame['r_direction'] == 'up').to_numpy()[:, None], p_up.to_numpy(), p_down.to_numpy())
        best = np.nan_to_num(best, nan=np.inf)
        carriers = np.argsort(best, axis=1, kind='stable')[:, :replication.DEFAULT_U]
        carrier_finite = np.isfinite(np.take_along_axis(best, carriers, axis=1))
        frame['r_cohorts'] = [[names[i] for i, finite in zip(row, flags) if finite]
                              for row, flags in zip(carriers.tolist(), carrier_finite.tolist())]
        frame['r_significant'] = frame['r_value'] < replication.R_THRESHOLD
        frame['replicated'] = frame['r_significant']
        frame['replicated_effect'] = frame['r_treat'] < replication.R_THRESHOLD
        frame['replicated_strong'] = frame['r_strong'] < replication.R_THRESHOLD
        frame['gene'] = per_cohort.groupby('gene_key')['gene'].first().reindex(frame.index)
        upper_names = np.asarray(upper.categories, dtype=object)
        display_names = np.asarray(display.categories, dtype=object)
        frame['gene'] = display_names[frame['gene'].to_numpy(dtype=int)]
        frame.index = pd.Index(upper_names[frame.index.to_numpy(dtype=int)], name='gene_key')
        per_cohort['gene'] = display_names[per_cohort['gene'].to_numpy(dtype=int)]
        per_cohort['gene_key'] = upper_names[per_cohort['gene_key'].to_numpy(dtype=int)]
        frame = frame.sort_index()
        per_cohort = per_cohort.sort_values(['gene_key', 'cohort'], kind='stable').reset_index(drop=True)
        return {'frame': frame, 'per_cohort': per_cohort, 'units': group_units}

    def _adjusted_age_family(self, facet: str, datasets: Sequence[Dict[str, Any]], exclude_secondary: bool,
                             scope: str, group: str) -> Optional[pd.DataFrame]:
        """Replication table of the pathology-adjusted age model for one cell type group, or None.

        Only the ``age`` facet has it: a gene of the unadjusted age ranking persists after the pathology
        adjustment when it also replicates, in the same direction, in ``age_adjusted`` (same scope and group).
        None for other facets, or when the adjusted model has no counted cohorts in the group.
        """
        if facet != 'age':
            return None
        entry = self.replication_families('age_adjusted', datasets, exclude_secondary, scope).get(group)
        return None if entry is None else entry['frame']

    def _resolve_group_filter(self, units: pd.DataFrame, cell_type: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        """``cell_type`` as (group, raw label): a group name selects the group,
        anything else a raw label (and through it, its group)."""
        if not cell_type:
            return None, None
        known_groups = set(units['celltype_group']) if len(units) else set()
        if cell_type in known_groups or cell_type in _known_group_names():
            return cell_type, None
        return None, cell_type

    def gene_effects(self, facet: str, gene: str, datasets: Sequence[Dict[str, Any]],
                     cell_type: Optional[str] = None, exclude_secondary: bool = True,
                     scope: Optional[str] = None) -> Dict[str, Any]:
        """The result card of one gene for one facet (``_gene_effects``), cached per gene until a file changes.

        The returned dict is shared between callers: do not modify it.
        """
        facet = canonical_facet(facet) if facet else facet
        scope = scope or self.default_scope(facet)
        prepared = self.prepare(datasets)
        return self._cached_build(
            self._cards, (facet, gene.upper(), cell_type, bool(exclude_secondary), scope), prepared.signature,
            lambda: self._gene_effects(facet, gene, prepared, cell_type, exclude_secondary, scope),
            max_entries=CARD_CACHE_ENTRIES)

    def _gene_effects(self, facet: str, gene: str, datasets: Sequence[Dict[str, Any]],
                      cell_type: Optional[str] = None, exclude_secondary: bool = True,
                      scope: Optional[str] = None) -> Dict[str, Any]:
        """The result card of one gene for one facet: every tested unit, grouped
        by cohort, with the replication entry per cell type group.

        Args:
            facet: Test facet (sex, disease, apoe4_carrier, braak).
            gene: Gene symbol (case-insensitive).
            datasets: Dicts with ``db_id``, ``group_folder``, ``stem`` and
                optional ``title``, ``sha256``, ``is_secondary``. Donor-pooled
                units (``pool:<id>``) are added automatically.
            cell_type: A cell type group (``microglia``) or a raw stratum
                label (``Mic P2RY12``) to restrict to.
            exclude_secondary: Secondary datasets are listed but not counted.
            scope: Facet scope (``lib/facet_scopes.py``); default ``default_scope(facet)``: human
                post-mortem where the facet is allowed there. Genes are never merged across scopes.

        Returns:
            ``{query, scope, scopes, facet, gene, units, replication, cell_types, notes,
            definition}``; ``scopes`` lists the scopes in which the facet has results;
            ``units`` is empty when the gene was not tested.
            Units carry the raw ``cell_type`` and its ``celltype_group``;
            ``replication`` and ``cell_types`` are per group. A replication
            entry has ``r_value``, ``p_pc``, ``k``, ``m``, ``replicated``,
            ``anchors`` (one per counted cohort) and the legacy fields.
        """
        facet = canonical_facet(facet) if facet else facet
        scope = scope or self.default_scope(facet)
        datasets = self.prepare(datasets)
        rows, units = self.facet_gene_table(facet, datasets, scope)
        families = self.replication_families(facet, datasets, exclude_secondary, scope)
        info = FACET_INFO.get(facet, {'contrast': facet, 'effect_unit': 'log2 fold change', 'provisional': False})
        units = self._counted_units(units, exclude_secondary, facet) if len(units) else units
        gene_rows = rows[rows['gene_upper'] == gene.upper()] if len(rows) else rows
        merged = gene_rows.merge(units, on='unit', how='left') if len(gene_rows) else None
        unit_cards: List[Dict[str, Any]] = []
        replication_entries: List[Dict[str, Any]] = []
        cell_types: List[Dict[str, Any]] = []
        if merged is not None and len(merged):
            group_filter, label_filter = self._resolve_group_filter(units, cell_type)
            if group_filter:
                merged = merged[merged['celltype_group'] == group_filter]
            elif label_filter:
                merged = merged[merged['cell_type'] == label_filter]
            for group, sub in merged.groupby('celltype_group', sort=False):
                family = (families.get(group) or {}).get('frame')
                family_row = family.loc[gene.upper()] if family is not None and gene.upper() in family.index else None
                rep = self._replication(sub, family_row, facet)
                labels = sorted(set(sub['cell_type']))
                details = group_info(group)
                counted_rows = sub[sub['counted_effective']].to_dict(orient='records')
                anchors = [_anchor_record(record) for record in counted_rows]
                forest = _forest(sub)
                bases = sorted({str(a['disease_basis']) for a in anchors if a['disease_basis']})
                replication_entries.append({'cell_type': group, 'celltype_group': group, 'cl_id': details['cl_id'],
                                            'labels': labels, 'anchors': anchors, 'forest': forest,
                                            'disease_basis': bases if facet == 'disease' else None, **rep})
                cell_types.append({'cell_type': group, 'celltype_group': group, 'labels': labels,
                                   'n_units': int(len(sub)), 'n_cohorts': rep['m'] or rep['cohorts_tested']})
            order = {r['celltype_group']: (r['celltype_group'] == UNMAPPED, -(r['m'] or r['cohorts_tested']),
                                           -len(merged[merged['celltype_group'] == r['celltype_group']]))
                     for r in replication_entries}
            merged = merged.assign(
                _ct=merged['celltype_group'].map(lambda g: order.get(g, (True, 0, 0))),
                _counted=~merged['counted_effective'],
                _role=merged['role'].fillna('').str.lower().str.startswith('anchor').map(lambda a: 0 if a else 1),
                _cohort=merged['cohort_label'].fillna('~'),
                _donors=-merged['n_donors'].fillna(0).astype(float))
            merged = merged.sort_values(['_ct', 'celltype_group', '_counted', '_cohort', '_role', '_donors',
                                         'cell_type'])
            for record in merged.to_dict(orient='records'):
                summary_unit = record.get('summary_unit') or {}
                model = (summary_unit.get('model') or {}).get('genes') or {}
                q_value = _finite(record.get('q_value'))
                n_siblings = int(record.get('n_siblings') or 1)
                q_counted = min(1.0, q_value * n_siblings) if q_value is not None else None
                unit_cards.append({
                    'db_id': record['db_id'], 'title': record.get('title'),
                    'gene': str(record['gene']),
                    'cell_type': record['cell_type'], 'celltype_source': record['celltype_source'],
                    'celltype_group': record['celltype_group'],
                    'cohort': record.get('cohort'), 'cohort_label': record.get('cohort_label'),
                    'role': record.get('role'),
                    'counted': bool(record.get('counted_effective')),
                    'anchor': bool(record.get('counted_effective')),
                    'anchor_counted': bool(record.get('counted_effective')),
                    'cohort_display': cohort_display(record.get('cohort_label')),
                    'modality': record.get('modality'), 'modality_note': record.get('modality_note'),
                    'small_sample': _is_small(record.get('n_donors')),
                    'not_counted_reason': _not_counted_reason(record),
                    'is_secondary': bool(record.get('is_secondary')),
                    'pooled': bool(record.get('pooled')),
                    'independence': record.get('independence'),
                    'disease_basis': record.get('disease_basis'),
                    'analysis_level': record.get('analysis_level'),
                    'replicate_unit': summary_unit_value(record, 'replicate_unit'),
                    'replicate_column': summary_unit_value(record, 'replicate_column'),
                    'facet_scope': record.get('facet_scope'),
                    'persists_after_pathology_adjustment': (None if record.get('persists_after_pathology_adjustment')
                                                            is None or pd.isna(record.get('persists_after_pathology_adjustment'))
                                                            else bool(record['persists_after_pathology_adjustment'])),
                    'lambda_gc': summary_unit_value(record, 'lambda_gc'), 'inflated': summary_unit_value(record, 'inflated'),
                    'df_total_median': summary_unit_value(record, 'df_total_median'),
                    'covariates_dropped': summary_unit_value(record, 'covariates_dropped') or [],
                    'n_sex_discordant': summary_unit_value(record, 'n_sex_discordant'),
                    'n_sex_checked': summary_unit_value(record, 'n_sex_checked'),
                    'age_top_coded': summary_unit_value(record, 'age_top_coded'),
                    'control_basis': summary_unit_value(record, 'control_basis'),
                    'design_subset': summary_unit_value(record, 'design_subset'),
                    'notes': summary_unit_value(record, 'notes') or [],
                    'logFC': _finite(record.get('logFC')),
                    'ci_low': _finite(record.get('ci_low')), 'ci_high': _finite(record.get('ci_high')),
                    'p_value': _finite(record.get('p_value')),
                    'q_value': q_value,
                    'p_treat': _finite(record.get('p_treat')), 'q_treat': _finite(record.get('q_treat')),
                    'tau_log2': _finite(record.get('tau_log2')),
                    'q_counted': q_counted if n_siblings > 1 else q_value,
                    'n_sibling_strata': n_siblings,
                    'significant': q_value is not None and q_value < Q_THRESHOLD,
                    'significant_treat': (_finite(record.get('q_treat')) is not None
                                          and _finite(record.get('q_treat')) < Q_THRESHOLD),
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
        notes = ['exploratory', f'facet scope {scope}: scopes (human, xenograft) are never mixed',
                 'datasets are tested separately; replication combines one anchor unit per cohort',
                 'cohort table is a DRAFT',
                 'cell types are counted by group (lib/celltype_groups.yaml); the raw label is on each unit']
        notes.append('composition: a fold change of a whole cell type partly reflects a shift in the mix of cell states '
                     'inside it (more cells in a state), not only induction of the gene in every cell')
        if facet in ('braak', 'disease'):
            notes.append('Braak stage and pathologic AD are the same pathology axis read two ways: where both replicate '
                         'it is one finding, not two replications')
        if facet in ('age', 'age_adjusted'):
            notes.append('age effects are confounded with pathology in every brain bank: compare the unadjusted and the '
                         'pathology-adjusted model (persists_after_pathology_adjustment)')
        if info.get('provisional'):
            notes.append(PROVISIONAL_NOTE)
        if facet == 'disease':
            notes.append('replication only combines pathology-based disease definitions '
                         '(disease_basis nia_reagan, braak_cerad, adnc)')
        if gene.upper() in APOE_LOCUS_GENES and facet == 'apoe4_carrier':
            notes.append(APOE_LOCUS_NOTE)
        return json_safe({
            'query': {'gene': gene, 'facet': facet, 'cell_type': cell_type,
                      'exclude_secondary': exclude_secondary, 'scope': scope},
            'scope': scope,
            'scopes': self.scopes_with_results(facet, datasets),
            'facet': {'name': facet, 'column': 'ant_' + facet, 'contrast': info['contrast'],
                      'effect_unit': info['effect_unit'], 'provisional': info['provisional'],
                      'status': 'exploratory'},
            'gene': unit_cards[0]['gene'] if unit_cards else gene,
            'gene_flags': gene_classes.flags(unit_cards[0]['gene'] if unit_cards else gene),
            'n_units': len(unit_cards),
            'units': unit_cards,
            'replication': replication_entries,
            'cell_types': cell_types,
            'notes': notes,
            'apoe_locus': gene.upper() in APOE_LOCUS_GENES,
            'definition': REPLICATION_DEFINITION,
            'donor_count_definition': DONOR_COUNT_DEFINITION,
        })

    def recurring_genes(self, facet: str, datasets: Sequence[Dict[str, Any]],
                        cell_type: Optional[str] = None, limit: int = 50,
                        exclude_secondary: bool = True, scope: Optional[str] = None,
                        offset: int = 0, effect_gate: bool = False, strict: bool = False) -> Dict[str, Any]:
        """Genes that replicate across cohorts for a facet, within one facet scope and cell type group.

        A gene is listed when its partial conjunction r-value is below 0.05 and it passes the effect gate
        (``RANKING_DEFINITION``): the effect is present in at least two
        cohorts in the same direction (``lib/replication.py``) and the median effect over the
        tested cohorts is at least tau. Ranking: r_value
        ascending, then k (cohorts with q_value < 0.05 in that direction)
        descending, then ``agreement`` and ``median_abs_logFC``. ``scope`` defaults to
        ``default_scope(facet)``; the response carries ``scope`` and ``scopes``.

        Without ``cell_type`` the group with the most counted cohorts is used
        (unmapped never). Rows: ``gene, r_value, p_pc, k, m, replicated,
        direction, agreement, median_abs_logFC, cohorts, overlap_unknown,
        cohorts_overlap_unknown, apoe_locus`` plus the legacy fields
        ``cohorts_sig_same_sign`` (= k), ``cohorts_tested`` (= m),
        ``cohorts_same_sign``, ``sign_discordant``. The response carries
        ``n_replicated`` (genes with r < 0.05 in the group) and
        ``n_genes_in_family`` (genes tested in at least two cohorts).

        Paging (2.45): ``offset`` skips ranked rows, ``limit`` has no upper bound here; ``n_matching`` is the
        number of rows the filters keep (``effect_gate``: only genes whose effect gate passed; ``strict``: only
        those that also pass the strict minimum-effect test, ``replicated_effect``), ``n_returned`` the rows in
        ``rows`` and ``has_more`` whether more follow. ``headline`` has the counts of the group in one place.
        """
        facet = canonical_facet(facet) if facet else facet
        scope = scope or self.default_scope(facet)
        datasets = self.prepare(datasets)
        rows, units = self.facet_gene_table(facet, datasets, scope)
        families = self.replication_families(facet, datasets, exclude_secondary, scope)
        units = self._counted_units(units, exclude_secondary, facet) if len(units) else units
        counted = units[units['counted_effective']] if len(units) else units
        options = []
        if len(counted):
            for group, sub in counted.groupby('celltype_group'):
                options.append({'cell_type': group, 'celltype_group': group,
                                'cl_id': group_info(group)['cl_id'],
                                'labels': sorted(set(sub['cell_type'])),
                                'n_cohorts': int(sub['cohort'].nunique()), 'n_units': int(len(sub)),
                                **family_counts((families.get(group) or {}).get('frame'))})
        options.sort(key=lambda o: (-o['n_cohorts'], -o['n_units'], o['celltype_group']))
        group_filter, label_filter = self._resolve_group_filter(units, cell_type) if len(units) else (None, None)
        if label_filter:
            group_filter = group_of(label_filter)
        chosen = group_filter or (options[0]['celltype_group'] if options else None)
        out_rows: List[Dict[str, Any]] = []
        n_cohorts = 0
        n_replicated = 0
        n_r_significant = 0
        n_effect_gate = 0
        n_replicated_effect = 0
        n_replicated_strong = 0
        n_family = 0
        n_strict = 0
        n_matching = 0
        strict_genes: List[str] = []
        group_donors: Dict[str, Optional[int]] = {'n_donors_sum': None, 'n_donors_distinct': None}
        overlap_labels: Dict[str, bool] = {}
        if chosen is not None and len(rows):
            chosen_units = counted[counted['celltype_group'] == chosen]
            n_cohorts = int(chosen_units['cohort'].nunique())
            group_donors = _donor_counts(chosen_units)
            overlap_labels = {str(label): bool(flag) for label, flag in zip(
                chosen_units['cohort_label'], chosen_units['overlap_unknown'])}
            small_labels = {str(label) for label, donors in zip(
                chosen_units['cohort_label'], pd.to_numeric(chosen_units['n_donors'], errors='coerce'))
                if donors < facet_rules.SMALL_COHORT_REPLICATES}
            family_entry = families.get(chosen)
            if family_entry is not None:
                frame = family_entry['frame']
                per_cohort = family_entry['per_cohort']
                labels = dict(zip(units['cohort'], units['cohort_label']))
                usable = frame[frame['r_value'].notna()]
                n_family = int(len(usable))
                n_r_significant = int((usable['r_value'] < replication.R_THRESHOLD).sum())
                replicated = usable[usable['replicated'].astype(bool)]
                n_replicated = int(len(replicated))
                n_effect_gate = int(replicated['effect_gate_passed'].astype(bool).sum())
                n_replicated_effect = int(usable['replicated_effect'].astype(bool).sum())
                n_replicated_strong = int(usable['replicated_strong'].astype(bool).sum())
                adjusted = self._adjusted_age_family(facet, datasets, exclude_secondary, scope, chosen)
                rows_by_gene = family_entry.get('rows_by_gene')
                if rows_by_gene is None:                     # built once per family: the rows of each gene by position
                    rows_by_gene = family_entry['rows_by_gene'] = per_cohort.groupby('gene_key', sort=False).indices
                donors_of: Dict[frozenset, Dict[str, Optional[int]]] = {}
                ordered = replicated.sort_values(['r_value', 'k', 'agreement', 'median_abs_logFC'],
                                                 ascending=[True, False, False, False])
                gated = ordered[ordered['effect_gate_passed'].astype(bool)]
                strict_rows = gated[gated['replicated_effect'].astype(bool)]
                n_strict = int(len(strict_rows))
                strict_genes = [str(name) for name in strict_rows['gene'].head(STRICT_GENES_LISTED)]
                kept = strict_rows if strict else gated if effect_gate else ordered
                n_matching = int(len(kept))
                ranked = kept.iloc[offset:offset + limit]
                for gene_key, record in ranked.iterrows():
                    direction = record['r_direction']
                    sig_column = 'up' if direction == 'up' else 'down'
                    gene_cohorts = per_cohort.iloc[rows_by_gene[gene_key]]
                    names = gene_cohorts.loc[gene_cohorts[sig_column], 'cohort']
                    tested_cohorts = frozenset(gene_cohorts['cohort'])
                    if tested_cohorts not in donors_of:      # few distinct cohort sets: count the donors once per set
                        donors_of[tested_cohorts] = _donor_counts(chosen_units[chosen_units['cohort'].isin(tested_cohorts)])
                    gene_donors = donors_of[tested_cohorts]
                    cohort_labels = sorted(labels.get(c) or c for c in tested_cohorts)
                    small_cohorts = sorted(small_labels & set(cohort_labels))
                    unknown = sorted(label for label in cohort_labels if overlap_labels.get(label))
                    out_rows.append({
                        'gene': str(record['gene']), 'r_value': _finite(record['r_value']),
                        'p_pc': _finite(record['p_pc']), 'k': int(record['k']), 'k_treat': int(record['k_treat']),
                        'm': int(record['m']), 'replicated': True, 'direction': direction,
                        'median_logFC': _finite(record.get('median_logFC')),
                        'effect_gate_passed': bool(record.get('effect_gate_passed')),
                        'effect_gate_tau': _finite(record.get('effect_gate_tau')),
                        'effect_gate_cohorts': [labels.get(c) or c for c in record.get('effect_gate_cohorts') or []],
                        'r_treat': _finite(record.get('r_treat')),
                        'replicated_effect': bool(record.get('replicated_effect')),
                        'persists_after_pathology_adjustment': (
                            None if adjusted is None else bool(
                                gene_key in adjusted.index and bool(adjusted.loc[gene_key, 'replicated'])
                                and adjusted.loc[gene_key, 'r_direction'] == direction)),
                        'r_strong': _finite(record.get('r_strong')),
                        'replicated_strong': (bool(record.get('replicated_strong'))
                                              if _finite(record.get('r_strong')) is not None else None),
                        'r_cohorts': [labels.get(c) or c for c in record.get('r_cohorts') or []],
                        'gene_flags': gene_classes.flags(str(record['gene'])),
                        'agreement': _finite(record.get('agreement')),
                        'median_abs_logFC': _finite(record.get('median_abs_logFC')),
                        'cohorts': sorted({labels.get(c) or c for c in names}),
                        'cohorts_tested_labels': cohort_labels,
                        'n_small': len(small_cohorts), 'small_cohorts': small_cohorts,
                        'overlap_unknown': bool(unknown), 'cohorts_overlap_unknown': unknown,
                        'cohorts_sig_same_sign': int(record['k']), 'cohorts_tested': int(record['m']),
                        'cohorts_same_sign': (int(round(record['agreement'] * len(gene_cohorts)))
                                              if _finite(record.get('agreement')) is not None else None),
                        'sign_discordant': bool(record['k_up'] > 0 and record['k_down'] > 0),
                        'apoe_locus': str(record['gene']).upper() in APOE_LOCUS_GENES,
                        **gene_donors,
                    })
        chosen_info = group_info(chosen) if chosen else {}
        return json_safe({
            'facet': facet,
            'headline': {'n_replicated': n_replicated, 'n_effect_gate': n_effect_gate, 'n_strict': n_strict,
                         'strict_genes': strict_genes, 'n_genes_in_family': n_family,
                         'cohorts': n_cohorts, 'effect_gate_unit': gate_words(facet)},
            'offset': offset, 'n_matching': n_matching, 'n_returned': len(out_rows),
            'has_more': offset + len(out_rows) < n_matching,
            'cell_type': chosen,
            'celltype_group': chosen,
            'cl_id': chosen_info.get('cl_id'),
            'labels': next((o['labels'] for o in options if o['celltype_group'] == chosen), []),
            'scope': scope,
            'scopes': self.scopes_with_results(facet, datasets),
            'cohorts_in_cell_type': n_cohorts,
            'n_replicated': n_replicated,
            'n_r_significant': n_r_significant,
            'n_effect_gate': n_effect_gate,
            'n_replicated_effect': n_replicated_effect,
            'n_replicated_strong': n_replicated_strong,
            'n_genes_in_family': n_family,
            'donors_in_cell_type': group_donors,
            'donor_count_definition': DONOR_COUNT_DEFINITION,
            'cell_types': options,
            'rows': out_rows,
            'definition': REPLICATION_DEFINITION,
            'ranking': RANKING_DEFINITION,
            'exclude_secondary': exclude_secondary,
            'cohort_table_status': 'DRAFT',
        })

    # ── gene sets and the facet overview ───────────────────────────────────

    def member_replication(self, facet: str, members: Sequence[str], datasets: Sequence[Dict[str, Any]],
                           exclude_secondary: bool = True, scope: Optional[str] = None) -> Dict[str, Any]:
        """The member genes of a gene set that replicate for one facet in one scope, per cell type group.

        Uses the gene replication families (the same r-values as ``/facets/{facet}/effects/top``), so the numbers are
        the gene-level truth, next to the module result of the whole set. Nothing is truncated.

        Args:
            facet: Test facet name.
            members: Gene symbols of the set (any case; matched by upper case symbol within the scope).
            datasets: As for ``gene_effects``.
            exclude_secondary: Secondary datasets are not counted.
            scope: Facet scope; default ``default_scope(facet)``.

        Returns:
            ``{facet, scope, scopes, n_members, groups}``; ``groups[group]`` has ``n_members``, ``n_tested`` (members
            tested in at least two cohorts), ``n_replicated`` (r < 0.05), ``n_up``, ``n_down``, ``n_effect_gate``
            (replicated and the effect gate passed), ``n_strict``, ``genes_up`` and ``genes_down`` (names of the
            replicated members by direction, best r first), ``genes_gate`` and ``genes`` (one dict per replicated
            member: ``gene, r_value, direction, median_logFC, k, m, effect_gate_passed, strict``).
        """
        facet = canonical_facet(facet) if facet else facet
        scope = scope or self.default_scope(facet)
        prepared = self.prepare(datasets)
        families = self.replication_families(facet, prepared, exclude_secondary, scope)
        keys = {str(name).strip().upper() for name in members if str(name).strip()}
        groups: Dict[str, Dict[str, Any]] = {}
        for group, entry in families.items():
            frame = entry['frame']
            present = frame.loc[frame.index.intersection(list(keys))]
            usable = present[present['r_value'].notna()]
            replicated = usable[usable['replicated'].astype(bool)].sort_values(
                ['r_value', 'k'], ascending=[True, False])
            gated = replicated[replicated['effect_gate_passed'].astype(bool)]
            strict_mask = gated['replicated_effect'].astype(bool)
            genes = [{'gene': str(record['gene']), 'r_value': _finite(record['r_value']),
                      'direction': record['r_direction'], 'median_logFC': _finite(record['median_logFC']),
                      'k': int(record['k']), 'm': int(record['m']),
                      'effect_gate_passed': bool(record['effect_gate_passed']),
                      'strict': bool(record['effect_gate_passed'] and record['replicated_effect'])}
                     for _, record in replicated.iterrows()]
            groups[group] = {
                'n_members': len(keys), 'n_in_data': int(len(present)), 'n_tested': int(len(usable)),
                'n_replicated': int(len(replicated)),
                'n_up': int((replicated['r_direction'] == 'up').sum()),
                'n_down': int((replicated['r_direction'] == 'down').sum()),
                'n_effect_gate': int(len(gated)), 'n_strict': int(strict_mask.sum()),
                'genes_up': [g['gene'] for g in genes if g['direction'] == 'up'],
                'genes_down': [g['gene'] for g in genes if g['direction'] == 'down'],
                'genes_gate': [g['gene'] for g in genes if g['effect_gate_passed']],
                'genes': genes}
        return json_safe({'facet': facet, 'scope': scope, 'scopes': self.scopes_with_results(facet, prepared),
                          'n_members': len(keys), 'groups': groups})

    def effects_overview(self, facet: str, datasets: Sequence[Dict[str, Any]], exclude_secondary: bool = True,
                         scope: Optional[str] = None, n_per_group: int = 10) -> Dict[str, Any]:
        """The data of a simplified facet view: the headline and every cell type group, each gene shown once.

        A gene replicated in several groups is listed only in the group where it is strongest (smallest r-value,
        then most cohorts), so a page does not repeat PTPRG in five cards. Groups without a replicated gene are
        named in ``quiet_groups`` with their cohort count and ``low_power`` (at most 2 cohorts), for one summary line.

        Returns:
            ``{facet, scope, scopes, headline, cell_types, quiet_groups, n_genes_unique}``. ``headline`` has the
            counts of the group with the most cohorts (``cell_type``) and ``strict_genes``; ``cell_types`` one
            card per group with replicated genes: ``celltype_group``, ``n_cohorts``, counts, ``n_assigned`` (genes
            listed here) and ``genes`` (the best ``n_per_group``: ``gene, r_value, direction, median_logFC, k, m,
            effect_gate_passed, strict, also_in`` (other groups where it replicates)).
        """
        facet = canonical_facet(facet) if facet else facet
        scope = scope or self.default_scope(facet)
        prepared = self.prepare(datasets)
        families = self.replication_families(facet, prepared, exclude_secondary, scope)
        candidates: List[pd.DataFrame] = []
        cohorts_of: Dict[str, int] = {}
        counts_of: Dict[str, Dict[str, int]] = {}
        for group, entry in families.items():
            frame = entry['frame']
            cohorts_of[group] = int(entry['units']['cohort'].nunique())
            counts_of[group] = family_counts(frame)
            usable = frame[frame['r_value'].notna() & frame['replicated'].astype(bool)
                           & frame['effect_gate_passed'].astype(bool)]
            if len(usable):
                candidates.append(usable.assign(group=group))
        everything = pd.concat(candidates) if candidates else None
        cards: List[Dict[str, Any]] = []
        owner: Dict[str, str] = {}
        if everything is not None:
            ranked = everything.reset_index().sort_values(['r_value', 'm', 'k'], ascending=[True, False, False],
                                                          kind='stable')
            owner = dict(zip(ranked.drop_duplicates('gene_key')['gene_key'],
                             ranked.drop_duplicates('gene_key')['group']))
            where = ranked.groupby('gene_key')['group'].agg(list).to_dict()
            for group in sorted(counts_of, key=lambda g: (-cohorts_of[g], -counts_of[g]['n_effect_gate'], g)):
                mine = ranked[(ranked['group'] == group) & ranked['gene_key'].map(owner).eq(group)]
                if mine.empty:
                    continue
                genes = [{'gene': str(record['gene']), 'r_value': _finite(record['r_value']),
                          'direction': record['r_direction'], 'median_logFC': _finite(record['median_logFC']),
                          'k': int(record['k']), 'm': int(record['m']), 'effect_gate_passed': True,
                          'strict': bool(record['replicated_effect']),
                          'also_in': [g for g in where[record['gene_key']] if g != group]}
                         for _, record in mine.head(n_per_group).iterrows()]
                cards.append({'celltype_group': group, 'cell_type': group, 'cl_id': group_info(group)['cl_id'],
                              'n_cohorts': cohorts_of[group], **counts_of[group], 'n_assigned': int(len(mine)),
                              'genes': genes})
        quiet = [{'celltype_group': group, 'n_cohorts': cohorts_of[group], **counts_of[group],
                  'low_power': cohorts_of[group] <= replication.DEFAULT_U}
                 for group in sorted(counts_of, key=lambda g: (-cohorts_of[g], g))
                 if group not in {card['celltype_group'] for card in cards} and group != UNMAPPED]
        main = max(counts_of, key=lambda g: (cohorts_of[g], counts_of[g]['n_effect_gate'])) if counts_of else None
        strict_genes: List[str] = []
        if main is not None and everything is not None:
            frame = families[main]['frame']
            hit = frame[frame['replicated'].astype(bool) & frame['effect_gate_passed'].astype(bool)
                        & frame['replicated_effect'].astype(bool)].sort_values(['r_value', 'k'], ascending=[True, False])
            strict_genes = [str(name) for name in hit['gene'].head(STRICT_GENES_LISTED)]
        headline = {'celltype_group': main, 'cohorts': cohorts_of.get(main, 0), **counts_of.get(main, family_counts(None)),
                    'strict_genes': strict_genes, 'effect_gate_unit': gate_words(facet)}
        return json_safe({'facet': facet, 'scope': scope, 'scopes': self.scopes_with_results(facet, prepared),
                          'exclude_secondary': exclude_secondary, 'headline': headline, 'cell_types': cards,
                          'quiet_groups': quiet, 'n_genes_unique': len(owner)})

    # ── one facet across datasets ──────────────────────────────────────────

    def facet_overview(self, facet: str, datasets: Sequence[Dict[str, Any]],
                       top_genes: int = 5, exclude_secondary: bool = True,
                       scope: Optional[str] = None) -> Dict[str, Any]:
        """Per-dataset summaries of one facet: one row per tested (dataset,
        stratum), sorted by donors analysed (most first). No pooling.

        Args:
            facet: Test facet name (``sex``, ``disease``, ``apoe4_carrier``, ``braak``).
            datasets: Dicts with ``db_id``, ``group_folder``, ``stem`` and
                optional ``title``, ``group``, ``sha256``.
            top_genes: Genes at q < 0.05 listed per row (smallest q first).
            exclude_secondary: Rows of secondary datasets stay in the list,
                with ``is_secondary`` true and ``counted`` false.
            scope: A facet scope; None selects ``default_scope(facet)``, ``all`` every scope (each row
                carries ``facet_scope``; counting is always per scope).

        Returns:
            ``{facet, scope, scopes, rows, not_tested, n_datasets_tested}``; ``not_tested``
            lists datasets with result files whose units for this facet were
            all untested, with the reason.
        """
        facet = canonical_facet(facet) if facet else facet
        scope = scope or self.default_scope(facet)
        all_scopes = self.scopes_with_results(facet, datasets)
        datasets = self._with_scope(self._with_pooled(self.prepare(datasets).datasets), None if scope == 'all' else scope)
        signature = (facet, scope, top_genes, exclude_secondary, _file_key(self.results_dir / COHORT_TABLE_NAME), tuple(
            (d['db_id'], bool(d.get('is_secondary')), d.get('disease_basis'), d.get('modality'),
             self.fingerprint(d['group_folder'], d['stem']))
            for d in datasets))
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
                                   current_sha256=dataset.get('sha256'), disease_basis=dataset.get('disease_basis'))
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
                        'covariates_omitted', 'flags', 'genes', 'modules', 'n_cells_in_stratum',
                        'analysis_level', 'analysis_level_label', 'analysis_level_reason', 'replicate_unit',
                        'replicate_column', 'tau_log2')},
                    'disease_basis': summary.get('disease_basis'),
                    'pooled': str(dataset['db_id']).startswith(POOL_PREFIX),
                    'independence': (summary.get('cohort') or {}).get('independence'),
                    'overlap_unknown': bool((summary.get('cohort') or {}).get('overlap_unknown')),
                    'top_genes': top_by_unit.get((unit['cell_type'], unit['celltype_source']), []),
                    'run_date': run_date,
                    'dataset_changed': summary.get('dataset_changed'),
                    'cohort': (summary.get('cohort') or {}).get('cohort'),
                    'cohort_label': (summary.get('cohort') or {}).get('cohort_label'),
                    'cohort_display': cohort_display((summary.get('cohort') or {}).get('cohort_label')),
                    'modality': dataset.get('modality'), 'modality_note': dataset.get('modality_note'),
                    'role': (summary.get('cohort') or {}).get('role'),
                    'counted': bool((summary.get('cohort') or {}).get('counted'))
                    and not (exclude_secondary and bool(dataset.get('is_secondary'))),
                    '_cohort_counted': bool((summary.get('cohort') or {}).get('counted')),
                    '_exclusion_missing': bool((summary.get('cohort') or {}).get('exclusion_missing')),
                    'is_secondary': bool(dataset.get('is_secondary')),
                    'celltype_group': group_of(unit.get('cell_type')),
                    'facet_scope': dataset['facet_scope'],
                    'model': unit.get('model'),
                    'sex_control': unit.get('sex_control'),
                })
        rows.sort(key=lambda r: (-(r.get('n_donors') or 0), r['db_id'], r.get('cell_type') or ''))
        for row_scope in sorted({r['facet_scope'] for r in rows}):
            self._add_not_counted_reasons([r for r in rows if r['facet_scope'] == row_scope], facet, exclude_secondary)
        result = json_safe({
            'facet': facet,
            'scope': scope,
            'scopes': all_scopes,
            'exclude_secondary': exclude_secondary,
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

    def _add_not_counted_reasons(self, rows: List[Dict[str, Any]], facet: str, exclude_secondary: bool) -> None:
        """Set ``not_counted_reason`` on overview rows (None when the row counts) with the
        same rules and wording as the gene effect cards; drops the private ``_`` fields."""
        if not rows:
            return
        frame = pd.DataFrame({
            'db_id': [r['db_id'] for r in rows], 'cell_type': [r.get('cell_type') for r in rows],
            'celltype_source': [r.get('celltype_source') for r in rows],
            'celltype_group': [r.get('celltype_group') for r in rows],
            'cohort': [r.get('cohort') or None for r in rows], 'role': [r.get('role') for r in rows],
            'counted': [r['_cohort_counted'] for r in rows], 'pooled': [r.get('pooled') for r in rows],
            'is_secondary': [r.get('is_secondary') for r in rows], 'n_donors': [r.get('n_donors') for r in rows],
            'disease_basis': [r.get('disease_basis') for r in rows],
            'descriptive_only': [bool((r.get('flags') or {}).get('descriptive_only')) for r in rows],
        })
        decided = self.counting_units(frame, exclude_secondary, facet).to_dict(orient='records')
        for row, record in zip(rows, decided):
            record['exclusion_missing'] = row.pop('_exclusion_missing')
            row.pop('_cohort_counted')
            row['not_counted_reason'] = _not_counted_reason(record)

    def _top_genes(self, path: Path, facet: str, n_top: int) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
        """(cell_type, celltype_source) -> the n_top genes with smallest q < 0.05."""
        facet = canonical_facet(facet) if facet else facet
        try:
            import pyarrow.parquet as pq
            names = set(pq.read_schema(path).names)
            wanted = [c for c in ('facet', 'cell_type', 'celltype_source', 'gene', 'logFC', 'q_value')
                      if c in names]
            if len(wanted) < 6:
                return {}
            frame = _rename_deprecated_facets(pd.read_parquet(
                path, columns=wanted, filters=[('facet', 'in', _stored_facet_names(facet))]))
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
        facet = canonical_facet(facet) if facet else facet
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
        elif facet in ('age', 'age_adjusted'):
            xlabel = 'log2 fold change per decade of age'
        elif level_ref and level_test:
            xlabel = f'log2 fold change ({level_test} vs {level_ref})'
        else:
            xlabel = 'log2 fold change'
        axes.set_xlabel(xlabel, fontsize=8)
        axes.set_ylabel(VOLCANO_YLABEL, fontsize=8)
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


UNASSIGNED_COHORT = 'unassigned'


def family_counts(frame: Optional[pd.DataFrame]) -> Dict[str, int]:
    """Headline counts of one replication family frame: genes tested in at least two cohorts, replicated
    (r < 0.05), replicated with the effect gate passed, and those that also pass the strict minimum-effect test
    (``replicated_effect``). Zeros for a missing frame."""
    if frame is None or frame.empty:
        return {'n_genes_in_family': 0, 'n_replicated': 0, 'n_effect_gate': 0, 'n_strict': 0}
    usable = frame[frame['r_value'].notna()]
    replicated = usable[usable['replicated'].astype(bool)]
    gated = replicated[replicated['effect_gate_passed'].astype(bool)]
    return {'n_genes_in_family': int(len(usable)), 'n_replicated': int(len(replicated)),
            'n_effect_gate': int(len(gated)), 'n_strict': int(gated['replicated_effect'].astype(bool).sum())}


def cohort_display(label: Any) -> str:
    """The cohort label as shown: ``unassigned`` for a dataset that is not in the cohort table (no label)."""
    text = _clean_text(label)
    return text if text else UNASSIGNED_COHORT


def _is_small(n_donors: Any) -> bool:
    """A unit below ``facet_rules.SMALL_COHORT_REPLICATES`` replicates (counted, flagged small)."""
    count = _as_int(n_donors)
    return count is not None and count < facet_rules.SMALL_COHORT_REPLICATES


def _rounded(value: Any, digits: int) -> Optional[float]:
    number = _finite(value)
    return None if number is None else round(number, digits)


def _forest_point(record: Dict[str, Any]) -> Dict[str, Any]:
    """One row of a forest plot from a merged gene row and unit record.

    ``ci_low`` / ``ci_high`` are the limma confidence interval (moderated t) of ``logFC`` as stored by the test
    run (rounded to 5 decimals); draw intervals only from them. ``anchor_counted`` says the row carries its cohort's
    vote. The row is deliberately slim (a gene page carries hundreds of them); the full unit card is in ``units``.
    """
    unit = record.get('summary_unit') or {}
    return {
        'cohort_label': _clean_text(record.get('cohort_label')),
        'db_id': record['db_id'], 'title': record.get('title'), 'cell_type': record['cell_type'],
        'celltype_source': record['celltype_source'], 'pooled': bool(record.get('pooled')),
        'anchor_counted': bool(record.get('counted_effective')),
        'n_donors': _as_int(record.get('n_donors')), 'n_donors_ref': _as_int(record.get('n_donors_ref')),
        'n_donors_test': _as_int(record.get('n_donors_test')), 'small_sample': _is_small(record.get('n_donors')),
        'logFC': _rounded(record.get('logFC'), 5), 'ci_low': _rounded(record.get('ci_low'), 5),
        'ci_high': _rounded(record.get('ci_high'), 5),
        'p_value': _finite(record.get('p_value')), 'q_value': _finite(record.get('q_value')),
        'lambda_gc': _rounded(unit.get('lambda_gc'), 3), 'inflated': unit.get('inflated'),
        'analysis_level': record.get('analysis_level'), 'replicate_unit': unit.get('replicate_unit'),
        'modality': record.get('modality'), 'modality_note': record.get('modality_note'),
        'not_counted_reason': None if record.get('counted_effective') else _not_counted_reason(record),
    }


def _folded_point(record: Dict[str, Any]) -> Dict[str, Any]:
    """A unit folded under ``more_units`` of a forest row: the estimate with its interval and why it is not the
    anchor (the minimum a reader needs; the full row is in the card's ``units``)."""
    return {
        'db_id': record['db_id'], 'title': record.get('title'), 'cell_type': record['cell_type'],
        'celltype_source': record['celltype_source'], 'anchor_counted': bool(record.get('counted_effective')),
        'n_donors': _as_int(record.get('n_donors')),
        'logFC': _rounded(record.get('logFC'), 5), 'ci_low': _rounded(record.get('ci_low'), 5),
        'ci_high': _rounded(record.get('ci_high'), 5), 'q_value': _finite(record.get('q_value')),
        'modality': record.get('modality'),
        'not_counted_reason': None if record.get('counted_effective') else _not_counted_reason(record),
    }


def _anchor_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """The legacy ``anchors`` row of a counted unit: the forest row plus the cohort id, treat p values, independence
    and disease basis (kept for clients of 2.44)."""
    return {**_forest_point(record), 'cohort': _clean_text(record.get('cohort')),
            'p_treat': _finite(record.get('p_treat')), 'q_treat': _finite(record.get('q_treat')),
            'independence': record.get('independence'), 'disease_basis': record.get('disease_basis'),
            'cohort_display': cohort_display(record.get('cohort_label'))}


FOREST_MODES = ('full', 'anchors', 'none')


def forest_view(forest: List[Dict[str, Any]], mode: str) -> List[Dict[str, Any]]:
    """A forest list in the requested detail (a copy; cached cards are shared and must not change).

    ``full``: every row with its folded ``more_units``. ``anchors``: the cohort rows with their counted anchor, the
    folded units left out (``n_more_units`` still says how many). ``none``: no rows. Raises ``FacetResultsError`` for
    another value.
    """
    if mode not in FOREST_MODES:
        raise FacetResultsError(f'forest must be one of {", ".join(FOREST_MODES)}')
    if mode == 'full':
        return forest
    if mode == 'none':
        return []
    return [{**row, 'more_units': []} for row in forest]


def _forest(sub: pd.DataFrame) -> List[Dict[str, Any]]:
    """Forest rows of one cell type group, one per cohort: its counted anchor, the other units folded.

    Counted cohorts come first (by label), then the cohorts that do not count (secondary datasets, units outside
    the cohort table), greyed by the reader: the visible counted rows agree with ``m`` (a counted cohort whose
    anchor has no p value is listed with ``in_m`` false). ``more_units`` are the cohort's other units of the group
    (``anchor_counted`` false unless the cohort has several equal strata, which share its one vote).
    """
    records_sorted = sub.to_dict(orient='records')
    records_sorted.sort(key=lambda r: (not r['counted_effective'], -(_as_int(r.get('n_donors')) or 0),
                                       str(r['db_id']), str(r['cell_type'])))
    by_cohort: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for record in records_sorted:
        by_cohort.setdefault(_clean_text(record.get('cohort')), []).append(record)
    entries: List[Dict[str, Any]] = []
    for cohort, records in by_cohort.items():
        counted = [record for record in records if record.get('counted_effective')]
        shown = counted[0] if counted else records[0]
        rest = [_folded_point(record) for record in records if record is not shown]
        point = _forest_point(shown)
        entries.append({
            'cohort': cohort, 'cohort_label': point['cohort_label'],
            'cohort_display': cohort_display(point['cohort_label']), 'counted': bool(counted),
            'in_m': bool(counted) and point['q_value'] is not None,
            'small': point['small_sample'] if counted else None,
            'anchor': point, 'more_units': rest, 'n_more_units': len(rest),
        })
    entries.sort(key=lambda e: (not e['counted'], str(e['cohort_display']).lower()))
    return entries


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
    'ant-facet-test/2.0': {
        'bh_family': 'Benjamini-Hochberg over the genes of one dataset x stratum x facet (q_value, q_treat)',
        'gene_model': 'limma lmFit on voom weights, then eBayes; limma treat on the same fit (p_treat, q_treat)',
    },
    'ant-facet-test/3.1': {
        'bh_family': 'Benjamini-Hochberg over the genes of one dataset x stratum x facet (q_value, q_treat)',
        'gene_model': ('limma lmFit on voom weights with sample quality weights (voomWithQualityWeights), '
                       'eBayes(robust = TRUE); limma treat on the same fit (p_treat, q_treat)'),
    },
    'ant-facet-test/3.0': {
        'bh_family': 'Benjamini-Hochberg over the genes of one dataset x stratum x facet (q_value, q_treat)',
        'gene_model': 'limma lmFit on voom weights, then eBayes; limma treat on the same fit (p_treat, q_treat)',
    },
    'ant-facet-test-modules/1.0': {},
    'ant-facet-test-modules/2.0': {},
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
    'apoe4_carrier': ('apoe4_carrier', 'e4 carrier vs non-carrier: coefficient = carrier minus non-carrier'),
    'braak': ('braak', 'Braak stage as a number: coefficient = change per Braak stage (linear trend)'),
    'age': ('age', 'age in decades (age / 10): coefficient = change per 10 years (linear trend); NOT adjusted for '
                   'pathology, so the age effect includes the effect of AD pathology prevalence'),
    'apoe4_dose': ('apoe4_dose', 'number of e4 alleles (0, 1, 2): coefficient = change per e4 allele (linear trend)'),
    'braak_threshold': ('braak_threshold', 'high (Braak III or higher) vs low (II or lower): coefficient = high minus low'),
    'age_adjusted': ('age', 'age in decades (age / 10) with the pathology term (Braak, else pathologic AD) in the model: '
                            'coefficient = change per 10 years at equal pathology'),
    'app_nlgf': ('app_nlgf', 'nlgf vs control: coefficient = App NL-G-F knock-in minus control (control is the reference)'),
}


def strongest_facet(cards: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The facet card with the strongest replication (smallest r_value among replicated entries).

    A gene page that opens on one facet makes a selection; this names it so the page can say so.

    Args:
        cards: ``facet -> gene_effects`` card.

    Returns:
        ``{facet, cell_type, r_value, scope}`` or None when no facet replicates.
    """
    best: Optional[Dict[str, Any]] = None
    for facet, card in cards.items():
        for entry in card.get('replication') or []:
            r_value = entry.get('r_value')
            if entry.get('replicated') and r_value is not None and (best is None or r_value < best['r_value']):
                best = {'facet': facet, 'cell_type': entry.get('cell_type'), 'r_value': r_value,
                        'scope': card.get('scope')}
    return best


def summary_unit_value(record: Dict[str, Any], key: str) -> Any:
    """A field of the summary unit dict a result row was joined to (None when absent)."""
    return (record.get('summary_unit') or {}).get(key)


def unit_notes(unit: Dict[str, Any]) -> List[str]:
    """Plain-language caveats of one facet unit (what a reader must keep in mind), from its facts."""
    notes: List[str] = []
    facet = unit.get('facet')
    if facet == 'age':
        notes.append('age, not adjusted for pathology: age and AD pathology are correlated in every brain bank; '
                     'read it next to the pathology-adjusted model')
    if facet == 'age_adjusted':
        notes.append('age, adjusted for pathology (Braak, else pathologic AD)')
    if facet in ('age', 'age_adjusted'):
        notes.append('10 percent per decade of age is the effect-gate threshold (46 percent over four decades)')
        if unit.get('age_top_coded'):
            notes.append(f"ages are top-coded at {unit['age_top_coded']:.0f}: the slope is attenuated at the top")
    if facet == 'app_nlgf':
        notes.append('detects large effects only (a handful of animals per genotype)')
        if unit.get('facet_scope') == facet_scopes.SCOPE_XENOGRAFT:
            notes.append('live-cell isolation: dissociation-response genes (FOS, JUN, HSP, NFKBIA) are suspect')
        basis = unit.get('control_basis')
        if basis:
            notes.append({'wt_host': 'control = wild-type host', 'humanised_app_host': 'control = humanised-App host '
                          '(NL-G-F vs AppHuHu isolates the three FAD mutations)'}.get(basis, f'control basis {basis}'))
        if unit.get('design_subset'):
            notes.append(f"stratified: {str(unit['design_subset']).replace('|', ', ')}")
    if unit.get('inflated'):
        notes.append(f"genomic inflation lambda {unit['lambda_gc']:.2f}: most genes shift together, either strong "
                     f"biology or a unit-wide technical shift (batch, composition, depth); read the p values with care")
    if (unit.get('n_sex_discordant') or 0) > 0:
        notes.append(f"{unit['n_sex_discordant']} of {unit.get('n_sex_checked')} replicates express the other sex "
                     f"than their label")
    flags = unit.get('flags') or {}
    if flags.get('small_sample'):
        if unit.get('analysis_level') == replicates.LEVEL_BELOW_FLOOR or flags.get('descriptive_only'):
            notes.append('small sample: below the p-value floor of its replicate unit, descriptive only, not counted')
        else:
            notes.append('small sample: low power')
    return notes


def _stratum_source_text(source: str) -> str:
    if '@' in source:
        base, subset = source.split('@', 1)
        return f'{_stratum_source_text(base)}, stratum {subset.replace("|", ", ")}'
    if source in _STRATUM_SOURCES:
        return _STRATUM_SOURCES[source]
    if source.endswith('_author_cluster'):
        return f'author clusters ({source}), dataset-scoped, expression-defined groups'
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
    df_reason = 'dropped to keep the test and at least 2 residual degrees of freedom'
    host = (manifest or {}).get('facet_scope') == facet_scopes.SCOPE_XENOGRAFT
    if facet != 'sex':
        sex_label = 'host sex (m vs f)' if host else 'm vs f'
        if 'sex' in included:
            rows.append({'name': 'sex', 'status': 'included', 'term': 'sex', 'reason': sex_label})
        elif 'sex(df)' in omitted:
            rows.append({'name': 'sex', 'status': 'dropped', 'term': '', 'reason': df_reason})
        elif 'sex' in omitted:
            rows.append({'name': 'sex', 'status': 'dropped', 'term': '',
                         'reason': f'{"host " if host else ""}sex known for fewer than {coverage_text} of replicates '
                                   f'or only one sex present'})
        else:
            rows.append({'name': 'sex', 'status': 'not used', 'term': '', 'reason': NOT_RECORDED})
    for name in ('age', 'pmi'):
        if name == facet:
            continue
        if name in included:
            rows.append({'name': name, 'status': 'included', 'term': f'{name}_z',
                         'reason': 'z-scored within the unit'})
        elif f'{name}(df)' in omitted:
            rows.append({'name': name, 'status': 'dropped', 'term': '', 'reason': df_reason})
        elif name in omitted:
            rows.append({'name': name, 'status': 'dropped', 'term': '',
                         'reason': f'known for fewer than {coverage_text} of donors, or constant'})
        elif total is not None and isinstance(min_age_pmi, (int, float)) and total < min_age_pmi:
            rows.append({'name': name, 'status': 'not used', 'term': '',
                         'reason': f'fewer than {int(min_age_pmi)} donors in the unit'})
        else:
            rows.append({'name': name, 'status': 'not used', 'term': '',
                         'reason': 'no values in this dataset'})
    for entry_name in included:
        if entry_name.startswith('pathology:'):
            rows.append({'name': 'pathology', 'status': 'included', 'term': 'pathology',
                         'reason': f'{entry_name.split(":", 1)[1]} (the age-adjusted model)'})
    batch_column = (manifest or {}).get('batch_column') if manifest else None
    if 'batch' in included:
        rows.append({'name': 'batch', 'status': 'included', 'term': 'batch',
                     'reason': f'donor-level column {batch_column}' if batch_column else 'donor-level batch'})
    elif record.get('batch_confounded'):
        rows.append({'name': 'batch', 'status': 'dropped', 'term': '',
                     'reason': f'{batch_column or "batch"} is confounded with the facet'})
    elif 'batch(df)' in omitted:
        rows.append({'name': 'batch', 'status': 'dropped', 'term': '', 'reason': df_reason})
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


def dataset_facet_note(facet: str, gene_manifest: Optional[dict], module_manifest: Optional[dict]) -> Optional[str]:
    """The dataset's own definition note for a facet, from the run manifests.

    The tools copy the ``facet_notes`` front-matter key of the dataset .md
    into the manifest (for example: ant_disease is a pathologic AD proxy, not a
    clinical diagnosis). Manifests from earlier tool runs have no such key.

    Args:
        facet: Facet name without the ``ant_`` prefix.
        gene_manifest: Gene-level run manifest or None.
        module_manifest: Module-level run manifest or None.

    Returns:
        The note text, or None when the dataset has none for this facet.
    """
    facet = canonical_facet(facet) if facet else facet
    for manifest in (gene_manifest, module_manifest):
        notes = (manifest or {}).get('facet_notes')
        if isinstance(notes, dict) and notes.get(facet):
            return str(notes[facet])
    return None


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
    floors_level = (parameters.get('p_floor_per_level_by_unit') or {})
    floors_numeric = (parameters.get('p_floor_numeric_by_unit') or {})
    if floors_level and floors_numeric:
        rule = ('descriptive (estimate and SE only, no p, q or CI) below the p-value floor of the replicate unit: '
                f'two-group facets {", ".join(f"{k} {v}" for k, v in floors_level.items())} per level; trends '
                f'{", ".join(f"{k} {v}" for k, v in floors_numeric.items())} replicates')
    elif n_lt5 is not None and numeric_min is not None:
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
    exclusion = ((gene_manifest or {}).get('donor_exclusion') or (module_manifest or {}).get('donor_exclusion'))
    if isinstance(exclusion, dict):
        reason = (exclusion.get('rule') or {}).get('reason') or 'donor exclusion rule of the dataset'
        if exclusion.get('counted'):
            donors['exclusion'] = (f'{exclusion.get("n_donors_excluded")} of {exclusion.get("n_donors_before")} '
                                   f'donors left out: {reason}')
        else:
            donors['exclusion'] = (f'full donor set (not counted); the counted run leaves out '
                                   f'{exclusion.get("n_donors_excluded")} donors: {reason}')
    facet_term = _FACET_TERMS.get(facet, (facet, NOT_RECORDED))[1]
    facet_note = dataset_facet_note(facet, gene_manifest, module_manifest)
    if facet_note:
        facet_term = f'{facet_term}. Dataset definition: {facet_note}'
    model: Dict[str, Any] = {
        'stratum': {'cell_type': unit['cell_type'], 'source': unit['celltype_source'],
                    'description': _stratum_source_text(unit['celltype_source'])},
        'facet_term': facet_term,
        'facet_note': facet_note,
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
