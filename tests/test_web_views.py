"""Tests for the gene, gene set and facet views (the new tabs next to the atlas, geneset and facet pages).

Same approach as ``test_web_browser.py``: ``web/browser.py`` is loaded by path, ``_api_get`` is replaced by an
in-process fake and the real Jinja templates are rendered through a ``TestClient``. The views build most of their
markup client-side, so these tests check the server contract (tabs, embedded dataset map, routes), the
wording that must be visible, and that every rendered script parses (when ``node`` is installed).
"""

# pyright: reportMissingImports=false

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import requests

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"
_spec = importlib.util.spec_from_file_location("anthive_web_browser_views", _WEB_DIR / "browser.py")
browser = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(browser)

_DATABASES = [
    {"id": "A/n1", "title": "Nuclei microglia", "celltype": "microglial cell", "facet_scope": "human_postmortem",
     "organism": "human", "suspension_type": "nucleus", "experimental_system": "post_mortem_tissue",
     "obs": {"categorical": ["sample", "ant_cell_type"]}},
    {"id": "A/c1", "title": "Cells microglia", "celltype": "microglial cell", "facet_scope": "human_postmortem",
     "organism": "human", "suspension_type": "cell", "experimental_system": "post_mortem_tissue"},
    {"id": "B/x1", "title": "Xenograft microglia", "celltype": "microglial cell", "facet_scope": "xenograft_human_in_mouse",
     "organism": "human", "suspension_type": "cell", "experimental_system": "xenograft", "is_secondary": True},
    {"id": "B/mix", "title": "Mixed", "celltype": ["microglial cell", "astrocyte"], "facet_scope": "human_postmortem",
     "organism": "human", "suspension_type": "nucleus", "modality": "nuclei"},
    {"id": "C/mouse", "title": "Mouse", "celltype": "microglial cell", "organism": "mouse", "suspension_type": "cell"},
]
_GENESET = {
    "geneset_id": "Study1/Exp1/DAM", "display_name": "DAM", "description": "", "format": "grp", "n_genes": 3,
    "genes": ["A", "B", "C"], "scored_in": ["A/n1", "A/c1"], "column_name": "Study1::Exp1::DAM", "direction": None,
    "geneset_metadata": {}, "experiment_metadata": {},
}


def _fake(api_url, path, **kwargs):
    path = path.split("?")[0]
    if path == "/databases":
        return {"databases": _DATABASES}
    if path.startswith("/genesets/Study1/Exp1/DAM"):
        return _GENESET
    if path.startswith("/facets/ant_disease"):
        return {"facet": "ant_disease", "type": "categorical", "n_datasets": 1, "total_n_cells": 10, "datasets": [],
                "definition": {"defined": True, "summary": "Disease."}, "scope": "human_postmortem",
                "scopes": [{"scope": "human_postmortem", "n_datasets": 1, "tested": True}]}
    if path == "/databases/A/n1/de":
        return {"studies": [{"study_id": "s1", "name": "Study one", "method": "glmm", "reference_type": "vsgrandmean"}]}
    if path.startswith("/databases/") and path.endswith("/de"):
        return {"studies": []}
    if path == "/databases/A/n1/de/by-gene/PTPRG":
        return {"gene": "PTPRG", "rows": [
            {"study_id": "s1", "study_name": "Study one", "method": "glmm", "term": "Sex_donor", "contrast": "F",
             "log2fc": 0.3, "padj": 1e-5, "pvalue": 1e-6},
            {"study_id": "s1", "study_name": "Study one", "method": "glmm", "term": "Strain", "contrast": "AppNLGF - AppHuHu",
             "log2fc": -0.2, "padj": 0.2, "pvalue": 0.1},
            {"study_id": "s1", "study_name": "Study one", "method": "glmm", "term": "intercept", "contrast": "x",
             "log2fc": 1.0, "padj": 0.0, "pvalue": 0.0},
            {"study_id": "s1", "study_name": "Study one", "method": "glmm", "term": "T", "contrast": "y",
             "log2fc": None, "padj": None, "pvalue": None}]}
    if "/de/by-gene/" in path:
        return {"gene": "PTPRG", "rows": []}
    raise requests.HTTPError(f"404 for {path}")


@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(browser, "_api_get", _fake)
    monkeypatch.setattr(browser, "_facets_visible_cfg", [[]])
    monkeypatch.setattr(browser, "_author_de_state", {"at": 0.0, "studies": {}})
    app = FastAPI()
    app.state.api_url = "http://testapi"
    app.state.reports = []
    app.state.genesets_available = True
    app.include_router(browser.router)
    return TestClient(app)


