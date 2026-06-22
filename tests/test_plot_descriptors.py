"""Unit tests for `plotting.build_plot_descriptor` — biology-keyed
title + filename for plot responses (X-Plot-Title / X-Plot-Filename
headers, introduced in API 2.9.0).

Each case mirrors a real /plot/{geom} request — the descriptor function
is pure, so we exercise it directly without spinning up the server.
"""
from __future__ import annotations

import re

import pytest


# Lazy import — the conftest fixture adds lib/ to sys.path.
def _import_descriptor():
    from plotting import build_plot_descriptor  # noqa: WPS433
    return build_plot_descriptor


def test_scatter_embedding_with_gene_color():
    """Title contains the gene; filename has the gene token."""
    bpd = _import_descriptor()
    desc = bpd(
        geom='scatter', db_id='S25/V6A',
        x='X_umap', x_kind='embedding',
        color='P2RY12', color_kind='gene',
    )
    assert desc['title'] == 'S25/V6A: P2RY12 expression on X_umap'
    assert desc['filename'] == 'S25_V6A_scatter_P2RY12'
    assert desc['filename_truncated'] == '0'


def test_hexbin_subset_only():
    """No color, subset filter present → title mentions subset and
    filename includes the subset value."""
    bpd = _import_descriptor()
    desc = bpd(
        geom='hexbin', db_id='S25/V6A',
        x='X_umap', x_kind='embedding',
        filters={'patient_geno_short': 'L276'},
    )
    assert 'subset patient_geno_short=L276' in desc['title']
    assert desc['title'].startswith('S25/V6A: cell density on X_umap')
    # Filename has both the density token (no colour → density)
    # AND the subset value.
    assert 'density' in desc['filename']
    assert 'L276' in desc['filename']


def test_bar_y_gene_x_cat():
    """Title format 'mean Y per X'; filename `bar_Y_per_X`."""
    bpd = _import_descriptor()
    desc = bpd(
        geom='bar', db_id='S25/V6A',
        x='cell_type', x_kind='categorical',
        y='HLA-DQB1', y_kind='gene',
    )
    assert desc['title'] == 'S25/V6A: mean HLA-DQB1 per cell_type'
    # 'HLA-DQB1' has a `-` — must pass through (case 6 in the spec).
    assert desc['filename'] == 'S25_V6A_bar_HLA-DQB1_per_cell_type'


def test_volcano_study_term_contrast():
    """Title format '{term} / {contrast} (volcano)'; filename
    `volcano_{term}_{contrast}`."""
    bpd = _import_descriptor()
    desc = bpd(
        geom='volcano', db_id='S25/V6A',
        de_study='DE_interpretable_glmm_vs_grandmean',
        de_term='Sex_donor', de_contrast='F',
    )
    assert desc['title'] == 'S25/V6A: Sex_donor / F (volcano)'
    assert desc['filename'] == 'S25_V6A_volcano_Sex_donor_F'


def test_de_heatmap_format():
    """`de_heatmap` title surfaces the term."""
    bpd = _import_descriptor()
    desc = bpd(
        geom='de_heatmap', db_id='S25/V6W',
        de_study='Module_DE_interpretable_lmem_vs_grandmean',
        de_term='patient_geno_short_relevel_sexreg',
    )
    assert 'DE matrix' in desc['title']
    assert 'patient_geno_short_relevel_sexreg' in desc['title']
    assert desc['filename'] == 'S25_V6W_de_heatmap_patient_geno_short_relevel_sexreg'


def test_filename_sanitisation_hyphen_passes_weird_chars_stripped():
    """`HLA-DQB1` keeps the hyphen; weirder chars (·, /, spaces) are stripped."""
    bpd = _import_descriptor()
    # color with hyphen passes through untouched.
    desc = bpd(
        geom='scatter', db_id='S25/V6A',
        x='X_umap', x_kind='embedding',
        color='HLA-DQB1', color_kind='gene',
    )
    assert 'HLA-DQB1' in desc['filename']
    # Weird chars in the colour name (e.g. unicode middle dot) get stripped.
    desc2 = bpd(
        geom='scatter', db_id='S25/V6A',
        x='X_umap', x_kind='embedding',
        color='gene·name with spaces', color_kind='gene',
    )
    # The filename must be `[A-Za-z0-9_-]` only.
    assert re.fullmatch(r'[A-Za-z0-9_\-]+', desc2['filename']), desc2['filename']
    # And the original tokens (`gene`, `name`, `with`, `spaces`) survive
    # joined by single underscores.
    assert 'gene' in desc2['filename']
    assert 'spaces' in desc2['filename']


