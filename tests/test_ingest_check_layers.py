"""ant ingest-check: layer checks (counts, log-normalised X, consistency) and gene ids."""
import numpy as np
import pytest

from ingest_check import FAIL, PASS, SKIP, WARN
from ingest_check import checks_layers, sampling
from ingest_check.overlap_index import DatasetDigests, OverlapIndex
from tests.ingest_helpers import Submission, make_h5ad
import pandas as pd


@pytest.fixture
def submission(tmp_path):
    return Submission(tmp_path)


def run(submission, check_function, **build_options):
    submission.build(**build_options)
    with submission.context() as ctx:
        return check_function(ctx)


@pytest.mark.parametrize('layout', ['csr', 'csc', 'dense'])
def test_counts_found_in_every_storage_layout(submission, layout):
    outcome = run(submission, checks_layers.layers_counts, layout=layout)
    assert outcome.status == PASS
    assert outcome.details['layer'] == 'counts'


def test_missing_counts_is_an_error_with_a_fix(submission):
    outcome = run(submission, checks_layers.layers_counts, counts=False)
    assert outcome.status == FAIL
    assert 'counts_unavailable' in outcome.fix


def test_counts_unavailable_with_reason_is_a_warning(submission):
    outcome = run(submission, checks_layers.layers_counts, counts=False, md={'counts_unavailable': 'authors only share normalised data'})
    assert outcome.status == WARN
    assert outcome.details['counts_unavailable'] == 'authors only share normalised data'


def test_float_layer_is_not_counts(submission):
    submission.build(counts=False)
    import anndata
    adata = anndata.read_h5ad(submission.group_dir / 'dataset.h5ad')
    adata.layers['scaled'] = adata.X.toarray() + 0.5
    adata.write_h5ad(submission.group_dir / 'dataset.h5ad')
    with submission.context() as ctx:
        assert checks_layers.layers_counts(ctx).status == FAIL


@pytest.mark.parametrize('x_kind,status', [('lognorm', PASS), ('counts', FAIL), ('scaled', FAIL), ('big', FAIL)])
def test_x_must_be_log_normalised(submission, x_kind, status):
    outcome = run(submission, checks_layers.layers_x_lognorm, x_kind=x_kind)
    assert outcome.status == status


def test_x_on_log_scale_without_library_size_normalisation_warns_and_accepts_a_declaration(submission):
    submission.build(x_noise=0.5, counts=False)
    with submission.context() as ctx:
        outcome = checks_layers.layers_x_lognorm(ctx)
    assert outcome.status == WARN
    assert 'layers["counts"]' in outcome.fix and 'only when X uses another method' in outcome.fix
    submission.build(x_noise=0.5, md={'normalisation': 'vst'})
    with submission.context() as ctx:
        declared = checks_layers.layers_x_lognorm(ctx)
    assert declared.status == PASS and declared.info


def test_all_zero_x_fails(tmp_path):
    submission = Submission(tmp_path).build()
    import anndata
    adata = anndata.read_h5ad(submission.group_dir / 'dataset.h5ad')
    adata.X = adata.X * 0
    adata.write_h5ad(submission.group_dir / 'dataset.h5ad')
    with submission.context() as ctx:
        assert checks_layers.layers_x_lognorm(ctx).status == FAIL


def test_consistency_passes_for_matching_x(submission):
    outcome = run(submission, checks_layers.layers_consistency)
    assert outcome.status == PASS
    assert outcome.details['median_r'] >= 0.99


@pytest.mark.parametrize('noise,status', [(0.5, WARN), (1.5, FAIL)])
def test_consistency_degrades_with_noise(submission, noise, status):
    outcome = run(submission, checks_layers.layers_consistency, x_noise=noise)
    assert outcome.status == status


def test_consistency_skipped_without_counts_or_with_declared_normalisation(submission):
    assert run(submission, checks_layers.layers_consistency, counts=False).status == SKIP
    outcome = run(submission, checks_layers.layers_consistency, x_noise=1.5, md={'normalisation': 'vst'})
    assert outcome.status == SKIP


