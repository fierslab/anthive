"""Tests for web/browser.py — the FastAPI router that renders the web UI.

Two layers:

1. Pure-helper unit tests — the abbreviation/summary/formatting functions that
   carry most of the display logic, exercised directly with no app or network.
2. Route integration tests — the seven public routes driven through a FastAPI
   ``TestClient`` with the ant-serve REST API replaced by an in-process fake
   (``_api_get`` monkeypatched). These render the real Jinja templates, so a
   broken route↔template contract surfaces here.

The web UI is a *client* of the REST API and never touches DuckDB/h5ad, so the
fake API is the only boundary that needs stubbing.

``browser.py`` is loaded the same way ``web/ant-browse`` loads it — via an
``importlib`` spec from the file path — because ``web/`` is not on ``sys.path``
as an importable package and ``import browser`` would risk colliding with other
modules.
"""

import importlib.util
from pathlib import Path

import pytest
import requests

# ── load web/browser.py by path (mirrors how web/ant-browse loads it) ──────────

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"
_spec = importlib.util.spec_from_file_location(
    "anthive_web_browser", _WEB_DIR / "browser.py"
)
browser = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(browser)


# ── unit tests: _as_list ───────────────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    (None, []),
    ("", []),
    (0, []),
    ([], []),
    ("human", ["human"]),
    (["human", "mouse"], ["human", "mouse"]),
    (42, [42]),
])
def test_as_list(value, expected):
    assert browser._as_list(value) == expected


# ── unit tests: _accession_url ─────────────────────────────────────────────────

@pytest.mark.parametrize("accession, fragment", [
    ("GSE12345", "ncbi.nlm.nih.gov/geo"),
    ("GSM999", "ncbi.nlm.nih.gov/geo"),
    ("GPL570", "ncbi.nlm.nih.gov/geo"),
    ("PRJNA123", "ncbi.nlm.nih.gov/bioproject"),
    ("PRJEB456", "ncbi.nlm.nih.gov/bioproject"),
    ("E-MTAB-1234", "ebi.ac.uk/biostudies/arrayexpress"),
    ("E-GEOD-9", "ebi.ac.uk/biostudies/arrayexpress"),
])
def test_accession_url_known_prefixes(accession, fragment):
    url = browser._accession_url(accession)
    assert url is not None
    assert fragment in url
    assert accession in url


def test_accession_url_case_insensitive():
    # prefix detection upper-cases, but the original casing is preserved in the URL
    assert browser._accession_url("gse42") == browser._accession_url("GSE42").replace(
        "GSE42", "gse42"
    )
    assert browser._accession_url("gse42") is not None


def test_accession_url_unknown_returns_none():
    assert browser._accession_url("FOO123") is None
    assert browser._accession_url("12345") is None


# ── unit tests: format_number ──────────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    (0, "0"),
    (0.0, "0"),
    (1_500_000, "1.50e+06"),     # >= 1e6 → scientific
    (0.0005, "5.00e-04"),        # < 1e-3 → scientific
    (5, "5.0"),                  # integer-valued
    (1500, "1,500.0"),           # integer-valued with thousands sep
    (123.456, "123.5"),          # >= 100 → 1 decimal
    (12.5, "12.50"),             # >= 10 → 2 decimals
    (3.14159, "3.142"),          # >= 1 → 3 decimals
    (0.0012, "0.0012"),          # < 1 (and >= 1e-3) → 4 decimals
])
def test_format_number(value, expected):
    assert browser.format_number(value) == expected


def test_format_number_integer_branch_precedes_magnitude():
    # 200.0 is integer-valued AND >= 100; the integer branch must win → ".0"
    assert browser.format_number(200.0) == "200.0"


# ── unit tests: _short_count ───────────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    (None, "—"),
    (0, "—"),
    (999, "999"),
    (1_000, "1.0k"),
    (1_400, "1.4k"),
    (144_000, "144k"),
    (500_000, "500k"),
    (1_000_000, "1.0M"),
    (2_300_000, "2.3M"),
])
def test_short_count(value, expected):
    assert browser._short_count(value) == expected


# ── unit tests: _summarize_meta_field ──────────────────────────────────────────