def test_filename_truncation_at_80_char_cap():
    """A request whose biology tokens push past the 80-char cap emits
    `filename_truncated='1'` and a clipped basename."""
    bpd = _import_descriptor()
    very_long_term = 'A' * 200
    desc = bpd(
        geom='volcano', db_id='S25/V6A',
        de_study='S', de_term=very_long_term, de_contrast='F',
    )
    assert len(desc['filename']) <= 80
    assert desc['filename_truncated'] == '1'
    # Truncation never leaves a trailing _ or - (visual courtesy).
    assert not desc['filename'].endswith('_')
    assert not desc['filename'].endswith('-')


# ── extra coverage — small but cheap, catches obvious drift ──────────


def test_hexbin_with_categorical_color_modal_phrasing():
    bpd = _import_descriptor()
    desc = bpd(
        geom='hexbin', db_id='S25/V6A',
        x='X_umap', x_kind='embedding',
        color='cell_type', color_kind='categorical',
    )
    assert desc['title'] == 'S25/V6A: modal cell_type on X_umap'
    assert desc['filename'] == 'S25_V6A_hexbin_cell_type'


def test_violin_y_present():
    bpd = _import_descriptor()
    desc = bpd(
        geom='violin', db_id='S25/V6A',
        x='cell_type', x_kind='categorical',
        y='P2RY12', y_kind='gene',
    )
    assert desc['title'] == 'S25/V6A: P2RY12 per cell_type'
    assert desc['filename'] == 'S25_V6A_violin_P2RY12_per_cell_type'


def test_violin_no_y_falls_back_to_counts():
    bpd = _import_descriptor()
    desc = bpd(geom='violin', db_id='S25/V6A', x='cell_type', x_kind='categorical')
    assert desc['title'] == 'S25/V6A: cell counts per cell_type'
    assert desc['filename'] == 'S25_V6A_violin_counts_per_cell_type'


def test_histogram_x_only():
    bpd = _import_descriptor()
    desc = bpd(geom='histogram', db_id='S25/V6A', x='n_counts', x_kind='numerical')
    assert desc['title'] == 'S25/V6A: n_counts distribution'
    # Histogram compresses to `hist` in the filename geom token.
    assert desc['filename'] == 'S25_V6A_hist_n_counts'


def test_forest_gene_only():
    bpd = _import_descriptor()
    desc = bpd(geom='forest', db_id='S25/V6A', de_gene='P2RY12')
    assert 'P2RY12' in desc['title']
    assert desc['filename'] == 'S25_V6A_forest_P2RY12'


def test_xy_scatter_continuous_x_y():
    """When x is gene/numerical and y given, we're in XY-mode."""
    bpd = _import_descriptor()
    desc = bpd(
        geom='scatter', db_id='S25/V6A',
        x='APOE', x_kind='gene',
        y='P2RY12', y_kind='gene',
    )
    assert desc['title'] == 'S25/V6A: P2RY12 vs APOE'
    assert desc['filename'] == 'S25_V6A_scatter_P2RY12_vs_APOE'


def test_subset_multiple_clauses_summarised():
    bpd = _import_descriptor()
    desc = bpd(
        geom='hexbin', db_id='S25/V6A',
        x='X_umap', x_kind='embedding',
        filters={'patient_geno_short': 'L276', 'sex_donor': 'F'},
    )
    # First clause shown; the rest summarised as "(+N more)".
    assert 'subset patient_geno_short=L276' in desc['title']
    assert '(+1 more)' in desc['title']
