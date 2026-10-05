"""Overlap indexes: donor digests with demographics, hashed cell barcodes, gene universes.

``bin/ingest/ant-ingest-index`` writes them from the existing datasets;
``ant ingest-check`` reads them to see whether a submission re-uses donors
or cells that are already in the store. The files hold quasi-identifiers
(demographics per donor digest), never donor identifiers: donor keys appear
only as ``key_digest`` (``donor_keys.key_digest``), barcodes only as 64-bit
hashes. They live under ``config.get_ingest_index_dir()``, never in the
store or the repo.

Layout::

    <index_dir>/state.json            db_key -> signature of the source files
    <index_dir>/donors/<key>.parquet  one row per donor
    <index_dir>/cells/<key>.{strict,loose}.npy  sorted unique uint64 hashes
    <index_dir>/genes/<key>.parquet   gene names of the dataset
    <index_dir>/donors.parquet        all donors, rebuilt after each build
    <index_dir>/genes.parquet         organism, gene, n_datasets
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import numpy as np
import pandas as pd

from donor_keys import KIND_FALLBACK, donor_key_map, key_digest

DONOR_COLUMNS = ['db_key', 'group_id', 'key_digest', 'key_kind', 'sex', 'age', 'age_censored', 'apoe',
                 'braak', 'pmi', 'n_cells']

#: Longest run of nucleotides (a 10x style barcode) inside a cell name.
_BARCODE_CORE = re.compile(r'[ACGTN]{12,}')
_TRAILING_SUFFIX = re.compile(r'([-.]\d+)+$')
_APOE_ALLELES = re.compile(r'[234]')


def safe_key(db_key: str) -> str:
    """File-name form of a ``group/id`` key."""
    return re.sub(r'[^A-Za-z0-9._-]+', '__', db_key)


def normalize_barcode(cell_name: Any) -> str:
    """Barcode without sample prefix, suffix or ``-1`` style lane number.

    A cell name that contains a nucleotide barcode (12 or more ACGTN) is
    reduced to the first such run, upper case. Other names (sci-RNA, plate
    based) keep their text minus trailing ``-1`` / ``.2`` style lane numbers.
    """
    text = str(cell_name).strip().upper()
    match = _BARCODE_CORE.search(text)
    if match:
        return match.group(0)
    return _TRAILING_SUFFIX.sub('', text)


def _hash64(text: str) -> int:
    """64-bit hash of a string (BLAKE2b, 8 bytes)."""
    return int.from_bytes(hashlib.blake2b(text.encode('utf-8'), digest_size=8).digest(), 'little')


def cell_hashes(cell_names: Iterable[Any], donor_digests: Iterable[str]) -> Tuple[np.ndarray, np.ndarray]:
    """Sorted unique ``(strict, loose)`` hashes of a dataset's cells.

    Strict: normalised barcode plus the cell's donor digest (empty when the
    cell has no donor). Loose: the normalised barcode alone, which matches
    the same cells even when the donor keys differ between datasets.
    """
    strict: List[int] = []
    loose: List[int] = []
    for name, digest in zip(cell_names, donor_digests):
        barcode = normalize_barcode(name)
        strict.append(_hash64(f'{barcode}|{digest or ""}'))
        loose.append(_hash64(barcode))
    return np.unique(np.array(strict, dtype=np.uint64)), np.unique(np.array(loose, dtype=np.uint64))


# ── donor demographics ───────────────────────────────────────────────────────

def _sex_code(value: Any) -> Optional[str]:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    text = str(value).strip().lower()
    if text.startswith('f'):
        return 'f'
    if text.startswith('m'):
        return 'm'
    return None


def _age_value(value: Any) -> Tuple[Optional[float], bool]:
    """``(years, censored)``; ``90+`` and ``89+`` style labels are censored."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None, False
    text = str(value).strip()
    censored = text.endswith('+')
    try:
        return float(text.rstrip('+').strip()), censored
    except ValueError:
        return None, False


def _apoe_code(value: Any) -> Optional[str]:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    alleles = _APOE_ALLELES.findall(str(value))
    if len(alleles) != 2:
        return None
    return '/'.join(sorted(alleles))


def _number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(number) else number


def _single(series: pd.Series) -> Any:
    """The donor's value when all non-NULL cells agree, else None."""
    unique = series.dropna().unique()
    return unique[0] if len(unique) == 1 else None


