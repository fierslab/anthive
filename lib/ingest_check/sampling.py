"""Row sampling from an h5ad without loading the matrix.

``read_rows`` reads a few cells of ``X``, a layer or ``raw/X`` straight from
the HDF5 file (CSR, CSC or dense) so the layer checks cost a few hundred MB
of IO even for very large datasets.

The cells are the same as in vark's local inspector
(``internal/deposit/vark_h5ad_inspect.py`` in the vark repo), so local and
server verdicts agree:

* ``sample_indices``: ``default_rng(seed).choice(n_obs, n_sample)``, sorted
  (all cells when there are fewer);
* ``choose_rows``: those cells, unless they would decompress more than
  ``thresholds.SAMPLE_INFLATE_BUDGET`` bytes of one compressed chunked matrix
  (X or the first counts candidate). A dense matrix stored in gzip chunks of
  ~1,000 cells has to decompress every chunk a sampled cell touches, and
  2,000 random cells touch nearly all of them: the whole matrix
  (Sierksma2026 Single.Grafts, 33 GB of int32 counts). Then
  ``thresholds.SAMPLE_RUNS`` runs of consecutive cells at random places are
  read instead (mode ``sampled_runs``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np

ROW_BLOCK = 125              # sampled rows read per block (dense float64)
CSC_COLUMN_BLOCK = 500       # CSC: columns scanned per block
RUN_GAP = 262_144            # CSR: neighbouring rows closer than this many values are read in one slice
RUN_MAX = 8_000_000          # CSR: at most this many values per slice
COUNTS_NAMES = ('counts', 'umis', 'umi', 'raw_counts', 'raw')

MODE_SAMPLED = 'sampled'
MODE_RUNS = 'sampled_runs'
MODE_ALL = 'matrix_exhausted'


def _text(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def matrix_paths(h5ad_path: Path) -> Dict[str, str]:
    """Display name -> HDF5 path of the expression matrices (``X``, layers, ``raw/X``)."""
    import h5py  # noqa: PLC0415

    paths: Dict[str, str] = {}
    with h5py.File(h5ad_path, 'r') as handle:
        if 'X' in handle:
            paths['X'] = 'X'
        if 'layers' in handle and isinstance(handle['layers'], h5py.Group):
            for name in handle['layers']:
                paths[name] = f'layers/{name}'
        if 'raw' in handle and isinstance(handle['raw'], h5py.Group) and 'X' in handle['raw']:
            paths['raw.X'] = 'raw/X'
    return paths


def matrix_shape(h5ad_path: Path, hdf5_path: str) -> Tuple[int, int]:
    """``(n_obs, n_vars)`` of a stored matrix."""
    import h5py  # noqa: PLC0415

    with h5py.File(h5ad_path, 'r') as handle:
        return StoredMatrix(handle, hdf5_path).shape


def sample_indices(n_obs: int, n_sample: int, seed: int) -> np.ndarray:
    """Sorted random row indices (all rows when the dataset is smaller)."""
    if n_obs <= n_sample:
        return np.arange(n_obs)
    return np.sort(np.random.default_rng(seed).choice(n_obs, size=n_sample, replace=False))


def run_indices(n_obs: int, n_sample: int, seed: int, runs: int) -> np.ndarray:
    """``runs`` runs of ``n_sample // runs`` consecutive rows at random places (vark's ``run_indices``)."""
    length = max(1, n_sample // runs)
    if n_obs <= n_sample:
        return np.arange(n_obs)
    segments = np.sort(np.random.default_rng(seed).choice(n_obs // length, size=runs, replace=False))
    return (segments[:, None] * length + np.arange(length)[None, :]).ravel()


class StoredMatrix:
    """An expression matrix in an open h5py file (``X``, ``layers/<name>`` or ``raw/X``), read by rows.

    Raises:
        ValueError: the stored encoding is neither CSR, CSC nor dense.
    """

    def __init__(self, handle, hdf5_path: str) -> None:
        import h5py  # noqa: PLC0415

        self.path = hdf5_path
        self.node = handle[hdf5_path]
        self._indptr: Optional[np.ndarray] = None
        if isinstance(self.node, h5py.Dataset):
            self.encoding = 'dense'
            self.shape = (int(self.node.shape[0]), int(self.node.shape[1]))
            return
        encoding = _text(self.node.attrs.get('encoding-type', self.node.attrs.get('h5sparse_format', '')))
        if encoding.startswith('csr'):
            self.encoding = 'csr'
        elif encoding.startswith('csc'):
            self.encoding = 'csc'
        else:
            raise ValueError(f'unsupported matrix encoding {encoding!r} at {hdf5_path}')
        shape = self.node.attrs.get('shape', self.node.attrs.get('h5sparse_shape'))
        self.shape = (int(shape[0]), int(shape[1]))
        self._indices = self.node['indices']
        self._data = self.node['data']

    def indptr(self) -> np.ndarray:
        if self._indptr is None:
            self._indptr = self.node['indptr'][:]
        return self._indptr

    def inflate_cost(self, rows: np.ndarray) -> int:
        """Bytes of compressed chunks to decompress for these rows (0 when not compressed, or CSC)."""
        rows = np.asarray(rows, dtype=np.int64)
        if self.encoding == 'csc' or len(rows) == 0:
            return 0
        if self.encoding == 'dense':
            node = self.node
            if node.chunks is None or node.compression is None:
                return 0
            bands = len(np.unique(rows // node.chunks[0]))
            per_band = int(np.prod(node.chunks)) * node.dtype.itemsize * -(-node.shape[1] // node.chunks[1])
            return bands * per_band
        indptr = self.indptr()
        starts, stops = indptr[rows].astype(np.int64), indptr[rows + 1].astype(np.int64)
        nonempty = stops > starts
        total = 0
        for dataset in (self._indices, self._data):
            if dataset.chunks is None or dataset.compression is None or not nonempty.any():
                continue
            size = dataset.chunks[0]
            first, last = starts[nonempty] // size, (stops[nonempty] - 1) // size
            touched = len(np.unique(np.concatenate([first, last]))) + int(np.maximum(last - first - 1, 0).sum())
            total += touched * size * dataset.dtype.itemsize
        return total

    def read(self, rows: Sequence[int]) -> np.ndarray:
        """Dense ``(len(rows), n_vars)`` float64 array of the given sorted rows."""
        rows = np.asarray(rows, dtype=np.int64)
        n_vars = self.shape[1]
        if len(rows) == 0:
            return np.zeros((0, n_vars), dtype=np.float64)
        if self.encoding == 'csc':
            return self._read_csc(rows)
        return np.concatenate(list(self._row_blocks(rows)), axis=0)

    def _row_blocks(self, rows: np.ndarray) -> Iterator[np.ndarray]:
        n_vars = self.shape[1]
        for first in range(0, len(rows), ROW_BLOCK):
            chunk = rows[first:first + ROW_BLOCK]
            if self.encoding == 'dense':
                if chunk[-1] - chunk[0] + 1 == len(chunk):  # consecutive: one hyperslab, each chunk inflated once
                    yield np.asarray(self.node[int(chunk[0]):int(chunk[-1]) + 1, :], dtype=np.float64)
                else:
                    yield np.asarray(self.node[chunk.tolist(), :], dtype=np.float64)
                continue
            out = np.zeros((len(chunk), n_vars), dtype=np.float64)
            for run_start, run_stop, members in self._csr_runs(chunk):
                indices = self._indices[run_start:run_stop]
                data = self._data[run_start:run_stop]
                for position, start, stop in members:
                    if stop > start:
                        out[position, indices[start - run_start:stop - run_start]] = \
                            data[start - run_start:stop - run_start]
            yield out

    def _csr_runs(self, chunk: np.ndarray) -> list:
        """Group the CSR spans of sorted rows into runs read with one slice each."""
        indptr = self.indptr()
        runs: list = []
        for position, row in enumerate(chunk):
            start, stop = int(indptr[row]), int(indptr[row + 1])
            if runs and start - runs[-1][1] <= RUN_GAP and stop - runs[-1][0] <= RUN_MAX:
                runs[-1][1] = stop
                runs[-1][2].append((position, start, stop))
            else:
                runs.append([start, stop, [(position, start, stop)]])
        return runs

    def _read_csc(self, rows: np.ndarray) -> np.ndarray:
        """CSC has no row access: one pass over the column blocks collects the sampled rows."""
        n_obs, n_vars = self.shape
        indptr = self.indptr()
        position_of = np.full(n_obs, -1, dtype=np.int64)
        position_of[rows] = np.arange(len(rows))
        out = np.zeros((len(rows), n_vars), dtype=np.float64)
        for first in range(0, n_vars, CSC_COLUMN_BLOCK):
            last = min(first + CSC_COLUMN_BLOCK, n_vars)
            start, stop = int(indptr[first]), int(indptr[last])
            if stop <= start:
                continue
            indices = self._indices[start:stop]
            data = self._data[start:stop]
            columns = np.repeat(np.arange(first, last), np.diff(indptr[first:last + 1]))
            positions = position_of[indices]
            keep = positions >= 0
            out[positions[keep], columns[keep]] = data[keep]
        return out


def read_rows(h5ad_path: Path, hdf5_path: str, rows: Sequence[int]) -> np.ndarray:
    """Dense ``(len(rows), n_vars)`` float64 array of the given sorted rows.

    Args:
        h5ad_path: The h5ad.
        hdf5_path: ``X``, ``layers/<name>`` or ``raw/X``.
        rows: Sorted row indices.

    Raises:
        ValueError: the stored encoding is neither CSR, CSC nor dense.
    """
    import h5py  # noqa: PLC0415

    with h5py.File(h5ad_path, 'r') as handle:
        return StoredMatrix(handle, hdf5_path).read(rows)


def counts_candidates(names: List[str]) -> List[str]:
    """Matrix names that are usually raw counts, in preference order."""
    preferred = [name for name in names if name.lower() in COUNTS_NAMES]
    return preferred + [name for name in names if name not in preferred and name != 'X']


def choose_rows(h5ad_path: Path, n_sample: int, seed: int, budget: int, runs: int
                ) -> Tuple[np.ndarray, str, int, int]:
    """The rows every matrix is sampled at: ``(rows, mode, inflate_bytes, n_obs)``.

    The random cells of ``sample_indices`` unless they would decompress more
    than ``budget`` bytes of X or of the first counts candidate (vark judges
    the same two matrices); then ``runs`` runs of consecutive cells. ``mode``
    is ``sampled``, ``sampled_runs`` or ``matrix_exhausted`` (all cells), as
    vark's ``exit_reason``. ``inflate_bytes`` is the cost of the random cells.
    """
    import h5py  # noqa: PLC0415

    paths = matrix_paths(h5ad_path)
    with h5py.File(h5ad_path, 'r') as handle:
        judged = []
        if 'X' in paths:
            judged.append(StoredMatrix(handle, paths['X']))
        n_obs = judged[0].shape[0] if judged else None
        for name in counts_candidates([name for name in paths if name != 'X']):
            candidate = StoredMatrix(handle, paths[name])
            if n_obs is None:
                n_obs = candidate.shape[0]
            if candidate.shape[0] == n_obs:
                judged.append(candidate)
                break
        if n_obs is None:
            return np.arange(0), MODE_ALL, 0, 0
        rows = sample_indices(n_obs, n_sample, seed)
        if n_obs <= n_sample:
            return rows, MODE_ALL, 0, n_obs
        cost = max([matrix.inflate_cost(rows) for matrix in judged] or [0])
    if cost <= budget:
        return rows, MODE_SAMPLED, cost, n_obs
    return run_indices(n_obs, n_sample, seed, runs), MODE_RUNS, cost, n_obs
