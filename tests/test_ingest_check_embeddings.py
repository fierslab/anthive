"""ant ingest-check h5ad.embeddings: zero-padded, non-finite and collapsed obsm embeddings.

F26/PFC (2026-10-21): the deposited X_pca was all zeros for 215,210 of 304,723 cells (three of five cohorts),
probably a PCA on a partial concatenation padded with zeros, and nothing caught it. Rule of vark's local check
(vark 487d259; numbers in lib/ingest_check/thresholds.py): a cell has no coordinate when its row is all zero or
holds NaN or inf; an embedding named X_* fails above 100 such cells or 1% of the cells and warns below; other obsm
names only warn; a dimension whose variance is below 1e-6 of the largest dimension's is a warning.
"""
from __future__ import annotations

import h5py
import numpy as np
import pytest

from ingest_check import FAIL, PASS, WARN, checks_files, thresholds
from tests.ingest_helpers import Submission


@pytest.fixture
def submission(tmp_path):
    return Submission(tmp_path)


def _with_obsm(submission, **matrices):
    submission.build()
    path = submission.group_dir / 'dataset.h5ad'
    with h5py.File(path, 'a') as handle:
        for name, matrix in matrices.items():
            if name in handle['obsm']:
                del handle['obsm'][name]
            dataset = handle['obsm'].create_dataset(name, data=matrix)
            dataset.attrs['encoding-type'] = 'array'
            dataset.attrs['encoding-version'] = '0.2.0'
    with submission.context() as ctx:
        return checks_files.h5ad_embeddings(ctx)


def _pca(n_cells=60, n_dims=5, seed=0):
    return np.random.default_rng(seed).normal(size=(n_cells, n_dims))


def test_clean_embeddings_pass(submission):
    outcome = _with_obsm(submission, X_pca=_pca())
    assert outcome.status == PASS and 'X_pca' in outcome.summary and 'X_umap' in outcome.summary


def test_zero_padded_rows_above_one_percent_fail_and_are_counted(submission):
    pca = _pca()
    pca[20:] = 0                                   # 40 of 60 cells, as a PCA computed on part of the data
    outcome = _with_obsm(submission, X_pca=pca)
    assert outcome.status == FAIL
    assert 'X_pca: 40 of 60 cells (66.7%) have no coordinate (40 all zero)' in outcome.summary
    entry = next(item for item in outcome.details['embeddings'] if item['name'] == 'X_pca')
    assert entry['zero_rows'] == 40 and entry['n_cells'] == 60


def test_few_zero_rows_warn(submission, monkeypatch):
    monkeypatch.setattr(thresholds, 'EMBEDDING_NO_COORD_FAIL_FRACTION', 0.05)
    pca = _pca()
    pca[0] = 0                                     # 1 of 60 = 1.7%: below 5% and below 100 cells
    outcome = _with_obsm(submission, X_pca=pca)
    assert outcome.status == WARN and 'X_pca: 1 of 60 cells (1.7%) have no coordinate' in outcome.summary


def test_more_than_100_zero_rows_fail_even_below_one_percent(submission, monkeypatch):
    monkeypatch.setattr(thresholds, 'EMBEDDING_NO_COORD_FAIL_CELLS', 2)
    monkeypatch.setattr(thresholds, 'EMBEDDING_NO_COORD_FAIL_FRACTION', 0.5)
    pca = _pca()
    pca[:3] = 0
    assert _with_obsm(submission, X_pca=pca).status == FAIL


def test_rows_with_nan_or_inf_have_no_coordinate(submission):
    pca = _pca()
    pca[3, 1] = np.nan
    pca[4, 2] = np.inf
    pca[5] = 0
    outcome = _with_obsm(submission, X_pca=pca)
    assert outcome.status == FAIL                  # 3 of 60 = 5% > 1%
    assert 'X_pca: 3 of 60 cells (5.0%) have no coordinate (1 all zero, 2 with NaN or inf)' in outcome.summary


def test_embeddings_not_named_x_only_warn(submission):
    scvi = _pca()
    scvi[10:] = 0
    outcome = _with_obsm(submission, scvi_10_2022=scvi)
    assert outcome.status == WARN and 'scvi_10_2022: 50 of 60 cells' in outcome.summary


def test_dataframe_style_obsm_entries_are_skipped(submission):
    submission.build()
    path = submission.group_dir / 'dataset.h5ad'
    with h5py.File(path, 'a') as handle:
        group = handle['obsm'].create_group('spatial_table')
        group.attrs['encoding-type'] = 'dataframe'
    entry = next(item for item in checks_files.embedding_stats(path) if item['name'] == 'spatial_table')
    assert entry == {'name': 'spatial_table', 'skipped': 'dataframe'}


def test_collapsed_dimensions_warn(submission):
    pca = _pca()
    pca[:, 3] = 1.0                                # constant: no variance
    pca[:, 4] = 1e-5 * pca[:, 0]                   # variance 1e-10 of the largest
    outcome = _with_obsm(submission, X_pca=pca)
    assert outcome.status == WARN and 'X_pca: 2 of 5 dimensions have (almost) no variance (dimension 4, 5)' in outcome.summary


def test_rows_are_read_in_blocks(submission, monkeypatch):
    monkeypatch.setattr(thresholds, 'EMBEDDING_READ_ROWS', 7)
    pca = _pca()
    pca[50:] = 0
    entry = next(item for item in checks_files.embedding_stats(_path(submission, pca)) if item['name'] == 'X_pca')
    assert entry['zero_rows'] == 10 and entry['collapsed_dims'] == []


def _path(submission, pca):
    _with_obsm(submission, X_pca=pca)
    return submission.group_dir / 'dataset.h5ad'


REFERENCE = '/data/1/project/trajectories/mark/knn_manual_perseids_hvg2000_nl2_nlat20_280926.h5ad'


@pytest.mark.skipif(not __import__('os').path.exists(REFERENCE), reason='the 36 GB reference file is on the dev box only')
def test_reference_file_matches_vark():
    # vark 487d259 on the same file: X_pca 215,210 and X_scVI_HVG 298,690 of 304,723 cells without a coordinate
    # (both fail), scvi_10_2022 298,690 (warn: not an X_* name); obsm only, read-only
    stats = {item['name']: item for item in checks_files.embedding_stats(REFERENCE) if 'skipped' not in item}
    assert stats['X_pca']['no_coordinate'] == 215_210 and stats['X_pca']['n_cells'] == 304_723
    assert stats['X_scVI_HVG']['no_coordinate'] == 298_690
    assert stats['scvi_10_2022']['no_coordinate'] == 298_690
    assert stats['X_umap']['no_coordinate'] == 0 and stats['X_scVI']['no_coordinate'] == 0
