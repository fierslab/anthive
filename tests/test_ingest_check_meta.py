"""ant ingest-check: file, metadata, version, access and design checks."""
import json

import pytest

from ingest_check import FAIL, PASS, SKIP, WARN, HELD_CONTROLLED, HELD_DATA_USE, HELD_LICENCE, HELD_PUBLIC
from ingest_check import checks_access, checks_design, checks_files, checks_meta
from ingest_check.context import Settings
from tests.ingest_helpers import GOOD_MUS, Submission, good_api, write_index_md, write_md


@pytest.fixture
def submission(tmp_path):
    return Submission(tmp_path)


def run(sub, check_function, md=None, mus=GOOD_MUS, duckdb=False, build=True, **context_options):
    if build:
        sub.build(md=md, mus=mus)
    with sub.context(duckdb_path=sub.duckdb() if duckdb else None, **context_options) as ctx:
        return check_function(ctx)


# ── files ────────────────────────────────────────────────────────────────────

def test_sha256_matches_md_and_declared_hash(submission):
    outcome = run(submission, checks_files.files_sha256)
    assert outcome.status == PASS and len(outcome.details['sha256']) == 64
    declared = run(submission, checks_files.files_sha256, declared_sha256=outcome.details['sha256'].upper())
    assert declared.status == PASS


def test_sha256_mismatch_with_declared_hash_fails(submission):
    outcome = run(submission, checks_files.files_sha256, declared_sha256='0' * 64)
    assert outcome.status == FAIL and 'declared hash' in outcome.summary


def test_sha256_mismatch_with_md_fails_for_new_and_warns_for_existing(submission):
    outcome = run(submission, checks_files.files_sha256, md={'sha256': '1' * 64})
    assert outcome.status == FAIL and 'sha256 in the .md' in outcome.summary
    existing = run(submission, checks_files.files_sha256, md={'sha256': '1' * 64}, mode='existing')
    assert existing.status == WARN and 'ant build-all' in existing.fix


def test_sha256_without_anything_declared_reports_the_hash(tmp_path):
    sub = Submission(tmp_path).build()
    write_md(sub.group_dir / 'dataset.md', id='ABC', title='x')
    with sub.context() as ctx:
        outcome = checks_files.files_sha256(ctx)
    assert outcome.status == PASS and 'nothing declared' in outcome.summary


def test_size_limit(submission):
    assert run(submission, checks_files.files_size).status == PASS
    outcome = run(submission, checks_files.files_size, settings=Settings(max_upload_gb=1e-9))
    assert outcome.status == FAIL and outcome.details['limit_gb'] == 1e-9


@pytest.mark.parametrize('basename,status', [('dataset', PASS), ('my data', FAIL), ('x' * 60, FAIL), ('bad$name', FAIL)])
def test_filename_rules(tmp_path, basename, status):
    sub = Submission(tmp_path, basename=basename)
    with sub.context() as ctx:
        assert checks_files.files_filename(ctx).status == status


@pytest.mark.parametrize('group', ['_incoming', '.hidden', 'bad group'])
def test_group_folder_names_are_validated(tmp_path, group):
    sub = Submission(tmp_path, group=group)
    with sub.context() as ctx:
        assert checks_files.files_filename(ctx).status == FAIL


def test_h5ad_structure(submission):
    outcome = run(submission, checks_files.h5ad_structure)
    assert outcome.status == PASS and outcome.details['n_obs'] == 60
    (submission.group_dir / 'dataset.h5ad').write_bytes(b'not an h5ad')
    with submission.context() as ctx:
        assert checks_files.h5ad_structure(ctx).status == FAIL


def test_h5ad_without_embedding_warns(submission):
    import anndata
    submission.build()
    adata = anndata.read_h5ad(submission.group_dir / 'dataset.h5ad')
    del adata.obsm['X_umap']
    adata.write_h5ad(submission.group_dir / 'dataset.h5ad')
    with submission.context() as ctx:
        assert checks_files.h5ad_structure(ctx).status == WARN


def test_duckdb_integrity_reports_a_failed_convert(submission):
    submission.build()
    with submission.context() as ctx:
        ctx.convert_error = 'ant-convert exited 1: boom'
        ctx._cache['duckdb'] = None
        outcome = checks_files.duckdb_integrity(ctx)
    assert outcome.status == FAIL and 'boom' in outcome.summary


