"""Catalogue fields of a dataset ``.md`` for the ingestion routes.

Used by ``bin/ingest/ant-ingest`` and ``bin/ingest/ant-ingest-watch`` (design
section 13, "S3 watch route" in ``doc/DESIGN_dataset_ingestion.md``). Three
jobs:

* :func:`profile_h5ad` reads an ``.h5ad`` without loading its matrices: cell
  and gene counts, gene names, obs columns with their kind, obsm keys, and
  per-layer statistics streamed over row chunks.
* :func:`fill_catalog_fields` fills a new dataset's ``.md`` front matter
  **non-destructively**: a key is written only when the curator left it
  unset (``sha256`` and the counts are facts of the file and always follow
  it). A new dataset starts at ``version: 1.0.0`` with one changelog entry.
* :func:`plan_update` compares the store's current dataset with an upload
  for the same path and returns the version bump (``minor`` when the h5ad
  changed, ``patch`` when only the sidecar did) with the changelog lines;
  :func:`update_md_text` applies it to the old ``.md`` with the verified
  minimal edits of :mod:`md_edit`, so caveats, curation fields and facet
  mappings stay as they are.

:func:`merge_sidecar_store_wins` merges an updated ``.h5ad.mus``: curated
store values win unless the deposit sets ``override_<key>=true``, new keys
are adopted, conflicts are reported for the changelog, a change of
``access`` or ``license`` is reported for a person to decide, and a change
of ``data_use`` is applied and reported for the changelog.

Nothing here talks to S3 or the server; nothing writes outside the paths the
caller names.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

import md_edit
from obs_derive_forms import api_source_columns
from sidecar_merge import MACHINE_KEYS, S3_KEYS, parse_lines

#: Categorical obs columns with more distinct values than this are not
#: listed in ``obscat_include`` by default (cell ids, barcodes).
MAX_DEFAULT_OBSCAT_VALUES = 1000
#: Row chunk for the streamed layer statistics.
STATS_CHUNK_ROWS = 20_000
#: Non-zero values kept for the median and q90 (an evenly thinned sample).
STATS_QUANTILE_SAMPLE = 2_000_000
#: Sidecar keys that change with every deposit and are not reported as sidecar changes
#: (the machine keys of sidecar_merge plus the file hash, reported as a data change instead).
UNREPORTED_SIDECAR_KEYS = MACHINE_KEYS | {'sha256'}
#: Sidecar keys that may name the person who deposited the data.
DEPOSITOR_KEYS = ('depositor', 'depositor_email')


@dataclass
class H5adProfile:
    """What the catalogue needs to know about one ``.h5ad``."""
    n_obs: int
    n_vars: int
    gene_names: List[str]
    obs_numeric: List[str]
    obs_categorical: Dict[str, int]          # column -> distinct values
    obsm_keys: List[str]
    layer_stats: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def obs_columns(self) -> Set[str]:
        return set(self.obs_numeric) | set(self.obs_categorical)


# ── reading the h5ad ─────────────────────────────────────────────────────────

def _decode(values: Any) -> List[str]:
    return [value.decode() if isinstance(value, bytes) else str(value) for value in values]


def _strings(element: Any) -> List[str]:
    """Values of a string array: a plain dataset, or a ``nullable-string-array`` group (pandas 3 writes these)."""
    if hasattr(element, 'dtype'):
        return _decode(element[()])
    if 'values' in element:
        return _decode(element['values'][()])
    return []


def _index_of(group: Any) -> List[str]:
    """Index (obs or var names) of an anndata dataframe group."""
    index_key = group.attrs.get('_index', '_index')
    if isinstance(index_key, bytes):
        index_key = index_key.decode()
    if index_key not in group:
        return []
    return _strings(group[index_key])


def _obs_columns(obs_group: Any) -> Tuple[List[str], Dict[str, int]]:
    """Numeric and categorical obs columns of an anndata obs group (h5py)."""
    numeric: List[str] = []
    categorical: Dict[str, int] = {}
    index_key = obs_group.attrs.get('_index', '_index')
    if isinstance(index_key, bytes):
        index_key = index_key.decode()
    order = obs_group.attrs.get('column-order', [])
    names = _decode(order) if len(order) else [name for name in obs_group.keys() if name != index_key]
    for name in names:
        if name not in obs_group or name == index_key:
            continue
        element = obs_group[name]
        encoding = element.attrs.get('encoding-type', b'')
        encoding = encoding.decode() if isinstance(encoding, bytes) else str(encoding)
        if encoding == 'categorical':
            categorical[name] = int(element['categories'].shape[0])
        elif hasattr(element, 'dtype'):
            kind = element.dtype.kind
            if kind in 'iuf':
                numeric.append(name)
            elif kind == 'b':
                categorical[name] = 2
            else:
                categorical[name] = len(set(_decode(element[()])))
        elif encoding == 'nullable-string-array':
            categorical[name] = len(set(_strings(element)))
        elif encoding in ('nullable-integer', 'nullable-boolean'):
            if encoding == 'nullable-boolean':
                categorical[name] = 2
            else:
                numeric.append(name)
        else:
            categorical[name] = MAX_DEFAULT_OBSCAT_VALUES + 1
    return numeric, categorical


class _StatsAccumulator:
    """Streamed min / max / zeros over row chunks; quantiles from a thinned sample of non-zeros."""

    def __init__(self, total_elements: int, dtype: str):
        self.total_elements = total_elements
        self.dtype = dtype
        self.minimum: Optional[float] = None
        self.maximum: Optional[float] = None
        self.min_nonzero: Optional[float] = None
        self.nonzero_count = 0
        self.samples: List[np.ndarray] = []
        self.sampled = 0

    def add_values(self, values: np.ndarray) -> None:
        values = values[values != 0]
        self.nonzero_count += int(values.size)
        if values.size:
            low, high = float(values.min()), float(values.max())
            self.maximum = high if self.maximum is None else max(self.maximum, high)
            self.minimum = low if self.minimum is None else min(self.minimum, low)
            self.min_nonzero = low if self.min_nonzero is None else min(self.min_nonzero, low)
            budget = STATS_QUANTILE_SAMPLE - self.sampled
            if budget > 0:
                step = max(1, int(np.ceil(values.size / budget)))
                kept = values[::step]
                self.samples.append(kept.astype(np.float64))
                self.sampled += kept.size

    def result(self) -> Dict[str, Any]:
        num_zeros = self.total_elements - self.nonzero_count
        sample = np.concatenate(self.samples) if self.samples else np.zeros(0)
        minimum = self.minimum if self.minimum is not None else 0.0
        if num_zeros > 0:
            minimum = min(minimum, 0.0)
        return {
            'dtype': self.dtype,
            'min_value': float(minimum),
            'min_nonzero': float(self.min_nonzero or 0.0),
            'max_value': float(max(self.maximum, 0.0) if num_zeros and self.maximum is not None
                               else (self.maximum or 0.0)),
            'median': float(np.median(sample)) if sample.size else 0.0,
            'q90': float(np.percentile(sample, 90)) if sample.size else 0.0,
            'num_zeros': int(num_zeros),
            'pct_zeros': float(num_zeros / self.total_elements * 100) if self.total_elements else 0.0,
        }


def _matrix_stats(element: Any, n_obs: int, n_vars: int) -> Optional[Dict[str, Any]]:
    """Statistics of one matrix element (dense dataset or csr/csc group), streamed."""
    total = n_obs * n_vars
    if hasattr(element, 'shape') and hasattr(element, 'dtype'):          # dense h5py dataset
        accumulator = _StatsAccumulator(total, str(element.dtype))
        for start in range(0, element.shape[0], STATS_CHUNK_ROWS):
            block = np.asarray(element[start:start + STATS_CHUNK_ROWS]).ravel()
            accumulator.add_values(block)
        return accumulator.result()
    if 'data' in element:                                                 # sparse: the stored values
        data = element['data']
        accumulator = _StatsAccumulator(total, str(data.dtype))
        chunk = STATS_CHUNK_ROWS * 1000
        for start in range(0, data.shape[0], chunk):
            accumulator.add_values(np.asarray(data[start:start + chunk]))
        return accumulator.result()
    return None


def profile_h5ad(h5ad_path: Path, with_layer_stats: bool = True) -> H5adProfile:
    """Profile an ``.h5ad`` with h5py only (no matrix is loaded whole).

    Args:
        h5ad_path: The file.
        with_layer_stats: Compute ``layer_stats`` for X and every layer
            (streams the stored values once).

    Returns:
        The profile. Median and q90 are of the non-zero values, from an evenly
        thinned sample of at most :data:`STATS_QUANTILE_SAMPLE` values.
    """
    import h5py  # noqa: PLC0415 - optional for callers that never profile

    with h5py.File(h5ad_path, 'r') as handle:
        obs_names = _index_of(handle['obs'])
        gene_names = _index_of(handle['var'])
        numeric, categorical = _obs_columns(handle['obs'])
        obsm_keys = sorted(handle['obsm'].keys()) if 'obsm' in handle else []
        profile = H5adProfile(n_obs=len(obs_names), n_vars=len(gene_names), gene_names=gene_names,
                              obs_numeric=numeric, obs_categorical=categorical, obsm_keys=obsm_keys)
        if with_layer_stats:
            elements = []
            if 'X' in handle:
                elements.append(('X', handle['X']))
            if 'layers' in handle:
                elements.extend((name, handle['layers'][name]) for name in sorted(handle['layers'].keys()))
            for name, element in elements:
                stats = _matrix_stats(element, profile.n_obs, profile.n_vars)
                if stats is not None:
                    profile.layer_stats[name] = stats
    return profile


# ── new datasets ─────────────────────────────────────────────────────────────

def _first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def unique_file_id(basename: str, group_id: Optional[str], taken_db_ids: Set[str]) -> Optional[str]:
    """A 3-character dataset id for ``basename`` that is free in its group, or None."""
    from md_check import generate_file_id  # noqa: PLC0415

    taken = {db_id.upper() for db_id in taken_db_ids}
    group = (group_id or '').upper()
    candidate = generate_file_id(basename)
    if not group_id or f'{group}/{candidate.upper()}' not in taken:
        return candidate
    letters = [char for char in basename.upper() if char.isalnum()]
    for first in letters[:1] or ['X']:
        for second in letters + list('ABCDEFGHIJKLMNOPQRSTUVWXYZ'):
            for third in letters + list('0123456789'):
                candidate = f'{first}{second}{third}'
                if f'{group}/{candidate}' not in taken:
                    return candidate
    return None


def ingest_block(route: str, source_key: str, etag: str, depositor: Optional[str], today: str) -> Dict[str, Any]:
    """The ``ingest:`` block recorded in a dataset ``.md`` by the ingestion routes."""
    block: Dict[str, Any] = {'route': route, 'source_key': source_key, 'etag': etag, 'date': today}
    if depositor:
        block['depositor'] = depositor
    return block


def fill_catalog_fields(meta: Dict[str, Any], profile: H5adProfile, sha256: str, sidecar: Dict[str, Any],
                        basename: str, group_id: Optional[str], taken_db_ids: Set[str], today: str,
                        ingest: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], List[str]]:
    """Fill the catalogue fields of a **new** dataset's ``.md`` without overwriting the curator's keys.

    Args:
        meta: Front matter the depositor supplied (may be empty).
        profile: :func:`profile_h5ad` of the uploaded file.
        sha256: The verified hash of the uploaded h5ad.
        sidecar: Parsed ``.h5ad.mus`` of the upload.
        basename: Stem of the h5ad.
        group_id: The group id (from ``index.md``), used for the id uniqueness test.
        taken_db_ids: ``group_id/id`` pairs already in the store.
        today: ISO date.
        ingest: Optional ``ingest:`` block (route, source key, ETag, depositor).

    Returns:
        ``(new front matter, names of the keys that were filled)``.
    """
    result = dict(meta)
    filled: List[str] = []

    def put(key: str, value: Any) -> None:
        if value in (None, '', [], {}):
            return
        if result.get(key) in (None, '', [], {}):
            result[key] = value
            filled.append(key)

    put('title', sidecar.get('title') or basename)
    if not result.get('id'):
        put('id', unique_file_id(basename, group_id, taken_db_ids))
    put('year', _first(sidecar.get('year')))
    put('description', sidecar.get('description'))
    # Facts of the file: always the uploaded file's values.
    for key, value in (('sha256', sha256), ('n_obs', profile.n_obs), ('n_vars', profile.n_vars)):
        if result.get(key) != value:
            result[key] = value
            filled.append(key)
    put('default_layer', 'X')
    put('layers_include', ['X'] + [name for name in profile.layer_stats if name != 'X'])
    put('obsm_include', {key: key for key in profile.obsm_keys})
    put('obsnum_include', list(profile.obs_numeric))
    # api: sources are always stored, whatever their number of values (a donor column with 1,499 donors)
    api_sources = set(api_source_columns(result.get('api') if isinstance(result.get('api'), dict) else None))
    put('obscat_include', [name for name, count in profile.obs_categorical.items()
                           if count <= MAX_DEFAULT_OBSCAT_VALUES or name in api_sources])
    if profile.layer_stats:
        put('layer_stats', profile.layer_stats)
    if not result.get('version'):
        result['version'] = '1.0.0'
        filled.append('version')
        if not result.get('changelog'):
            result['changelog'] = [{
                'version': '1.0.0', 'date': today, 'sha256': sha256,
                'changes': ['First version, ingested from an S3 deposit.' if ingest and ingest.get('route') == 's3-watch'
                            else 'First version.'],
            }]
            filled.append('changelog')
    put('date', today)
    if ingest and 'ingest' not in result:
        result['ingest'] = ingest
        filled.append('ingest')
    return result, filled


def coerce_year(meta: Dict[str, Any]) -> Dict[str, Any]:
    """``year`` as an integer when it is a quoted number (``'2024'``): clients read it as a number."""
    value = meta.get('year')
    if isinstance(value, str) and re.fullmatch(r'\s*\d{4}\s*', value):
        meta['year'] = int(value)
    return meta


def build_index_md(sidecar: Dict[str, Any], group: str, taken_group_ids: Set[str],
                   default_dataset: str) -> Tuple[Dict[str, Any], str]:
    """A minimal ``index.md`` for a **new** group from the sidecar of its first dataset.

    Only what the sidecar states is written (``doi``, ``pmid``, ``year``,
    ``paper_title`` / ``authors`` / ``journal`` when present). A missing paper
    title or author list makes ``meta.index_md`` fail in ``ant-ingest-check``,
    which is the intended outcome: a person completes the index.md.
    """
    from md_check import generate_group_id  # noqa: PLC0415

    group_id = generate_group_id(group)
    if group_id.upper() in {taken.upper() for taken in taken_group_ids}:
        group_id = None
    meta: Dict[str, Any] = {'group': group}
    if group_id:
        meta['group_id'] = group_id
    for key in ('doi', 'pmid', 'year', 'journal', 'paper_title'):
        value = _first(sidecar.get(key))
        if value:
            meta[key] = value
    authors = sidecar.get('authors')
    if authors:
        meta['authors'] = authors if isinstance(authors, list) else [part.strip() for part in str(authors).split(';')]
    meta['default_dataset'] = default_dataset
    body = f'# {group}\n'
    return meta, body


_NAME_RE = re.compile(r"^[^\W\d_][\w .,'-]{0,79}$")
_EMAIL_RE = re.compile(r'^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$')


def depositor_of(sidecar: Dict[str, Any]) -> Optional[str]:
    """``Name <email>`` usable as a git author, from ``depositor`` / ``depositor_email``; None when absent or invalid.

    The name may hold letters, spaces and ``.,'-``; the e-mail must look like
    one. Anything else is refused (see :func:`depositor_note`), so a sidecar
    cannot smuggle options or odd bytes into a git command.
    """
    name = str(sidecar.get('depositor') or '').strip()
    email = str(sidecar.get('depositor_email') or '').strip()
    if not email or not _EMAIL_RE.fullmatch(email):
        return None
    if not name:
        name = email.split('@')[0]
    if not _NAME_RE.fullmatch(name):
        return None
    return f'{name} <{email}>'


def depositor_note(sidecar: Dict[str, Any]) -> Optional[str]:
    """Changelog line for a depositor that is stated but not usable as a git author."""
    raw = ' '.join(str(sidecar.get(key) or '') for key in DEPOSITOR_KEYS).strip()
    if not raw or depositor_of(sidecar):
        return None
    cleaned = ''.join(char for char in raw if char.isprintable())[:120]
    return f'Depositor as stated in the sidecar (not used as the git author): {cleaned!r}.'


# ── sidecar merge for updates ────────────────────────────────────────────────

#: Keys whose change on an update always needs a person (publication terms).
GUARDED_SIDECAR_KEYS = ('access', 'license')
#: Keys the depositor declares: a change on an update is applied as plain metadata (patch version, changelog
#: line, caveat). ``data_use`` moved here from the guarded keys in 2.47.0: data use agreements apply to a whole
#: server (decision D7 revised 2026-10-20), so a change neither holds nor stops an update.
DECLARED_SIDECAR_KEYS = ('data_use',)
OVERRIDE_PREFIX = 'override_'


@dataclass
class SidecarMerge:
    """Result of :func:`merge_sidecar_store_wins`."""
    text: str
    adopted: List[Tuple[str, str, str]] = field(default_factory=list)       # (key, '', new)
    overridden: List[Tuple[str, str, str]] = field(default_factory=list)    # (key, old, new)
    conflicts: List[Tuple[str, str, str]] = field(default_factory=list)     # (key, store, deposit): store kept
    guarded: List[Tuple[str, str, str]] = field(default_factory=list)       # access / license changes
    declared: List[Tuple[str, str, str]] = field(default_factory=list)      # data_use changes, applied

    @property
    def changes(self) -> List[Tuple[str, str, str]]:
        return self.adopted + self.overridden


def _pairs(text: str) -> Dict[str, str]:
    return {key: value for kind, key, value in parse_lines(text) if kind == 'pair'}


def merge_sidecar_store_wins(store_text: str, incoming_text: str) -> SidecarMerge:
    """Merge an updated ``.h5ad.mus`` into the store's, protecting curated values.

    * a key only in the deposit is adopted;
    * a key in both with another value keeps the **store** value (a conflict,
      recorded) unless the deposit also sets ``override_<key>=true``;
    * machine keys (``s3_*``, ``sha256``, ...) follow the deposit;
    * ``access`` and ``license`` changes are reported in
      ``guarded``: the caller stops the item for a person, override or not;
    * a ``data_use`` change follows the deposit and is reported in
      ``declared`` (the caller writes a changelog line and a caveat);
    * ``override_*`` lines are not written.
    """
    incoming = _pairs(incoming_text)
    overrides = {key[len(OVERRIDE_PREFIX):] for key, value in incoming.items()
                 if key.startswith(OVERRIDE_PREFIX) and value.strip().lower() in ('true', '1', 'yes')}
    result = SidecarMerge(text='')
    out_lines: List[str] = []
    written: Set[str] = set()
    for kind, key, raw in parse_lines(store_text):
        if kind != 'pair':
            out_lines.append(raw)
            continue
        if key in written:
            continue
        written.add(key)
        value = raw
        if key in incoming and incoming[key].strip() != raw.strip():
            old, new = raw.strip(), incoming[key].strip()
            if key in GUARDED_SIDECAR_KEYS:
                result.guarded.append((key, old, new))
            elif key in DECLARED_SIDECAR_KEYS:
                value = incoming[key]
                result.declared.append((key, old, new))
            elif key in S3_KEYS:
                pass                                  # the deposit's stamps describe the incoming copy
            elif key in UNREPORTED_SIDECAR_KEYS:
                value = incoming[key]
            elif key in overrides:
                value = incoming[key]
                result.overridden.append((key, old, new))
            else:
                result.conflicts.append((key, old, new))
        out_lines.append(f'{key}={value}')
    new_keys = sorted(key for key in incoming if key not in written and not key.startswith(OVERRIDE_PREFIX)
                      and key not in S3_KEYS)
    if new_keys and out_lines and out_lines[-1].strip():
        out_lines.append('')
    for key in new_keys:
        out_lines.append(f'{key}={incoming[key]}')
        if key in GUARDED_SIDECAR_KEYS:
            result.guarded.append((key, '', incoming[key].strip()))
        elif key not in UNREPORTED_SIDECAR_KEYS:
            result.adopted.append((key, '', incoming[key].strip()))
    text = '\n'.join(out_lines)
    result.text = text if text.endswith('\n') else text + '\n'
    return result


#: Keys of a dataset ``.md`` the route manages itself; a deposited seed never sets them on an update.
SEED_MANAGED_KEYS = frozenset({'version', 'changelog', 'sha256', 'n_obs', 'n_vars', 'layer_stats', 'ingest',
                               'updated', 'date', 'caveats'})
#: Keys of a dataset ``.md`` the depositor owns: an update by the dataset owner or an admin applies them (2.47.0).
#: Every other key is curated (a person edits it); access, license and data_use live in the sidecar.
DEPOSITOR_EDITABLE_KEYS = ('api', 'obscat_include', 'obsnum_include', 'obs_exclude', 'disease_basis',
                           'disease_derivation', 'description', 'title', 'replicate_unit', 'replicate_column',
                           'is_secondary', 'reanalysis_of', 'donor_group', 'facet_donor_exclusions',
                           'counts_unavailable', 'normalisation', 'year', 'qc', 'qc_of')
#: Editable keys that change what ant-convert builds: a change rebuilds the views.
VIEW_KEYS = frozenset({'api', 'obscat_include', 'obsnum_include', 'obs_exclude'})


@dataclass
class SeedDelta:
    """What a deposited ``<basename>.md`` seed changes in a published dataset (:func:`seed_md_changes`)."""
    new_caveats: List[Dict[str, Any]] = field(default_factory=list)
    editable: Dict[str, Tuple[Any, Any]] = field(default_factory=dict)      # key -> (store value, seed value)
    conflicts: List[str] = field(default_factory=list)                     # curated keys that differ

    @property
    def rebuild_views(self) -> bool:
        return bool(VIEW_KEYS & set(self.editable))


def seed_md_changes(seed_meta: Dict[str, Any], store_meta: Dict[str, Any]) -> SeedDelta:
    """Split a deposited seed against the store ``.md``: new caveats, depositor-editable changes, curated conflicts.

    Caveats count as new when their message is not in the store yet. A key the
    seed leaves out is not a change (the store keeps it).
    """
    known = {str(caveat.get('message')) for caveat in (store_meta.get('caveats') or []) if isinstance(caveat, dict)}
    delta = SeedDelta()
    delta.new_caveats = [caveat for caveat in (seed_meta.get('caveats') or [])
                         if isinstance(caveat, dict) and caveat.get('message') and str(caveat['message']) not in known]
    for key in sorted(seed_meta):
        value = seed_meta[key]
        if key in SEED_MANAGED_KEYS or store_meta.get(key) == value:
            continue
        if key in DEPOSITOR_EDITABLE_KEYS:
            delta.editable[key] = (store_meta.get(key), value)
        else:
            delta.conflicts.append(key)
    return delta


def seed_md_delta(seed_meta: Dict[str, Any], store_meta: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """``(new caveats, keys that differ)``: the pre-2.47 view of :func:`seed_md_changes` (editable and curated)."""
    delta = seed_md_changes(seed_meta, store_meta)
    return delta.new_caveats, sorted(set(delta.conflicts) | set(delta.editable))


def describe_change(key: str, old: Any, new: Any) -> str:
    """One changelog line for a changed ``.md`` key, short enough to read (mappings name their changed parts)."""
    def short(value: Any) -> str:
        text = repr(value)
        return text if len(text) <= 80 else text[:77] + '...'

    if isinstance(old, dict) and isinstance(new, dict):
        changed = sorted(str(part) for part in set(old) | set(new) if old.get(part) != new.get(part))
        return f'.md {key}: changed {", ".join(changed[:12])}' + (f' and {len(changed) - 12} more' if len(changed) > 12 else '') \
            + ' (deposited .md).'
    if isinstance(old, list) and isinstance(new, list):
        added = [item for item in new if item not in old]
        removed = [item for item in old if item not in new]
        return (f'.md {key}: ' + ', '.join(part for part in (f'added {short(added)}' if added else '',
                                                              f'removed {short(removed)}' if removed else '') if part)
                + ' (deposited .md).') if (added or removed) else f'.md {key}: reordered (deposited .md).'
    if old is None:
        return f'.md {key} set to {short(new)} (deposited .md).'
    return f'.md {key}: {short(old)} -> {short(new)} (deposited .md).'


# ── updates of an existing dataset ───────────────────────────────────────────

@dataclass
class UpdatePlan:
    """What an upload changes in a dataset that is already in the store."""
    part: Optional[str]                     # 'minor', 'patch' or None (nothing changed)
    changes: List[str]
    previous_sha256: Optional[str]
    sidecar_changes: List[Tuple[str, str, str]]


def _short(values: Sequence[str], limit: int = 8) -> str:
    shown = ', '.join(sorted(values)[:limit])
    return shown + (f' and {len(values) - limit} more' if len(values) > limit else '')


def plan_update(old_meta: Dict[str, Any], old_profile: Optional[H5adProfile], new_profile: Optional[H5adProfile],
                old_sha: str, new_sha: str, sidecar_changes: List[Tuple[str, str, str]],
                extra_changes: Sequence[str] = (), notes: Sequence[str] = ()) -> UpdatePlan:
    """Version bump and changelog lines for an upload to an existing dataset path.

    ``minor`` when the h5ad hash differs, ``patch`` when only sidecar keys
    changed, ``None`` when the upload is identical to what is published.
    """
    changes: List[str] = []
    data_changed = (not old_sha) or old_sha != new_sha
    if data_changed:
        changes.append(f'New data from an S3 deposit: h5ad sha256 {old_sha[:12] or "unknown"} -> {new_sha[:12]}.')
        if old_profile and new_profile:
            if old_profile.n_obs != new_profile.n_obs:
                changes.append(f'Cells: {old_profile.n_obs} -> {new_profile.n_obs}.')
            old_genes, new_genes = set(old_profile.gene_names), set(new_profile.gene_names)
            if old_genes != new_genes:
                changes.append(f'Genes: {len(old_genes)} -> {len(new_genes)} '
                               f'({len(new_genes - old_genes)} added, {len(old_genes - new_genes)} removed).')
            added = new_profile.obs_columns - old_profile.obs_columns
            removed = old_profile.obs_columns - new_profile.obs_columns
            if added:
                changes.append(f'obs columns added: {_short(list(added))}.')
            if removed:
                changes.append(f'obs columns removed: {_short(list(removed))}.')
        elif new_profile and old_meta.get('n_obs') not in (None, new_profile.n_obs):
            changes.append(f'Cells: {old_meta.get("n_obs")} -> {new_profile.n_obs}.')
    for key, old_value, new_value in sidecar_changes:
        if old_value:
            changes.append(f'Sidecar {key}: {old_value!r} -> {new_value!r} (override in the deposit).')
        else:
            changes.append(f'Sidecar {key} added: {new_value!r}.')
    changes.extend(extra_changes)
    if not data_changed and not sidecar_changes and not extra_changes:
        return UpdatePlan(None, [], None, [])
    changes.extend(notes)
    return UpdatePlan('minor' if data_changed else 'patch', changes, old_sha if data_changed else None,
                      sidecar_changes)


def update_md_text(old_text: str, plan: UpdatePlan, new_sha: str, new_profile: Optional[H5adProfile],
                   today: str, caveat: Optional[Dict[str, Any]] = None,
                   ingest: Optional[Dict[str, Any]] = None,
                   extra_caveats: Sequence[Dict[str, Any]] = (),
                   set_keys: Optional[Dict[str, Any]] = None) -> str:
    """Apply an :class:`UpdatePlan` to the store's ``.md`` text with verified minimal edits.

    Only ``sha256``, ``n_obs``, ``n_vars``, ``layer_stats`` (when the old
    file had them), ``version``, ``updated``, ``changelog``, the ``ingest``
    block, the appended caveats and the ``set_keys`` (depositor-editable keys
    of a deposited seed, :data:`DEPOSITOR_EDITABLE_KEYS`) change; everything
    else (curation fields, the body) stays byte for byte.
    """
    if plan.part is None:
        return old_text
    import yaml  # noqa: PLC0415

    front = md_edit.FRONT_RE.match(old_text)
    old_meta = (yaml.safe_load(front.group(1)) or {}) if front else {}
    operations: List[Dict[str, Any]] = []
    if old_meta.get('sha256') != new_sha:
        operations.append(md_edit.set_top('sha256', new_sha))
    if new_profile is not None and plan.part == 'minor':
        for key, value in (('n_obs', new_profile.n_obs), ('n_vars', new_profile.n_vars)):
            if old_meta.get(key) != value:
                operations.append(md_edit.set_top(key, value))
        if 'layer_stats' in old_meta and new_profile.layer_stats:
            operations.append(md_edit.set_top('layer_stats', new_profile.layer_stats))
    if ingest is not None:
        operations.append(md_edit.set_top('ingest', ingest))
    for key, value in (set_keys or {}).items():
        if key not in DEPOSITOR_EDITABLE_KEYS:
            raise ValueError(f'{key} is not a depositor-editable key')
        operations.append(md_edit.set_top(key, value))
    for item in list(extra_caveats) + ([caveat] if caveat is not None else []):
        operations.append(md_edit.append_top('caveats', item))
    if not old_meta.get('version'):
        operations.append(md_edit.set_top('version', '0.0.0'))     # archived as 0.0.0, bumped from there
    operations.append(md_edit.bump_version(plan.changes, today, part=plan.part,
                                           previous_sha256=plan.previous_sha256))
    return md_edit.apply_edits(old_text, operations)


def curation_caveat(message: str, topic: str, today: str, detected_by: str = 'ant-ingest-watch') -> Dict[str, Any]:
    """A caveat entry in the store's format for a decision the ingestion route took."""
    return {'level': 'low', 'kind': 'note', 'scope': 'object', 'obs_flag': None, 'message': message,
            'detected_at': today, 'detected_by': detected_by, 'topic': topic}


def today_iso() -> str:
    return _dt.date.today().isoformat()
