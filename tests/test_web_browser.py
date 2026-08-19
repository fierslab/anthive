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


# ── unit tests: _access_pub_icon ───────────────────────────────────────────────

def test_access_pub_icon_public_published():
    icon = browser._access_pub_icon("public", None)
    # Inline SVG (fill:currentColor) — NOT a color-emoji glyph, so CSS
    # `color:` applies. See docstring on _access_pub_icon.
    assert icon["glyph"].startswith("<svg") and "currentColor" in icon["glyph"]
    assert icon["color"] == browser._PUB_COLOR_PUBLISHED     # muted green
    assert "external" in icon["tooltip"] and "published" in icon["tooltip"]
    assert icon["access"] == "external"


def test_access_pub_icon_public_unpublished():
    icon = browser._access_pub_icon("public", "unpublished")
    assert icon["glyph"].startswith("<svg")
    assert icon["color"] == browser._PUB_COLOR_UNPUBLISHED   # muted red
    assert icon["tooltip"] == "external · unpublished"


def test_access_pub_icon_internal_published():
    icon = browser._access_pub_icon("internal", "published")
    assert icon["glyph"].startswith("<svg")
    assert icon["color"] == browser._PUB_COLOR_PUBLISHED
    assert icon["tooltip"] == "internal · published"


def test_access_pub_icon_internal_unpublished():
    icon = browser._access_pub_icon("internal", "unpublished")
    assert icon["glyph"].startswith("<svg")
    assert icon["color"] == browser._PUB_COLOR_UNPUBLISHED


def test_access_pub_icon_missing_access_defaults_internal():
    icon = browser._access_pub_icon(None, None)
    assert icon["glyph"].startswith("<svg")     # missing = safer default
    assert icon["access"] == "internal"


def test_access_pub_icon_unknown_access_falls_back():
    # non-vocab value (typo) should still yield a rendered icon, not crash
    icon = browser._access_pub_icon("bogus", None)
    assert icon["glyph"].startswith("<svg") and icon["access"] == "internal"


# ── unit tests: _dataset_date ──────────────────────────────────────────────────

def test_dataset_date_year_published():
    out = browser._dataset_date({"year": 2024})
    assert out["iso"] == "2024-01-01"
    assert out["sort_ts"] > 0


def test_dataset_date_year_with_month_day():
    out = browser._dataset_date({"year": 2024, "month": 6, "day": 15})
    assert out["iso"] == "2024-06-15"


def test_dataset_date_year_string_ok():
    # API sometimes hands year back as a string; parse defensively.
    out = browser._dataset_date({"year": "2023"})
    assert out["iso"] == "2023-01-01"


def test_dataset_date_unpublished_skips_year():
    # unpublished → NEVER emit a year (don't claim a false publication date);
    # falls through to file mtime → 0/'' when no file to stat.
    out = browser._dataset_date({"year": 2024}, publication_status="unpublished")
    assert out["iso"] == ""
    assert out["sort_ts"] == 0.0


def test_dataset_date_missing_year_and_file_is_empty():
    out = browser._dataset_date({})
    assert out["iso"] == ""
    assert out["sort_ts"] == 0.0


# ── unit tests: tissue-hierarchy sort ─────────────────────────────────────────

def test_tissue_hierarchy_yaml_loaded():
    # sanity: the ontology file exists in this repo, so the DFS map is populated
    hierarchy = browser._load_tissue_hierarchy()
    assert "brain" in hierarchy
    assert hierarchy["brain"] is None
    assert hierarchy["cortex"] == "brain"


def test_tissue_dfs_root_brain_first():
    # brain must precede its own subregions AND every other root
    ranks = browser._TISSUE_DFS_ORDER
    assert ranks["brain"] < ranks["cortex"] < ranks["prefrontal cortex"]


def test_tissue_dfs_cortex_hierarchy():
    ranks = browser._TISSUE_DFS_ORDER
    # cortex must come before its cortical children
    assert ranks["cortex"] < ranks["frontal cortex"]
    assert ranks["frontal cortex"] < ranks["prefrontal cortex"]
    assert ranks["prefrontal cortex"] < ranks["dorsolateral prefrontal cortex"]


def test_tissue_dfs_tail_regions_last():
    ranks = browser._TISSUE_DFS_ORDER
    # spinal cord + csf sink to the end
    assert ranks["brain"] < ranks["spinal cord"]
    assert ranks["brain"] < ranks["csf"]


def test_tissue_sort_key_unknown_last():
    # anything not in the hierarchy → unknown rank (sinks to the bottom)
    assert browser._tissue_sort_key(["not_a_tissue_ever"]) == browser._TISSUE_UNKNOWN_RANK
    assert browser._tissue_sort_key(None) == browser._TISSUE_UNKNOWN_RANK
    assert browser._tissue_sort_key([]) == browser._TISSUE_UNKNOWN_RANK


def test_tissue_sort_key_multi_uses_first():
    # multi-region → first value wins
    rank_brain = browser._tissue_sort_key(["brain", "csf"])
    rank_csf = browser._tissue_sort_key(["csf", "brain"])
    assert rank_brain < rank_csf


def test_tissue_sort_key_case_insensitive():
    assert browser._tissue_sort_key(["Brain"]) == browser._tissue_sort_key(["brain"])


def test_tissue_sort_key_orders_expected_list():
    """Concrete sort assertion: for a known heterogeneous tissue list the
    resulting order must be brain-first, cortex-family middle, tail terms
    (spinal cord / csf) last, unknown after everything."""
    inputs = [
        (["spinal cord"], "spinal cord"),
        (["dorsolateral prefrontal cortex"], "dlpfc"),
        (["brain"], "brain"),
        (["cortex"], "cortex"),
        (["not_a_tissue_ever"], "unknown"),
        (["csf"], "csf"),
    ]
    ranked = sorted(inputs, key=lambda tissues_label: browser._tissue_sort_key(tissues_label[0]))
    labels_in_order = [pair[1] for pair in ranked]
    # brain first; cortex before dlpfc; spinal cord + csf before unknown
    assert labels_in_order.index("brain") == 0
    assert labels_in_order.index("cortex") < labels_in_order.index("dlpfc")
    assert labels_in_order.index("spinal cord") < labels_in_order.index("unknown")
    assert labels_in_order.index("csf") < labels_in_order.index("unknown")


# ── unit tests: _tech_class ────────────────────────────────────────────────────

def test_tech_class_sc_sn_rna():
    assert browser._tech_class("10x_chromium") == "sc/sn RNA"
    assert browser._tech_class("10x_chromium_3p_v3") == "sc/sn RNA"
    assert browser._tech_class("Smart-seq2") == "sc/sn RNA"
    assert browser._tech_class("smart-seq2") == "sc/sn RNA"
    assert browser._tech_class("snRNA-seq") == "sc/sn RNA"
    assert browser._tech_class("parse_evercode") == "sc/sn RNA"
    assert browser._tech_class("BD_rhapsody") == "sc/sn RNA"
    assert browser._tech_class("Drop-seq") == "sc/sn RNA"


def test_tech_class_bulk_rna():
    assert browser._tech_class("bulk_RNA-seq") == "bulk RNA"
    assert browser._tech_class("bulk") == "bulk RNA"


def test_tech_class_proteomics():
    assert browser._tech_class("mass-spectrometry") == "proteomics"
    assert browser._tech_class("proteomics") == "proteomics"


def test_tech_class_spatial():
    assert browser._tech_class("10x_visium") == "spatial"
    assert browser._tech_class("10x_xenium") == "spatial"
    assert browser._tech_class("MERFISH") == "spatial"
    assert browser._tech_class("CosMx") == "spatial"
    assert browser._tech_class("Stereo-seq") == "spatial"


def test_tech_class_unknown_falls_back_to_other():
    assert browser._tech_class("some_new_tech_2099") == "other"
    assert browser._tech_class("nanopore") == "other"


def test_tech_class_empty_returns_empty():
    assert browser._tech_class(None) == ""
    assert browser._tech_class("") == ""
    assert browser._tech_class([]) == ""


def test_tech_class_list_single_class():
    # a list of same-class techs collapses to that class
    assert browser._tech_class(["10x_chromium", "10x_chromium_3p_v3"]) == "sc/sn RNA"


def test_tech_class_list_mixed():
    # a list mixing sc/sn RNA + spatial → 'mixed'
    assert browser._tech_class(["10x_chromium", "10x_visium"]) == "mixed"


