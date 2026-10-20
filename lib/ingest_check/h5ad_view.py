"""What the checks need of an h5ad, read with h5py: obs, var and key lists.

``anndata.read_h5ad(backed='r')`` keeps only X on disk; layers, raw, obsm,
obsp and uns are read into memory in full when the file opens. A 36 GB
deposit with 22 GB of obsp kNN graphs cost about 30 GB of RAM in the watcher,
and a dense gzip counts layer of 33 GB (Sierksma2026 Single.Grafts) has to be
decompressed whole before the first check runs.

``H5adSummary`` decodes only obs and var (anndata's element reader) and lists
the keys of X, layers, raw, obsm, obsp, varm and uns. The expression
matrices are read by ``sampling`` (a few thousand cells). It offers the
subset of the AnnData interface the checks and ``h5ad_utils.validate_structure``
/ ``get_h5ad_info`` use: ``obs``, ``var``, ``obs_names``, ``var_names``,
``n_obs``, ``n_vars``, ``X`` (a ``StoredElement`` or None), ``layers``,
``obsm``, ``obsp``, ``varm``, ``uns`` (name -> ``StoredElement``), ``raw``
(``StoredElement`` or None). The file is closed when the object is built.

vark's local inspector (``internal/deposit/vark_h5ad_inspect.py``) reads the
file the same way.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

import pandas as pd


@dataclass(frozen=True)
class StoredElement:
    """An element left on disk: its HDF5 path and, for arrays and matrices, the shape."""

    path: str
    shape: Optional[Tuple[int, ...]] = None


def _read_elem(node):
    try:
        from anndata.io import read_elem  # noqa: PLC0415 - anndata >= 0.11
    except ImportError:  # pragma: no cover - older anndata
        from anndata.experimental import read_elem  # noqa: PLC0415
    return read_elem(node)


def _element(node, path: str) -> StoredElement:
    import h5py  # noqa: PLC0415

    if isinstance(node, h5py.Dataset):
        return StoredElement(path, tuple(int(v) for v in node.shape))
    shape = node.attrs.get('shape', node.attrs.get('h5sparse_shape'))
    return StoredElement(path, tuple(int(v) for v in shape) if shape is not None else None)


def _children(handle, key: str) -> Dict[str, StoredElement]:
    import h5py  # noqa: PLC0415

    if key not in handle or not isinstance(handle[key], h5py.Group):
        return {}
    return {str(name): _element(handle[key][name], f'{key}/{name}') for name in handle[key]}


class H5adSummary:
    """obs, var and the element keys of an h5ad (see the module docstring).

    Args:
        path: The h5ad.

    Raises:
        KeyError: obs or var is missing.
        OSError: not an HDF5 file.
    """

    def __init__(self, path: Path) -> None:
        import h5py  # noqa: PLC0415

        self.path = Path(path)
        with h5py.File(self.path, 'r') as handle:
            self.obs: pd.DataFrame = _read_elem(handle['obs'])
            self.var: pd.DataFrame = _read_elem(handle['var'])
            self.X: Optional[StoredElement] = _element(handle['X'], 'X') if 'X' in handle else None
            self.layers = _children(handle, 'layers')
            self.obsm = _children(handle, 'obsm')
            self.obsp = _children(handle, 'obsp')
            self.varm = _children(handle, 'varm')
            self.uns = _children(handle, 'uns')
            self.raw: Optional[StoredElement] = StoredElement('raw') if 'raw' in handle else None

    @property
    def obs_names(self) -> pd.Index:
        return self.obs.index

    @property
    def var_names(self) -> pd.Index:
        return self.var.index

    @property
    def n_obs(self) -> int:
        return len(self.obs.index)

    @property
    def n_vars(self) -> int:
        return len(self.var.index)
