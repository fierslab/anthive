"""Dataset deposits through the API (lib/dataset_deposit.py): rules, presigning, completion, status, withdraw.

The S3 side is an in-memory fake that behaves like S3 for presigned URLs
(signed Content-Length and x-amz-meta-* headers, expiry). Design section 22.
"""
from __future__ import annotations

import json
import time

import pytest

import dataset_deposit as dd
import ingest_watch
from tests.deposit_helpers import (ADMIN, ALICE, BOB, STORE_DB_ID, DepositFakeS3, file_entry, make_service,
                                   sha256_bytes, sidecar_for, simple_deposit, upload)
from tests.ingest_watch_helpers import PREFIX, S3Error


@pytest.fixture()
def clock():
    return [time.time()]


def created(service, identity=ALICE, **kwargs):
    body, payloads = simple_deposit(**kwargs)
    return service.create(identity, body), payloads


# ── create: instructions, keys, methods ─────────────────────────────────────

def test_create_returns_presigned_puts_for_exact_keys(tmp_path, clock):
    service = make_service(tmp_path, clock)
    response, payloads = created(service)
    assert response['state'] == 'uploading' and dd.DEPOSIT_ID_RE.match(response['deposit_id'])
    keys = {entry['name']: entry['key'] for entry in response['files']}
    assert keys == {'cells.h5ad': f'{PREFIX}New2026/cells.h5ad', 'cells.h5ad.mus': f'{PREFIX}New2026/cells.h5ad.mus',
                    'cells.md': f'{PREFIX}New2026/static/cells.md', 'index.md': f'{PREFIX}New2026/static/index.md'}
    put = response['files'][0]
    assert put['method'] == 'put'
    assert put['headers']['x-amz-meta-deposit'] == response['deposit_id']
    assert put['headers']['x-amz-meta-sha256'] == sha256_bytes(payloads['cells.h5ad'])
    signed = list(service.s3.presigned.values())[0]['params']
    assert signed['ContentLength'] == len(payloads['cells.h5ad'])          # the size is part of the signature
    assert 'secret' not in json.dumps(response) and 's3' not in response   # no credentials in the answer


def test_large_files_are_multipart_with_parts_in_batches(tmp_path, clock):
    service = make_service(tmp_path, clock, single_put_max_mb=0, parts_per_batch=2)
    big = b'x' * 10
    response, _ = created(service, h5ad=big)
    h5ad = response['files'][0]
    assert h5ad['method'] == 'multipart' and h5ad['upload_id'] in service.s3.uploads
    assert h5ad['part_count'] == 1 and h5ad['parts'][0]['size'] == 10 and h5ad['parts_more'] is False
    assert service.s3.uploads[h5ad['upload_id']]['metadata'] == {'sha256': sha256_bytes(big),
                                                                 'deposit': response['deposit_id']}


def test_part_size_scales_with_the_file():
    assert dd.choose_part_size(1) == 64 * dd.MiB
    huge = 2 * 1024 ** 4                                     # 2 TiB
    part = dd.choose_part_size(huge)
    assert part % dd.MiB == 0 and huge / part <= dd.PART_COUNT_TARGET
    assert sum(dd.part_sizes(1000, 300)) == 1000 and dd.part_sizes(1000, 300)[-1] == 100


@pytest.mark.parametrize('name,kind', [
    ('../cells.h5ad', 'h5ad'), ('sub/cells.md', 'seed_md'), ('.hidden', 'static'), ('_x.pdf', 'static'),
    ('a b.pdf', 'static'), ('café.pdf', 'static'), ('de/../x.tsv', 'de'), ('x.tsv', 'de'),
    ('/etc/passwd', 'static'), ('other.h5ad', 'h5ad'), ('index.md', 'static'), ('cells.md', 'static'),
    ('x.h5ad', 'static'),
])
def test_path_traversal_and_bad_names_are_refused(tmp_path, clock, name, kind):
    service = make_service(tmp_path, clock)
    body, _ = simple_deposit(extra_files={name: (b'x', kind)}) if kind != 'h5ad' else simple_deposit(with_h5ad=False)
    if kind == 'h5ad':
        body['files'].append(file_entry(name, b'x', 'h5ad'))
    with pytest.raises(dd.DepositError) as exc:
        service.create(ALICE, body)
    assert exc.value.status == 422 and exc.value.body['status'] in ('invalid_name', 'invalid')
    assert not service.store.deposits()


