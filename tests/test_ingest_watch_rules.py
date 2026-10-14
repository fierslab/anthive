"""S3 watch route: catalogue fill, version bumps, archived versions and purge, config, source guards."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import config
import ingest_md_fill as md_fill
import ingest_versions
import md_edit
from tests.ingest_watch_helpers import BUCKET, FakeS3, make_h5ad, sha_of

ROOT = Path(__file__).parent.parent
WATCH_SOURCES = [ROOT / 'lib' / 'ingest_pipeline.py', ROOT / 'lib' / 'ingest_watch.py',
                 ROOT / 'lib' / 'ingest_md_fill.py', ROOT / 'lib' / 'ingest_versions.py',
                 ROOT / 'bin' / 'ingest' / 'ant-ingest-watch', ROOT / 'bin' / 'ingest' / 'ant-ingest']


# ── source guards ───────────────────────────────────────────────────────────

@pytest.mark.parametrize('source', WATCH_SOURCES, ids=lambda path: path.name)
def test_the_destructive_checkers_are_never_called(source):
    text = source.read_text()
    for pattern in (r'ant-h5ad-check', r"'ant-check'", r'meta/ant-check', r'ant-md-check', r'fix\s*=\s*True'):
        assert not re.search(pattern, text), f'{source.name} mentions {pattern}'


def test_s3_deletes_live_only_in_the_guarded_functions():
    """DeleteObject appears only in _delete_if_unchanged and _delete_selftest_object (watch) and purge (versions)."""
    for source in WATCH_SOURCES[:5]:
        text = source.read_text()
        assert 'delete_objects' not in text, source.name
        calls = [match.start() for match in re.finditer(r'\.delete_object\(', text)]
        enclosing = {text[text.rfind('\ndef ', 0, start):].split('(')[0].strip() for start in calls}
        if source.name == 'ingest_pipeline.py':
            # consumed incoming keys (HEAD-compared, optional IfMatch) and the IfMatch self-test's own scratch key
            assert enclosing == {'def _delete_if_unchanged', 'def _delete_selftest_object'} and len(calls) == 3
        elif source.name == 'ingest_versions.py':
            assert enclosing == {'def purge'} and len(calls) == 1
        else:
            assert not calls, source.name


def test_nothing_is_written_under_the_repo_by_default_paths(monkeypatch, tmp_path):
    monkeypatch.setenv('ANTHIVE_INGEST_WORK_DIR', str(tmp_path / 'work'))
    assert config.get_ingest_work_dir({}) == tmp_path / 'work'
    monkeypatch.delenv('ANTHIVE_INGEST_WORK_DIR')
    monkeypatch.setenv('ANTHIVE_TMP_DIR', str(tmp_path / 'tmp'))
    assert config.get_ingest_work_dir({}) == tmp_path / 'tmp' / 'ingest_work'


# ── config ──────────────────────────────────────────────────────────────────

def test_ingest_watch_settings_precedence(monkeypatch):
    settings = config.get_ingest_watch_settings({})
    assert settings['enabled'] is False and settings['interval_seconds'] == 300
    assert settings['prefix'] == '__incoming/' and settings['catalog_prefix'] == ''
    assert settings['delete_incoming_after_ingest'] is True and settings['store_git_push'] is False
    yaml_cfg = {'ingest_watch': {'interval_seconds': 60, 'prefix': 'x', 'notify': {'webhook_url': 'http://h'}}}
    settings = config.get_ingest_watch_settings(yaml_cfg)
    assert settings['interval_seconds'] == 60 and settings['prefix'] == 'x/'
    assert settings['notify']['webhook_url'] == 'http://h' and settings['notify']['smtp_port'] == 25
    monkeypatch.setenv('ANTHIVE_INGEST_WATCH_INTERVAL_SECONDS', '120')
    monkeypatch.setenv('ANTHIVE_INGEST_WATCH_DELETE_INCOMING_AFTER_INGEST', 'false')
    monkeypatch.setenv('ANTHIVE_INGEST_WATCH_POST_STEPS', 'score,manifest')
    settings = config.get_ingest_watch_settings(yaml_cfg)
    assert settings['interval_seconds'] == 120 and settings['delete_incoming_after_ingest'] is False
    assert settings['post_steps'] == ['score', 'manifest']


def test_default_yaml_has_the_block(anthive_root):
    defaults = yaml.safe_load((anthive_root / 'anthive.default.yaml').read_text())
    assert set(defaults['ingest_watch']) == set(config.INGEST_WATCH_DEFAULTS)
    assert defaults['ingest_watch']['enabled'] is False
    assert defaults['ingest_watch']['delete_incoming_after_ingest'] is True


# ── md_edit bumps ───────────────────────────────────────────────────────────

MD = ('---\ntitle: T\nid: ABC\nversion: 0.1.7\nupdated: \'2026-01-01\'\nsha256: aa\ncaveats:\n- level: low\n'
      '  message: keep me\napi:\n  obs_rename:\n    BrNum: ant_donor\nchangelog:\n- version: 0.1.7\n'
      '  date: \'2026-01-01\'\n  changes:\n  - old\n---\n\n# Body\n')


def test_minor_bump_with_previous_sha():
    text = md_edit.apply_edits(MD, [md_edit.bump_version(['new data'], '2026-10-14', part='minor',
                                                         previous_sha256='aa')])
    meta = yaml.safe_load(text.split('---\n')[1])
    assert meta['version'] == '0.2.0' and meta['updated'] == '2026-10-14'
    assert meta['changelog'][-1] == {'version': '0.2.0', 'date': '2026-10-14', 'previous_sha256': 'aa',
                                     'changes': ['new data']}
    assert text.endswith('# Body\n')


def test_patch_bump_is_unchanged_behaviour():
    meta = yaml.safe_load(md_edit.apply_edits(MD, [md_edit.bump_version(['x'], '2026-10-14')]).split('---\n')[1])
    assert meta['version'] == '0.1.8' and 'previous_sha256' not in meta['changelog'][-1]


def test_append_top_keeps_existing_items():
    text = md_edit.apply_edits(MD, [md_edit.append_top('caveats', {'level': 'low', 'message': 'new'})])
    meta = yaml.safe_load(text.split('---\n')[1])
    assert [caveat['message'] for caveat in meta['caveats']] == ['keep me', 'new']


# ── catalogue fill ──────────────────────────────────────────────────────────

def test_fill_never_overwrites_curator_keys(tmp_path):
    h5ad = make_h5ad(tmp_path / 'x.h5ad')
    profile = md_fill.profile_h5ad(h5ad)
    curated = {'title': 'Mine', 'id': 'ZZZ', 'obscat_include': ['donor'], 'obsnum_include': [], 'version': '2.0.0',
               'changelog': [{'version': '2.0.0', 'date': 'd', 'changes': ['c']}], 'sha256': 'stale'}
    meta, filled = md_fill.fill_catalog_fields(curated, profile, 'f' * 64, {'title': 'Sidecar title'}, 'x', 'G01',
                                               set(), '2026-10-14')
    assert meta['title'] == 'Mine' and meta['id'] == 'ZZZ' and meta['version'] == '2.0.0'
    assert meta['changelog'] == curated['changelog']
    assert meta['obsnum_include'] == ['n_counts']                  # empty list counts as unset
    assert meta['sha256'] == 'f' * 64 and meta['n_obs'] == 6       # facts of the file always follow it
    assert 'title' not in filled and 'sha256' in filled


def test_fill_new_dataset_defaults(tmp_path):
    h5ad = make_h5ad(tmp_path / 'my_micro_data.h5ad')
    profile = md_fill.profile_h5ad(h5ad)
    meta, _ = md_fill.fill_catalog_fields({}, profile, sha_of(h5ad), {}, 'my_micro_data', 'G01', {'G01/MMD'},
                                          '2026-10-14', md_fill.ingest_block('s3-watch', 'k', 'e', None, 'd'))
    assert meta['version'] == '1.0.0' and meta['changelog'][0]['sha256'] == sha_of(h5ad)
    assert meta['id'] != 'MMD' and len(meta['id']) == 3          # the generated id is free in the group
    assert meta['layers_include'] == ['X', 'counts'] and meta['obsm_include'] == {'X_umap': 'X_umap'}
    assert meta['ingest']['route'] == 's3-watch'


def test_layer_stats_match_a_full_computation(tmp_path):
    import anndata
    from layer_stats import calculate_layer_statistics

    h5ad = make_h5ad(tmp_path / 'x.h5ad', n_cells=40)
    profile = md_fill.profile_h5ad(h5ad)
    adata = anndata.read_h5ad(h5ad)
    for name, matrix in (('X', adata.X), ('counts', adata.layers['counts'])):
        expected = calculate_layer_statistics(matrix, name)
        for key in ('min_nonzero', 'max_value', 'median', 'q90', 'num_zeros'):
            assert profile.layer_stats[name][key] == pytest.approx(expected[key]), (name, key)


def test_sidecar_merge_store_wins_override_and_guarded_keys():
    merge = md_fill.merge_sidecar_store_wins(
        '# local\norganism=human\ntissue=brain\ncell_type=microglia\nlocal_only=1\ns3_etag=old\naccess=internal\n',
        'organism=human\ntissue=cortex\ncell_type=astrocyte\noverride_cell_type=true\nnew_key=v\ns3_etag=new\n'
        'access=public\n')
    assert 'tissue=brain' in merge.text and 'cell_type=astrocyte' in merge.text
    assert 'local_only=1' in merge.text and '# local' in merge.text
    assert 's3_etag=old' in merge.text                     # the deposit's s3_* stamps never replace the store's
    assert 'override_' not in merge.text and 'access=internal' in merge.text
    assert merge.conflicts == [('tissue', 'brain', 'cortex')]
    assert merge.overridden == [('cell_type', 'microglia', 'astrocyte')]
    assert merge.adopted == [('new_key', '', 'v')]
    assert merge.guarded == [('access', 'internal', 'public')]


def test_seed_md_delta():
    store = {'title': 'T', 'caveats': [{'message': 'old'}], 'version': '1.0.0'}
    caveats, conflicts = md_fill.seed_md_delta({'title': 'T', 'caveats': [{'message': 'old'}, {'message': 'new'}],
                                                'version': '9'}, store)
    assert caveats == [{'message': 'new'}] and conflicts == []
    assert md_fill.seed_md_delta({'title': 'Other'}, store)[1] == ['title']


def test_plan_update_rules(tmp_path):
    first = md_fill.profile_h5ad(make_h5ad(tmp_path / 'a.h5ad', n_cells=6))
    second = md_fill.profile_h5ad(make_h5ad(tmp_path / 'b.h5ad', n_cells=8, extra_obs='batch'))
    minor = md_fill.plan_update({}, first, second, 'a' * 64, 'b' * 64, [])
    assert minor.part == 'minor' and minor.previous_sha256 == 'a' * 64
    assert any('6 -> 8' in change for change in minor.changes)
    assert any('batch' in change for change in minor.changes)
    patch = md_fill.plan_update({}, None, None, 'a' * 64, 'a' * 64, [('tissue', 'x', 'y')])
    assert patch.part == 'patch' and patch.previous_sha256 is None
    assert md_fill.plan_update({}, None, None, 'a' * 64, 'a' * 64, []).part is None


def test_update_md_text_preserves_caveats_and_mapping():
    plan = md_fill.UpdatePlan('minor', ['New data.'], 'aa', [])
    text = md_fill.update_md_text(MD, plan, 'bb', None, '2026-10-14',
                                  caveat=md_fill.curation_caveat('updated', 'dataset_update', '2026-10-14'))
    meta = yaml.safe_load(text.split('---\n')[1])
    assert meta['sha256'] == 'bb' and meta['version'] == '0.2.0'
    assert meta['caveats'][0]['message'] == 'keep me' and meta['caveats'][1]['topic'] == 'dataset_update'
    assert meta['api'] == {'obs_rename': {'BrNum': 'ant_donor'}}
    unchanged_lines = [line for line in MD.splitlines() if line.startswith(('title', 'id:', '  message: keep'))]
    assert all(line in text.splitlines() for line in unchanged_lines)


# ── archived versions and purge ─────────────────────────────────────────────

def _dataset_with_versions(tmp_path: Path, versions) -> tuple:
    group = tmp_path / 'store' / 'G'
    group.mkdir(parents=True)
    (group / 'index.md').write_text('---\ngroup: G\ngroup_id: G01\n---\n')
    (group / 'd.md').write_text('---\nid: DDD\nversion: 9.0.0\n---\n')
    (group / 'd.h5ad').write_bytes(b'current')
    s3 = FakeS3()
    s3.put('G/d.h5ad', b'current')
    s3.put('G/other.h5ad', b'unrelated')
    s3.put('G/_versions/other/1.0.0/other.h5ad', b'unrelated archive')
    for version in versions:
        folder = group / '_versions' / 'd' / version
        folder.mkdir(parents=True)
        (folder / 'd.h5ad').write_bytes(version.encode())
        s3.put(f'G/_versions/d/{version}/d.h5ad', version.encode())
        s3.put(f'G/_versions/d/{version}/d.h5ad.mus', b'm')
    return group, s3


def test_archive_plan_links_and_never_moves(tmp_path):
    group = tmp_path / 'G'
    group.mkdir()
    for suffix in ('.h5ad', '.md', '.h5ad.mus', '.scores.parquet', '.de.parquet', '.duckdb'):
        (group / f'd{suffix}').write_text(suffix)
    links, remove_after = ingest_versions.archive_plan(group, 'd', '1.0.0')
    assert {destination.name for _, destination in links} == {'d.h5ad', 'd.md', 'd.h5ad.mus', 'd.scores.parquet',
                                                              'd.de.parquet'}
    assert remove_after == [group / 'd.scores.parquet']
    assert not (group / '_versions').exists()                      # planning touches nothing
    archived = ingest_versions.archive_local(group, 'd', '1.0.0')
    folder = group / '_versions' / 'd' / '1.0.0'
    assert all((group / f'd{suffix}').exists() for suffix in ('.h5ad', '.md', '.h5ad.mus', '.scores.parquet'))
    assert (folder / 'd.h5ad').read_text() == '.h5ad' and len(archived) == 5
    assert (folder / 'd.h5ad').stat().st_ino == (group / 'd.h5ad').stat().st_ino     # a hard link
    with pytest.raises(FileExistsError):
        ingest_versions.archive_plan(group, 'd', '1.0.0')


def test_archive_plan_metadata_only_keeps_the_scores(tmp_path):
    group = tmp_path / 'G'
    group.mkdir()
    for suffix in ('.h5ad', '.md', '.h5ad.mus', '.scores.parquet'):
        (group / f'd{suffix}').write_text(suffix)
    _, remove_after = ingest_versions.archive_plan(group, 'd', '1.0.0', data_changed=False)
    assert remove_after == []


def test_archived_versions_are_skipped_by_scan_sync_and_md_check(tmp_path):
    group, _ = _dataset_with_versions(tmp_path, ['1.0.0'])
    (group / '_versions' / 'd' / '1.0.0' / 'd.md').write_text('---\nid: DDD\nversion: 1.0.0\n---\n')
    found = ingest_versions.find_datasets(tmp_path / 'store')
    assert [entry['db_id'] for entry in found] == ['G01/DDD']
    from catalog_manifest import build_manifest
    manifest = build_manifest(tmp_path / 'store')
    assert len(manifest['data_projects']['G']['datasets']) == 1
    sync_source = (ROOT / 'bin' / 'ops' / 'ant-sync').read_text()
    assert "if any(part.startswith('_') for part in path.parts[:-1]):" in sync_source
    md_check_source = (ROOT / 'lib' / 'md_check.py').read_text()
    assert "if any(part.startswith('_') for part in md_path.relative_to(folder).parts[:-1]):" in md_check_source
    serve_source = (ROOT / 'bin' / 'api' / 'ant-serve').read_text()
    assert "if item.name.startswith('_'):" in serve_source


@pytest.mark.parametrize('keep, remaining', [(1, []), (2, ['3.0.0']), (3, ['2.0.0', '3.0.0']), (9, ['1.0.0', '2.0.0', '3.0.0'])])
def test_purge_keeps_exactly_n_and_touches_only_version_keys(tmp_path, keep, remaining):
    group, s3 = _dataset_with_versions(tmp_path, ['1.0.0', '3.0.0', '2.0.0'])
    plan = ingest_versions.plan_purge('G01/DDD', group, 'G', 'd', '9.0.0', keep, s3=s3, bucket=BUCKET)
    ingest_versions.purge(plan, s3=s3, bucket=BUCKET)
    assert ingest_versions.list_local_versions(group, 'd') == remaining
    assert sorted(ingest_versions.list_s3_versions(s3, BUCKET, '', 'G', 'd')) == remaining
    assert all('/_versions/d/' in key for key in s3.deleted)
    assert 'G/d.h5ad' in s3.objects and 'G/other.h5ad' in s3.objects
    assert 'G/_versions/other/1.0.0/other.h5ad' in s3.objects
    assert (group / 'd.h5ad').exists()


def test_purge_plan_alone_deletes_nothing(tmp_path):
    group, s3 = _dataset_with_versions(tmp_path, ['1.0.0'])
    before = dict(s3.objects)
    plan = ingest_versions.plan_purge('G01/DDD', group, 'G', 'd', '9.0.0', 1, s3=s3, bucket=BUCKET)
    assert plan.remove_versions == ['1.0.0'] and plan.s3_keys and plan.local_dirs
    assert s3.objects == before and not s3.deleted
    assert (group / '_versions' / 'd' / '1.0.0').exists()


def test_purge_refuses_keep_zero(tmp_path):
    group, s3 = _dataset_with_versions(tmp_path, ['1.0.0'])
    with pytest.raises(ValueError):
        ingest_versions.plan_purge('G01/DDD', group, 'G', 'd', '9.0.0', 0, s3=s3, bucket=BUCKET)


def test_archive_s3_copies_to_the_version_prefix(tmp_path):
    group = tmp_path / 'G'
    folder = group / '_versions' / 'd' / '1.0.0'
    folder.mkdir(parents=True)
    (folder / 'd.md').write_text('md')
    (folder / 'd.h5ad.mus').write_text('mus')
    s3 = FakeS3()
    s3.put('cat/G/d.h5ad', b'h5ad')
    written = ingest_versions.archive_s3(s3, BUCKET, 'cat/', 'G', 'd', '1.0.0',
                                         [folder / 'd.md', folder / 'd.h5ad.mus'])
    assert set(written) == {'cat/G/_versions/d/1.0.0/d.h5ad', 'cat/G/_versions/d/1.0.0/d.h5ad.mus',
                            'cat/G/_versions/d/1.0.0/d.md'}
    assert 'cat/G/d.h5ad' in s3.objects
