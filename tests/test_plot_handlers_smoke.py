"""Live-API smoke tests for every plot handler.

These tests hit the running ant-serve at the URL in ANTHIVE_TEST_URL (or
the anthive.yaml default). They are NOT unit tests — they verify that
every /plot/{geom} endpoint:
  * responds 200 with a non-empty body on a representative request
  * honours format=csv (returns text/csv with a body), where the
    handler supports data export
  * honours color_scale=sequential|divergent where it's meant to take
    effect (auto / sequential / divergent give three different cache
    keys; we just verify all three return 200)

Skipped when ANTHIVE_TEST_URL is unset OR the URL doesn't respond to
/health. This keeps the test suite runnable offline.

This catches the kind of regression the QA review flagged: parameter
threading that breaks silently (B1, B2, I1). Without these tests, a
typo in `pick_cmap` or a forgotten `req.color_scale` pass-through goes
undetected until a user reports it.
"""
from __future__ import annotations

import os
from typing import Optional

import pytest

try:
    import requests
except ImportError:
    pytest.skip("requests not installed", allow_module_level=True)


_DEFAULT_URL = os.environ.get(
    'ANTHIVE_TEST_URL', 'https://localhost:8501/api',
)
_USER = os.environ.get('ANTHIVE_TEST_USER', 'mf')
_PW = os.environ.get('ANTHIVE_TEST_PASSWORD', 'ahToehR5g1xsEpg')


def _probe() -> Optional[str]:
    """Return the working base URL or None if the server isn't reachable."""
    try:
        r = requests.get(f"{_DEFAULT_URL}/health", auth=(_USER, _PW),
                         verify=False, timeout=3)
        if r.status_code == 200:
            return _DEFAULT_URL
    except Exception:
        pass
    return None


_BASE_URL = _probe()

pytestmark = pytest.mark.skipif(
    _BASE_URL is None,
    reason=f"ant-serve not reachable at {_DEFAULT_URL}",
)


@pytest.fixture(scope='module')
def base_url():
    return _BASE_URL


def _get(path: str, **params):
    url = f"{_BASE_URL}{path}"
    return requests.get(
        url, params=params, auth=(_USER, _PW), verify=False, timeout=60,
    )


# Dataset fixtures: V6A has gene + numerical + categorical obs + DE +
# embeddings. V6W has module DE. SAN has DESeq2 baseMean. We use these
# as known-shape probes.

V6A = 'S25/V6A'
V6W = 'S25/V6W'
SAN = 'SF9/SAN'


# ── geom-coverage matrix ──────────────────────────────────────────────


@pytest.mark.parametrize('geom,params', [
    ('scatter',  dict(x='X_umap', color='Strain', sample=200)),
    ('hexbin',   dict(x='X_umap', color='Strain', sample=200, gridsize=20)),
    ('violin',   dict(x='Strain', y='ARM_100_microtype', sample=200)),
    ('box',      dict(x='Strain', y='ARM_100_microtype', sample=200)),
    ('bar',      dict(x='Strain', sample=200)),
    ('histogram',dict(x='ARM_100_microtype', sample=200)),
    ('ecdf',     dict(x='ARM_100_microtype', sample=200)),
    ('kde',      dict(x='ARM_100_microtype', sample=200)),
    ('heatmap',  dict(x='APOE,TREM2', y='Strain', sample=500)),
    ('kde2d',    dict(x='ARM_100_microtype',
                     y='Antigen-presenting response (HLA)_microtype',
                     sample=500, kde_n=200)),
])
def test_geom_renders_png(geom, params):
    """Every cell-level geom returns a non-empty PNG on a small sample."""
    r = _get(f"/databases/{V6A}/plot/{geom}", **params, cache='miss')
    assert r.status_code == 200, (
        f"{geom}: HTTP {r.status_code} body={r.text[:300]}"
    )
    assert r.headers.get('content-type', '').startswith('image/'), (
        f"{geom}: unexpected content-type {r.headers.get('content-type')}"
    )
    assert len(r.content) > 1000, (
        f"{geom}: image suspiciously small ({len(r.content)} bytes)"
    )