def test_report_lines_parse_the_tool_output():
    output = '\n'.join(['junk', '=' * 10, 'WARNINGS & ERRORS', '=' * 10, '', '✗ Missing tables:', '  - a', '',
                        '  (Warnings/errors logged to /x)', '=' * 10, '✗ CHECK FAILED'])
    assert checks_files._report_lines(output) == ['✗ Missing tables:', '- a']


# ── metadata ─────────────────────────────────────────────────────────────────

def test_md_parse(submission):
    assert run(submission, checks_meta.meta_md_parse).status == PASS
    (submission.group_dir / 'dataset.md').write_text('---\n: : bad yaml [\n---\n')
    with submission.context() as ctx:
        assert checks_meta.meta_md_parse(ctx).status == FAIL
    (submission.group_dir / 'dataset.md').unlink()
    with submission.context() as ctx:
        outcome = checks_meta.meta_md_parse(ctx)
    assert outcome.status == FAIL and 'not found' in outcome.summary


def test_required_keys(submission):
    assert run(submission, checks_meta.meta_required_keys).status == PASS
    outcome = run(submission, checks_meta.meta_required_keys, md={'description': 'too short', 'changelog': []})
    assert outcome.status == FAIL
    assert 'missing changelog' in outcome.summary and 'description is 9 characters' in outcome.summary


def test_description_may_be_the_markdown_body_and_is_only_a_warning_for_legacy_datasets(submission):
    submission.build(md={'description': ''})
    long_body = 'word ' * 60
    (submission.group_dir / 'dataset.md').write_text((submission.group_dir / 'dataset.md').read_text() + long_body)
    with submission.context() as ctx:
        assert checks_meta.meta_required_keys(ctx).status == PASS
    submission.build(md={'description': ''})
    with submission.context(mode='existing') as ctx:
        outcome = checks_meta.meta_required_keys(ctx)
    assert outcome.status == WARN and 'existing dataset' in outcome.summary


def test_catalog_valid_id_and_generated_id(submission):
    outcome = run(submission, checks_meta.meta_catalog)
    assert outcome.status == PASS and outcome.details['db_id'] == 'ZZ1/ABC'
    submission.build()
    write_md(submission.group_dir / 'dataset.md', title='no id here')
    with submission.context() as ctx:
        outcome = checks_meta.meta_catalog(ctx)
    assert outcome.status == WARN and outcome.details['proposed_id']


def test_catalog_flags_bad_ids_and_store_duplicates(submission):
    outcome = run(submission, checks_meta.meta_catalog, md={'id': 'TOOLONG'})
    assert outcome.status == FAIL and 'not exactly 3 characters' in outcome.summary
    taken = submission.store / 'Other'
    taken.mkdir()
    write_index_md(taken, group_id='ZZ1')
    write_md(taken / 'x.md', id='ABC', title='x')
    (taken / 'x.h5ad').write_bytes(b'')
    outcome = run(submission, checks_meta.meta_catalog)
    assert outcome.status == FAIL and 'already exists in the store' in outcome.summary


def test_catalog_scans_the_group_folder_even_below_an_underscore_folder(tmp_path):
    """md_check skips `_` folders below its root: the root must be the group folder itself."""
    sub = Submission(tmp_path)
    incoming = sub.store / '_incoming' / 'user' / 'sid' / 'Group'
    incoming.mkdir(parents=True)
    sub.group_dir = incoming
    sub.build(md={'id': 'TOOLONG'})
    with sub.context() as ctx:
        outcome = checks_meta.meta_catalog(ctx)
    assert outcome.status == FAIL and 'not exactly 3 characters' in outcome.summary


def test_index_md_new_group(submission):
    assert run(submission, checks_meta.meta_index_md).status == PASS
    (submission.group_dir / 'index.md').unlink()
    with submission.context() as ctx:
        assert checks_meta.meta_index_md(ctx).status == FAIL
    write_index_md(submission.group_dir, group_id='AB')
    with submission.context() as ctx:
        outcome = checks_meta.meta_index_md(ctx)
    assert outcome.status == FAIL and 'not exactly 3 characters' in outcome.summary


def test_index_md_incomplete_and_mismatching_folder(submission):
    submission.build(with_index_md=False)
    write_md(submission.group_dir / 'index.md', group='Wrong', group_id='ZZ1')
    with submission.context() as ctx:
        outcome = checks_meta.meta_index_md(ctx)
    assert outcome.status == FAIL
    assert 'does not match the folder name' in outcome.summary and 'missing doi or paper_title' in outcome.summary


def test_index_md_group_id_used_by_another_group(submission):
    other = submission.store / 'Other'
    other.mkdir()
    write_index_md(other, group_id='ZZ1')
    outcome = run(submission, checks_meta.meta_index_md)
    assert outcome.status == FAIL and 'already used by another group' in outcome.summary


