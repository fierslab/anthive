"""ant ingest-check: less common branches (no database, invalid declarations, store validator, odd inputs)."""
import json
import stat

import numpy as np
import pandas as pd
import pytest

from ingest_check import FAIL, PASS, SKIP, WARN, no_database
from ingest_check import (checks_access, checks_cells, checks_design, checks_donors, checks_facets, checks_files,
                          checks_layers, checks_meta)
from ingest_check.dataset_view import DatasetView
from tests.ingest_helpers import GOOD_MUS, Submission, good_api, make_duckdb, make_h5ad, write_index_md, write_md


@pytest.fixture
def submission(tmp_path):
    return Submission(tmp_path)


def test_no_database_names_the_convert_error(submission):
    submission.build()
    with submission.context() as ctx:
        assert no_database(ctx).summary == 'no readable .duckdb'
        ctx.convert_error = 'ant-convert exited 1'
        assert no_database(ctx).summary == 'no readable .duckdb (ant-convert exited 1)'


@pytest.mark.parametrize('check_function', [
    checks_facets.facets_unmapped_values, checks_facets.facets_braak_range, checks_donors.donor_overlap_ids,
    checks_donors.donor_overlap_demographics, checks_cells.cells_overlap, checks_design.design_replicates_per_group])
def test_checks_that_need_the_database_skip_without_one(submission, check_function):
    submission.build()
    submission.index_dir.mkdir()
    (submission.index_dir / 'state.json').write_text('{"A/B": {}}')          # a non-empty index
    with submission.context() as ctx:
        assert check_function(ctx).status == SKIP


def test_invalid_replicate_unit_is_reported_once_and_skipped_elsewhere(submission):
    submission.build(md={'replicate_unit': 'cell'})
    submission.duckdb()
    with submission.context(duckdb_path=submission.duckdb()) as ctx:
        assert checks_design.design_replicate_unit(ctx).status == FAIL
        assert checks_design.design_replicates_per_group(ctx).status == SKIP
        assert checks_facets.facets_core_present(ctx).status in (PASS, WARN)
        assert checks_donors.donor_uid_form(ctx).status == PASS


def test_unparseable_md_is_reported_by_meta_md_parse_only(submission):
    submission.build()
    (submission.group_dir / 'dataset.md').write_text('---\n[unclosed\n---\n')
    with submission.context() as ctx:
        assert checks_meta.meta_catalog(ctx).status == SKIP
        assert checks_meta.meta_required_keys(ctx).status == SKIP
        assert checks_meta.version_initial(ctx).status == SKIP
        assert checks_meta.meta_md_parse(ctx).status == FAIL


def test_store_validator_findings_for_this_dataset_fail_the_catalog_check(submission):
    submission.build()
    scripts = submission.store / 'scripts'
    scripts.mkdir()
    (scripts / 'validate_md.py').write_text(
        'import sys\nprint("dataset.md: id is bad")\nprint("other.md: ignored")\nsys.exit(1)\n')
    with submission.context() as ctx:
        outcome = checks_meta.meta_catalog(ctx)
    assert outcome.status == FAIL and 'dataset.md: id is bad' in outcome.summary and 'other.md' not in outcome.summary


def test_unreadable_neighbour_index_does_not_break_group_id_uniqueness(submission):
    broken = submission.store / 'Broken'
    broken.mkdir()
    (broken / 'index.md').write_text('---\n[unclosed\n---\n')
    submission.build()
    with submission.context() as ctx:
        assert checks_meta.meta_index_md(ctx).status == PASS


def test_index_md_without_group_id(submission):
    submission.build(with_index_md=False)
    write_md(submission.group_dir / 'index.md', group='TestGroup', doi='10.1/x', year=2026, first_author='A', default_dataset='ABC')
    with submission.context() as ctx:
        assert 'group_id missing' in checks_meta.meta_index_md(ctx).summary


def test_index_md_missing_for_an_existing_dataset_warns(submission):
    submission.build(with_index_md=False)
    with submission.context(mode='existing') as ctx:
        assert checks_meta.meta_index_md(ctx).status == WARN


def test_version_initial_changelog_entry_with_wrong_version(submission):
    submission.build(md={'changelog': [{'version': '0.9.0', 'date': 'd', 'sha256': 'x', 'changes': ['c']}]})
    with submission.context() as ctx:
        assert 'changelog entry version is' in checks_meta.version_initial(ctx).summary


def test_access_values_may_be_lists(submission):
    submission.build(mus=GOOD_MUS.replace('access=internal', 'access=internal,public'))
    with submission.context() as ctx:
        assert checks_access.access_level(ctx).status == PASS


def test_mismatched_shapes_and_flat_expression_skip_the_consistency_check(submission, monkeypatch):
    submission.build()
    with submission.context() as ctx:
        counts = np.ones((5, 4))
        ctx._cache['counts_layer'] = ('counts', ['counts'])
        monkeypatch.setattr(ctx, 'sample_matrix', lambda name, n, seed: (np.arange(5), counts if name == 'counts' else np.ones((5, 3))))
        assert 'differ in shape' in checks_layers.layers_consistency(ctx).summary
        monkeypatch.setattr(ctx, 'sample_matrix', lambda name, n, seed: (np.arange(5), counts))
        assert 'no cell with variation' in checks_layers.layers_consistency(ctx).summary


def test_empty_sample_is_not_counts():
    assert checks_layers._is_counts(np.zeros((0, 3))) is False
    assert checks_layers._is_counts(np.zeros((3, 3))) is False