@pytest.mark.parametrize('geom,params', [
    ('scatter',  dict(x='X_umap', color='Strain', sample=200)),
    ('hexbin',   dict(x='X_umap', color='Strain', sample=200, gridsize=20)),
    ('violin',   dict(x='Strain', y='ARM_100_microtype', sample=200)),
    ('bar',      dict(x='Strain', sample=200)),
    ('histogram',dict(x='ARM_100_microtype', sample=200)),
    ('heatmap',  dict(x='APOE,TREM2', y='Strain', sample=200)),
])
def test_geom_csv_export(geom, params):
    """format=csv on cell-level geoms returns text/csv with a body."""
    r = _get(f"/databases/{V6A}/plot/{geom}", **params, format='csv',
             cache='miss')
    assert r.status_code == 200, (
        f"{geom}+csv: HTTP {r.status_code} body={r.text[:300]}"
    )
    ct = r.headers.get('content-type', '')
    assert ct.startswith('text/csv'), f"{geom}+csv: ct={ct}"
    assert r.headers.get('content-disposition', '').startswith('attachment'), (
        f"{geom}+csv: no attachment header"
    )
    # First line is the header; second line should be a real row.
    lines = r.text.splitlines()
    assert len(lines) >= 2, f"{geom}+csv: only {len(lines)} lines, content={r.text[:200]}"


# ── color_scale param ────────────────────────────────────────────────


@pytest.mark.parametrize('geom,params', [
    ('scatter',  dict(x='X_umap', color='ARM_100_microtype', sample=200)),
    ('hexbin',   dict(x='X_umap', color='ARM_100_microtype', sample=200, gridsize=20)),
    ('heatmap',  dict(x='APOE,TREM2', y='Strain', sample=200)),
    ('kde2d',    dict(x='ARM_100_microtype',
                     y='Antigen-presenting response (HLA)_microtype',
                     sample=500, kde_n=200)),
])
@pytest.mark.parametrize('color_scale', ['auto', 'sequential', 'divergent'])
def test_color_scale_threads(geom, params, color_scale):
    """color_scale is accepted (no 422) and returns 200 for geoms with
    continuous color. The renders differ but we only assert success
    here — visual difference is verified by hand."""
    r = _get(f"/databases/{V6A}/plot/{geom}", **params,
             color_scale=color_scale, cache='miss')
    assert r.status_code == 200, (
        f"{geom}+color_scale={color_scale}: HTTP {r.status_code} body={r.text[:300]}"
    )


# ── bar-specific knobs ────────────────────────────────────────────────


def test_bar_dodge_default():
    r = _get(f"/databases/{V6A}/plot/bar", x='Strain', sample=200,
             cache='miss')
    assert r.status_code == 200


def test_bar_stack_mode():
    r = _get(f"/databases/{V6A}/plot/bar", x='Strain', color='Sex_donor',
             bar_mode='stack', sample=200, cache='miss')
    assert r.status_code == 200


def test_bar_stack_normalize():
    r = _get(f"/databases/{V6A}/plot/bar", x='Strain', color='Sex_donor',
             bar_mode='stack', normalize='true', sample=200, cache='miss')
    assert r.status_code == 200


def test_bar_facet():
    r = _get(f"/databases/{V6A}/plot/bar", x='Strain', color='Sex_donor',
             facet='Village', sample=500, cache='miss')
    assert r.status_code == 200


def test_bar_group_by():
    r = _get(f"/databases/{V6A}/plot/bar", x='mouse',
             color='patient_geno_short', bar_mode='stack',
             group_by='Village', sample=500, cache='miss')
    assert r.status_code == 200


def test_bar_cap_enforced():
    """High-cardinality x without filtering → 400 with the helpful detail."""
    r = _get(f"/databases/GF4/MIC/plot/bar", x='batch', cache='miss')
    assert r.status_code == 400
    assert 'capped at' in r.json().get('detail', '')