def test_index_md_for_an_existing_group_must_agree_with_the_store(submission):
    stored = submission.store / submission.group_dir.name
    stored.mkdir()
    write_index_md(stored, group_id='QQ9')
    outcome = run(submission, checks_meta.meta_index_md)
    assert outcome.status == FAIL and 'differs from the group in the store' in outcome.summary
    (submission.group_dir / 'index.md').unlink()
    with submission.context() as ctx:
        assert checks_meta.meta_index_md(ctx).status == PASS         # uses the stored index.md


def test_index_md_warns_for_missing_pieces_in_an_existing_dataset(submission):
    submission.build(with_index_md=False)
    write_md(submission.group_dir / 'index.md', group=submission.group_dir.name, group_id='ZZ1')
    with submission.context(mode='existing') as ctx:
        assert checks_meta.meta_index_md(ctx).status == WARN


def test_mus_vocab(submission):
    assert run(submission, checks_meta.mus_vocab).status == PASS
    outcome = run(submission, checks_meta.mus_vocab, mus=GOOD_MUS.replace('organism=human\n', ''))
    assert outcome.status == FAIL and 'organism' in outcome.summary
    outcome = run(submission, checks_meta.mus_vocab, mus=GOOD_MUS.replace('suspension_type=nucleus', 'suspension_type=blob'))
    assert outcome.status == FAIL and 'suspension_type' in outcome.summary
    soft = '\n'.join(line for line in GOOD_MUS.splitlines() if not line.startswith(('license', 'disease')))
    assert run(submission, checks_meta.mus_vocab, mus=soft).status == WARN


def test_mus_missing_sidecar_fails(submission):
    submission.build()
    (submission.group_dir / 'dataset.h5ad.mus').unlink()
    with submission.context() as ctx:
        assert checks_meta.mus_vocab(ctx).status == FAIL


def test_mus_fields_come_from_the_env_cascade_too(submission):
    submission.build(mus='access=internal\nsource=external\n')
    (submission.group_dir / '.env').write_text('organism=human\ntissue=brain\ncell_type=microglial cell\n'
                                               'technology=10x_chromium\nsuspension_type=nucleus\n')
    with submission.context() as ctx:
        assert checks_meta.mus_vocab(ctx).status != FAIL


def test_version_initial(submission):
    assert run(submission, checks_meta.version_initial).status == PASS
    bad = run(submission, checks_meta.version_initial, md={'version': '0.3.0'})
    assert bad.status == FAIL and '0.3.0' in bad.summary
    two = run(submission, checks_meta.version_initial, md={'changelog': [{'version': '1.0.0'}, {'version': '1.0.1'}]})
    assert two.status == FAIL and '2 entries' in two.summary
    wrong_hash = run(submission, checks_meta.version_initial, md={'changelog': [
        {'version': '1.0.0', 'date': '2026-10-05', 'sha256': 'f' * 64, 'changes': ['x']}]})
    assert wrong_hash.status == FAIL and 'sha256' in wrong_hash.summary
    incomplete = run(submission, checks_meta.version_initial, md={'changelog': [{'version': '1.0.0', 'sha256': 'f' * 64}]})
    assert 'lacks date' in incomplete.summary


def test_version_initial_skipped_for_existing_datasets(submission):
    assert run(submission, checks_meta.version_initial, mode='existing').status == SKIP


# ── access ───────────────────────────────────────────────────────────────────

def test_access_level(submission):
    assert run(submission, checks_access.access_level).status == PASS
    public = run(submission, checks_access.access_level, mus=GOOD_MUS.replace('access=internal', 'access=public'))
    assert public.status == WARN and public.held == [HELD_PUBLIC]
    allowed = run(submission, checks_access.access_level, mus=GOOD_MUS.replace('access=internal', 'access=public'),
                  settings=Settings(auto_publish_public=True))
    assert allowed.status == PASS and not allowed.held
    for text in ('access=secret', ''):
        mus = GOOD_MUS.replace('access=internal', text)
        assert run(submission, checks_access.access_level, mus=mus).status == FAIL


def test_access_controlled(submission):
    assert run(submission, checks_access.access_controlled).status == PASS
    controlled = run(submission, checks_access.access_controlled, mus=GOOD_MUS.replace('data_use=open', 'data_use=controlled'))
    assert controlled.status == WARN and controlled.held == [HELD_CONTROLLED]
    unknown = run(submission, checks_access.access_controlled, mus=GOOD_MUS.replace('data_use=open\n', ''))
    assert unknown.status == WARN and unknown.held == [HELD_DATA_USE]
    invalid = run(submission, checks_access.access_controlled, mus=GOOD_MUS.replace('data_use=open', 'data_use=maybe'))
    assert invalid.status == FAIL


