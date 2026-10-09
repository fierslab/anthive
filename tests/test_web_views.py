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
     "organism": "human", "suspension_type": "nucleus", "experimental_system": "post_mortem_tissue"},
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
    assert result["A/n1"] == {"title": "Nuclei microglia", "scope": "human_postmortem", "group": "microglia",
                              "human": True, "secondary": False, "modality": "nuclei"}
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
    for marker in ('data-vtab="gene"', 'data-vtab="atlas"', 'data-vtab="de"', 'id="vt-gene"', 'id="vt-atlas"', 'id="vwg-p1"', 'id="vwg-p2"'):
        assert marker in text, marker
    assert 'data-vtab="de" data-hash="author-de" role="tab" hidden' in text      # shown only when DE exists
    dbs = json.loads(re.search(r"var DBS = (\{.*?\});\n", text, re.S).group(1))
    assert dbs["A/n1"]["modality"] == "nuclei" and dbs["A/c1"]["group"] == "microglia"
    assert "Donor variability" in text and "Coming later" in text      # panel 3 placeholder
    assert "Detection rates are not comparable between nuclei and cells" in text
    assert "Braak stage and pathologic AD are one pathology axis" in text


def test_gene_list_and_empty_gene_page_have_no_views(client):
    assert 'data-vtab="gene"' not in client.get("/genes/PTPRG,APOE").text
    assert 'data-vtab="gene"' not in client.get("/genes").text


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
    for marker in ('data-vtab="geneset"', 'data-vtab="page"', 'id="vt-geneset"', 'id="vt-page"', 'id="vws-p1"', 'id="vws-p2"'):
        assert marker in text, marker
    payload = json.loads(re.search(r"var PAYLOAD = (\{.*?\});\n", text, re.S).group(1))
    assert payload["scored_in"] == ["A/n1", "A/c1"] and payload["column_name"] == "Study1::Exp1::DAM"
    assert "Scores of different sets are not on one scale" in text or "not ranked against each other" in text
    assert "A mean score is not well suited" in text
    assert "Rank among modules" not in text


def test_facet_page_has_overview_and_detail_tabs(client):
    text = client.get("/facets/ant_disease").text
    for marker in ('data-vtab="overview"', 'data-vtab="detail"', 'id="vt-overview"', 'id="vt-detail"', 'id="vwf-answer"', 'id="facet-effects"'):
        assert marker in text, marker
    assert "Details for experts" in text or "vwf-expert" in text
    assert "Detail (current)" in text


def test_facet_page_without_tests_keeps_the_old_page_only(client):
    text = client.get("/facets/ant_notatestedfacet").text
    assert 'data-vtab="overview"' not in text


# ── wording and scripts ────────────────────────────────────────────────────────

_VIEW_TEMPLATES = ("_views.html", "_view_gene.html", "_view_geneset.html", "_view_facet.html")


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
    for header in ("1. Where is it expressed", "2. What shifts it", "3. Donor variability"):
        assert header in text, header
    for panel in ("vwg-p1-help", "vwg-p2-help"):
        assert f'id="{panel}" hidden' in text
    for gone in ("panel 1, descriptive", "tested per donor or mouse", "Mean, log-normalised", "low det."):
        assert gone not in text, gone
    assert "without per-cell cell types" in text and "dataset-level label" in text
    assert "Detected in only" in text and "percentile of the gene's mean (log-normalised)" in text