def test_sampling_is_reproducible_and_reads_every_encoding(tmp_path):
    paths = {}
    for layout in ('csr', 'csc', 'dense'):
        paths[layout] = make_h5ad(tmp_path / f'{layout}.h5ad', layout=layout, seed=3)
    samples = []
    for layout, path in paths.items():
        hdf5_path = sampling.matrix_paths(path)['counts']
        n_obs, n_vars = sampling.matrix_shape(path, hdf5_path)
        rows = sampling.sample_indices(n_obs, 20, seed=0)
        samples.append(sampling.read_rows(path, hdf5_path, rows))
    assert (n_obs, n_vars) == (60, 40)
    np.testing.assert_array_equal(samples[0], samples[1])
    np.testing.assert_array_equal(samples[0], samples[2])
    np.testing.assert_array_equal(sampling.sample_indices(60, 20, 0), sampling.sample_indices(60, 20, 0))
    np.testing.assert_array_equal(sampling.sample_indices(10, 20, 0), np.arange(10))


def test_sampling_rejects_unknown_encoding(tmp_path):
    import h5py
    path = tmp_path / 'odd.h5ad'
    with h5py.File(path, 'w') as handle:
        group = handle.create_group('X')
        group.attrs['encoding-type'] = 'coo_matrix'
        group.attrs['shape'] = (2, 2)
        group.create_dataset('indptr', data=np.array([0, 1, 2]))
    with pytest.raises(ValueError):
        sampling.read_rows(path, 'X', [0])


def test_counts_candidates_prefer_conventional_names():
    assert sampling.counts_candidates(['scaled', 'counts', 'UMIs', 'X']) == ['counts', 'UMIs', 'scaled']


def seed_gene_universe(index_dir, genes, n_datasets=3, organism='human'):
    index = OverlapIndex(index_dir)
    for number in range(n_datasets):
        digests = DatasetDigests(f'X{number}/AAA', f'X{number}', pd.DataFrame(), np.array([], dtype=np.uint64),
                                 np.array([], dtype=np.uint64), genes, 0)
        index.write_dataset(digests, {'seed': number}, organism)
    index.rebuild_combined()


def test_genes_pass_with_known_symbols(submission):
    genes = [f'GENE{i}' for i in range(40)]
    seed_gene_universe(submission.index_dir, genes)
    outcome = run(submission, checks_layers.genes_ids)
    assert outcome.status == PASS and outcome.details['known_share'] == 1.0


@pytest.mark.parametrize('known,status', [(30, WARN), (20, FAIL)])
def test_genes_known_share_thresholds(submission, known, status):
    seed_gene_universe(submission.index_dir, [f'GENE{i}' for i in range(known)])
    assert run(submission, checks_layers.genes_ids).status == status


def test_genes_case_insensitive_match_for_mouse_style_symbols(tmp_path):
    submission = Submission(tmp_path)
    seed_gene_universe(submission.index_dir, [f'Gene{i}' for i in range(40)])
    outcome = run(submission, checks_layers.genes_ids)
    assert outcome.status == PASS


def test_genes_without_universe_pass_with_a_note(submission):
    outcome = run(submission, checks_layers.genes_ids)
    assert outcome.status == PASS and 'not judged' in outcome.summary


def test_genes_ensembl_names_fail(submission):
    names = [f'ENSG{index:011d}' for index in range(40)]
    submission.build(gene_names=names)
    with submission.context() as ctx:
        outcome = checks_layers.genes_ids(ctx)
    assert outcome.status == FAIL and 'ensg_id' in outcome.fix


def test_genes_duplicates_fail(submission):
    names = ['GENEA'] * 5 + [f'GENE{i}' for i in range(35)]
    submission.build(gene_names=names)
    with submission.context() as ctx:
        outcome = checks_layers.genes_ids(ctx)
    assert outcome.status == FAIL and outcome.details['n_duplicated'] == 4


def test_x_row_sums_vary_with_a_counts_layer_defers_to_consistency(submission):
    """With a counts layer, x_lognorm does not advise declaring normalisation (that skips the comparison)."""
    submission.build(x_noise=0.5)
    with submission.context() as ctx:
        outcome = checks_layers.layers_x_lognorm(ctx)
    assert outcome.status == PASS and outcome.info
    assert 'layers.consistency' in outcome.summary and 'declare' not in outcome.summary