def test_summarize_all_null_is_empty():
    out = browser._summarize_meta_field([{"organism": None}, {}], "organism")
    assert out == {"abbrev": "", "full": ""}


def test_summarize_single_value_with_abbrev_map():
    out = browser._summarize_meta_field(
        [{"organism": "human"}], "organism", abbrev_map=browser._ORGANISM_ABBREV
    )
    assert out == {"abbrev": "Hs", "full": "human"}


def test_summarize_celltype_abbrev_map():
    out = browser._summarize_meta_field(
        [{"celltype": "microglial cell"}], "celltype",
        abbrev_map=browser._CELLTYPE_ABBREV,
    )
    assert out == {"abbrev": "MG", "full": "microglial cell"}


def test_summarize_abbrev_fallback_first_six_chars():
    # value absent from the map → first 6 characters
    out = browser._summarize_meta_field(
        [{"organism": "platypus"}], "organism", abbrev_map=browser._ORGANISM_ABBREV
    )
    assert out == {"abbrev": "platyp", "full": "platypus"}


def test_summarize_max_len_truncation():
    out = browser._summarize_meta_field(
        [{"tech_cat": "single-cell transcriptomics"}], "tech_cat", max_len=10
    )
    assert out["full"] == "single-cell transcriptomics"
    assert out["abbrev"] == "single-cel…"


def test_summarize_multiple_values_is_mix():
    out = browser._summarize_meta_field(
        [{"organism": "human"}, {"organism": "mouse"}], "organism",
        abbrev_map=browser._ORGANISM_ABBREV,
    )
    assert out["abbrev"] == "mix"
    # full lists every unique value, sorted
    assert out["full"] == "human, mouse"


def test_summarize_flattens_list_valued_fields():
    out = browser._summarize_meta_field(
        [{"tissue": ["cortex", "hippocampus"]}], "tissue"
    )
    assert out["abbrev"] == "mix"
    assert out["full"] == "cortex, hippocampus"


def test_summarize_dedups_across_datasets():
    out = browser._summarize_meta_field(
        [{"organism": "human"}, {"organism": "human"}], "organism",
        abbrev_map=browser._ORGANISM_ABBREV,
    )
    assert out == {"abbrev": "Hs", "full": "human"}


# ── unit tests: _modified_ts ───────────────────────────────────────────────────

def test_modified_ts_prefers_updated():
    db = {"updated": "2026-05-01", "created": "2026-01-01", "date": "2025-01-01",
          "year": 2020}
    assert browser._modified_ts(db) == "2026-05-01"


def test_modified_ts_falls_through_to_created_then_date():
    assert browser._modified_ts({"created": "2026-02-02", "year": 2020}) == "2026-02-02"
    assert browser._modified_ts({"date": "2025-03-03"}) == "2025-03-03"


def test_modified_ts_year_cast_to_iso():
    assert browser._modified_ts({"year": 2024}) == "2024-01-01"


def test_modified_ts_empty_when_nothing():
    assert browser._modified_ts({}) == ""


# ── fake REST API ──────────────────────────────────────────────────────────────

# Two collections. GroupA mixes organism/celltype/disease across its two
# datasets (→ 'mix' summaries); GroupB omits group_id to exercise the
# id.split('/')[0] fallback.
_DATABASES = [
    {
        "id": "GroupA/ds1", "group_id": "GroupA", "file_id": "ds1",
        "title": "Dataset One", "n_cells": 1500, "n_genes": 20000,
        "collection": {"group": "Group A Study", "experiment": "# Heading\nBody text."},
        "organism": "human", "technology": "10x_chromium_3p_v3",
        "technology_category": "single-cell", "celltype": "microglial cell",
        "tissue": "prefrontal cortex", "disease": "alzheimer disease",
        "access": "public", "source": "internal", "suspension_type": "nucleus",
        "layers": {"X": {}, "counts": {}}, "embeddings": {"X_umap": {}},
        "year": 2025, "updated": "2026-05-01",
        "doi": "10.1/abc", "link": ["https://example.org/ds1"],
    },
    {
        "id": "GroupA/ds2", "group_id": "GroupA", "file_id": "ds2",
        "title": "Dataset Two", "n_cells": 500_000, "n_genes": 19000,
        "collection": {"group": "Group A Study"},
        "organism": "mouse", "technology": "10x_chromium_3p_v3",
        "technology_category": "single-cell", "celltype": "astrocyte",
        "tissue": "prefrontal cortex", "disease": "normal",
        "access": "public", "source": "internal", "suspension_type": "nucleus",
        "layers": {"X": {}}, "embeddings": {"X_umap": {}},
        "year": 2024, "updated": "2026-04-01",
    },
    {
        # no group_id → group derived from id prefix "GroupB"
        "id": "GroupB/ds3", "file_id": "ds3",
        "title": "Dataset Three", "n_cells": 800, "n_genes": 15000,
        "collection": {}, "organism": "human", "technology": "smart-seq2",
        "technology_category": "single-cell", "celltype": "neuron",
        "tissue": "hippocampus", "disease": "normal",
        "access": "internal", "source": "external", "suspension_type": "cell",
        "layers": {"X": {}}, "embeddings": {"X_pca": {}},
        "year": 2023, "updated": "2026-03-01",
    },
]

