"""Regression tests for the independent review of the artefact store A1 + A2 (findings S1 to S4, C5 to C11 and
the nits). Each test started as a probe that showed the problem; it now asserts the fixed behaviour.

Same harness as tests/test_artefact_pipeline.py: the real deposit service, presigned uploads and the publisher
on a fake S3 that fails on calls the route must never make.
"""
from __future__ import annotations

import io
import json
import time
import zipfile

import pytest

import artefact_pipeline
import artefacts
import dataset_deposit as dd
from ingest_pipeline import ObjInfo
from tests.artefact_deposit_helpers import (ADMIN, ANALYST, CATALOG, PNG, PREFIX, artefact_body, context, deposit,
                                            listing, make_publisher, make_service, poll_until_final, watch_row)
from tests.deposit_helpers import simple_deposit

ANONYMOUS = artefacts.Viewer(sees_internal=False, is_admin=False, user='')
MEMBER = artefacts.Viewer(sees_internal=True, is_admin=False, user='ana')
INTERNAL = {'scope': 'int', 'made_from': [{'db_id': 'PU1/int', 'dataset_version': '1.0.0'}]}
VOLCANO = f'{CATALOG}Public2021/static/artefacts/int/figure/volcano/'


@pytest.fixture
def clock():
    return [time.time()]


def _fresh(tmp_path):
    ctx = context(tmp_path / 'store')
    return ctx.index, ctx.policy


def _held(db, deposit_id):
    return json.loads(watch_row(db, deposit_id).get('held_reasons') or '[]')


# ── S1: made_from never names an internal dataset to a caller who cannot see it ─────────────────────────────

def test_made_from_of_an_internal_dataset_is_held_then_redacted(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock)
    publisher, db, _git, _api = make_publisher(tmp_path, s3, clock, settings={'artefacts_hold_public': False})
    answer = deposit(service, s3, ANALYST, {'volcano.png': PNG}, scope='pub',
                     made_from=[{'db_id': 'PU1/pub', 'dataset_version': '1.1.0'},
                                {'db_id': 'PU1/int', 'dataset_version': '1.0.0'}])
    poll_until_final(publisher, s3, clock)
    assert watch_row(db, answer['deposit_id'])['state'] == 'held'          # even with hold_public off
    assert _held(db, answer['deposit_id']) == ['made_from_internal']
    assert s3.keys(CATALOG) == []
    service.approve(ADMIN, answer['deposit_id'], 'the source id may show to members only')
    poll_until_final(publisher, s3, clock)
    assert watch_row(db, answer['deposit_id'])['state'] == 'live'

    index, policy = _fresh(tmp_path)
    reader = artefacts.ArtefactService(tmp_path / 'store', {})
    entry = reader.resolve(index, policy, ANONYMOUS, 'PU1', 'pub', 'volcano')
    anonymous = reader.detail(entry, ANONYMOUS, policy)
    text = json.dumps(anonymous)
    assert 'PU1/int' not in text
    assert [item['db_id'] for item in anonymous['manifest']['made_from']] == ['PU1/pub', None]
    assert anonymous['manifest']['recorded']['uploaded_by'] == 'a member'
    assert anonymous['owner'] == 'a member'
    version = reader.version_payload(entry, entry.versions[1], ANONYMOUS, policy)
    assert 'PU1/int' not in json.dumps(version)
    member = reader.detail(entry, MEMBER, policy)
    assert [item['db_id'] for item in member['manifest']['made_from']] == ['PU1/pub', 'PU1/int']
    assert member['owner'] == 'ana'


def test_internal_access_with_internal_sources_is_not_held_for_made_from(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock)
    publisher, db, _git, _api = make_publisher(tmp_path, s3, clock, settings={'artefacts_hold_public': False})
    answer = deposit(service, s3, ANALYST, {'volcano.png': PNG}, scope='pub', access='internal',
                     made_from=[{'db_id': 'PU1/pub', 'dataset_version': '1.1.0'},
                                {'db_id': 'PU1/int', 'dataset_version': '1.0.0'}])
    poll_until_final(publisher, s3, clock)
    assert watch_row(db, answer['deposit_id'])['state'] == 'live'


# ── C5: internal stays internal after the internal version is purged ───────────────────────────────────────

