"""Purge policy of archived dataset versions (lib/ingest_versions.py, design section 25).

Local copies go once their S3 copy is verified; on S3 a predecessor of a verified patch or minor version can go,
a major predecessor stays; versions an artefact's made_from or a facet result names stay without --force.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

import ingest_versions as iv
from tests.ingest_watch_helpers import FakeS3

SHA = {'1.0.0': 'a' * 64, '1.1.0': 'b' * 64, '2.0.0': 'c' * 64}


def _md(version: str, sha: str, changelog=None) -> str:
    meta = {'id': 'ds', 'version': version, 'sha256': sha, 'changelog': changelog or []}
    return '---\n' + yaml.safe_dump(meta, sort_keys=False) + '---\n'


CHANGELOG = [
    {'version': '1.0.0', 'date': '2026-10-01', 'changes': ['first']},
    {'version': '1.1.0', 'date': '2026-10-02', 'previous_sha256': SHA['1.0.0'],
     'changes': ['New data', 'Version class minor: additive: ... added obs/batch.']},
    {'version': '2.0.0', 'date': '2026-10-03', 'previous_sha256': SHA['1.1.0'],
     'changes': ['New data', 'Version class major: data altered: X/data differs.']},
]


def _store(tmp_path: Path, s3: FakeS3, mismatch: str = '', legacy: bool = False):
    folder = tmp_path / 'store' / 'G'
    folder.mkdir(parents=True)
    changelog = [dict(entry) for entry in CHANGELOG]
    if legacy:                                      # a minor bump from before the classifier: no class line
        changelog[1]['changes'] = ['New data from an S3 deposit']
    (folder / 'ds.md').write_text(_md('2.0.0', SHA['2.0.0'], changelog))
    s3.put('G/ds.h5ad', b'current', metadata={'sha256': SHA['2.0.0']})
    for version in ('1.0.0', '1.1.0'):
        local = folder / '_versions' / 'ds' / version
        local.mkdir(parents=True)
        (local / 'ds.md').write_text(_md(version, SHA[version]))
        (local / 'ds.h5ad').write_bytes(version.encode())
        prefix = f'G/_versions/ds/{version}/'
        s3.put(prefix + 'ds.md', _md(version, SHA[version]).encode())
        s3.put(prefix + 'ds.h5ad', version.encode(),
               metadata={'sha256': 'f' * 64 if version == mismatch else SHA[version]})
    return {'db_id': 'G1/ds', 'dataset_dir': folder, 'rel_dir': 'G', 'basename': 'ds', 'version': '2.0.0'}


def _meta(entry):
    return yaml.safe_load((entry['dataset_dir'] / 'ds.md').read_text().split('---\n')[1])


NO_REFS = {'made_from': {}, 'made_from_sha': {}, 'facet': {}}


def test_local_copies_go_only_when_their_s3_copy_is_verified(tmp_path):
    s3 = FakeS3()
    entry = _store(tmp_path, s3, mismatch='1.1.0')
    plan = iv.plan_local_verified(entry, s3, 'b', '')
    assert plan.remove_versions == ['1.0.0'] and plan.s3_keys == []
    assert 'expected' in plan.blocked['1.1.0'][0]
    iv.purge(plan, s3=s3, bucket='b')
    assert not (entry['dataset_dir'] / '_versions/ds/1.0.0').exists()
    assert (entry['dataset_dir'] / '_versions/ds/1.1.0').exists()
    assert 'G/_versions/ds/1.0.0/ds.h5ad' in s3.objects                       # S3 untouched


def test_superseded_minor_removes_predecessors_of_verified_minor_versions_only(tmp_path):
    s3 = FakeS3()
    entry = _store(tmp_path, s3)
    plan = iv.plan_superseded(entry, _meta(entry), s3, 'b', '', NO_REFS)
    assert plan.remove_versions == ['1.0.0']                                 # its successor 1.1.0 is minor
    assert plan.blocked == {'1.1.0': ['its successor 2.0.0 is major']}       # a major predecessor stays
    assert sorted(plan.s3_keys) == ['G/_versions/ds/1.0.0/ds.h5ad', 'G/_versions/ds/1.0.0/ds.md']
    iv.purge(plan, s3=s3, bucket='b')
    assert 'G/_versions/ds/1.0.0/ds.h5ad' not in s3.objects and 'G/_versions/ds/1.1.0/ds.h5ad' in s3.objects


def test_an_unverified_successor_or_a_legacy_minor_bump_keeps_the_predecessor(tmp_path):
    s3 = FakeS3()
    entry = _store(tmp_path, s3, mismatch='1.1.0')
    plan = iv.plan_superseded(entry, _meta(entry), s3, 'b', '', NO_REFS)
    assert plan.remove_versions == [] and 'not verified on S3' in plan.blocked['1.0.0'][0]
    s3 = FakeS3()
    entry = _store(tmp_path / 'legacy', s3, legacy=True)
    plan = iv.plan_superseded(entry, _meta(entry), s3, 'b', '', NO_REFS)
    assert plan.remove_versions == [] and plan.blocked['1.0.0'] == ['its successor 1.1.0 is unknown']


def test_versions_others_point_at_stay_without_force(tmp_path):
    s3 = FakeS3()
    entry = _store(tmp_path, s3)
    store = tmp_path / 'store'
    manifest = store / 'G' / 'static' / 'artefacts' / 'SLM' / 'figure' / 'umap' / 'v1' / 'manifest.json'
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({'ref': 'G1/SLM/umap@v1',
                                    'made_from': [{'db_id': 'g1/ds', 'dataset_version': '1.0.0'}]}))
    facet = store / '_facet_tests' / 'G' / 'ds.facet_manifest.json'
    facet.parent.mkdir(parents=True)
    facet.write_text(json.dumps({'dataset_sha256': SHA['1.1.0']}))
    refs = iv.version_references(store)
    plan = iv.plan_superseded(entry, _meta(entry), s3, 'b', '', refs)
    assert plan.remove_versions == [] and plan.blocked['1.0.0'] == ['artefact G1/SLM/umap@v1 was made from it']
    forced = iv.plan_superseded(entry, _meta(entry), s3, 'b', '', refs, force=True)
    assert forced.remove_versions == ['1.0.0'] and forced.references['1.0.0']
    keep = iv.plan_purge('G1/ds', entry['dataset_dir'], 'G', 'ds', '2.0.0', 1, s3=s3, bucket='b')
    assert keep.remove_versions == ['1.0.0', '1.1.0']
    iv.protect_plan(keep, refs, s3=s3, bucket='b')
    assert keep.remove_versions == [] and keep.local_dirs == [] and keep.s3_keys == []
    assert keep.blocked['1.1.0'] == ['facet result G/ds.facet_manifest.json recorded it']


def test_the_watcher_can_drop_the_local_copy_right_after_a_verified_s3_archive(tmp_path):
    from tests.test_ingest_watch import Env, _live_v1
    env = Env(tmp_path)
    env.pipeline.config.keep_local_versions = False
    _live_v1(env)
    env.deposit(seed=1, with_seeds=False)
    env.clock_value += 10
    assert env.poll_twice() == {'live': 1}
    group = env.store / 'WatchTest2026'
    assert not (group / '_versions' / 'micro' / '1.0.0').exists()
    assert any(key.endswith('_versions/micro/1.0.0/micro.h5ad') for key in env.s3.objects)


def test_a_plain_local_only_purge_checks_s3_first(tmp_path):
    s3 = FakeS3()
    entry = _store(tmp_path, s3, mismatch='1.0.0')
    plan = iv.plan_purge('G1/ds', entry['dataset_dir'], 'G', 'ds', '2.0.0', 1)
    assert plan.remove_versions == ['1.0.0', '1.1.0']
    iv.verify_local_plan(plan, s3, 'b', '')
    assert plan.remove_versions == ['1.1.0'] and plan.s3_keys == [] and 'expected' in plan.blocked['1.0.0'][0]
    assert [folder.name for folder in plan.local_dirs] == ['1.1.0']