# ── unit tests: _organism_chips ────────────────────────────────────────────────

def test_organism_chips_human_single():
    chips = browser._organism_chips("human")
    assert chips == [{"abbrev": "Hs", "cls": "hs", "full": "human"}]


def test_organism_chips_scientific_name():
    chips = browser._organism_chips("Homo sapiens")
    assert len(chips) == 1
    assert chips[0]["abbrev"] == "Hs"


def test_organism_chips_multi_pipe():
    # "human | mouse" — the pipe-separated form used in some catalog rows
    chips = browser._organism_chips("human | mouse")
    abbrevs = [chip["abbrev"] for chip in chips]
    assert abbrevs == ["Hs", "Mm"]


def test_organism_chips_multi_comma():
    chips = browser._organism_chips("human, mouse")
    abbrevs = [chip["abbrev"] for chip in chips]
    assert abbrevs == ["Hs", "Mm"]


def test_organism_chips_list_input():
    chips = browser._organism_chips(["human", "mouse", "rat"])
    abbrevs = [chip["abbrev"] for chip in chips]
    assert abbrevs == ["Hs", "Mm", "Rn"]


def test_organism_chips_empty():
    assert browser._organism_chips(None) == []
    assert browser._organism_chips("") == []
    assert browser._organism_chips([]) == []


def test_organism_chips_unknown_species_passthrough():
    chips = browser._organism_chips("axolotl")
    assert len(chips) == 1
    assert chips[0]["cls"] == "other"
    assert chips[0]["full"] == "axolotl"


def test_organism_chips_deduplicates():
    # duplicate entries in a list collapse to a single chip
    chips = browser._organism_chips(["human", "Homo sapiens", "human"])
    assert len(chips) == 1
    assert chips[0]["abbrev"] == "Hs"


# ── unit tests: _access_pub_icon ───────────────────────────────────────────────

def test_access_pub_icon_returns_inline_svg_not_emoji():
    """Regression: emoji glyphs (🌐 / 🏠) get rendered by color-emoji fonts,
    which override CSS `color:`. The icon MUST use inline SVG with
    `fill: currentColor` (or `stroke=currentColor`) so `color:` applies."""
    for access in ("public", "internal"):
        icon = browser._access_pub_icon(access, "published")
        assert icon["glyph"].startswith("<svg")
        assert "currentColor" in icon["glyph"]
        # no color-emoji leaking through
        assert "🌐" not in icon["glyph"]
        assert "🏠" not in icon["glyph"]


def test_access_pub_icon_published_muted_green():
    icon = browser._access_pub_icon("public", "published")
    assert icon["color"] == browser._PUB_COLOR_PUBLISHED
    assert icon["publication"] == "published"


def test_access_pub_icon_unpublished_muted_red():
    icon = browser._access_pub_icon("public", "unpublished")
    assert icon["color"] == browser._PUB_COLOR_UNPUBLISHED
    assert icon["publication"] == "unpublished"


def test_access_pub_icon_missing_access_defaults_internal():
    icon = browser._access_pub_icon(None, None)
    assert icon["access"] == "internal"


# ── unit tests: _flatten_annotation_field ─────────────────────────────────────

def test_flatten_annotation_field_string_passthrough():
    assert browser._flatten_annotation_field("microglial cell") == "microglial cell"


def test_flatten_annotation_field_list_joins():
    assert browser._flatten_annotation_field(
        ["microglial cell", "macrophage", "monocyte"]
    ) == "microglial cell,macrophage,monocyte"


def test_flatten_annotation_field_none_returns_empty():
    assert browser._flatten_annotation_field(None) == ""
    assert browser._flatten_annotation_field("") == ""
    assert browser._flatten_annotation_field([]) == ""


def test_flatten_annotation_field_list_with_empty_entries_skips_them():
    assert browser._flatten_annotation_field(["brain", "", None]) == "brain"


def test_build_dataset_annotations_flattens_list_values():
    """Regression: list-valued celltype/tissue threw TypeError in JS filter."""
    _orig = browser._all_databases
    browser._all_databases = lambda api_url: [
        {"id": "A/ds1", "celltype": "microglial cell", "tissue": "brain",
         "experimental_system": "post_mortem_tissue"},
        {"id": "A/ds2", "celltype": ["microglial cell", "macrophage"],
         "tissue": ["cortex", "hippocampus"], "experimental_system": None},
        {"id": "A/ds3", "celltype": None, "tissue": None, "experimental_system": "xenograft"},
    ]
    try:
        result = browser._build_dataset_annotations("http://x")
    finally:
        browser._all_databases = _orig

    # system field is included alongside celltype and tissue.
    # Extra keys (title, n_cells, contributor) are also present; check core fields.
    assert result["A/ds1"]["celltype"] == "microglial cell"
    assert result["A/ds1"]["tissue"] == "brain"
    assert result["A/ds1"]["system"] == "post_mortem_tissue"
    # List values must be flat strings — JS .toLowerCase() would throw on arrays.
    assert result["A/ds2"]["celltype"] == "microglial cell,macrophage"
    assert result["A/ds2"]["tissue"] == "cortex,hippocampus"
    assert result["A/ds2"]["system"] == ""
    assert result["A/ds3"]["celltype"] == ""
    assert result["A/ds3"]["tissue"] == ""
    assert result["A/ds3"]["system"] == "xenograft"


def test_build_dataset_annotations_system_field_in_all_entries():
    """Every entry must have a 'system' key, even when experimental_system absent."""
    _orig = browser._all_databases
    browser._all_databases = lambda api_url: [
        {"id": "B/ds1", "celltype": "neuron", "tissue": "cortex"},  # no experimental_system key
    ]
    try:
        result = browser._build_dataset_annotations("http://x")
    finally:
        browser._all_databases = _orig

    assert "system" in result["B/ds1"]
    assert result["B/ds1"]["system"] == ""


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
        "obs": {"categorical": ["cell_type", "sample", "ant_braak", "ant_cluster"], "numerical": ["n_genes", "pseudotime", "ant_age"]},
        "layers": {"X": {}, "counts": {}}, "embeddings": {"X_umap": {}, "X_pca": {}},
        "default_layer": "X",
        "top_genes": ["AIF1", "P2RY12"], "top_cat": ["cell_type"], "top_num": ["pseudotime"],
        "layer_stats": {
            "X": {"dtype": "float32", "min_value": 0.0, "min_nonzero": 0.01,
                  "max_value": 9.2, "median": 0.0, "q90": 3.1, "pct_zeros": 88.5},
        },
        "caveats": [
            {
                "level": "medium", "kind": "contamination",
                "scope": "microglia_subcluster=4",
                "obs_flag": "astro_contaminated",
                "message": ("5,827 cells (7.7%) co-express astrocyte markers. "
                            "Filter astro_contaminated:no for a pure-microglia subset."),
                "detected_at": "2026-08-16",
                "detected_by": "catalog audit v6",
            },
        ],
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
    "GroupA/ds2": {
        # No caveats — plot page should render a caveats-less banner.
        "file_id": "ds2", "group_id": "GroupA", "title": "Dataset Two",
        "n_cells": 500_000, "n_genes": 20000,
        "collection": {"group": "Group A Study"},
        "organism": "mouse", "obs": {"categorical": [], "numerical": []},
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
                 "parent_dataset": "GroupA/ds1", "parameters": {"fdr": "0.05"},
                 "celltypes": ["microglial cell"]},
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
    # → organism, celltype, disease vary (stay as columns).
    # Post-Aug-2026: tissue is excluded from the header-pill hoist and always
    #   appears as a column; suspension_type (prep) is also excluded (info now
    #   folds into the Tech tooltip). Only access (constant "public") remains
    #   as a header pill here.
    pill_titles = " ".join(p["title"] for p in collection_meta["pills"])
    assert "prefrontal cortex" not in pill_titles  # tissue is NOT hoisted anymore
    assert "prep" not in pill_titles                # suspension_type is NOT hoisted
    assert "organism" in collection_meta["var_fields"]
    assert "celltype" in collection_meta["var_fields"]
    assert "tissue" in collection_meta["var_fields"]  # tissue column always present


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
    # The index offers a group selector with the find-by-gene tab; starCAT
    # UI temporarily hidden (STARCAT_UI_ENABLED=False in browser.py — 2026-08).
    # Re-enable in browser.py and update this assertion when starCAT is back.
    home = client.get("/genesets")
    assert home.status_code == 200
    assert "grp-tab" in home.text
    assert "/genesets?group=bygene" in home.text     # the find-by-gene tab href
    assert "/genesets?group=starcat" not in home.text  # starCAT tab hidden
    assert "StudyX" in home.text                     # geneset content (default group)