def test_purging_the_internal_version_keeps_the_artefact_internal(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock)
    publisher, _db, _git, _api = make_publisher(tmp_path, s3, clock, settings={'artefacts_hold_public': False})
    deposit(service, s3, ANALYST, {'volcano.png': PNG}, scope='pub', access='internal')
    poll_until_final(publisher, s3, clock)
    deposit(service, s3, ANALYST, {'volcano.png': PNG + b'2'}, scope='pub', new_version_of=1, change_note='v2')
    poll_until_final(publisher, s3, clock)
    index, policy = _fresh(tmp_path)
    entry = index.get('PU1', 'pub', 'volcano')
    assert entry.latest == 2 and not policy.artefact_visible(entry, ANONYMOUS)
    service.request_purge(ANALYST, context(service.store_root), 'PU1', 'pub', 'volcano', 'v1',
                          {'reason': 'old one', 'confirm': 'PU1/pub/volcano@v1'})
    publisher.poll(listing(s3))
    index, policy = _fresh(tmp_path)
    entry = index.get('PU1', 'pub', 'volcano')
    assert entry.versions[1].state == 'purged' and entry.versions[1].access == 'internal'
    assert not policy.artefact_visible(entry, ANONYMOUS)
    assert policy.artefact_visible(entry, MEMBER)


def test_tombstone_hides_logins_and_reason_from_anonymous(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock)
    publisher, _db, _git, _api = make_publisher(tmp_path, s3, clock, settings={'artefacts_hold_public': False})
    deposit(service, s3, ANALYST, {'volcano.png': PNG}, scope='pub')
    poll_until_final(publisher, s3, clock)
    deposit(service, s3, ANALYST, {'volcano.png': PNG + b'2'}, scope='pub', new_version_of=1, change_note='v2')
    poll_until_final(publisher, s3, clock)
    service.request_purge(ANALYST, context(service.store_root), 'PU1', 'pub', 'volcano', 'v1',
                          {'reason': 'contains a typo in the title', 'confirm': 'PU1/pub/volcano@v1'})
    publisher.poll(listing(s3))
    index, _policy = _fresh(tmp_path)
    version = index.get('PU1', 'pub', 'volcano').versions[1]
    anonymous = artefacts.tombstone(version, ANONYMOUS)
    assert anonymous['purged_by'] == 'a member' and anonymous['purge_reason'] is None
    assert artefacts.tombstone(version, MEMBER)['purge_reason'] == 'contains a typo in the title'


# ── S3: a failed placement reserves its number, never blocks the next deposit, and an admin cleans it up ────

def test_a_failed_placement_does_not_block_a_later_deposit(tmp_path, clock, monkeypatch):
    service, s3 = make_service(tmp_path, clock)
    publisher, db, _git, _api = make_publisher(tmp_path, s3, clock)
    real = publisher._manifest

    def broken(*args, **kwargs):
        raise artefact_pipeline.Stop('failed', 'simulated failure after placement')
    monkeypatch.setattr(publisher, '_manifest', broken)
    first = deposit(service, s3, ANALYST, {'volcano.png': PNG}, **INTERNAL)
    poll_until_final(publisher, s3, clock)
    assert watch_row(db, first['deposit_id'])['state'] == 'failed'
    assert [key for key in s3.keys(VOLCANO) if '/v1/files/' in key] == []        # copies removed
    head = (tmp_path / 'store' / 'Public2021' / 'static' / 'artefacts' / 'int' / 'figure' / 'volcano' /
            'artefact.md').read_text()
    assert 'state: failed' in head

    monkeypatch.setattr(publisher, '_manifest', real)
    second = deposit(service, s3, ANALYST, {'volcano.png': PNG + b'new'}, **INTERNAL)
    poll_until_final(publisher, s3, clock)
    assert watch_row(db, second['deposit_id'])['state'] == 'live'
    index, _policy = _fresh(tmp_path)
    entry = index.get('PU1', 'int', 'volcano')
    assert entry.latest == 2                                 # the failed number is never reused
    assert entry.versions[1].state == 'failed' and entry.versions[2].state == 'live'

    with pytest.raises(dd.DepositError) as caught:          # only an admin cleans up a failed version
        service.request_purge(ANALYST, context(service.store_root), 'PU1', 'int', 'volcano', 'v1',
                              {'reason': 'clean up', 'confirm': 'PU1/int/volcano@v1'})
    assert caught.value.status == 403
    admin_ctx = context(service.store_root, artefacts.Viewer(sees_internal=True, is_admin=True, user='root'))
    service.request_purge(ADMIN, admin_ctx, 'PU1', 'int', 'volcano', 'v1',
                          {'reason': 'clean up the failed publication', 'confirm': 'PU1/int/volcano@v1'})
    publisher.poll(listing(s3))
    index, _policy = _fresh(tmp_path)
    assert index.get('PU1', 'int', 'volcano').versions[1].state == 'purged'
    assert [key for key in s3.keys(VOLCANO) if '/v1/' in key] == []