# /databases/{id}/info payloads, keyed by db id.
_INFO = {
    "GroupA/ds1": {
        "file_id": "ds1", "group_id": "GroupA", "title": "Dataset One",
        "n_cells": 1500, "n_genes": 20000,
        "collection": {"group": "Group A Study"},
        "organism": "human", "technology": "10x_chromium_3p_v3",
        "cell_type": "microglial cell", "tissue": "prefrontal cortex",
        "disease": "alzheimer disease", "condition": "AD", "sex": "female",
        "age": "82", "strain": None, "access": "public", "source": "internal",
        "license": "CC-BY", "year": 2025, "contributor": "Lab X",
        "accession": "GSE12345", "doi": "10.1/abc", "pmid": "123456",
        "link": [], "suspension_type": "nucleus",
        "obs": {"categorical": ["cell_type", "sample"], "numerical": ["n_genes", "pseudotime"]},
        "layers": {"X": {}, "counts": {}}, "embeddings": {"X_umap": {}, "X_pca": {}},
        "default_layer": "X",
        "top_genes": ["AIF1", "P2RY12"], "top_cat": ["cell_type"], "top_num": ["pseudotime"],
        "layer_stats": {
            "X": {"dtype": "float32", "min_value": 0.0, "min_nonzero": 0.01,
                  "max_value": 9.2, "median": 0.0, "q90": 3.1, "pct_zeros": 88.5},
        },
        "markdown": "## Notes\nSome **markdown**.",
    },
    "GroupA/sub/ds4": {
        # exercises the {dataset_browse_id:path} multi-segment converter
        "file_id": "sub/ds4", "group_id": "GroupA", "title": "Nested Dataset",
        "n_cells": 300, "n_genes": 12000, "collection": {"group": "Group A Study"},
        "organism": "human", "obs": {"categorical": [], "numerical": []},
        "layers": {"X": {}}, "embeddings": {"X_umap": {}}, "default_layer": "X",
        "top_genes": [], "top_cat": [], "top_num": [], "layer_stats": {},
        "markdown": None,
    },
}

_GENESETS = [
    {"study": "StudyX", "experiment": "Exp1", "name": "gs_up",
     "display_name": "Up genes", "format": "grp", "n_genes": 50,
     "organism": "human", "method": "DESeq2", "direction": "up",
     "parent_dataset": "GroupA/ds1", "description": "Upregulated"},
    {"study": "StudyX", "experiment": "Exp1", "name": "gs_dn",
     "display_name": "Down genes", "format": "grp", "n_genes": 30,
     "organism": "human", "method": "DESeq2", "direction": "down",
     "parent_dataset": "GroupA/ds1", "description": "Downregulated"},
    {"study": "StudyY", "experiment": "Exp2", "name": "gs_y",
     "display_name": "Y set", "format": "rnk", "n_genes": 100,
     "organism": "mouse", "method": "edgeR", "direction": None,
     "parent_dataset": None, "description": ""},
]

_EXPERIMENT_PAYLOAD = {
    "metadata": {"name": "Experiment One", "organism": "human", "method": "DESeq2",
                 "parent_dataset": "GroupA/ds1", "parameters": {"fdr": "0.05"}},
    "body": "## Experiment\nDescription.",
    "genesets": [g for g in _GENESETS if g["experiment"] == "Exp1"],
}