def donor_frame(cells: pd.DataFrame, group_id: Optional[str], db_key: str) -> pd.DataFrame:
    """One row per donor from the per-cell published columns.

    Args:
        cells: Columns ``ant_donor`` and optionally ``ant_donor_uid``,
            ``ant_sex``, ``ant_age``, ``ant_apoe``, ``ant_braak``, ``ant_pmi``.
        group_id: Group id for the fallback donor key.
        db_key: ``<group_id>/<id>`` of the dataset.

    Returns:
        Frame with ``DONOR_COLUMNS``; empty when the dataset has no donors.
        A donor attribute that conflicts between cells is NULL.
    """
    if 'ant_donor' not in cells or cells['ant_donor'].dropna().empty:
        return pd.DataFrame(columns=DONOR_COLUMNS)
    uid_column = cells['ant_donor_uid'] if 'ant_donor_uid' in cells else [None] * len(cells)
    key_of, _stats = donor_key_map(zip(cells['ant_donor'], uid_column), group_id)
    rows: List[Dict[str, Any]] = []
    for label, group in cells.dropna(subset=['ant_donor']).groupby(cells['ant_donor'].astype(str), sort=True):
        key = key_of.get(str(label))
        if key is None:
            continue
        age, censored = _age_value(_single(group['ant_age']) if 'ant_age' in group else None)
        rows.append({
            'db_key': db_key, 'group_id': group_id or '', 'key_digest': key_digest(key),
            'key_kind': ('uid' if 'ant_donor_uid' in group and group['ant_donor_uid'].dropna().nunique() == 1
                         else KIND_FALLBACK),
            'sex': _single(group['ant_sex'].map(_sex_code)) if 'ant_sex' in group else None,
            'age': age, 'age_censored': censored,
            'apoe': _single(group['ant_apoe'].map(_apoe_code)) if 'ant_apoe' in group else None,
            'braak': _number(_single(group['ant_braak']) if 'ant_braak' in group else None),
            'pmi': _number(_single(group['ant_pmi']) if 'ant_pmi' in group else None),
            'n_cells': int(len(group)),
        })
    return pd.DataFrame(rows, columns=DONOR_COLUMNS)


def cell_donor_digests(cells: pd.DataFrame, group_id: Optional[str]) -> List[str]:
    """Donor key digest of every cell row (empty string without a donor)."""
    if 'ant_donor' not in cells:
        return [''] * len(cells)
    uid_column = cells['ant_donor_uid'] if 'ant_donor_uid' in cells else [None] * len(cells)
    key_of, _stats = donor_key_map(zip(cells['ant_donor'], uid_column), group_id)
    digest_of = {label: key_digest(key) for label, key in key_of.items()}
    return [digest_of.get(str(label), '') if pd.notna(label) else '' for label in cells['ant_donor']]


#: Published columns the index reads.
INDEX_COLUMNS = ['ant_donor', 'ant_donor_uid', 'ant_sex', 'ant_age', 'ant_apoe', 'ant_braak', 'ant_pmi']


@dataclass
class DatasetDigests:
    """Everything the overlap checks need to know about one dataset."""

    db_key: str
    group_id: str
    donors: pd.DataFrame
    strict: np.ndarray
    loose: np.ndarray
    genes: List[str]
    n_cells: int


def digests_from_view(view: Any, db_key: str, group_id: Optional[str]) -> DatasetDigests:
    """Donor, cell and gene digests of a dataset behind a ``DatasetView``."""
    cells = view.cell_frame(INDEX_COLUMNS)
    names = view.cell_names()
    digests = cell_donor_digests(cells, group_id)
    strict, loose = cell_hashes(names, digests)
    genes = [row[0] for row in view.conn.execute('SELECT gene_name FROM __genes ORDER BY gene_id').fetchall()]
    return DatasetDigests(db_key, group_id or '', donor_frame(cells, group_id, db_key), strict, loose, genes,
                          len(names))


# ── the on-disk index ────────────────────────────────────────────────────────

