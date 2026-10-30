"""lib/journal.py: the dataset journal's store and rules (doc/DESIGN_dataset_journal.md)."""
from __future__ import annotations

import pytest

import journal
from journal import Actor, JournalError, JournalService, JournalStore


class Clock:
    def __init__(self, now: float = 1_800_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


ALICE = Actor('alice')
BOB = Actor('bob')
CURATOR = Actor('carol', may_triage=True)
ADMIN = Actor('root', is_admin=True)
ANON = Actor('')


def everything(target_type: str, target_id: str) -> bool:
    return True


def no_internal(target_type: str, target_id: str) -> bool:
    return target_id not in ('S99/SEC', 'S99/_group/secret-fig')


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def service(tmp_path, clock):
    return JournalService(JournalStore(tmp_path / 'journal.sqlite', clock=clock), retention_days=730,
                          writes_per_minute=0)


def test_note_roundtrip_with_links_refs_and_version(service):
    entry = service.add_note(ALICE, everything, 'dataset', 'S26/SLM', 'annotation-concern',
                             "I don't trust the annotation of cluster 7",
                             links=[{'url': 'https://eln.example.org/exp/12', 'label': 'ELN 12', 'type': 'eln'}],
                             refs={'column': 'cell_type', 'value': '7', 'evidence': 'P2RY12 absent'},
                             dataset_version='1.2.0')
    assert entry['status'] == 'open'
    assert entry['dataset_version'] == '1.2.0'
    assert entry['links'][0]['type'] == 'eln'
    assert entry['refs']['column'] == 'cell_type'
    assert entry['may_edit'] is True
    listed = service.list(BOB, everything, 'dataset', 'S26/SLM')
    assert [item['id'] for item in listed['entries']] == [entry['id']]
    assert listed['entries'][0]['may_edit'] is False


@pytest.mark.parametrize('links', [
    [{'url': 'javascript:alert(1)'}], [{'url': 'ftp://x'}], [{'url': 'https://x', 'type': 'nope'}], 'https://x'])
def test_links_http_only_and_typed(service, links):
    with pytest.raises(JournalError) as err:
        service.add_note(ALICE, everything, 'dataset', 'S26/SLM', 'comment', 'x', links=links)
    assert err.value.status == 400


def test_body_limits_and_bad_input(service):
    with pytest.raises(JournalError):
        service.add_note(ALICE, everything, 'dataset', 'S26/SLM', 'comment', '   ')
    with pytest.raises(JournalError) as err:
        service.add_note(ALICE, everything, 'dataset', 'S26/SLM', 'comment', 'x' * 10001)
    assert err.value.body['status'] == 'body_too_long'
    with pytest.raises(JournalError):
        service.add_note(ALICE, everything, 'dataset', 'S26/SLM', 'downloaded', 'events are not notes')
    with pytest.raises(JournalError):
        service.add_note(ALICE, everything, 'planet', 'X', 'comment', 'x')
    with pytest.raises(JournalError):
        service.add_note(ALICE, everything, 'dataset', '../etc', 'comment', 'x')
    with pytest.raises(JournalError):
        service.add_note(ALICE, everything, 'dataset', 'S26/SLM', 'comment', 'x', refs={'sql': 'drop'})
    with pytest.raises(JournalError):
        service.add_note(ALICE, everything, 'dataset', 'S26/SLM', 'link', 'no link given')


def test_anonymous_cannot_write(service):
    with pytest.raises(JournalError) as err:
        service.add_note(ANON, everything, 'dataset', 'S26/SLM', 'comment', 'hello')
    assert err.value.status == 401


def test_invisible_targets_are_404(service):
    with pytest.raises(JournalError) as err:
        service.add_note(ALICE, no_internal, 'dataset', 'S99/SEC', 'comment', 'x')
    assert err.value.status == 404
    entry = service.add_note(ADMIN, everything, 'dataset', 'S99/SEC', 'data-error', 'wrong sex labels')
    with pytest.raises(JournalError):
        service.get(ALICE, no_internal, entry['id'])
    with pytest.raises(JournalError):
        service.list(ALICE, no_internal, 'dataset', 'S99/SEC')
    assert service.open_issues(ALICE, no_internal)['issues'] == []
    assert len(service.open_issues(ADMIN, everything)['issues']) == 1


def test_replies_attach_to_the_root_and_have_no_status(service):
    root = service.add_note(ALICE, everything, 'dataset', 'S26/SLM', 'data-error', 'Braak column shifted')
    reply = service.add_note(BOB, everything, 'dataset', 'IGNORED/X', 'comment', 'confirmed', parent_id=root['id'])
    nested = service.add_note(ALICE, everything, 'dataset', 'S26/SLM', None, 'thanks', parent_id=reply['id'])
    assert reply['target_id'] == 'S26/SLM'
    assert nested['parent_id'] == root['id']
    assert 'status' not in reply
    with pytest.raises(JournalError):
        service.add_note(BOB, everything, 'dataset', 'S26/SLM', 'data-error', 'x', parent_id=root['id'])
    listed = service.list(ALICE, everything, 'dataset', 'S26/SLM')
    assert len(listed['entries']) == 1
    assert [item['body'] for item in listed['entries'][0]['replies']] == ['confirmed', 'thanks']


def test_only_the_author_edits_and_history_is_kept(service, clock):
    entry = service.add_note(ALICE, everything, 'dataset', 'S26/SLM', 'comment', 'first')
    with pytest.raises(JournalError) as err:
        service.edit(BOB, everything, entry['id'], body='hijack')
    assert err.value.status == 403
    with pytest.raises(JournalError):
        service.edit(ADMIN, everything, entry['id'], body='admin edit')
    clock.now += 10
    edited = service.edit(ALICE, everything, entry['id'], body='second')
    assert edited['body'] == 'second' and edited['edited']
    history = service.history(ALICE, everything, entry['id'])['history']
    assert history[0]['action'] == 'edit' and history[0]['before']['body'] == 'first'
    with pytest.raises(JournalError):
        service.history(BOB, everything, entry['id'])


def test_status_rules(service):
    issue = service.add_note(ALICE, everything, 'dataset', 'S26/SLM', 'data-error', 'sex swapped',
                             dataset_version='1.2.0')
    with pytest.raises(JournalError) as err:
        service.set_status(BOB, everything, issue['id'], 'acknowledged')
    assert err.value.status == 403
    with pytest.raises(JournalError) as err:
        service.set_status(ALICE, everything, issue['id'], 'fixed')
    assert err.value.status == 403          # the author is not a triager
    acked = service.set_status(CURATOR, everything, issue['id'], 'acknowledged', note='looking')
    assert acked['status'] == 'acknowledged' and acked['status_by'] == 'carol'
    with pytest.raises(JournalError) as err:
        service.set_status(CURATOR, everything, issue['id'], 'fixed')
    assert err.value.body['status'] == 'changelog_required'
    with pytest.raises(JournalError) as err:
        service.set_status(CURATOR, everything, issue['id'], 'fixed', changelog_ref='x', fixed_in_version='1.2.0')
    assert err.value.body['status'] == 'same_version'
    fixed = service.set_status(Actor('dora', owns=True), everything, issue['id'], 'fixed',
                               changelog_ref='1.2.1: sex labels corrected', fixed_in_version='1.2.1')
    assert fixed['status'] == 'fixed' and fixed['fixed_in_version'] == '1.2.1'
    assert service.open_issues(ADMIN, everything)['issues'] == []
    comment = service.add_note(ALICE, everything, 'dataset', 'S26/SLM', 'comment', 'nice')
    with pytest.raises(JournalError):
        service.set_status(ADMIN, everything, comment['id'], 'fixed')
    request = service.add_note(ALICE, everything, 'collection', 'S26', 'data-request', 'add the mouse data')
    assert service.set_status(ADMIN, everything, request['id'], 'fixed')['status'] == 'fixed'


def test_hide_is_admin_only_and_never_deletes(service):
    entry = service.add_note(ALICE, everything, 'geneset', 'Brown_Lysosome', 'comment', 'spam')
    with pytest.raises(JournalError):
        service.hide(ALICE, everything, entry['id'])
    service.hide(ADMIN, everything, entry['id'], reason='off topic')
    assert service.list(BOB, everything, 'geneset', 'Brown_Lysosome')['entries'] == []
    shown = service.list(ADMIN, everything, 'geneset', 'Brown_Lysosome')['entries']
    assert shown[0]['hidden'] is True and shown[0]['hidden_reason'] == 'off topic'
    with pytest.raises(JournalError):
        service.get(BOB, everything, entry['id'])
    service.hide(ADMIN, everything, entry['id'], hidden=False)
    assert service.get(BOB, everything, entry['id'])['body'] == 'spam'


def test_download_counts_for_all_names_for_admin_and_owner(service, clock):
    service.record_download('dataset', 'S26/SLM', 'h5ad', 'alice', '1.2.0', 'SLM.h5ad')
    service.record_download('dataset', 'S26/SLM', 'h5ad', 'alice', '1.2.0', 'SLM.h5ad')
    service.record_download('dataset', 'S26/SLM', 'static', '', None, 'paper.pdf')
    seen_by_bob = service.downloads(BOB, everything, 'dataset', 'S26/SLM')
    assert (seen_by_bob['n_downloads'], seen_by_bob['n_people'], seen_by_bob['n_anonymous']) == (3, 1, 1)
    assert 'rows' not in seen_by_bob and seen_by_bob['names_visible'] is False
    assert len(service.downloads(ADMIN, everything, 'dataset', 'S26/SLM')['rows']) == 3
    assert service.downloads(Actor('dora', owns=True), everything, 'dataset', 'S26/SLM')['names_visible']
    assert service.list(BOB, everything, 'dataset', 'S26/SLM')['downloads']['n_downloads'] == 3
    with pytest.raises(ValueError):
        service.record_download('dataset', 'S26/SLM', 'duckdb', 'alice')


def test_download_retention(service, clock):
    service.record_download('dataset', 'S26/SLM', 'h5ad', 'alice')
    clock.now += 731 * 86400
    service.record_download('dataset', 'S26/SLM', 'h5ad', 'bob')
    assert service.purge_due()
    assert service.purge_old_downloads() == 1
    assert not service.purge_due()
    assert service.store.download_summary('dataset', 'S26/SLM')['n_downloads'] == 1


def test_declared_use(service):
    used = service.declare_use(ALICE, everything, 'dataset', 'S26/SLM', body='WGCNA on microglia',
                               links=[{'url': 'https://eln.example.org/1', 'type': 'eln'}],
                               dataset_version='1.2.0', tool='vark 0.0.30')
    assert used['kind'] == 'event' and used['category'] == 'used-in-analysis'
    assert used['may_edit'] is False
    with pytest.raises(JournalError):
        service.declare_use(ANON, everything, 'dataset', 'S26/SLM')


def test_version_sync_baseline_then_events(service):
    assert service.sync_dataset_versions({'S26/SLM': '1.0.0', 'S20/SIE': '2.1.0'}) == 0
    assert service.list(ALICE, everything, 'dataset', 'S26/SLM')['entries'] == []
    owners = {('S26/SLM', '1.1.0'): 'dora'}
    written = service.sync_dataset_versions({'S26/SLM': '1.1.0', 'S20/SIE': '2.1.0', 'S30/NEW': '1.0.0'},
                                            depositor=lambda db_id, version: owners.get((db_id, version)))
    assert written == 2
    slm = service.list(ALICE, everything, 'dataset', 'S26/SLM')['entries'][0]
    assert (slm['category'], slm['author'], slm['dataset_version']) == ('deposited', 'dora', '1.1.0')
    new = service.list(ALICE, everything, 'dataset', 'S30/NEW')['entries'][0]
    assert (new['category'], new['author']) == ('version-published', 'anthive')
    assert service.sync_dataset_versions({'S26/SLM': '1.1.0', 'S20/SIE': '2.1.0', 'S30/NEW': '1.0.0'}) == 0


def test_artefact_events_hidden_when_the_artefact_is_not_visible(service):
    versions = [{'ref': 'S99/_group/secret-fig', 'version': 1, 'published': '2026-10-20T10:00:00Z',
                 'owner': 'erin', 'title': 'Figure 2', 'made_from': [{'db_id': 'S26/SLM', 'dataset_version': '1.0.0'}]},
                {'ref': 'S26/SLM/de-a', 'version': 2, 'published': '2026-10-21T10:00:00Z', 'owner': 'erin',
                 'title': 'DE', 'made_from': [{'db_id': 'S26/SLM', 'dataset_version': '1.0.0'}]}]
    assert service.sync_artefact_versions(versions) == 2
    assert service.sync_artefact_versions(versions) == 0
    seen = service.list(ALICE, no_internal, 'dataset', 'S26/SLM')['entries']
    assert [item['about']['id'] for item in seen] == ['S26/SLM/de-a']
    assert seen[0]['created'] == '2026-10-21T10:00:00Z'
    assert len(service.list(ADMIN, everything, 'dataset', 'S26/SLM')['entries']) == 2


def test_list_filters_and_paging(service):
    for number in range(5):
        service.add_note(ALICE, everything, 'dataset', 'S26/SLM', 'comment', f'c{number}')
    service.add_note(ALICE, everything, 'dataset', 'S26/SLM', 'question', 'q?')
    page = service.list(ALICE, everything, 'dataset', 'S26/SLM', category=['comment'], limit=3)
    assert [item['body'] for item in page['entries']] == ['c4', 'c3', 'c2']
    rest = service.list(ALICE, everything, 'dataset', 'S26/SLM', category=['comment'], before_id=page['next_before_id'])
    assert [item['body'] for item in rest['entries']] == ['c1', 'c0']
    with pytest.raises(JournalError):
        service.list(ALICE, everything, 'dataset', 'S26/SLM', status=['closed'])


def test_write_limiter(tmp_path, clock):
    limited = JournalService(JournalStore(tmp_path / 'j.sqlite', clock=clock), writes_per_minute=2)
    limited.add_note(ALICE, everything, 'dataset', 'S26/SLM', 'comment', 'a')
    limited.add_note(ALICE, everything, 'dataset', 'S26/SLM', 'comment', 'b')
    with pytest.raises(JournalError) as err:
        limited.add_note(ALICE, everything, 'dataset', 'S26/SLM', 'comment', 'c')
    assert err.value.status == 429
    limited.add_note(BOB, everything, 'dataset', 'S26/SLM', 'comment', 'other user is fine')
    clock.now += 61
    limited.add_note(ALICE, everything, 'dataset', 'S26/SLM', 'comment', 'later is fine')


def test_config_settings(tmp_path, monkeypatch):
    import config
    cfg = {'state_dir': str(tmp_path), 'auth': {'mode': 'authelia'}}
    settings = config.get_journal_settings(cfg)
    assert settings['db_path'] == tmp_path / 'journal.sqlite'
    assert settings['notes'] is True and settings['download_retention_days'] == 730
    assert config.get_journal_settings({**cfg, 'auth': {'mode': 'none'}})['notes'] is False
    monkeypatch.setenv('ANTHIVE_JOURNAL_ENABLED', 'false')
    off = config.get_journal_settings(cfg)
    assert off['enabled'] is False and off['notes'] is False
    monkeypatch.delenv('ANTHIVE_JOURNAL_ENABLED')
    with pytest.raises(config.ConfigError):
        config.get_journal_settings({**cfg, 'journal': {'download_retention_days': -1}})
    with pytest.raises(config.ConfigError):
        config.get_journal_settings({**cfg, 'journal': 'yes'})


def test_may_set_status_in_the_list(service):
    service.add_note(ALICE, everything, 'dataset', 'S26/SLM', 'comment', 'hello')
    assert service.list(ALICE, everything, 'dataset', 'S26/SLM')['may_set_status'] is False
    assert service.list(CURATOR, everything, 'dataset', 'S26/SLM')['may_set_status'] is True
    assert service.list(ADMIN, everything, 'dataset', 'S26/SLM')['may_set_status'] is True
    assert service.list(Actor('dora', owns=True), everything, 'dataset', 'S26/SLM')['may_set_status'] is True


def test_open_issues_never_drop_visible_reports_behind_invisible_ones(service):
    visible_issue = service.add_note(ALICE, everything, 'dataset', 'S26/SLM', 'data-error', 'visible one')
    for number in range(1200):
        service.store.insert(target_type='dataset', target_id='S99/SEC', author='alice', kind='note',
                             category='data-error', body=f'hidden {number}', links=[], refs={},
                             dataset_version=None, parent_id=None, status='open')
    issues = service.open_issues(BOB, no_internal, limit=10)['issues']
    assert [item['id'] for item in issues] == [visible_issue['id']]


def test_the_author_reads_the_history_of_a_hidden_note_and_gets_409_on_edit(service):
    entry = service.add_note(ALICE, everything, 'dataset', 'S26/SLM', 'comment', 'first')
    service.edit(ALICE, everything, entry['id'], body='second')
    service.hide(ADMIN, everything, entry['id'], reason='off topic')
    history = service.history(ALICE, everything, entry['id'])['history']
    assert [item['action'] for item in history] == ['edit', 'hide']
    with pytest.raises(JournalError) as err:
        service.history(BOB, everything, entry['id'])
    assert err.value.status == 404
    with pytest.raises(JournalError) as err:
        service.edit(ALICE, everything, entry['id'], body='third')
    assert err.value.status == 409 and err.value.body['status'] == 'hidden'
    with pytest.raises(JournalError) as err:
        service.edit(BOB, everything, entry['id'], body='third')
    assert err.value.status == 404


def test_reply_gets_the_given_dataset_version(service):
    root = service.add_note(ALICE, everything, 'dataset', 'S26/SLM', 'question', 'why?', dataset_version='1.2.0')
    reply = service.add_note(BOB, everything, 'dataset', 'reply', 'comment', 'because', dataset_version='1.3.0',
                             parent_id=root['id'])
    assert reply['dataset_version'] == '1.3.0'


@pytest.mark.parametrize('value', [1.5, '2.5', True])
def test_config_rejects_non_integer_numbers(tmp_path, value):
    import config
    cfg = {'state_dir': str(tmp_path), 'auth': {'mode': 'authelia'}, 'journal': {'download_retention_days': value}}
    with pytest.raises(config.ConfigError):
        config.get_journal_settings(cfg)