@pytest.mark.parametrize('group,word', [('../etc', 'invalid_data_project'), ('_staging', 'invalid_data_project'),
                                        ('smith2024', 'invalid_data_project'), ('Smith', 'invalid_data_project'),
                                        ('old2020', 'invalid_data_project'), ('Grp/2024', 'invalid_data_project')])
def test_data_project_rules(tmp_path, clock, group, word):
    service = make_service(tmp_path, clock)
    body, _ = simple_deposit(group=group)
    with pytest.raises(dd.DepositError) as exc:
        service.create(ALICE, body)
    assert exc.value.body['status'] == word


def test_basename_rules(tmp_path, clock):
    service = make_service(tmp_path, clock)
    for basename in ('x' * 51, 'a.h5ad', '-x'):
        body, _ = simple_deposit(basename=basename)
        with pytest.raises(dd.DepositError):
            service.create(ALICE, body)


def test_oversize_is_413(tmp_path, clock):
    service = make_service(tmp_path, clock, max_gb=1e-9)
    with pytest.raises(dd.DepositError) as exc:
        created(service)
    assert exc.value.status == 413 and exc.value.body['status'] == 'too_large'
    service = make_service(tmp_path / 'b', clock)
    body, _ = simple_deposit()
    body['files'][1]['size'] = dd.TEXT_MAX_BYTES + 1                   # the sidecar
    with pytest.raises(dd.DepositError) as exc:
        service.create(ALICE, body)
    assert exc.value.status == 413


def test_sidecar_is_required_and_kinds_unique(tmp_path, clock):
    service = make_service(tmp_path, clock)
    body, _ = simple_deposit()
    body['files'] = [entry for entry in body['files'] if entry['kind'] != 'mus']
    with pytest.raises(dd.DepositError):
        service.create(ALICE, body)
    body, _ = simple_deposit()
    body['files'][0]['sha256'] = 'abc'
    with pytest.raises(dd.DepositError):
        service.create(ALICE, body)


def test_disabled_without_credentials(tmp_path, clock):
    service = make_service(tmp_path, clock)
    service.settings.s3 = {}
    with pytest.raises(dd.DepositError) as exc:
        created(service)
    assert exc.value.status == 503 and exc.value.body['status'] == 'disabled'
    assert service.vocab()['uploads_enabled'] is False


# ── complete ────────────────────────────────────────────────────────────────

def test_complete_records_etags_and_moves_to_uploaded(tmp_path, clock):
    service = make_service(tmp_path, clock)
    response, payloads = created(service)
    upload(service.s3, response, payloads)
    result = service.complete(ALICE, response['deposit_id'])
    assert result['state'] == 'uploaded' and not result['warnings']
    record = service.store.get(response['deposit_id'])
    assert record['h5ad_etag'] == service.s3.objects[f'{PREFIX}New2026/cells.h5ad']['etag']
    assert record['mus_etag'] == service.s3.objects[f'{PREFIX}New2026/cells.h5ad.mus']['etag']
    assert service.s3.objects[f'{PREFIX}New2026/cells.h5ad']['metadata']['sha256'] == sha256_bytes(payloads['cells.h5ad'])
    assert service.status(ALICE, response['deposit_id'])['state'] == 'uploaded'
    with pytest.raises(dd.DepositError) as exc:
        service.complete(ALICE, response['deposit_id'])
    assert exc.value.status == 409


def test_complete_multipart_checks_every_part(tmp_path, clock):
    service = make_service(tmp_path, clock, single_put_max_mb=0)
    response, payloads = created(service)
    for entry in response['files'][1:]:                                 # everything but the h5ad parts
        for part in entry['parts']:
            service.s3.client_upload_part(part['url'], payloads[entry['name']])
    with pytest.raises(dd.DepositError) as exc:
        service.complete(ALICE, response['deposit_id'])
    assert exc.value.status == 409 and exc.value.body['status'] == 'incomplete'
    assert exc.value.body['files'] == [{'name': 'cells.h5ad', 'ok': False, 'missing_parts': [1],
                                        'wrong_size_parts': [], 'uploaded_bytes': 0}]
    upload(service.s3, {'files': response['files'][:1]}, payloads)
    assert service.complete(ALICE, response['deposit_id'])['state'] == 'uploaded'
    assert service.s3.objects[f'{PREFIX}New2026/cells.h5ad']['body'] == payloads['cells.h5ad']