def test_a_key_of_another_deposit_is_never_reused(tmp_path, clock):
    """Same bytes, same sha256, but placed by another deposit (or re-uploaded without the mark): a stop."""
    service, s3 = make_service(tmp_path, clock)
    publisher, db, _git, _api = make_publisher(tmp_path, s3, clock)
    s3.put(f'{VOLCANO}v1/files/volcano.png', PNG, clock[0],
           metadata={'sha256': __import__('hashlib').sha256(PNG).hexdigest(), 'deposit': 'someone-else'})
    answer = deposit(service, s3, ANALYST, {'volcano.png': PNG}, **INTERNAL)
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] in ('needs_decision', 'failed')
    assert 'never overwritten' in (row.get('error') or '')


def test_stripped_sha_metadata_stops_purge(tmp_path, clock):
    """An `ant sync push` re-upload (metadata = mtime only) must not let a purge delete the key unchecked."""
    service, s3 = make_service(tmp_path, clock)
    publisher, _db, _git, _api = make_publisher(tmp_path, s3, clock)
    deposit(service, s3, ANALYST, {'volcano.png': PNG}, **INTERNAL)
    poll_until_final(publisher, s3, clock)
    key = f'{VOLCANO}v1/files/volcano.png'
    s3.objects[key]['metadata'] = {'mtime': '1'}
    service.request_purge(ANALYST, context(service.store_root), 'PU1', 'int', 'volcano', 'v1',
                          {'reason': 'old one', 'confirm': 'PU1/int/volcano@v1'})
    publisher.poll(listing(s3))
    assert service.purges(ADMIN)[0]['state'] == 'needs_decision'
    assert key in s3.objects


# ── C6: a multipart copy is checked against the source after the copy ─────────────────────────────────────

def _multipart_case(tmp_path, clock, change_source):
    service, s3 = make_service(tmp_path, clock)
    publisher, _db, _git, _api = make_publisher(tmp_path, s3, clock)
    source_key = f'{PREFIX}Public2021/artefacts/dep1/volcano.png'
    s3.put(source_key, PNG, clock[0])
    s3.objects[source_key]['etag'] = 'abc123-2'              # a multipart ETag
    s3.ignore_conditions = True                              # a store that ignores CopySourceIfMatch
    if change_source:
        def swap(_key):
            s3.objects[source_key]['etag'] = 'def456-2'
        s3.before_copy = swap
    sha = __import__('hashlib').sha256(PNG).hexdigest()
    path = artefacts.ArtefactPath('Public2021', 'int', 'figure', 'volcano')
    publisher._place(path, 1, 'volcano.png', ObjInfo(source_key, 'abc123-2', len(PNG)),
                     {'sha256': sha, 'size': len(PNG)}, {'id': 'dep1'}, 'PU1/int/volcano')
    return s3


def test_multipart_copy_of_an_unchanged_source_passes(tmp_path, clock):
    s3 = _multipart_case(tmp_path, clock, change_source=False)
    assert f'{VOLCANO}v1/files/volcano.png' in s3.objects


def test_multipart_copy_stops_when_the_source_changed(tmp_path, clock):
    with pytest.raises(artefact_pipeline.Stop) as caught:
        _multipart_case(tmp_path, clock, change_source=True)
    assert caught.value.state == 'needs_decision'


# ── C8: active types are stored as octet-stream attachments ────────────────────────────────────────────────

def test_active_content_is_stored_as_an_attachment(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock)
    publisher, db, _git, _api = make_publisher(tmp_path, s3, clock)
    html = b'<html><body><script>alert(1)</script></body></html>'
    answer = deposit(service, s3, ANALYST, {'report.html': html}, kind='report',
                     artefact_id='report', **INTERNAL)
    poll_until_final(publisher, s3, clock)
    assert watch_row(db, answer['deposit_id'])['state'] == 'live'
    base = f'{CATALOG}Public2021/static/artefacts/int/report/report/v1/files/'
    assert s3.content_types[f'{base}report.html'] == 'application/octet-stream'
    assert s3.dispositions[f'{base}report.html'].startswith('attachment')
    assert artefact_pipeline.stored_type('x.svg')[0] == 'application/octet-stream'
    assert artefact_pipeline.stored_type('x.png')[0] == 'image/png'


# ── C9: the artefact lock is its own file ───────────────────────────────────────────────────────────────