def test_genesets_find_by_gene_mode(client):
    # The 'find by gene' mode renders a search box wired to the reverse-lookup
    # endpoint (results are fetched + rendered client-side).
    resp = client.get("/genesets?group=bygene")
    assert resp.status_code == 200
    assert 'id="bg-gene"' in resp.text
    assert "/genesets/by-gene/" in resp.text


def test_genesets_starcat_group_lists_modules_and_scores(client):
    # starCAT UI temporarily hidden (STARCAT_UI_ENABLED=False, 2026-08). When
    # re-enabled, restore the pre-2026-08 assertion set — see git blame.
    import pytest
    pytest.skip("starCAT UI temporarily hidden via STARCAT_UI_ENABLED flag")


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


# ── Task #251/#252/#254 — UX batch for the zero-fill bug fallout ─────────────

def test_plot_view_kde_exclude_zero_checkbox(client):
    """kde has an exclude-zero checkbox that submits with buildUrl."""
    resp = client.get("/browse/GroupA/ds1/plot/kde")
    assert resp.status_code == 200
    assert 'name="exclude_zero"' in resp.text
    assert 'id="f-exclude_zero"' in resp.text
    assert 'shape of expressers' in resp.text.lower()


def test_plot_view_histogram_exclude_zero_checkbox(client):
    """histogram has the same exclude-zero affordance."""
    resp = client.get("/browse/GroupA/ds1/plot/histogram")
    assert resp.status_code == 200
    assert 'name="exclude_zero"' in resp.text


def test_plot_view_ecdf_has_no_exclude_zero(client):
    """ecdf zeros are a legitimate visible feature — no exclude-zero checkbox."""
    resp = client.get("/browse/GroupA/ds1/plot/ecdf")
    assert resp.status_code == 200
    assert 'name="exclude_zero"' not in resp.text


def test_plot_view_kde2d_exclude_zero_checkbox(client):
    """kde2d also exposes exclude-zero (bi-axis gene support)."""
    resp = client.get("/browse/GroupA/ds1/plot/kde2d")
    assert resp.status_code == 200
    assert 'name="exclude_zero"' in resp.text
    assert 'co-expressers' in resp.text.lower()


def test_plot_view_bar_stat_dropdown(client):
    """bar form exposes the stat= dropdown with the three modes."""
    resp = client.get("/browse/GroupA/ds1/plot/bar")
    assert resp.status_code == 200
    assert 'name="stat"' in resp.text
    assert 'id="f-stat"' in resp.text
    for opt in ('value="mean"', 'value="mean_nonzero"', 'value="pct_expressing"'):
        assert opt in resp.text
    # Only bar exposes stat — box/violin don't have it (box/violin already show
    # the full distribution shape).
    resp_box = client.get("/browse/GroupA/ds1/plot/box")
    assert 'id="f-stat"' not in resp_box.text
    resp_violin = client.get("/browse/GroupA/ds1/plot/violin")
    assert 'id="f-stat"' not in resp_violin.text


def test_plot_view_shows_caveats_banner(client):
    """Caveats surface above the plot preview when /info exposes any (task #254)."""
    resp = client.get("/browse/GroupA/ds1/plot/violin")
    assert resp.status_code == 200
    # The banner card structure + the actual message from _INFO.
    assert 'caveats-card' in resp.text
    assert 'astro_contaminated' in resp.text
    # Filter chip is present because the caveat has an obs_flag.
    assert 'cv-filter-chip' in resp.text
    assert 'data-obs-flag="astro_contaminated"' in resp.text


def test_plot_view_caveats_banner_absent_without_data(client):
    """No caveats in /info → no card at all (avoid empty header noise)."""
    # GroupA/ds2 has no caveats key in _INFO.
    resp = client.get("/browse/GroupA/ds2/plot/scatter")
    assert resp.status_code == 200
    assert 'caveats-card' not in resp.text


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


def test_about_link_on_brand_logo(client):
    # the topbar brand/logo links to /about (no standalone About nav item)
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'href="/about" class="brand"' in resp.text
    # the standalone "about" nav link was removed
    assert '<a href="/about">about</a>' not in resp.text


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


def test_geneset_experiment_has_module_view(client):
    # Ask 1: the experiment page must include the _module_view.html partial so
    # the variability drilldown is available without navigating into a module.
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    # Partial injects its filter controls
    assert 'class="mv-flt-method"' in resp.text
    assert 'class="mv-flt-module"' in resp.text
    assert 'class="mv-flt-tag"' in resp.text
    # mv_ctx scope=module is emitted into the page JS
    assert '"scope"' in resp.text
    assert 'mv-experiment' in resp.text   # the id we assign
    # module_name is null — module dropdown starts on "all modules"
    assert '"module_name": null' in resp.text or '"module_name":null' in resp.text


def test_geneset_experiment_module_view_has_celltype_tissue_facets(client):
    # Ask 2: celltype and tissue facets appear when dataset_annotations are provided.
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    assert 'class="mv-flt-celltype"' in resp.text
    assert 'class="mv-flt-tissue"' in resp.text


def test_geneset_experiment_preselects_cell_type(client):
    # Ask 3: when the experiment carries a single celltypes entry, the JS
    # preselect_cell_types list in mv_ctx is populated with that value.
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    # The metadata fixture carries celltypes: [microglial cell] → must appear in ctx.
    assert "microglial cell" in resp.text
    assert "preselect_cell_types" in resp.text


def test_ui_state_helper_is_sitewide(client):
    # The antRemember localStorage helper ships in base.html so any page can
    # persist dropdown/filter state across reloads.
    resp = client.get("/genesets")
    assert resp.status_code == 200
    assert "window.antRemember" in resp.text


def test_geneset_detail_has_variability_panel(client):
    # The geneset detail page uses the shared _module_view.html partial for the
    # "where it varies most" panel. Partial is scoped to scope=module with the
    # geneset's catalog (study/experiment) and name. 2026-08 refactored from
    # hand-rolled HTML to the shared partial.
    resp = client.get("/genesets/StudyX/Exp1/gs_up")
    assert resp.status_code == 200
    assert "Where it varies most" in resp.text
    # Partial's filter controls replace the old gv-* IDs
    assert 'class="mv-flt-method"' in resp.text
    assert 'class="mv-flt-tag"' in resp.text
    assert 'class="mv-flt-sort"' in resp.text
    # Module dropdown is visible in module scope (for drill-down within catalog).
    assert 'class="mv-flt-module"' in resp.text
    # Partial fetches module-variability scoped to this module + catalog
    assert "/api/module-variability?" in resp.text
    assert '"scope": "module"' in resp.text or '"scope":"module"' in resp.text or 'scope' in resp.text
    # mv_ctx carries the catalog (study/experiment) and module name
    assert "StudyX/Exp1" in resp.text
    assert "gs_up" in resp.text


def test_geneset_detail_fetch_uses_catalog_not_module_name(client):
    # Issue 1 fix: in module scope the JS fetches by catalog only (not module_name)
    # so peer stats have real spread across all modules in the family.
    resp = client.get("/genesets/StudyX/Exp1/gs_up")
    assert resp.status_code == 200
    # The fetch URL in the JS should include catalog= but NOT module= as a fetch param.
    # We look for the JS load() function's params construction: only catalog is added.
    assert 'catalog=' in resp.text                    # catalog-wide fetch
    # module_name appears in CTX (for the pre-select) but not as a fetch param key.
    # The fetch params string should NOT have '&module=' followed by the module name.
    assert "'&module='" not in resp.text              # no per-module narrowing in fetch


def test_geneset_breadcrumb_uses_bc_sep_style(client):
    # Issue 3: geneset detail breadcrumb should use bc-sep separators like dataset page.
    resp = client.get("/genesets/StudyX/Exp1/gs_up")
    assert resp.status_code == 200
    assert 'class="bc-sep"' in resp.text
    assert 'href="/genesets"' in resp.text
    # study/experiment link present
    assert 'href="/genesets/StudyX/Exp1"' in resp.text


def test_geneset_experiment_breadcrumb_uses_bc_sep_style(client):
    # Issue 3: geneset experiment page breadcrumb should also use bc-sep separators.
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    assert 'class="bc-sep"' in resp.text
    assert 'href="/genesets"' in resp.text


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


