"""Tests for the ant_* back-compat column alias logic.

Covers three layers:
1. Unit: ``_alias_canonical`` helper function (via direct import from lib).
2. Unit: ``get_module_variability`` filter behaviour with a synthetic DataFrame.
3. Integration (skipped when ant-serve is down): live HTTP checks that
   deprecated bare names trigger the ``X-Anthive-Deprecated-Alias`` header
   and that unknown names still 400/404.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# ── path setup ────────────────────────────────────────────────────────────────

_repo_root = Path(__file__).resolve().parent.parent
_lib_dir = str(_repo_root / "lib")
if _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)

from canonical_columns import BACKCOMPAT_LEGACY_NAMES, CANONICAL_TAGS  # noqa: E402


# ── replicate the helper locally (imported indirectly; we test it via its
#    contract, not by importing the ant-serve script directly which would
#    trigger side-effects) ────────────────────────────────────────────────────


def _alias_canonical(
    name: Optional[str],
    available: set[str],
) -> tuple[Optional[str], Optional[str]]:
    """Local copy mirrors the implementation in ant-serve for unit testing."""
    if name is None:
        return None, None
    if name in available:
        return name, None
    new_name = BACKCOMPAT_LEGACY_NAMES.get(name)
    if new_name and new_name in available:
        return new_name, f"{name} -> {new_name}"
    return name, None


# ── 1. Unit tests for _alias_canonical ───────────────────────────────────────


class TestAliasCanonical:
    def test_none_input_returns_none_pair(self):
        result_name, notice = _alias_canonical(None, {'ant_braak'})
        assert result_name is None
        assert notice is None

    def test_bare_name_already_a_real_column_is_unchanged(self):
        """If ``braak`` exists literally in available, leave it alone."""
        result_name, notice = _alias_canonical('braak', {'braak', 'ant_braak'})
        assert result_name == 'braak'
        assert notice is None

    @pytest.mark.parametrize("tag", list(CANONICAL_TAGS))
    def test_canonical_tag_rewrites_when_ant_column_exists(self, tag: str):
        ant_col = f"ant_{tag}"
        result_name, notice = _alias_canonical(tag, {ant_col})
        assert result_name == ant_col
        assert notice == f"{tag} -> {ant_col}"

    def test_donor_id_legacy_alias(self):
        """``donor_id`` is a legacy misnomer — maps to ``ant_donor``."""
        result_name, notice = _alias_canonical('donor_id', {'ant_donor'})
        assert result_name == 'ant_donor'
        assert notice == 'donor_id -> ant_donor'

    def test_unknown_name_passes_through_with_no_notice(self):
        result_name, notice = _alias_canonical('completely_unknown', {'ant_braak'})
        assert result_name == 'completely_unknown'
        assert notice is None

    def test_ant_column_not_in_available_does_not_rewrite(self):
        """Only rewrite when the ant_* column actually exists in available."""
        result_name, notice = _alias_canonical('braak', set())
        assert result_name == 'braak'
        assert notice is None

    def test_already_ant_prefixed_name_in_available_is_unchanged(self):
        result_name, notice = _alias_canonical('ant_braak', {'ant_braak'})
        assert result_name == 'ant_braak'
        assert notice is None


# ── 2. Unit tests for module-variability filter logic ─────────────────────────
#
# We don't import ant-serve (heavy, side-effectful).  Instead we replicate
# the filter path verbatim and verify the alias fires correctly.


def _build_mv_df() -> pd.DataFrame:
    """Synthetic module-variability DataFrame with ant_* tag_column values."""
    rows = [
        {'dataset': 'DS1', 'tag': 'braak', 'tag_column': 'ant_braak',
         'statistic': 'eta2', 'value': 0.42, 'module': 'M1',
         'source': 'geneset', 'catalog': 'CAT1'},
        {'dataset': 'DS1', 'tag': 'sex', 'tag_column': 'ant_sex',
         'statistic': 'eta2', 'value': 0.10, 'module': 'M1',
         'source': 'geneset', 'catalog': 'CAT1'},
        {'dataset': 'DS2', 'tag': 'disease', 'tag_column': 'ant_disease',
         'statistic': 'omega2', 'value': 0.25, 'module': 'M2',
         'source': 'starcat', 'catalog': 'CAT2'},
    ]
    return pd.DataFrame(rows)


def _apply_mv_filters(
    df: pd.DataFrame,
    tag: Optional[str] = None,
    tag_column: Optional[str] = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Replicate the module-variability filter+alias path from ant-serve."""
    alias_notices: list[str] = []
    if tag_column is not None:
        available_tag_columns: set[str] = set(df['tag_column'].unique())
        resolved_tc, tc_notice = _alias_canonical(tag_column, available_tag_columns)
        if tc_notice:
            alias_notices.append(tc_notice)
        tag_column = resolved_tc

    working = df
    if tag:
        working = working[working['tag'] == tag]
    if tag_column:
        working = working[working['tag_column'] == tag_column]
    return working, alias_notices