def test_bar_facet_cap_enforced():
    r = _get(f"/databases/{V6A}/plot/bar", x='Strain',
             color='patient_geno_short', facet='mouse', cache='miss')
    assert r.status_code == 400
    assert 'facet' in r.json().get('detail', '').lower()


# ── DE-driven geoms ───────────────────────────────────────────────────


def test_volcano():
    r = _get(f"/databases/{SAN}/plot/volcano",
             study='sala_microglia_genoKvsW', term='genotype',
             contrast='K_vs_W', cache='miss')
    assert r.status_code == 200, r.text[:300]


def test_ma():
    r = _get(f"/databases/{SAN}/plot/ma",
             study='sala_microglia_genoKvsW', term='genotype',
             contrast='K_vs_W', cache='miss')
    assert r.status_code == 200, r.text[:300]


def test_forest():
    r = _get(f"/databases/{SAN}/plot/forest", gene='Apoe', cache='miss')
    assert r.status_code == 200, r.text[:300]


def test_de_heatmap_module():
    """V6W module DE — Sierksma Fig 2E target."""
    r = _get(f"/databases/{V6W}/plot/de_heatmap",
             study='Module_DE_interpretable_lmem_vs_grandmean',
             term='patient_geno_short_relevel_sexreg', cache='miss')
    assert r.status_code == 200, r.text[:300]
    assert r.headers.get('content-type', '').startswith('image/')


def test_de_heatmap_cap_fires():
    """V6A gene DE without filters → cap (29 × 10000 cells)."""
    r = _get(f"/databases/{V6A}/plot/de_heatmap",
             study='DE_interpretable_glmm_vs_grandmean',
             term='patient_geno_short_relevel_huhu_specific_sexreg',
             cache='miss')
    assert r.status_code == 400
    assert 'capped at' in r.json().get('detail', '').lower()


def test_de_heatmap_missing_filter_value():
    """Typo in contrasts= → 404 (none matched)."""
    r = _get(f"/databases/{V6W}/plot/de_heatmap",
             study='Module_DE_interpretable_lmem_vs_grandmean',
             term='patient_geno_short_relevel_sexreg',
             contrasts='nonexistent_typo_value', cache='miss')
    assert r.status_code == 404
    assert 'none of contrasts' in r.json().get('detail', '').lower()


# ── validation regressions ────────────────────────────────────────────


def test_regline_validation():
    """?regline=banana → 422 (regex mismatch)."""
    r = _get(f"/databases/{V6A}/plot/scatter", x='ARM_100_microtype',
             y='ARM_200_microtype', regline='banana')
    assert r.status_code == 422


def test_color_scale_validation():
    r = _get(f"/databases/{V6A}/plot/scatter", x='X_umap',
             color='ARM_100_microtype', color_scale='xxx')
    assert r.status_code == 422


def test_de_value_validation():
    r = _get(f"/databases/{V6W}/plot/de_heatmap",
             study='Module_DE_interpretable_lmem_vs_grandmean',
             term='patient_geno_short_relevel_sexreg', value='xxx')
    assert r.status_code == 422


def test_color_embedding_rejected():
    """color=<embedding> on scatter → 400 with helpful message."""
    r = _get(f"/databases/{V6A}/plot/scatter", x='X_umap',
             color='X_umap', cache='miss')
    assert r.status_code == 400
    assert 'embedding' in r.json().get('detail', '').lower()


def test_kde2d_regline_by_group_rejected():
    """kde2d has no categorical input — by_group is meaningless and gets 422."""
    r = _get(f"/databases/{V6A}/plot/kde2d", x='ARM_100_microtype',
             y='Antigen-presenting response (HLA)_microtype',
             regline='by_group')
    assert r.status_code == 422


# ── format=csv on DE geoms ────────────────────────────────────────────


def test_volcano_csv():
    r = _get(f"/databases/{SAN}/plot/volcano",
             study='sala_microglia_genoKvsW', term='genotype',
             contrast='K_vs_W', format='csv', cache='miss')
    assert r.status_code == 200
    assert r.headers.get('content-type', '').startswith('text/csv')
    lines = r.text.splitlines()
    assert len(lines) > 100  # tens of thousands of genes, but at least a non-trivial body