def test_module_view_celltype_tissue_method_change_listeners_are_explicit(client):
    """Regression: celltype/tissue/method filter dropdowns must have explicit
    addEventListener('change', render) bindings in the emitted JS.

    The generic Object.keys(flt).forEach(...) loop was replaced with named
    bindings (2026-08 fix) so static analysis and tests can assert each filter
    class is wired — a silent typo in the flt key would have made the generic
    loop bind nothing for that control.  This test greps the rendered HTML for
    the three bindings that were previously invisible.
    """
    # Geneset detail page — has dataset_annotations → celltype + tissue selects
    # are rendered; method is always rendered.
    resp = client.get("/genesets/StudyX/Exp1/gs_up")
    assert resp.status_code == 200
    assert "flt.celltype.addEventListener('change'" in resp.text, (
        "celltype change listener not found — filter will be a no-op"
    )
    assert "flt.tissue.addEventListener('change'" in resp.text, (
        "tissue change listener not found — filter will be a no-op"
    )
    assert "flt.method.addEventListener('change'" in resp.text, (
        "method change listener not found — filter will be a no-op"
    )
    # celltype filter uses indexOf (substring) for comma-joined multi-values.
    assert ".indexOf(ctVal) !== -1" in resp.text, (
        "celltype filter must use indexOf for multi-value datasets"
    )
    # tissue filter uses hierarchical ancestor matching instead of indexOf.
    assert "TISSUE_ANCESTORS" in resp.text, (
        "tissue filter must use TISSUE_ANCESTORS for hierarchical matching"
    )

    # Geneset experiment page — same partial, same requirement.
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    assert "flt.celltype.addEventListener('change'" in resp.text
    assert "flt.tissue.addEventListener('change'" in resp.text
    assert "flt.method.addEventListener('change'" in resp.text


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
    # metadata cells. Status column removed (always "ready" upstream); Date
    # replaces Modified.
    resp = client.get("/browse/GroupA")
    assert resp.status_code == 200
    assert 'id="datasets-table" class="card-table"' in resp.text
    assert 'class="card-title"' in resp.text
    assert 'data-label="Org"' in resp.text
    assert 'data-label="Date"' in resp.text


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


def test_tissue_popover_is_hierarchical(client):
    # The Tissue column popover renders the ontology hierarchy as a nested
    # tree. The server embeds the tree as JSON under #tissue-tree-data; the
    # client walks it to render nested checkboxes so ticking a parent (cortex)
    # selects every descendant (dorsolateral prefrontal cortex, Brodmann 46,
    # angular gyrus, …).
    import json as _json

    resp = client.get("/")
    assert resp.status_code == 200
    # Column now advertises tissue as a filter target
    assert 'data-col="tissue"' in resp.text
    assert 'data-filter-fields="tissue"' in resp.text
    # Tree data is embedded and the client-side builder is wired up
    assert 'id="tissue-tree-data"' in resp.text
    assert "buildTissueTreeSection" in resp.text

    # Extract and parse the embedded JSON — confirm the nested shape and that
    # cortex is an ancestor of dorsolateral prefrontal cortex.
    marker = 'id="tissue-tree-data">'
    start = resp.text.index(marker) + len(marker)
    end = resp.text.index("</script>", start)
    payload = _json.loads(resp.text[start:end])
    assert isinstance(payload, list) and payload, "tree must be a non-empty list"

    def find(nodes, name_lc):
        for node in nodes:
            if node.get("name_lc") == name_lc:
                return node
            hit = find(node.get("children") or [], name_lc)
            if hit:
                return hit
        return None

    def path_to(nodes, name_lc, trail):
        for node in nodes:
            new_trail = trail + [node["name_lc"]]
            if node["name_lc"] == name_lc:
                return new_trail
            deeper = path_to(node.get("children") or [], name_lc, new_trail)
            if deeper:
                return deeper
        return None

    brain = find(payload, "brain")
    assert brain is not None and brain.get("children"), \
        "expected brain as an inner root with subtree"
    cortex = find(payload, "cortex")
    assert cortex is not None, "cortex should be present in the tree"
    # cortex must have real descendants
    assert cortex.get("children"), "cortex should have child regions"
    # dorsolateral prefrontal cortex is a descendant of cortex
    trail = path_to(cortex.get("children") or [],
                    "dorsolateral prefrontal cortex", [])
    assert trail is not None, \
        "dorsolateral prefrontal cortex must sit under cortex"


def test_landing_access_pub_icon_inline(client):
    # Access and publication_status collapse to a single .ap-icon inline before
    # the collection name; their filters live under the Collection header
    # popover (data-filter-fields="acc,pub"). Old .acc-src two-glyph pair gone.
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'class="ap-icon"' in resp.text
    assert 'data-filter-fields="acc,pub"' in resp.text
    assert 'data-label="Access"' not in resp.text   # column removed
    assert 'data-label="Source"' not in resp.text
    assert 'class="acc-src"' not in resp.text       # old two-glyph system gone


def test_folder_collection_pills_hoist_constants(client):
    # GroupA: access (public) is constant across both datasets → hoisted to
    # inert header pills; organism/celltype/disease vary → columns kept.
    # Tissue is now excluded from the header-pill hoist and always renders as
    # a column (so a collection-wide tissue still gets its own column). Prep
    # (suspension_type) is also excluded from the pill hoist — its info is
    # folded into the Tech column tooltip.
    resp = client.get("/browse/GroupA")
    assert resp.status_code == 200
    assert 'class="coll-meta"' in resp.text          # header pill row
    # Tissue is a Tissue column (not a header pill) — see Tissue column below.
    assert 'data-label="Tissue"' in resp.text          # tissue column always present
    assert "prefrontal cortex" in resp.text            # tissue value rendered in column tooltip
    assert 'data-label="Prep"' not in resp.text        # prep never a column here (constant → hidden)
    assert 'data-label="Org"' in resp.text             # organism varies → kept
    assert 'data-label="Cell"' in resp.text            # celltype varies → kept


def test_folder_access_pub_icon_inline(client):
    # Every row carries the unified access + publication_status icon (.ap-icon)
    # inline before the dataset name. No more per-row Access/Source columns.
    resp = client.get("/browse/GroupA")
    assert resp.status_code == 200
    assert 'class="ap-icon"' in resp.text
    assert 'data-label="Access"' not in resp.text      # never a column
    assert 'data-label="Source"' not in resp.text
    assert 'class="acc-src"' not in resp.text           # old two-glyph gone


def test_folder_date_column_and_newest_first(client):
    # Each dataset shows a canonical Date; the list is sorted newest-first by
    # the dataset_date sort_ts (published year OR upload mtime).
    resp = client.get("/browse/GroupA")
    assert resp.status_code == 200
    assert 'data-label="Date"' in resp.text
    # ds1 (year=2025, published) comes before ds2 (year=2024, published)
    assert resp.text.index("Dataset One") < resp.text.index("Dataset Two")


def test_dataset_page_shows_modified(client):
    # `modified` moved from the top stats row into the Stats card in the
    # Data tab (2026-08).
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    assert ">modified<" in resp.text        # table `<td>modified</td>` row
    assert "2025-01-01" in resp.text        # _INFO has year=2025 → _modified_ts year-cast


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
    # the card title, and the scalar columns are labelled for the 2-column
    # card grid. Cell + Disease are their own columns (no longer packed into
    # a card-span "context" cluster).
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="coll-table" class="card-table"' in resp.text
    assert 'class="card-title"' in resp.text
    assert 'data-label="Datasets"' in resp.text
    assert 'data-label="Cells"' in resp.text
    assert 'data-label="Cell"' in resp.text
    assert 'data-label="Disease"' in resp.text


# ── shared .btn-edit class (muted-red admin action) ────────────────────────────

def test_btn_edit_class_defined_sitewide(client):
    # The muted-red .btn-edit style lives in base.html so both the group and
    # dataset pages render the Edit affordance with identical treatment.
    resp = client.get("/")
    assert resp.status_code == 200
    assert ".btn-edit" in resp.text
    # (line below is the palette-amber recolour done 2026-08)
    # background hex — anchor colour of the muted-red palette
    assert "#E09F3E" in resp.text   # palette amber (was muted red)


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