def test_wrong_size_and_missing_signed_headers_are_refused_by_s3(tmp_path, clock):
    service = make_service(tmp_path, clock)
    response, payloads = created(service)
    put = response['files'][0]
    with pytest.raises(S3Error):
        service.s3.client_put(put['url'], payloads['cells.h5ad'] + b'more', put['headers'])
    headers = {key: value for key, value in put['headers'].items() if key != 'x-amz-meta-sha256'}
    with pytest.raises(S3Error):
        service.s3.client_put(put['url'], payloads['cells.h5ad'], headers)


def test_sidecar_with_another_sha256_is_refused_and_removed(tmp_path, clock):
    service = make_service(tmp_path, clock)
    body, payloads = simple_deposit()
    wrong = sidecar_for('0' * 64)
    payloads['cells.h5ad.mus'] = wrong
    body['files'][1] = file_entry('cells.h5ad.mus', wrong, 'mus')
    response = service.create(ALICE, body)
    upload(service.s3, response, payloads)
    with pytest.raises(dd.DepositError) as exc:
        service.complete(ALICE, response['deposit_id'])
    assert exc.value.status == 422 and exc.value.body['status'] == 'sidecar_mismatch'
    assert f'{PREFIX}New2026/cells.h5ad.mus' not in service.s3.objects
    assert service.store.get(response['deposit_id'])['state'] == 'uploading'


def test_complete_warns_about_held_reasons_and_ignored_depositor(tmp_path, clock):
    service = make_service(tmp_path, clock)
    response, payloads = created(service, sidecar_extra={'license': None, 'data_use': None, 'depositor': 'Mallory',
                                                         'access': 'public'})
    upload(service.s3, response, payloads)
    warnings = ' '.join(service.complete(ALICE, response['deposit_id'])['warnings'])
    assert 'licence_unknown' in warnings and 'data_use_unknown' in warnings and 'public_access' in warnings
    assert 'ignored for identity' in warnings


# ── who may see and change what ─────────────────────────────────────────────

def test_other_users_deposit_is_404_for_every_action(tmp_path, clock):
    service = make_service(tmp_path, clock)
    response, payloads = created(service)
    deposit_id = response['deposit_id']
    for call in (lambda: service.status(BOB, deposit_id), lambda: service.complete(BOB, deposit_id),
                 lambda: service.refresh_urls(BOB, deposit_id, {'name': 'cells.h5ad'}),
                 lambda: service.withdraw(BOB, deposit_id), lambda: service.report(BOB, deposit_id)):
        with pytest.raises(dd.DepositError) as exc:
            call()
        assert exc.value.status == 404
    assert service.list(BOB)['deposits'] == []
    with pytest.raises(dd.DepositError) as exc:
        service.list(BOB, all_users=True)
    assert exc.value.status == 403
    assert [entry['deposit_id'] for entry in service.list(ADMIN, all_users=True)['deposits']] == [deposit_id]
    assert service.list(ADMIN, all_users=True)['deposits'][0]['user'] == 'alice'
    assert 'user' not in service.list(ALICE)['deposits'][0]
    assert service.status(ADMIN, deposit_id)['deposit_id'] == deposit_id
    with pytest.raises(dd.DepositError):
        service.status(ALICE, '../../etc/passwd')