class OverlapIndex:
    """Reader and writer of the index directory."""

    def __init__(self, index_dir: Path) -> None:
        self.dir = Path(index_dir)
        self._donors: Optional[pd.DataFrame] = None

    # state
    def state(self) -> Dict[str, Dict[str, Any]]:
        """``db_key -> signature`` of the datasets in the index."""
        path = self.dir / 'state.json'
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    def is_empty(self) -> bool:
        """True when nothing has been indexed."""
        return not self.state()

    # reading
    def donors(self) -> pd.DataFrame:
        """All indexed donors (``DONOR_COLUMNS``); empty when there is no index."""
        if self._donors is None:
            path = self.dir / 'donors.parquet'
            self._donors = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=DONOR_COLUMNS)
        return self._donors

    def cells(self, db_key: str) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        """Sorted ``(strict, loose)`` hash arrays of one dataset, or None."""
        base = self.dir / 'cells' / safe_key(db_key)
        strict_path, loose_path = Path(f'{base}.strict.npy'), Path(f'{base}.loose.npy')
        if not strict_path.exists() or not loose_path.exists():
            return None
        return np.load(strict_path), np.load(loose_path)

    def gene_universe(self, organism: str) -> Tuple[set, int]:
        """``(gene names, n datasets)`` of an organism; empty when unknown."""
        path = self.dir / 'genes.parquet'
        if not path.exists():
            return set(), 0
        frame = pd.read_parquet(path)
        subset = frame[frame['organism'] == organism.strip().lower()]
        if subset.empty:
            return set(), 0
        return set(subset['gene'].astype(str)), int(subset['n_datasets'].max())

    # writing
    def write_dataset(self, digests: DatasetDigests, signature: Mapping[str, Any], organism: str) -> None:
        """Store one dataset's digests (replacing an older entry) and its state."""
        for sub in ('donors', 'cells', 'genes'):
            (self.dir / sub).mkdir(parents=True, exist_ok=True, mode=0o700)
        name = safe_key(digests.db_key)
        digests.donors.to_parquet(self.dir / 'donors' / f'{name}.parquet', index=False)
        np.save(self.dir / 'cells' / f'{name}.strict.npy', digests.strict)
        np.save(self.dir / 'cells' / f'{name}.loose.npy', digests.loose)
        pd.DataFrame({'gene': digests.genes, 'organism': organism.strip().lower()}).to_parquet(
            self.dir / 'genes' / f'{name}.parquet', index=False)
        state = self.state()
        state[digests.db_key] = dict(signature, organism=organism.strip().lower(), n_cells=digests.n_cells,
                                     n_donors=int(len(digests.donors)))
        self._write_state(state)

    def drop_dataset(self, db_key: str) -> None:
        """Remove a dataset that is no longer in the store."""
        name = safe_key(db_key)
        for sub in ('donors', 'genes'):
            (self.dir / sub / f'{name}.parquet').unlink(missing_ok=True)
        for kind in ('strict', 'loose'):
            (self.dir / 'cells' / f'{name}.{kind}.npy').unlink(missing_ok=True)
        state = self.state()
        state.pop(db_key, None)
        self._write_state(state)

    def _write_state(self, state: Mapping[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        temporary = self.dir / 'state.json.tmp'
        temporary.write_text(json.dumps(state, indent=1, sort_keys=True))
        temporary.replace(self.dir / 'state.json')

    def rebuild_combined(self) -> None:
        """Rewrite ``donors.parquet`` and ``genes.parquet`` from the per-dataset files."""
        donor_files = sorted((self.dir / 'donors').glob('*.parquet'))
        donor_parts = [pd.read_parquet(path) for path in donor_files]
        donors = (pd.concat([part for part in donor_parts if len(part)], ignore_index=True)
                  if any(len(part) for part in donor_parts) else pd.DataFrame(columns=DONOR_COLUMNS))
        donors.to_parquet(self.dir / 'donors.parquet', index=False)
        gene_parts = [pd.read_parquet(path) for path in sorted((self.dir / 'genes').glob('*.parquet'))]
        if gene_parts:
            genes = pd.concat(gene_parts, ignore_index=True)
            counted = genes.groupby(['organism', 'gene']).size().rename('n_datasets').reset_index()
        else:
            counted = pd.DataFrame(columns=['organism', 'gene', 'n_datasets'])
        counted.to_parquet(self.dir / 'genes.parquet', index=False)
        self.dir.chmod(0o700)
        for path in list(self.dir.rglob('*.parquet')) + list(self.dir.rglob('*.npy')):
            path.chmod(0o600)
        self._donors = None


__all__ = ['DONOR_COLUMNS', 'INDEX_COLUMNS', 'DatasetDigests', 'OverlapIndex', 'cell_donor_digests', 'cell_hashes',
           'digests_from_view', 'donor_frame', 'normalize_barcode', 'safe_key', 'KIND_FALLBACK']