_GENESET_PAYLOAD = {
    "study": "StudyX", "experiment": "Exp1", "name": "gs_up",
    "display_name": "Up genes", "description": "Upregulated", "format": "grp",
    "n_genes": 3, "organism": "human", "method": "DESeq2", "direction": "up",
    "parent_dataset": "GroupA/ds1",
    "genes": ["AIF1", "P2RY12", "CX3CR1"],
    "geneset_body": "gene list notes", "experiment_body": "exp notes",
    "geneset_metadata": {"top_go": ["microglial activation"]},
    "experiment_metadata": {"method": "DESeq2"},
}


# DE study list returned by the fake /databases/{id}/de endpoint.
_DE_STUDIES = {
    "studies": [
        {"study_id": "glmm_demo", "name": "GLMM demo", "method": "glmm",
         "target": "gene", "reference_type": "vsgrandmean",
         "n_rows": 1000, "n_terms": 2, "n_contrasts": 5},
    ]
}

# Per-study detail (terms) returned by /databases/{id}/de/{study_id}.
_DE_STUDY_DETAIL = {
    "study_id": "glmm_demo", "name": "GLMM demo", "method": "glmm",
    "target": "gene", "reference_type": "vsgrandmean",
    "n_rows": 1000, "n_terms": 2, "n_contrasts": 5,
    "terms": [
        {"term": "batch", "n_contrasts": 3, "n_rows": 600},
        {"term": "strain", "n_contrasts": 2, "n_rows": 400},
    ],
}


def _fake_api_get(api_url, path):
    """In-process stand-in for browser._api_get. Dispatches on path; raises
    requests.HTTPError for anything unknown (mirrors a 404 from ant-serve,
    which the routes catch)."""
    if path == "/databases":
        return {"databases": _DATABASES}
    if path.startswith("/databases/") and path.endswith("/de"):
        return _DE_STUDIES
    if "/de/" in path:           # /databases/{id}/de/{study_id}
        return _DE_STUDY_DETAIL
    if path.startswith("/databases/") and path.endswith("/info"):
        db_id = path[len("/databases/"):-len("/info")]
        if db_id in _INFO:
            return _INFO[db_id]
        raise requests.HTTPError(f"404 for {db_id}")
    if path == "/genesets":
        return {"genesets": _GENESETS}
    if path == "/genesets/StudyX/Exp1":
        return _EXPERIMENT_PAYLOAD
    if path == "/genesets/StudyX/Exp1/gs_up":
        return _GENESET_PAYLOAD
    raise requests.HTTPError(f"404 for {path}")


# ── data-builder tests (fake API, no app) ──────────────────────────────────────

@pytest.fixture
def fake_api(monkeypatch):
    monkeypatch.setattr(browser, "_api_get", _fake_api_get)


def test_get_collection_list_groups_and_counts(fake_api):
    collections = browser.get_collection_list("http://x")
    by_id = {c["group_id"]: c for c in collections}

    assert set(by_id) == {"GroupA", "GroupB"}
    assert by_id["GroupA"]["dataset_count"] == 2
    assert by_id["GroupA"]["total_cells"] == 501_500
    # friendly name comes from collection.group
    assert by_id["GroupA"]["name"] == "Group A Study"
    # GroupB has no group_id field → derived from id prefix
    assert by_id["GroupB"]["dataset_count"] == 1


def test_get_collection_list_mix_summaries(fake_api):
    by_id = {c["group_id"]: c for c in browser.get_collection_list("http://x")}
    ga = by_id["GroupA"]
    # human + mouse → mix
    assert ga["organism"]["abbrev"] == "mix"
    assert ga["organism"]["full"] == "human, mouse"
    # microglial cell + astrocyte → mix
    assert ga["celltype"]["abbrev"] == "mix"
    # shared single value → abbreviated
    assert ga["tissue"]["abbrev"] == "PFC"


def test_get_collection_list_sorted_newest_first(fake_api):
    collections = browser.get_collection_list("http://x")
    # GroupA last_modified 2026-05-01 > GroupB 2026-03-01
    assert collections[0]["group_id"] == "GroupA"
    assert collections[0]["last_modified"] == "2026-05-01"