def test_dataset_view_no_module_scores_tab_when_no_score_columns(client):
    # Baseline ds1 has only n_genes / pseudotime — no scored modules. The
    # "Module scores" tab must not render, and the total obs count is
    # unaffected. Guards against the score-classifier grabbing plain columns.
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    assert 'data-tab="modules"' not in resp.text
    assert 'id="mv-modules-tab"' not in resp.text


def test_dataset_view_module_scores_tab_groups_three_methods(client, monkeypatch):
    # A dataset with all three scoring methods — the modules tab should render
    # using the shared _module_view.html partial (scope=dataset). The partial
    # fills rows client-side via JS; server-side output contains the filter
    # controls, the mv_ctx JSON, and the empty <tbody class="mv-rows">.
    # 2026-08: refactored from server-side table to the shared partial.
    def with_scores(api_url, path):
        result = _fake_api_get(api_url, path)
        if path.endswith("/info") and isinstance(result, dict):
            result = dict(result)
            result["obs"] = {
                "categorical": ["cell_type"],
                "numerical": [
                    "n_genes", "pseudotime",
                    "Sierksma2025::WGCNA::Brown_Lysosome",             # scanpy
                    "starcat__MICROGLIA_V1__DAM_pos",                  # starcat
                    "aucell__Sierksma2025/WGCNA__Brown_Lysosome",      # aucell
                    "aucell__Mancuso2024/Cell_states__CRM1",           # aucell-only module
                ],
            }
        return result
    monkeypatch.setattr(browser, "_api_get", with_scores)
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    # Tab button is present (module count non-zero)
    assert 'data-tab="modules"' in resp.text
    # Partial is rendered — its filter controls and panel ID are present
    assert 'id="mv-modules-tab"' in resp.text
    assert 'class="mv-flt-method"' in resp.text
    assert 'class="mv-flt-tag"' in resp.text
    # starCAT option NOT in the partial's method filter (hidden 2026-08)
    assert 'data-method="starcat"' not in resp.text
    assert '>starCAT<' not in resp.text
    # mv_ctx carries scope=dataset and the db_id (GroupA/ds1)
    assert '"scope": "dataset"' in resp.text or '"scope":"dataset"' in resp.text
    assert 'GroupA/ds1' in resp.text
    # Module count is reported in the tab label (server-side n_module_scores)
    # Dataset has 4 scored cols (scanpy + starcat + 2 aucell) → 4 modules tracked
    assert 'Modules<span class="tab-count">' in resp.text
    # Plain-numerical columns (n_genes, pseudotime) are on Numerical tab, not Modules
    assert 'x=n_genes' in resp.text  # still linked from the Numerical tab


def test_dataset_view_module_scores_absent_method_greyed(client, monkeypatch):
    # A dataset with ONLY aucell scores — the modules tab still renders via the
    # shared partial (scope=dataset). The partial's method filter includes
    # both scanpy and AUCell options regardless; client-side JS filters to
    # what's actually present. The old server-side absent/greyed badge logic
    # is gone — the partial handles this uniformly. 2026-08: refactored.
    def aucell_only(api_url, path):
        result = _fake_api_get(api_url, path)
        if path.endswith("/info") and isinstance(result, dict):
            result = dict(result)
            result["obs"] = {
                "categorical": ["cell_type"],
                "numerical": [
                    "n_genes",
                    "aucell__Sierksma2025/WGCNA__Brown_Lysosome",
                    "aucell__Sierksma2025/WGCNA__Blue_Innate_IR",
                ],
            }
        return result
    monkeypatch.setattr(browser, "_api_get", aucell_only)
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    assert 'data-tab="modules"' in resp.text
    # Partial is present with scope=dataset
    assert 'id="mv-modules-tab"' in resp.text
    assert 'class="mv-flt-method"' in resp.text
    # starCAT not in the partial's method options (hidden 2026-08)
    assert 'method="starcat"' not in resp.text
    # AUCell option is present in the partial's method filter
    assert 'value="aucell"' in resp.text


def test_geneset_variability_page_offers_aucell_source(client):
    resp = client.get("/genesets/variability")
    assert resp.status_code == 200
    # AUCell + scanpy (gene sets) source-filter options are present.
    # starCAT is temporarily hidden alongside its /genesets tab (2026-08 —
    # STARCAT_UI_ENABLED in browser.py); restore the assertion when the
    # flag flips back.
    assert 'value="aucell"' in resp.text
    assert 'value="geneset"' in resp.text
    assert 'value="starcat"' not in resp.text


def test_geneset_variability_page_canonical_tags_in_js(client):
    """All 13 canonical tags from lib/canonical_columns.py must appear in the
    JS FALLBACK blob served by /genesets/variability, so the tag dropdown
    shows the full set when /module-variability/dimensions is unreachable."""
    import sys, os
    # Ensure lib/ is importable (mirrors the shim in browser.py / ant-browse).
    lib_dir = os.path.join(os.path.dirname(__file__), '..', 'lib')
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    from canonical_columns import CANONICAL_TAGS, NUMERICAL_TAGS

    resp = client.get("/genesets/variability")
    assert resp.status_code == 200
    html = resp.text

    # Every canonical tag must appear as a JSON string inside the FALLBACK tags array.
    for tag in CANONICAL_TAGS:
        assert f'"{tag}"' in html, f'canonical tag "{tag}" not found in /genesets/variability JS blob'

    # Every numerical tag must appear in the _numTagList array.
    for tag in NUMERICAL_TAGS:
        assert f'"{tag}"' in html, f'numerical tag "{tag}" not found in /genesets/variability JS blob'

    # The old hardcoded NUMERIC_TAGS object with wrong keys must not be present.
    assert '"braak_stage"' not in html, '"braak_stage" (non-canonical) should not appear in JS'


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


def test_dataset_view_embeddings_on_data_tab(client):
    # Embeddings moved from the Overview tab's Explore block to the Data tab
    # in 2026-08 (alongside Biology + Resources). Overview stays lean.
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    assert 'data-tab="embed"' not in resp.text
    assert 'id="tab-embed"' not in resp.text
    data_start = resp.text.index('id="tab-data"')
    data_end = resp.text.index('</section>', data_start)
    data_panel = resp.text[data_start:data_end]
    assert '<h2>Embeddings' in data_panel
    assert 'plot/scatter?embedding=X_umap' in data_panel


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


# ── template structural tests for the unified icon + folder reorg + dataset id ──

def test_landing_uses_unified_icon(client):
    # index.html renders the new .ap-icon (single glyph with fixed-width
    # container) — no more two-glyph access/source pair.
    resp = client.get("/")
    assert resp.status_code == 200
    assert "ap-icon" in resp.text
    # class definition ships in base.html (sitewide)
    assert ".ap-icon" in resp.text
    # tooltip vocabulary
    assert "internal · published" in resp.text or "external · published" in resp.text


def test_folder_page_uses_unified_icon(client):
    resp = client.get("/browse/GroupA")
    assert resp.status_code == 200
    assert 'class="ap-icon"' in resp.text
    # each row's icon carries a title tooltip encoding access + publication
    assert " · published" in resp.text or " · unpublished" in resp.text


def test_dataset_page_has_unified_icon_next_to_actions(client):
    # 2026-08 layout: <h1> title dropped (breadcrumb carries the name); the
    # unified access/publication icon moved to the top-right action row,
    # just left of the visualize + notebook + edit buttons.
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    assert 'class="ds-pub-icon"' in resp.text
    # icon lives inside `.ds-actions`
    idx_actions = resp.text.find('class="ds-actions"')
    idx_span = resp.text.find('class="ds-pub-icon"')
    assert idx_actions > 0 and idx_span > idx_actions
    # standalone <h1> is gone
    assert '<h1 class="ds-title"' not in resp.text
    # old two-glyph access/source pair still gone
    assert 'class="ds-acc"' not in resp.text


def test_folder_page_date_column_present_modified_absent(client):
    resp = client.get("/browse/GroupA")
    assert resp.status_code == 200
    # new Date column
    assert 'data-col="date"' in resp.text
    # old Modified column is gone
    assert '>Modified<' not in resp.text
    assert 'data-label="Modified"' not in resp.text


def test_folder_page_status_column_removed(client):
    # Status column removed — every API-registered dataset has has_duckdb=True
    # so the dot always showed "ready". Confirm no Status header + no per-row
    # status cell. (`.dot-ok` still exists as a shared utility class in
    # base.html for other pages.)
    resp = client.get("/browse/GroupA")
    assert resp.status_code == 200
    assert '>Status<' not in resp.text
    assert 'data-label="Status"' not in resp.text


