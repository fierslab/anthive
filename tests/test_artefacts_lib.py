"""lib/artefacts.py (artefact store, phase A1 of doc/DESIGN_artefacts.md): names, layout, references, head
records, manifests, the collection registry, the artefact index, access, signed URLs and local copies.

The signed-URL tests use real boto3 presigning (offline) and check the URL with an independent SigV4
verifier (tests/artefact_helpers.verify_sigv4_query), the same computation the object store does: a URL
must open exactly one key, with GET only, until it expires.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
import threading
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlsplit, urlunsplit

import pytest

import artefacts
import config
import md_check
from tests.artefact_helpers import (S3Down, StrictFakeS3, manifest_for, verify_sigv4_query, write_artefact,
                                    write_dataset_md, write_index_md)

GID = 'PU1'
FOLDER = 'Public2021'


# ── names and paths ───────────────────────────────────────────────────────────

@pytest.mark.parametrize('name', ['volcano.pdf', 'a', 'panels/a.png', 'A-1_b.c.tsv.gz', 'x' * 120,
                                  'panels/' + 'y' * 120])
def test_good_file_names(name):
    assert artefacts.file_name_problem(name) is None


@pytest.mark.parametrize('name', [
    '', '..', '.', '../etc/passwd', 'a/../../b', 'panels/../a.png', '/abs.png', 'a//b', 'a/', 'a\\b.png',
    '.hidden', '_private.png', 'panels/.git', 'panels/_x.png', 'a/b/c.png', 'x' * 121, 'café.png',
    'café.png', 'a b.png', 'a\x00b', 'naïve/a.png', '‥/a', 'a%2F..%2Fb', 'a?b', 'a#b', 'a"b', "a'b",
    'a\nb', '~/x', '*.png', None, 3,
])
def test_bad_file_names(name):
    assert artefacts.file_name_problem(name) is not None


def test_case_clashes():
    assert artefacts.casefold_clashes(['A.png', 'b.png', 'a.PNG']) == ['a.PNG']
    assert artefacts.casefold_clashes(['a.png', 'b.png']) == []


@pytest.mark.parametrize('artefact_id, ok', [('volcano-tvsc', True), ('ab', True), ('a', False), ('-ab', False),
                                             ('Ab', False), ('a_b', False), ('a' * 64, True), ('a' * 65, False),
                                             ('ab\n', False), ('éa', False), ('a.b', False)])
def test_artefact_ids(artefact_id, ok):
    assert artefacts.valid_artefact_id(artefact_id) is ok


@pytest.mark.parametrize('scope, ok', [('SLM', True), ('_group', True), ('_other', False), ('.x', False),
                                       ('..', False), ('a/b', False), ('x' * 64, True), ('x' * 65, False),
                                       ('SÉM', False), ('', False)])
def test_scopes(scope, ok):
    assert artefacts.valid_scope(scope) is ok


def test_segment_rule_equals_the_pipeline():
    import ingest_pipeline
    assert artefacts.SEGMENT_RE.pattern == ingest_pipeline.SEGMENT_RE.pattern
    import dataset_deposit
    assert artefacts.DATA_PROJECT_RE.pattern == dataset_deposit.NEW_GROUP_RE.pattern
    assert artefacts.DB_ID_RE.pattern == dataset_deposit.DB_ID_RE.pattern


def test_layout_and_keys():
    path = artefacts.ArtefactPath('Sun2026', 'SLM', 'figure', 'volcano-tvsc')
    assert str(path.head_rel) == 'Sun2026/static/artefacts/SLM/figure/volcano-tvsc/artefact.md'
    assert str(path.manifest_rel(2)) == 'Sun2026/static/artefacts/SLM/figure/volcano-tvsc/v2/manifest.json'
    assert str(path.file_rel(2, 'panels/a.png')) == \
        'Sun2026/static/artefacts/SLM/figure/volcano-tvsc/v2/files/panels/a.png'
    assert str(path.preview_rel(1, 'a.png.webp')).endswith('/v1/preview/a.png.webp')
    assert artefacts.s3_key('', path.file_rel(1, 'a.png')) == str(path.file_rel(1, 'a.png'))
    assert artefacts.s3_key('cat/', path.head_rel) == f'cat/{path.head_rel}'
    assert artefacts.s3_key('/cat', path.head_rel) == f'cat/{path.head_rel}'
    group = artefacts.ArtefactPath('Fiers2026', '_group', 'table', 'umap-atlas')
    assert str(group.rel) == 'Fiers2026/static/artefacts/_group/table/umap-atlas'


@pytest.mark.parametrize('parts', [
    ('..', 'SLM', 'figure', 'ab'), ('_Sun2026', 'SLM', 'figure', 'ab'), ('Sun2026', '..', 'figure', 'ab'),
    ('Sun2026', 'SLM', 'video', 'ab'), ('Sun2026', 'SLM', 'figure', '../ab'), ('Sun/2026', 'SLM', 'figure', 'ab'),
    ('Sun2026', '_groups', 'figure', 'ab'), ('Sün2026', 'SLM', 'figure', 'ab'),
])
def test_layout_refuses_bad_parts(parts):
    with pytest.raises(ValueError):
        artefacts.ArtefactPath(*parts)


@pytest.mark.parametrize('version', [0, -1, 1000000, True, '1', 1.0, None])
def test_layout_refuses_bad_versions(version):
    path = artefacts.ArtefactPath('Sun2026', 'SLM', 'figure', 'ab')
    with pytest.raises(ValueError):
        path.version_rel(version)


@pytest.mark.parametrize('name', ['../x', '_x', 'a/b/c', '.x', ''])
def test_layout_refuses_bad_file_names(name):
    path = artefacts.ArtefactPath('Sun2026', 'SLM', 'figure', 'ab')
    with pytest.raises(ValueError):
        path.file_rel(1, name)
    with pytest.raises(ValueError):
        path.preview_rel(1, name)


def test_references():
    ref = artefacts.parse_ref('S26/SLM/volcano-tvsc@v2')
    assert (ref.group_id, ref.scope, ref.artefact_id, ref.version) == ('S26', 'SLM', 'volcano-tvsc', 2)
    assert str(ref) == 'S26/SLM/volcano-tvsc@v2'
    assert artefacts.parse_ref('F26/_group/umap-atlas').version is None
    assert artefacts.parse_ref('F26/_group/umap-atlas@latest').version is None
    for bad in ('S26/SLM', 'S26/SLM/x', 'S26/SLM/ab@v0', 'S26/SLM/ab@2', 'S2/SLM/ab', 'S26/../ab',
                'S26/SLM/ab/c', 'S26/SLM/ab@v1@v2', '', None, 'S26/_x/ab'):
        with pytest.raises(ValueError):
            artefacts.parse_ref(bad)
    assert artefacts.parse_version_label('v12') == 12
    for bad in ('v0', 'V1', '1', 'v01', 'v1 ', 'v1234567', 'latest2', ''):
        with pytest.raises(ValueError):
            artefacts.parse_version_label(bad)


# ── head record and manifest ──────────────────────────────────────────────────

def _head(**changes):
    head = {'artefact_id': 'ab', 'kind': 'figure', 'scope': 'SLM', 'owner': 'jdoe', 'created': '2026-10-14',
            'title': 'T', 'latest': 2,
            'versions': [{'version': 1, 'state': 'purged', 'manifest_sha256': 'a' * 64, 'published': '2026-10-14',
                          'purged': '2026-10-16', 'purged_by': 'jdoe', 'purge_reason': 'wrong'},
                         {'version': 2, 'state': 'live', 'manifest_sha256': 'b' * 64, 'published': '2026-10-16'}]}
    head.update(changes)
    return head


def test_head_record_round_trip_and_dates():
    text = artefacts.render_head_record(_head())
    assert text.startswith('---\n') and text.endswith('---\n')
    parsed = artefacts.parse_head_record(text)
    assert parsed == _head()
    unquoted = text.replace("'2026-10-14'", '2026-10-14')
    assert artefacts.parse_head_record(unquoted)['created'] == '2026-10-14'     # YAML dates come back as text
    path = artefacts.ArtefactPath('Sun2026', 'SLM', 'figure', 'ab')
    assert artefacts.validate_head_record(parsed, path) == []


@pytest.mark.parametrize('changes, expected', [
    ({'artefact_id': 'other'}, 'artefact_id'), ({'kind': 'table'}, 'kind'), ({'scope': '_group'}, 'scope'),
    ({'owner': ''}, 'owner'), ({'latest': 1}, 'latest'), ({'latest': None}, 'latest'), ({'versions': []}, 'versions'),
    ({'versions': [{'version': 1, 'state': 'live', 'manifest_sha256': 'x', 'published': 'd'}], 'latest': 1},
     'manifest_sha256'),
    ({'versions': [{'version': 1, 'state': 'gone', 'manifest_sha256': 'a' * 64, 'published': 'd'}], 'latest': None},
     'state'),
    ({'versions': [{'version': 1, 'state': 'live', 'manifest_sha256': 'a' * 64, 'published': 'd'},
                   {'version': 1, 'state': 'live', 'manifest_sha256': 'a' * 64, 'published': 'd'}], 'latest': 1},
     'twice'),
    ({'versions': [{'version': 1, 'state': 'purged', 'manifest_sha256': 'a' * 64, 'published': 'd'}],
      'latest': None}, 'purged without'),
    ({'versions': [{'version': True, 'state': 'live', 'manifest_sha256': 'a' * 64, 'published': 'd'}],
      'latest': 1}, 'invalid version'),
])
def test_head_record_problems(changes, expected):
    path = artefacts.ArtefactPath('Sun2026', 'SLM', 'figure', 'ab')
    problems = artefacts.validate_head_record(_head(**changes), path)
    assert any(expected in problem for problem in problems), problems


def test_front_matter_must_be_closed_and_a_mapping():
    for text in ('no front matter', '---\na: 1\n', '---\n- a\n---\n', '---\na: 1\n---x\n'):
        with pytest.raises(ValueError):
            artefacts.parse_front_matter(text)
    assert artefacts.parse_front_matter('---\na: "x\\n---y"\n---\nbody') == {'a': 'x\n---y'}


def _manifest(**overrides):
    manifest = manifest_for('Sun2026', 'S26', 'SLM', 'figure', 'volcano', 2, {'v.pdf': b'%PDF', 'v.png': b'png'})
    manifest.update(overrides)
    return manifest


def test_valid_manifest():
    path = artefacts.ArtefactPath('Sun2026', 'SLM', 'figure', 'volcano')
    assert artefacts.validate_manifest(_manifest(), path, 'S26', 2) == []
    group_path = artefacts.ArtefactPath('Fiers2026', '_group', 'table', 'atlas')
    manifest = manifest_for('Fiers2026', 'F26', '_group', 'table', 'atlas', 1, {'a.tsv': b'x'})
    assert manifest['db_id'] is None and manifest['made_from'] == []
    assert artefacts.validate_manifest(manifest, group_path, 'F26', 1) == []


@pytest.mark.parametrize('overrides, expected', [
    ({'schema': 'anthive.artefact/2'}, 'schema'),
    ({'ref': 'S26/SLM/volcano@v1'}, 'ref'), ({'version': 1}, 'version'), ({'version': True}, 'version'),
    ({'kind': 'table'}, 'kind'), ({'group': 'Other2026'}, 'group'), ({'group_id': 'X26'}, 'group_id'),
    ({'scope': 'ABC'}, 'scope'), ({'db_id': 'S26/ABC'}, 'db_id'), ({'artefact_id': 'x'}, 'artefact_id'),
    ({'title': ''}, 'title'), ({'title': 'x' * 201}, 'title'), ({'description': 'x' * 30000}, 'description'),
    ({'access': 'public'}, 'access'), ({'access': None}, 'access'), ({'supersedes': 2}, 'supersedes'),
    ({'files': []}, 'files'),
    ({'files': [{'name': '../x', 'size': 1, 'sha256': 'a' * 64, 'content_type': 'image/png', 'role': 'main'}]},
     '../x'),
    ({'files': [{'name': 'a.png', 'size': 1, 'sha256': 'A' * 64, 'content_type': 'image/png', 'role': 'main'}]},
     'sha256'),
    ({'files': [{'name': 'a.png', 'size': -1, 'sha256': 'a' * 64, 'content_type': 'image/png', 'role': 'main'}]},
     'size'),
    ({'files': [{'name': 'a.png', 'size': 1, 'sha256': 'a' * 64, 'content_type': 'text/html; x', 'role': 'main'}]},
     'content_type'),
    ({'files': [{'name': 'a.png', 'size': 1, 'sha256': 'a' * 64, 'content_type': 'image/png', 'role': 'boss'}]},
     'role'),
    ({'files': [{'name': 'a.png', 'size': 1, 'sha256': 'a' * 64, 'content_type': 'image/png', 'role': 'main'},
                {'name': 'A.PNG', 'size': 1, 'sha256': 'a' * 64, 'content_type': 'image/png', 'role': 'main'}]},
     'case'),
    ({'made_from': []}, 'scope dataset'), ({'made_from': [{'db_id': 'no-slash', 'dataset_version': '1'}]}, 'db_id'),
    ({'made_from': [{'db_id': 'S26/SLM', 'dataset_version': '1', 'h5ad_sha256': 'nope'}]}, 'h5ad_sha256'),
    ({'declared': None}, 'declared'), ({'recorded': {'published': 'x'}}, 'uploaded_by'),
    ({'de': {'study_id': 'x'}}, 'de_table'),
])
def test_manifest_problems(overrides, expected):
    path = artefacts.ArtefactPath('Sun2026', 'SLM', 'figure', 'volcano')
    problems = artefacts.validate_manifest(_manifest(**overrides), path, 'S26', 2)
    assert any(expected in problem for problem in problems), problems


def test_manifest_must_be_an_object():
    path = artefacts.ArtefactPath('Sun2026', 'SLM', 'figure', 'volcano')
    assert artefacts.validate_manifest([1], path, 'S26', 2)


# ── collection registry ───────────────────────────────────────────────────────

def _analysis(store, folder='Atlas2026', **changes):
    meta = {'group': folder, 'group_id': 'AT6', 'collection_kind': 'analysis', 'title': 'Atlas analyses',
            'authors': ['Mark Fiers'], 'year': 2026, 'access': 'public', 'created_by': 'mfiers'}
    meta.update(changes)
    meta = {key: value for key, value in meta.items() if value is not None}
    return write_index_md(store, folder, **meta)


def test_registry_with_and_without_datasets(tmp_path):
    write_index_md(tmp_path, FOLDER, group=FOLDER, group_id=GID, default_dataset='pub')
    write_dataset_md(tmp_path, FOLDER, 'pub', 'pub')
    _analysis(tmp_path)
    write_index_md(tmp_path, 'Noindex2020', group='Noindex2020')            # no group_id, no datasets
    (tmp_path / 'Bare2020').mkdir()                                          # datasets without index.md
    (tmp_path / '_staging').mkdir()
    write_index_md(tmp_path, '_staging', group='_staging', group_id='STG')
    registry, problems = artefacts.scan_collections(tmp_path, {FOLDER: {GID}, 'Bare2020': {'BA0'}})
    assert set(registry) == {GID, 'AT6', 'BA0'}
    assert registry['AT6'].kind == 'analysis' and registry['AT6'].access == 'public'
    assert registry['AT6'].title == 'Atlas analyses'
    assert registry[GID].kind == 'datasets' and registry['BA0'].folder == 'Bare2020'
    assert registry['AT6'].description.startswith('Collection notes')


@pytest.mark.parametrize('changes, expected', [
    ({'authors': None}, 'authors'), ({'authors': []}, 'authors'), ({'authors': ['  ']}, 'authors'),
    ({'title': None}, 'title'), ({'group': 'Other2026'}, 'group'), ({'group_id': 'AT66'}, 'group_id'),
    ({'group_id': None}, 'group_id'), ({'access': 'open'}, 'access'),
])
def test_incomplete_analysis_collections_are_left_out(tmp_path, changes, expected):
    _analysis(tmp_path, **changes)
    registry, problems = artefacts.scan_collections(tmp_path, {})
    assert registry == {}
    assert any(expected in problem for problem in problems), problems


def test_analysis_naming_rule_and_unknown_kind(tmp_path):
    _analysis(tmp_path, folder='atlas', group='atlas')
    write_index_md(tmp_path, 'Odd2020', group='Odd2020', group_id='OD0', collection_kind='mixed')
    registry, problems = artefacts.scan_collections(tmp_path, {})
    assert registry == {}
    assert any('naming rule' in p for p in problems) and any("'mixed'" in p for p in problems)


def test_analysis_access_defaults_to_internal(tmp_path):
    _analysis(tmp_path, access=None)
    registry, _ = artefacts.scan_collections(tmp_path, {})
    assert registry['AT6'].access is None
    policy = artefacts.AccessPolicy(registry, {}, {})
    assert policy.collection_public('AT6') is False


def test_duplicate_group_ids_are_left_out(tmp_path):
    write_index_md(tmp_path, 'One2020', group='One2020', group_id='DUP')
    write_index_md(tmp_path, 'Two2020', group='Two2020', group_id='DUP')
    registry, problems = artefacts.scan_collections(tmp_path, {'One2020': {'DUP'}, 'Two2020': {'DUP'}})
    assert registry == {} and any('claimed by' in p for p in problems)


# ── the index ─────────────────────────────────────────────────────────────────

def _store(tmp_path):
    write_index_md(tmp_path, FOLDER, group=FOLDER, group_id=GID)
    write_dataset_md(tmp_path, FOLDER, 'pub', 'pub')
    registry, _ = artefacts.scan_collections(tmp_path, {FOLDER: {GID}})
    return registry


def test_index_reads_valid_artefacts(tmp_path):
    registry = _store(tmp_path)
    write_artefact(tmp_path, FOLDER, GID, 'pub', 'figure', 'volcano',
                   [{'files': {'v.png': b'one'}, 'state': 'purged'}, {'files': {'v.png': b'two', 'p/a.png': b'3'}}])
    index = artefacts.ArtefactIndex.build(tmp_path, registry)
    assert index.problems == []
    entry = index.get(GID, 'pub', 'volcano')
    assert entry.latest == 2 and entry.live_versions == [2] and entry.db_id == f'{GID}/pub'
    assert entry.versions[1].state == 'purged' and entry.versions[1].manifest is None
    assert entry.ref == f'{GID}/pub/volcano'


def test_index_refuses_an_edited_manifest(tmp_path):
    registry = _store(tmp_path)
    made = write_artefact(tmp_path, FOLDER, GID, 'pub', 'figure', 'volcano',
                          [{'files': {'v.png': b'one'}}, {'files': {'v.png': b'two'}}])
    manifest_path = made['root'] / 'v2' / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['files'][0]['sha256'] = hashlib.sha256(b'evil').hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    index = artefacts.ArtefactIndex.build(tmp_path, registry)
    entry = index.get(GID, 'pub', 'volcano')
    assert entry.latest == 1                          # v2 is not served: its manifest differs from the head record
    assert not entry.versions[2].servable
    assert any('differs from the head record' in p for p in index.problems)


def test_index_refuses_a_manifest_that_disagrees_with_its_place(tmp_path):
    registry = _store(tmp_path)
    made = write_artefact(tmp_path, FOLDER, GID, 'pub', 'figure', 'volcano', [{'files': {'v.png': b'one'}}])
    # a manifest copied from another artefact, with the head record's sha256 updated to match
    other = manifest_for(FOLDER, GID, 'pub', 'figure', 'other', 1, {'v.png': b'one'})
    raw = (json.dumps(other) + '\n').encode()
    (made['root'] / 'v1' / 'manifest.json').write_bytes(raw)
    head = dict(made['head'])
    head['versions'] = [dict(head['versions'][0], manifest_sha256=hashlib.sha256(raw).hexdigest())]
    (made['root'] / 'artefact.md').write_text(artefacts.render_head_record(head))
    index = artefacts.ArtefactIndex.build(tmp_path, registry)
    assert index.get(GID, 'pub', 'volcano').latest is None
    assert any('artefact_id' in p for p in index.problems)


def test_index_skips_bad_folders(tmp_path):
    registry = _store(tmp_path)
    base = tmp_path / FOLDER / 'static' / 'artefacts'
    for rel in ('_other/figure/ab', 'pub/video/ab', 'pub/figure/Bad_Id', '.hidden/figure/ab'):
        (base / rel).mkdir(parents=True)
        (base / rel / 'artefact.md').write_text('---\nartefact_id: x\n---\n')
    (base / 'pub' / 'figure' / 'nohead').mkdir(parents=True)
    index = artefacts.ArtefactIndex.build(tmp_path, registry)
    assert len(index) == 0 and len(index.problems) >= 4


def test_index_refuses_symlinks_out_of_the_store(tmp_path):
    registry = _store(tmp_path)
    outside = tmp_path / 'outside'
    made = write_artefact(outside, FOLDER, GID, 'pub', 'figure', 'volcano', [{'files': {'v.png': b'one'}}])
    target = tmp_path / FOLDER / 'static' / 'artefacts' / 'pub' / 'figure'
    target.mkdir(parents=True)
    os.symlink(made['root'], target / 'volcano')
    index = artefacts.ArtefactIndex.build(tmp_path, registry)
    assert index.get(GID, 'pub', 'volcano') is None
    assert any('inside the artefacts folder' in p for p in index.problems)


def test_whole_group_symlinked_to_another_store_still_works(tmp_path):
    other_store = tmp_path / 'main'
    write_artefact(other_store, FOLDER, GID, 'pub', 'figure', 'volcano', [{'files': {'v.png': b'one'}}])
    write_index_md(other_store, FOLDER, group=FOLDER, group_id=GID)
    viewer_store = tmp_path / 'viewer'
    viewer_store.mkdir()
    os.symlink(other_store / FOLDER, viewer_store / FOLDER)          # `make symlink TARGET=<Group>`
    registry, _ = artefacts.scan_collections(viewer_store, {FOLDER: {GID}})
    index = artefacts.ArtefactIndex.build(viewer_store, registry)
    assert index.get(GID, 'pub', 'volcano').latest == 1


def test_one_id_in_two_kinds_is_left_out(tmp_path):
    registry = _store(tmp_path)
    write_artefact(tmp_path, FOLDER, GID, 'pub', 'figure', 'volcano', [{'files': {'v.png': b'one'}}])
    write_artefact(tmp_path, FOLDER, GID, 'pub', 'table', 'volcano', [{'files': {'v.tsv': b'one'}}])
    write_artefact(tmp_path, FOLDER, GID, 'pub', 'table', 'other', [{'files': {'v.tsv': b'one'}}])
    index = artefacts.ArtefactIndex.build(tmp_path, registry)
    assert index.get(GID, 'pub', 'volcano') is None and index.get(GID, 'pub', 'other') is not None


def test_oversized_and_broken_records(tmp_path):
    registry = _store(tmp_path)
    made = write_artefact(tmp_path, FOLDER, GID, 'pub', 'figure', 'big', [{'files': {'v.png': b'one'}}])
    (made['root'] / 'artefact.md').write_bytes(b'---\n' + b'# x\n' * 300000 + b'---\n')
    made = write_artefact(tmp_path, FOLDER, GID, 'pub', 'figure', 'broken', [{'files': {'v.png': b'one'}}])
    (made['root'] / 'artefact.md').write_bytes(b'---\nartefact_id: [\n---\n')
    made = write_artefact(tmp_path, FOLDER, GID, 'pub', 'figure', 'binary', [{'files': {'v.png': b'one'}}])
    (made['root'] / 'artefact.md').write_bytes(b'\xff\xfe\x00')
    index = artefacts.ArtefactIndex.build(tmp_path, registry)
    assert len(index) == 0 and len(index.problems) == 3


# ── access ────────────────────────────────────────────────────────────────────

ANON = artefacts.Viewer(sees_internal=False)
USER = artefacts.Viewer(sees_internal=True, user='alice')
ADMIN = artefacts.Viewer(sees_internal=True, is_admin=True, user='root')


def _policy(tmp_path, served, folders, collections_meta):
    for folder, meta in collections_meta.items():
        write_index_md(tmp_path, folder, **meta)
    registry, _ = artefacts.scan_collections(tmp_path, {f: {m['group_id']} for f, m in collections_meta.items()
                                                       if m.get('collection_kind') != 'analysis'})
    return artefacts.AccessPolicy(registry, served, folders)


def test_collection_access_rules(tmp_path):
    served = {'MX0/pub': {'access': 'public'}, 'MX0/int': {'access': 'internal'}, 'PU1/pub': {'access': 'public'},
              'PU1/pub2': {'access': 'public'}, 'NA0/pub': {'access': 'public'}, 'AT6/x': {'access': 'internal'}}
    folders = {'Mixed2020': ['MX0/pub', 'MX0/int'], 'Public2021': ['PU1/pub', 'PU1/pub2'],
               'Narrow2020': ['NA0/pub'], 'Atlas2026': ['AT6/x']}
    policy = _policy(tmp_path, served, folders, {
        'Mixed2020': {'group': 'Mixed2020', 'group_id': 'MX0'},
        'Public2021': {'group': 'Public2021', 'group_id': 'PU1'},
        'Narrow2020': {'group': 'Narrow2020', 'group_id': 'NA0', 'access': 'internal'},
        'Empty2020': {'group': 'Empty2020', 'group_id': 'EM0'},
        'Atlas2026': {'group': 'Atlas2026', 'group_id': 'AT6', 'collection_kind': 'analysis', 'title': 'A',
                      'authors': ['M'], 'access': 'public'},
        'Notes2026': {'group': 'Notes2026', 'group_id': 'NO6', 'collection_kind': 'analysis', 'title': 'N',
                      'authors': ['M'], 'access': 'internal'},
        'Open2026': {'group': 'Open2026', 'group_id': 'OP6', 'collection_kind': 'analysis', 'title': 'O',
                     'authors': ['M'], 'access': 'public'},
    })
    assert policy.collection_public('PU1') is True
    assert policy.collection_public('MX0') is False           # the most restrictive dataset wins
    assert policy.collection_public('NA0') is False           # index.md narrows a public collection
    assert policy.collection_public('OP6') is True            # an analysis collection: its own access
    assert policy.collection_public('NO6') is False
    assert policy.collection_public('AT6') is False           # public analysis collection holding an internal dataset
    assert policy.collection_public('XXX') is False
    assert policy.collection_public('EM0') is False           # a dataset collection without served datasets
    assert policy.collection_visible('EM0', USER) and not policy.collection_visible('EM0', ANON)
    for group_id in ('PU1', 'OP6'):
        assert policy.collection_visible(group_id, ANON)
    for group_id in ('MX0', 'NA0', 'NO6', 'AT6'):
        assert not policy.collection_visible(group_id, ANON)
        assert policy.collection_visible(group_id, USER)
    assert not policy.collection_visible('XXX', ADMIN)


def test_dataset_collection_without_served_datasets_is_internal(tmp_path):
    write_index_md(tmp_path, 'Gone2020', group='Gone2020', group_id='GO0')
    registry, _ = artefacts.scan_collections(tmp_path, {})
    policy = artefacts.AccessPolicy(registry, {}, {})
    assert policy.collection_public('GO0') is False
    assert policy.collection_visible('GO0', USER) and not policy.collection_visible('GO0', ANON)


def test_artefact_access_follows_scope_and_narrows(tmp_path):
    registry = _store(tmp_path)
    write_artefact(tmp_path, FOLDER, GID, 'pub', 'figure', 'open', [{'files': {'v.png': b'1'}}])
    write_artefact(tmp_path, FOLDER, GID, 'pub', 'figure', 'narrow',
                   [{'files': {'v.png': b'1'}, 'access': 'internal'}, {'files': {'v.png': b'2'}}])
    write_artefact(tmp_path, FOLDER, GID, 'gone', 'figure', 'orphan', [{'files': {'v.png': b'1'}}])
    write_artefact(tmp_path, FOLDER, GID, '_group', 'table', 'group-table', [{'files': {'t.tsv': b'1'}}])
    index = artefacts.ArtefactIndex.build(tmp_path, registry)
    public = artefacts.AccessPolicy(registry, {f'{GID}/pub': {'access': 'public'}}, {FOLDER: [f'{GID}/pub']})
    internal = artefacts.AccessPolicy(registry, {f'{GID}/pub': {'access': 'internal'}}, {FOLDER: [f'{GID}/pub']})
    entries = {name: index.get(GID, scope, name) for scope, name in (('pub', 'open'), ('pub', 'narrow'),
                                                                      ('gone', 'orphan'), ('_group', 'group-table'))}
    assert public.artefact_visible(entries['open'], ANON)
    assert not public.artefact_visible(entries['narrow'], ANON)        # v1 narrowed to internal: most restrictive
    assert public.artefact_visible(entries['narrow'], USER)
    assert public.artefact_visible(entries['group-table'], ANON)
    for name in ('open', 'narrow', 'group-table'):
        assert not internal.artefact_visible(entries[name], ANON)
        assert internal.artefact_visible(entries[name], USER)
    for policy in (public, internal):
        assert not policy.artefact_visible(entries['orphan'], USER)
        assert not policy.artefact_visible(entries['orphan'], ADMIN)
        assert policy.artefact_visible(entries['orphan'], ADMIN, include_orphans=True)
        assert not policy.artefact_visible(entries['orphan'], USER, include_orphans=True)
    hidden = artefacts.Viewer(sees_internal=True, hidden=frozenset({f'{GID}/pub'}))
    assert not public.artefact_visible(entries['open'], hidden)        # the request's hidden set always wins


# ── delivery ──────────────────────────────────────────────────────────────────

SETTINGS = {'artefacts_download_per_minute': 1000, 'artefacts_url_ttl_seconds': 300, 'artefacts_serve_local':
            'fallback', 's3_readable': True, 'bucket': 'labbucket', 'catalog_prefix': ''}


def _service(tmp_path, s3=None, **settings):
    merged = {**SETTINGS, **settings}
    if s3 is None:
        merged['s3_readable'] = False
    return artefacts.ArtefactService(tmp_path, merged, s3_factory=(lambda: s3) if s3 is not None else None)


def _entry(tmp_path, local_files=True, previews=False, files=None):
    registry = _store(tmp_path)
    files = files or {'v.pdf': b'%PDF-1.7 bytes', 'panels/a.png': b'\x89PNG data'}
    made = write_artefact(tmp_path, FOLDER, GID, 'pub', 'figure', 'volcano',
                          [{'files': {'v.pdf': b'old'}}, {'files': files}], local_files=local_files,
                          previews=previews)
    index = artefacts.ArtefactIndex.build(tmp_path, registry)
    return index.get(GID, 'pub', 'volcano'), made


def _put_all(s3, made, version=2, prefix='', sha=True):
    path = made['path']
    for name, body in made['files'][version].items():
        s3.put(artefacts.s3_key(prefix, path.file_rel(version, name)), body,
               hashlib.sha256(body).hexdigest() if sha else None)


def test_redirect_signs_exactly_the_one_key(tmp_path):
    entry, made = _entry(tmp_path, local_files=False)
    s3 = StrictFakeS3()
    _put_all(s3, made)
    service = _service(tmp_path, s3)
    delivery = service.deliver(entry, entry.versions[2], 'panels/a.png', 'user:alice')
    assert delivery.how == 'redirect'
    key = f'{FOLDER}/static/artefacts/pub/figure/volcano/v2/files/panels/a.png'
    assert s3.calls[0] == ('head_object', 'labbucket', key)
    _name, method, params, ttl, http_method = s3.calls[1]
    assert method == 'get_object' and http_method == 'GET' and ttl == 300
    assert params == {'Bucket': 'labbucket', 'Key': key, 'ResponseContentType': 'image/png',
                      'ResponseContentDisposition': 'attachment; filename="a.png"'}
    assert not any('ACL' in k or 'acl' in k for k in params)
    assert [call[0] for call in s3.calls] == ['head_object', 'generate_presigned_url']


def test_redirect_uses_the_catalog_prefix(tmp_path):
    entry, made = _entry(tmp_path, local_files=False)
    s3 = StrictFakeS3()
    _put_all(s3, made, prefix='cat/')
    delivery = _service(tmp_path, s3, catalog_prefix='cat/').deliver(entry, entry.versions[2], 'v.pdf', 'k')
    assert delivery.how == 'redirect' and s3.calls[0][2].startswith('cat/Public2021/static/artefacts/')


def test_unknown_names_never_reach_s3_or_disk(tmp_path):
    entry, made = _entry(tmp_path)
    s3 = StrictFakeS3()
    _put_all(s3, made)
    service = _service(tmp_path, s3)
    for name in ('../v1/files/v.pdf', 'V.PDF', 'v.pdf/', 'panels', 'panels/../v.pdf', '../../../../etc/passwd',
                 'artefact.md', '../manifest.json', 'v.pdf\x00', '%2e%2e/v.pdf'):
        with pytest.raises(artefacts.ArtefactError) as caught:
            service.deliver(entry, entry.versions[2], name, 'k')
        assert caught.value.status == 404 and caught.value.body['status'] == 'not_found'
    assert s3.calls == []


def test_dangerous_types_are_still_attachments_and_odd_types_are_neutralised(tmp_path):
    entry, made = _entry(tmp_path, local_files=False, files={'page.html': b'<script>x</script>',
                                                            'img.svg': b'<svg onload=x>'})
    s3 = StrictFakeS3()
    _put_all(s3, made)
    service = _service(tmp_path, s3)
    service.deliver(entry, entry.versions[2], 'page.html', 'k')
    params = s3.calls[1][2]
    assert params['ResponseContentDisposition'].startswith('attachment;')
    assert artefacts.safe_content_type('text/html\r\nSet-Cookie: x') == 'application/octet-stream'
    assert artefacts.safe_content_type('TEXT/HTML') == 'text/html'
    with pytest.raises(ValueError):
        artefacts.presign_get(s3, 'b', 'k', 'a"b', 'text/plain', 300)


def test_s3_object_that_does_not_match_the_manifest_is_never_signed(tmp_path):
    entry, made = _entry(tmp_path, local_files=False)
    s3 = StrictFakeS3()
    key = artefacts.s3_key('', made['path'].file_rel(2, 'v.pdf'))
    s3.put(key, b'%PDF-1.7 bytes', hashlib.sha256(b'other').hexdigest())
    with pytest.raises(artefacts.ArtefactError) as caught:
        _service(tmp_path, s3).deliver(entry, entry.versions[2], 'v.pdf', 'k')
    assert caught.value.status == 502 and caught.value.body['status'] == 'integrity_error'
    s3.put(key, b'short', None)                                   # wrong size, no sha256 metadata
    with pytest.raises(artefacts.ArtefactError):
        _service(tmp_path, s3).deliver(entry, entry.versions[2], 'v.pdf', 'k')
    assert not any(call[0] == 'generate_presigned_url' for call in s3.calls)


def test_missing_sha256_metadata_with_the_right_size_is_signed(tmp_path):
    entry, made = _entry(tmp_path, local_files=False)
    s3 = StrictFakeS3()
    _put_all(s3, made, sha=False)
    assert _service(tmp_path, s3).deliver(entry, entry.versions[2], 'v.pdf', 'k').how == 'redirect'


def test_local_fallback_without_s3(tmp_path):
    entry, made = _entry(tmp_path)
    service = _service(tmp_path)                                   # site without S3 read access
    delivery = service.deliver(entry, entry.versions[2], 'panels/a.png', 'k')
    assert delivery.how == 'local' and delivery.path.read_bytes() == b'\x89PNG data'
    assert delivery.filename == 'a.png' and delivery.content_type == 'image/png'


def test_local_copy_with_the_wrong_bytes_is_refused(tmp_path):
    entry, made = _entry(tmp_path)
    (made['root'] / 'v2' / 'files' / 'v.pdf').write_bytes(b'%PDF-1.7 bytez')   # same size, other bytes
    with pytest.raises(artefacts.ArtefactError) as caught:
        _service(tmp_path).deliver(entry, entry.versions[2], 'v.pdf', 'k')
    assert caught.value.status == 404 and caught.value.body['status'] == 'not_on_this_site'
    assert caught.value.body['ref'] == f'{GID}/pub/volcano@v2'


def test_local_copy_symlinked_out_of_the_artefact_is_refused(tmp_path):
    entry, made = _entry(tmp_path)
    secret = tmp_path / 'secret.pdf'
    secret.write_bytes(b'%PDF-1.7 bytes')                           # same bytes, outside the artefact
    local = made['root'] / 'v2' / 'files' / 'v.pdf'
    local.unlink()
    os.symlink(secret, local)
    with pytest.raises(artefacts.ArtefactError) as caught:
        _service(tmp_path).deliver(entry, entry.versions[2], 'v.pdf', 'k')
    assert caught.value.body['status'] == 'not_on_this_site'


def test_local_copy_is_hashed_once_and_rechecked_after_a_change(tmp_path, monkeypatch):
    entry, made = _entry(tmp_path)
    service = _service(tmp_path)
    opened = []
    real_open = Path.open

    def counting_open(self, *args, **kwargs):
        if self.name == 'v.pdf':
            opened.append(self)
        return real_open(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', counting_open)
    for _ in range(3):
        service.deliver(entry, entry.versions[2], 'v.pdf', 'k')
    assert len(opened) == 1 and len(service.verifier._cache) == 1
    local = made['root'] / 'v2' / 'files' / 'v.pdf'
    local.write_bytes(b'%PDF-1.7 bytez')
    os.utime(local, ns=(1, 1))
    with pytest.raises(artefacts.ArtefactError):
        service.deliver(entry, entry.versions[2], 'v.pdf', 'k')


def test_s3_missing_falls_back_to_local_and_down_is_503(tmp_path):
    entry, made = _entry(tmp_path)
    s3 = StrictFakeS3()                                             # nothing on S3
    assert _service(tmp_path, s3).deliver(entry, entry.versions[2], 'v.pdf', 'k').how == 'local'
    s3.down = True
    assert _service(tmp_path, s3).deliver(entry, entry.versions[2], 'v.pdf', 'k').how == 'local'
    entry, made = _entry(tmp_path / 'b', local_files=False)
    with pytest.raises(artefacts.ArtefactError) as caught:
        _service(tmp_path / 'b', s3).deliver(entry, entry.versions[2], 'v.pdf', 'k')
    assert caught.value.status == 503 and caught.value.headers.get('Retry-After')
    s3.down = False
    with pytest.raises(artefacts.ArtefactError) as caught:
        _service(tmp_path / 'b', s3).deliver(entry, entry.versions[2], 'v.pdf', 'k')
    assert caught.value.status == 404 and caught.value.body['status'] == 'not_on_this_site'


def test_prefer_local_skips_s3(tmp_path):
    entry, made = _entry(tmp_path)
    s3 = StrictFakeS3()
    _put_all(s3, made)
    delivery = _service(tmp_path, s3, artefacts_serve_local='prefer').deliver(entry, entry.versions[2], 'v.pdf', 'k')
    assert delivery.how == 'local' and s3.calls == []
    os.remove(made['root'] / 'v2' / 'files' / 'v.pdf')
    delivery = _service(tmp_path, s3, artefacts_serve_local='prefer').deliver(entry, entry.versions[2], 'v.pdf', 'k')
    assert delivery.how == 'redirect'


def test_broken_s3_client_falls_back(tmp_path):
    entry, made = _entry(tmp_path)

    def broken():
        raise RuntimeError('bad endpoint')
    service = artefacts.ArtefactService(tmp_path, SETTINGS, s3_factory=broken)
    assert service.deliver(entry, entry.versions[2], 'v.pdf', 'k').how == 'local'


def test_download_rate_limit(tmp_path):
    entry, made = _entry(tmp_path)
    now = [1000.0]
    service = artefacts.ArtefactService(tmp_path, {**SETTINGS, 's3_readable': False,
                                                   'artefacts_download_per_minute': 2}, clock=lambda: now[0])
    service.deliver(entry, entry.versions[2], 'v.pdf', 'user:a')
    service.deliver(entry, entry.versions[2], 'v.pdf', 'user:a')
    with pytest.raises(artefacts.ArtefactError) as caught:
        service.deliver(entry, entry.versions[2], 'v.pdf', 'user:a')
    assert caught.value.status == 429 and int(caught.value.headers['Retry-After']) >= 1
    service.deliver(entry, entry.versions[2], 'v.pdf', 'user:b')      # per caller
    now[0] += 61
    service.deliver(entry, entry.versions[2], 'v.pdf', 'user:a')
    assert service.download_stats() == {'downloads': 4, 'callers': 2}


def test_caller_keys():
    assert artefacts.caller_key('alice', {}, '127.0.0.1') == 'user:alice'
    assert artefacts.caller_key('', {'x-forwarded-for': 'spoofed, 10.1.2.3'}, '127.0.0.1') == 'anon:10.1.2.3'
    assert artefacts.caller_key('', {}, '10.9.9.9') == 'anon:10.9.9.9'


def test_versions_purged_and_latest(tmp_path):
    registry = _store(tmp_path)
    write_artefact(tmp_path, FOLDER, GID, 'pub', 'figure', 'gone',
                   [{'files': {'a.png': b'1'}, 'state': 'purged', 'purged_by': 'root', 'purged_by_admin': True}])
    write_artefact(tmp_path, FOLDER, GID, 'pub', 'figure', 'two',
                   [{'files': {'a.png': b'1'}, 'state': 'purged'}, {'files': {'a.png': b'2'}}])
    index = artefacts.ArtefactIndex.build(tmp_path, registry)
    service = _service(tmp_path)
    gone, two = index.get(GID, 'pub', 'gone'), index.get(GID, 'pub', 'two')
    with pytest.raises(artefacts.ArtefactError) as caught:
        service.version_of(gone, 'latest', USER)
    assert caught.value.status == 410
    with pytest.raises(artefacts.ArtefactError) as caught:
        service.version_of(gone, 'v1', USER)
    assert caught.value.status == 410 and caught.value.body['tombstone']['purged_by'] == 'an admin'
    with pytest.raises(artefacts.ArtefactError) as caught:
        service.version_of(gone, 'v1', ADMIN)
    assert caught.value.body['tombstone']['purged_by'] == 'root'
    assert service.version_of(two, 'latest', USER).number == 2
    for label in ('v3', 'v0', 'x', '2', '../v2'):
        with pytest.raises(artefacts.ArtefactError) as caught:
            service.version_of(two, label, USER)
        assert caught.value.status == 404
    payload = service.versions_payload(two, USER)
    assert [v['state'] for v in payload['versions']] == ['purged', 'live']
    assert payload['versions'][0]['purged_by'] == 'alice'


def test_previews_are_local_typed_and_looked_up(tmp_path):
    entry, made = _entry(tmp_path, previews=True)
    service = _service(tmp_path)
    path, content_type = service.preview(entry, entry.versions[2], 'panels/a.png.webp')
    assert content_type == 'image/webp' and path.is_file()
    for name in ('panels/a.png', '../v1/preview/v.pdf.webp', 'x.webp', 'v.pdf.html', 'v.pdf.webp/../x'):
        with pytest.raises(artefacts.ArtefactError):
            service.preview(entry, entry.versions[2], name)
    summary = service.summary(entry)
    assert summary['preview'] == f'/artefacts/{GID}/pub/volcano/v2/preview/v.pdf.webp'


def test_listing_filters_and_cursor(tmp_path):
    registry = _store(tmp_path)
    for number in range(5):
        write_artefact(tmp_path, FOLDER, GID, 'pub', 'figure' if number % 2 else 'table', f'item-{number}',
                       [{'files': {'a.png': b'x' * number}}], owner='alice' if number < 3 else 'bob')
    index = artefacts.ArtefactIndex.build(tmp_path, registry)
    policy = artefacts.AccessPolicy(registry, {f'{GID}/pub': {'access': 'public'}}, {FOLDER: [f'{GID}/pub']})
    service = _service(tmp_path)
    page = service.listing(index, policy, ANON, limit=2)
    assert [a['artefact_id'] for a in page['artefacts']] == ['item-0', 'item-1'] and page['next_cursor']
    page2 = service.listing(index, policy, ANON, limit=2, cursor=page['next_cursor'])
    page3 = service.listing(index, policy, ANON, limit=2, cursor=page2['next_cursor'])
    assert [a['artefact_id'] for a in page2['artefacts'] + page3['artefacts']] == ['item-2', 'item-3', 'item-4']
    assert page3['next_cursor'] is None
    assert len(service.listing(index, policy, ANON, kind='figure')['artefacts']) == 2
    assert len(service.listing(index, policy, USER, owner='bob')['artefacts']) == 2
    with pytest.raises(artefacts.ArtefactError):                  # anonymous callers never probe logins
        service.listing(index, policy, ANON, owner='bob')
    assert {row['owner'] for row in service.listing(index, policy, ANON)['artefacts']} == {'a member'}
    assert len(service.listing(index, policy, ANON, query='ITEM-3')['artefacts']) == 1
    assert len(service.listing(index, policy, ANON, db_id=f'{GID}/pub')['artefacts']) == 5
    assert service.listing(index, policy, ANON, group_id='XX0')['artefacts'] == []
    for bad in ({'kind': 'video'}, {'db_id': 'no-slash'}, {'limit': 0}, {'limit': 201}, {'cursor': 'garbage!'},
                {'cursor': 'WyJhIl0'}):
        with pytest.raises(artefacts.ArtefactError) as caught:
            service.listing(index, policy, ANON, **bad)
        assert caught.value.status == 422
    summary = page['artefacts'][0]
    assert set(summary) >= {'ref', 'artefact_id', 'kind', 'group_id', 'scope', 'db_id', 'title', 'latest',
                            'versions_live', 'owner', 'updated', 'size_bytes', 'main_file', 'preview'}


# ── config ────────────────────────────────────────────────────────────────────

def test_artefact_settings_defaults_and_precedence(monkeypatch):
    for key in list(os.environ):
        if key.startswith('ANTHIVE_ARTEFACTS') or key == 'ANTHIVE_COLLECTIONS_CREATE_GROUPS':
            monkeypatch.delenv(key)
    settings = config.get_artefact_settings({})
    assert settings['artefacts_enabled'] is False
    assert settings['artefacts_upload_groups'] == ['admins', 'analysts']
    assert settings['collections_create_groups'] == ['admins', 'analysts']
    assert settings['artefacts_max_file_gb'] == 100 and settings['artefacts_url_ttl_seconds'] == 300
    assert settings['artefacts_serve_local'] == 'fallback' and settings['s3_readable'] is False
    cfg = {'artefacts_max_file_gb': 50, 'artefacts_upload_groups': ['Analysts', 'labA'],
           's3': {'bucket': 'b', 'access_key_id': 'k', 'secret_access_key': 's', 'prefix': '/store/'}}
    settings = config.get_artefact_settings(cfg)
    assert settings['artefacts_max_file_gb'] == 50 and settings['artefacts_upload_groups'] == ['analysts', 'laba']
    assert settings['credential_source'] == 's3' and settings['catalog_prefix'] == 'store/'
    monkeypatch.setenv('ANTHIVE_ARTEFACTS_MAX_FILE_GB', '7')
    monkeypatch.setenv('ANTHIVE_ARTEFACTS_ENABLED', 'yes')
    monkeypatch.setenv('ANTHIVE_ARTEFACTS_UPLOAD_GROUPS', 'x, y')
    monkeypatch.setenv('ANTHIVE_ARTEFACTS_S3_ACCESS_KEY_ID', 'rk')
    monkeypatch.setenv('ANTHIVE_ARTEFACTS_S3_SECRET_ACCESS_KEY', 'rs')
    settings = config.get_artefact_settings(cfg)
    assert settings['artefacts_max_file_gb'] == 7 and settings['artefacts_enabled'] is True
    assert settings['artefacts_upload_groups'] == ['x', 'y']
    assert settings['credential_source'] == 'artefacts_s3'
    assert settings['s3']['access_key_id'] == 'rk' and settings['s3']['bucket'] == 'b'


@pytest.mark.parametrize('cfg', [{'artefacts_serve_local': 'always'}, {'artefacts_url_ttl_seconds': 5},
                                 {'artefacts_url_ttl_seconds': 86400}, {'artefacts_max_files': 'many'},
                                 {'artefacts_upload_groups': 7},
                                 {'artefacts_max_file_gb': -1}])
def test_artefact_settings_refuse_bad_values(cfg, monkeypatch):
    for key in list(os.environ):
        if key.startswith('ANTHIVE_ARTEFACTS'):
            monkeypatch.delenv(key)
    with pytest.raises(config.ConfigError):
        config.get_artefact_settings(cfg)


def test_upload_group_lists_are_critical_keys():
    defaults = config.load_default_config()
    for key in ('artefacts_upload_groups', 'collections_create_groups'):
        assert key in config.CRITICAL_KEYS and key in defaults
        problems = config._critical_problems({key: 'admins'}, defaults, set())
        assert problems == [f'{key} must be a list of group names']
    assert defaults['artefacts_s3'] == {} and defaults['artefacts_enabled'] is False
    for key, value in config.ARTEFACT_DEFAULTS.items():
        assert defaults[key] == value, key


def test_who_may_upload_and_create(monkeypatch):
    for key in list(os.environ):
        if key.startswith('ANTHIVE_ARTEFACTS') or key.startswith('ANTHIVE_COLLECTIONS'):
            monkeypatch.delenv(key)
    cfg = {}
    assert config.may_upload_artefacts(['analysts'], False, cfg)
    assert config.may_upload_artefacts(['curators'], False, cfg)        # dataset uploaders too (design 4.1)
    assert config.may_upload_artefacts([], True, cfg)
    assert not config.may_upload_artefacts(['users'], False, cfg)
    assert config.may_create_collections(['analysts'], False, cfg)
    assert not config.may_create_collections(['curators'], False, cfg)  # not automatically
    assert config.may_create_collections([], True, cfg)


# ── md_check and ingest-check on analysis collections ─────────────────────────

def test_md_check_accepts_an_analysis_collection_without_default_dataset(tmp_path):
    path = _analysis(tmp_path)
    before = path.read_bytes()
    report = md_check.MdCheckReport()
    md_check.check_md_folder(tmp_path, report, fix=True)
    assert report.errors == [] and path.read_bytes() == before


def test_md_check_reports_but_never_fixes_an_analysis_collection(tmp_path):
    path = _analysis(tmp_path, group_id=None, authors=None)
    before = path.read_bytes()
    report = md_check.MdCheckReport()
    md_check.check_md_folder(tmp_path, report, fix=True)
    assert path.read_bytes() == before and report.fixed == []
    assert any('authors' in e for e in report.errors) and any('group_id' in e for e in report.errors)
    write_index_md(tmp_path, 'Odd2020', group='Odd2020', group_id='OD0', collection_kind='mixed')
    report = md_check.MdCheckReport()
    md_check.check_md_folder(tmp_path, report, fix=False)
    assert any("'mixed'" in e for e in report.errors)


def test_md_check_still_fixes_dataset_collections(tmp_path):
    write_index_md(tmp_path, 'Data2020', group='Data2020')
    report = md_check.MdCheckReport()
    md_check.check_md_folder(tmp_path, report, fix=True)
    assert report.fixed and 'group_id' in (tmp_path / 'Data2020' / 'index.md').read_text()


# ingest-check: tests/test_ingest_check_meta.py::test_index_md_of_an_analysis_collection_needs_no_default_dataset


# ── real boto3 presigning, checked like the object store does ─────────────────

def _boto3_client():
    boto3 = pytest.importorskip('boto3')
    from botocore.config import Config as BotoConfig
    return boto3.client('s3', endpoint_url='https://s3.test', region_name='us-east-1',
                        aws_access_key_id='AKIDREAD', aws_secret_access_key='read-secret',
                        config=BotoConfig(signature_version='s3v4', s3={'addressing_style': 'path'}))


def test_presigned_url_opens_one_key_with_get_until_it_expires():
    client = _boto3_client()
    key = 'Public2021/static/artefacts/pub/figure/volcano/v2/files/v.pdf'
    before = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=1)
    url = artefacts.presign_get(client, 'labbucket', key, 'v.pdf', 'application/pdf', 300)
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert parts.path == f'/labbucket/{key}'
    assert query['X-Amz-Expires'] == ['300'] and query['X-Amz-SignedHeaders'] == ['host']
    assert query['response-content-disposition'] == ['attachment; filename="v.pdf"']
    assert query['response-content-type'] == ['application/pdf']
    assert not any('acl' in name.lower() for name in query)
    now = _dt.datetime.now(_dt.timezone.utc)
    assert verify_sigv4_query(url, 'GET', 'read-secret', now)
    # the same signature for another key, another method, another disposition, a later time: refused
    other = urlunsplit(parts._replace(path=parts.path.replace('v.pdf', 'other.pdf')))
    assert not verify_sigv4_query(other, 'GET', 'read-secret', now)
    other_bucket = urlunsplit(parts._replace(path=parts.path.replace('/labbucket/', '/otherbucket/')))
    assert not verify_sigv4_query(other_bucket, 'GET', 'read-secret', now)
    for method in ('PUT', 'DELETE', 'POST', 'HEAD'):
        assert not verify_sigv4_query(url, method, 'read-secret', now)
    inline = url.replace('attachment', 'inline')
    assert not verify_sigv4_query(inline, 'GET', 'read-secret', now)
    assert not verify_sigv4_query(url, 'GET', 'read-secret', before + _dt.timedelta(seconds=302))
    assert not verify_sigv4_query(url, 'GET', 'wrong-secret', now)


def test_presign_ttl_comes_from_the_settings(tmp_path):
    client = _boto3_client()
    entry, made = _entry(tmp_path, local_files=False)

    class HeadOnly:
        def head_object(self, Bucket, Key):  # noqa: N803
            name = Key.rsplit('/', 1)[-1]
            body = made['files'][2]['v.pdf'] if name == 'v.pdf' else b''
            return {'ContentLength': len(body), 'Metadata': {}}

        def generate_presigned_url(self, *args, **kwargs):
            return client.generate_presigned_url(*args, **kwargs)
    service = artefacts.ArtefactService(tmp_path, {**SETTINGS, 'artefacts_url_ttl_seconds': 60},
                                        s3_factory=HeadOnly)
    delivery = service.deliver(entry, entry.versions[2], 'v.pdf', 'k')
    assert parse_qs(urlsplit(delivery.url).query)['X-Amz-Expires'] == ['60']
