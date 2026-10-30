"""ant-stack and the site.yaml journal: block (doc/DESIGN_dataset_journal.md): validation, rendering into
anthive.yaml, the plan line and the check line."""
from __future__ import annotations

import pytest

from tests.test_ant_stack_artefacts import _configs, _site, ant_stack  # noqa: F401 - the fixture


def test_default_site_gets_the_default_journal(tmp_path, ant_stack):  # noqa: F811
    site = _site(ant_stack, tmp_path)
    _stack, configs = _configs(ant_stack, site)
    assert configs['api']['journal']['enabled'] is True
    assert configs['api']['journal']['download_retention_days'] == 730
    assert ant_stack._journal_summary(site).startswith('notes and reports on')


def test_journal_keys_reach_anthive_yaml(tmp_path, ant_stack):  # noqa: F811
    site = _site(ant_stack, tmp_path, journal={'download_retention_days': 365, 'triage_groups': ['admins']})
    _stack, configs = _configs(ant_stack, site)
    assert configs['api']['journal']['download_retention_days'] == 365
    assert configs['api']['journal']['triage_groups'] == ['admins']


def test_no_login_site_keeps_counts_only(tmp_path, ant_stack):  # noqa: F811
    site = _site(ant_stack, tmp_path, auth={'mode': 'none', 'admins': [], 'users': []})
    assert 'anonymous download counts only' in ant_stack._journal_summary(site)


@pytest.mark.parametrize('block', [{'enabled': 'yes'}, {'download_retention_days': -1}, {'colour': 'red'},
                                   {'triage_groups': 'curators'}, {'max_body_chars': 50}, 'on'])
def test_journal_block_is_validated(tmp_path, ant_stack, block):  # noqa: F811
    with pytest.raises(ant_stack.SiteError):
        _site(ant_stack, tmp_path, journal=block)