def test_get_filter_options_splits_and_dedups(fake_api):
    collections = browser.get_collection_list("http://x")
    options = browser.get_filter_options(collections)
    # organism mix in GroupA contributes both; GroupB contributes human again
    assert options["organism"] == ["human", "mouse"]
    assert options["disease"] == ["alzheimer disease", "normal"]
    assert "single-cell" in options["technology_category"]


def test_get_collection_datasets_filters_and_builds_description(fake_api):
    datasets, description_html = browser.get_collection_datasets("http://x", "GroupA")
    assert len(datasets) == 2
    names = {d["name"] for d in datasets}
    assert names == {"Dataset One", "Dataset Two"}
    # description rendered from the first dataset's collection.experiment markdown
    assert description_html is not None
    assert "<h1" in description_html  # "# Heading" → <h1>


def test_get_collection_datasets_unknown_group_is_empty(fake_api):
    datasets, description_html = browser.get_collection_datasets("http://x", "Nope")
    assert datasets == []
    assert description_html is None


# ── route integration tests (TestClient + fake API + real templates) ───────────

@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(browser, "_api_get", _fake_api_get)

    app = FastAPI()
    app.state.api_url = "http://testapi"
    app.state.reports = []
    app.state.genesets_available = True
    app.include_router(browser.router)
    return TestClient(app)


def test_index_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    # collection appears in the rendered catalog
    assert "Group A Study" in resp.text


def test_index_survives_api_failure(client, monkeypatch):
    def boom(api_url, path):
        raise requests.HTTPError("API down")
    monkeypatch.setattr(browser, "_api_get", boom)
    resp = client.get("/")
    # route catches the error and renders an empty catalog rather than 500
    assert resp.status_code == 200


def test_folder_view_renders(client):
    resp = client.get("/browse/GroupA")
    assert resp.status_code == 200
    assert "Dataset One" in resp.text
    assert "Dataset Two" in resp.text


def test_folder_view_unknown_group_404(client):
    resp = client.get("/browse/DoesNotExist")
    assert resp.status_code == 404


def test_dataset_view_renders(client):
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    assert "Dataset One" in resp.text
    # markdown body rendered
    assert "markdown" in resp.text.lower()


def test_dataset_view_nested_path_id(client):
    # {dataset_browse_id:path} must accept multi-segment ids
    resp = client.get("/browse/GroupA/sub/ds4")
    assert resp.status_code == 200
    assert "Nested Dataset" in resp.text


def test_dataset_view_api_failure_404(client):
    resp = client.get("/browse/GroupA/missing")
    assert resp.status_code == 404


def test_dataset_view_de_panel_and_badge(client):
    # Dataset with DE data: the DE panel, the stat-row badge, and the DE
    # category in the visualize dropdown all render.
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    assert "Differential expression" in resp.text   # panel heading
    assert "GLMM demo" in resp.text                  # study name
    assert "DE:" in resp.text                         # stat-row badge
    assert "differential expression" in resp.text     # dropdown category label
    # browse-DE deep link to the (stage-3) de page
    assert "/de/glmm_demo" in resp.text


def test_dataset_view_no_de_surfaces_hidden(client, monkeypatch):
    # No DE data → none of the DE surfaces appear.
    def no_de(api_url, path):
        if path.endswith("/de"):
            raise requests.HTTPError("no DE")
        return _fake_api_get(api_url, path)
    monkeypatch.setattr(browser, "_api_get", no_de)
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    assert "Differential expression" not in resp.text
    assert "GLMM demo" not in resp.text


def test_dataset_view_visualize_dropdown(client):
    # The viz buttons are collapsed into a single top-right dropdown button.
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    assert 'id="viz-menu-btn"' in resp.text
    assert 'id="viz-menu-panel"' in resp.text


def test_plot_view_renders_known_geom(client):
    resp = client.get("/browse/GroupA/ds1/plot/scatter")
    assert resp.status_code == 200
    # geom label from _PLOT_GEOMS_UI appears
    assert "Scatter" in resp.text


def test_plot_view_unknown_geom_404(client):
    resp = client.get("/browse/GroupA/ds1/plot/bogus")
    assert resp.status_code == 404