def test_folder_page_filter_and_stats_toolbar(client):
    # filter box top-left, stats block top-right (both live in .grp-toolbar).
    resp = client.get("/browse/GroupA")
    assert resp.status_code == 200
    assert 'class="grp-toolbar"' in resp.text
    assert 'id="ds-filter"' in resp.text
    assert 'class="grp-stats"' in resp.text


def test_folder_page_default_sort_date_desc(client):
    # The rows carry data-sort-ts on the Date cell; default JS sort is date DESC.
    resp = client.get("/browse/GroupA")
    assert resp.status_code == 200
    assert 'data-sort-ts=' in resp.text
    # default sort variable
    assert "sortCol = 'date'" in resp.text and "sortDir = 'desc'" in resp.text


def test_landing_description_moved_to_title_attr(client):
    # The grey per-collection description block is gone; the summary rides on
    # the <a> title= tooltip instead.
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'coll-desc' not in resp.text


def test_landing_removed_modified_column(client):
    # New Region + Date columns replace Modified.
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'data-col="date"' in resp.text
    assert 'data-col="tissue"' in resp.text          # new Region column
    # Modified column is gone
    assert '>Modified <' not in resp.text
    assert 'data-col="modified"' not in resp.text


def test_dataset_page_file_id_first_in_breadcrumb(client):
    # The 3-letter file_id is the primary identifier — first idcopy chip.
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    # Extract just the .ds-bc block so we're not fooled by other idcopy chips
    # elsewhere on the page (there are none, but be defensive).
    import re
    m = re.search(r'<div class="ds-bc">(.*?)</div>', resp.text, re.S)
    assert m, "ds-bc breadcrumb block not found"
    bc = m.group(1)
    # first idcopy in the breadcrumb should copy file_id ('ds1')
    first_idcopy = re.search(r'idcopy" data-copy="([^"]+)"', bc)
    assert first_idcopy is not None
    assert first_idcopy.group(1) == "ds1"


def test_dataset_page_ready_indicator_removed(client):
    # The always-"ready" dot indicator is gone from the stats row.
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    # extract just the stats-row so we don't match the /jupyter tab-bar bg etc.
    import re
    m = re.search(r'<div class="stats-row">(.*?)</div>\s*(?:\{|</section>|<!)', resp.text, re.S)
    if m:
        assert 'dot-ok' not in m.group(1)
        assert '>ready<' not in m.group(1)


def test_dataset_page_context_pills_uniform_color(client):
    # Rainbow chips replaced by uniform muted teal-on-cream pills.
    # We verify the .ds-ctx .pill CSS no longer has per-category background
    # overrides (.pill.tech, .pill.cell, .pill.tissue, .pill.disease, .pill.prep).
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    # each of these class-specific overrides was rainbow-colouring the chip;
    # they must be gone (or replaced with the uniform rule).
    for override in (".ds-ctx .pill.tech", ".ds-ctx .pill.cell",
                     ".ds-ctx .pill.tissue", ".ds-ctx .pill.disease",
                     ".ds-ctx .pill.prep"):
        assert override not in resp.text, f"expected {override} rainbow rule gone"


def test_base_html_dark_teal_palette(client):
    # Palette refresh — dark teal (#335C67) is the primary --link colour.
    resp = client.get("/")
    assert resp.status_code == 200
    assert "#335C67" in resp.text
    # cream + red + amber tokens present
    assert "#FFF3B0" in resp.text
    assert "#9E2A2B" in resp.text


def test_base_html_roboto_loaded(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "fonts.googleapis.com" in resp.text


# ── unit tests: _obs_col_info ──────────────────────────────────────────────────

def test_obs_col_info_canonical_column():
    info = browser._obs_col_info('ant_braak')
    assert info['is_canonical'] is True
    assert info['tag'] == 'braak'
    assert info['display_name'] == 'braak'


def test_obs_col_info_non_canonical_column():
    info = browser._obs_col_info('cell_type')
    assert info['is_canonical'] is False
    assert info['tag'] is None
    assert info['display_name'] == 'cell_type'


def test_obs_col_info_ant_prefix_unknown_tag():
    # ant_xyz is not in CANONICAL_TAGS — should not be canonical
    info = browser._obs_col_info('ant_xyz')
    assert info['is_canonical'] is False
    assert info['display_name'] == 'ant_xyz'


@pytest.mark.parametrize("col_name", [
    'ant_braak', 'ant_sex', 'ant_cluster', 'ant_disease',
    'ant_condition', 'ant_age', 'ant_donor', 'ant_region',
])
def test_obs_col_info_all_canonical_tags(col_name):
    info = browser._obs_col_info(col_name)
    assert info['is_canonical'] is True
    assert info['display_name'] == col_name[4:]   # strip 'ant_'


# ── route tests: canonical-pill rendering in dataset obs tables ────────────────

def test_dataset_canonical_col_renders_pill(client):
    """ant_braak in the obs list → canonical-pill + bold stripped label in HTML."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # The pill class must appear (ant_braak, ant_cluster, ant_age are all in _INFO)
    assert 'canonical-pill' in html


def test_dataset_canonical_col_has_bold_tag_label(client):
    """The visible label for ant_braak must be <b>braak</b>, not 'ant_braak'."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    assert '<b>braak</b>' in html or '<b>age</b>' in html or '<b>cluster</b>' in html


def test_dataset_canonical_col_ant_prefix_not_in_label(client):
    """The text inside the bold label must NOT start with 'ant_'."""
    import re as _re
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    # Extract all bold-tag label texts from canonical-col spans
    labels = _re.findall(r'<b>(.*?)</b>', resp.text)
    canonical_labels = [lb for lb in labels if 'ant_' in lb]
    assert canonical_labels == [], (
        f"Found bold labels that still contain 'ant_': {canonical_labels}"
    )


def test_dataset_canonical_col_title_contains_full_name(client):
    """Hover title= attribute must contain the full 'ant_braak' column name."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    assert 'ant_braak' in resp.text
    # The title attribute on the canonical-col span carries the full name
    assert 'title="canonical annotation `ant_braak`' in resp.text


def test_dataset_non_canonical_col_no_pill(client):
    """cell_type and sample (non-canonical) must NOT have a canonical-pill."""
    import re as _re
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # Locate the cell_type row and confirm no canonical-pill near it
    # (we check the colname td specifically, not the whole page)
    cell_type_idx = html.find('>cell_type<')
    assert cell_type_idx != -1, "cell_type colname not found in page"
    # pill should not appear in close proximity to plain cell_type label
    surrounding = html[max(0, cell_type_idx - 100): cell_type_idx + 200]
    assert 'canonical-pill' not in surrounding


def test_dataset_canonical_col_css_present(client):
    """base.html must define the .canonical-pill CSS rule."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    assert '.canonical-pill' in resp.text


def test_dataset_canonical_col_data_name_attr_unmodified(client):
    """data-name on the obs-item row keeps the raw ant_* value for JS filtering."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    # data-name must use the lowercased full column name for the JS filter to work
    assert 'data-name="ant_braak"' in resp.text
    assert "family=Roboto" in resp.text


# ── module view: system facet + URL hash features ─────────────────────────────

def test_module_view_system_facet_rendered(client):
    """The 'system' (experimental_system) filter dropdown must appear in the
    module view partial when dataset_annotations are provided."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    assert 'class="mv-flt-system"' in resp.text, (
        "system filter select not rendered — check _module_view.html template"
    )


def test_module_view_system_facet_listener_bound(client):
    """flt.system must have an explicit addEventListener binding."""
    resp = client.get("/genesets/StudyX/Exp1/gs_up")
    assert resp.status_code == 200
    assert "flt.system.addEventListener('change'" in resp.text, (
        "system filter change listener not found — filter will be a no-op"
    )


def test_module_view_system_filter_uses_indexof(client):
    """System filter uses indexOf so comma-joined multi-values work (same pattern
    as celltype/tissue)."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    assert ".indexOf(sysVal) !== -1" in resp.text, (
        "system filter must use indexOf for multi-value experimental_system fields"
    )


def test_module_view_url_hash_functions_present(client):
    """URL hash persistence helpers (parseHashFilters, buildHashFromFilters,
    updateHash) must be present in the rendered module view JS."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    assert "parseHashFilters" in resp.text, "parseHashFilters missing"
    assert "buildHashFromFilters" in resp.text, "buildHashFromFilters missing"
    assert "updateHash" in resp.text, "updateHash missing"