def test_controlled_cohort_in_the_join_holds_open_data(submission):
    api = good_api()
    api['obs_derive']['ant_donor_uid'] = {'source': 'donor', 'join': 'donor_crosswalk.tsv', 'cohort': 'secretco'}
    (submission.store / 'donor_crosswalk.tsv').write_text(
        'cohort\talias_type\talias\tcanonical_uid\tsource\tn_datasets\tcontrolled\nsecretco\tdonor_id\tA1\tsecretco:A1\tx\t1\ttrue\n')
    outcome = run(submission, checks_access.access_controlled, md={'api': api})
    assert outcome.status == WARN and outcome.held == [HELD_CONTROLLED]
    assert outcome.admin_detail == {'cohort': 'secretco'} and 'secretco' not in json.dumps(outcome.details) + outcome.summary


@pytest.mark.parametrize('licence,status,held', [('CC-BY-4.0', PASS, []), ('in-house', PASS, []), ('', WARN, [HELD_LICENCE]),
                                                 ('unknown', WARN, [HELD_LICENCE]), ('see the paper please', FAIL, [])])
def test_licence(submission, licence, status, held):
    mus = '\n'.join(line for line in GOOD_MUS.splitlines() if not line.startswith('license'))
    if licence:
        mus += f'\nlicense={licence}'
    outcome = run(submission, checks_access.access_licence, mus=mus)
    assert outcome.status == status and outcome.held == held


# ── design ───────────────────────────────────────────────────────────────────

def test_designation(submission):
    assert run(submission, checks_design.design_designation).status == PASS
    missing = run(submission, checks_design.design_designation, md={'is_secondary': True, 'reanalysis_of': ['NO/SUCH']})
    assert missing.status == FAIL and 'reanalysis_target_missing' in missing.summary
    not_secondary = run(submission, checks_design.design_designation, md={'reanalysis_of': ['NO/SUCH']})
    assert 'reanalysis_not_secondary' in not_secondary.summary
    assert run(submission, checks_design.design_designation, md={'donor_group': 'Bad Slug'}).status == WARN


@pytest.mark.parametrize('md,status,text', [
    ({}, PASS, 'replicate_unit donor'),
    ({'replicate_unit': None, 'replicate_column': None}, FAIL, 'replicate_unit_missing'),
    ({'replicate_unit': 'cell'}, FAIL, 'replicate_unit_invalid'),
    ({'replicate_unit': 'donor', 'replicate_column': 'nope'}, FAIL, 'replicate_column_missing'),
    ({'replicate_unit': 'none', 'replicate_column': 'donor'}, WARN, 'replicate_column_unused'),
])
def test_replicate_unit(submission, md, status, text):
    outcome = run(submission, checks_design.design_replicate_unit, md=md)
    assert outcome.status == status and text in outcome.summary


def test_samples_must_nest_within_donors(submission):
    submission.obs['sample'] = [f'S{index % 5}' for index in range(60)]       # each S* spans several donors
    api = good_api()
    md = {'replicate_unit': 'sample', 'replicate_column': 'sample', 'api': api}
    outcome = run(submission, checks_design.design_replicate_unit, md=md)
    assert outcome.status == FAIL and outcome.details['n_samples_in_several_donors'] == 5
    submission.obs['sample'] = submission.obs['donor'] + '_' + submission.obs['sample']
    assert run(submission, checks_design.design_replicate_unit, md=md).status == PASS


def test_replicates_per_group(submission):
    submission.obs['braak'] = 1                                   # one Braak group with all donors
    outcome = run(submission, checks_design.design_replicates_per_group, duckdb=True)
    assert outcome.status == PASS and outcome.details['n_units'] == 8
    assert outcome.details['per_facet']['ant_sex'] == {'f': 4, 'm': 4}


def test_few_replicates_and_thin_levels_warn(tmp_path):
    from tests.ingest_helpers import donor_obs
    sub = Submission(tmp_path / 'few')
    sub.obs = donor_obs(30, n_donors=3)
    outcome = run(sub, checks_design.design_replicates_per_group, duckdb=True)
    assert outcome.status == WARN and 'fewer than 4' in outcome.summary
    thin = Submission(tmp_path / 'thin')
    thin.obs = donor_obs(70, n_donors=7)
    thin.obs['dx'] = thin.obs['donor'].map(lambda label: 'AD' if label in ('D00', 'D01') else 'CTRL')
    outcome = run(thin, checks_design.design_replicates_per_group, duckdb=True)
    assert outcome.status == WARN and 'ant_disease=AD (2)' in outcome.summary