# ── server side helpers ────────────────────────────────────────────────────────

@pytest.mark.parametrize("db, expected", [
    ({"suspension_type": "nucleus"}, "nuclei"),
    ({"suspension_type": "cell", "experimental_system": "post_mortem_tissue"}, "cells"),
    ({"suspension_type": "cell", "experimental_system": "xenograft"}, "sorted live cells"),
    ({"suspension_type": "cell", "experimental_system": "chimera"}, "sorted live cells"),
    ({"suspension_type": "na"}, ""),
    ({}, ""),
    ({"suspension_type": "cell", "modality": "Nuclei"}, "nuclei"),
])
def test_view_modality(db, expected):
    assert browser._view_modality(db) == expected


def test_view_db_map_groups_scope_and_flags(monkeypatch):
    monkeypatch.setattr(browser, "_api_get", _fake)
    result = browser._view_db_map("http://x")
    assert result["A/n1"] == {"title": "Nuclei microglia", "celltype_col": "ant_cell_type", "scope": "human_postmortem", "group": "microglia",
                              "human": True, "secondary": False, "modality": "nuclei", "category": "post_mortem", "facet_exclude_reason": ""}
    assert result["A/c1"]["modality"] == "cells"
    assert result["B/x1"]["secondary"] is True and result["B/x1"]["modality"] == "sorted live cells"
    assert result["B/mix"]["group"] == ""          # several cell types: not summarised
    assert result["C/mouse"]["human"] is False and result["C/mouse"]["scope"] == ""


def test_view_db_map_survives_an_unreachable_api(monkeypatch):
    def boom(*args, **kwargs):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(browser, "_api_get", boom)
    assert browser._view_db_map("http://x") == {}


@pytest.mark.parametrize("row, study, expected", [
    ({"term": "Strain", "contrast": "AppNLGF - AppHuHu"}, {}, "AppNLGF versus AppHuHu (Strain)"),
    ({"term": "Sex_donor", "contrast": "F"}, {"reference_type": "vsgrandmean"}, "F versus the average of all levels (Sex donor)"),
    ({"term": "Treatment", "contrast": "Lec_vs_iGg1"}, {"reference_type": "vsreference"}, "Lec versus iGg1 (Treatment)"),
    ({"term": "batch", "contrast": "ASI021"}, {}, "batch: ASI021"),
    ({"term": "batch", "contrast": ""}, {}, "batch"),
])
def test_plain_de_label(row, study, expected):
    assert browser._plain_de_label(row, study) == expected


# ── routes ─────────────────────────────────────────────────────────────────────

def test_gene_page_has_the_three_tabs_and_the_dataset_map(client):
    text = client.get("/genes/PTPRG").text
    for marker in ('data-vtab="expression"', 'data-vtab="shifts"', 'data-vtab="variability"', 'data-vtab="atlas"', 'data-vtab="de"',
                   'id="vt-expression"', 'id="vt-shifts"', 'id="vt-variability"', 'id="vt-atlas"', 'id="vwg-p1"', 'id="vwg-p2"', 'id="vwg-headline"'):
        assert marker in text, marker
    for label in ("Expression", "Facet DE", "Donor variability", "Author DE (as published)", "Atlas (old)"):
        assert label in text, label
    for hash_name in ("expression", "facet-de", "variability", "author-de", "atlas"):
        assert f'data-hash="{hash_name}"' in text, hash_name
    assert text.index('id="vwg-headline"') > text.index('id="vt-shifts"')      # facet summary sentence lives in the Facet DE tab
    assert not re.search(r'data-vtab="\w+"[^>]*>\d\. ', text)               # no numbers on tab labels
    assert text.index('data-vtab="de"') < text.index('data-vtab="atlas"')      # the old atlas tab is last
    assert "expression across the catalogue" in text
    assert 'data-vtab="de" data-hash="author-de" role="tab" hidden' in text      # shown only when DE exists
    assert '<col class="c-name">' in text and "table-layout: fixed" in text      # panel 1 is one fixed-width table
    dbs = json.loads(re.search(r"var DBS = (\{.*?\});\n", text, re.S).group(1))
    assert dbs["A/n1"]["modality"] == "nuclei" and dbs["A/c1"]["group"] == "microglia"
    assert "Donor variability" in text and "Coming later" in text      # panel 3 placeholder
    assert "Detection rates are not comparable between nuclei and cells" in text
    assert "Braak stage and pathologic AD are one pathology axis" in text