def test_withdraw_aborts_and_deletes_only_own_objects(tmp_path, clock):
    service = make_service(tmp_path, clock, single_put_max_mb=0)
    response, payloads = created(service)
    deposit_id = response['deposit_id']
    upload(service.s3, {'files': response['files'][1:]}, payloads)       # all but the h5ad
    other_key = f'{PREFIX}New2026/static/paper.pdf'
    service.s3.put(other_key, b'%PDF not ours')
    with pytest.raises(dd.DepositError) as exc:
        service.withdraw(BOB, deposit_id)
    assert exc.value.status == 404
    result = service.withdraw(ALICE, deposit_id)
    assert result['state'] == 'withdrawn'
    assert not service.s3.uploads                                        # the multipart uploads are aborted
    assert other_key in service.s3.objects
    assert not [key for key in service.s3.objects if key.startswith(f'{PREFIX}New2026/') and key != other_key]
    assert all(':' not in key for key in result['deleted'])
    with pytest.raises(dd.DepositError) as exc:
        service.withdraw(ALICE, deposit_id)
    assert exc.value.status == 409
    assert service.status(ALICE, deposit_id)['state'] == 'withdrawn'


def test_withdraw_keeps_an_object_another_deposit_overwrote(tmp_path, clock):
    service = make_service(tmp_path, clock)
    response, payloads = created(service)
    upload(service.s3, response, payloads)
    service.complete(ALICE, response['deposit_id'])
    key = f'{PREFIX}New2026/static/index.md'
    service.s3.put(key, b'newer index', metadata={'deposit': 'dep_' + 'f' * 20, 'sha256': '0' * 64})
    result = service.withdraw(ALICE, response['deposit_id'])
    assert key in service.s3.objects and {'key': key, 'reason': 'written by another deposit or upload'} in result['kept']


# ── expiry ──────────────────────────────────────────────────────────────────

def test_expired_urls_and_deposits(tmp_path, clock):
    service = make_service(tmp_path, clock, single_put_max_mb=0, url_ttl_hours=1)
    response, payloads = created(service)
    deposit_id = response['deposit_id']
    first_part = response['files'][1]['parts'][0]
    clock[0] += 3601
    with pytest.raises(S3Error):                                         # S3 refuses the old URL
        service.s3.client_upload_part(first_part['url'], payloads['cells.h5ad.mus'])
    for call in (lambda: service.refresh_urls(ALICE, deposit_id, {'name': 'cells.h5ad'}),
                 lambda: service.complete(ALICE, deposit_id)):
        with pytest.raises(dd.DepositError) as exc:
            call()
        assert exc.value.status == 410
    assert service.store.get(deposit_id)['state'] == 'expired'
    assert not service.s3.uploads


def test_refresh_extends_the_window_and_reports_parts_done(tmp_path, clock):
    service = make_service(tmp_path, clock, single_put_max_mb=0, url_ttl_hours=1)
    response, payloads = created(service)
    deposit_id = response['deposit_id']
    h5ad = response['files'][0]
    service.s3.client_upload_part(h5ad['parts'][0]['url'], payloads['cells.h5ad'])
    clock[0] += 3000
    fresh = service.refresh_urls(ALICE, deposit_id, {'name': 'cells.h5ad', 'first_part': 1, 'last_part': 1})
    assert fresh['parts_done'][0]['part_number'] == 1 and fresh['parts'][0]['url'] != h5ad['parts'][0]['url']
    clock[0] += 3000                                                     # past the first window, inside the second
    assert service.status(ALICE, deposit_id)['state'] == 'uploading'
    with pytest.raises(dd.DepositError):
        service.refresh_urls(ALICE, deposit_id, {'name': 'cells.h5ad', 'first_part': 2, 'last_part': 1})


# ── limits ──────────────────────────────────────────────────────────────────

def test_open_deposit_limit_quota_and_rate_limit(tmp_path, clock):
    service = make_service(tmp_path, clock, max_open_per_user=2)
    created(service, group='Aaa2026')
    created(service, group='Bbb2026')
    with pytest.raises(dd.DepositError) as exc:
        created(service, group='Ccc2026')
    assert exc.value.status == 429 and exc.value.body['status'] == 'too_many_open_deposits'
    created(service, BOB, group='Ccc2026')                               # another user is not affected

    quota = make_service(tmp_path / 'q', clock, max_pending_gb_per_user=2000 / 1024 ** 3)
    created(quota, h5ad=b'x' * 900, group='Aaa2026')
    with pytest.raises(dd.DepositError) as exc:
        created(quota, h5ad=b'x' * 900, group='Bbb2026')
    assert exc.value.status == 413 and exc.value.body['status'] == 'quota_exceeded'

    limited = make_service(tmp_path / 'r', clock, presign_per_minute=2, max_open_per_user=10)
    created(limited, group='Aaa2026')
    created(limited, group='Bbb2026')
    with pytest.raises(dd.DepositError) as exc:
        created(limited, group='Ccc2026')
    assert exc.value.status == 429 and exc.value.headers['Retry-After']
    clock[0] += 61
    created(limited, group='Ccc2026')