def test_replicates_none_is_informative_and_unpublished_column_is_skipped(submission):
    outcome = run(submission, checks_design.design_replicates_per_group, md={'replicate_unit': 'none', 'replicate_column': None}, duckdb=True)
    assert outcome.status == PASS and outcome.info
    skipped = run(submission, checks_design.design_replicates_per_group, md={'replicate_unit': 'sample', 'replicate_column': 'nope'}, duckdb=True)
    assert skipped.status == SKIP
    assert run(submission, checks_design.design_replicates_per_group, duckdb=False).status == SKIP


def test_disease_basis(submission):
    assert run(submission, checks_design.design_disease_basis).status == PASS
    missing = run(submission, checks_design.design_disease_basis, md={'disease_basis': None})
    assert missing.status == FAIL and 'disease_basis_missing' in missing.summary
    invalid = run(submission, checks_design.design_disease_basis, md={'disease_basis': 'vibes'})
    assert 'disease_basis_invalid' in invalid.summary
    derivation = run(submission, checks_design.design_disease_basis, md={'disease_basis': 'braak_cerad'})
    assert derivation.status == FAIL and 'disease_derivation_invalid' in derivation.summary


def test_disease_basis_not_needed_without_ant_disease(submission):
    api = good_api()
    api['obs_rename'].pop('dx')
    outcome = run(submission, checks_design.design_disease_basis, md={'disease_basis': None, 'api': api})
    assert outcome.status == PASS and 'no disease basis needed' in outcome.summary


def test_replicate_column_that_is_not_published_is_counted_from_the_obs(submission):
    submission.obs['batch'] = [f'B{index % 7}' for index in range(60)]
    md = {'replicate_unit': 'sample', 'replicate_column': 'batch'}
    api = good_api()
    api['obs_hidden'] = ['batch']
    submission.build(md={**md, 'api': api})
    with submission.context(duckdb_path=submission.duckdb()) as ctx:
        outcome = checks_design.design_replicates_per_group(ctx)
        assert outcome.status == PASS and outcome.details['n_units'] == 7 and outcome.details['per_facet'] == {}
    submission.obs['batch'] = [f'B{index % 3}' for index in range(60)]
    submission.build(md={**md, 'api': api})
    with submission.context(duckdb_path=submission.duckdb()) as ctx:
        assert checks_design.design_replicates_per_group(ctx).status == WARN


def test_scores_sidecar_explains_missing_score_columns(tmp_path):
    import pandas as pd
    folder = tmp_path / 'store' / 'G'
    folder.mkdir(parents=True)
    pd.DataFrame({'cell_id': [1], 'source': ['geneset'], 'catalog': ['S/C'], 'module': ['M1'], 'score': [0.5]}).to_parquet(
        folder / 'ds.scores.parquet')
    database = folder / 'ds.duckdb'
    lines = ['\u2717 Missing required tables:', '- __obsnum.S::C::M1 (numerical obs column missing)']
    assert checks_files._covered_by_scores(lines, database)
    assert not checks_files._covered_by_scores(lines + ['- __obsnum.S::C::M2 (numerical obs column missing)'], database)
    assert not checks_files._covered_by_scores(lines + ['\u2717 Missing table __genes'], database)
    assert not checks_files._covered_by_scores(['\u2717 Missing required tables:'], database)
    assert not checks_files._covered_by_scores(lines, tmp_path / 'elsewhere' / 'ds.duckdb')


def test_index_md_of_an_analysis_collection_needs_no_default_dataset(submission):
    """doc/DESIGN_artefacts.md 2.6: an analysis collection has no dataset to show first."""
    submission.build(with_index_md=False)
    front = {'group': submission.group_dir.name, 'group_id': 'ZZ1', 'doi': '10.1000/test', 'year': 2026,
             'first_author': 'Test Author'}
    write_md(submission.group_dir / 'index.md', **front)
    with submission.context() as ctx:
        outcome = checks_meta.meta_index_md(ctx)
    assert outcome.status == FAIL and 'default_dataset' in outcome.summary
    write_md(submission.group_dir / 'index.md', collection_kind='analysis', **front)
    with submission.context() as ctx:
        assert checks_meta.meta_index_md(ctx).status == PASS
