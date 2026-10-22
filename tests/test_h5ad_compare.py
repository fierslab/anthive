"""The version class of a new h5ad against the published one (lib/h5ad_compare.py, design section 25)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import scipy.sparse as sp

import h5ad_compare


def _adata(n_cells=40, seed=0):
    import anndata
    rng = np.random.default_rng(seed)
    counts = sp.random(n_cells, 12, density=0.3, format='csr', random_state=seed,
                       data_rvs=lambda n: rng.integers(1, 20, n)).astype(np.float32)
    lognorm = counts.copy()
    lognorm.data = np.log1p(lognorm.data)
    obs = pd.DataFrame({'cell_type': pd.Categorical(rng.choice(['mg', 'ast'], n_cells)),
                        'n_counts': np.asarray(counts.sum(1)).ravel()},
                       index=[f'c{i}' for i in range(n_cells)])
    adata = anndata.AnnData(X=lognorm, obs=obs, var=pd.DataFrame(index=[f'G{i}' for i in range(12)]))
    adata.layers['counts'] = counts
    adata.obsm['X_umap'] = rng.normal(size=(n_cells, 2)).astype(np.float32)
    return adata


def _write(adata, path: Path) -> Path:
    adata.write_h5ad(path)
    return path


@pytest.fixture
def published(tmp_path):
    return _write(_adata(), tmp_path / 'old.h5ad')


def test_identical_files_are_minor_without_a_data_change(published, tmp_path):
    result = h5ad_compare.classify(published, _write(_adata(), tmp_path / 'new.h5ad'))
    assert result.part == 'minor' and result.added == [] and 'no data change' in result.reason


@pytest.mark.parametrize('change, added', [
    (lambda a: a.obs.__setitem__('batch', pd.Categorical(['b1'] * a.n_obs)), 'obs/batch'),
    (lambda a: a.obsm.__setitem__('X_pca', np.zeros((a.n_obs, 3), dtype=np.float32)), 'obsm/X_pca'),
    (lambda a: a.layers.__setitem__('spliced', a.layers['counts'].copy()), 'layers/spliced'),
    (lambda a: a.var.__setitem__('highly_variable', np.ones(a.n_vars, dtype=bool)), 'var/highly_variable'),
])
def test_additions_are_minor(published, tmp_path, change, added):
    adata = _adata()
    change(adata)
    result = h5ad_compare.classify(published, _write(adata, tmp_path / 'new.h5ad'))
    assert result.part == 'minor' and added in result.added, result.reason


def _x_value_changed(adata):
    adata.X = adata.X.copy()
    adata.X.data[0] += 1.0


def _obs_value_changed(adata):
    adata.obs['n_counts'] = adata.obs['n_counts'].to_numpy() + 1.0


def _cell_removed(adata):
    return adata[1:].copy()


def _cells_reordered(adata):
    return adata[list(range(adata.n_obs))[::-1]].copy()


def _gene_added(adata):
    import anndata
    extra = anndata.AnnData(X=sp.csr_matrix((adata.n_obs, 1), dtype=np.float32), obs=adata.obs[[]],
                            var=pd.DataFrame(index=['NEW']))
    extra.layers['counts'] = sp.csr_matrix((adata.n_obs, 1), dtype=np.float32)
    joined = anndata.concat([adata, extra], axis=1, merge='first')
    joined.obs = adata.obs
    joined.obsm = adata.obsm
    return joined


def _column_removed(adata):
    del adata.obs['n_counts']


def _layer_changed(adata):
    adata.layers['counts'] = adata.layers['counts'] * 2


@pytest.mark.parametrize('change, words', [
    (_x_value_changed, 'X/data'), (_obs_value_changed, 'obs/n_counts'), (_cell_removed, 'the cells differ'),
    (_cells_reordered, 'the cells differ'), (_gene_added, 'X'), (_column_removed, 'obs/n_counts was removed'),
    (_layer_changed, 'layers/counts'),
])
def test_alterations_are_major(published, tmp_path, change, words):
    adata = _adata()
    changed = change(adata)
    result = h5ad_compare.classify(published, _write(changed if changed is not None else adata,
                                                     tmp_path / 'new.h5ad'))
    assert result.part == 'major' and result.reason.startswith('data altered') and words in result.reason, \
        result.reason


def test_the_comparison_is_streamed_in_bounded_blocks(published, tmp_path, monkeypatch):
    monkeypatch.setattr(h5ad_compare, 'BLOCK_ELEMENTS', 7)          # many small blocks, same answer
    adata = _adata()
    _x_value_changed(adata)
    assert h5ad_compare.classify(published, published).part == 'minor'
    assert h5ad_compare.classify(published, _write(adata, tmp_path / 'new.h5ad')).part == 'major'


def test_a_missing_or_unreadable_old_file_is_major(published, tmp_path):
    assert h5ad_compare.classify_or_major(None, published)[0] == 'major'
    broken = tmp_path / 'broken.h5ad'
    broken.write_bytes(b'not hdf5')
    part, reason, _ = h5ad_compare.classify_or_major(broken, published)
    assert part == 'major' and 'could not be compared' in reason