def test_plot_route_wins_over_dataset_path_converter(client):
    """The plot route is registered before dataset_view's {id:path} route, so
    '/browse/g/f/plot/scatter' must hit plot_view (which requests
    /databases/GroupA/ds1/info) — not dataset_view treating 'ds1/plot/scatter'
    as the dataset id (which would request /databases/GroupA/ds1/plot/scatter/info
    and 404)."""
    requested = []

    def recording(api_url, path):
        requested.append(path)
        return _fake_api_get(api_url, path)

    # rebind on the client's monkeypatched module
    browser._api_get = recording
    try:
        resp = client.get("/browse/GroupA/ds1/plot/scatter")
    finally:
        browser._api_get = _fake_api_get
    assert resp.status_code == 200
    assert "/databases/GroupA/ds1/info" in requested
    assert "/databases/GroupA/ds1/plot/scatter/info" not in requested


def test_plot_view_api_failure_404(client):
    resp = client.get("/browse/GroupA/missing/plot/scatter")
    assert resp.status_code == 404


# ── DE plot geoms (REST API 2.3) ───────────────────────────────────────────────

def test_geom_switcher_lists_de_geoms_when_de_present(client):
    # When the dataset has DE data, the plot-type switcher offers the DE geoms.
    resp = client.get("/browse/GroupA/ds1/plot/scatter")
    assert resp.status_code == 200
    assert "Volcano (DE)" in resp.text
    assert "Forest (gene × contrasts)" in resp.text
    assert "MA (DE)" in resp.text


def test_geom_switcher_hides_de_geoms_without_de(client, monkeypatch):
    # No DE data → the switcher must NOT offer volcano/ma/forest on a non-DE
    # geom page. This is the "only show DE links for datasets that have DE" rule.
    def no_de(api_url, path):
        if path.endswith("/de"):
            raise requests.HTTPError("no DE")
        return _fake_api_get(api_url, path)
    monkeypatch.setattr(browser, "_api_get", no_de)
    resp = client.get("/browse/GroupA/ds1/plot/scatter")
    assert resp.status_code == 200
    assert "Volcano (DE)" not in resp.text
    assert "MA (DE)" not in resp.text
    assert "Forest (gene × contrasts)" not in resp.text


def test_geom_switcher_keeps_current_de_geom_without_de(client, monkeypatch):
    # A direct link to a DE geom on a no-DE dataset still lists that geom (so
    # the switcher reflects the current page) but not the other DE geoms.
    def no_de(api_url, path):
        if path.endswith("/de"):
            raise requests.HTTPError("no DE")
        return _fake_api_get(api_url, path)
    monkeypatch.setattr(browser, "_api_get", no_de)
    resp = client.get("/browse/GroupA/ds1/plot/volcano")
    assert resp.status_code == 200
    assert "Volcano (DE)" in resp.text     # current geom, kept
    assert "Forest (gene × contrasts)" not in resp.text


def test_plot_view_scatter_xy_mode_affordance(client):
    # scatter/hexbin expose the embedding ⇄ X-vs-Y mode switch with the XY
    # inputs and per-axis transform dropdowns (REST API 2.4 replaced the
    # log_x/log_y checkboxes with transform_x/_y).
    resp = client.get("/browse/GroupA/ds1/plot/scatter")
    assert resp.status_code == 200
    assert 'name="_xymode"' in resp.text
    assert 'id="f-x-xy"' in resp.text
    assert 'id="f-y-xy"' in resp.text
    assert 'name="transform_x"' in resp.text and 'name="transform_y"' in resp.text
    assert 'signed_log1p' in resp.text          # the 7-kind transform list
    # shared overlays (marginals / regression) for scatter/hexbin/kde2d
    assert 'name="marginals"' in resp.text and 'name="regline"' in resp.text


def test_plot_view_hexbin_density_autoclip(client):
    resp = client.get("/browse/GroupA/ds1/plot/hexbin")
    assert resp.status_code == 200
    assert 'name="vmin_quantile"' in resp.text and 'name="vmax_quantile"' in resp.text
    assert 'auto-clip density' in resp.text