def test_gene_list_and_empty_gene_page_have_no_views(client):
    assert 'data-vtab="expression"' not in client.get("/genes/PTPRG,APOE").text
    assert 'data-vtab="expression"' not in client.get("/genes").text


def test_author_de_route_labels_filters_and_sorts(client):
    data = client.get("/genes/PTPRG/author-de").json()
    labels = [row["label"] for row in data["rows"]]
    assert labels == ["F versus the average of all levels (Sex donor)", "AppNLGF versus AppHuHu (Strain)"]
    assert data["rows"][0]["db_id"] == "A/n1" and data["rows"][0]["collection"] == ""
    assert client.get("/genes/NOPE/author-de").json() == {"gene": "NOPE", "rows": []}


def test_author_de_dataset_scan_is_cached(client, monkeypatch):
    calls = []

    def counting(api_url, path, **kwargs):
        calls.append(path)
        return _fake(api_url, path, **kwargs)
    monkeypatch.setattr(browser, "_api_get", counting)
    client.get("/genes/PTPRG/author-de")
    scans = [p for p in calls if p.endswith("/de")]
    client.get("/genes/PTPRG/author-de")
    assert [p for p in calls if p.endswith("/de")] == scans


def test_author_de_route_reports_an_unavailable_api(client, monkeypatch):
    def boom(*args, **kwargs):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(browser, "_api_get", boom)
    assert client.get("/genes/PTPRG/author-de").json()["rows"] == []


def test_geneset_page_has_the_view_tab(client):
    text = client.get("/genesets/Study1/Exp1/DAM").text
    for marker in ('data-vtab="score"', 'data-vtab="shifts"', 'data-vtab="variability"', 'data-vtab="page"', 'id="vt-score"', 'id="vt-shifts"',
                   'id="vt-variability"', 'id="vt-page"', 'id="vws-p1"', 'id="vws-p2"', 'id="vws-headline"', 'id="vws-p1-help" hidden', 'id="vws-p2-help" hidden'):
        assert marker in text, marker
    for label in ("Expression", "Module score", "Facet DE", "Donor variability", "Geneset page (old)"):
        assert label in text, label
    assert text.index('id="vws-headline"') > text.index('id="vt-shifts"')
    assert "doc-block" not in text.split('id="vt-page"')[0].split('id="vw-tabs"')[1]       # no doc_block clutter in the view tabs
    payload = json.loads(re.search(r"var PAYLOAD = (\{.*?\});\n", text, re.S).group(1))
    assert payload["scored_in"] == ["A/n1", "A/c1"] and payload["column_name"] == "Study1::Exp1::DAM"
    assert "Rank among gene sets" in text and "within one dataset only, never across datasets" in text
    assert "depends on the number of genes in the set" in text and "Mean module score" not in text
    assert "/rank?db_id=" in text and "UCell is not used" in text
    assert "A mean score is not well suited" in text
    assert "Rank among modules" not in text


def test_facet_page_has_overview_and_detail_tabs(client):
    text = client.get("/facets/ant_disease").text
    for marker in ('data-vtab="overview"', 'data-vtab="detail"', 'id="vt-overview"', 'id="vt-detail"', 'id="vwf-answer"', 'id="facet-effects"'):
        assert marker in text, marker
    assert "Details for experts" in text or "vwf-expert" in text
    assert "Detail (old)" in text and "Detail (current)" not in text
    for header in ("1. Cell types", "2. Replicated genes", "3. Gene sets", "4. Cohorts"):
        assert header in text, header
    for panel in ("vwf-p1-help", "vwf-p2-help", "vwf-p3-help", "vwf-p4-help"):
        assert f'id="{panel}" hidden' in text, panel


def test_facet_page_without_tests_keeps_the_old_page_only(client):
    text = client.get("/facets/ant_notatestedfacet").text
    assert 'data-vtab="overview"' not in text


# ── wording and scripts ────────────────────────────────────────────────────────

_VIEW_TEMPLATES = ("_views.html", "_view_gene.html", "_view_geneset.html", "_view_facet.html", "_effect_bar.html")


@pytest.mark.parametrize("name", _VIEW_TEMPLATES)
def test_view_templates_have_no_dashes_and_avoid_banned_words(name):
    text = (_WEB_DIR / "templates" / name).read_text(encoding="utf-8")
    assert "—" not in text and "–" not in text
    assert "independent cohorts" not in text            # "cohorts", never "independent cohorts"
    assert "direction-consistent, small" not in text.lower()
    assert not re.search(r"\bsmall effect", text)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("path", ["/genes/PTPRG", "/genesets/Study1/Exp1/DAM", "/facets/ant_disease"])