def test_de_heatmap_csv():
    r = _get(f"/databases/{V6W}/plot/de_heatmap",
             study='Module_DE_interpretable_lmem_vs_grandmean',
             term='patient_geno_short_relevel_sexreg', format='csv',
             cache='miss')
    assert r.status_code == 200
    assert r.headers.get('content-type', '').startswith('text/csv')
    first_line = r.text.split('\n', 1)[0]
    assert 'contrast' in first_line and 'gene' in first_line


# ── /plot/de_heatmap: 2.10.0 features (clip + exclude + sort) ─────────


_V6W_STUDY = 'Module_DE_interpretable_lmem_vs_grandmean'
_V6W_TERM = 'patient_geno_short_relevel_sexreg'


def _de_heatmap_columns(**extra):
    """Helper: fetch a de_heatmap as CSV and return the list of distinct
    gene tokens (column-axis labels) and contrast tokens (row-axis labels)."""
    r = _get(
        f"/databases/{V6W}/plot/de_heatmap",
        study=_V6W_STUDY, term=_V6W_TERM, format='csv', cache='miss',
        **extra,
    )
    assert r.status_code == 200, r.text[:300]
    lines = r.text.strip().splitlines()
    header = lines[0].split(',')
    contrast_ix = header.index('contrast')
    gene_ix = header.index('gene')
    contrasts = []
    genes = []
    seen_g = set()
    seen_c = set()
    for line in lines[1:]:
        parts = line.split(',')
        c = parts[contrast_ix]
        g = parts[gene_ix]
        if c not in seen_c:
            seen_c.add(c)
            contrasts.append(c)
        if g not in seen_g:
            seen_g.add(g)
            genes.append(g)
    return contrasts, genes


def test_de_heatmap_vmin_vmax_clip():
    """?vmin=-2&vmax=2 — caption mentions the clip; image renders."""
    r = _get(f"/databases/{V6W}/plot/de_heatmap",
             study=_V6W_STUDY, term=_V6W_TERM,
             vmin=-2, vmax=2, cache='miss')
    assert r.status_code == 200, r.text[:300]
    assert r.headers.get('content-type', '').startswith('image/')
    caption = r.headers.get('x-plot-caption', '').lower()
    assert 'clipped to [-2, 2]' in caption, caption


def test_de_heatmap_vmax_quantile_clip():
    """?vmax_quantile=0.95 — caption mentions the quantile band."""
    r = _get(f"/databases/{V6W}/plot/de_heatmap",
             study=_V6W_STUDY, term=_V6W_TERM,
             vmax_quantile=0.95, cache='miss')
    assert r.status_code == 200, r.text[:300]
    caption = r.headers.get('x-plot-caption', '').lower()
    assert 'quantile band' in caption, caption


def test_de_heatmap_exclude_only_gene():
    """?genes=-MEbrown — every gene EXCEPT MEbrown."""
    # First grab all genes so we know what to compare against.
    _, all_genes = _de_heatmap_columns()
    # Pick a gene that's actually present so the exclude is meaningful.
    target = all_genes[0]
    _, kept_genes = _de_heatmap_columns(genes=f'-{target}')
    assert target not in kept_genes
    assert set(kept_genes) == set(all_genes) - {target}


def test_de_heatmap_redundant_negative_no_error():
    """?genes=A,B,-A — net effect = {B}. Negative on already-excluded
    value (i.e. a value not in the positive set) is also a silent no-op."""
    _, all_genes = _de_heatmap_columns()
    a, b = all_genes[0], all_genes[1]
    # Case 1: A in positives + -A → final = {B}
    _, genes = _de_heatmap_columns(genes=f'{a},{b},-{a}')
    assert set(genes) == {b}
    # Case 2: -nonexistent silently ignored
    _, genes2 = _de_heatmap_columns(genes=f'{a},{b},-DOES_NOT_EXIST')
    assert set(genes2) == {a, b}