def test_an_h5ad_without_expression_fails_structure(submission):
    # backed anndata used to raise on a file without X; read with h5py the file opens and the
    # structure rule names what is missing
    import anndata
    submission.build()
    adata = anndata.AnnData(obs=pd.DataFrame(index=['a']), var=pd.DataFrame(index=['g']))
    adata.write_h5ad(submission.group_dir / 'dataset.h5ad')
    with submission.context() as ctx:
        outcome = checks_files.h5ad_structure(ctx)
    assert outcome.status == FAIL and 'Missing both .X and .layers' in outcome.summary


def test_an_h5ad_that_cannot_be_inspected_fails_structure(submission):
    submission.build()
    (submission.group_dir / 'dataset.h5ad').write_bytes(b'not an hdf5 file' * 64)
    with submission.context() as ctx:
        outcome = checks_files.h5ad_structure(ctx)
    assert outcome.status == FAIL and 'cannot be read' in outcome.summary


def test_an_h5ad_without_var_fails_structure(submission):
    import h5py
    submission.build()
    target = submission.group_dir / 'dataset.h5ad'
    with h5py.File(target, 'a') as handle:
        del handle['var']
    with submission.context() as ctx:
        outcome = checks_files.h5ad_structure(ctx)
    assert outcome.status == FAIL and 'cannot be read' in outcome.summary


def test_dataset_view_published_columns_and_distinct_values(tmp_path):
    sub = Submission(tmp_path)
    database = make_duckdb(tmp_path / 'view.duckdb', sub.obs)
    view = DatasetView(database, good_api(), 'ZZ1', db_label='ZZ1/ABC')
    try:
        columns = view.published_columns()
        assert columns['ant_sex'] == 'categorical' and columns['ant_age'] == 'numerical' and 'cell_name' not in columns
        assert view.n_cells() == 60 and len(view.cell_names()) == 60
        frame = view.cell_frame(['ant_donor', 'ant_age', 'nope'])
        assert list(frame.columns) == ['cell_id', 'ant_donor', 'ant_age']
        assert dict(view.distinct_values('ant_sex')) == {'f': 30, 'm': 30} or sum(n for _v, n in view.distinct_values('ant_sex')) == 60
        assert view.distinct_values('does_not_exist') is None
        assert view.view_columns('missing_view') == {}
    finally:
        view.close()


def test_dataset_view_rejects_duplicate_cell_names(tmp_path):
    obs = Submission(tmp_path).obs.copy()
    obs.index = ['same'] * len(obs)
    database = make_duckdb(tmp_path / 'dup.duckdb', obs)
    view = DatasetView(database, good_api(), 'ZZ1')
    try:
        with pytest.raises(RuntimeError, match='duplicate cell names'):
            view.cell_frame(['ant_donor'])
    finally:
        view.close()


def test_report_modes_for_group_folders_in_and_out_of_the_store(tmp_path):
    sub = Submission(tmp_path).build()
    with sub.context() as ctx:
        assert ctx.mode == 'new' and not ctx.in_store and not ctx.is_existing_group
    inside = sub.store / 'TestGroup'
    inside.mkdir()
    write_index_md(inside, group_id='ZZ1')
    with sub.context() as ctx:
        assert ctx.mode == 'new' and ctx.is_existing_group and not ctx.in_store
    with sub.context(mode='existing') as ctx:
        assert ctx.mode == 'existing'


def test_context_in_store_uses_the_store_cascade_root(tmp_path):
    sub = Submission(tmp_path)
    sub.group_dir = sub.store / 'TestGroup'
    sub.group_dir.mkdir()
    sub.build()
    (sub.store / '.env').write_text('organism=human\n')
    with sub.context() as ctx:
        assert ctx.in_store and ctx.mode == 'existing' and ctx.organism == 'human'


def test_annotation_merges_the_store_group_cascade_for_a_new_dataset_in_an_existing_group(tmp_path):
    sub = Submission(tmp_path).build(mus='access=internal\nsource=external\n')
    stored = sub.store / 'TestGroup'
    stored.mkdir()
    (stored / '.env').write_text('organism=mouse\ntissue=cortex\n')
    with sub.context() as ctx:
        assert ctx.organism == 'mouse' and ctx.annotation['tissue'] == 'cortex' and ctx.annotation['access'] == 'internal'


def test_organism_may_be_a_list(tmp_path):
    sub = Submission(tmp_path).build(mus='organism=human,mouse\n')
    with sub.context() as ctx:
        assert ctx.organism == 'human'


def test_close_is_idempotent_and_handles_failures(submission):
    submission.build()
    ctx = submission.context()
    ctx._closers.append(lambda: (_ for _ in ()).throw(RuntimeError('already closed')))
    ctx.close()
    ctx.close()


def test_sensitive_tokens_ignore_short_values_and_a_missing_file(submission):
    submission.build()
    with submission.context() as ctx:
        assert ctx.sensitive_tokens() == set() and ctx.controlled_cohorts() == set()
    (submission.store / 'donor_crosswalk.tsv').write_text('cohort\talias_type\talias\tcanonical_uid\tsource\tn_datasets\n'
                                                          'c\tx\tab\tc:abcdef\ts\t1\n')
    with submission.context() as ctx:
        assert ctx.sensitive_tokens() == {'c:abcdef'} and ctx.controlled_cohorts() == set()


def test_crosswalk_reports_stay_json_serialisable(submission):
    submission.build()
    with submission.context() as ctx:
        json.dumps({'tokens': sorted(ctx.sensitive_tokens())})