def test_rendered_scripts_parse(client, tmp_path, path):
    html = client.get(path).text
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert scripts
    for index, script in enumerate(scripts):
        target = tmp_path / f"s{index}.js"
        target.write_text(script, encoding="utf-8")
        done = subprocess.run(["node", "--check", str(target)], capture_output=True, text=True)
        assert done.returncode == 0, f"{path} script {index}: {done.stderr[:400]}"


def test_help_has_the_view_topics_and_glossary_terms():
    import sys
    sys.path.insert(0, str(_WEB_DIR.parent / "lib"))
    import help_content
    for topic_id in ("gene-view", "geneset-view", "facet-overview"):
        topic = help_content.get_topic(topic_id)
        assert topic is not None and topic.body_html.strip()
        assert "—" not in topic.body_html
    for slug in ("low-detection", "modality"):
        assert help_content.get_term(slug) is not None
    assert "gene-view" in help_content.get_topic("genes").children
    assert "facet-overview" in help_content.get_topic("facet-effects").children


def test_section_help_and_col_info_macros_render_the_shared_pattern():
    template = browser.templates.env.from_string(
        '{% from "_macros.html" import section_help, col_info %}'
        '{% call section_help("1. Title", "x-help") %}<p>body</p>{% endcall %}{{ col_info("tip", "r-value") }}')
    html = template.render()
    assert '<h2 class="section-head">1. Title<button type="button" class="help-btn" data-help-for="x-help"' in html
    assert '<div class="help-panel" id="x-help" hidden><p>body</p></div>' in html
    assert '<span class="info" title="tip" data-glossary="r-value"' in html


def test_gene_page_uses_numbered_help_headers_and_drops_old_labels(client):
    text = client.get("/genes/PTPRG").text
    for header in ("1. Where is it expressed", "Facet DE by cell type", "3. Donor variability"):
        assert header in text, header
    for panel in ("vwg-p1-help", "vwg-p2-help", "vwg-p3-help"):
        assert f'id="{panel}" hidden' in text
    for gone in ("panel 1, descriptive", "tested per donor or mouse", "Mean, log-normalised", "low det."):
        assert gone not in text, gone
    assert "without per-cell cell types" in text and "dataset-level label" in text
    assert "Detected in only" in text and "percentile of the gene's mean (log-normalised)" in text


@pytest.mark.parametrize("name, needles", [
    ("_views.html", ("vw-dplot", "X-Plot-Caption", "adjusted for covariates", "per-cell view in", "donor values (JSON)", "table-layout: fixed", 'class="eb-cell"', "numberHtml")),
    ("_view_gene.html", ("plot by cell type", "dataset page", "unitsTable")),
    ("_view_geneset.html", ("plot by cell type", "unitsTable", "vws-p2-help")),
])
def test_view_scripts_carry_the_donor_plot_links_and_q_column(name, needles):
    text = (_WEB_DIR / "templates" / name).read_text(encoding="utf-8")
    for needle in needles:
        assert needle in text, needle


@pytest.mark.parametrize("path, hashes", [
    ("/genes/PTPRG", ("expression", "facet-de", "variability", "author-de", "atlas")),
    ("/genesets/Study1/Exp1/DAM", ("module-score", "facet-de", "variability", "page")),
    ("/facets/ant_disease", ("overview", "detail")),
])
def test_every_tab_has_a_deep_link_hash_and_a_panel(client, path, hashes):
    text = client.get(path).text
    for hash_name in hashes:
        assert f'data-hash="{hash_name}"' in text, hash_name
    assert "initTabs" in text


@pytest.mark.parametrize("name", ["genes.html", "geneset.html", "facet.html"])
def test_view_page_templates_have_no_em_dash(name):
    assert "\u2014" not in (_WEB_DIR / "templates" / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name, expected", [
    ({"obs": {"categorical": ["x", "ant_cell_type", "cell_type"]}}, "ant_cell_type"),
    ({"obs": {"categorical": ["x", "Cell_Type"]}}, "Cell_Type"),
    ({"obs": {"categorical": ["cluster"]}}, ""),
    ({"obs": None}, ""),
    ({}, ""),
])
def test_view_celltype_column(name, expected):
    assert browser._view_celltype_column(name) == expected


# ── effect bar, ranked shifts, genes tab ───────────────────────────────────────