def test_plot_view_kde2d_form(client):
    # kde2d is registered (REST API 2.4) and renders its 2-axis density form.
    resp = client.get("/browse/GroupA/ds1/plot/kde2d")
    assert resp.status_code == 200
    assert "KDE density (2D)" in resp.text
    assert 'name="x"' in resp.text and 'name="y"' in resp.text
    assert 'name="transform_x"' in resp.text
    assert 'name="n_levels"' in resp.text
    assert 'name="iso_overlay"' in resp.text and 'name="point_overlay"' in resp.text


def test_kde2d_in_geom_switcher(client):
    # listed in the plot-type switcher (non-DE geom → always offered)
    resp = client.get("/browse/GroupA/ds1/plot/scatter")
    assert "KDE density (2D)" in resp.text


def test_dataset_dropdown_has_kde2d(client):
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    assert "/plot/kde2d" in resp.text


@pytest.mark.parametrize("geom", ["volcano", "ma"])
def test_plot_view_de_cascade_form(client, geom):
    # volcano/ma render the study→term→contrast cascade, with the study
    # select seeded from /databases/{id}/de (fake returns 'GLMM demo').
    resp = client.get(f"/browse/GroupA/ds1/plot/{geom}")
    assert resp.status_code == 200
    assert 'name="study"' in resp.text
    assert 'name="term"' in resp.text
    assert 'name="contrast"' in resp.text
    assert "GLMM demo" in resp.text   # study option seeded from /de


def test_plot_view_volcano_threshold_inputs(client):
    resp = client.get("/browse/GroupA/ds1/plot/volcano")
    assert resp.status_code == 200
    for field in ("padj_threshold", "log2fc_threshold", "n_label"):
        assert f'name="{field}"' in resp.text


def test_plot_view_forest_gene_input(client):
    resp = client.get("/browse/GroupA/ds1/plot/forest")
    assert resp.status_code == 200
    assert 'name="gene"' in resp.text
    # optional narrowing selectors present too
    assert 'name="study"' in resp.text


def test_plot_view_de_geom_without_de_data(client, monkeypatch):
    # When /de fails (no DE ingested), the form still renders and shows the
    # "no DE data" hint rather than 500ing.
    def no_de(api_url, path):
        if path.endswith("/de"):
            raise requests.HTTPError("no DE")
        return _fake_api_get(api_url, path)
    monkeypatch.setattr(browser, "_api_get", no_de)
    resp = client.get("/browse/GroupA/ds1/plot/volcano")
    assert resp.status_code == 200
    assert "No differential-expression data" in resp.text


def test_genesets_index_renders_grouped(client):
    resp = client.get("/genesets")
    assert resp.status_code == 200
    assert "StudyX" in resp.text
    assert "StudyY" in resp.text
    assert "Up genes" in resp.text


def test_genesets_index_survives_api_failure(client, monkeypatch):
    monkeypatch.setattr(browser, "_api_get",
                        lambda api_url, path: (_ for _ in ()).throw(requests.HTTPError()))
    resp = client.get("/genesets")
    assert resp.status_code == 200


def test_geneset_experiment_view_renders(client):
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    assert "Experiment One" in resp.text
    assert "Up genes" in resp.text


def test_geneset_experiment_view_404(client):
    resp = client.get("/genesets/StudyX/Nope")
    assert resp.status_code == 404


def test_geneset_view_renders(client):
    resp = client.get("/genesets/StudyX/Exp1/gs_up")
    assert resp.status_code == 200
    assert "Up genes" in resp.text
    # genes from the grp list rendered
    assert "P2RY12" in resp.text


def test_geneset_view_404(client):
    resp = client.get("/genesets/StudyX/Exp1/nope")
    assert resp.status_code == 404


# ── DE study browser page ──────────────────────────────────────────────────────

def test_de_study_view_renders(client):
    resp = client.get("/browse/GroupA/ds1/de/glmm_demo")
    assert resp.status_code == 200
    assert "GLMM demo" in resp.text          # study name
    assert 'id="de-rail"' in resp.text        # term tree
    assert 'data-term="batch"' in resp.text   # term server-rendered
    assert 'data-term="strain"' in resp.text


def test_de_study_view_route_beats_dataset_path(client):
    # The /de/{study} route must win over dataset_view's {dataset:path}
    # catch-all (which would treat 'ds1/de/glmm_demo' as a dataset id → 404).
    resp = client.get("/browse/GroupA/ds1/de/glmm_demo")
    assert resp.status_code == 200
    assert 'class="de-page"' in resp.text     # DE browser template, not index