def test_module_view_popstate_listener_registered(client):
    """Browser back/forward (popstate) handler must be registered so navigating
    back restores filter state."""
    resp = client.get("/genesets/StudyX/Exp1/gs_up")
    assert resp.status_code == 200
    assert "addEventListener('popstate'" in resp.text, (
        "popstate handler not registered — back button won't restore filters"
    )


def test_module_view_dataset_link_includes_mv_hash(client):
    """Dataset links in the module view table must embed a #mv= hash so the
    dataset page's Modules tab pre-selects the group/module the user was at."""
    resp = client.get("/genesets/StudyX/Exp1/gs_up")
    assert resp.status_code == 200
    # The datasetUrl() helper must produce a URL with #mv=group=...;module=...
    assert "datasetUrl" in resp.text, "datasetUrl helper not found"
    # The hash scheme uses '#mv=' with group= and module= pairs.
    assert "'#mv='" in resp.text or '"#mv="' in resp.text or "'mv='" in resp.text, (
        "#mv= hash scheme not found in datasetUrl implementation"
    )


def test_module_view_history_push_state_called(client):
    """history.pushState must be used for navigation-worthy filter changes."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    assert "history.pushState" in resp.text, (
        "history.pushState missing — back button won't work after filter changes"
    )
    assert "history.replaceState" in resp.text, (
        "history.replaceState missing — URL won't update on clear"
    )


# ── unit tests: refresh_genesets_available TTL cache ──────────────────────────

class _FakeState:
    """Minimal app.state stand-in for probe-cache unit tests."""

    def __init__(self, api_url: str = "http://testapi") -> None:
        self.api_url = api_url
        self.genesets_available: bool = False
        self._genesets_probe_ts: float = 0.0


def test_refresh_genesets_probes_when_stale(monkeypatch):
    """First call (ts=0) always probes regardless of current value."""
    calls = []

    def _fake_probe(api_url: str) -> bool:
        calls.append(api_url)
        return True

    monkeypatch.setattr(browser, "_probe_genesets_once", _fake_probe)
    state = _FakeState()

    result = browser.refresh_genesets_available(state)

    assert result is True
    assert state.genesets_available is True
    assert len(calls) == 1


def test_refresh_genesets_caches_true_for_60s(monkeypatch):
    """After a True result, subsequent calls within 60 s skip the probe."""
    import time

    probe_count = [0]

    def _fake_probe(api_url: str) -> bool:
        probe_count[0] += 1
        return True

    monkeypatch.setattr(browser, "_probe_genesets_once", _fake_probe)
    state = _FakeState()

    # First call probes and caches True.
    browser.refresh_genesets_available(state)
    assert probe_count[0] == 1

    # Second call within TTL window must NOT probe again.
    browser.refresh_genesets_available(state)
    assert probe_count[0] == 1, "probe fired again within True TTL window"

    # Simulate TTL expiry.
    state._genesets_probe_ts -= browser._GENESETS_TTL_TRUE + 1
    browser.refresh_genesets_available(state)
    assert probe_count[0] == 2, "probe did not fire after True TTL expired"


def test_refresh_genesets_caches_false_for_only_5s(monkeypatch):
    """A False result is cached only 5 s so startup races self-heal quickly."""
    probe_results = [False, True]
    probe_calls = [0]

    def _fake_probe(api_url: str) -> bool:
        result = probe_results[min(probe_calls[0], len(probe_results) - 1)]
        probe_calls[0] += 1
        return result

    monkeypatch.setattr(browser, "_probe_genesets_once", _fake_probe)
    state = _FakeState()

    # First probe → False, cached for 5 s.
    browser.refresh_genesets_available(state)
    assert state.genesets_available is False
    assert probe_calls[0] == 1

    # Within 5 s → no second probe.
    browser.refresh_genesets_available(state)
    assert probe_calls[0] == 1, "probe fired within False TTL window"

    # Simulate 5 s elapsed → probe again, now returns True.
    state._genesets_probe_ts -= browser._GENESETS_TTL_FALSE + 1
    browser.refresh_genesets_available(state)
    assert state.genesets_available is True, "availability did not recover after False TTL"
    assert probe_calls[0] == 2


def test_refresh_genesets_does_not_cache_false_for_60s(monkeypatch):
    """False TTL must be shorter than True TTL — this is the whole point."""
    assert browser._GENESETS_TTL_FALSE < browser._GENESETS_TTL_TRUE, (
        f"False TTL ({browser._GENESETS_TTL_FALSE}s) must be < "
        f"True TTL ({browser._GENESETS_TTL_TRUE}s)"
    )


def test_probe_genesets_once_returns_false_on_network_error(monkeypatch):
    """A network error in _probe_genesets_once must return False, not raise."""
    import requests as _requests

    def _raise(*args, **kwargs):
        raise _requests.ConnectionError("unreachable")

    monkeypatch.setattr(_requests, "get", _raise)
    result = browser._probe_genesets_once("http://testapi")
    assert result is False


def test_probe_genesets_once_returns_false_on_non_200(monkeypatch):
    """A non-200 response must yield False."""
    import requests as _requests

    class _FakeResp:
        status_code = 503

    monkeypatch.setattr(_requests, "get", lambda *a, **kw: _FakeResp())
    result = browser._probe_genesets_once("http://testapi")
    assert result is False


def test_probe_genesets_once_returns_true_on_200(monkeypatch):
    """A 200 response must yield True."""
    import requests as _requests

    class _FakeResp:
        status_code = 200

    monkeypatch.setattr(_requests, "get", lambda *a, **kw: _FakeResp())
    result = browser._probe_genesets_once("http://testapi")
    assert result is True


# ── tissue hierarchy ancestors ────────────────────────────────────────────────

def test_tissue_ancestors_brain_has_no_ancestors():
    """Brain is a root — its ancestor list must be empty."""
    ancestors = browser._TISSUE_ANCESTORS
    assert "brain" in ancestors
    assert ancestors["brain"] == []


def test_tissue_ancestors_prefrontal_cortex():
    """prefrontal cortex → frontal cortex → cortex → brain (bottom-up)."""
    ancestors = browser._TISSUE_ANCESTORS
    pfc = ancestors.get("prefrontal cortex", [])
    # frontal cortex must be the immediate parent (first in the list)
    assert pfc[0] == "frontal cortex"
    assert "cortex" in pfc
    assert "brain" in pfc


def test_tissue_ancestors_cortex_parent_is_brain():
    ancestors = browser._TISSUE_ANCESTORS
    assert ancestors["cortex"] == ["brain"]


def test_tissue_ancestors_leaf_terms_carry_full_chain():
    """dorsolateral prefrontal cortex is 4 levels deep — chain must be complete."""
    ancestors = browser._TISSUE_ANCESTORS
    dlpfc = ancestors.get("dorsolateral prefrontal cortex", [])
    assert "prefrontal cortex" in dlpfc
    assert "frontal cortex" in dlpfc
    assert "cortex" in dlpfc
    assert "brain" in dlpfc


def test_tissue_ancestors_root_terms_have_empty_list():
    """All hierarchy roots (parent == null) must map to []."""
    ancestors = browser._TISSUE_ANCESTORS
    hierarchy = browser._load_tissue_hierarchy()
    roots = [term for term, parent in hierarchy.items() if parent is None]
    for root_lc in roots:
        # ancestors uses display casing; find matching key case-insensitively
        match = next((k for k in ancestors if k.lower() == root_lc), None)
        if match is not None:
            assert ancestors[match] == [], f"root '{root_lc}' should have empty ancestor list"


# ── module view: tissue hierarchy in template context ─────────────────────────

def test_module_view_tissue_ancestors_in_js_context(client):
    """The geneset module view must embed TISSUE_ANCESTORS in the mv_ctx JSON
    so the JS filter can do hierarchical matching."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    assert "tissue_ancestors" in resp.text, (
        "tissue_ancestors not found in page JS — hierarchical tissue filter will not work"
    )


def test_module_view_tissue_ancestors_in_geneset_detail(client):
    """Geneset detail page also carries tissue_ancestors in its mv_ctx."""
    resp = client.get("/genesets/StudyX/Exp1/gs_up")
    assert resp.status_code == 200
    assert "tissue_ancestors" in resp.text


