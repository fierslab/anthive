"""Server fixes from the skills-only novice test (2026-10-15): fresh updates are not superseded, DE term and
contrast checked at complete with depositor wording, integer years, no admin instructions in depositor reports,
the caller in the vocab, the accepted de_dataset_id forms."""
from __future__ import annotations

import json
import time

import pytest
import yaml

import dataset_deposit as dd
from ingest_pipeline import de_sidecar_names, de_sidecar_problems, friendly_de_error
from tests.deposit_helpers import ADMIN, ALICE, BOB, file_entry, make_service, sha256_bytes, upload
from tests.ingest_watch_helpers import INDEX_SEED, PREFIX
from tests.test_dataset_deposit_vark import de_body, de_files
from tests.test_dataset_deposit_watch import GROUP, deposit, make_env


# 1 ─ a completed update deposit is uploaded, not superseded

def test_a_fresh_update_reads_uploaded_until_the_watcher_polls(tmp_path):
    env, service, clock = make_env(tmp_path)
    deposit(env, service)
    assert env.poll_twice() == {'live': 1}
    clock[0] = env.clock_value = time.time() + 60          # the update is completed after the old row was written
    second = deposit(env, service, seed=1, index=False, update_of='WT6/MIC')
    assert service.status(ALICE, second)['state'] == 'uploaded'
    env.clock_value += 10
    assert env.poll_twice() == {'live': 1}
    assert service.status(ALICE, second)['state'] == 'live'


def test_superseded_only_when_a_newer_deposit_replaced_it(tmp_path):
    env, service, clock = make_env(tmp_path)
    first = deposit(env, service)
    clock[0] += 5
    newer = deposit(env, service, seed=1, index=False)   # replaces the files before the watcher saw the first
    env.watcher.poll()                                   # the watcher lists the newer objects
    assert service.status(ALICE, first)['state'] == 'superseded'
    assert service.status(ALICE, newer)['state'] in ('seen', 'stable')


# 2 ─ DE term and contrast

def test_a_deseq2_table_without_term_and_contrast_is_refused_at_complete(tmp_path):
    env, service, _ = make_env(tmp_path)
    env.deposit()
    env.poll_twice()
    table = b'gene\tlog2FoldChange\tpvalue\tpadj\nTREM2\t1\t0.1\t0.2\n'
    sidecar = f'de_format=deseq2\nde_dataset_id=MIC\nsha256={sha256_bytes(table)}\n'.encode()
    payloads = {'deseq/t.tsv': table, 'deseq/t.tsv.mus': sidecar}
    response = service.create(ADMIN, de_body(payloads))
    upload(env.s3, response, payloads)
    with pytest.raises(dd.DepositError) as exc:
        service.complete(ADMIN, response['deposit_id'])
    message = exc.value.body['message']
    assert exc.value.status == 422 and 'de_term=' in message and 'de_contrast=' in message
    assert '--term' not in message


def test_de_sidecar_problems_follow_the_preset():
    assert de_sidecar_problems({'de_format': 'deseq2', 'de_term': 't', 'de_contrast': 'c'}) == []
    assert de_sidecar_problems({'de_format': 'long', 'de_method': 'lmem'}) == []
    assert 'de_method=' in de_sidecar_problems({'de_format': 'long'})[0]
    assert 'de_format must be one of' in de_sidecar_problems({})[0]
    assert len(de_sidecar_problems({'de_format': 'edger'})) == 2


def test_de_tables_of_a_dataset_deposit_are_checked_too(tmp_path):
    service = make_service(tmp_path, [time.time()])
    from tests.deposit_helpers import simple_deposit
    table = b'gene\tlogFC\nTREM2\t1\n'
    body, payloads = simple_deposit(extra_files={'de1/t.tsv': (table, 'de'),
                                                 'de1/t.tsv.mus': (b'de_format=edger\n', 'de')})
    response = service.create(ALICE, body)
    upload(service.s3, response, payloads)
    with pytest.raises(dd.DepositError) as exc:
        service.complete(ALICE, response['deposit_id'])
    assert exc.value.body['status'] == 'de_mismatch' and 'de_term=' in exc.value.body['message']


def test_import_failures_are_told_in_sidecar_keys():
    tail = ("ant-de-ingest: preset 'deseq2' requires these CLI flags: --term, --contrast\n"
            "Run `ant-de-ingest --help-preset deseq2` for details.")
    text = friendly_de_error(tail)
    assert 'de_term=' in text and 'de_contrast=' in text and 'ant-de-ingest' not in text and '--term' not in text


# 3 ─ integer years

def test_a_quoted_year_is_written_as_a_number(tmp_path):
    env, service, _ = make_env(tmp_path)
    env.deposit(sidecar_extra={'year': '2024'})
    index = env.s3.objects[f'{PREFIX}{GROUP}/static/index.md']
    index['body'] = INDEX_SEED.replace('year: 2026', "year: '2026'").encode()
    import hashlib
    index['etag'] = hashlib.md5(index['body']).hexdigest()
    assert env.poll_twice() == {'live': 1}
    md = yaml.safe_load((env.store / GROUP / 'micro.md').read_text().split('---\n')[1])
    idx = yaml.safe_load((env.store / GROUP / 'index.md').read_text().split('---\n')[1])
    assert md['year'] == 2024 and idx['year'] == 2026


# 4 ─ no admin instructions for depositors

def test_depositor_reports_hold_no_admin_instructions():
    report = {'checks': [
        {'id': 'donor.overlap_ids', 'status': 'skip', 'summary': 'no overlap index; an admin runs `ant ingest-index build`'},
        {'id': 'genes.ids', 'status': 'pass', 'summary': "no duplicates; the share of known genes is not judged (no gene "
                                                          "universe for organism 'human'; run ant ingest-index)"},
        {'id': 'scores.modules_complete', 'status': 'warn', 'summary': 'no scores sidecar yet',
         'fix': 'Scoring runs after the conversion (ant-score-fleet run --only <db_id>); score before the facets.'}]}
    view = json.dumps(dd.redacted_report(report))
    assert 'ant ingest-index' not in view and 'ant-score-fleet' not in view and 'an admin runs' not in view
    assert 'nothing for you to do' in view


# 5 ─ the caller in the vocab

def test_the_vocab_says_what_the_caller_may_do(tmp_path):
    service = make_service(tmp_path, [time.time()])
    assert service.vocab(ALICE)['caller'] == {'user': 'alice', 'is_admin': False, 'may_replace_de_study': False,
                                              'may_list_all_deposits': False, 'may_update_any_dataset': False}
    assert service.vocab(ADMIN)['caller']['is_admin'] is True
    assert service.vocab()['caller'] is None


# 6 ─ de_dataset_id forms

def test_de_dataset_id_accepts_the_file_id_and_the_db_id(tmp_path):
    assert de_sidecar_names({'de_dataset_id': 'smi'}, 'SMI', 'x', 'ST6/SMI')
    assert de_sidecar_names({'de_dataset_id': 'st6/smi'}, 'SMI', 'x', 'ST6/SMI')
    assert not de_sidecar_names({'de_dataset_id': 'XX6/SMI'}, 'SMI', 'x', 'ST6/SMI')
    vocab = make_service(tmp_path, [time.time()]).vocab()
    assert 'file id' in vocab['de_only'] and 'de_term' in vocab['de_sidecar_keys']