def test_de_heatmap_sort_by_gene():
    """?sort=<gene> — rows reordered by that column's values ascending."""
    _, all_genes = _de_heatmap_columns()
    pick = all_genes[0]
    r = _get(f"/databases/{V6W}/plot/de_heatmap",
             study=_V6W_STUDY, term=_V6W_TERM,
             sort=pick, cache='miss')
    assert r.status_code == 200, r.text[:300]
    caption = r.headers.get('x-plot-caption', '').lower()
    assert 'reordered by' in caption, caption
    assert pick.lower() in caption


def test_de_heatmap_sort_unknown_label_400():
    """?sort=xyz where xyz matches no row OR column → 400 with hints."""
    r = _get(f"/databases/{V6W}/plot/de_heatmap",
             study=_V6W_STUDY, term=_V6W_TERM,
             sort='absolutely_not_a_thing', cache='miss')
    assert r.status_code == 400, r.text[:300]
    detail = r.json().get('detail', '').lower()
    assert 'matches neither' in detail, detail


def test_de_heatmap_sort_desc():
    """?sort=<gene>&sort_desc=true — caption mentions descending."""
    _, all_genes = _de_heatmap_columns()
    pick = all_genes[0]
    r = _get(f"/databases/{V6W}/plot/de_heatmap",
             study=_V6W_STUDY, term=_V6W_TERM,
             sort=pick, sort_desc='true', cache='miss')
    assert r.status_code == 200, r.text[:300]
    caption = r.headers.get('x-plot-caption', '').lower()
    assert 'descending' in caption, caption


# ── new cmap registration ─────────────────────────────────────────────


# ── /plot/de_quadrant: 2.12.0 ─────────────────────────────────────────


_S25_V6A_STUDY = 'DE_interpretable_glmm_vs_grandmean'


def test_de_quadrant_within_dataset():
    """Two terms of the same study on S25/V6A — Sex_donor/F vs sex_mouse/m.

    This is the canonical example: human donor-sex contrast (signed log2fc)
    vs the mouse-sex covariate term. Most points cluster near y=0 because
    mouse-sex has weak signal in the human contrast; Y-chrom genes
    spread on the X-axis.
    """
    r = _get(f"/databases/{V6A}/plot/de_quadrant",
             study=_S25_V6A_STUDY, term='Sex_donor', contrast='F',
             term_y='sex_mouse', contrast_y='m', cache='miss')
    assert r.status_code == 200, r.text[:300]
    assert r.headers.get('content-type', '').startswith('image/')
    caption = r.headers.get('x-plot-caption', '')
    # Caption must report all four gene-set counts (n_x, n_y, union, intersection).
    assert 'A side =' in caption and 'B side =' in caption, caption
    assert 'union =' in caption and 'intersection =' in caption, caption
    assert 'Pearson r' in caption, caption
    assert 'Spearman' in caption, caption
    # 4-way category counts
    assert 'both sig =' in caption, caption
    assert 'X-only =' in caption and 'Y-only =' in caption, caption


def test_de_quadrant_intra_two_contrasts():
    """Two contrasts of the same term on the same dataset (intra-dataset)."""
    r = _get(f"/databases/{V6A}/plot/de_quadrant",
             study=_S25_V6A_STUDY,
             term='patient_geno_short_relevel', contrast='Aipt_33',
             term_y='patient_geno_short_relevel', contrast_y='BR04',
             cache='miss')
    assert r.status_code == 200, r.text[:300]
    assert r.headers.get('content-type', '').startswith('image/')


def test_de_quadrant_cross_organism_rejected():
    """S25/V6A (human) vs SF9/SAN (mouse) → 400 with descriptive detail."""
    r = _get(f"/databases/{V6A}/plot/de_quadrant",
             study=_S25_V6A_STUDY, term='Sex_donor', contrast='F',
             db_y=SAN, study_y='sala_microglia_genoKvsW',
             term_y='genotype', contrast_y='K_vs_W', cache='miss')
    assert r.status_code == 400, r.text[:300]
    detail = r.json().get('detail', '').lower()
    assert 'cross-organism' in detail, detail
    assert 'human' in detail and 'mouse' in detail, detail