def test_two_datasets_of_one_new_group_use_their_own_index_seed(tmp_path, clock):
    service = make_service(tmp_path, clock)
    created(service)
    with pytest.raises(dd.DepositError) as exc:                          # index.md is in the open deposit
        created(service, basename='more')
    assert exc.value.body['status'] == 'key_in_open_deposit' and 'index.md' in exc.value.body['message']
    body, _ = simple_deposit(basename='more')
    for entry in body['files']:
        if entry['kind'] == 'index_md':
            entry['name'] = 'more.index.md'
    assert service.create(ALICE, body)['state'] == 'uploading'


def test_a_key_in_an_open_deposit_or_owned_by_another_user_is_refused(tmp_path, clock):
    service = make_service(tmp_path, clock)
    response, payloads = created(service)
    with pytest.raises(dd.DepositError) as exc:
        created(service, BOB)
    assert exc.value.status == 409 and exc.value.body['status'] == 'key_in_open_deposit'
    assert response['deposit_id'] not in exc.value.body['message']       # another user's id is not revealed
    upload(service.s3, response, payloads)
    service.complete(ALICE, response['deposit_id'])
    with pytest.raises(dd.DepositError) as exc:
        created(service, BOB)
    assert exc.value.status == 409 and exc.value.body['status'] == 'key_taken'
    service.s3.put(f'{PREFIX}Lab2025/x.h5ad', b'from mus')                 # a key-holder's upload
    with pytest.raises(dd.DepositError) as exc:
        created(service, BOB, group='Lab2025', basename='x')
    assert exc.value.body['status'] == 'key_taken'
    created(service, ADMIN, group='Lab2025', basename='x')                 # admins may replace


# ── updates and corrected sidecars ──────────────────────────────────────────

def test_update_rules(tmp_path, clock):
    service = make_service(tmp_path, clock)
    for update_of, status, word in ((None, 409, 'update_of_required'), ('OL2/xyz', 409, 'update_of_mismatch'),
                                    (STORE_DB_ID, 403, 'not_your_dataset')):
        with pytest.raises(dd.DepositError) as exc:
            created(service, group='Old2020', basename='micro', update_of=update_of)
        assert (exc.value.status, exc.value.body['status']) == (status, word)
    with pytest.raises(dd.DepositError) as exc:
        created(service, update_of='NW6/cells')
    assert exc.value.body['status'] == 'update_of_unknown'
    assert created(service, ADMIN, group='Old2020', basename='micro', update_of=STORE_DB_ID)[0]['update_of'] == STORE_DB_ID
    # the user whose deposit published the dataset may deposit a new version
    owner = make_service(tmp_path / 'o', clock)
    owner.store.insert({'id': 'dep_' + 'a' * 20, 'user': 'alice', 'data_project': 'Old2020', 'basename': 'micro',
                        'state': 'uploaded', 'watch_state': 'live', 'db_id': STORE_DB_ID, 'created': 1.0,
                        'completed': 1.0, 'expires': 1.0}, [])
    assert created(owner, group='Old2020', basename='micro', update_of=STORE_DB_ID.lower())[0]['state'] == 'uploading'
    anyone = make_service(tmp_path / 'p', clock, update_policy='any')
    assert created(anyone, BOB, group='Old2020', basename='micro', update_of=STORE_DB_ID)[0]['state'] == 'uploading'


