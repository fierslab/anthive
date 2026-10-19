"""de_table artefacts (doc/DESIGN_artefacts.md sections 5, 6.2 and 13, phase A3): the DE checks, the
server rules, the de_only alias, the all-or-nothing import into the dataset with the real ``ant-de-ingest``,
kills between steps, purge (the study leaves the dataset as a patch version with a caveat) and the migration
of ``de_sources/``.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import time
from pathlib import Path

import duckdb
import pytest
import yaml

import artefact_de
import artefact_migrate
import artefacts
import dataset_deposit as dd
from tests.artefact_deposit_helpers import (ADMIN, ANALYST, CATALOG, DE_INTERNAL, GENES, OTHER, PREFIX,
                                            artefact_body, context, de_files, de_sidecar, deposit, deseq2_table,
                                            listing, make_dataset_duckdb, make_publisher, make_service,
                                            poll_until_final, upload, watch_row)

MD = 'Public2021/int.md'


class Killed(BaseException):
    """SIGKILL stand-in: no handler in the publisher catches it."""


@pytest.fixture
def clock():
    return [time.time()]


def _setup(tmp_path, clock, **kwargs):
    service, s3 = make_service(tmp_path, clock)
    make_dataset_duckdb(tmp_path / 'store')
    publisher, db, git, api = make_publisher(tmp_path, s3, clock, **kwargs)
    return service, s3, publisher, db, git, api


def _studies(tmp_path):
    connection = duckdb.connect(str(tmp_path / 'store' / 'Public2021' / 'int.duckdb'), read_only=True)
    try:
        tables = {row[0] for row in connection.execute('SELECT table_name FROM information_schema.tables').fetchall()}
        if '__de_study' not in tables:
            return {}
        return {row[0]: row[1] for row in connection.execute(
            'SELECT s.study_id, count(d.gene_id) FROM __de_study s LEFT JOIN __de d USING (study_id) '
            'GROUP BY s.study_id').fetchall()}
    finally:
        connection.close()


def _md(tmp_path):
    return yaml.safe_load((tmp_path / 'store' / MD).read_text().split('---\n')[1])


# ── the DE checks ─────────────────────────────────────────────────────────────

def _check(tmp_path, table: bytes, name='t.tsv', genes=set(GENES), **meta):
    path = tmp_path / name
    path.write_bytes(table)
    sidecar = {'de_format': 'deseq2', 'de_term': 'treatment', 'de_contrast': 'Treated_vs_Control',
               'de_method': 'DESeq2', 'de_dataset_id': 'int', **meta}
    sidecar = {key: value for key, value in sidecar.items() if value is not None}
    return artefact_de.check_table(path, name, name + '.mus', sidecar, file_id='int', dataset_basename='int',
                                   db_id='PU1/int', genes=genes, study_id='s')


def _status(result, check_id):
    return [entry['status'] for entry in result.checks if entry['id'] == check_id]


def test_a_good_table_passes(tmp_path):
    result = _check(tmp_path, deseq2_table())
    assert not [entry for entry in result.checks if entry['status'] in ('fail', 'warn')], result.checks
    assert result.rows == len(GENES) and result.genes_matched == 1.0
    assert result.sign_convention == 'positive log2 fold change = higher in Treated than in Control'


def test_p_values_out_of_range_and_order(tmp_path):
    table = deseq2_table().replace(b'0.0100\t0.0200', b'1.5\t-0.1')
    assert _status(_check(tmp_path, table), 'de.pvalues') == ['fail']
    swapped = b'\n'.join(line if index == 0 else b'\t'.join(line.split(b'\t')[:4] + [line.split(b'\t')[5],
                                                                                     line.split(b'\t')[4]])
                         for index, line in enumerate(deseq2_table().strip().split(b'\n'))) + b'\n'
    assert _status(_check(tmp_path, swapped), 'de.padj_order') == ['warn']
    table = deseq2_table().replace(b'-1.000', b'inf')
    assert _status(_check(tmp_path, table), 'de.effects') == ['fail']
    na = deseq2_table().replace(b'0.0200\n', b'NA\n')
    assert _status(_check(tmp_path, na), 'de.pvalues') == ['pass']        # DESeq2's NA padj is not an error


def test_genes_must_be_genes_of_the_dataset(tmp_path):
    other = [f'X{index}' for index in range(20)]
    result = _check(tmp_path, deseq2_table(other))
    assert _status(result, 'de.genes') == ['fail']
    half = GENES[:17] + ['X1', 'X2', 'X3']
    assert _status(_check(tmp_path, deseq2_table(half)), 'de.genes') == ['warn']
    upper = [gene.upper() for gene in GENES]
    assert 'ignoring case' in next(e['summary'] for e in _check(tmp_path, deseq2_table(upper)).checks
                                   if e['id'] == 'de.genes')
    assert _status(_check(tmp_path, deseq2_table(), genes=None), 'de.genes') == ['warn']


def test_duplicates_rows_and_columns(tmp_path):
    duplicated = deseq2_table() + b'Apoe\t1\t0.1\t0.1\t0.5\t0.6\n'
    assert _status(_check(tmp_path, duplicated), 'de.duplicates') == ['warn']
    assert _status(_check(tmp_path, b'gene\tbaseMean\tlog2FoldChange\tlfcSE\tpvalue\tpadj\n'), 'de.rows') == ['fail']
    scanpy_like = b'names\tgroup\tlogfoldchanges\tpvals\tpvals_adj\nApoe\tA\t1\t0.1\t0.2\n'
    result = _check(tmp_path, scanpy_like)
    failure = next(entry for entry in result.checks if entry['id'] == 'de.columns')
    assert failure['status'] == 'fail' and 'scanpy' in failure['fix']


def test_sidecar_rules(tmp_path):
    assert _status(_check(tmp_path, deseq2_table(), de_term=None), 'de.sidecar') == ['fail']
    assert _status(_check(tmp_path, deseq2_table(), de_contrast='auto'), 'de.sidecar') == ['fail']
    assert _status(_check(tmp_path, deseq2_table(), de_format='nonsense'), 'de.sidecar') == ['fail']
    assert _status(_check(tmp_path, deseq2_table(), de_dataset_id='XYZ'), 'de.dataset') == ['fail']
    assert _status(_check(tmp_path, deseq2_table(), abspath='/lustre1/x'), 'de.sidecar_local_keys') == ['warn']


def test_sign_convention_is_a_warning_when_unclear(tmp_path):
    result = _check(tmp_path, deseq2_table(), de_contrast='treatment_effect')
    assert _status(result, 'de.sign_convention') == ['warn'] and result.sign_convention is None
    assert artefact_de.sign_convention('HM.1_vs_HM.2').endswith('higher in HM.1 than in HM.2')
    assert artefact_de.sign_convention('lecanemab vs igg1').endswith('higher in lecanemab than in igg1')


def test_index_column_and_gzip_like_the_real_tables(tmp_path):
    # Sun2026 style: an unnamed first column (the gene), mapped with de_map_gene=column0
    lines = ['\tbaseMean\tlog2FoldChange\tlfcSE\tpvalue\tpadj'] + [f'{gene}\t1\t0.5\t0.1\t0.01\t0.02' for gene in GENES]
    table = gzip.compress(('\n'.join(lines) + '\n').encode())
    result = _check(tmp_path, table, name='t.tsv.gz', de_map_gene='column0')
    assert _status(result, 'de.columns') == ['pass'] and result.genes_matched == 1.0


def test_long_format_with_many_contrasts(tmp_path):
    lines = ['gene_id\teffsize\tpvalue\tpadj\tterm\tcontrast']
    lines += [f'{gene}\t0.5\t0.01\t0.02\tall\tARM_vs_HM1' for gene in GENES]
    lines += [f'{gene}\t0.5\t0.01\t0.02\tall\tHM1_vs_HM2' for gene in GENES]
    result = _check(tmp_path, ('\n'.join(lines) + '\n').encode(), de_format='long', de_term=None, de_contrast=None,
                    de_method='wilcoxon', de_map_gene='gene_id', de_map_log2fc='effsize')
    assert not [entry for entry in result.checks if entry['status'] == 'fail'], result.checks
    assert 'first group' in result.sign_convention


# ── server rules ──────────────────────────────────────────────────────────────

def _refused(service, body, status, word, identity=ANALYST):
    with pytest.raises(dd.DepositError) as caught:
        service.create(identity, body, context(service.store_root))
    assert (caught.value.status, caught.value.body['status']) == (status, word), caught.value.body


def test_de_table_file_rules(tmp_path, clock):
    service, _s3 = make_service(tmp_path, clock)
    files = de_files()
    _refused(service, artefact_body({'deseq2/x.tsv': deseq2_table()}, **DE_INTERNAL), 422, 'invalid')
    _refused(service, artefact_body({**files, 'deseq2/y.tsv.mus': b'x=1\n'}, **DE_INTERNAL), 422, 'invalid')
    _refused(service, artefact_body(files, **{**DE_INTERNAL, 'scope': '_group', 'made_from': []}), 422,
             'invalid_scope')
    body = artefact_body(files, **DE_INTERNAL)
    body['files'][0]['role'] = 'sidecar'
    _refused(service, body, 422, 'invalid')
    answer = service.create(ANALYST, artefact_body(files, **DE_INTERNAL), context(service.store_root))
    block = json.loads(service.store.get(answer['deposit_id'])['artefact'])
    assert block['dataset_basename'] == 'int'
    assert {entry['name']: entry['role'] for entry in block['files']} == {
        'deseq2/treated_vs_control.tsv': 'main', 'deseq2/treated_vs_control.tsv.mus': 'sidecar'}


@pytest.mark.parametrize('extra, fragment', [
    ({'de_term': None}, 'de_term'), ({'de_dataset_id': 'OTHER'}, 'names neither'),
])
def test_complete_reads_the_sidecars_back(tmp_path, clock, extra, fragment):
    service, s3 = make_service(tmp_path, clock)
    files = de_files(**extra)
    answer = service.create(ANALYST, artefact_body(files, **DE_INTERNAL), context(service.store_root))
    upload(s3, answer, files)
    with pytest.raises(dd.DepositError) as caught:
        service.complete(ANALYST, answer['deposit_id'])
    assert caught.value.body['status'] == 'de_mismatch' and fragment in caught.value.body['message']
    assert s3.keys(PREFIX) == []


def test_two_tables_with_one_study_id_are_refused_at_complete(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock)
    files = {**de_files('same', folder='a'), **de_files('same', folder='b')}
    answer = service.create(ANALYST, artefact_body(files, **DE_INTERNAL), context(service.store_root))
    upload(s3, answer, files)
    with pytest.raises(dd.DepositError) as caught:
        service.complete(ANALYST, answer['deposit_id'])
    assert 'same study id' in caught.value.body['message']


# ── the de_only alias ─────────────────────────────────────────────────────────

def _legacy_body(files):
    return {'data_project': 'Public2021', 'basename': 'int', 'update_of': 'PU1/int',
            'files': [{'name': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'kind': 'de'}
                      for name, data in files.items()]}


def test_a_2_46_de_only_body_becomes_a_de_table_artefact(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock)
    files = de_files('Treated_vs_Control.v2')
    answer = service.create(ANALYST, _legacy_body(files), context(service.store_root))
    assert answer['alias_of'] == 'de_only' and answer['deposit_kind'] == 'artefact'
    assert answer['artefact']['ref_without_version'] == 'PU1/int/treated-vs-control-v2'
    assert any('alias' in warning for warning in answer['warnings'])
    record = service.store.get(answer['deposit_id'])
    assert record['deposit_kind'] == 'artefact' and json.loads(record['artefact'])['kind'] == 'de_table'
    upload(s3, answer, files)
    assert service.complete(ANALYST, answer['deposit_id'])['state'] == 'uploaded'
    explicit = service.create(ANALYST, {**_legacy_body(de_files('second')), 'deposit_kind': 'de_only'},
                              context(service.store_root))
    assert explicit['alias_of'] == 'de_only'


def test_the_alias_follows_ownership(tmp_path, clock):
    from tests.artefact_helpers import write_artefact
    service, _s3 = make_service(tmp_path, clock)
    write_artefact(tmp_path / 'store', 'Public2021', 'PU1', 'int', 'de_table', 'treated-vs-control',
                   [{'files': {'a/treated_vs_control.tsv': b'x', 'a/treated_vs_control.tsv.mus': b'y'},
                     'manifest': {'made_from': [{'db_id': 'PU1/int', 'dataset_version': '1.0.0'}]}}], owner='otto')
    with pytest.raises(dd.DepositError) as caught:
        service.create(ANALYST, _legacy_body(de_files()), context(service.store_root))
    assert caught.value.body['status'] == 'artefact_exists'
    answer = service.create(OTHER, _legacy_body(de_files()), context(service.store_root))
    assert answer['artefact']['new_version_of'] == 1


def test_without_artefacts_the_alias_is_the_2_46_route(tmp_path, clock):
    service, _s3 = make_service(tmp_path, clock, artefact_settings={'artefacts_enabled': False}, datasets_enabled=True)
    (tmp_path / 'store' / 'Public2021' / 'int.h5ad').write_bytes(b'h5ad')
    answer = service.create(ADMIN, _legacy_body(de_files()), context(service.store_root))
    assert answer['deposit_kind'] == 'de_only' and 'alias_of' not in answer


# ── publication with the real ant-de-ingest ───────────────────────────────────

def test_a_de_table_goes_live_and_its_study_is_imported(tmp_path, clock):
    service, s3, publisher, db, git, api = _setup(tmp_path, clock)
    answer = deposit(service, s3, ANALYST, de_files(), artefact_id='tvc', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'live', row['error']
    assert _studies(tmp_path) == {'treated_vs_control': len(GENES)}
    meta = _md(tmp_path)
    assert meta['version'] == '1.0.1'
    assert 'artefact PU1/int/tvc@v1' in meta['changelog'][-1]['changes'][0]
    entry = context(tmp_path / 'store').index.get('PU1', 'int', 'tvc')
    de = entry.versions[1].manifest['de']
    assert de['imported']['study_ids'] == ['treated_vs_control'] and de['imported']['rows'] == {
        'treated_vs_control': len(GENES)} and de['imported']['dataset_version_after'] == '1.0.1'
    assert de['tables'][0]['sign_convention'].endswith('Treated than in Control')
    sidecar_key = f'{CATALOG}Public2021/int.de.parquet'
    assert sidecar_key in s3.objects
    commit = next(cmd for cmd in git.commands if 'commit' in cmd)
    assert MD in commit
    assert ('POST', '/admin/release/PU1/int') in api.calls and ('POST', '/admin/restore/PU1/int') in api.calls
    assert ('POST', '/admin/rescan') in api.calls


def test_a_new_version_replaces_its_own_study(tmp_path, clock):
    service, s3, publisher, db, git, api = _setup(tmp_path, clock)
    deposit(service, s3, ANALYST, de_files(), artefact_id='tvc', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    second = deposit(service, s3, ANALYST, de_files(table=deseq2_table(GENES[:15], scale=2)), artefact_id='tvc',
                     new_version_of=1, change_note='15 genes', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    assert watch_row(db, second['deposit_id'])['state'] == 'live'
    assert _studies(tmp_path) == {'treated_vs_control': 15}
    archived = list((tmp_path / 'store' / 'Public2021' / '_versions' / 'int' / 'de' / 'treated_vs_control').glob('*'))
    assert len(archived) == 1
    assert _md(tmp_path)['version'] == '1.0.2'


def test_a_foreign_study_is_refused_for_a_depositor_and_replaced_by_an_admin(tmp_path, clock):
    service, s3, publisher, db, git, api = _setup(tmp_path, clock)
    deposit(service, s3, ANALYST, de_files(), artefact_id='first', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    duckdb_before = (tmp_path / 'store' / 'Public2021' / 'int.duckdb').read_bytes()
    md_before = (tmp_path / 'store' / MD).read_text()
    clash = deposit(service, s3, OTHER, de_files(table=deseq2_table(GENES[:12])), artefact_id='second',
                    **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, clash['deposit_id'])
    assert row['state'] == 'failed' and 'already has the DE study' in row['error']
    assert (tmp_path / 'store' / 'Public2021' / 'int.duckdb').read_bytes() == duckdb_before
    assert (tmp_path / 'store' / MD).read_text() == md_before
    assert not any('/second/v1/files/' in key for key in s3.objects)          # the abandoned copies are gone
    admin = deposit(service, s3, ADMIN, de_files(table=deseq2_table(GENES[:12])), artefact_id='third',
                    **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    assert watch_row(db, admin['deposit_id'])['state'] == 'live'
    assert _studies(tmp_path) == {'treated_vs_control': 12}


def test_failed_checks_change_nothing(tmp_path, clock):
    service, s3, publisher, db, git, api = _setup(tmp_path, clock)
    duckdb_before = (tmp_path / 'store' / 'Public2021' / 'int.duckdb').read_bytes()
    answer = deposit(service, s3, ANALYST, de_files(table=deseq2_table([f'X{i}' for i in range(20)])),
                     artefact_id='tvc', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'failed'
    assert any(entry['id'] == 'de.genes' and entry['status'] == 'fail'
               for entry in service.report(ANALYST, answer['deposit_id'])['checks'])
    assert (tmp_path / 'store' / 'Public2021' / 'int.duckdb').read_bytes() == duckdb_before
    assert s3.keys(CATALOG) == []


def test_an_import_failure_changes_nothing_and_records_the_failed_version(tmp_path, clock):
    class FailingRunner:
        def run(self, label, cmd, cwd=None, timeout=None):
            return 1, 'ant-de-ingest: --term is required'
    service, s3, publisher, db, git, api = _setup(tmp_path, clock, de_runner=FailingRunner())
    duckdb_before = (tmp_path / 'store' / 'Public2021' / 'int.duckdb').read_bytes()
    md_before = (tmp_path / 'store' / MD).read_text()
    answer = deposit(service, s3, ANALYST, de_files(), artefact_id='tvc', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    row = watch_row(db, answer['deposit_id'])
    assert row['state'] == 'failed' and 'de_term=' in row['error'] and 'Nothing was changed' in row['error']
    assert (tmp_path / 'store' / 'Public2021' / 'int.duckdb').read_bytes() == duckdb_before
    assert (tmp_path / 'store' / MD).read_text() == md_before
    head_key = f'{CATALOG}Public2021/static/artefacts/int/de_table/tvc/artefact.md'
    assert s3.keys(CATALOG) == [head_key] and publisher.load_marker(answer['deposit_id']) is None
    head = artefacts.parse_head_record(s3.objects[head_key]['body'].decode())
    assert [(v['version'], v['state']) for v in head['versions']] == [(1, 'failed')] and head['latest'] is None
    # a corrected deposit is published with the real tool as v2 (a failed number is never reused)
    fresh, db2, _git, _api = make_publisher(tmp_path, s3, clock)
    retry = deposit(service, s3, ANALYST, de_files(), artefact_id='tvc', **DE_INTERNAL)
    poll_until_final(fresh, s3, clock)
    assert watch_row(db2, retry['deposit_id'])['version'] == 'v2'


@pytest.mark.parametrize('step', ['before_import', 'after_import'])
def test_a_kill_around_the_import_never_imports_twice(tmp_path, clock, monkeypatch, step):
    import ingest_pipeline
    service, s3, publisher, db, git, api = _setup(tmp_path, clock)
    answer = deposit(service, s3, ANALYST, de_files(), artefact_id='tvc', **DE_INTERNAL)
    real = ingest_pipeline.import_de_all_or_nothing

    def dies(**kwargs):
        if step == 'before_import':
            raise Killed()
        result = real(**kwargs)
        raise Killed()
    monkeypatch.setattr(ingest_pipeline, 'import_de_all_or_nothing', dies)
    with pytest.raises(Killed):
        poll_until_final(publisher, s3, clock)
    monkeypatch.undo()
    fresh, db2, _git, _api = make_publisher(tmp_path, s3, clock)
    poll_until_final(fresh, s3, clock)
    row = watch_row(db2, answer['deposit_id'])
    assert row['state'] == 'live' and row['version'] == 'v1', row['error']
    meta = _md(tmp_path)
    entries = [entry for entry in meta['changelog'] if 'artefact PU1/int/tvc@v1' in entry['changes'][0]]
    assert len(entries) == 1 and meta['version'] == '1.0.1'
    assert _studies(tmp_path) == {'treated_vs_control': len(GENES)}


# ── purge ─────────────────────────────────────────────────────────────────────

def _purge(service, identity, artefact_id, version):
    return service.request_purge(identity, context(service.store_root), 'PU1', 'int', artefact_id, f'v{version}',
                                 {'reason': 'wrong contrast', 'confirm': f'PU1/int/{artefact_id}@v{version}'})


def test_purging_a_de_table_removes_its_study_with_a_caveat(tmp_path, clock):
    service, s3, publisher, db, git, api = _setup(tmp_path, clock)
    deposit(service, s3, ANALYST, de_files(), artefact_id='tvc', **DE_INTERNAL)
    deposit(service, s3, ANALYST, de_files('other_study'), artefact_id='other', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    assert set(_studies(tmp_path)) == {'treated_vs_control', 'other_study'}
    _purge(service, ANALYST, 'tvc', 1)
    publisher.poll(listing(s3))
    assert service.purges(ADMIN)[0]['state'] == 'done'
    assert set(_studies(tmp_path)) == {'other_study'}
    meta = _md(tmp_path)
    assert 'DE study treated_vs_control removed: purged artefact PU1/int/tvc@v1' in meta['changelog'][-1]['changes'][0]
    assert any('purged artefact PU1/int/tvc@v1' in caveat['message'] for caveat in meta['caveats'])
    assert context(tmp_path / 'store').index.get('PU1', 'int', 'tvc').versions[1].state == 'purged'


def test_purging_the_last_study_removes_the_sidecar(tmp_path, clock):
    service, s3, publisher, db, git, api = _setup(tmp_path, clock)
    deposit(service, s3, ANALYST, de_files(), artefact_id='tvc', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    assert (tmp_path / 'store' / 'Public2021' / 'int.de.parquet').exists()
    _purge(service, ADMIN, 'tvc', 1)
    publisher.poll(listing(s3))
    assert _studies(tmp_path) == {}
    assert not (tmp_path / 'store' / 'Public2021' / 'int.de.parquet').exists()
    assert f'{CATALOG}Public2021/int.de.parquet' not in s3.objects


def test_purging_an_old_version_keeps_the_study_a_newer_one_imported(tmp_path, clock):
    service, s3, publisher, db, git, api = _setup(tmp_path, clock)
    deposit(service, s3, ANALYST, de_files(), artefact_id='tvc', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    deposit(service, s3, ANALYST, de_files(table=deseq2_table(GENES[:15])), artefact_id='tvc', new_version_of=1,
            change_note='fewer genes', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    version_before = _md(tmp_path)['version']
    _purge(service, ANALYST, 'tvc', 1)
    publisher.poll(listing(s3))
    assert _studies(tmp_path) == {'treated_vs_control': 15}
    assert _md(tmp_path)['version'] == version_before                  # nothing removed, no bump


def test_a_kill_after_the_study_removal_finishes_the_purge(tmp_path, clock, monkeypatch):
    service, s3, publisher, db, git, api = _setup(tmp_path, clock)
    deposit(service, s3, ANALYST, de_files(), artefact_id='tvc', **DE_INTERNAL)
    poll_until_final(publisher, s3, clock)
    _purge(service, ADMIN, 'tvc', 1)
    real = publisher._guarded_delete

    def dies(*args, **kwargs):
        raise Killed()
    monkeypatch.setattr(publisher, '_guarded_delete', dies)
    with pytest.raises(Killed):
        publisher.poll(listing(s3))
    monkeypatch.undo()
    assert _studies(tmp_path) == {}
    fresh, _db, _git, _api = make_publisher(tmp_path, s3, clock)
    fresh.poll(listing(s3))
    assert service.purges(ADMIN)[0]['state'] == 'done'
    meta = _md(tmp_path)
    assert sum('removed: purged artefact' in entry['changes'][0] for entry in meta['changelog']) == 1


# ── migration of de_sources ───────────────────────────────────────────────────

def _sources(tmp_path):
    store = tmp_path / 'store'
    folder = store / 'Public2021' / 'de_sources' / 'int' / 'deseq2'
    folder.mkdir(parents=True)
    (folder / 'treated_vs_control.tsv').write_bytes(deseq2_table())
    (folder / 'treated_vs_control.tsv.mus').write_bytes(de_sidecar() + b'abspath=/lustre/x\neln_study_id=7\n')
    legacy = store / 'Public2021' / 'de_sources' / 'old'
    legacy.mkdir(parents=True)
    (legacy / 'orphan.tsv').write_bytes(deseq2_table())
    (legacy / 'orphan.tsv.mus').write_bytes(de_sidecar('orphan', de_dataset_id='NOPE'))
    (legacy / 'nosidecar.tsv').write_bytes(deseq2_table())
    return store


def _snapshot(root: Path):
    return {str(path): path.read_bytes() for path in sorted(root.rglob('*')) if path.is_file()}


def test_migration_dry_run_writes_nothing(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock)
    store = _sources(tmp_path)
    make_dataset_duckdb(store)
    before = _snapshot(store)
    items = artefact_migrate.plan(store)
    rows = {Path(item.table).name: item.as_dict(store) for item in items}
    assert rows['treated_vs_control.tsv']['ref'] == 'PU1/int/treated-vs-control'
    assert rows['treated_vs_control.tsv']['skip'] is None
    assert rows['treated_vs_control.tsv']['study_in_duckdb'] is False
    assert {'abspath', 'eln_study_id'} <= set(rows['treated_vs_control.tsv']['sidecar_keys_dropped'])
    assert rows['orphan.tsv']['skip'].startswith('the dataset') and rows['nosidecar.tsv']['skip']
    assert _snapshot(store) == before and s3.calls == []


def test_migration_apply_writes_valid_artefacts_and_never_overwrites(tmp_path, clock):
    service, s3 = make_service(tmp_path, clock)
    store = _sources(tmp_path)
    make_dataset_duckdb(store)
    written = artefact_migrate.apply(store, artefact_migrate.plan(store), s3, 'bucket', CATALOG, site_id='t',
                                     log=lambda message: None)
    assert written == ['PU1/int/treated-vs-control']
    entry = context(store).index.get('PU1', 'int', 'treated-vs-control')
    assert entry is not None and entry.latest == 1 and entry.owner == 'migration'
    manifest = entry.versions[1].manifest
    sidecar = next(item for item in manifest['files'] if item['role'] == 'sidecar')
    key = artefacts.s3_key(CATALOG, entry.path.file_rel(1, sidecar['name']))
    assert b'abspath' not in s3.objects[key]['body'] and b'de_format=deseq2' in s3.objects[key]['body']
    assert manifest['recorded']['uploaded_by'] == 'migration'
    assert (store / 'Public2021' / 'de_sources').is_dir()                 # never removed here
    again = artefact_migrate.plan(store)
    assert all(item.skip for item in again if item.table.name == 'treated_vs_control.tsv')
