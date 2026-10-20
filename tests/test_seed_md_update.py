"""Updates that change the dataset .md (2.47.0): depositor-editable keys are applied, caveats always.

Real case: F26/PFC update 1.1.0 (dep_a8e508e2e7fb8583bd90), whose seed changed api, description,
disease_basis, disease_derivation and obscat_include; before, nothing of it was applied, not even its caveats.
Rules: the editable keys (``ingest_md_fill.DEPOSITOR_EDITABLE_KEYS``) of an owner's or admin's update are merged
into the store .md with one changelog line each and a caveat (patch bump without new data); an api or include
change rebuilds the views through ant-convert; curated keys stay a conflict (not applied, the seed stays in
incoming) but the seed's new caveats are applied anyway; a non-owner cannot deposit an update; access and
license changes in the sidecar are still needs_decision.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import dataset_deposit as dd
import ingest_md_fill as md_fill
from tests.deposit_helpers import ALICE, BOB, file_entry, upload
from tests.ingest_watch_helpers import CATALOG, PREFIX
from tests.test_dataset_deposit_metadata_update import published_by_alice, sidecar_with
from tests.test_dataset_deposit_watch import GROUP, SEED_MD

API_SEED = SEED_MD.replace('replicate_unit: donor\n', 'replicate_unit: donor\ndisease_basis: clinical_dx\n'
                           'api:\n  obs_rename:\n    donor: ant_donor\n'
                           'caveats:\n- level: low\n  kind: note\n  message: Donor ids are study ids.\n')


def _meta(path: Path) -> dict:
    return yaml.safe_load(path.read_text().split('---\n')[1])


def _seed_update(env, service, clock, seed: str, identity=ALICE):
    body = {'data_project': GROUP, 'basename': 'micro', 'update_of': 'WT6/MIC',
            'files': [file_entry('micro.md', seed.encode(), 'seed_md')]}
    response = service.create(identity, body)
    upload(env.s3, {'files': [entry for entry in response['files'] if entry['method'] != 'reuse']},
           {'micro.md': seed.encode()})
    service.complete(identity, response['deposit_id'])
    clock[0] = env.clock_value = env.clock_value + 10
    return response, env.poll_twice()


def test_seed_split_into_caveats_editable_keys_and_curated_conflicts():
    store = {'title': 'T', 'id': 'MIC', 'api': {'obs_rename': {'a': 'ant_sex'}}, 'caveats': [{'message': 'old'}],
             'version': '1.0.0', 'curator_note': 'x'}
    seed = {'title': 'T2', 'id': 'XYZ', 'api': {'obs_rename': {'a': 'ant_sex', 'donor': 'ant_donor'}},
            'caveats': [{'message': 'old'}, {'message': 'new'}], 'version': '9.9.9', 'disease_basis': 'clinical_dx'}
    delta = md_fill.seed_md_changes(seed, store)
    assert delta.new_caveats == [{'message': 'new'}]
    assert set(delta.editable) == {'title', 'api', 'disease_basis'} and delta.conflicts == ['id']
    assert delta.rebuild_views
    assert md_fill.describe_change('api', store['api'], seed['api']) == '.md api: changed obs_rename (deposited .md).'
    assert md_fill.describe_change('obscat_include', ['a'], ['a', 'b']) == ".md obscat_include: added ['b'] (deposited .md)."
    assert not md_fill.seed_md_changes({'title': 'T2'}, store).rebuild_views


def test_update_md_text_refuses_keys_that_are_not_editable():
    text = '---\ntitle: T\nversion: 1.0.0\n---\n\nbody\n'
    plan = md_fill.UpdatePlan('patch', ['x'], None, [])
    with pytest.raises(ValueError):
        md_fill.update_md_text(text, plan, 'a' * 64, None, '2026-10-20', set_keys={'id': 'XYZ'})
    new = md_fill.update_md_text(text, plan, 'a' * 64, None, '2026-10-20', set_keys={'title': 'T2'})
    assert 'title: T2' in new and new.endswith('body\n')


def test_owner_update_applies_the_api_with_a_patch_and_rebuilds_the_views(tmp_path):
    env, service, clock = published_by_alice(tmp_path)
    converts_before = sum(1 for cmd in env.runner.commands if Path(cmd[0]).name == 'ant-convert')
    response, counts = _seed_update(env, service, clock, API_SEED)
    assert counts == {'live': 1}
    meta = _meta(env.store / GROUP / 'micro.md')
    assert meta['version'] == '1.0.1'
    assert meta['api'] == {'obs_rename': {'donor': 'ant_donor'}} and meta['disease_basis'] == 'clinical_dx'
    changes = meta['changelog'][-1]['changes']
    assert '.md api set to' in ' '.join(changes) and any(line.startswith('.md disease_basis') for line in changes)
    topics = [caveat.get('topic') for caveat in meta['caveats']]
    assert 'seed_md_update' in topics and any(c['message'] == 'Donor ids are study ids.' for c in meta['caveats'])
    commands = env.runner.commands
    converts = [cmd for cmd in commands if Path(cmd[0]).name == 'ant-convert']
    assert len(converts) == converts_before + 1                    # the views are rebuilt for the patch
    check = [cmd for cmd in commands if Path(cmd[0]).name == 'ant-ingest-check'][-1]
    assert '--duckdb' not in check                                 # checked against the rebuilt database
    status = service.status(ALICE, response['deposit_id'])
    assert status['state'] == 'live' and status['kind'] == 'update_patch'
    assert any('applied from' in warning and 'api' in warning for warning in status.get('warnings') or [])
    assert f'{PREFIX}{GROUP}/static/micro.md' not in env.s3.objects  # consumed and deleted


def test_curated_conflict_is_not_applied_but_the_caveats_are(tmp_path):
    env, service, clock = published_by_alice(tmp_path)
    seed = API_SEED.replace('title: Watch test microglia\n', 'title: Watch test microglia\nid: XYZ\n')
    _, counts = _seed_update(env, service, clock, seed)
    assert counts == {'live': 1}
    meta = _meta(env.store / GROUP / 'micro.md')
    assert meta['id'] != 'XYZ'                                     # curated: not applied
    assert any(c['message'] == 'Donor ids are study ids.' for c in meta['caveats'])
    assert meta['api'] == {'obs_rename': {'donor': 'ant_donor'}}  # editable keys still applied
    row = env.db.get(f'{PREFIX}{GROUP}/micro.h5ad')
    assert 'curated id: not applied, left in incoming' in row['warnings']
    assert f'{PREFIX}{GROUP}/static/micro.md' in env.s3.objects    # the seed stays in incoming


def test_a_non_owner_cannot_deposit_an_update(tmp_path):
    env, service, clock = published_by_alice(tmp_path)
    with pytest.raises(dd.DepositError) as exc:
        _seed_update(env, service, clock, API_SEED, identity=BOB)
    assert exc.value.body['status'] == 'name_taken'
    assert 'api' not in _meta(env.store / GROUP / 'micro.md')


@pytest.mark.parametrize('key, value', [('access', 'public'), ('license', 'CC0-1.0')])
def test_access_and_licence_changes_still_need_a_decision(tmp_path, key, value):
    env, service, clock = published_by_alice(tmp_path)
    sidecar = sidecar_with(env, **{key: value, f'override_{key}': 'true'})
    lines = [line for line in sidecar.decode().splitlines() if not line.startswith(f'{key}=') or line == f'{key}={value}']
    sidecar = ('\n'.join(lines) + '\n').encode()
    body = {'data_project': GROUP, 'basename': 'micro', 'update_of': 'WT6/MIC',
            'files': [file_entry('micro.h5ad.mus', sidecar, 'mus')]}
    response = service.create(ALICE, body)
    upload(env.s3, {'files': [entry for entry in response['files'] if entry['method'] != 'reuse']},
           {'micro.h5ad.mus': sidecar})
    service.complete(ALICE, response['deposit_id'])
    clock[0] = env.clock_value = env.clock_value + 10
    assert env.poll_twice() == {'needs_decision': 1}
    assert _meta(env.store / GROUP / 'micro.md')['version'] == '1.0.0'


# ── GET /datasets/deposit/md/{db_id}: the store's editable keys for a client's diff ───────────

def test_editable_md_for_the_owner_and_admins_only(tmp_path):
    from tests.deposit_helpers import ADMIN

    env, service, clock = published_by_alice(tmp_path)
    _seed_update(env, service, clock, API_SEED)
    md_path = env.store / GROUP / 'micro.md'
    answer = service.editable_md(ALICE, 'WT6/MIC', md_path)
    assert answer['db_id'] == 'WT6/MIC' and answer['version'] == '1.0.1'
    assert answer['keys']['api'] == {'obs_rename': {'donor': 'ant_donor'}}
    assert set(answer['keys']) <= set(md_fill.DEPOSITOR_EDITABLE_KEYS)
    assert 'caveats' not in answer['keys'] and 'changelog' not in answer['keys'] and 'id' not in answer['keys']
    assert answer['editable_keys'] == list(md_fill.DEPOSITOR_EDITABLE_KEYS)
    assert service.editable_md(ADMIN, 'WT6/MIC', md_path)['keys'] == answer['keys']
    with pytest.raises(dd.DepositError) as exc:
        service.editable_md(BOB, 'WT6/MIC', md_path)
    assert exc.value.status == 403
    with pytest.raises(dd.DepositError) as exc:                     # not owned: the same 403, nothing revealed
        service.editable_md(BOB, 'WT6/NOPE', None)
    assert exc.value.status == 403
    with pytest.raises(dd.DepositError) as exc:
        service.editable_md(ADMIN, 'WT6/NOPE', None)
    assert exc.value.status == 404
    with pytest.raises(dd.DepositError) as exc:
        service.editable_md(ADMIN, '../etc', None)
    assert exc.value.status == 422
    service.settings.update_policy = 'any'
    assert service.editable_md(BOB, 'WT6/MIC', md_path)['keys'] == answer['keys']


def test_editable_md_route_is_registered_before_the_deposit_id_route():
    source = (Path(__file__).resolve().parent.parent / 'bin' / 'api' / 'ant-serve').read_text()
    route = source.index('@app.get("/datasets/deposit/md/{db_id:path}"')
    assert route < source.index('@app.get("/datasets/deposit/{deposit_id}"')
    assert 'service.editable_md(identity, db_id, md_path)' in source[route:route + 900]
