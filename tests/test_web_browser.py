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

# pyright: reportMissingImports=false

import importlib.util
from pathlib import Path

import pytest
import requests

# ── load web/browser.py by path (mirrors how web/ant-browse loads it) ──────────

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"
_spec = importlib.util.spec_from_file_location(
    "anthive_web_browser", _WEB_DIR / "browser.py"
)
assert _spec is not None and _spec.loader is not None, (
    f"could not load web/browser.py spec from {_WEB_DIR}"
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


# ── unit tests: _expand_plot_thumbnails ─────────────────────────────────────────

def test_plot_thumbnail_expands_with_default_db():
    html = '<p><img alt="umap" src="plot:scatter?x=X_umap&amp;color=TREM2" /></p>'
    out = browser._expand_plot_thumbnails(html, default_db="GroupA/ds1")
    # wrapped in a link to the interactive plot page…
    assert 'href="/browse/GroupA/ds1/plot/scatter?' in out
    # …with a thumbnail img pointing at the PNG endpoint (size + png injected)
    assert 'src="/api/databases/GroupA/ds1/plot/scatter?' in out
    assert "format=png" in out and "dpi=80" in out
    assert 'class="md-plot-thumb"' in out and 'alt="umap"' in out
    assert "color=TREM2" in out and "plot:" not in out


def test_plot_thumbnail_explicit_db_overrides_default():
    html = '<img src="plot:hexbin?color=SPP1&amp;db=GroupB/ds3" />'
    out = browser._expand_plot_thumbnails(html, default_db="GroupA/ds1")
    assert "/databases/GroupB/ds3/plot/hexbin?" in out
    assert "GroupA" not in out


def test_plot_thumbnail_unresolved_db_gives_hint():
    html = '<img src="plot:scatter?color=TREM2" />'
    out = browser._expand_plot_thumbnails(html, default_db=None)
    assert "needs" in out and "db=group/file" in out
    assert "<img" not in out          # no broken image left behind


def test_plot_thumbnail_noop_without_scheme():
    html = '<p><img src="https://x.org/f.png" /></p>'
    assert browser._expand_plot_thumbnails(html, default_db="GroupA/ds1") == html


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
        "collection": {"group": "Group A Study", "experiment": "Pharmacogenomics",
                       "description": "# Group A Study\n\nA cohort of microglia datasets."},
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
        "access": "public", "source": "external", "suspension_type": "nucleus",
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


# /catalogs (REST 2.30): one starcat catalog + one geneset catalog.
_CATALOGS = {
    "count": 2,
    "catalogs": [
        {"catalog_id": "MICROGLIA_T", "source": "starcat", "name": "Microglia Test",
         "description": "test starcat catalog", "n_modules": 2, "n_scores": 1, "n_genes": 100,
         "metadata": {"organism": "human"}},
        {"catalog_id": "StudyX/Exp1", "source": "geneset", "name": "StudyX Exp1",
         "description": "", "n_modules": 1, "n_scores": 0, "n_genes": 50, "metadata": {}},
    ],
}
_CATALOG_STARCAT = {
    "catalog_id": "MICROGLIA_T", "source": "starcat", "name": "Microglia Test",
    "description": "test starcat catalog", "n_modules": 2, "n_scores": 1, "n_genes": 100,
    "metadata": {"organism": "human"},
    "modules": [
        {"name": "Microglia_DAM", "group": "states", "description": "DAM program; APOE, TREM2",
         "n_genes": 100, "has_weights": True},
        {"name": "Microglia_Homeostatic", "group": "identity", "description": "Homeostatic; P2RY12",
         "n_genes": 100, "has_weights": True},
    ],
    "scores": [
        {"name": "DAM_progression", "kind": "continuous",
         "columns": ["Microglia_DAM", "Microglia_Homeostatic"], "weights": [1, -1],
         "threshold": None, "normalization": "normalized"},
    ],
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


def _fake_api_get(api_url, path):  # pyright: ignore
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
    if path == "/catalogs":
        return _CATALOGS
    if path == "/catalogs/starcat/MICROGLIA_T":
        return _CATALOG_STARCAT
    if path.startswith("/module-variability"):
        return {"rows": [
            {"catalog": "MICROGLIA_V1", "module": "DAM_pos", "dataset": "GroupA/ds1"},
            {"catalog": "MICROGLIA_V1", "module": "DAM_pos", "dataset": "GroupA/ds2"},
            {"catalog": "MICROGLIA_V1", "module": "Cycling_pos", "dataset": "GroupA/ds1"},
        ]}
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


def test_get_collection_list_groups_and_counts(fake_api):  # pyright: ignore
    collections = browser.get_collection_list("http://x")
    by_id = {c["group_id"]: c for c in collections}

    assert set(by_id) == {"GroupA", "GroupB"}
    assert by_id["GroupA"]["dataset_count"] == 2
    assert by_id["GroupA"]["total_cells"] == 501_500
    # friendly name comes from collection.group
    assert by_id["GroupA"]["name"] == "Group A Study"
    # GroupB has no group_id field → derived from id prefix
    assert by_id["GroupB"]["dataset_count"] == 1


def test_get_collection_list_mix_summaries(fake_api):  # pyright: ignore
    by_id = {c["group_id"]: c for c in browser.get_collection_list("http://x")}
    ga = by_id["GroupA"]
    # human + mouse → mix
    assert ga["organism"]["abbrev"] == "mix"
    assert ga["organism"]["full"] == "human, mouse"
    # microglial cell + astrocyte → mix
    assert ga["celltype"]["abbrev"] == "mix"
    # shared single value → abbreviated
    assert ga["tissue"]["abbrev"] == "PFC"


def test_get_collection_list_sorted_newest_first(fake_api):  # pyright: ignore
    collections = browser.get_collection_list("http://x")
    # GroupA last_modified 2026-05-01 > GroupB 2026-03-01
    assert collections[0]["group_id"] == "GroupA"
    assert collections[0]["last_modified"] == "2026-05-01"


def test_get_filter_options_splits_and_dedups(fake_api):  # pyright: ignore
    collections = browser.get_collection_list("http://x")
    options = browser.get_filter_options(collections)
    # organism mix in GroupA contributes both; GroupB contributes human again
    assert options["organism"] == ["human", "mouse"]
    assert options["disease"] == ["alzheimer disease", "normal"]
    assert "single-cell" in options["technology_category"]


def test_get_collection_datasets_filters_and_builds_description(fake_api):  # pyright: ignore
    datasets, description_html, collection_meta = browser.get_collection_datasets("http://x", "GroupA")
    assert len(datasets) == 2
    names = {d["name"] for d in datasets}
    assert names == {"Dataset One", "Dataset Two"}
    # blurb rendered from collection.description (index.md body), NOT experiment;
    # the leading '# Group A Study' H1 is stripped (avoids duplicating the title).
    assert description_html is not None
    assert "A cohort of microglia datasets" in description_html
    assert "<h1" not in description_html          # leading H1 stripped
    assert "Pharmacogenomics" not in description_html  # experiment NOT used as blurb
    # GroupA: ds1 (human/microglia/PFC/AD/nucleus) + ds2 (mouse/astrocyte/PFC/normal)
    # → tissue + suspension_type are constant (header pills); organism, celltype,
    #   disease vary (stay as columns).
    pill_titles = " ".join(p["title"] for p in collection_meta["pills"])
    assert "prefrontal cortex" in pill_titles      # tissue constant → header
    assert "organism" in collection_meta["var_fields"]
    assert "celltype" in collection_meta["var_fields"]
    assert "tissue" not in collection_meta["var_fields"]


def test_get_collection_datasets_unknown_group_is_empty(fake_api):  # pyright: ignore
    datasets, description_html, collection_meta = browser.get_collection_datasets("http://x", "Nope")
    assert datasets == []
    assert description_html is None
    assert collection_meta["pills"] == [] and collection_meta["var_fields"] == []


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


def test_chat_widget_present_and_status_gated(client):
    # The sitewide data-assistant widget ships in base.html on every page, but
    # is gated entirely client-side on GET /api/chat/status — so the launcher
    # markup is present yet hidden, and the page never assumes chat is on.
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="chat-launcher"' in resp.text
    assert 'id="chat-panel"' in resp.text
    # gated on the capability endpoint, and it talks to the POST chat endpoint
    assert "/api/chat/status" in resp.text
    assert "/api/chat" in resp.text
    # launcher must start hidden (revealed by JS only when status says enabled)
    assert "#chat-launcher" in resp.text and "display: none" in resp.text


def test_chat_consent_gate_present(client):
    # One-time privacy consent gate lives in the panel and acks via POST.
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="chat-gate"' in resp.text
    assert 'id="chat-ack-btn"' in resp.text
    assert "/api/chat/ack" in resp.text
    # links to the standalone privacy note
    assert 'href="/privacy"' in resp.text


def test_privacy_page_renders(client):
    resp = client.get("/privacy")
    assert resp.status_code == 200
    assert "privacy" in resp.text.lower()
    assert "never leaves this server" in resp.text


def test_geneset_variability_page_renders(client):
    resp = client.get("/genesets/variability")
    assert resp.status_code == 200
    assert 'id="mv-table"' in resp.text
    assert "/module-variability" in resp.text
    # 'variability' must not be swallowed as a study id by the 2-seg route
    assert "module variability" in resp.text.lower()
    # configurable colour-scale clip + dismissable exploratory caveat
    assert 'id="mv-cscale"' in resp.text
    assert 'id="mv-caveat-ack"' in resp.text


def test_genesets_index_links_variability(client):
    resp = client.get("/genesets")
    assert resp.status_code == 200
    assert '/genesets/variability' in resp.text


def test_genesets_group_selector(client):
    # The index offers a group selector; the starCAT + find-by-gene tabs are
    # present, geneset content shows on the default group.
    home = client.get("/genesets")
    assert home.status_code == 200
    assert "grp-tab" in home.text
    assert "/genesets?group=starcat" in home.text   # the starCAT tab href
    assert "/genesets?group=bygene" in home.text     # the find-by-gene tab href
    assert "StudyX" in home.text                     # geneset content (default group)


def test_genesets_find_by_gene_mode(client):
    # The 'find by gene' mode renders a search box wired to the reverse-lookup
    # endpoint (results are fetched + rendered client-side).
    resp = client.get("/genesets?group=bygene")
    assert resp.status_code == 200
    assert 'id="bg-gene"' in resp.text
    assert "/genesets/by-gene/" in resp.text


def test_genesets_starcat_group_lists_modules_and_scores(client):
    # The starCAT group (from /catalogs) embeds the catalog data for the
    # dropdown-driven client view: catalog/category selectors + module data +
    # descriptions + the derived-score names. Rows are rendered client-side, so
    # we assert on the embedded JSON + the controls, not built URLs.
    resp = client.get("/genesets?group=starcat")
    assert resp.status_code == 200
    assert 'id="sc-cat"' in resp.text and 'id="sc-group"' in resp.text   # dropdowns
    assert 'id="sc-data"' in resp.text                                   # embedded catalog data
    assert "Microglia_DAM" in resp.text
    assert "DAM program; APOE, TREM2" in resp.text          # module description
    assert "DAM_progression" in resp.text                   # derived score
    assert "Derived scores" in resp.text


def test_geneset_starcat_module_detail_renders(client):
    # Module detail page renders and isn't swallowed by the generic
    # /genesets/{study}/{experiment}/{name} route; fetches the module endpoint.
    resp = client.get("/genesets/starcat/MICROGLIA_T/modules/Microglia_DAM")
    assert resp.status_code == 200
    assert 'id="sc-top"' in resp.text
    assert "/catalogs/starcat/" in resp.text
    assert "starCAT program" in resp.text


def test_admin_feedback_requires_admin(client):
    # Non-admin → 403 (the triage page is admin-only).
    resp = client.get("/admin/feedback", follow_redirects=False)
    assert resp.status_code == 403


def test_admin_feedback_page_renders(client):
    resp = client.get("/admin/feedback", headers={"Remote-Groups": "admins"})
    assert resp.status_code == 200
    assert 'id="fb-table"' in resp.text
    assert "/api/chat/feedback" in resp.text
    # wrapped for responsive scroll per the table convention
    assert 'class="table-wrap"' in resp.text


def test_admin_feedback_link_shown_only_to_admins(client):
    # The topbar feedback link appears for admins…
    admin = client.get("/", headers={"Remote-User": "mf", "Remote-Groups": "admins"})
    assert '/admin/feedback' in admin.text
    # …and not for a signed-in non-admin.
    plain = client.get("/", headers={"Remote-User": "mf", "Remote-Groups": "users"})
    assert '/admin/feedback' not in plain.text


def test_dataset_editor_requires_admin(client):
    # Without the Authelia admins group the edit page is refused.
    resp = client.get("/admin/edit/GroupA/ds1", follow_redirects=False)
    assert resp.status_code == 403


def test_dataset_editor_mus_section_present(client):
    # Admin → the dataset editor renders, including the .mus biology-annotation
    # editor (REST 2.25) that talks to /source.mus and the optional /mus/vocab.
    resp = client.get("/admin/edit/GroupA/ds1",
                      headers={"Remote-Groups": "admins, users"})
    assert resp.status_code == 200
    assert 'id="mus-fields"' in resp.text
    assert 'id="mus-save"' in resp.text
    assert "/source.mus" in resp.text
    assert "/api/mus/vocab" in resp.text
    # the old "edited separately — not here" dead-end note is gone
    assert "not wired here yet" not in resp.text


def test_index_renders_with_multiple_collections_no_redirect(client):
    # Sanity: with the default fake (two groups), '/' renders the overview;
    # the single-collection auto-jump must not fire here.
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 200


def test_index_auto_redirects_to_only_collection(client, monkeypatch):
    # Reduce the fake catalog to a single group → '/' should 302 into it.
    single = [db for db in _DATABASES if db.get("group_id") == "GroupA"
              or db["id"].startswith("GroupA/")]

    def fake(api_url, path):
        if path == "/databases":
            return {"databases": single}
        return _fake_api_get(api_url, path)

    monkeypatch.setattr(browser, "_api_get", fake)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/browse/GroupA"


def test_index_no_redirect_when_query_string_present(client, monkeypatch):
    # Opt-out: any query string (search, filter, explicit ?all=1) suppresses
    # the auto-jump so the overview is reachable even with one collection.
    single = [db for db in _DATABASES if db.get("group_id") == "GroupA"
              or db["id"].startswith("GroupA/")]

    def fake(api_url, path):
        if path == "/databases":
            return {"databases": single}
        return _fake_api_get(api_url, path)

    monkeypatch.setattr(browser, "_api_get", fake)
    for qs in ("?all=1", "?search=foo", "?organism=human"):
        resp = client.get(f"/{qs}", follow_redirects=False)
        assert resp.status_code == 200, f"unexpected redirect for {qs!r}"
        assert "Group A Study" in resp.text


def test_index_zero_collections_no_redirect(client, monkeypatch):
    # An empty catalog must keep rendering the existing empty page, not 302
    # somewhere broken.
    def fake(api_url, path):
        if path == "/databases":
            return {"databases": []}
        return _fake_api_get(api_url, path)

    monkeypatch.setattr(browser, "_api_get", fake)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 200


def test_index_survives_api_failure(client, monkeypatch):
    def boom(api_url, path):  # pyright: ignore
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


def test_dataset_view_downloads_section(client):
    # Downloads section: client fetches /files and links to /download/{kind}
    # (presigned S3 or streamed local).
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    assert 'id="ds-files"' in resp.text
    assert "/files" in resp.text
    assert "/download/" in resp.text


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

    # rebind on the client's monkeypatched module (setattr keeps pyright quiet
    # about the dynamic _api_get attribute on the ModuleType)
    setattr(browser, "_api_get", recording)
    try:
        resp = client.get("/browse/GroupA/ds1/plot/scatter")
    finally:
        setattr(browser, "_api_get", _fake_api_get)
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
    assert 'clip density to percentiles' in resp.text


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


def test_plot_view_volcano_axis_clamp(client):
    # optional axis-clamp controls (REST API 2.7.2): log2fc_clip (x) + neglog10p_clip (y)
    resp = client.get("/browse/GroupA/ds1/plot/volcano")
    assert resp.status_code == 200
    assert 'name="log2fc_clip"' in resp.text
    assert 'name="neglog10p_clip"' in resp.text


def test_plot_view_ma_axis_clamp(client):
    resp = client.get("/browse/GroupA/ds1/plot/ma")
    assert resp.status_code == 200
    assert 'name="log2fc_clip"' in resp.text
    assert 'name="logmean_clip"' in resp.text


def test_plot_view_notebook_button(client):
    # "📓 notebook" opens the plot as a one-off JupyterLite notebook via the
    # ant-serve /plot/{geom}/notebook endpoint + JupyterLite ?fromURL= (API 2.8).
    resp = client.get("/browse/GroupA/ds1/plot/scatter")
    assert resp.status_code == 200
    assert 'id="open-notebook"' in resp.text
    assert "/jupyter/lab/index.html?fromURL=" in resp.text
    # base64-path-encoded endpoint (Plan D): no query string → clean basename
    assert "/api/notebook/" in resp.text
    assert "plot.ipynb" in resp.text


def test_plot_view_de_quadrant_form(client):
    # de_quadrant (API 2.12): two DE cascades (X = path dataset, Y = db_y||path),
    # per-axis stat selects, optional db_y override, mode/n_label knobs.
    resp = client.get("/browse/GroupA/ds1/plot/de_quadrant")
    assert resp.status_code == 200
    for field in ("study", "term", "contrast", "db_y", "study_y", "term_y",
                  "contrast_y", "axis_x", "axis_y", "mode", "n_label",
                  "clip_x", "clip_y"):
        assert f'name="{field}"' in resp.text
    assert "GLMM demo" in resp.text          # X-side study seeded from /de
    assert "initDeQuadrant" in resp.text     # dedicated dual-cascade init


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


def test_plot_view_de_heatmap_form(client):
    # de_heatmap (DE matrix; contrasts × genes) renders the study→term cascade
    # plus the value / significance-overlay / CSV-narrower controls.
    resp = client.get("/browse/GroupA/ds1/plot/de_heatmap")
    assert resp.status_code == 200
    for field in ("study", "term", "value", "sig_overlay", "contrasts", "genes",
                  "vmin", "vmax", "vmin_quantile", "vmax_quantile", "sort", "sort_desc"):
        assert f'name="{field}"' in resp.text
    assert "GLMM demo" in resp.text   # study option seeded from /de
    assert "to EXCLUDE" in resp.text   # the '-' exclude-syntax help (API 2.10)


def test_plot_view_color_scale_control(client):
    # The continuous-colour-ramp override (auto/sequential/divergent) and the
    # new named single-hue cmaps live in the settings panel of every plot page.
    resp = client.get("/browse/GroupA/ds1/plot/hexbin")
    assert resp.status_code == 200
    assert 'id="s-color_scale"' in resp.text
    assert 'value="sequential"' in resp.text
    assert 'value="divergent"' in resp.text
    assert "'turquoise'" in resp.text   # new named cmap registered in PALETTES


def test_about_page_renders(client):
    # /about: hero + inlined logo (data URI from the committed asset), what-it-is
    # copy, live catalogue counts, and quick links.
    resp = client.get("/about")
    assert resp.status_code == 200
    assert "about-hero" in resp.text
    assert "data:image/png;base64," in resp.text   # logo inlined, no static dir
    assert "browse datasets" in resp.text
    assert "REST API" in resp.text


def test_about_link_in_nav(client):
    # the topbar nav links to /about
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'href="/about"' in resp.text


def test_gene_atlas_page_renders(client):
    # /genes/{gene}: server-rendered shell; per-gene cross-dataset stats are
    # fetched client-side from /api/gene_stats_all (API 2.14).
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    assert "gene atlas" in resp.text.lower()
    assert "/api/gene_stats_all" in resp.text
    assert "APOE" in resp.text
    assert 'data-sort="scaled_mean"' in resp.text   # sort-by-abundance control
    assert "renderHeatmap" in resp.text             # multi-gene branch present
    assert 'id="flt-cell_type"' in resp.text         # in-page filter bar (organism/cell_type/tissue)


def test_gene_atlas_multigene_route(client):
    # a comma-separated gene list resolves (→ client renders the gene×dataset heatmap)
    resp = client.get("/genes/APOE,TREM2,CX3CR1")
    assert resp.status_code == 200
    assert "renderHeatmap" in resp.text


def test_gene_search_box_in_topbar(client):
    # topbar gene search → /genes/{gene} (present on every page incl. landing)
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'class="gene-search"' in resp.text
    assert "'/genes/'" in resp.text


def test_favicon_routes_served(client):
    # favicons/manifest are served by the app (from web/favicons/, since the
    # webapp/ static dir is gitignored). The reverse proxy routes these here.
    for path, ctype in [("/favicon.ico", "image/x-icon"),
                        ("/favicon.svg", "svg"),
                        ("/site.webmanifest", "manifest"),
                        ("/apple-touch-icon.png", "png")]:
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert ctype in resp.headers.get("content-type", ""), path


def test_favicon_links_in_head(client):
    resp = client.get("/")
    assert 'rel="manifest"' in resp.text
    assert "/favicon.svg" in resp.text
    assert 'name="theme-color"' in resp.text


def test_plot_view_info_tooltips_and_collapse(client):
    # Verbose help sits behind hover ⓘ icons (class="info"); single-choice
    # selects collapse to static text client-side; size presets have a
    # portrait/landscape toggle.
    resp = client.get("/browse/GroupA/ds1/plot/scatter")
    assert resp.status_code == 200
    assert 'class="info"' in resp.text
    assert "function collapseSingleSelects" in resp.text
    assert 'class="orient-btn' in resp.text


def test_genesets_index_renders_grouped(client):
    # Gene-set group is now dropdown-driven: the study/experiment selectors +
    # the embedded by_study JSON (which carries the study names + display names).
    resp = client.get("/genesets")
    assert resp.status_code == 200
    assert 'id="gs-study"' in resp.text and 'id="gs-exp"' in resp.text
    assert "StudyX" in resp.text          # study key (embedded JSON)
    assert "StudyY" in resp.text
    assert "Up genes" in resp.text        # geneset display_name (embedded JSON)


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


def test_ui_state_helper_is_sitewide(client):
    # The antRemember localStorage helper ships in base.html so any page can
    # persist dropdown/filter state across reloads.
    resp = client.get("/genesets")
    assert resp.status_code == 200
    assert "window.antRemember" in resp.text


def test_geneset_detail_has_variability_panel(client):
    # The geneset detail page includes the 'where it varies most' panel, which
    # fetches /module-variability for this geneset name client-side.
    resp = client.get("/genesets/StudyX/Exp1/gs_up")
    assert resp.status_code == 200
    assert "Where it varies most" in resp.text
    assert 'id="gv-stat"' in resp.text
    assert "/module-variability?statistic=" in resp.text


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


def test_topbar_authelia_identity_and_signout(client):
    # When behind Authelia (Remote-* headers forwarded by Caddy), the topbar
    # surfaces the user + a sign-out action + account-settings link.
    resp = client.get("/", headers={
        "Remote-User": "mf", "Remote-Name": "Mark Fiers",
        "Remote-Email": "mark@example.org",
    })
    assert resp.status_code == 200
    assert "Mark Fiers" in resp.text
    assert 'id="authelia-signout"' in resp.text
    assert "/authelia/api/logout" in resp.text
    assert "/authelia/" in resp.text          # account settings link


def test_topbar_no_auth_section_without_header(client):
    # Direct / un-proxied access (no Remote-User) → no auth UI.
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="authelia-signout"' not in resp.text


def test_landing_popover_filter_guard(client):
    # Regression: the column-header popover is a child of the <th>, so its th
    # click handler must ignore clicks that originate inside the open popover —
    # otherwise filter checkboxes close the popover before they register.
    resp = client.get("/")
    assert resp.status_code == 200
    assert "activePopover.contains(e.target)" in resp.text


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


def test_folder_collection_pills_hoist_constants(client):
    # GroupA: tissue (prefrontal cortex) + prep (nucleus) + access (public) are
    # constant across both datasets → hoisted to inert header pills and their
    # columns dropped; organism/celltype/disease vary → columns kept.
    resp = client.get("/browse/GroupA")
    assert resp.status_code == 200
    assert 'class="coll-meta"' in resp.text          # header pill row
    assert "prefrontal cortex" in resp.text            # tissue constant → pill
    assert 'data-label="Tissue"' not in resp.text      # tissue column dropped
    assert 'data-label="Prep"' not in resp.text        # prep column dropped
    assert 'data-label="Org"' in resp.text             # organism varies → kept
    assert 'data-label="Cell"' in resp.text            # celltype varies → kept


def test_folder_access_source_inline_only_when_variable(client):
    # source varies (internal vs external) → inline glyph before the name;
    # access is constant (public) → it's a header pill, not inline.
    resp = client.get("/browse/GroupA")
    assert resp.status_code == 200
    assert 'class="acc-src"' in resp.text              # source inline (varies)
    assert 'data-label="Access"' not in resp.text      # never a column
    assert 'data-label="Source"' not in resp.text


def test_folder_modified_column_and_newest_first(client):
    # Each dataset shows a Modified date; the list is sorted newest-first.
    resp = client.get("/browse/GroupA")
    assert resp.status_code == 200
    assert 'data-label="Modified"' in resp.text
    assert "2026-05-01" in resp.text          # ds1's updated date
    # ds1 (2026-05-01) sorts above ds2 (2026-04-01)
    assert resp.text.index("Dataset One") < resp.text.index("Dataset Two")


def test_dataset_page_shows_modified(client):
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    assert "modified:" in resp.text
    assert "2025-01-01" in resp.text   # _INFO has year=2025 → _modified_ts year-cast


def test_folder_external_link_icons(client):
    # datasets with a doi/link get small link icons after the name (.ext-lnk)
    resp = client.get("/browse/GroupA")
    assert resp.status_code == 200
    assert 'class="ext-lnk"' in resp.text
    assert "https://doi.org/10.1/abc" in resp.text
    assert "https://example.org/ds1" in resp.text


def test_dataset_quick_links(client):
    # DOI / PubMed / accession surfaced as compact badges in the top provenance
    # row (.ds-prov), just after the id strip.
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    assert 'class="ds-prov"' in resp.text
    assert "https://doi.org/10.1/abc" in resp.text
    assert "pubmed.ncbi.nlm.nih.gov/123456" in resp.text


def test_plot_bar_stacked_controls(client):
    # bar form exposes the stacked-composition controls (REST API 2.6).
    resp = client.get("/browse/GroupA/ds1/plot/bar")
    assert resp.status_code == 200
    assert 'name="bar_mode"' in resp.text
    assert 'value="stack"' in resp.text
    assert 'name="normalize"' in resp.text
    assert 'name="group_by"' in resp.text   # in-plot clustering (REST API 2.6)
    assert 'name="facet"' in resp.text       # separate panels
    # not on non-bar grouped geoms
    v = client.get("/browse/GroupA/ds1/plot/violin")
    assert 'name="bar_mode"' not in v.text
    assert 'name="facet"' not in v.text
    assert 'name="group_by"' not in v.text


def test_plot_data_download_button(client):
    # "⬇ data" button (CSV export of the plot's underlying data, REST API 2.6).
    resp = client.get("/browse/GroupA/ds1/plot/scatter")
    assert resp.status_code == 200
    assert 'id="dl-data"' in resp.text
    # downloads are blob-fetched at click time (no href → no URL in the status
    # bar); the CSV button is wired through downloadBlob via the dl-data entry.
    assert "function downloadBlob" in resp.text
    assert "['dl-data', 'csv', 'csv']" in resp.text


def test_plot_force_refresh_control(client):
    # The plot toolbar offers a force-refresh (cache-bypassing re-render).
    resp = client.get("/browse/GroupA/ds1/plot/scatter")
    assert resp.status_code == 200
    assert 'id="force-refresh"' in resp.text


def test_plot_settings_in_toolbar_dropdown(client):
    # Figure settings live in a toolbar dropdown (.settings-menu), not the sidebar.
    resp = client.get("/browse/GroupA/ds1/plot/scatter")
    assert resp.status_code == 200
    assert 'class="settings-menu"' in resp.text
    assert 'id="settings-toggle"' in resp.text
    assert 'id="s-dpi"' in resp.text   # a settings control still present


def test_plot_settings_metric_and_presets(client):
    # mm units, A4/A5/A6 + screen presets, a resolution dropdown (DPI alt) with
    # px readout. Palettes are JS-generated (selects start empty in HTML).
    resp = client.get("/browse/GroupA/ds1/plot/scatter")
    assert resp.status_code == 200
    assert "width (mm)" in resp.text and "height (mm)" in resp.text   # metric
    assert 'data-long="210" data-short="148"' in resp.text           # A5 preset (orientation-aware)
    assert 'class="size-presets"' in resp.text                        # preset rows
    assert 'class="orient-btn' in resp.text                           # portrait/landscape toggle
    assert 'id="px-readout"' in resp.text                             # DPI alternative readout
    assert "screen (96)" in resp.text and "print (300)" in resp.text  # resolution dropdown
    # palette base lists + auto-reverse are defined in JS
    assert "function populatePalettes" in resp.text
    assert "viridis" in resp.text and "vlag" in resp.text


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


# ── shared .btn-edit class (muted-red admin action) ────────────────────────────

def test_btn_edit_class_defined_sitewide(client):
    # The muted-red .btn-edit style lives in base.html so both the group and
    # dataset pages render the Edit affordance with identical treatment.
    resp = client.get("/")
    assert resp.status_code == 200
    assert ".btn-edit" in resp.text
    # background hex — anchor colour of the muted-red palette
    assert "#c26b6b" in resp.text


def test_dataset_edit_button_uses_btn_edit(client):
    # Admin sees a top-right ✎ edit button that uses the shared .btn-edit class
    # (muted-red), not a chip.
    resp = client.get("/browse/GroupA/ds1",
                      headers={"Remote-Groups": "admins"})
    assert resp.status_code == 200
    assert 'class="btn-edit' in resp.text
    assert '/admin/edit/GroupA/ds1' in resp.text


def test_folder_edit_button_top_right(client):
    # Group page also carries the muted-red Edit button top-right, admin-gated.
    admin = client.get("/browse/GroupA", headers={"Remote-Groups": "admins"})
    assert admin.status_code == 200
    assert 'class="btn-edit' in admin.text
    assert '/admin/edit/GroupA' in admin.text
    # Non-admin sees no Edit affordance on the group page.
    plain = client.get("/browse/GroupA")
    assert plain.status_code == 200
    assert 'class="btn-edit' not in plain.text


# ── sitewide tab-bar CSS (used by dataset + edit_dataset pages) ────────────────

def test_tab_bar_css_shared_in_base(client):
    # The .tab-bar / .tab-btn / .tab-panel styles live in base.html so both the
    # dataset page and the edit page pick them up.
    resp = client.get("/")
    assert resp.status_code == 200
    assert ".tab-bar" in resp.text
    assert ".tab-btn" in resp.text
    assert ".tab-panel" in resp.text


# ── dataset page: tab refactor ─────────────────────────────────────────────────

def test_dataset_view_has_tab_bar(client):
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    # Tab bar container + at least the always-visible tabs
    assert 'id="ds-tab-bar"' in resp.text
    assert 'data-tab="overview"' in resp.text
    assert 'data-tab="data"' in resp.text
    assert 'data-tab="num"' in resp.text
    assert 'data-tab="cat"' in resp.text
    # Header items live ABOVE the tab bar
    assert resp.text.index('id="ds-tab-bar"') > resp.text.index('class="ds-top"')


def test_dataset_view_de_tab_only_when_de_present(client, monkeypatch):
    # DE tab appears when the dataset has DE data …
    resp_de = client.get("/browse/GroupA/ds1")
    assert 'data-tab="de"' in resp_de.text
    # … and is absent when it does not.
    def no_de(api_url, path):
        if path.endswith("/de"):
            raise requests.HTTPError("no DE")
        return _fake_api_get(api_url, path)
    monkeypatch.setattr(browser, "_api_get", no_de)
    resp_no = client.get("/browse/GroupA/ds1")
    assert 'data-tab="de"' not in resp_no.text


def test_dataset_view_downloads_tab_hidden_by_default(client):
    # The Downloads tab is display:none until the /files fetch confirms an
    # h5ad exists (JS reveals it). Server-render should carry the hidden state.
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    assert 'id="tab-btn-downloads"' in resp.text
    # rendered with style="display:none" until JS proves otherwise
    idx = resp.text.index('id="tab-btn-downloads"')
    button_snippet = resp.text[idx:idx + 200]
    assert 'display:none' in button_snippet


def test_dataset_view_embeddings_on_overview_tab(client):
    # The standalone Embeddings tab was removed; its content is now merged
    # INTO the Explore section on the Overview tab as a fourth sibling row
    # (genes / categories / numerical / embeddings), promoted 2026-08. When
    # obsm keys exist we expect an "embeddings" label + scatter chip inside
    # the #tab-overview panel's Explore block, and NO Embeddings tab button
    # and NO standalone <h2>Embeddings heading.
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    assert 'data-tab="embed"' not in resp.text
    assert 'id="tab-embed"' not in resp.text
    # Embeddings row lives inside the Explore section on the Overview tab.
    overview_start = resp.text.index('id="tab-overview"')
    overview_end = resp.text.index('</section>', overview_start)
    overview_panel = resp.text[overview_start:overview_end]
    explore_start = overview_panel.index('<h2>Explore</h2>')
    # Bound the Explore block by the next <h2> (Biology) or Resources.
    next_h2 = overview_panel.find('<h2>', explore_start + 1)
    explore_panel = overview_panel[explore_start:next_h2 if next_h2 != -1 else None]
    assert '>embeddings<' in explore_panel  # the <span class="mk"> label
    # ds1's fake exposes X_umap → a scatter chip must be inside Explore
    assert 'plot/scatter?embedding=X_umap' in explore_panel
    # And there must be no standalone <h2>Embeddings anywhere.
    assert '<h2>Embeddings' not in resp.text


def test_dataset_view_embeddings_subheading_hidden_when_no_obsm(client, monkeypatch):
    # If the dataset has no obsm keys, the embeddings row inside Explore must
    # not render (but the Data tab itself stays present).
    def no_obsm(api_url, path):
        result = _fake_api_get(api_url, path)
        if path.endswith("/info") and isinstance(result, dict):
            result = dict(result)
            result["embeddings"] = {}
        return result
    monkeypatch.setattr(browser, "_api_get", no_obsm)
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    # Data tab still present
    assert 'data-tab="data"' in resp.text
    # No standalone Embeddings heading anywhere
    assert '<h2>Embeddings' not in resp.text
    # And no embeddings row inside Explore (no scatter chip)
    assert 'plot/scatter?embedding=' not in resp.text


def test_dataset_view_downloads_client_filters_to_h5ad(client):
    # The Downloads JS must filter /files output to kind == 'h5ad' only and
    # additionally suppress .duckdb / .layer.*.parquet / .h5ad.mus / .md /
    # dotfile / underscore-prefixed entries.
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    assert "f.kind !== 'h5ad'" in resp.text
    assert ".duckdb" in resp.text
    assert ".layer." in resp.text
    assert ".h5ad.mus" in resp.text


# ── group page: static folder + Downloads section ──────────────────────────────

def test_is_download_hidden():
    hide = browser._is_download_hidden
    assert hide('foo.duckdb')
    assert hide('foo.h5ad.mus')
    assert hide('foo.md')
    assert hide('foo.inactive')
    assert hide('foo.layer.counts.parquet')
    assert hide('_staging.txt')
    assert hide('.hidden')
    assert not hide('supplement.pdf')
    assert not hide('table.csv')
    assert not hide('archive.zip')


def test_group_static_files_missing_dir_empty(tmp_path, monkeypatch):
    # No h5adstore/<group>/static directory → empty list, never raises.
    monkeypatch.setattr(browser, "_H5ADSTORE_ROOT", tmp_path)
    assert browser._group_static_files("Nope") == []


def test_group_static_files_lists_only_visible(tmp_path, monkeypatch):
    static = tmp_path / "GroupA" / "static"
    static.mkdir(parents=True)
    (static / "supplement.pdf").write_bytes(b"pdf")
    (static / "results.csv").write_bytes(b"csv")
    (static / "internal.duckdb").write_bytes(b"x")   # hidden by suffix
    (static / "_draft.txt").write_bytes(b"x")         # hidden by prefix
    (static / ".DS_Store").write_bytes(b"x")          # hidden by prefix
    monkeypatch.setattr(browser, "_H5ADSTORE_ROOT", tmp_path)
    names = [f["name"] for f in browser._group_static_files("GroupA")]
    assert names == ["results.csv", "supplement.pdf"]


def test_folder_view_hides_downloads_when_static_empty(client):
    # Default fake has no static/ dir → the group page must not render a
    # "Downloads" heading.
    resp = client.get("/browse/GroupA")
    assert resp.status_code == 200
    assert "<h2>Downloads" not in resp.text


def test_folder_view_shows_static_downloads(client, tmp_path, monkeypatch):
    # With a populated static/ dir, the group page renders a Downloads section
    # and each file links to the group_static_download route.
    static = tmp_path / "GroupA" / "static"
    static.mkdir(parents=True)
    (static / "supplement.pdf").write_bytes(b"pdf-bytes")
    monkeypatch.setattr(browser, "_H5ADSTORE_ROOT", tmp_path)
    resp = client.get("/browse/GroupA")
    assert resp.status_code == 200
    assert "<h2>Downloads" in resp.text
    assert "/browse/GroupA/static/supplement.pdf" in resp.text


def test_group_static_download_serves_file(client, tmp_path, monkeypatch):
    static = tmp_path / "GroupA" / "static"
    static.mkdir(parents=True)
    (static / "hello.txt").write_text("hi there")
    monkeypatch.setattr(browser, "_H5ADSTORE_ROOT", tmp_path)
    resp = client.get("/browse/GroupA/static/hello.txt")
    assert resp.status_code == 200
    assert resp.text == "hi there"


def test_group_static_download_rejects_traversal(client, tmp_path, monkeypatch):
    static = tmp_path / "GroupA" / "static"
    static.mkdir(parents=True)
    (static / "ok.txt").write_text("ok")
    (tmp_path / "secret.txt").write_text("secret")
    monkeypatch.setattr(browser, "_H5ADSTORE_ROOT", tmp_path)
    # `..` in the filename is rejected before disk access
    resp = client.get("/browse/GroupA/static/..%2Fsecret.txt")
    assert resp.status_code == 404


def test_group_static_download_rejects_hidden_suffix(client, tmp_path, monkeypatch):
    static = tmp_path / "GroupA" / "static"
    static.mkdir(parents=True)
    (static / "x.duckdb").write_bytes(b"x")
    monkeypatch.setattr(browser, "_H5ADSTORE_ROOT", tmp_path)
    resp = client.get("/browse/GroupA/static/x.duckdb")
    assert resp.status_code == 404