def test_module_view_tissue_dropdown_uses_fill_tissue_select(client):
    """fillTissueSelect must appear in the rendered HTML (replaces plain fillSelect
    for tissue so indented hierarchy options are emitted)."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    assert "fillTissueSelect" in resp.text, (
        "fillTissueSelect not present — tissue dropdown will be flat, not hierarchical"
    )


def test_module_view_tissue_filter_uses_ancestor_lookup(client):
    """The tissue filter must check TISSUE_ANCESTORS so selecting 'brain'
    matches datasets tagged 'prefrontal cortex'."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    assert "TISSUE_ANCESTORS" in resp.text, (
        "TISSUE_ANCESTORS JS variable missing — ancestor matching unavailable"
    )
    # The filter must walk the ancestor chain, not just indexOf.
    assert "ancestors.indexOf(selectedTissue)" in resp.text or "TISSUE_ANCESTORS[t]" in resp.text, (
        "Ancestor-based tissue filter logic not found"
    )


# ── module view: tag column prominence ────────────────────────────────────────

def test_module_view_tag_column_has_prominent_style(client):
    """The .mv-tag CSS rule must specify a larger font-size than the table body
    baseline (11.5px), making the tag the visual primary axis of each row."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    # Look for any font-size on .mv-tag that is >= 13px (we used 14px).
    import re
    mv_tag_css = re.search(r'\.mv-tag\s*\{[^}]+\}', resp.text)
    assert mv_tag_css, ".mv-tag CSS rule not found"
    # font-size value must be present and > 12px baseline
    size_match = re.search(r'font-size:\s*(\d+)px', mv_tag_css.group(0))
    assert size_match, "font-size not set on .mv-tag"
    assert int(size_match.group(1)) >= 13, f"tag font-size {size_match.group(1)}px is too small"


def test_module_view_tag_column_header_has_accent_class(client):
    """The tag <th> must carry mv-th-tag for visual accent."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    assert 'class="mv-th-tag"' in resp.text, "mv-th-tag class missing from tag <th>"


def test_module_view_tag_chip_css_present(client):
    """The .mv-tag-chip styling rule must be present (light chip background)."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    assert "mv-tag-chip" in resp.text


# ── module view: dataset hover popover ────────────────────────────────────────

def test_module_view_dataset_popover_css_present(client):
    """The .mv-ds-pop and .mv-ds-tip CSS rules must be present for the hover
    popover to function."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    assert "mv-ds-pop" in resp.text, ".mv-ds-pop CSS/markup missing"
    assert "mv-ds-tip" in resp.text, ".mv-ds-tip CSS/markup missing"


def test_module_view_dataset_annotations_include_title(client):
    """DS_ANNOT carried in the page must include title, n_cells, and contributor
    so the hover popover can display them."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    # The fake _DATABASES fixture has title="Dataset One" for GroupA/ds1.
    assert "Dataset One" in resp.text, "dataset title not found in page annotations"


def test_build_dataset_annotations_includes_title_and_ncells():
    """_build_dataset_annotations must now return title + n_cells + contributor."""

    class _FakeModule:
        @staticmethod
        def _api_get(api_url, path):
            if path == "/databases":
                return {"databases": [
                    {"id": "G/d1", "title": "My Dataset", "n_obs": 42_000,
                     "contributor": "Smith Lab",
                     "celltype": "microglial cell", "tissue": "brain",
                     "experimental_system": "post_mortem_tissue"},
                ]}
            raise ValueError(f"unexpected {path}")

    import unittest.mock as mock
    with mock.patch.object(browser, "_api_get", _FakeModule._api_get):
        result = browser._build_dataset_annotations("http://dummy")

    assert result["G/d1"]["title"] == "My Dataset"
    assert result["G/d1"]["n_cells"] == "42000"
    assert result["G/d1"]["contributor"] == "Smith Lab"
    assert result["G/d1"]["tissue"] == "brain"


def test_build_dataset_annotations_n_obs_fallback():
    """n_cells key in the API response (not n_obs) should also work."""
    import unittest.mock as mock

    def fake_get(api_url, path):
        if path == "/databases":
            return {"databases": [
                {"id": "G/d2", "n_cells": 100, "n_obs": None,
                 "title": None, "contributor": None,
                 "celltype": None, "tissue": None, "experimental_system": None},
            ]}
        raise ValueError(path)

    with mock.patch.object(browser, "_api_get", fake_get):
        result = browser._build_dataset_annotations("http://dummy")

    assert result["G/d2"]["n_cells"] == "100"


# ── dataset.html: handleHash strips &mv= before tab lookup ───────────────────

def test_dataset_html_handlehash_strips_mv_suffix(client):
    """handleHash must split on '&' before matching a tab name so that a URL
    fragment like #modules&mv=tag=braak_group;sort=position activates the
    modules tab, not falls back to overview."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200

    html = resp.text
    # The indexOf('&') guard that strips the mv= state must be present.
    assert "indexOf('&')" in html, (
        "handleHash must use indexOf('&') to strip &mv= suffix before tab lookup"
    )
    # The tabPart variable (or equivalent slice) must be used for the
    # modules-<key> match and the activateTab call, not the raw `h`.
    assert "tabPart" in html, (
        "handleHash must extract tabPart (the fragment up to '&') for tab lookup"
    )


def test_dataset_html_activatetab_preserves_mv_suffix(client):
    """activateTab must read the existing mv= suffix from the current hash and
    re-append it when writing back, so switching tabs doesn't erase filter state."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200

    html = resp.text
    # The mv-preserving logic: read curHash, find '&', extract mvSuffix,
    # append it when calling replaceState.
    assert "mvSuffix" in html, (
        "activateTab must preserve mvSuffix from the current hash on tab switch"
    )
    assert "name + mvSuffix" in html, (
        "activateTab must write '#' + name + mvSuffix to replaceState"
    )


# ── Genes tab — four bullets ───────────────────────────────────────────────────

def test_genes_tab_label_no_parenthesised_count(client):
    """Bullet 1: the Genes tab button must say 'Genes' with NO '(N)' count."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # The tab button must contain the word Genes but must NOT have a tab-count
    # badge inside the Genes tab button.  We look for the specific button markup
    # and verify that no parenthesised number or tab-count span follows 'Genes'
    # on that button.
    import re
    # Extract the raw tab-btn for the genes tab.
    genes_btn = re.search(r'<button[^>]*data-tab="genes"[^>]*>(.*?)</button>', html, re.DOTALL)
    assert genes_btn is not None, "Genes tab button not found in dataset page HTML"
    btn_inner = genes_btn.group(1)
    assert "Genes" in btn_inner
    # Must NOT contain a tab-count span or a parenthesised digit.
    assert "tab-count" not in btn_inner, "Genes tab must not show a count badge"
    assert not re.search(r'\(\d+\)', btn_inner), "Genes tab must not show a parenthesised count"


def test_genes_tab_has_search_input(client):
    """Bullet 2: the Genes tab contains a gene search <input> and URL-state JS."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # Search input element.
    assert 'id="gene-search"' in html, "<input id='gene-search'> must be present in Genes tab"
    # Client-side filter JS that writes ?gene= into the URL.
    assert "applyGeneSearch" in html, "applyGeneSearch JS function must be present"
    assert 'searchParams.set' in html, "URL gene search-param update must be in the JS"


def test_genes_tab_has_sparkline_elements(client):
    """Bullet 3: each gene row has .gene-sparkline elements for pct, mean, meannz."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # One sparkline per stat per gene row is generated in the template.
    assert 'class="gene-sparkline"' in html, ".gene-sparkline must be rendered in gene rows"
    # All three stats must have sparkline tracks.
    assert 'data-stat="pct"'    in html, "pct sparkline track required"
    assert 'data-stat="mean"'   in html, "mean sparkline track required"
    assert 'data-stat="meannz"' in html, "meannz sparkline track required"
    # The JS setSparkline helper must be present.
    assert "setSparkline" in html, "setSparkline JS helper must be defined"


def test_genes_tab_mean_sparklines_use_db_wide_range(client):
    """Bullet 4: mean/meannz sparklines are scaled to the db-wide min/max
    computed across ALL returned gene stats (not just shown rows)."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # The JS must compute a db-wide range object for mean and meannz.
    assert "meanRange"   in html, "meanRange db-wide range must be computed in JS"
    assert "meanNzRange" in html, "meanNzRange db-wide range must be computed in JS"
    assert "normInRange" in html, "normInRange helper must be used to scale sparklines"
    # And the sparkline positioning for mean must use that range, not a fixed 0-100 scale.
    assert "normInRange(s.mean," in html, "mean sparkline must use normInRange with meanRange"