def test_the_scope_lock_is_not_the_duckdb_lock(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock)
    publisher, _db, _git, _api = make_publisher(tmp_path, s3, clock)
    path = artefacts.ArtefactPath('Public2021', 'int', 'figure', 'volcano')
    md = tmp_path / 'store' / 'Public2021' / 'int.md'
    lock_base = publisher._scope_lock_path(path, md)
    assert lock_base.name == 'int.artefacts'                 # writer_lock adds ".lock": int.artefacts.lock
    assert lock_base.with_name(lock_base.name + '.lock').name != 'int.duckdb.lock'


# ── C10: dataset deposits against collections ────────────────────────────────────────────────────────────

def test_a_dataset_deposit_into_an_analysis_collection_needs_its_creator(tmp_path, clock):
    service, _s3 = make_service(tmp_path, clock, datasets_enabled=True)
    body, _payloads = simple_deposit(group='Atlas2026')
    with pytest.raises(dd.DepositError) as caught:
        service.create(ANALYST, body)
    assert caught.value.status == 403
    try:
        service.create(ADMIN, body)
    except dd.DepositError as exc:                           # other checks may refuse; never this rule
        assert 'analysis collection' not in str(exc.body)


def test_a_dataset_deposit_waits_for_a_pending_collection_of_that_name(tmp_path, clock):
    service, _s3 = make_service(tmp_path, clock, datasets_enabled=True)
    service.create(ANALYST, {'deposit_kind': 'collection', 'data_project': 'Fiers2026',
                             'collection': {'title': 'Microglia atlas work', 'authors': ['Mark Fiers']}},
                   context(service.store_root))
    body, _payloads = simple_deposit(group='Fiers2026')
    with pytest.raises(dd.DepositError) as caught:
        service.create(ANALYST, body)
    assert caught.value.status == 409 and 'collection_pending' in json.dumps(caught.value.body)


def test_a_collection_waits_for_a_pending_new_group_dataset(tmp_path, clock):
    service, _s3 = make_service(tmp_path, clock, datasets_enabled=True)
    body, _payloads = simple_deposit(group='New2026')
    service.create(ANALYST, body)
    with pytest.raises(dd.DepositError) as caught:
        service.create(ANALYST, {'deposit_kind': 'collection', 'data_project': 'New2026',
                                 'collection': {'title': 'Microglia atlas work', 'authors': ['Mark Fiers']}},
                       context(service.store_root))
    assert caught.value.status == 409


# ── nits: quota counts pending deposits; listing by owner needs a sign-in ────────────────────────────────

def test_the_quota_counts_pending_deposits(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock,
                               artefact_settings={'artefacts_max_stored_gb_per_user': 300 / 1024 ** 3})
    deposit(service, s3, ANALYST, {'volcano.png': PNG}, **INTERNAL)            # waits for the watcher
    with pytest.raises(dd.DepositError) as caught:
        service.create(ANALYST, artefact_body({'other.png': PNG}, artefact_id='other', **INTERNAL),
                       context(service.store_root))
    assert caught.value.status == 413


# ── S4 / C7: streamed scans, not_scanned holds, more file types in the donor scan, ranged ZIP reads ──────

def test_secret_scan_finds_a_secret_across_a_chunk_boundary(tmp_path):
    secret = b'aws_secret_access_key = abcdefghijklmnopqrstuvwxyz0123\n'
    path = tmp_path / 'notes.txt'
    path.write_bytes(b'x' * (artefact_pipeline.SCAN_CHUNK - 10) + b'\n' + secret + b'y' * 100)
    assert artefact_pipeline.scan_secrets(path)
    clean = tmp_path / 'clean.txt'
    clean.write_bytes(b'x' * (artefact_pipeline.SCAN_CHUNK * 2))
    assert artefact_pipeline.scan_secrets(clean) == []


@pytest.mark.parametrize('name,body', [
    ('donors.json', b'{"donors": ["RM101", "RM999"]}'),
    ('donors.txt', b'the donor RM101 was excluded\n'),
    ('donors.csv', b'donor,value\nrm101,1\n'),
])
def test_donor_scan_reads_text_and_json(tmp_path, name, body):
    path = tmp_path / name
    path.write_bytes(body)
    hits, truncated = artefact_pipeline.donor_hits(path, name, {'rm101', 'leuven:rm101'})
    assert hits == 1 and not truncated


