"""ant ingest-check: the h5ad is read with h5py (obs, var, key lists) and sampled without loading matrices.

Regression tests for two problems found on store files:

* ``anndata.read_h5ad(backed='r')`` read layers, raw, obsm, obsp and uns into
  memory in full (a 36 GB deposit with 22 GB of obsp graphs cost ~30 GB RAM);
* 2,000 random cells of a dense gzip matrix stored in chunks of ~1,000 cells
  decompressed nearly the whole matrix (Sierksma2026 Single.Grafts, minutes).

The sample must stay identical to vark's local inspector
(``internal/deposit/vark_h5ad_inspect.py``): ``default_rng(0).choice``, sorted,
and the same runs fallback.
"""
import anndata
import h5py
import numpy as np
import pandas as pd
import pytest
import scipy.sparse as sp

from ingest_check import PASS, checks_files, checks_layers, sampling, thresholds
from ingest_check.h5ad_view import H5adSummary
from tests.ingest_helpers import Submission, donor_obs


@pytest.fixture
def submission(tmp_path):
    return Submission(tmp_path)


def write_h5ad(path, n_obs=300, n_vars=30, layout='dense', compression='gzip', chunk_rows=64, extras=True):
    """An h5ad with counts and log1p(CP10k) X, chunked in bands of ``chunk_rows`` cells, plus obsm/obsp/uns."""
    rng = np.random.default_rng(1)
    counts = rng.poisson(1.5, size=(n_obs, n_vars)).astype(np.float32)
    counts[:, 0] += 5
    lognorm = np.log1p(counts / counts.sum(axis=1, keepdims=True) * 1e4).astype(np.float32)
    wrap = {'dense': np.asarray, 'csr': sp.csr_matrix, 'csc': sp.csc_matrix}[layout]
    adata = anndata.AnnData(X=wrap(lognorm), obs=donor_obs(n_obs), var=pd.DataFrame(index=[f'G{i}' for i in range(n_vars)]))
    adata.layers['counts'] = wrap(counts)
    if extras:
        adata.obsm['X_umap'] = rng.normal(size=(n_obs, 2))
        adata.obsp['connectivities'] = sp.random(n_obs, n_obs, density=0.05, format='csr', random_state=1)
        adata.uns['big'] = rng.normal(size=1000)
    adata.write_h5ad(path, **({'compression': compression} if compression else {}))
    if layout == 'dense' and compression:
        rechunk(path, (chunk_rows, max(1, n_vars // 3)))
    return path, counts, lognorm


def rechunk(path, chunks):
    """Rewrite the dense X and layers as gzip datasets with these chunks (anndata picks its own)."""
    with h5py.File(path, 'a') as handle:
        for name in ['X'] + [f'layers/{layer}' for layer in handle['layers']]:
            node = handle[name]
            if not isinstance(node, h5py.Dataset):
                continue
            data, attrs = node[()], dict(node.attrs)
            del handle[name]
            rewritten = handle.create_dataset(name, data=data, chunks=chunks, compression='gzip')
            rewritten.attrs.update(attrs)


# ── h5py summary instead of backed anndata ───────────────────────────────────


def test_summary_decodes_obs_and_var_like_anndata_and_lists_the_rest(tmp_path):
    path, _, _ = write_h5ad(tmp_path / 'a.h5ad')
    summary = H5adSummary(path)
    reference = anndata.read_h5ad(path)
    pd.testing.assert_frame_equal(summary.obs, reference.obs)
    pd.testing.assert_frame_equal(summary.var, reference.var, check_column_type=False)  # no var columns
    assert (summary.n_obs, summary.n_vars) == reference.shape
    assert list(summary.var_names) == list(reference.var_names)
    assert list(summary.layers) == ['counts'] and summary.layers['counts'].shape == reference.shape
    assert list(summary.obsm) == ['X_umap'] and list(summary.obsp) == ['connectivities']
    assert 'big' in summary.uns and summary.X.shape == reference.shape and summary.raw is None


def test_summary_never_reads_matrices_graphs_or_uns(tmp_path, monkeypatch):
    path, _, _ = write_h5ad(tmp_path / 'a.h5ad', layout='csr')
    read = []
    original = h5py.Dataset.__getitem__

    def recording(self, key):
        read.append(self.name)
        return original(self, key)

    monkeypatch.setattr(h5py.Dataset, '__getitem__', recording)
    H5adSummary(path)
    assert read, 'obs and var are decoded'
    assert all(name.startswith(('/obs/', '/var/')) for name in read), sorted(set(read))


def test_context_never_opens_the_h5ad_with_anndata(submission, monkeypatch):
    submission.build()

    def refuse(*args, **kwargs):
        raise AssertionError('anndata.read_h5ad loads layers, obsm, obsp and uns into memory')

    monkeypatch.setattr(anndata, 'read_h5ad', refuse)
    with submission.context() as ctx:
        assert checks_files.h5ad_structure(ctx).status != 'fail'
        assert checks_layers.layers_counts(ctx).status == PASS
        assert checks_layers.genes_ids(ctx).details['n_genes'] == 40
        assert ctx.adata.obsm['X_umap'].shape == (60, 2)


def test_structure_details_unchanged(submission):
    submission.build()
    with submission.context() as ctx:
        outcome = checks_files.h5ad_structure(ctx)
    assert outcome.details == {'n_obs': 60, 'n_vars': 40, 'layers': ['counts'], 'obsm_keys': ['X_umap'],
                               'n_obs_columns': len(submission.obs.columns)}


# ── reading rows ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize('layout', ['dense', 'csr', 'csc'])
@pytest.mark.parametrize('compression', ['gzip', None])
def test_read_rows_matches_the_matrix_for_random_and_consecutive_rows(tmp_path, layout, compression):
    path, counts, lognorm = write_h5ad(tmp_path / 'a.h5ad', layout=layout, compression=compression, extras=False)
    random_rows = sampling.sample_indices(300, 140, 0)
    consecutive = np.arange(100, 260)
    for rows in (random_rows, consecutive, np.arange(0)):
        np.testing.assert_array_equal(sampling.read_rows(path, 'layers/counts', rows), counts[rows])
        np.testing.assert_allclose(sampling.read_rows(path, 'X', rows), lognorm[rows].astype(np.float64))


def test_sample_indices_are_the_shared_vark_sample():
    rows = sampling.sample_indices(15599, 2000, 0)
    expected = np.sort(np.random.default_rng(0).choice(15599, size=2000, replace=False))
    np.testing.assert_array_equal(rows, expected)
    np.testing.assert_array_equal(sampling.sample_indices(50, 2000, 0), np.arange(50))


def test_run_indices_are_vark_runs():
    rows = sampling.run_indices(299368, 2000, 0, 16)
    segments = np.sort(np.random.default_rng(0).choice(299368 // 125, size=16, replace=False))
    np.testing.assert_array_equal(rows, (segments[:, None] * 125 + np.arange(125)[None, :]).ravel())
    assert len(rows) == 2000 and len(np.unique(rows)) == 2000 and np.all(np.diff(rows) > 0) and rows.max() < 299368


def test_inflate_cost_counts_the_chunks_a_sample_touches(tmp_path):
    path, _, _ = write_h5ad(tmp_path / 'a.h5ad', chunk_rows=64, extras=False)
    with h5py.File(path, 'r') as handle:
        matrix = sampling.StoredMatrix(handle, 'layers/counts')
        node = handle['layers/counts']
        per_band = int(np.prod(node.chunks)) * node.dtype.itemsize * -(-30 // node.chunks[1])
        assert matrix.inflate_cost(np.array([0, 1, 63])) == per_band
        assert matrix.inflate_cost(np.array([0, 64, 299])) == 3 * per_band
    plain, _, _ = write_h5ad(tmp_path / 'b.h5ad', compression=None, extras=False)
    with h5py.File(plain, 'r') as handle:
        assert sampling.StoredMatrix(handle, 'layers/counts').inflate_cost(np.arange(300)) == 0


def test_choose_rows_falls_back_to_runs_only_over_the_budget(tmp_path):
    path, _, _ = write_h5ad(tmp_path / 'a.h5ad', extras=False)
    rows, mode, cost, n_obs = sampling.choose_rows(path, 100, 0, budget=10 ** 12, runs=4)
    assert mode == 'sampled' and n_obs == 300 and cost > 0
    np.testing.assert_array_equal(rows, sampling.sample_indices(300, 100, 0))
    rows, mode, cost_again, _ = sampling.choose_rows(path, 100, 0, budget=cost - 1, runs=4)
    assert mode == 'sampled_runs' and cost_again == cost
    np.testing.assert_array_equal(rows, sampling.run_indices(300, 100, 0, 4))
    rows, mode, _, _ = sampling.choose_rows(path, 1000, 0, budget=1, runs=4)
    assert mode == 'matrix_exhausted' and len(rows) == 300


def test_choose_rows_judges_x_and_the_first_counts_candidate_only(tmp_path):
    # an uncompressed X and counts: a compressed third layer does not trigger runs (as vark)
    path, counts, _ = write_h5ad(tmp_path / 'a.h5ad', compression=None, extras=False)
    with h5py.File(path, 'a') as handle:
        handle['layers'].create_dataset('zzz', data=counts, chunks=(64, 10), compression='gzip')
        handle['layers/zzz'].attrs['encoding-type'] = 'array'
        handle['layers/zzz'].attrs['encoding-version'] = '0.2.0'
    _, mode, cost, _ = sampling.choose_rows(path, 100, 0, budget=1, runs=4)
    assert mode == 'sampled' and cost == 0


def test_counts_candidate_order_matches_vark():
    assert sampling.counts_candidates(['spliced', 'raw.X', 'UMI', 'counts']) == ['UMI', 'counts', 'spliced', 'raw.X']


# ── the fallback in the check output ─────────────────────────────────────────


def _gzip_submission(submission):
    submission.build(layout='dense')
    target = submission.group_dir / 'dataset.h5ad'
    rechunk(target, (16, 8))
    return submission


def test_runs_fallback_is_reported_and_keeps_the_verdicts(submission, monkeypatch):
    _gzip_submission(submission)
    monkeypatch.setattr(thresholds, 'SAMPLE_CELLS', 20)
    monkeypatch.setattr(thresholds, 'CONSISTENCY_CELLS', 20)
    monkeypatch.setattr(thresholds, 'SAMPLE_RUNS', 4)
    checks = (checks_layers.layers_counts, checks_layers.layers_x_lognorm, checks_layers.layers_consistency)
    with submission.context() as ctx:
        random_outcomes = [check(ctx) for check in checks]
    monkeypatch.setattr(thresholds, 'SAMPLE_INFLATE_BUDGET', 1)
    with submission.context() as ctx:
        run_outcomes = [check(ctx) for check in checks]
        rows, _, _, _ = ctx.sample_rows(20, 0)
    np.testing.assert_array_equal(rows, sampling.run_indices(60, 20, 0, 4))
    for random_outcome, run_outcome in zip(random_outcomes, run_outcomes):
        assert random_outcome.status == run_outcome.status == PASS
        assert random_outcome.details['sample_mode'] == 'sampled' and 'runs of' not in random_outcome.summary
        assert run_outcome.details['sample_mode'] == 'sampled_runs'
        assert run_outcome.details['sample_runs'] == 4 and run_outcome.details['sample_run_length'] == 5
        assert 'read as 4 runs of 5 consecutive cells' in run_outcome.summary


def test_small_dataset_reads_every_cell(submission):
    submission.build()
    with submission.context() as ctx:
        outcome = checks_layers.layers_counts(ctx)
    assert outcome.details['sample_mode'] == 'matrix_exhausted'


def test_runs_note_on_every_outcome_but_skip(submission, monkeypatch):
    # Single.Grafts: x_lognorm is a note and consistency a warning there; both must say how the cells were read
    from ingest_check import note, skip, warn
    _gzip_submission(submission)
    monkeypatch.setattr(thresholds, 'SAMPLE_CELLS', 20)
    monkeypatch.setattr(thresholds, 'SAMPLE_RUNS', 4)
    monkeypatch.setattr(thresholds, 'SAMPLE_INFLATE_BUDGET', 1)
    with submission.context() as ctx:
        for outcome in (note('x'), warn('x', 'fix')):
            marked = checks_layers.with_sample_note(ctx, outcome, 20)
            assert 'runs of 5 consecutive cells' in marked.summary and marked.details['sample_mode'] == 'sampled_runs'
        skipped = checks_layers.with_sample_note(ctx, skip('nothing read'), 20)
        assert skipped.summary == 'nothing read' and 'sample_mode' not in skipped.details