def _effect_bar_script():
    text = (_WEB_DIR / "templates" / "_effect_bar.html").read_text(encoding="utf-8")
    return re.search(r"<script>(.*?)</script>", text, re.S).group(1)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_effect_bar_scales_arrowheads_fill_and_donor_stats(tmp_path):
    script = tmp_path / "bar.js"
    script.write_text("var window = {};\n" + _effect_bar_script() + """
var bar = window.antEffectBar, out = {};
out.keys = [bar.scaleKey('sex', 'lfc'), bar.scaleKey('ant_braak', 'lfc'), bar.scaleKey('age_adjusted', 'sd'), bar.scaleKey('apoe4_dose', 'lfc')];
out.max = out.keys.map(function (k) { return bar.scaleOf(k).max; });
out.faint = [bar.scaleOf('lfc_two').faint, bar.scaleOf('lfc_braak').faint];
var capped = bar.barHtml({ scale: 'lfc_braak', est: 0.9, lo: 0.1, hi: 1.2, sig: true });
out.capped = [capped.indexOf('eb-arrow r') >= 0, capped.indexOf(' up') >= 0, capped.indexOf('filled') >= 0, capped.indexOf('width:50%') >= 0];
var open = bar.barHtml({ scale: 'lfc_two', est: -0.2, sig: false });
out.open = [open.indexOf(' down') >= 0, open.indexOf('open') >= 0, open.indexOf('eb-arrow') < 0];
out.grey = bar.barHtml({ scale: 'lfc_two', est: 0.5, counted: false }).indexOf('grey') >= 0;
out.axis = bar.axisHtml('lfc_braak');
out.two = bar.pointStats({ n_donors: 17, n_donors_test: 9, n_donors_ref: 8 }, 'sex');
out.trend = [bar.pointStats({ n_donors: 29 }, 'braak').small, bar.pointStats({ n_donors: 30 }, 'braak').small];
out.fallback = [bar.pointStats({ small: true }, 'sex').small, bar.pointStats({}, 'sex').small];
console.log(JSON.stringify(out));
""", encoding="utf-8")
    done = subprocess.run(["node", str(script)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["keys"] == ["lfc_two", "lfc_braak", "sd_decade", "lfc_e4"] and out["max"] == [1, 0.3, 0.3, 0.5]
    assert out["faint"][0] == pytest.approx(0.1375) and out["faint"][1] is None
    assert out["capped"] == [True, True, True, True] and out["open"] == [True, True, True] and out["grey"] is True
    assert "per Braak stage" in out["axis"] and "+0.3" in out["axis"]
    assert out["two"]["donors"] == "9 vs 8" and out["two"]["small"] is True and out["two"]["minGroup"] == 8
    assert out["trend"] == [True, False] and out["fallback"] == [True, None]


def test_every_page_carries_the_effect_bar(client):
    assert "window.antEffectBar" in client.get("/genes/PTPRG").text


def test_geneset_page_has_ranked_shifts_genes_tab_and_hides_donor_variability(client):
    text = client.get("/genesets/Study1/Exp1/DAM").text
    assert 'data-vtab="genes" data-hash="genes"' in text and 'id="vt-genes"' in text and 'id="vws-genes-body"' in text
    assert re.search(r'data-vtab="variability"[^>]*\bhidden>', text)
    for needle in ("Ranked shifts", "rankedShifts", "Member genes replicating", "family_rank", "Replication not possible: only one informative cohort",
                   "after adjusting over", "just above the 0.05 line", "ranks low by construction", "vws-provenance"):
        assert needle in text or needle in (_WEB_DIR / "templates" / "geneset.html").read_text(encoding="utf-8"), needle
    assert 'class="gn"' not in text and "replicated, strict test passed" in text      # the strict swatch is dropped only for modules in the script
    assert "Geneset page (old)" in text


def test_gene_page_hides_donor_variability_tab(client):
    assert re.search(r'data-vtab="variability"[^>]*\bhidden>', client.get("/genes/PTPRG").text)


def test_geneset_provenance_lists_experiment_method_source_dataset_and_source_paper(monkeypatch):
    monkeypatch.setattr(browser, "_dataset_papers", lambda api_url, db_id, scope="dataset": [
        {"role": "source", "citation_line": "Smith et al., Nature, 2024", "url": "/papers/smith", "title": "T"},
        {"role": "related", "citation_line": "Other", "url": "/papers/other", "title": "O"}])
    payload = {"method": "WGCNA", "organism": "human", "parent_dataset": "S25/PhM",
               "experiment_metadata": {"name": "Sierksma WGCNA", "description": " About it. ", "software": "WGCNA", "derived_by": "A. B."},
               "geneset_metadata": {}}
    info = browser._geneset_provenance("http://api", payload, "Sierksma2025", "WGCNA")
    labels = {label: (text, href) for label, text, href in info["items"]}
    assert labels["Experiment"] == ("Sierksma WGCNA", "/genesets/Sierksma2025/WGCNA")
    assert labels["Method"][0] == "WGCNA" and labels["Source dataset"] == ("S25/PhM", "/browse/S25/PhM")
    assert labels["Derived by"][0] == "A. B." and [p["citation_line"] for p in info["papers"]] == ["Smith et al., Nature, 2024"]
    assert info["description"] == "About it."
    bare = browser._geneset_provenance("http://api", {}, "S", "E")
    assert bare["papers"] == [] and [item[0] for item in bare["items"]] == ["Experiment"]


@pytest.mark.parametrize("db, expected", [
    ({"experimental_system": "post_mortem_tissue", "technology": "10x_chromium"}, "post_mortem"),
    ({"experimental_system": "postmortem_tissue"}, "post_mortem"),
    ({"experimental_system": "xenograft", "technology": "10x_chromium"}, "xenograft"),
    ({"experimental_system": "chimera"}, "xenograft"),
    ({"experimental_system": "fresh_biopsy"}, "biopsy"),
    ({"experimental_system": "live_biopsy", "facet_scope": "human_postmortem"}, "biopsy"),
    ({"experimental_system": "in_vitro_2d", "technology": "10x_chromium"}, "in_vitro"),
    ({"experimental_system": "post_mortem_tissue", "technology": "10x_visium"}, "spatial"),
    ({"experimental_system": "xenograft", "technology": "CosMx"}, "spatial"),
    ({"experimental_system": "bulk_tissue", "technology": "bulk_RNA-seq"}, "bulk"),
    ({"experimental_system": "in_vitro_2d", "technology": "mass-spectrometry"}, "bulk"),
    ({"facet_scope": "human_postmortem"}, "post_mortem"),
    ({"facet_scope": "in_vitro"}, "in_vitro"),
    ({}, "unclassified"),
])
def test_view_category(db, expected):
    assert browser._view_category(db) == expected
    assert expected in browser.VIEW_CATEGORIES


def test_gene_expression_panel_groups_by_cell_type_with_category_tags(client):
    text = client.get("/genes/PTPRG").text
    for needle in ("var CATS", "vw-cat", "Datasets without per-cell cell types", "Grouping and dataset categories", "vw-flat"):
        assert needle in text, needle
    for gone in ("are on the Atlas tab and are not summarised here", "vw-scope-row\"><td colspan=\"4\">' + V.esc(V.SCOPES"):
        assert gone not in text, gone


def test_gene_expression_panel_lists_in_vitro_and_bulk_in_the_cell_type_row_without_pooling(client):
    text = client.get("/genes/PTPRG").text
    for needle in ("pooled: !!CATS[category].typed", "not pooled", "Bulk samples have no per-cell detection rate.",
                   "category !== 'spatial' && category !== 'unclassified' && V.GROUPS.indexOf(db.group) >= 0"):
        assert needle in text, needle


def test_genes_landing_lists_examples_and_the_favourites_hook(client):
    anonymous = client.get("/genes").text
    for gene in browser.LANDING_EXAMPLE_GENES:
        assert f'href="/genes/{gene}"' in anonymous
    assert "Examples" in anonymous and "Your favourites" in anonymous
    assert "favouriteGenes()" not in anonymous.split('id="gv-example-block"', 1)[1].split("</main>")[0]
    signed_in = client.get("/genes", headers={"Remote-User": "mf"}).text
    assert "antSettings.favouriteGenes()" in signed_in.split('id="gv-example-block"', 1)[1]


def test_cell_type_names_are_never_hyphenated(client):
    views = (_WEB_DIR / "templates" / "_views.html").read_text(encoding="utf-8")
    assert "oligodendro-cyte" not in views and "&shy;" not in views and "hyphens: auto" not in views
    text = client.get("/genes/PTPRG").text
    assert "'oligodendrocyte'" in text and "oligodendro-" not in text


def test_landing_keeps_the_multi_gene_comparison_separate_and_notes_it_on_the_page(client):
    landing = client.get("/genes").text
    examples = landing.split('id="gv-example-block"', 1)[1].split("</div>", 1)[0]
    assert "," not in examples
    assert "Compare several genes" in landing and 'href="/genes/APOE,TREM2,CX3CR1"' in landing
    page = client.get("/genes/APOE,TREM2").text
    assert 'id="gv-multi-note"' in page and 'href="/genes/TREM2"' in page and "Facet results are per gene" in page


# ── approved views, Expression tab, gene set search ────────────────────────────

def test_view_registry_and_initial_approval_list():
    assert browser.APPROVED_VIEWS <= set(browser.VIEW_REGISTRY)
    assert browser.APPROVED_VIEWS == {"datasets", "genes", "genesets", "gene-expression", "geneset-expression"}
    assert browser.view_approved("datasets") and not browser.view_approved("facets")
    assert browser.view_class("facets") == "vw-preview" and browser.view_class("genes") == ""
    assert browser.VIEW_PREVIEW_TIP in browser.view_tip("facets", "Facets")
    assert browser.view_tip("genes", "Genes") == "Genes"
    with pytest.raises(KeyError):
        browser.view_approved("no-such-view")


def test_nav_greys_only_unapproved_views(client):
    nav = client.get("/").text.split('<nav aria-label="Main">')[1].split("</nav>")[0]
    facets = nav.split('href="/facets"')[1].split("</a>")[0]
    assert 'class="vw-preview"' in facets and "preview" in facets and browser.VIEW_PREVIEW_TIP in facets
    for href in ('href="/"', 'href="/genesets"', 'href="/genes"'):
        entry = nav.split(href)[1].split("</a>")[0]
        assert "vw-preview" not in entry and "preview" not in entry


def test_approving_a_view_removes_its_marker(client, monkeypatch):
    monkeypatch.setattr(browser, "APPROVED_VIEWS", browser.APPROVED_VIEWS | {"facets"})
    text = client.get("/facets").text
    assert 'class="review-banner"' not in text
    assert 'class="vw-preview"' not in text.split('<nav aria-label="Main">')[1].split("</nav>")[0]


@pytest.mark.parametrize("path", ["/facets", "/facets/ant_disease", "/donor-groups", "/genesets/variability"])
def test_unapproved_pages_show_banner_but_stay_reachable(client, path):
    response = client.get(path)
    assert response.status_code == 200 or path == "/donor-groups"
    if response.status_code == 200:
        assert 'class="review-banner"' in response.text and browser.VIEW_PREVIEW_TIP in response.text


@pytest.mark.parametrize("path", ["/about", "/help", "/privacy", "/settings", "/api-guide", "/"])
def test_utility_and_approved_pages_have_no_banner(client, path):
    assert 'class="review-banner"' not in client.get(path).text


def _tab_button(text, vtab):
    return re.search(r'<button[^>]*data-vtab="%s"[^>]*>' % vtab, text).group(0)


def test_gene_page_expression_is_first_and_only_approved_tab(client):
    text = client.get("/genes/PTPRG").text
    assert text.index('data-vtab="expression"') < text.index('data-vtab="shifts"')
    assert "vw-preview" not in _tab_button(text, "expression")
    for vtab in ("shifts", "variability", "de", "detection", "atlas"):
        assert "vw-preview" in _tab_button(text, vtab), vtab
    assert "initTabs(document.getElementById('vw-tabs'), 'expression')" in text
    panel = lambda name: text.split('id="vt-%s"' % name)[1].split('class="tab-panel', 1)[0]
    assert "review-banner" not in panel("expression").split('id="vx-help"')[0]
    for name in ("shifts", "detection"):
        assert "review-banner" in panel(name)
    for marker in ('id="vx-root" data-kind="gene"', "Expression rank by cell type", "Expression rank in other datasets", 'id="vx-help" hidden', 'id="vx-help-other" hidden', "window.antExpression"):
        assert marker in text, marker


def test_geneset_page_expression_is_first_and_default(client):
    text = client.get("/genesets/Study1/Exp1/DAM").text
    assert text.index('data-vtab="expression"') < text.index('data-vtab="score"')
    assert "vw-preview" not in _tab_button(text, "expression")
    for vtab in ("score", "shifts", "variability", "genes", "page"):
        assert "vw-preview" in _tab_button(text, vtab), vtab
    assert "initTabs(document.getElementById('vw-tabs'), 'expression')" in text
    assert 'id="vx-root" data-kind="geneset"' in text and "/rank?db_id=" in text
    assert "review-banner" in text.split('id="vt-score"')[1].split('id="vt-shifts"')[0]


def test_gene_and_geneset_expression_use_one_renderer(client):
    gene = client.get("/genes/PTPRG").text
    geneset = client.get("/genesets/Study1/Exp1/DAM").text
    for text in (gene, geneset):
        assert text.count("window.antExpression = window.antExpression") == 1
        assert "Rank among" in text and 'id="vx-other"' in text
        assert "top x%" not in text.split('id="vt-expression"')[1].split('class="tab-panel', 1)[0] and "median of" not in text.split("window.antExpression = ")[1].split("</script>")[0]
    assert "mean (log-normalised) expression" in gene and "scanpy score" in geneset


def test_geneset_landing_has_search_over_every_set(client, monkeypatch):
    sets = [{"study": "S1", "experiment": "E1", "name": "Brown_Lysosome", "display_name": "Brown (Lysosome)", "n_genes": 34,
             "celltypes": ["microglial cell"], "organism": "human", "scored_in": ["A/n1", None]},
            {"study": "S1", "experiment": "E1", "display_name": "no name"}]
    base = browser._api_get

    def fake(api_url, path, **kwargs):
        if path.split("?")[0] == "/genesets":
            return {"genesets": sets}
        return _fake(api_url, path, **kwargs)
    monkeypatch.setattr(browser, "_api_get", fake)
    text = client.get("/genesets").text
    assert 'id="gs-search-q"' in text and 'aria-live="polite"' in text and "gene sets match" in text
    data = json.loads(re.search(r'<script id="gs-search-data" type="application/json">(.*?)</script>', text, re.S).group(1))
    assert data == [{"study": "S1", "experiment": "E1", "name": "Brown_Lysosome", "display_name": "Brown (Lysosome)", "n_genes": 34,
                     "celltypes": ["microglial cell"], "organism": "human", "n_scored": 1}]
    assert base is not fake


def test_geneset_landing_without_the_list_has_no_search(client):
    assert 'id="gs-search-q"' not in client.get("/genesets").text


def test_help_describes_preview_marker_and_expression_rank():
    import help_content
    gene_view = help_content.get_topic("gene-view").body_html
    assert "preview" in gene_view and "Expression rank" in gene_view and "76%" in gene_view
    assert "Expression rank" in help_content.get_topic("geneset-view").body_html


def test_facet_de_tab_rename_alias_and_two_sections(client):
    for path in ("/genes/PTPRG", "/genesets/Study1/Exp1/DAM"):
        text = client.get(path).text
        button = _tab_button(text, "shifts")
        assert ">Facet DE<" in text and 'data-hash="facet-de"' in button and 'data-alias="shifts"' in button
        assert "What shifts it" not in text
        for header in ("Facet DE by cell type", "Facet DE in other datasets"):
            assert header in text
        assert text.count('class="section-head">Facet DE') == 2
    assert 'id="vwg-p2o-help" hidden' in client.get("/genes/PTPRG").text
    assert 'id="vws-p2o-help" hidden' in client.get("/genesets/Study1/Exp1/DAM").text


def test_facet_matrix_uses_the_fixed_site_wide_column_order(client):
    text = client.get("/genes/PTPRG").text
    assert "groups: Object.keys(seen).length ? V.GROUPS : []" in text
    assert "GROUPS.map(function (g) { return '<th>'" in text
    gene_set = client.get("/genesets/Study1/Exp1/DAM").text
    assert "groups: Object.keys(seen).length ? V.GROUPS : []" in gene_set
    assert "var GROUPS = ['microglia', 'astrocyte'" in text      # microglia first, one list for every grid


def test_opened_cell_is_a_plain_table_without_plots(client):
    for path in ("/genes/PTPRG", "/genesets/Study1/Exp1/DAM"):
        text = client.get(path).text
        detail_source = text.split("function detail")[1].split("/* ----------")[0]
        assert "V.unitsTable(" in detail_source and "forestHtml(" not in detail_source and "linksHtml(" not in detail_source
    assert "Adj. p" in client.get("/genes/PTPRG").text and "label: 'LFC'" in client.get("/genes/PTPRG").text
    assert "label: 'Effect (SD)'" in client.get("/genesets/Study1/Exp1/DAM").text


def test_view_db_map_carries_the_facet_exclusion_reason(monkeypatch):
    databases = [dict(_DATABASES[0], id="X/sp", facet_exclude_reason="spatial technology (10x_visium): no replicate-level cell-type pseudobulk")]
    monkeypatch.setattr(browser, "_api_get", lambda api_url, path, **kwargs: {"databases": databases})
    assert browser._view_db_map("http://x")["X/sp"]["facet_exclude_reason"].startswith("spatial technology")
