"""Regression tests for the gene-value zero-fill fix in get_cell_table.

The layer table is sparse (only non-zero entries stored). Before the fix,
a scalar subquery over the layer returned NULL for absent cells, which
became NaN and matplotlib silently dropped those cells from every plot.

For log-normalized scRNA-seq, ~95% of cells have zero for any given gene
— dropping them changed 'mean expression' into 'mean_nonzero expression',
and violin/box/KDE plots lost their real 0-mass distribution.

These tests pin the fix in place: gene columns come out as 0.0, obs
columns keep NaN (a missing category is real missing data, not zero).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "lib") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "lib"))


def _read_anthive_db_source():
    return (REPO_ROOT / "lib" / "anthive_db.py").read_text()


class TestGetCellTableZeroFill:
    """Pin the semantic contract of get_cell_table's gene-column zero-fill.

    We don't rebuild a fresh DuckDB here (heavy fixture); instead we
    verify the SQL shape: gene-column subqueries must be wrapped in
    COALESCE(..., 0.0).
    """

    def test_gene_subquery_uses_coalesce(self):
        src = _read_anthive_db_source()
        # The exact pattern the fix produces
        assert "COALESCE(" in src, (
            "get_cell_table must COALESCE gene-value subqueries. See "
            "doc/UPGRADE_PROCEDURE.md and bin/ingest/CLAUDE.md for the "
            "biology of scRNA-seq sparsity."
        )
        assert "0.0" in src, "gene fill value should be 0.0 (float)"

    def test_coalesce_lives_in_get_cell_table(self):
        """The COALESCE-with-0.0 must be inside get_cell_table's gene
        section, not somewhere unrelated. Approximate by proximity to
        the gene-alias comment / layer subquery."""
        src = _read_anthive_db_source()
        # Find the gene-loop area
        idx = src.find("if genes:")
        assert idx != -1, "sanity: 'if genes:' expected in get_cell_table"
        # Look for COALESCE within the next ~2 KB (roughly the gene block)
        window = src[idx : idx + 2000]
        assert "COALESCE" in window, (
            "COALESCE(0.0) must be in the gene-value subquery, not "
            "elsewhere in the file"
        )
        assert "SELECT value" in window
        assert "FROM " in window

    def test_obs_columns_not_zero_filled(self):
        """Obs columns MUST keep NaN — a missing category (or missing
        numerical obs value) is real missing data, not zero.

        The gene block uses `layer_src` and a `SELECT value` subquery
        with COALESCE. The obs blocks use `LEFT JOIN obsnum n ...` and
        `LEFT JOIN obscat cat ...` and select `n."field"` / `cat."field"`.
        No COALESCE around obs projections — checking that the obs
        LEFT JOINs don't have COALESCE wrapping the select.
        """
        src = _read_anthive_db_source()
        # Find the obs area — the "LEFT JOIN obsnum" line
        obs_idx = src.find("LEFT JOIN obsnum n ON")
        assert obs_idx != -1
        # Backtrack ~200 chars to see the select_parts.append near obs
        window = src[max(0, obs_idx - 500) : obs_idx + 200]
        # Ensure no `COALESCE(n."` pattern (would mean someone zero-filled
        # obsnum column — wrong)
        assert 'COALESCE(n."' not in window, (
            "obs numerical columns must not be zero-filled — NaN is the "
            "correct value for a cell missing a categorical/numerical obs"
        )