def test_de_study_view_404_on_missing_study(client, monkeypatch):
    def no_study(api_url, path):
        if "/de/" in path:
            raise requests.HTTPError("no study")
        return _fake_api_get(api_url, path)
    monkeypatch.setattr(browser, "_api_get", no_study)
    resp = client.get("/browse/GroupA/ds1/de/nope")
    assert resp.status_code == 404


# ── responsive / mobile markup ─────────────────────────────────────────────────

def test_table_wrap_present_on_table_pages(client):
    # Every data table is wrapped in .table-wrap (overflow-x guard) so a wide
    # table scrolls inside its box rather than widening the page on mobile.
    for path in ("/", "/browse/GroupA", "/browse/GroupA/ds1", "/genesets",
                 "/genesets/StudyX/Exp1"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert 'class="table-wrap"' in resp.text, f"missing .table-wrap on {path}"


def test_table_wrap_style_defined(client):
    # The .table-wrap rule itself ships in base.html's <style>.
    resp = client.get("/")
    assert ".table-wrap" in resp.text
    assert "overflow-x: auto" in resp.text


def test_card_table_rule_defined(client):
    # The shared mobile card-grid layout lives in base.html, keyed on
    # table.card-table, and triggers at <=760px.
    resp = client.get("/")
    assert "table.card-table" in resp.text
    assert "@media (max-width: 760px)" in resp.text
    assert "grid-template-columns: 1fr 1fr" in resp.text


def test_folder_mobile_card_markup(client):
    # The folder table opts into the card layout and carries the per-cell
    # hooks it consumes: card-title on the dataset name, data-labels on the
    # metadata cells.
    resp = client.get("/browse/GroupA")
    assert resp.status_code == 200
    assert 'id="datasets-table" class="card-table"' in resp.text
    assert 'class="card-title"' in resp.text
    assert 'data-label="Org"' in resp.text
    assert 'data-label="Status"' in resp.text


def test_landing_access_source_inline(client):
    # Acc/Source are no longer their own columns; the icons sit inline after the
    # collection name (.acc-src) and their filters fold into the Collection
    # header popover (data-filter-fields="acc,src").
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'class="acc-src"' in resp.text
    assert 'data-filter-fields="acc,src"' in resp.text
    assert 'data-label="Access"' not in resp.text   # column removed
    assert 'data-label="Source"' not in resp.text


def test_folder_access_source_inline(client):
    resp = client.get("/browse/GroupA")
    assert resp.status_code == 200
    assert 'class="acc-src"' in resp.text
    assert 'data-label="Access"' not in resp.text
    assert 'data-label="Source"' not in resp.text


def test_folder_external_link_icons(client):
    # datasets with a doi/link get small link icons after the name (.ext-lnk)
    resp = client.get("/browse/GroupA")
    assert resp.status_code == 200
    assert 'class="ext-lnk"' in resp.text
    assert "https://doi.org/10.1/abc" in resp.text
    assert "https://example.org/ds1" in resp.text


def test_dataset_quick_links(client):
    # DOI / PubMed / accession surfaced as compact badges up top (.ds-links),
    # in addition to the verbose Provenance section.
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    assert 'class="ds-links"' in resp.text
    assert "https://doi.org/10.1/abc" in resp.text
    assert "pubmed.ncbi.nlm.nih.gov/123456" in resp.text


def test_plot_pdf_label_and_no_source_line(client):
    resp = client.get("/browse/GroupA/ds1/plot/scatter")
    assert resp.status_code == 200
    assert "PDF (publication)" not in resp.text   # label trimmed to "⬇ PDF"
    assert 'id="src-url"' not in resp.text          # source line removed
    assert "spinner-lg" in resp.text                # prominent in-canvas loader


def test_landing_mobile_card_markup(client):
    # The landing collection table is also a card-table: collection name is
    # the card title, the context pill cluster spans full width (card-span),
    # and the scalar columns are labelled for the 2-column card grid.
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="coll-table" class="card-table"' in resp.text
    assert 'class="card-title"' in resp.text
    assert 'class="card-span"' in resp.text
    assert 'data-label="Datasets"' in resp.text
    assert 'data-label="Cells"' in resp.text