def test_de_quadrant_mixed_axes():
    """axis_x=log2fc + axis_y=signed_log10p — mixed-stat case."""
    r = _get(f"/databases/{V6A}/plot/de_quadrant",
             study=_S25_V6A_STUDY, term='Sex_donor', contrast='F',
             term_y='sex_mouse', contrast_y='m',
             axis_x='log2fc', axis_y='signed_log10p', cache='miss')
    assert r.status_code == 200, r.text[:300]
    caption = r.headers.get('x-plot-caption', '')
    assert 'X-axis: log2fc' in caption, caption
    assert 'Y-axis: signed_log10p' in caption, caption


def test_de_quadrant_hex_mode():
    r = _get(f"/databases/{V6A}/plot/de_quadrant",
             study=_S25_V6A_STUDY, term='Sex_donor', contrast='F',
             term_y='sex_mouse', contrast_y='m',
             mode='hex', cache='miss')
    assert r.status_code == 200, r.text[:300]
    assert r.headers.get('content-type', '').startswith('image/')


def test_de_quadrant_n_label_zero():
    r = _get(f"/databases/{V6A}/plot/de_quadrant",
             study=_S25_V6A_STUDY, term='Sex_donor', contrast='F',
             term_y='sex_mouse', contrast_y='m',
             n_label=0, cache='miss')
    assert r.status_code == 200, r.text[:300]


def test_de_quadrant_invalid_axis_422():
    """axis_x=banana → 422 (regex validation)."""
    r = _get(f"/databases/{V6A}/plot/de_quadrant",
             study=_S25_V6A_STUDY, term='Sex_donor', contrast='F',
             term_y='sex_mouse', contrast_y='m',
             axis_x='banana', cache='miss')
    assert r.status_code == 422


def test_de_quadrant_invalid_mode_422():
    r = _get(f"/databases/{V6A}/plot/de_quadrant",
             study=_S25_V6A_STUDY, term='Sex_donor', contrast='F',
             term_y='sex_mouse', contrast_y='m',
             mode='cube', cache='miss')
    assert r.status_code == 422


def test_de_quadrant_csv_export():
    """format=csv on de_quadrant returns the post-join dataframe."""
    r = _get(f"/databases/{V6A}/plot/de_quadrant",
             study=_S25_V6A_STUDY, term='Sex_donor', contrast='F',
             term_y='sex_mouse', contrast_y='m',
             format='csv', cache='miss')
    assert r.status_code == 200, r.text[:300]
    assert r.headers.get('content-type', '').startswith('text/csv')
    first_line = r.text.split('\n', 1)[0]
    assert 'gene_name' in first_line
    assert 'stat_x' in first_line and 'stat_y' in first_line
    assert 'padj_x' in first_line and 'padj_y' in first_line


def test_de_quadrant_registered_in_version():
    """`de_quadrant` must appear in /version.plot_geoms."""
    r = requests.get(f"{_BASE_URL}/version", auth=(_USER, _PW),
                     verify=False, timeout=10)
    assert r.status_code == 200
    assert 'de_quadrant' in r.json().get('plot_geoms', [])


# ── new cmap registration ─────────────────────────────────────────────


def test_custom_cmaps_registered():
    """The 14 white→colour cmaps registered at module import are all
    addressable from the live api."""
    for name in ('magenta', 'blue', 'red', 'green', 'orange',
                 'purple', 'cyan', 'yellow', 'black', 'tan',
                 'greenyellow', 'turquoise', 'hotpink', 'brown'):
        r = _get(f"/databases/{V6A}/plot/scatter", x='X_umap',
                 color='ARM_100_microtype', palette_continuous=name,
                 sample=100, cache='miss')
        assert r.status_code == 200, (
            f"{name}: HTTP {r.status_code} body={r.text[:200]}"
        )