def test_donor_scan_reads_parquet_and_xlsx(tmp_path):
    import duckdb
    parquet = tmp_path / 'donors.parquet'
    connection = duckdb.connect()
    connection.execute(f"COPY (SELECT 'RM101' AS donor, 1 AS value) TO '{parquet}' (FORMAT PARQUET)")
    connection.close()
    assert artefact_pipeline.donor_hits(parquet, 'donors.parquet', {'rm101'}) == (1, False)
    xlsx = tmp_path / 'donors.xlsx'
    with zipfile.ZipFile(xlsx, 'w') as archive:
        archive.writestr('xl/sharedStrings.xml', '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/'
                                                 '2006/main"><si><t>RM101</t></si></sst>')
        archive.writestr('xl/worksheets/sheet1.xml', '<worksheet/>')
    assert artefact_pipeline.donor_hits(xlsx, 'donors.xlsx', {'rm101'}) == (1, False)


def test_donor_scan_reports_the_row_cap(tmp_path):
    path = tmp_path / 'big.tsv'
    path.write_text('donor\n' + 'x\n' * 50)
    assert artefact_pipeline.donor_hits(path, 'big.tsv', {'rm101'}, row_cap=10) == (0, True)


def test_a_json_with_donor_ids_is_held(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock)
    publisher, db, _git, _api = make_publisher(tmp_path, s3, clock)
    (tmp_path / 'store' / 'donor_crosswalk.tsv').write_text(
        'cohort\talias_type\talias\tcanonical_uid\tsource\tn_datasets\nleuven\tdonor_id\tRM101\tleuven:RM101\tx\t1\n')
    answer = deposit(service, s3, ANALYST, {'donors.json': b'{"excluded": ["RM101"]}'}, kind='other',
                     artefact_id='donors', **INTERNAL)
    poll_until_final(publisher, s3, clock)
    assert _held(db, answer['deposit_id']) == ['donor_identifiers']


def test_a_table_too_large_to_scan_is_held_not_passed(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock)
    publisher, db, _git, _api = make_publisher(tmp_path, s3, clock,
                                               settings={'artefacts_content_check_max_gb': 10 / 1024 ** 3,
                                                         'artefacts_inline_max_mb': 10 / 1024 ** 2})
    answer = deposit(service, s3, ANALYST, {'table.tsv': b'gene\tvalue\nApoe\t1\nTrem2\t2\n'}, kind='table',
                     artefact_id='table', **INTERNAL)
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'held' and 'not_scanned' in _held(db, answer['deposit_id'])
    assert s3.keys(CATALOG) == []


def test_ranged_reader_lists_a_zip_without_downloading_it(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('weights/model.bin', b'\x00' * 5000)
    body = buffer.getvalue()
    key = f'{PREFIX}Public2021/artefacts/dep1/model.zip'
    s3.put(key, body, clock[0])
    ranges = []

    def get_object(Bucket, Key, IfMatch=None, Range=None):  # noqa: N803
        assert IfMatch == f'"{s3.objects[Key]["etag"]}"'
        start, end = (int(part) for part in Range.split('=')[1].split('-'))
        ranges.append((start, end))
        return {'Body': io.BytesIO(s3.objects[Key]['body'][start:end + 1])}
    s3.__dict__['get_object'] = get_object
    reader = artefact_pipeline.RangedObject(s3, 'bucket', key, s3.objects[key]['etag'], len(body))
    with zipfile.ZipFile(reader) as archive:
        assert archive.namelist() == ['weights/model.bin']
    assert sum(end - start + 1 for start, end in ranges) < len(body)          # the member was never read


# ── review round 3, nit 7: active content types are never recorded or served as such ─────────────────────

@pytest.mark.parametrize('value', ['text/html', 'image/svg+xml', 'application/javascript', 'text/javascript',
                                   'application/xml', 'text/xml', 'application/xhtml+xml', 'TEXT/HTML'])
def test_active_content_types_become_octet_stream(value):
    assert artefacts.safe_content_type(value) == 'application/octet-stream'


def test_plain_content_types_are_kept():
    assert artefacts.safe_content_type('image/png') == 'image/png'
    assert artefacts.safe_content_type('text/tab-separated-values') == 'text/tab-separated-values'


def test_an_html_report_is_recorded_as_octet_stream_in_its_manifest(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock)
    publisher, db, _git, _api = make_publisher(tmp_path, s3, clock)
    answer = deposit(service, s3, ANALYST, {'report.html': b'<html><body>x</body></html>'}, kind='report',
                     artefact_id='report', **INTERNAL)
    poll_until_final(publisher, s3, clock)
    assert watch_row(db, answer['deposit_id'])['state'] == 'live'
    index, _policy = _fresh(tmp_path)
    manifest = index.get('PU1', 'int', 'report').versions[1].manifest
    assert manifest['files'][0]['content_type'] == 'application/octet-stream'
