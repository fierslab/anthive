"""Row sampling from an h5ad without loading the matrix.

``read_rows`` reads a few cells of ``X``, a layer or ``raw/X`` straight from
the HDF5 file (CSR, CSC or dense) so the layer checks cost a few hundred MB
of IO even for very large datasets.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import numpy as np


def matrix_paths(h5ad_path: Path) -> Dict[str, str]:
    """Display name -> HDF5 path of the expression matrices (``X``, layers, ``raw/X``)."""
    import h5py  # noqa: PLC0415

    paths: Dict[str, str] = {}
    with h5py.File(h5ad_path, 'r') as handle:
        if 'X' in handle:
            paths['X'] = 'X'
        for name in (handle['layers'] if 'layers' in handle else []):
            paths[name] = f'layers/{name}'
        if 'raw' in handle and 'X' in handle['raw']:
            paths['raw.X'] = 'raw/X'
    return paths


def matrix_shape(h5ad_path: Path, hdf5_path: str) -> Tuple[int, int]:
    """``(n_obs, n_vars)`` of a stored matrix."""
    import h5py  # noqa: PLC0415

    with h5py.File(h5ad_path, 'r') as handle:
        node = handle[hdf5_path]
        shape = node.attrs['shape'] if 'shape' in node.attrs else node.shape
        return int(shape[0]), int(shape[1])


def sample_indices(n_obs: int, n_sample: int, seed: int) -> np.ndarray:
    """Sorted random row indices (all rows when the dataset is smaller)."""
    if n_obs <= n_sample:
        return np.arange(n_obs)
    return np.sort(np.random.default_rng(seed).choice(n_obs, size=n_sample, replace=False))


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

    rows = np.asarray(rows, dtype=np.int64)
    with h5py.File(h5ad_path, 'r') as handle:
        node = handle[hdf5_path]
        if isinstance(node, h5py.Dataset):
            return np.asarray(node[rows.tolist(), :], dtype=np.float64)
        encoding = node.attrs.get('encoding-type', node.attrs.get('h5sparse_format', ''))
        encoding = encoding.decode() if isinstance(encoding, bytes) else str(encoding)
        n_obs, n_vars = (int(v) for v in node.attrs['shape'])
        indptr = node['indptr'][:]
        out = np.zeros((len(rows), n_vars), dtype=np.float64)
        if encoding.startswith('csr'):
            for position, row in enumerate(rows):
                start, stop = int(indptr[row]), int(indptr[row + 1])
                out[position, node['indices'][start:stop]] = node['data'][start:stop]
            return out
        if encoding.startswith('csc'):
            position_of = {int(row): position for position, row in enumerate(rows)}
            wanted = np.zeros(n_obs, dtype=bool)
            wanted[rows] = True
            chunk = 500
            for first in range(0, n_vars, chunk):
                last = min(first + chunk, n_vars)
                start, stop = int(indptr[first]), int(indptr[last])
                indices = node['indices'][start:stop]
                data = node['data'][start:stop]
                counts = np.diff(indptr[first:last + 1])
                columns = np.repeat(np.arange(first, last), counts)
                keep = wanted[indices]
                for row, column, value in zip(indices[keep], columns[keep], data[keep]):
                    out[position_of[int(row)], column] = value
            return out
        raise ValueError(f'unsupported matrix encoding {encoding!r} at {hdf5_path}')


def counts_candidates(names: List[str]) -> List[str]:
    """Matrix names that are usually raw counts, in preference order."""
    preferred = [name for name in names if name.lower() in {'counts', 'umis', 'umi', 'raw_counts', 'raw'}]
    return preferred + [name for name in names if name not in preferred and name != 'X']