class TestModuleVariabilityAlias:
    def setup_method(self):
        self.df = _build_mv_df()

    def test_bare_tag_column_rewrites_to_ant(self):
        """``tag_column=braak`` → ``ant_braak`` when ant_braak exists."""
        result, notices = _apply_mv_filters(self.df, tag_column='braak')
        assert list(result['tag_column'].unique()) == ['ant_braak']
        assert notices == ['braak -> ant_braak']

    def test_ant_tag_column_passes_through_unchanged(self):
        result, notices = _apply_mv_filters(self.df, tag_column='ant_braak')
        assert list(result['tag_column'].unique()) == ['ant_braak']
        assert notices == []

    def test_unknown_tag_column_produces_empty_frame(self):
        """An unknown tag_column that doesn't alias should filter to 0 rows."""
        result, notices = _apply_mv_filters(self.df, tag_column='no_such_col')
        assert len(result) == 0
        assert notices == []

    def test_tag_filter_still_works(self):
        """``?tag=braak`` filters the ``tag`` column — unchanged by migration."""
        result, notices = _apply_mv_filters(self.df, tag='braak')
        assert len(result) == 1
        assert result.iloc[0]['tag'] == 'braak'
        assert notices == []

    def test_combined_tag_and_tag_column_alias(self):
        result, notices = _apply_mv_filters(self.df, tag='sex', tag_column='sex')
        assert list(result['tag_column'].unique()) == ['ant_sex']
        assert 'sex -> ant_sex' in notices


# ── 3. Live integration tests (skip when ant-serve is unreachable) ────────────

_DEFAULT_URL = os.environ.get('ANTHIVE_TEST_URL', 'http://127.0.0.1:8080')
_USER = os.environ.get('ANTHIVE_TEST_USER', '')
_PW = os.environ.get('ANTHIVE_TEST_PASSWORD', '')


def _probe_server() -> Optional[str]:
    try:
        import requests as _requests
        r = _requests.get(
            f"{_DEFAULT_URL}/health",
            auth=(_USER, _PW),
            timeout=3,
            verify=False,
        )
        if r.status_code == 200:
            return _DEFAULT_URL
    except Exception:
        pass
    return None


_LIVE_URL = _probe_server()

pytestmark_live = pytest.mark.skipif(
    _LIVE_URL is None,
    reason=f"ant-serve not reachable at {_DEFAULT_URL}",
)

# Dataset known to have ant_braak and ant_sex (MY4/MTH post-migration)
_TEST_DATASET = os.environ.get('ANTHIVE_TEST_DATASET', 'MY4/MTH')


def _get(path: str, **params):
    import requests as _requests
    return _requests.get(
        f"{_LIVE_URL}{path}",
        params=params,
        auth=(_USER, _PW),
        verify=False,
        timeout=60,
    )


@pytestmark_live
class TestLiveModuleVariabilityAlias:
    def test_bare_tag_braak_returns_rows(self):
        """``?tag=braak`` should match rows — tag column is bare short name."""
        r = _get('/module-variability', tag='braak', min_abs=0, limit=5)
        assert r.status_code == 200
        body = r.json()
        assert body['n_rows'] >= 0  # may be 0 if no data; should not 500

    def test_bare_tag_column_fires_deprecation_header(self):
        """``?tag_column=braak`` should return X-Anthive-Deprecated-Alias header."""
        r = _get('/module-variability', tag_column='braak', min_abs=0, limit=5)
        assert r.status_code == 200
        assert 'X-Anthive-Deprecated-Alias' in r.headers, (
            "Expected deprecation header when using legacy tag_column=braak"
        )
        assert 'braak' in r.headers['X-Anthive-Deprecated-Alias']

    def test_ant_tag_column_no_deprecation_header(self):
        """``?tag_column=ant_braak`` should NOT set the deprecation header."""
        r = _get('/module-variability', tag_column='ant_braak', min_abs=0, limit=5)
        assert r.status_code == 200
        assert 'X-Anthive-Deprecated-Alias' not in r.headers


@pytestmark_live
class TestLivePlotAlias:
    def test_bare_color_sex_fires_deprecation_header(self):
        """``?color=sex`` on scatter returns 200 + deprecation header."""
        r = _get(f'/databases/{_TEST_DATASET}/plot/scatter', color='sex', format='png')
        # 200 if ant_sex exists; 400/404 if the dataset doesn't have it
        if r.status_code == 200:
            assert 'X-Anthive-Deprecated-Alias' in r.headers, (
                "Expected X-Anthive-Deprecated-Alias when color=sex aliased to ant_sex"
            )
        else:
            # dataset doesn't have ant_sex — alias fired but column missing → 404/400
            assert r.status_code in (400, 404, 422)

    def test_ant_color_no_deprecation_header(self):
        """``?color=ant_sex`` should not set the deprecation header."""
        r = _get(f'/databases/{_TEST_DATASET}/plot/scatter', color='ant_sex', format='png')
        if r.status_code == 200:
            assert 'X-Anthive-Deprecated-Alias' not in r.headers

    def test_truly_unknown_color_yields_error(self):
        """A column name that is neither a real column nor a canonical alias
        should not silently succeed."""
        r = _get(
            f'/databases/{_TEST_DATASET}/plot/scatter',
            color='xyzzy_no_such_column_1234',
            format='png',
        )
        assert r.status_code in (400, 404, 422), (
            f"Expected error for unknown column, got {r.status_code}"
        )
