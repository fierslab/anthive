"""Regression tests for the 2026-10-14 review of the S3 watch route (C1, C2, S1 to S11, C-a to C-j, nits)."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

import config
import ingest_md_fill as md_fill
import ingest_pipeline
import ingest_watch
from ingest_pipeline import SwapJournal, delete_consumed, plan_items, recover_swaps, slug, valid_segment
from tests.ingest_watch_helpers import (BUCKET, CATALOG, INDEX_SEED, PREFIX, FakeS3, make_h5ad, sha_of,
                                        sidecar_text)
from tests.test_ingest_watch import Env, _live_v1

ROOT = Path(__file__).parent.parent


def _meta(path: Path) -> dict:
    return yaml.safe_load(path.read_text().split('---\n')[1])


def _listing(s3: FakeS3) -> list:
    return [{'Key': key, 'ETag': obj['etag'], 'Size': len(obj['body']), 'LastModified': obj['mtime']}
            for key, obj in s3.objects.items()]


def _add_curation(md: Path) -> None:
    md.write_text(md.read_text().replace('\n---\n', '\ncaveats:\n- level: low\n  message: curator note\n'
                                                   'api:\n  obs_rename:\n    donor: ant_donor\n---\n', 1))


# ── C1: crash-safe update swap, recovery, stop ──────────────────────────────

def test_c1_failure_while_staging_rolls_back_and_the_retry_is_an_update(tmp_path, monkeypatch):
    env = Env(tmp_path)
    _live_v1(env)
    group = env.store / 'WatchTest2026'
    _add_curation(group / 'micro.md')
    before = sorted(path.name for path in group.iterdir())
    keys = env.deposit(seed=1, with_seeds=False)
    env.clock_value += 10
    real_place = ingest_pipeline.place_file
    state = {'n': 0}

    def flaky(source, target, partial=None):
        if target.name.endswith('micro.h5ad.ingest-new') and state['n'] == 0:
            state['n'] += 1
            raise OSError(28, 'No space left on device')
        return real_place(source, target, partial)

    monkeypatch.setattr(ingest_pipeline, 'place_file', flaky)
    assert env.poll_twice() == {'retry': 1}
    assert env.row(keys['h5ad'])['state'] == 'stable'
    after = sorted(path.name for path in group.iterdir() if not path.name.endswith('.lock'))
    assert after == before                                                  # nothing moved, nothing left behind
    assert 'release:WT6/MIC' not in env.api.calls          # staging happens before the release (N6)
    assert not (env.state / 'swaps').exists() or not list((env.state / 'swaps').glob('*.json'))
    env.clock_value += 3600                                                 # past the retry delay
    assert env.watcher.poll() == {'live': 1}
    row = env.row(keys['h5ad'])
    assert row['kind'] == 'update_minor' and row['version'] == '1.1.0'
    assert 'curator note' in (group / 'micro.md').read_text()
    assert _meta(group / 'micro.md')['api'] == {'obs_rename': {'donor': 'ant_donor'}}


def test_c1_crash_during_commit_is_rolled_forward_on_the_next_run(tmp_path, monkeypatch):
    env = Env(tmp_path)
    _live_v1(env)
    group = env.store / 'WatchTest2026'
    _add_curation(group / 'micro.md')
    keys = env.deposit(seed=1, with_seeds=False)
    env.clock_value += 10
    real_replace = os.replace

    def crash_on_md(source, target):
        if str(target).endswith('WatchTest2026/micro.md') and str(source).endswith('.ingest-new'):
            raise OSError(5, 'I/O error (simulated kill)')
        return real_replace(source, target)

    monkeypatch.setattr(ingest_pipeline.os, 'replace', crash_on_md)
    assert env.poll_twice() == {'retry': 1}
    journals = list((env.state / 'swaps').glob('*.json'))
    assert len(journals) == 1 and json.loads(journals[0].read_text())['phase'] == 'committing'
    assert _meta(group / 'micro.md')['version'] == '1.0.0'                  # .md is renamed last
    monkeypatch.setattr(ingest_pipeline.os, 'replace', real_replace)
    restored = []
    assert recover_swaps(env.state / 'swaps', log=lambda message: None, restore=restored.append) == \
        [f'swap {journals[0].stem}: rolled forward, post steps pending, WT6/MIC restored in ant-serve']
    assert restored == ['WT6/MIC']
    meta = _meta(group / 'micro.md')
    assert meta['version'] == '1.1.0' and 'curator note' in (group / 'micro.md').read_text()
    assert sha_of(group / 'micro.h5ad') == meta['sha256']
    assert (group / '_versions' / 'micro' / '1.0.0' / 'micro.md').exists()
    assert not list(group.glob('.*.ingest-new'))


def test_c1_a_staged_journal_is_rolled_back_by_recovery(tmp_path):
    store = tmp_path / 'store' / 'G'
    store.mkdir(parents=True)
    (store / 'd.md').write_text('old')
    source = tmp_path / 'new.md'
    source.write_text('new')
    journal = SwapJournal(tmp_path / 'swaps', 'x', tmp_path / 'store' / 'G')
    journal.link(store / 'd.md', store / '_versions' / 'd' / '1.0.0' / 'd.md')
    journal.stage(source, store / 'd.md')
    assert recover_swaps(tmp_path / 'swaps', log=lambda message: None) == ['swap x: rolled back']
    assert (store / 'd.md').read_text() == 'old' and not (store / '_versions').exists()
    assert not list(store.glob('.*'))


def test_c1_versions_without_a_current_md_need_a_decision(tmp_path):
    env = Env(tmp_path)
    group = env.store / 'WatchTest2026'
    (group / '_versions' / 'micro' / '1.0.0').mkdir(parents=True)
    keys = env.deposit()
    assert env.poll_twice() == {'needs_decision': 1}
    assert 'archived versions' in env.row(keys['h5ad'])['error']
    assert not (group / 'micro.md').exists() and not env.s3.deleted


def test_c1_stop_requested_before_the_swap_changes_nothing(tmp_path):
    env = Env(tmp_path)
    _live_v1(env)
    group = env.store / 'WatchTest2026'
    md_before = (group / 'micro.md').read_text()
    keys = env.deposit(seed=1, with_seeds=False)
    env.clock_value += 10
    real_run = env.runner.run

    def run_then_stop(label, cmd, cwd=None, timeout=None):
        result = real_run(label, cmd, cwd, timeout)
        if label == 'ant-ingest-check':
            env.stop = True                                  # SIGTERM arrives during the check
        return result

    env.runner.run = run_then_stop
    env.watcher.poll()
    assert env.watcher.poll() == {'retry': 1}
    assert (group / 'micro.md').read_text() == md_before
    assert not (group / '_versions').exists() and env.row(keys['h5ad'])['state'] == 'stable'
    assert 'release:WT6/MIC' not in env.api.calls[-5:]


def test_c1_systemd_example_documents_the_stop():
    text = (ROOT / 'config' / 'examples' / 'anthive-ingest-watch.service.example').read_text()
    assert 'TimeoutStopSec=600' in text and 'stops between steps' not in text


# ── C2: unsafe keys ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('segment, ok', [('Group2026', True), ('a.b-c_d', True), ('..', False), ('.', False),
                                         ('_x', False), ('.hidden', False), ('café', False), ('a b', False),
                                         ('', False)])
def test_c2_valid_segment(segment, ok):
    assert valid_segment(segment) is ok


def test_c2_dotdot_keys_are_malformed_and_never_written_or_deleted(tmp_path):
    env = Env(tmp_path)
    local = make_h5ad(tmp_path / 'up' / 'evil.h5ad')
    sha = sha_of(local)
    env.s3.put_file(f'{PREFIX}WatchTest2026/../evil.h5ad', local, env.clock_value)
    env.s3.put(f'{PREFIX}WatchTest2026/../evil.h5ad.mus', sidecar_text(sha).encode(), env.clock_value)
    env.s3.put(f'{PREFIX}WatchTest2026/static/../../x.md', b'x', env.clock_value)
    items, malformed = plan_items(_listing(env.s3), PREFIX)
    assert items == [] and len(malformed) == 3
    assert env.poll_twice() == {}
    assert list(env.store.iterdir()) == [] and not env.s3.deleted


def test_c2_de_dotdot_is_never_placed(tmp_path):
    env = Env(tmp_path)
    keys = env.deposit()
    data = b'gene\tlogFC\nTREM2\t1\n'
    env.s3.put(f'{PREFIX}WatchTest2026/de/../victim_map.tsv', data, env.clock_value)
    env.s3.put(f'{PREFIX}WatchTest2026/de/../victim_map.tsv.mus',
               f'de_dataset_h5ad=micro.h5ad\nde_format=x\nsha256={hashlib.sha256(data).hexdigest()}\n'.encode(),
               env.clock_value)
    assert env.poll_twice() == {'live': 1}
    group = env.store / 'WatchTest2026'
    assert not (group / 'victim_map.tsv').exists() and not (group / 'de_sources').exists()
    assert f'{PREFIX}WatchTest2026/de/../victim_map.tsv' in env.s3.objects


def test_c2_ensure_inside():
    with pytest.raises(ValueError):
        ingest_pipeline.ensure_inside(Path('/tmp/a/../b'), Path('/tmp/a'))
    assert ingest_pipeline.ensure_inside(Path('/tmp/a/b/c'), Path('/tmp/a')) == Path('/tmp/a/b/c').resolve()


# ── S1, S2: static files and seeds ──────────────────────────────────────────

def test_s1_unchanged_reupload_places_new_static_and_applies_seed_caveats(tmp_path):
    env = Env(tmp_path)
    _live_v1(env)
    keys = env.deposit(seed=0, with_seeds=False)
    env.s3.put(keys['mus'], env.s3.objects[keys['mus']]['body'] + b'\n', env.clock_value)
    figure, seed = f'{PREFIX}WatchTest2026/static/figure_v2.png', f'{PREFIX}WatchTest2026/static/micro.md'
    env.s3.put(figure, b'new figure', env.clock_value)
    env.s3.put(seed, b'---\ntitle: Watch test microglia\ncaveats:\n- message: NEW CURATOR CAVEAT\n---\nbody\n',
               env.clock_value)
    env.clock_value += 10
    assert env.poll_twice() == {'live': 1}
    group = env.store / 'WatchTest2026'
    assert env.row(keys['h5ad'])['kind'] == 'update_patch'
    assert (group / 'static' / 'figure_v2.png').read_bytes() == b'new figure'
    assert 'NEW CURATOR CAVEAT' in (group / 'micro.md').read_text()
    assert figure not in env.s3.objects and seed not in env.s3.objects


def test_s1_a_seed_with_curated_keys_is_left_in_incoming(tmp_path):
    env = Env(tmp_path)
    _live_v1(env)
    keys = env.deposit(seed=0, with_seeds=False)
    env.s3.put(keys['mus'], env.s3.objects[keys['mus']]['body'] + b'\n', env.clock_value)
    seed = f'{PREFIX}WatchTest2026/static/micro.md'
    env.s3.put(seed, b'---\nid: XYZ\n---\nbody\n', env.clock_value)
    env.clock_value += 10
    assert env.poll_twice() == {'live': 1}
    assert seed in env.s3.objects
    assert any('not applied' in warning for warning in json.loads(env.row(keys['h5ad'])['warnings']))


def test_s1_a_seed_with_a_new_title_is_applied(tmp_path):
    # 2.47.0: title is a depositor-editable key: applied with a changelog line, the seed is consumed
    env = Env(tmp_path)
    _live_v1(env)
    keys = env.deposit(seed=0, with_seeds=False)
    env.s3.put(keys['mus'], env.s3.objects[keys['mus']]['body'] + b'\n', env.clock_value)
    seed = f'{PREFIX}WatchTest2026/static/micro.md'
    env.s3.put(seed, b'---\ntitle: A new title\n---\nbody\n', env.clock_value)
    env.clock_value += 10
    assert env.poll_twice() == {'live': 1}
    meta = _meta(env.store / 'WatchTest2026' / 'micro.md')
    assert meta['title'] == 'A new title' and meta['version'] == '1.0.1'
    assert any(line.startswith('.md title:') for line in meta['changelog'][-1]['changes'])
    assert seed not in env.s3.objects


def test_s1_a_differing_static_file_needs_a_decision_and_stays(tmp_path):
    env = Env(tmp_path)
    _live_v1(env)
    keys = env.deposit(seed=1, with_seeds=False)
    pdf = f'{PREFIX}WatchTest2026/static/paper.pdf'
    env.s3.put(pdf, b'%PDF-1.4 another paper', env.clock_value)
    env.clock_value += 10
    assert env.poll_twice() == {'needs_decision': 1}
    assert pdf in env.s3.objects and keys['h5ad'] in env.s3.objects
    assert _meta(env.store / 'WatchTest2026' / 'micro.md')['version'] == '1.0.0'


def test_s2_shared_static_is_kept_while_a_sibling_is_held(tmp_path):
    env = Env(tmp_path)
    keys_a = env.deposit(basename='alpha', seed=0)
    keys_b = env.deposit(basename='beta', seed=1, with_seeds=False)
    original_run = env.runner.run

    def run(label, cmd, cwd=None, timeout=None):
        text = [str(part) for part in cmd]
        if Path(text[0]).name == 'ant-ingest-check' and 'alpha' in text:
            Path(text[text.index('--json') + 1]).write_text(json.dumps(
                {'result': 'pass', 'publish': 'held', 'held_reasons': ['donor_overlap'], 'checks': [],
                 'dataset': {}}))
            return 0, 'held'
        return original_run(label, cmd, cwd, timeout)

    env.runner.run = run
    env.s3.put(f'{PREFIX}WatchTest2026/static/beta.md', env.s3.objects[keys_a['md']]['body'], env.clock_value)
    counts = env.poll_twice()
    assert counts == {'held': 1, 'live': 1}
    assert env.row(keys_a['h5ad'])['state'] == 'held' and env.row(keys_b['h5ad'])['state'] == 'live'
    for name in ('h5ad', 'mus', 'index', 'md', 'pdf'):
        assert keys_a[name] in env.s3.objects, name                    # alpha's deposit and the shared files
    assert keys_b['h5ad'] not in env.s3.objects                        # beta's own keys are gone
    outcomes = {entry['s3_key']: entry['outcome'] for entry in env.db.deletions()}
    assert outcomes[keys_a['pdf']] == 'skipped:shared_with_pending_sibling'


def test_s2_index_seed_of_the_item_wins(tmp_path):
    objects = [{'Key': f'{PREFIX}G/a.h5ad', 'ETag': '"1"', 'Size': 1, 'LastModified': 0},
               {'Key': f'{PREFIX}G/b.h5ad', 'ETag': '"2"', 'Size': 1, 'LastModified': 0},
               {'Key': f'{PREFIX}G/static/index.md', 'ETag': '"3"', 'Size': 1, 'LastModified': 0},
               {'Key': f'{PREFIX}G/static/b.index.md', 'ETag': '"4"', 'Size': 1, 'LastModified': 0}]
    items = {item.basename: item for item in plan_items(objects, PREFIX)[0]}
    assert items['b'].index_seed().name == 'b.index.md'
    assert items['a'].index_seed() is None                             # ambiguous: b has its own


# ── S3: access, license, data_use and curated keys ──────────────────────────

def test_s3_update_without_access_keeps_the_store_value(tmp_path):
    env = Env(tmp_path)
    _live_v1(env)
    group = env.store / 'WatchTest2026'
    mus_path = group / 'micro.h5ad.mus'
    mus_path.write_text(mus_path.read_text().replace('access=internal', 'access=public'))
    keys = env.deposit(seed=1, with_seeds=False, sidecar_extra={'access': None})
    env.clock_value += 10
    assert env.poll_twice() == {'held': 1}                       # an update of public data waits for an admin
    assert json.loads(env.row(keys['h5ad'])['held_reasons']) == ['public_access']
    ingest_watch.approve(env.db, keys['h5ad'], 'Mark')
    assert env.watcher.poll() == {'live': 1}
    assert [line for line in mus_path.read_text().splitlines() if line.startswith('access')] == ['access=public']
    assert not any(caveat.get('topic') == 'access_default' for caveat in _meta(group / 'micro.md')['caveats'])


@pytest.mark.parametrize('override', [False, True])
def test_s3_data_use_change_is_plain_metadata(tmp_path, override):
    # 2.47.0: data use applies to the whole server (D7 revised 2026-10-20); a change is applied as a patch
    # version with a changelog line and a caveat, with or without override_data_use
    env = Env(tmp_path)
    _live_v1(env)
    group = env.store / 'WatchTest2026'
    extra = {'data_use': 'controlled', **({'override_data_use': 'true'} if override else {})}
    keys = env.deposit(seed=0, with_seeds=False, sidecar_extra=extra)
    env.clock_value += 10
    assert env.poll_twice() == {'live': 1}
    assert env.row(keys['h5ad'])['held_reasons'] in (None, '', '[]')
    assert 'data_use=controlled' in (group / 'micro.h5ad.mus').read_text().splitlines()
    meta = _meta(group / 'micro.md')
    assert meta['version'] == '1.0.1'
    assert any("Sidecar data_use: 'open' -> 'controlled'" in line for line in meta['changelog'][-1]['changes'])
    caveat = next(caveat for caveat in meta['caveats'] if caveat.get('topic') == 'data_use')
    assert 'everyone with access to it can see this dataset' in caveat['message']


@pytest.mark.parametrize('key, value', [('access', 'public'), ('license', 'CC0-1.0')])
def test_s3_guarded_key_changes_need_a_decision(tmp_path, key, value):
    env = Env(tmp_path)
    _live_v1(env)
    keys = env.deposit(seed=1, with_seeds=False, sidecar_extra={key: value, f'override_{key}': 'true'})
    env.clock_value += 10
    assert env.poll_twice() == {'needs_decision': 1}
    assert key in env.row(keys['h5ad'])['error']
    assert _meta(env.store / 'WatchTest2026' / 'micro.md')['version'] == '1.0.0'


# ── S4: interrupted children are retried ────────────────────────────────────

@pytest.mark.parametrize('code', [-9, -15, 124, 127, 137, 143])
def test_s4_killed_convert_is_retried(tmp_path, code):
    env = Env(tmp_path)
    env.runner.convert_code = code
    keys = env.deposit()
    assert env.poll_twice() == {'retry': 1}
    assert env.row(keys['h5ad'])['state'] == 'stable'


def test_s4_killed_check_is_retried(tmp_path):
    env = Env(tmp_path)
    env.runner.check_code = -9
    keys = env.deposit()
    assert env.poll_twice() == {'retry': 1}
    env.runner.check_code = None
    env.clock_value += 7200
    assert env.watcher.poll() == {'live': 1}


def test_s4_stop_between_items(tmp_path):
    env = Env(tmp_path)
    env.deposit(basename='alpha', seed=0, with_seeds=False)
    env.deposit(basename='beta', seed=1, with_seeds=False)
    env.s3.put(f'{PREFIX}WatchTest2026/static/index.md', INDEX_SEED.encode(), env.clock_value)
    env.watcher.poll()
    original = env.watcher.process

    def process_then_stop(item, listed=()):
        state = original(item, listed)
        env.stop = True
        return state

    env.watcher.process = process_then_stop
    assert sum(env.watcher.poll().values()) == 1


# ── S5: one lock for the watcher and ant-ingest ─────────────────────────────

def test_s5_ant_ingest_takes_the_watch_lock():
    text = (ROOT / 'bin' / 'ingest' / 'ant-ingest').read_text()
    assert 'ingest_watch.WatchLock(' in text
    main = text[text.index('def main()'):]
    assert main.index('WatchLock(') < main.index('cmd_purge(') and main.index('WatchLock(') < main.index('cmd_run(')


# ── S6: served means the new sha256 and version ─────────────────────────────

def test_s6_stale_served_version_prevents_deletes(tmp_path):
    env = Env(tmp_path)
    env.api.stale_info = True
    keys = env.deposit()
    assert env.poll_twice() == {'live': 1}
    assert env.row(keys['h5ad'])['served'] == 0 and not env.s3.deleted
    env.api.stale_info = False
    env.watcher.poll()
    assert keys['h5ad'] in env.s3.deleted


# ── S7: S3 copy of the old version first ────────────────────────────────────

def test_s7_failed_s3_archive_changes_nothing(tmp_path):
    env = Env(tmp_path)
    _live_v1(env)
    group = env.store / 'WatchTest2026'
    md_before = (group / 'micro.md').read_text()
    env.s3.copy_error = RuntimeError('S3 copy refused')
    keys = env.deposit(seed=1, with_seeds=False)
    env.clock_value += 10
    deleted_before = list(env.s3.deleted)
    assert env.poll_twice() == {'retry': 1}
    assert (group / 'micro.md').read_text() == md_before and not (group / '_versions').exists()
    assert env.s3.deleted == deleted_before
    assert 'release:WT6/MIC' not in env.api.calls


# ── S8: name collisions ─────────────────────────────────────────────────────

def test_s8_update_of_mismatch_needs_a_decision(tmp_path):
    env = Env(tmp_path)
    _live_v1(env)
    keys = env.deposit(seed=1, with_seeds=False, sidecar_extra={'update_of': 'XX1/ABC'})
    env.clock_value += 10
    assert env.poll_twice() == {'needs_decision': 1}


def test_s8_previous_sha_mismatch_and_marker_required(tmp_path):
    env = Env(tmp_path)
    _live_v1(env)
    env.deposit(seed=1, with_seeds=False, sidecar_extra={'previous_sha256': 'f' * 64})
    env.clock_value += 10
    assert env.poll_twice() == {'needs_decision': 1}
    env2 = Env(tmp_path / 'b')
    _live_v1(env2)
    env2.pipeline.config.require_update_marker = True
    env2.deposit(seed=1, with_seeds=False)
    env2.clock_value += 10
    assert env2.poll_twice() == {'needs_decision': 1}
    old_sha = sha_of(env2.store / 'WatchTest2026' / 'micro.h5ad')
    keys = env2.deposit(seed=1, with_seeds=False, sidecar_extra={'previous_sha256': old_sha, 'update_of': 'wt6/mic'})
    env2.clock_value += 10
    assert env2.poll_twice() == {'live': 1}


def test_s8_update_marker_on_a_new_dataset_needs_a_decision(tmp_path):
    env = Env(tmp_path)
    env.deposit(sidecar_extra={'update_of': 'WT6/MIC'})
    assert env.poll_twice() == {'needs_decision': 1}


# ── S9: release failure ─────────────────────────────────────────────────────

def test_s9_release_failure_is_transient_and_swaps_nothing(tmp_path):
    env = Env(tmp_path)
    _live_v1(env)
    group = env.store / 'WatchTest2026'
    md_before = (group / 'micro.md').read_text()
    env.api.release_fails = True
    env.deposit(seed=1, with_seeds=False)
    env.clock_value += 10
    assert env.poll_twice() == {'retry': 1}
    assert (group / 'micro.md').read_text() == md_before and not (group / '_versions').exists()
    assert 'restore:WT6/MIC' not in env.api.calls


# ── S10: work dirs, cached pulls, free space ────────────────────────────────

def test_s10_work_dir_removed_after_live_and_pulls_reused_on_retry(tmp_path):
    env = Env(tmp_path)
    env.runner.convert_code = -9
    keys = env.deposit()
    env.poll_twice()
    downloads = [call for call in env.s3.calls if call == ('download', keys['h5ad'])]
    assert len(downloads) == 1
    env.runner.convert_code = None
    env.clock_value += 7200
    assert env.watcher.poll() == {'live': 1}
    assert [call for call in env.s3.calls if call == ('download', keys['h5ad'])] == downloads
    assert not (tmp_path / 'work' / slug(keys['h5ad'])).exists()


def test_s10_free_space_check(tmp_path):
    env = Env(tmp_path)
    env.pipeline.config.min_free_gb = 10 ** 9
    keys = env.deposit()
    assert env.poll_twice() == {'retry': 1}
    assert 'free' in env.row(keys['h5ad'])['error']


def test_s10_old_work_dirs_of_finished_items_are_pruned(tmp_path):
    env = Env(tmp_path, report={'result': 'fail', 'publish': 'held', 'held_reasons': [], 'checks': []})
    keys = env.deposit()
    env.poll_twice()
    work = tmp_path / 'work' / slug(keys['h5ad'])
    assert work.exists()
    env.clock_value += 15 * 86400
    os.utime(work, (env.clock_value - 15 * 86400, env.clock_value - 15 * 86400))
    env.watcher.poll()
    assert not work.exists()


# ── S11: legacy catalog objects ─────────────────────────────────────────────

def test_s11_legacy_object_without_sha_metadata_is_verified_and_backfilled(tmp_path):
    env = Env(tmp_path)
    _live_v1(env)
    catalog_key = f'{CATALOG}WatchTest2026/micro.h5ad'
    env.s3.objects[catalog_key]['metadata'] = {}                   # as written by ant-sync
    keys = env.deposit(seed=0, with_seeds=False)
    env.s3.put(keys['mus'], env.s3.objects[keys['mus']]['body'] + b'\n', env.clock_value)
    env.clock_value += 10
    assert env.poll_twice() == {'live': 1}
    assert env.row(keys['h5ad'])['catalog_verified'] == 1
    assert env.s3.objects[catalog_key]['metadata']['sha256'] == sha_of(env.store / 'WatchTest2026' / 'micro.h5ad')


# ── C-b, C-c, C-e, C-g, C-h, C-i, C-j ───────────────────────────────────────

def test_cb_public_is_held_even_when_the_check_says_auto(tmp_path):
    env = Env(tmp_path)
    keys = env.deposit(sidecar_extra={'access': 'public'})
    assert env.poll_twice() == {'held': 1}
    assert json.loads(env.row(keys['h5ad'])['held_reasons']) == ['public_access']


def test_cc_conditional_delete_and_fallback():
    s3 = FakeS3()
    etag = s3.put(f'{PREFIX}G/a.h5ad', b'one')
    s3.before_delete = lambda key: s3.put(key, b'two') if s3.objects[key]['body'] == b'one' else None
    deleted, skipped = delete_consumed(s3, BUCKET, PREFIX, {f'{PREFIX}G/a.h5ad': etag}, if_match=True)
    assert not deleted and f'{PREFIX}G/a.h5ad' in s3.objects            # IfMatch caught the race
    s3 = FakeS3()
    s3.if_match_supported = False
    etag = s3.put(f'{PREFIX}G/a.h5ad', b'one')
    other = s3.put(f'{PREFIX}G/b.h5ad', b'x')
    s3.put(f'{PREFIX}G/b.h5ad', b'changed')
    deleted, skipped = delete_consumed(s3, BUCKET, PREFIX, {f'{PREFIX}G/a.h5ad': etag, f'{PREFIX}G/b.h5ad': other})
    assert deleted == [f'{PREFIX}G/a.h5ad'] and skipped == [f'{PREFIX}G/b.h5ad']


def test_ce_one_rescan_per_poll_for_unverified_rows(tmp_path):
    env = Env(tmp_path)
    env.api.down = True
    env.deposit(basename='alpha', seed=0)
    env.deposit(basename='beta', seed=1, with_seeds=False)
    env.poll_twice()
    env.api.down = False
    rescans = env.api.calls.count('rescan')
    env.watcher.poll()
    assert env.api.calls.count('rescan') - rescans == 1


def test_cg_catalog_object_without_a_local_dataset_needs_a_decision(tmp_path):
    env = Env(tmp_path)
    env.s3.put(f'{CATALOG}WatchTest2026/micro.h5ad', b'retired')
    env.deposit()
    assert env.poll_twice() == {'needs_decision': 1}
    assert not (env.store / 'WatchTest2026').exists()


def test_ch_default_dirs_inside_the_repo_are_refused(monkeypatch):
    for name in ('ANTHIVE_STATE_DIR', 'ANTHIVE_TMP_DIR', 'ANTHIVE_INGEST_STATE_DIR', 'ANTHIVE_INGEST_WORK_DIR'):
        monkeypatch.delenv(name, raising=False)
    defaults = {'ingest_state_dir': config.get_ingest_state_dir({}, create=False),
                'ingest_work_dir': config.get_ingest_work_dir({}, create=False),
                'tmp_dir': config.get_tmp_dir({}, create=False)}
    assert len(ingest_watch.repo_dir_problems(defaults, ROOT)) == 3
    assert ingest_watch.repo_dir_problems({'x': Path('/tmp/elsewhere')}, ROOT) == []
    makefile = (ROOT / 'Makefile').read_text()
    env_block = makefile[makefile.index('INGEST_WATCH_ENV ='):makefile.index('\ningest-watch:')]
    assert 'ANTHIVE_INGEST_STATE_DIR' in env_block and 'ANTHIVE_TMP_DIR' in env_block
    assert 'ANTHIVE_INGEST_WORK_DIR' in env_block
    target = makefile[makefile.index('\ningest-watch:'):makefile.index('\ningest-watch-once:')]
    assert '$(INGEST_WATCH_ENV)' in target


def test_ci_failed_commit_unstages(tmp_path):
    env = Env(tmp_path)
    (env.store / '.git').mkdir()
    env.pipeline.config.store_git_commit = True
    env.runner.git_commit_code = 1
    keys = env.deposit()
    assert env.poll_twice() == {'live': 1}
    assert any(cmd[:4] == ['git', '-C', str(env.store), 'reset'] for cmd in env.runner.commands)
    assert any('git commit failed' in warning for warning in json.loads(env.row(keys['h5ad'])['warnings']))


def test_ci_unchanged_deposit_commits_only_when_files_changed(tmp_path):
    env = Env(tmp_path)
    (env.store / '.git').mkdir()
    _live_v1(env)
    keys = env.deposit(seed=0, with_seeds=False)
    env.s3.put(keys['mus'], env.s3.objects[keys['mus']]['body'] + b'\n', env.clock_value)
    env.clock_value += 10
    original = env.runner.run
    env.runner.run = lambda label, cmd, cwd=None, timeout=None: (0, '') if label == 'git status' \
        else original(label, cmd, cwd, timeout)
    commits = sum('commit' in cmd for cmd in env.runner.commands)
    assert env.poll_twice() == {'live': 1}
    assert env.row(keys['h5ad'])['kind'] == 'unchanged'
    assert sum('commit' in cmd for cmd in env.runner.commands) == commits      # nothing to commit: no commit


def test_ci_push_only_when_nothing_else_is_unpushed(tmp_path):
    env = Env(tmp_path)
    (env.store / '.git').mkdir()
    env.pipeline.config.store_git_push = True
    original = env.runner.run
    env.runner.run = lambda label, cmd, cwd=None, timeout=None: (0, '3') if label == 'git rev-list' \
        else original(label, cmd, cwd, timeout)
    keys = env.deposit()
    env.poll_twice()
    assert not any('push' in cmd for cmd in env.runner.commands)
    assert any('not pushed' in warning for warning in json.loads(env.row(keys['h5ad'])['warnings']))


def test_cj_approve_publishes_a_held_item_with_a_caveat(tmp_path):
    env = Env(tmp_path)
    keys = env.deposit(sidecar_extra={'access': 'public'})
    assert env.poll_twice() == {'held': 1}
    with pytest.raises(ValueError):
        ingest_watch.approve(env.db, keys['h5ad'], '')
    ingest_watch.approve(env.db, keys['h5ad'], 'Mark Fiers', 'checked the licence')
    assert env.watcher.poll() == {'live': 1}
    meta = _meta(env.store / 'WatchTest2026' / 'micro.md')
    caveat = next(caveat for caveat in meta['caveats'] if caveat['topic'] == 'admin_approval')
    assert 'Mark Fiers' in caveat['message'] and 'public_access' in caveat['message']


def test_cj_approval_is_void_when_the_upload_changes(tmp_path):
    env = Env(tmp_path)
    keys = env.deposit(sidecar_extra={'access': 'public'})
    env.poll_twice()
    ingest_watch.approve(env.db, keys['h5ad'], 'Mark')
    env.s3.put(keys['mus'], env.s3.objects[keys['mus']]['body'] + b'# changed\n', env.clock_value)
    env.watcher.poll()
    assert env.watcher.poll() == {'held': 1}


def test_cj_reject(tmp_path):
    env = Env(tmp_path, report={'result': 'fail', 'publish': 'held', 'held_reasons': [], 'checks': []})
    keys = env.deposit()
    env.poll_twice()
    ingest_watch.reject(env.db, keys['h5ad'], 'Mark', 'wrong file')
    assert env.row(keys['h5ad'])['state'] == 'rejected'
    assert env.watcher.poll() == {} and not env.s3.deleted


# ── nits ────────────────────────────────────────────────────────────────────

def test_nit_unversioned_md_is_archived_as_0_0_0_and_bumped_from_it(tmp_path):
    env = Env(tmp_path)
    _live_v1(env)
    md = env.store / 'WatchTest2026' / 'micro.md'
    md.write_text(md.read_text().replace('version: 1.0.0\n', '', 1))
    keys = env.deposit(seed=1, with_seeds=False)
    env.clock_value += 10
    assert env.poll_twice() == {'live': 1}
    assert _meta(md)['version'] == '0.1.0'
    assert (env.store / 'WatchTest2026' / '_versions' / 'micro' / '0.0.0' / 'micro.md').exists()


def test_nit_ids_compare_case_insensitively():
    assert md_fill.unique_file_id('my_micro_data', 'g01', {'G01/MMD'}) != 'MMD'
    meta, _ = md_fill.build_index_md({}, 'WatchTest2026', {'wt6'}, 'x')
    assert 'group_id' not in meta


@pytest.mark.parametrize('sidecar, author', [
    ({'depositor': 'Ada Lovelace', 'depositor_email': 'ada@example.org'}, 'Ada Lovelace <ada@example.org>'),
    ({'depositor_email': 'ada@example.org'}, 'ada <ada@example.org>'),
    ({'depositor': '--author=evil', 'depositor_email': 'ada@example.org'}, None),
    ({'depositor': 'Ada', 'depositor_email': 'not an email'}, None),
    ({'depositor': 'Ada'}, None),
])
def test_nit_depositor_validation(sidecar, author):
    assert md_fill.depositor_of(sidecar) == author
    if sidecar.get('depositor') and author is None:
        assert 'not used as the git author' in md_fill.depositor_note(sidecar)


# ── the real ant-ingest-check report schema ─────────────────────────────────

@pytest.mark.slow
def test_real_ingest_check_report_schema_is_what_the_pipeline_reads(tmp_path):
    group = tmp_path / 'sub' / 'WatchTest2026'
    h5ad = make_h5ad(group / 'micro.h5ad')
    (group / 'micro.h5ad.mus').write_text(sidecar_text(sha_of(h5ad)))
    (group / 'micro.md').write_text('---\ntitle: T\nid: MIC\nversion: 1.0.0\n---\n\nshort\n')
    (group / 'index.md').write_text(INDEX_SEED)
    store = tmp_path / 'store'
    store.mkdir()
    out = tmp_path / 'out' / 'report.json'
    out.parent.mkdir()
    env = {**os.environ, 'ANTHIVE_TMP_DIR': str(tmp_path / 'tmp'), 'ANTHIVE_INGEST_STATE_DIR': str(tmp_path / 'st')}
    completed = subprocess.run([str(ROOT / 'bin' / 'ingest' / 'ant-ingest-check'), str(group), '--dataset', 'micro',
                                '--store', str(store), '--no-convert', '--json', str(out)],
                               capture_output=True, text=True, env=env, timeout=900, check=False)
    assert completed.returncode in (0, 1), completed.stderr[-2000:]
    report = json.loads(out.read_text())
    assert report['result'] in ('pass', 'fail') and report['publish'] in ('auto', 'held')
    assert isinstance(report['held_reasons'], list)
    assert report['checks'] and all({'id', 'status'} <= set(entry) for entry in report['checks'])
    assert {entry['status'] for entry in report['checks']} <= {'pass', 'warn', 'fail', 'skip'}
    assert report['result'] == 'fail'                                   # the short description and no changelog
    assert 'meta.required_keys' in {entry['id'] for entry in report['checks'] if entry['status'] == 'fail'}
