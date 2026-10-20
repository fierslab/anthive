"""ant ingest-check h5ad.obs_columns: obs columns ant-convert will skip are named with the reason.

Francken2026 PFC attempt 1 (2026-10-20): ant-convert skipped ``sex`` as a case-insensitive duplicate of ``Sex``
with only a console line. The check mirrors ant-convert: include lists plus the api: sources, per table the case
collisions and reserved names, and (with include lists) text columns above the default value limit.
"""
from __future__ import annotations

import pandas as pd
import pytest

from ingest_check import PASS, WARN, checks_files
from ingest_check.checks_files import obs_storage
from tests.ingest_helpers import Submission, donor_obs


@pytest.fixture
def submission(tmp_path):
    return Submission(tmp_path)


def _obs(n_cells: int = 60) -> pd.DataFrame:
    obs = donor_obs(n_cells)
    obs['Sex'] = obs['sex_raw']
    obs['sex'] = obs['sex_raw'].str.lower()
    obs['cell_barcode'] = [f'BC{i}' for i in range(n_cells)]
    return obs


def test_storage_mirrors_ant_convert(monkeypatch):
    import ingest_md_fill
    monkeypatch.setattr(ingest_md_fill, 'MAX_DEFAULT_OBSCAT_VALUES', 10)
    obs = _obs()
    obs['cell_id'] = range(len(obs))
    metadata = {'obscat_include': ['Sex', 'sex', 'cell_id', 'dx'], 'obsnum_include': ['age'],
                'api': {'obs_rename': {'donor': 'ant_donor'}}}
    plan = obs_storage(obs, metadata)
    assert plan['stored'] == ['age', 'Sex', 'dx', 'donor']
    reasons = {item['column']: item['reason'] for item in plan['skipped']}
    assert "differs from 'Sex' only in case" in reasons['sex']
    assert reasons['cell_id'].startswith('reserved name')
    assert reasons['cell_barcode'].startswith('60 distinct values, above the 10')
    assert 'braak' not in reasons and 'sex_raw' not in reasons    # few values: not listed by choice, not named


def test_without_lists_only_the_api_sources_are_stored():
    plan = obs_storage(_obs(), {'api': {'obs_rename': {'donor': 'ant_donor', 'age': 'ant_age'}}})
    assert sorted(plan['stored']) == ['age', 'donor'] and not plan['has_lists'] and plan['skipped'] == []


def test_check_warns_and_names_a_case_collision(submission):
    submission.obs = _obs()
    submission.build(md={'obscat_include': ['Sex', 'sex', 'dx']})
    with submission.context() as ctx:
        outcome = checks_files.h5ad_obs_columns(ctx)
    assert outcome.status == WARN and "sex: differs from 'Sex' only in case" in outcome.summary
    assert outcome.details['skipped'][0]['column'] == 'sex'


def test_check_notes_the_value_limit_and_passes_a_clean_list(submission, monkeypatch):
    import ingest_md_fill
    monkeypatch.setattr(ingest_md_fill, 'MAX_DEFAULT_OBSCAT_VALUES', 10)
    submission.obs = donor_obs(60).assign(cell_barcode=[f'BC{i}' for i in range(60)])
    submission.build(md={'obscat_include': ['dx'], 'obsnum_include': ['age']})
    with submission.context() as ctx:
        outcome = checks_files.h5ad_obs_columns(ctx)
    assert outcome.status == PASS and outcome.info and 'cell_barcode: 60 distinct values' in outcome.summary
    submission2 = Submission(submission.root / 'second')
    submission2.build(md={'obscat_include': ['dx'], 'obsnum_include': ['age']})
    with submission2.context() as ctx:
        outcome = checks_files.h5ad_obs_columns(ctx)
    assert outcome.status == PASS and not outcome.info and outcome.details['skipped'] == []