def test_a_corrected_sidecar_continues_an_own_deposit(tmp_path, clock):
    service = make_service(tmp_path, clock)
    response, payloads = created(service)
    upload(service.s3, response, payloads)
    service.complete(ALICE, response['deposit_id'])
    sidecar = sidecar_for(sha256_bytes(payloads['cells.h5ad']), license='CC0-1.0')
    body = {'data_project': 'New2026', 'basename': 'cells', 'files': [file_entry('cells.h5ad.mus', sidecar, 'mus')]}
    with pytest.raises(dd.DepositError) as exc:
        service.create(BOB, body)
    assert exc.value.body['status'] == 'h5ad_required'
    follow = service.create(ALICE, body)
    assert follow['continues'] == response['deposit_id']
    upload(service.s3, follow, {'cells.h5ad.mus': sidecar})
    assert service.complete(ALICE, follow['deposit_id'])['state'] == 'uploaded'
    record = service.store.get(follow['deposit_id'])
    assert record['h5ad_etag'] == service.store.get(response['deposit_id'])['h5ad_etag']


# ── status from the watcher, redaction ──────────────────────────────────────

def _watch_row(tmp_path, record, **fields):
    db = ingest_watch.StateDB(tmp_path / 'state' / dd.WATCH_DB_FILE)
    db.upsert(record['watch_key'], state=fields.pop('state'), h5ad_etag=record['h5ad_etag'],
              mus_etag=record['mus_etag'], data_project=record['data_project'], basename=record['basename'], **fields)
    db.close()


def test_status_follows_the_watcher_and_is_redacted(tmp_path, clock):
    service = make_service(tmp_path, clock)
    response, payloads = created(service)
    upload(service.s3, response, payloads)
    service.complete(ALICE, response['deposit_id'])
    record = service.store.get(response['deposit_id'])
    report_path = tmp_path / 'state' / 'reports' / 'r.json'
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps({
        'result': 'pass', 'publish': 'held', 'held_reasons': ['data_use_unknown'], 'counts': {'error': 0},
        'checks': [{'id': 'access.controlled', 'status': 'warn', 'summary': f'see {tmp_path}/store/New2026/x.md',
                    'fix': 'set data_use='}],
        'admin_detail': {'donor.overlap_ids': {'matched': ['M23/AST']}}}))
    _watch_row(tmp_path, record, state='pulled')
    assert service.status(ALICE, record['id'])['state'] == 'checking'
    _watch_row(tmp_path, record, state='held', held_reasons=json.dumps(['data_use_unknown']),
               error=f'held for an admin: data_use_unknown; staged in {tmp_path}/work/abc/New2026',
               report_path=str(report_path))
    status = service.status(ALICE, record['id'])
    assert status['state'] == 'held' and status['held_reasons'] == ['data_use_unknown']
    assert str(tmp_path) not in json.dumps(status)
    assert status['report']['problems'][0]['id'] == 'access.controlled'
    report = service.report(ALICE, record['id'])
    assert 'admin_detail' not in report and 'M23/AST' not in json.dumps(report)
    assert str(tmp_path) not in json.dumps(report) and '<store>/New2026/x.md' in json.dumps(report)
    # a newer upload of the same key by someone else: this deposit keeps its last known state
    service.store.record_watch(record['id'], {'state': 'held', 'held_reasons': '["data_use_unknown"]'})
    _watch_row(tmp_path, {**record, 'h5ad_etag': 'other'}, state='seen')
    assert service.status(ALICE, record['id'])['state'] == 'held'


def test_sanitize_text():
    roots = {'store': '/srv/store'}
    assert dd.sanitize_text('/srv/store/G/x.md exists', roots) == '<store>/G/x.md exists'
    assert dd.sanitize_text('in /home/u/work/abc/x.h5ad: bad') == 'in <path>/x.h5ad: bad'
    assert dd.sanitize_text('see https://example.org/a/b and __incoming/G/x.h5ad') == \
        'see https://example.org/a/b and __incoming/G/x.h5ad'
    assert dd.sanitize_text(None) is None


def test_vocab_is_the_servers_vocabulary(tmp_path, clock):
    import mus

    vocab = make_service(tmp_path, clock).vocab()
    assert vocab['mus']['required'] == mus.required_fields()
    assert vocab['mus']['vocabulary']['access'] == ['internal', 'public']
    for key in ('data_use', 'license', 'depositor', 'update_of', 'previous_sha256', 'override_<key>', 'sha256'):
        assert key in vocab['sidecar_keys']
    assert 'version' in vocab['seed_md']['leave_out'] and vocab['seed_md']['template'].startswith('---')
    assert vocab['uploads_enabled'] is True and vocab['limits']['max_gb'] == 200.0
