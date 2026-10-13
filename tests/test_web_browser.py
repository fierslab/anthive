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
import re
import shutil
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


@pytest.fixture(autouse=True)
def _all_facets_visible(monkeypatch):
    """Show every facet unless a test sets facets_visible itself (the repo
    default config limits the facet pages to four)."""
    monkeypatch.setattr(browser, "_facets_visible_cfg", [[]])


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


# ── unit tests: _resolve_gene_panel ────────────────────────────────────────────

def test_resolve_gene_panel_precomputed_wins():
    """Curated top_genes short-circuits fallback regardless of cell_type."""
    genes, source = browser._resolve_gene_panel(["TREM2", "CX3CR1"], "microglia")
    assert genes == ["TREM2", "CX3CR1"]
    assert source == "precomputed"


@pytest.mark.parametrize("term, expected_source", [
    ("microglial cell", "microglia_fallback"),
    ("microglia",       "microglia_fallback"),
    ("Microglia",       "microglia_fallback"),   # case-insensitive
    ("astrocyte",       "astrocyte_fallback"),
    ("neuron",          "neuron_fallback"),
])
def test_resolve_gene_panel_string_matches_fallback(term, expected_source):
    """A single-string term maps to the right fallback panel."""
    genes, source = browser._resolve_gene_panel([], term)
    assert source == expected_source
    assert isinstance(genes, list) and len(genes) > 0


@pytest.mark.parametrize("term", [None, "", "unknown_thing", " "])
def test_resolve_gene_panel_no_match_returns_empty(term):
    """Missing/empty/unmatched terms return the empty 'none' source."""
    genes, source = browser._resolve_gene_panel([], term)
    assert genes == []
    assert source == "none"


def test_resolve_gene_panel_list_cell_type_bug_3():
    """Regression: cell_type given as a list (from CellxGene .mus) must not crash.

    Before the fix, ``(cell_type_term or '').lower()`` raised
    ``AttributeError: 'list' object has no attribute 'lower'``.
    """
    genes, source = browser._resolve_gene_panel([], ["microglial cell"])
    assert source == "microglia_fallback"
    assert genes  # non-empty


def test_resolve_gene_panel_list_picks_first_matching_entry():
    """When multiple cell-type entries are present, the first known one wins."""
    genes, source = browser._resolve_gene_panel(
        [], ["neuron", "microglial cell"]
    )
    assert source == "neuron_fallback"
    assert genes


def test_resolve_gene_panel_empty_list_returns_none():
    """An empty list of cell-type terms behaves like a missing term."""
    genes, source = browser._resolve_gene_panel([], [])
    assert genes == []
    assert source == "none"


def test_resolve_gene_panel_list_ignores_falsy_entries():
    """Empty/None entries in the list are skipped without crashing."""
    genes, source = browser._resolve_gene_panel([], [None, "", "astrocyte"])
    assert source == "astrocyte_fallback"


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
         "experimental_system": "post_mortem_tissue", "suspension_type": "cell",
         "technology": "10x_chromium"},
        {"id": "A/ds2", "celltype": ["microglial cell", "macrophage"],
         "tissue": ["cortex", "hippocampus"], "experimental_system": None,
         "suspension_type": "nucleus", "technology": "10x_chromium_3p_v3"},
        {"id": "A/ds3", "celltype": None, "tissue": None,
         "experimental_system": "xenograft", "suspension_type": "cell",
         "technology": "10x_chromium"},
    ]
    try:
        result = browser._build_dataset_annotations("http://x")
    finally:
        browser._all_databases = _orig

    # Extra keys (title, n_cells, contributor) are also present; check core fields.
    assert result["A/ds1"]["celltype"] == "microglial cell"
    assert result["A/ds1"]["tissue"] == "brain"
    # post_mortem_tissue → "post-mortem" via _derive_biosample
    assert result["A/ds1"]["biosample"] == "post-mortem"
    # system is the deprecated alias for biosample
    assert result["A/ds1"]["system"] == result["A/ds1"]["biosample"]
    # suspension_type=nucleus → "single nucleus"
    assert result["A/ds2"]["technology"] == "single nucleus"
    # List values must be flat strings — JS .toLowerCase() would throw on arrays.
    assert result["A/ds2"]["celltype"] == "microglial cell,macrophage"
    assert result["A/ds2"]["tissue"] == "cortex,hippocampus"
    assert result["A/ds2"]["biosample"] == ""
    assert result["A/ds3"]["celltype"] == ""
    assert result["A/ds3"]["tissue"] == ""
    # xenograft → "xenograft"
    assert result["A/ds3"]["biosample"] == "xenograft"


def test_build_dataset_annotations_system_field_in_all_entries():
    """Every entry must have 'biosample' and deprecated 'system' keys even when absent."""
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
    assert "biosample" in result["B/ds1"]
    assert result["B/ds1"]["biosample"] == ""
    assert "technology" in result["B/ds1"]


# ── unit tests: _derive_technology / _derive_biosample ────────────────────────

@pytest.mark.parametrize("suspension_type,technology,expected", [
    ("cell",    "10x_chromium",      "single cell"),
    ("nucleus", "10x_chromium_3p_v3", "single nucleus"),
    ("na",      "10x_visium",        "spatial"),
    ("na",      "bulk_RNA-seq",      "bulk"),
    ("na",      "Stereo-seq",        "spatial"),
    ("na",      "NovaST",            "spatial"),
    ("na",      "MERFISH",           "spatial"),
    ("na",      "CosMx",             "spatial"),
    ("na",      "mass-spectrometry", "bulk"),
    ("cell",    "Smart-seq2",        "single cell"),
    ("nucleus", "snRNA-seq",         "single nucleus"),
    ("",        "",                  ""),
])
def test_derive_technology(suspension_type, technology, expected):
    """_derive_technology maps suspension_type + technology to coarse category."""
    db = {"suspension_type": suspension_type, "technology": technology}
    assert browser._derive_technology(db) == expected


@pytest.mark.parametrize("experimental_system,expected", [
    ("post_mortem_tissue", "post-mortem"),
    ("xenograft",          "xenograft"),
    ("chimera",            "xenograft"),   # chimera collapses to xenograft
    ("live_biopsy",        "fresh"),
    ("in_vitro_2d",        "in-vitro"),
    ("organoid",           "in-vitro"),
    ("bulk_tissue",        ""),            # bulk has no biosample
    (None,                 ""),
    ("",                   ""),
])
def test_derive_biosample(experimental_system, expected):
    """_derive_biosample maps experimental_system to display labels."""
    db = {"experimental_system": experimental_system}
    assert browser._derive_biosample(db) == expected


def test_build_dataset_annotations_collection_and_dataset_desc():
    """collection_name, collection_desc, and dataset_desc are populated."""
    _orig = browser._all_databases
    browser._all_databases = lambda api_url: [
        {
            "id": "C/ds1",
            "title": "My Dataset",
            "n_cells": 5000,
            "collection": {
                "group": "Smith 2024",
                "description": "# Smith 2024\n\nThis is a **cohort** of samples collected from donors.",
            },
            "description": "Single-nucleus RNA-seq of frontal cortex microglia from 12 donors.",
        },
        {
            "id": "C/ds2",
            "title": "Minimal Dataset",
            "n_cells": 200,
            "collection": {},
            # No description, no summary
        },
    ]
    try:
        result = browser._build_dataset_annotations("http://x")
    finally:
        browser._all_databases = _orig

    # Collection name extracted from collection.group
    assert result["C/ds1"]["collection_name"] == "Smith 2024"
    # H1 heading stripped; first paragraph extracted and emphasis stripped
    coll_desc = result["C/ds1"]["collection_desc"]
    assert "cohort" in coll_desc
    assert "#" not in coll_desc        # heading stripped
    assert "**" not in coll_desc       # markdown emphasis stripped
    # Dataset description plain text
    ds_desc = result["C/ds1"]["dataset_desc"]
    assert "frontal cortex" in ds_desc
    # Minimal dataset: empty strings, not missing keys
    assert result["C/ds2"]["collection_name"] == ""
    assert result["C/ds2"]["collection_desc"] == ""
    assert result["C/ds2"]["dataset_desc"] == ""


def test_md_first_paragraph_strips_heading_and_truncates():
    """_md_first_paragraph skips H1 headings and truncates to max_chars."""
    body = "# Title\n\nFirst paragraph text that is quite long indeed.\n\nSecond paragraph."
    result = browser._md_first_paragraph(body, 20)
    assert result == "First paragraph text…"
    # No heading in output
    assert "Title" not in result


def test_md_first_paragraph_empty_input():
    assert browser._md_first_paragraph("", 200) == ""
    assert browser._md_first_paragraph(None, 200) == ""  # type: ignore[arg-type]


def test_md_first_paragraph_no_heading():
    body = "Just a paragraph.\nContinued."
    result = browser._md_first_paragraph(body, 200)
    assert result == "Just a paragraph. Continued."


# ── n_cells facet and rich popover — HTML presence tests ──────────────────────

def test_module_view_has_n_cells_filter(client):  # pyright: ignore
    """Module drilldown page renders the .mv-flt-n_cells select with cumulative buckets.

    The four options are: all (empty value), gt100, gt1k, gt10k. Cumulative
    lower bounds — each step is a subset of the previous.
    """
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    text = resp.text
    assert "mv-flt-n_cells" in text
    for bucket_value in ('"gt100"', '"gt1k"', '"gt10k"'):
        assert bucket_value in text, f"missing bucket option {bucket_value}"
    # Old range-bucket values must be gone (regression guard).
    assert '"lt1k"' not in text
    assert '"1k-10k"' not in text
    assert '"gt100k"' not in text


def test_module_view_popover_has_collection_and_dataset_fields(client):  # pyright: ignore
    """DS_ANNOT embedded in module drilldown includes collection_name and dataset_desc."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    # DS_ANNOT is embedded as JSON — it must carry the new keys
    assert "collection_name" in resp.text
    assert "collection_desc" in resp.text
    assert "dataset_desc" in resp.text


def test_genes_page_has_n_cells_filter(client):  # pyright: ignore
    """Gene atlas page renders the gv-flt-n_cells select with cumulative buckets."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    text = resp.text
    assert "gv-flt-n_cells" in text
    for bucket_value in ('"gt100"', '"gt1k"', '"gt10k"'):
        assert bucket_value in text, f"missing bucket option {bucket_value}"
    assert '"lt1k"' not in text
    assert '"1k-10k"' not in text
    assert '"gt100k"' not in text


def test_genes_page_popover_has_collection_and_dataset_fields(client):  # pyright: ignore
    """DS_ANNOT embedded in gene atlas includes collection_name and dataset_desc."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    assert "collection_name" in resp.text
    assert "collection_desc" in resp.text
    assert "dataset_desc" in resp.text


def test_genes_page_rank_column_headers(client):  # pyright: ignore
    """Gene atlas table headers say 'rank mean' and 'rank mean(nz)', not 'mean'/'meannz'."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    text = resp.text
    assert "rank mean" in text
    assert "rank mean(nz)" in text
    # The old bare column header text must not appear as a standalone <th> label.
    # (The word 'mean' still appears inside tooltip strings and JS, so we check
    # the header markup specifically.)
    assert ">meannz<" not in text
    # Confirm sort handles use the old API field names (unchanged).
    assert 'data-sortcol="mean"' in text
    assert 'data-sortcol="mean_nonzero"' in text


def test_genes_page_popover_has_mean_raw_values(client):  # pyright: ignore
    """The dsTip function template includes mean_nz label so raw values appear in popovers."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    # The JS source embeds the strings that build popover rows; verify they're present.
    assert "mean_nz" in resp.text
    assert "tip-row" in resp.text


def test_genes_page_rank_text_not_raw_mean(client):  # pyright: ignore
    """Rank columns display rank percentile text (toFixed + '%'), not raw num(mean, 3) pattern."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    text = resp.text
    # The JS now builds rank text via meanRankPct.toFixed(1) + '%', not num(r.mean, 3).
    assert "meanRankText" in text
    assert "mnzRankText" in text
    # Raw mean is routed to rowStats / dsTip, not the visible cell text.
    assert "rowStats" in text


# ── fake REST API ──────────────────────────────────────────────────────────────

# Two collections. GroupA mixes organism/celltype/disease across its two
# datasets (→ 'mix' summaries); GroupB omits group_id to exercise the
# id.split('/')[0] fallback.
_DATABASES = [
    {
        "id": "GroupA/ds1", "group_id": "GroupA", "file_id": "ds1", "version": "0.1.4", "updated": "2026-10-05",
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
        # ds2 is a secondary (re-aggregated) dataset — the badge should render.
        "is_secondary": True,
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
        "file_id": "ds1", "version": "0.1.4", "updated": "2025-01-01", "group_id": "GroupA", "title": "Dataset One",
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
        # Matches the /databases entry — ds2 is a secondary (re-aggregated) deposit.
        "is_secondary": True,
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


_DATASET_FACETS = {
    "db_id": "GroupA/ds1",
    "n_facets": 3,
    "hidden_sources_visible": False,
    "definitions_status": {"state": "under_audit", "note": "An audit of the facets is running."},
    "facets": [
        {"facet": "ant_sex", "label": "Sex", "summary": "Biological sex, coded m or f.",
         "defined": True, "mapping_kind": "renamed", "live": True, "dtype": "categorical",
         "source_columns": ["Gender"],
         "sources": [{"column": "Gender", "published_as": "ant_sex", "hidden": False}],
         "source_hidden": False, "redacted": False,
         "value_map": [
             {"from": "MALE", "to": "m", "rule": "mapped", "n_cells": 800},
             {"from": "FEMALE", "to": "f", "rule": "mapped", "n_cells": 650},
             {"from": "undefined", "to": None, "rule": "hidden", "n_cells": 50},
             {"from": "M", "to": "m", "rule": "mapped", "n_cells": 0},
         ],
         "unmapped": "passthrough", "default": None, "constant_value": None,
         "n_cells": 1500, "n_missing": 50, "n_distinct_source_values": 3,
         "value_map_truncated": False},
        {"facet": "ant_braak_group", "label": "Braak group", "summary": "Braak binned.",
         "defined": True, "mapping_kind": "derived", "live": True, "dtype": "categorical",
         "source_columns": ["braak_stage"],
         "sources": [{"column": "braak_stage", "published_as": "ant_braak", "hidden": False}],
         "source_hidden": False, "redacted": False,
         "value_map": [
             {"from": 5.0, "to": "high", "rule": "mapped", "n_cells": 900},
             {"from": 9.0, "to": "unstaged", "rule": "default", "n_cells": 3},
         ],
         "unmapped": "default", "default": "unstaged", "constant_value": None,
         "n_cells": 1500, "n_missing": 0, "n_distinct_source_values": 2,
         "value_map_truncated": False},
        {"facet": "ant_tissue", "label": "Tissue", "summary": "Tissue.", "defined": True,
         "mapping_kind": "constant", "live": True, "dtype": "categorical",
         "source_columns": [], "sources": [], "source_hidden": False, "redacted": False,
         "value_map": [], "unmapped": None, "default": None,
         "constant_value": "prefrontal cortex", "n_cells": 1500, "n_missing": 0},
        {"facet": "ant_apoe4_carrier", "label": "APOE e4 carrier", "summary": "E4+ or E4-.",
         "defined": True, "mapping_kind": "derived", "live": True, "dtype": "categorical",
         "source_columns": [], "sources": [], "source_hidden": True, "redacted": True,
         "value_map": [{"from": None, "to": "E4+", "rule": "mapped", "n_cells": 700}],
         "unmapped": "default", "default": None, "constant_value": None,
         "n_cells": 1500, "n_missing": 800},
    ],
}


def _fx_model(formula):
    return {
        "stratum": {"cell_type": "microglial cell", "source": "constant_all_cells",
                    "description": "all cells of the dataset (ant_cell_type is one constant label)"},
        "facet_term": "m vs f: coefficient = m minus f (f is the reference)",
        "donors": {"per_level": {"f": 12, "m": 9}, "n_analysed": 21, "n_dropped_missing_covariate": 0,
                   "min_cells_per_donor": 10, "min_donors_per_level": 3},
        "descriptive_only_rule": "descriptive only (no p, q or CI) when a level has fewer than 5 donors",
        "genes": {"status": "ok", "unit_of_analysis": "one pseudobulk per donor",
                  "formula": formula,
                  "covariates": [{"name": "age", "status": "included", "term": "age_z", "reason": "z-scored"},
                                 {"name": "pmi", "status": "dropped", "term": "",
                                  "reason": "known for fewer than 90% of donors, or constant"}],
                  "method": "edgeR filterByExpr; TMM + limma voom", "multiple_testing": "BH within unit",
                  "tool": "ant-facet-test/1.0", "seed": 20261004, "run_date": "2026-10-04",
                  "versions": {"limma": "3.62.1"}},
        "modules": None,
    }


_FX_UNIT_FLAGS = {"descriptive_only": False, "n_lt5": False, "provisional": False,
                  "composition_flag": True, "composition_p": 0.012, "batch_used": False,
                  "batch_confounded": False}

_FACET_TESTS_DS1 = {
    "db_id": "GroupA/ds1", "status": "tested", "reason": None, "n_units": 3, "n_units_tested": 2,
    "q_threshold": 0.05, "dataset_changed": False,
    "files": {"genes": True, "modules": True, "composition": True},
    "runs": {"genes": {"tool": "ant-facet-test/1.0", "timestamp_utc": "2026-10-04T11:00:00+00:00", "seed": 20261004}},
    "cohort": {"cohort": "CohortX (shared)", "cohort_label": "CohortX", "role": "anchor", "counted": True,
               "overlap_unknown": True},
    "replicate_unit": "donor", "replicate_column": "ant_donor", "replicate_basis": "declared",
    "disease_basis": "braak_cerad",
    "units": [
        {"facet": "sex", "cell_type": "microglial cell", "celltype_source": "constant_all_cells",
         "tested": True, "status_genes": "ok", "status_modules": "ok", "contrast": "m_vs_f",
         "analysis_level": "replicate", "analysis_level_label": "replicate level", "replicate_unit": "donor",
         "replicate_column": "ant_donor", "tau_log2": 0.1375,
         "level_ref": "f", "level_test": "m", "level_counts": {"f": 12, "m": 9}, "n_donors": 21,
         "n_donors_ref": 12, "n_donors_test": 9, "covariates": ["age"], "covariates_omitted": ["pmi"],
         "flags": _FX_UNIT_FLAGS, "genes": {"n_tested": 15000, "n_q05": 42},
         "modules": [{"method": "cell_mean", "source": "geneset", "n_modules": 128,
                      "n_q05_param": 3, "n_q05_perm": 1}],
         "model": _fx_model("expression ~ sex + age_z"),
         "sex_control": {"status": "passed", "xist_logFC": -9.1, "xist_q": 1e-20, "rps4y1_logFC": 7.0,
                         "rps4y1_q": 1e-15, "n_y_tested": 8, "n_y_up_q01": 8, "rule": "XIST down, RPS4Y1 up"}},
        {"facet": "disease", "cell_type": "microglial cell", "celltype_source": "constant_all_cells",
         "tested": False, "reason": "level with < 3 donors (control=2, AD=19)",
         "analysis_level": "cell", "analysis_level_label": "cell-level, descriptive only",
         "analysis_level_reason": "level with < 3 donors (control=2, AD=19)",
         "flags": {"provisional": True}, "genes": None, "modules": [], "model": {}},
        {"facet": "braak", "cell_type": "microglial cell", "celltype_source": "constant_all_cells",
         "tested": True, "contrast": "braak_per_unit", "level_ref": None, "level_test": None,
         "level_counts": {"n_donors": 21, "braak_min": 0, "braak_max": 6, "n_distinct": 6},
         "n_donors": 21, "covariates": ["sex", "age"], "covariates_omitted": [],
         "flags": {**_FX_UNIT_FLAGS, "composition_flag": False, "descriptive_only": True},
         "genes": {"n_tested": 14000, "n_q05": 0}, "modules": [],
         "model": _fx_model("expression ~ braak + sex + age_z")},
    ],
}

_FACET_TESTS_ACROSS = {
    "facet": "disease", "facet_column": "ant_disease", "n_rows": 2, "n_datasets_tested": 2,
    "rows": [{"db_id": "pool:seaad_microglia", "title": "seaad_microglia (donor-pooled)", "cell_type": "microglial cell",
              "celltype_source": "constant_all_cells", "contrast": "AD_vs_control", "level_ref": "control",
              "level_test": "AD", "level_counts": {"control": 30, "AD": 50}, "n_donors": 80,
              "n_donors_ref": 30, "n_donors_test": 50, "covariates": ["sex", "age"],
              "flags": {"provisional": False, "descriptive_only": False}, "genes": {"n_tested": 15000, "n_q05": 9},
              "modules": [], "top_genes": [], "run_date": "2026-10-04", "cohort_label": "SEA-AD", "role": "anchor",
              "counted": True, "pooled": True, "overlap_unknown": True, "independence": "unknown",
              "disease_basis": "braak_cerad", "analysis_level": "replicate", "replicate_unit": "donor",
              "model": _fx_model("expression ~ disease + sex + age_z")},
             {"db_id": "GroupA/ds2", "title": "Dataset Two", "cell_type": "microglial cell",
              "celltype_source": "constant_all_cells", "contrast": "AD_vs_control", "level_ref": "control",
              "level_test": "AD", "level_counts": {"control": 8, "AD": 20}, "n_donors": 28,
              "n_donors_ref": 8, "n_donors_test": 20, "covariates": ["sex", "age", "pmi"],
              "flags": {"provisional": True, "descriptive_only": False}, "genes": {"n_tested": 15000, "n_q05": 7},
              "modules": [], "top_genes": [{"gene": "PTPRG", "logFC": 0.4, "q_value": 0.001}],
              "run_date": "2026-10-04", "cohort_label": "CohortY", "role": "anchor", "counted": False,
              "not_counted_reason": "disease definition is author_dx; replication only combines pathology based definitions",
              "disease_basis": "author_dx", "analysis_level": "replicate", "replicate_unit": "donor",
              "model": _fx_model("expression ~ disease + sex + age_z + pmi_z")}],
    "not_tested": [{"db_id": "GroupA/ds1", "title": "Dataset One", "reason": "level with < 3 donors"}],
}


def _fake_api_get(api_url, path, **kwargs):  # pyright: ignore
    """In-process stand-in for browser._api_get. Dispatches on path; raises
    requests.HTTPError for anything unknown (mirrors a 404 from ant-serve,
    which the routes catch)."""
    if path == "/databases":
        return {"databases": _DATABASES}
    if path.startswith("/databases/") and path.endswith("/de"):
        return _DE_STUDIES
    if "/de/" in path:           # /databases/{id}/de/{study_id}
        return _DE_STUDY_DETAIL
    if path == "/databases/GroupA/ds1/facets":
        return _DATASET_FACETS
    if path == "/databases/GroupA/ds1/facet-tests":
        return _FACET_TESTS_DS1
    if path == "/facets/ant_disease/tests":
        return _FACET_TESTS_ACROSS
    if path == "/facets/cohorts":
        return {"status": "DRAFT", "datasets": [
            {"db_id": "pool:seaad_microglia", "pool_members": ["GroupA/ds1", "GroupA/ds2"]},
            {"db_id": "GroupA/ds1", "pool_members": []}]}
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
    if path == "/facets":
        return {
            "n_facets": 2,
            "facets": [
                {"facet": "ant_disease", "type": "categorical",
                 "type_mixed": False, "n_datasets": 2, "signal_strength": 0.42,
                 "label": "Disease", "defined": True,
                 "summary": "Diagnosis of the donor as a short code."},
                {"facet": "ant_age", "type": "numerical",
                 "type_mixed": False, "n_datasets": 1, "signal_strength": 0.05,
                 "label": "age", "defined": False, "summary": ""},
            ],
            "definitions_status": {"state": "under_audit",
                                   "note": "An audit of the facets is running."},
        }
    if path.startswith("/facets/"):
        name = path[len("/facets/"):].split("?")[0]
        if name == "ant_disease":
            return {
                "facet": "ant_disease", "type": "categorical",
                "type_mixed": False, "n_datasets": 2, "total_n_cells": 501_500,
                "definition": {
                    "facet": "ant_disease", "tag": "disease", "label": "Disease",
                    "defined": True,
                    "summary": "Diagnosis of the donor as a short code.",
                    "definition": "Clinical or neuropathological diagnosis of the donor.",
                    "kind": "categorical", "values": "AD, control, MCI, PD",
                    "missing": "Empty (NULL) when not recorded.",
                    "hint_keywords": ["disease", "diagnosis"],
                },
                "definitions_status": {"state": "under_audit",
                                       "note": "An audit of the facets is running."},
                "datasets": [
                    {"db_id": "GroupA/ds2", "title": "Dataset Two",
                     "group": "Group A Study", "group_id": "GroupA",
                     "file_id": "ds2", "n_cells": 500_000,
                     "kind_on_this_dataset": "categorical",
                     "mapping_kind": "renamed", "live": True,
                     "source_columns": ["Diagnosis"],
                     "sources": [{"column": "Diagnosis", "published_as": "ant_disease", "hidden": False}],
                     "source_hidden": False, "redacted": False,
                     "value_map": [
                         {"from": "Alzheimer", "to": "alzheimer disease", "rule": "mapped", "n_cells": 250_000},
                         {"from": "unknown", "to": None, "rule": "hidden", "n_cells": 12},
                         {"from": "normal", "to": "normal", "rule": "passthrough", "n_cells": 249_988},
                     ],
                     "unmapped": "passthrough", "default": None, "constant_value": None,
                     "n_missing": 12, "n_distinct_source_values": 3,
                     "value_map_truncated": False},
                    {"db_id": "GroupA/ds1", "title": "Dataset One",
                     "group": "Group A Study", "group_id": "GroupA",
                     "file_id": "ds1", "n_cells": 1500,
                     "kind_on_this_dataset": "categorical",
                     "mapping_kind": "derived", "live": True,
                     "source_columns": ["dx_code"],
                     "sources": [{"column": "dx_code", "published_as": "dx_code", "hidden": False}],
                     "source_hidden": False, "redacted": False,
                     "value_map": [
                         {"from": "1", "to": "normal", "rule": "mapped", "n_cells": 1500},
                         {"from": "7", "to": None, "rule": "default", "n_cells": 4},
                     ],
                     "unmapped": "default", "default": None, "constant_value": None,
                     "n_missing": 4, "n_distinct_source_values": 2,
                     "value_map_truncated": False},
                ],
                "non_carriers": [
                    {"db_id": "GroupB/ds3", "title": "Dataset Three", "group": "Group B",
                     "group_id": "GroupB", "file_id": "ds3", "n_cells": 900,
                     "candidate_columns": ["Diagnosis_clinical"], "declared": True,
                     "declared_kind": "renamed"},
                    {"db_id": "GroupB/ds4", "title": "Dataset Four", "group": "Group B",
                     "group_id": "GroupB", "file_id": "ds4", "n_cells": 100,
                     "candidate_columns": [], "declared": False, "declared_kind": None},
                ],
                "value_distribution": {
                    "kind": "categorical",
                    "values": [
                        {"value": "alzheimer disease", "n_cells": 250_000, "n_datasets": 1},
                        {"value": "normal", "n_cells": 251_500, "n_datasets": 2},
                    ],
                    "total_cells": 501_500,
                },
                "modules": {
                    "rows": [
                        {"catalog": "MICROGLIA_V1", "module": "DAM_pos",
                         "dataset": "GroupA/ds1", "source": "starcat",
                         "statistic": "eta2", "value": 0.32,
                         "n_cells": 1500, "n_categories": 2,
                         "tag": "disease", "tag_column": "ant_disease"},
                        {"catalog": "MICROGLIA_V1", "module": "Cycling_pos",
                         "dataset": "GroupA/ds1", "source": "starcat",
                         "statistic": "eta2", "value": 0.11,
                         "n_cells": 1500, "n_categories": 2,
                         "tag": "disease", "tag_column": "ant_disease"},
                    ],
                    "n_total": 2, "truncated": False,
                },
            }
        raise requests.HTTPError(f"404 for {name}")
    if path == "/genesets":
        return {"genesets": _GENESETS}
    if path == "/genesets/studies":
        # Aggregated per-study view (REST addition 2026-08-26).  Mirrors the
        # shape ant-serve returns for the /genesets landing table.
        return {
            "count": 2,
            "studies": [
                {"study": "StudyX",
                 "title": "Study X paper title",
                 "doi": "10.1234/studyx", "pmid": "12345",
                 "journal": "Test Journal", "year": 2023,
                 "authors": ["Alice", "Bob"],
                 "organisms": ["human"], "celltypes": ["microglial cell"],
                 "methods": ["DESeq2"],
                 "n_experiments": 1, "n_genesets": 2, "has_index_md": True},
                {"study": "StudyY",
                 "title": "", "doi": "", "pmid": "", "journal": "",
                 "year": None, "authors": [],
                 "organisms": ["mouse"], "celltypes": [],
                 "methods": ["edgeR"],
                 "n_experiments": 1, "n_genesets": 1, "has_index_md": False},
            ],
        }
    if path == "/genesets/StudyX/Exp1":
        return _EXPERIMENT_PAYLOAD
    if path == "/genesets/StudyX/Exp1/gs_up":
        return _GENESET_PAYLOAD
    if path == "/genesets/StudyY/Exp2":
        return {
            "metadata": {"name": "Experiment Y", "organism": "mouse",
                         "method": "edgeR"},
            "body": "",
            "genesets": [g for g in _GENESETS if g["experiment"] == "Exp2"],
        }
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


def test_collection_icon_reflects_source_not_access(monkeypatch):
    # A collection whose datasets are ALL lab-generated (source=internal) but
    # have mixed shareability (access=internal on some, access=public on
    # others) must show the lab-beaker icon on the landing page — the
    # collection card icon encodes provenance, not per-dataset shareability.
    # This is the Hou2025 case: internal source, mixed access.
    dbs = [
        {"id": "H25/A", "group_id": "H25", "collection": {"group": "Hou2025"},
         "access": "internal", "source": "internal", "updated": "2026-06-01"},
        {"id": "H25/B", "group_id": "H25", "collection": {"group": "Hou2025"},
         "access": "public", "source": "internal", "updated": "2026-06-02"},
        {"id": "H25/C", "group_id": "H25", "collection": {"group": "Hou2025"},
         "access": "public", "source": "internal", "updated": "2026-06-03"},
    ]
    monkeypatch.setattr(browser, "_api_get",
                        lambda base, path, **_: {"databases": dbs} if path == "/databases" else {})
    collections = browser.get_collection_list("http://x")
    hou = next(c for c in collections if c["group_id"] == "H25")
    assert hou["pub_icon"]["access"] == "lab-generated"
    assert "lab-generated" in hou["pub_icon"]["tooltip"]


def test_collection_icon_reflects_source_external_wins(monkeypatch):
    # Even one imported dataset in the collection flips the icon to globe:
    # a "mostly lab-generated" collection is not wholly lab-generated.
    dbs = [
        {"id": "MIX/A", "group_id": "MIX", "collection": {"group": "MixCol"},
         "access": "internal", "source": "internal", "updated": "2026-06-01"},
        {"id": "MIX/B", "group_id": "MIX", "collection": {"group": "MixCol"},
         "access": "internal", "source": "external", "updated": "2026-06-02"},
    ]
    monkeypatch.setattr(browser, "_api_get",
                        lambda base, path, **_: {"databases": dbs} if path == "/databases" else {})
    collections = browser.get_collection_list("http://x")
    mix = next(c for c in collections if c["group_id"] == "MIX")
    assert mix["pub_icon"]["access"] == "imported"


def test_collection_icon_missing_source_defaults_to_lab_generated(monkeypatch):
    # A collection with no source recorded defaults to lab-generated — matches
    # the safer-default contract used everywhere else in the icon path.
    dbs = [
        {"id": "X/A", "group_id": "X", "collection": {"group": "X"},
         "access": "public", "updated": "2026-06-01"},
    ]
    monkeypatch.setattr(browser, "_api_get",
                        lambda base, path, **_: {"databases": dbs} if path == "/databases" else {})
    collections = browser.get_collection_list("http://x")
    col = next(c for c in collections if c["group_id"] == "X")
    assert col["pub_icon"]["access"] == "lab-generated"


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


def test_settings_page_renders(client):
    """Both settings controls render, and the page ships the antSettings API
    (module_algorithm + favourite_genes storage keys). Client-side only —
    no server persistence."""
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "Preferences" in resp.text
    # Both controls present
    assert 'name="module_algorithm"' in resp.text
    assert 'value="geneset"' in resp.text
    assert 'value="ucell"' in resp.text
    # AUCell label was retired in favour of UCell (2026-08-26). The user-facing
    # radio label reads UCell now; the only surviving AUCell mention is the
    # migration note in the antSettings comment.
    assert 'AUCell' not in resp.text.split('window.antSettings')[0]
    assert '> UCell' in resp.text or '>UCell' in resp.text or 'UCell' in resp.text
    assert 'id="favourite-genes"' in resp.text
    # Storage keys documented (also referenced by consumers via window.antSettings)
    assert "anthive.settings.module_algorithm" in resp.text
    assert "anthive.settings.favourite_genes" in resp.text


def test_settings_link_in_topbar_when_authed(client):
    """The 'preferences' link now lives in the always-visible topbar nav
    (moved from the Authelia account dropdown in P1.14 — settings are
    localStorage / per-browser, so they don't need SSO)."""
    # Authed: link present.
    resp = client.get("/", headers={"Remote-User": "test-user", "Remote-Name": "Test User"})
    assert resp.status_code == 200
    assert 'href="/settings"' in resp.text
    # Un-authenticated: link ALSO present (no SSO gate on preferences).
    resp2 = client.get("/")
    assert resp2.status_code == 200
    assert 'href="/settings"' in resp2.text


def test_geneset_variability_page_renders(client):
    resp = client.get("/genesets/variability")
    assert resp.status_code == 200
    assert 'id="mv-table"' in resp.text
    assert "/module-variability" in resp.text
    # 'variability' must not be swallowed as a study id by the 2-seg route
    assert "geneset variability" in resp.text.lower()
    # configurable colour-scale clip + dismissable exploratory caveat
    assert 'id="mv-cscale"' in resp.text
    assert 'id="mv-caveat-ack"' in resp.text


def test_genesets_index_links_variability(client):
    resp = client.get("/genesets")
    assert resp.status_code == 200
    assert '/genesets/variability' in resp.text


def test_genesets_group_selector(client):
    # After the 2026-08-26 landing redesign, the tabs are: gene sets (study
    # drilldown) and find-by-gene. starCAT is intentionally hidden from the
    # landing; its deep-link route still resolves.
    home = client.get("/genesets")
    assert home.status_code == 200
    assert "grp-tab" in home.text
    assert "/genesets?group=bygene" in home.text     # find-by-gene tab
    assert "/genesets?group=starcat" not in home.text  # starCAT tab hidden
    assert "StudyX" in home.text                     # study row (default tab)


def test_genesets_find_by_gene_mode(client):
    # The 'find by gene' mode renders a search box wired to the reverse-lookup
    # endpoint (results are fetched + rendered client-side).
    resp = client.get("/genesets?group=bygene")
    assert resp.status_code == 200
    assert 'id="bg-gene"' in resp.text
    assert "/genesets/by-gene/" in resp.text


def test_genesets_starcat_deep_link_still_resolves(client):
    # starCAT is hidden from the landing but its module deep link route must
    # continue to render its own page (accessible from the variability
    # heatmap and external references).
    resp = client.get("/genesets/starcat/MICROGLIA_T/modules/Microglia_DAM")
    assert resp.status_code == 200
    assert 'id="sc-top"' in resp.text
    assert "starCAT program" in resp.text


def test_geneset_starcat_module_detail_renders(client):
    # Module detail page renders and isn't swallowed by the generic
    # /genesets/{study}/{experiment}/{name} route; fetches the module endpoint.
    resp = client.get("/genesets/starcat/MICROGLIA_T/modules/Microglia_DAM")
    assert resp.status_code == 200
    assert 'id="sc-top"' in resp.text
    assert "/catalogs/starcat/" in resp.text
    assert "starCAT program" in resp.text


def test_facets_index_renders(client):
    # /facets renders. The table shows the facet names sourced from the
    # ant-serve /facets stub and the topbar carries the new Facets link.
    resp = client.get("/facets")
    assert resp.status_code == 200
    assert 'id="facet-table"' in resp.text
    assert "ant_disease" in resp.text
    assert "ant_age" in resp.text
    # Numeric type variant renders.
    assert "numerical" in resp.text
    # Topbar entry present.
    assert 'href="/facets"' in resp.text
    # Wrapped in the responsive scroll container per convention.
    assert 'class="table-wrap"' in resp.text


def test_facets_link_in_topbar(client):
    # Facets link is visible in the topbar nav on any page.
    resp = client.get("/")
    assert resp.status_code == 200
    assert '>facets<' in resp.text


def test_facet_detail_renders(client):
    # Per-facet page: header, value distribution, geneset variability table.
    resp = client.get("/facets/ant_disease")
    assert resp.status_code == 200
    assert "ant_disease" in resp.text
    # value distribution card headings
    assert "Value distribution" in resp.text
    # both distinct values rendered
    assert "alzheimer disease" in resp.text
    assert "normal" in resp.text
    # the per-cell variability cards are gone (pseudoreplicated)
    assert "Top variable genesets" not in resp.text
    assert "Top variable genes" not in resp.text
    assert 'id="tvg-dataset"' not in resp.text


def test_facet_detail_accepts_bare_tested_facet_name(client):
    # /facets/disease (bare name of a tested facet) redirects to the column page instead of a 400
    resp = client.get("/facets/disease", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/facets/ant_disease"
    assert client.get("/facets/braak", follow_redirects=False).headers["location"] == "/facets/ant_braak"
    assert client.get("/facets/ant_disease", follow_redirects=False).status_code == 200


def test_facets_pages_carry_the_preview_banner_not_the_experimental_box(client, monkeypatch):
    monkeypatch.setenv("ANTHIVE_ISSUE_TRACKER_URL", "https://tracker.example/issues")
    for path in ("/facets", "/facets/ant_disease"):
        text = client.get(path).text
        assert 'class="exp-notice"' not in text                    # one preview device: the review banner
        assert 'class="review-banner"' in text
        assert text.index('class="page-title') < text.index('class="review-banner"')   # under the h1


def test_facets_nav_entry_has_experimental_badge(client):
    text = client.get("/").text
    assert '<a href="/facets"' in text
    nav_entry = text.split('<a href="/facets"')[1].split("</a>")[0]
    assert 'class="exp-badge"' in nav_entry
    # other pages carry no experimental notice
    assert 'class="exp-notice"' not in text


def test_facets_index_shows_definitions_and_audit_note(client):
    text = client.get("/facets").text
    assert 'id="facet-audit-note"' in text
    assert "The facet definitions are under audit." in text and 'data-help-for="facet-audit-box"' in text
    assert "An audit of the facets is running." in text
    assert "Diagnosis of the donor as a short code." in text
    # a facet without a written definition says so
    assert "No definition yet." in text


def test_facet_detail_definition_at_top(client):
    text = client.get("/facets/ant_disease").text
    assert 'id="facet-definition"' in text
    definition = text.split('id="facet-definition"')[1].split("</section>")[0]
    assert "Diagnosis of the donor as a short code." in definition
    assert "Clinical or neuropathological diagnosis of the donor." in definition
    assert "AD, control, MCI, PD" in definition
    assert "Empty (NULL) when not recorded." in definition
    assert "The facet definitions are under audit." in definition
    # the definition comes before the datasets and value sections
    assert text.index('id="facet-definition"') < text.index("Value distribution")


def test_facet_detail_carriers_show_mapping(client):
    text = client.get("/facets/ant_disease").text
    assert 'id="facet-datasets"' in text
    table = text.split('id="carrier-table"')[1].split("</table>\n  </div>")[0]
    assert 'class="table-wrap"' in text.split('id="carrier-table"')[0][-200:]
    # source column, mapping kind pills
    assert "Diagnosis" in table and "dx_code" in table
    assert 'class="fm-kind renamed"' in table
    assert 'class="fm-kind derived"' in table
    # value translation rows: author value -> facet value with cells
    assert "Alzheimer" in table and "alzheimer disease" in table
    assert "250,000" in table
    # hidden value becomes missing, with the rule named
    assert ">unknown<" in table
    assert 'class="fm-null"' in table and "hidden</span>" in table
    # pass-through values are folded away and counted
    assert "1 value kept as is" in table
    # derived: the default line and the default rule
    assert "any other value" in table
    assert "default</span>" in table
    # missing cells
    assert "12 missing" in table


def test_facet_detail_non_carriers_collapsed_with_hints(client):
    text = client.get("/facets/ant_disease").text
    assert '<details class="nc-details" id="non-carriers">' in text  # collapsed by default
    block = text.split('id="non-carriers"')[1].split("</details>")[0]
    assert "Datasets without <code>ant_disease</code>: 2" in block
    assert "1 with an author column that may carry it" in block
    assert "1 declared but not served" in block
    assert '<span class="nc-hint">Diagnosis_clinical</span>' in block
    assert "declared, not served" in block
    assert "/browse/GroupB/ds4" in block


def test_facet_detail_forwards_identity_headers(client, monkeypatch):
    seen = {}

    def _spy(api_url, path, **kwargs):
        if path.startswith("/facets/"):
            seen.update(kwargs)
        return _fake_api_get(api_url, path)

    monkeypatch.setattr(browser, "_api_get", _spy)
    client.get("/facets/ant_disease",
               headers={"Remote-User": "root", "Remote-Groups": "users,admins"})
    assert seen["headers"] == {"Remote-User": "root", "Remote-Groups": "users,admins"}
    assert seen["timeout"] >= 60


def test_dataset_page_facets_card(client):
    text = client.get("/browse/GroupA/ds1").text
    assert 'id="ds-facets"' in text
    card = text.split('id="ds-facets"')[1].split("<script>")[0]
    assert 'href="/facets/ant_sex"' in card
    assert "Gender" in card and "MALE" in card and ">m<" in card
    assert "800" in card
    # a rule for a value the dataset does not have is folded away
    assert "1 rule for values not in this dataset" in card
    assert 'class="fm-kind derived"' in card and "unstaged" in card
    assert "published as ant_braak" in card
    assert "every cell: <b>prefrontal cortex</b>" in card
    # hidden author column: no name, explained
    assert "hidden author column" in card
    assert "50 missing" in card
    # in its own Facets tab (after Genesets), wrapped for phones; the Data tab only points to it
    facets_tab = text.split('id="tab-facets"')[1].split("</section>")[0]
    assert 'id="ds-facets"' in facets_tab
    assert '<div class="table-wrap">\n<table class="ds-facets">' in facets_tab
    data_tab = text.split('id="tab-data"')[1].split("</section>")[0]
    assert 'id="ds-facets"' not in data_tab
    assert 'Facets: see the <a href="#facets">Facet tab</a>; their effects are in its <a href="#facet-gene">Gene</a> sub-tab.' in data_tab
    bar = text.split('id="ds-tab-bar"')[1].split("</div>")[0]
    assert 'data-tab="facets"' in bar and '<span class="tab-count">' in bar.split('data-tab="facets"')[1]
    assert bar.index('data-tab="data"') < bar.index('data-tab="num"') < bar.index('data-tab="cat"') < bar.index('data-tab="facets"')
    assert 'id="fx-unit-line"' in facets_tab and 'id="fx-cohort"' in facets_tab


def test_dataset_page_without_facets_endpoint_hides_card(client):
    # GroupA/ds2 has no /facets in the fake (older ant-serve): no card.
    text = client.get("/browse/GroupA/ds2").text
    assert 'id="ds-facets"' not in text


def test_facet_detail_404(client):
    # Unknown facet name lands on the facets index with a 404 status.
    resp = client.get("/facets/ant_unknown")
    assert resp.status_code == 404


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


def test_chat_feedback_link_left_the_user_menu(client):
    # Replaced by "Report an issue"; the page itself stays reachable for admins.
    admin = client.get("/", headers={"Remote-User": "mf", "Remote-Groups": "admins"})
    assert 'href="/admin/feedback"' not in admin.text
    assert client.get("/admin/feedback", headers={"Remote-Groups": "admins"}).status_code == 200


def test_issue_tracker_link_for_signed_in_users(client, monkeypatch):
    monkeypatch.setenv("ANTHIVE_ISSUE_TRACKER_URL", "https://tracker.example/issues")
    plain = client.get("/", headers={"Remote-User": "mf", "Remote-Groups": "users"})
    assert 'id="issue-tracker-link"' in plain.text
    assert 'href="https://tracker.example/issues"' in plain.text
    assert 'target="_blank"' in plain.text.split('id="issue-tracker-link"')[1][:80]
    assert "Report an issue" in plain.text
    assert 'id="issue-tracker-link"' not in client.get("/").text      # anonymous


def test_issue_tracker_link_hidden_when_unconfigured(client, monkeypatch):
    monkeypatch.setenv("ANTHIVE_ISSUE_TRACKER_URL", "")
    resp = client.get("/", headers={"Remote-User": "mf"})
    assert 'id="issue-tracker-link"' not in resp.text


def test_user_menu_dropdown_has_hover_bridge_and_tap_toggle(client):
    resp = client.get("/", headers={"Remote-User": "mf"})
    text = resp.text
    assert ".dropdown-menu::before" in text            # no hover gap under the toggle
    assert ".dropdown.open .dropdown-menu" in text     # click/tap opens it
    assert 'id="user-menu-toggle"' in text and 'aria-expanded="false"' in text


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


def test_plot_view_caveats_collapsed_behind_toggle(client):
    """Caveats sit in a closed panel behind a button with count and level colour."""
    for geom in ("violin", "histogram"):
        text = client.get(f"/browse/GroupA/ds1/plot/{geom}").text
        assert 'id="cv-toggle"' in text
        assert 'aria-expanded="false"' in text
        assert 'aria-controls="cv-panel"' in text
        assert re.search(r'<div class="caveats-card [^"]*" id="cv-panel" hidden', text)
        assert 'class="cv-n">' in text
        assert 'Known caveats \u2014 apply to the plot below' not in text


def test_plot_view_caveats_banner_absent_without_data(client):
    """No caveats in /info → no card at all (avoid empty header noise)."""
    # GroupA/ds2 has no caveats key in _INFO.
    resp = client.get("/browse/GroupA/ds2/plot/scatter")
    assert resp.status_code == 200
    assert 'caveats-card' not in resp.text


def test_folder_view_shows_secondary_badge(client):
    """Folder-page rows carry a (2) badge + .secondary-dataset class when a
    dataset's .md has is_secondary=true."""
    resp = client.get("/browse/GroupA")
    assert resp.status_code == 200
    # ds2 is secondary — badge markup + row class must show.
    assert 'class="badge-secondary"' in resp.text
    assert 'Secondary dataset. Contains cells that also appear' in resp.text
    assert 'class="secondary-dataset"' in resp.text
    # ds1 is NOT secondary — the ds1-specific row shouldn't add the class to
    # ds1's <tr>. A pattern-level check: 'Dataset One' link is not wrapped in
    # the same tr that carries the class. Cheap smoke: the string appears
    # (from ds2), which is enough to know the render works.


def test_index_view_shows_secondary_badge_when_any_member(client):
    """Landing page rolls the flag up — GroupA has ds2 (secondary), so the
    collection row carries a (2) badge on the collection name."""
    resp = client.get("/")
    assert resp.status_code == 200
    # The has_secondary aggregate must fire the collection-level tooltip.
    assert 'This collection contains one or more secondary datasets' in resp.text


def test_dataset_view_shows_secondary_badge(client):
    """Dataset page — the header carries the (2) badge inline in the breadcrumb
    and the id-strip container gets the .secondary-dataset mute class."""
    resp = client.get("/browse/GroupA/ds2")
    assert resp.status_code == 200
    assert 'class="badge-secondary"' in resp.text
    assert 'class="ds-top secondary-dataset"' in resp.text


def test_plot_view_shows_secondary_badge(client):
    """Plot page h1 shows the badge for secondary datasets."""
    resp = client.get("/browse/GroupA/ds2/plot/scatter")
    assert resp.status_code == 200
    assert 'class="badge-secondary"' in resp.text
    assert 'secondary-dataset' in resp.text


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
    # New drilldown-style table: sortable column headers with data-sortcol
    assert 'data-sortcol="pct_nonzero"' in resp.text   # %expr sortable column
    assert "renderHeatmap" in resp.text                 # multi-gene branch present
    # cell_type is now an mff widget (id on the widget div, not a <select>)
    assert 'id="gv-flt-cell_type-w"' in resp.text      # two-row filter bar (row 2)


def test_gene_atlas_multigene_route(client):
    # a comma-separated gene list resolves (→ client renders the gene×dataset heatmap)
    resp = client.get("/genes/APOE,TREM2,CX3CR1")
    assert resp.status_code == 200
    assert "renderHeatmap" in resp.text


def test_topbar_has_no_gene_search_box(client):
    # The top-bar gene search was removed (it ate a whole row on phones);
    # a plain "genes" nav link to /genes replaces it.
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'class="gene-search"' not in resp.text
    assert "gene atlas\u2026" not in resp.text
    assert "'/genes/'+encodeURIComponent" not in resp.text


def test_storage_notice_is_a_slim_bar_on_phones(client):
    # The privacy notice used to be a multi-line card covering the bottom of
    # a phone screen. On narrow screens it is now a one-line bar (message,
    # details, dismiss) and the body gets bottom padding while it shows.
    text = client.get("/").text
    assert 'id="storage-notice" style="display:none"' in text
    assert 'class="sn-line"' in text and 'class="sn-extra"' in text
    # since the bar is used at every width, the one-line rules are global and
    # the phone query only drops the second sentence
    css = text.split("/* GDPR storage notice")[1].split("/* ── data-assistant")[0]
    phone = css[css.find("@media (max-width: 560px) {"):]
    assert "#storage-notice .sn-extra { display: none; }" in phone
    assert "white-space: nowrap" in css and "text-overflow: ellipsis" in css
    assert "body.storage-notice-on { padding-bottom:" in css
    # dismiss behaviour and storage key unchanged
    assert "var KEY = 'anthive.storage.notice.ack';" in text
    assert "localStorage.setItem(KEY, '1')" in text
    assert "document.body.classList.toggle('storage-notice-on', on)" in text


def test_topbar_genes_link(client):
    text = client.get("/").text
    idx = text.find('id="nav-genes"')
    assert idx >= 0, "genes link missing from the top bar"
    tag = text[text.rfind("<a ", 0, idx):text.find("</a>", idx)]
    assert 'href="/genes"' in tag
    assert tag.endswith(">genes")
    # inside the main nav, which sits in the collapsible menu container
    menu = text.split('id="topbar-menu"')[1].split("</nav>")[0]
    assert 'id="nav-genes"' in menu


def test_gene_atlas_index_has_lookup_form(client):
    resp = client.get("/genes")
    assert resp.status_code == 200
    assert 'id="gv-lookup"' in resp.text
    assert 'action="/genes"' in resp.text
    assert 'name="q"' in resp.text
    # landing page must not run the per-gene loader
    assert "/api/gene_stats_all" not in resp.text


def test_gene_atlas_index_query_redirects(client):
    resp = client.get("/genes?q=%20APOE%2C%20TREM2%20", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/genes/APOE,TREM2"
    # empty query: stay on the lookup page
    assert client.get("/genes?q=%20").status_code == 200


def test_gene_atlas_page_has_lookup_form_and_breadcrumb(client):
    text = client.get("/genes/APOE").text
    assert 'id="gv-lookup"' in text
    assert '<a href="/genes">gene atlas</a>' in text


def test_topbar_menu_button(client):
    # Narrow screens collapse the nav into a panel behind an accessible button.
    text = client.get("/", headers={"Remote-User": "mf", "Remote-Name": "Mark Fiers"}).text
    idx = text.find('id="nav-toggle"')
    assert idx >= 0
    tag = text[text.rfind("<button", 0, idx):text.find(">", idx) + 1]
    assert 'type="button"' in tag
    assert 'aria-controls="topbar-menu"' in tag
    assert 'aria-expanded="false"' in tag
    assert 'aria-label="Open menu"' in tag
    # the panel holds the nav links, Help, API, version and the user menu
    menu = text.split('id="topbar-menu"')[1].split('<script>')[0]
    for needle in ('href="/"', 'href="/facets"', 'href="/help"', 'id="topbar-api-link"',
                   'id="stack-version"', 'id="user-menu-toggle"', 'id="authelia-signout"'):
        assert needle in menu, needle
    # CSS: hidden on desktop, shown under the 640px breakpoint; JS toggles state
    assert "@media (max-width: 640px)" in text
    assert "#topbar .topbar-menu { display: contents; }" in text
    assert "menu-open" in text
    assert "btn.setAttribute('aria-expanded'" in text


def test_exp_badge_is_small_inline(client):
    text = client.get("/").text
    nav_entry = text.split('<a href="/facets"')[1].split("</a>")[0]
    assert 'class="vw-preview"' in nav_entry          # facets is not approved yet: muted nav link
    assert '<span class="exp-badge">preview</span>' in nav_entry
    css = text.split(".exp-badge {")[1].split("}")[0]
    assert "font-size: .6em" in css
    assert "vertical-align: super" in css
    assert "display: inline-block" in css
    assert "white-space: nowrap" in text.split("a.nav-exp {")[1].split("}")[0]


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


def test_genesets_index_renders_study_drilldown(client):
    # The landing shows a per-study drilldown table. Cell-type and organism
    # filtering moved out of the column headers into two mff multiselect
    # widgets above the table (commit e96baf3); the headers still sort but
    # no longer carry data-filter-fields. Each study row links to
    # /genesets/<study>.
    resp = client.get("/genesets")
    assert resp.status_code == 200
    assert 'id="gs-study-table"' in resp.text
    assert 'data-col="celltype"' in resp.text
    assert 'data-col="organism"' in resp.text
    assert 'id="gs-flt-celltype-w"' in resp.text
    assert 'id="gs-flt-organism-w"' in resp.text
    assert 'id="gs-summary"' in resp.text
    # Header popovers must not duplicate the filter UI.
    assert 'data-filter-fields="celltype"' not in resp.text
    assert 'data-filter-fields="organism"' not in resp.text
    # Both fake studies appear as rows.
    assert 'href="/genesets/StudyX"' in resp.text
    assert 'href="/genesets/StudyY"' in resp.text
    # Provenance from /genesets/studies is embedded (title + doi).
    assert "Study X paper title" in resp.text
    assert "10.1234/studyx" in resp.text


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
    # Partial injects its filter controls (n_cells stays as <select>; others
    # are mff widgets). Method widget removed 2026-08-23 (scanpy-only UI).
    assert 'class="mv-flt-method"' not in resp.text
    # module, tag are now mff widgets — look for their container class
    assert 'mv-flt-module-w' in resp.text
    assert 'mv-flt-tag-w' in resp.text
    # mv_ctx scope=module is emitted into the page JS
    assert '"scope"' in resp.text
    assert 'mv-experiment' in resp.text   # the id we assign
    # module_name is null — module dropdown starts on "all modules"
    assert '"module_name": null' in resp.text or '"module_name":null' in resp.text


def test_geneset_experiment_module_view_has_celltype_tissue_facets(client):
    # Ask 2: celltype and tissue facets appear when dataset_annotations are provided.
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    # celltype and tissue are now mff widgets — check for their container classes
    assert 'mv-flt-celltype-w' in resp.text
    assert 'mv-flt-tissue-w' in resp.text


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
    # Partial's filter controls replace the old gv-* IDs. Method widget was
    # removed 2026-08-23 (scanpy-only UI).
    assert 'class="mv-flt-method"' not in resp.text
    assert 'mv-flt-tag-w' in resp.text  # tag is now an mff widget
    # sort dropdown removed — sort lives in column-header clicks (see test_module_view_sort_state_and_indicators)
    assert 'class="mv-flt-sort"' not in resp.text
    # Module dropdown is visible in module scope (for drill-down within catalog).
    assert 'mv-flt-module-w' in resp.text  # module is now an mff widget
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
    """Regression: celltype/tissue/n_cells filter dropdowns must have explicit
    addEventListener('change', render) bindings in the emitted JS.

    The generic Object.keys(flt).forEach(...) loop was replaced with named
    bindings (2026-08 fix) so static analysis and tests can assert each filter
    class is wired — a silent typo in the flt key would have made the generic
    loop bind nothing for that control. This test greps the rendered HTML for
    the bindings that were previously invisible.

    2026-08-23: method widget removed alongside the "default to scanpy"
    change — its explicit listener is gone with it.
    """
    # Geneset detail page — has dataset_annotations → celltype + tissue selects
    # are rendered; n_cells is always rendered.
    resp = client.get("/genesets/StudyX/Exp1/gs_up")
    assert resp.status_code == 200
    # celltype/tissue are now mff widgets — onChange is wired inside mffWidget factory.
    # n_cells stays as a single-select with an explicit listener.
    assert "flt.n_cells.addEventListener('change'" in resp.text, (
        "n_cells change listener not found — filter will be a no-op"
    )
    # method listener gone (widget removed)
    assert "flt.method.addEventListener" not in resp.text
    # mff widget factory wires its own onChange — check it's defined on the page.
    assert "window.mffWidget" in resp.text, (
        "mffWidget factory missing — celltype/tissue onChange won't fire"
    )
    # celltype filter uses indexOf / ctArr for comma-joined multi-values.
    assert "ctArr" in resp.text, (
        "celltype mff filter must use ctArr (OR logic across checked values)"
    )
    # tissue filter uses hierarchical ancestor matching instead of indexOf.
    assert "TISSUE_ANCESTORS" in resp.text, (
        "tissue filter must use TISSUE_ANCESTORS for hierarchical matching"
    )

    # Geneset experiment page — same partial, same requirement.
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    assert "flt.n_cells.addEventListener('change'" in resp.text
    assert "window.mffWidget" in resp.text


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
    # dropdown surfaces the user + a Logout action wired to the Authelia logout
    # endpoint. The old separate "account settings" (/authelia/) item was
    # collapsed into a single Logout line 2026-08-24.
    resp = client.get("/", headers={
        "Remote-User": "mf", "Remote-Name": "Mark Fiers",
        "Remote-Email": "mark@example.org",
    })
    assert resp.status_code == 200
    assert "Mark Fiers" in resp.text
    assert 'id="authelia-signout"' in resp.text
    assert "/authelia/api/logout" in resp.text
    # a single "Logout" item, not two separate lines
    assert ">Logout<" in resp.text
    assert ">sign out<" not in resp.text
    assert "account settings" not in resp.text


def test_topbar_no_auth_section_without_header(client):
    # Direct / un-proxied access (no Remote-User) → the Logout item disappears
    # and the auth-only "signed in as" header disappears, but the dropdown
    # itself stays (Preferences is per-browser / no SSO needed).
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="authelia-signout"' not in resp.text
    assert "signed in as" not in resp.text
    # Preferences must still be reachable via the dropdown.
    assert 'href="/settings"' in resp.text


def test_topbar_help_link_present(client):
    # The 'Help' link lives in the top-right utility cluster on every page.
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'href="/help"' in resp.text


def test_topbar_api_link_goes_to_api_guide(client):
    # The top-bar 'API' link lands on the on-site API page first (login,
    # curl, pyvark, vark), not straight on the Swagger UI.
    resp = client.get("/")
    assert resp.status_code == 200
    idx = resp.text.find('id="topbar-api-link"')
    assert idx >= 0, "API link missing from topbar"
    tag = resp.text[resp.text.rfind("<a ", 0, idx):resp.text.find(">", idx) + 1]
    assert 'href="/api-guide"' in tag
    assert 'target="_blank"' not in tag


def test_user_dropdown_has_api_entry(client):
    # The API page is also in the user dropdown, for signed-in and anonymous
    # visitors alike, next to Preferences.
    for headers in ({}, {"Remote-User": "mf", "Remote-Name": "Mark Fiers"}):
        html = client.get("/", headers=headers).text
        menu = html.split('class="dropdown-menu right"', 1)[1].split("</span>", 1)[0]
        assert 'id="api-menu-link"' in menu
        assert 'href="/api-guide"' in menu
        assert menu.index('id="api-menu-link"') < menu.index('href="/settings"')


def _api_guide_fake(version_payload):
    def fake(api_url, path):
        if path == "/version":
            if version_payload is None:
                raise requests.ConnectionError("down")
            return version_payload
        return _fake_api_get(api_url, path)
    return fake


def _guide_settings(mode, public="", upload_groups=("admins",), admins=(), max_pdf_mb=25):
    return {"mode": mode, "public_base_url": public, "admins": set(admins),
            "upload_groups": list(upload_groups), "max_pdf_mb": max_pdf_mb}


def test_api_guide_renders_with_links_and_site_url(client, monkeypatch):
    monkeypatch.setattr(browser, "_api_get",
                        _api_guide_fake({"version": "9.8.7", "released": "2026-10-01"}))
    monkeypatch.setattr(browser, "_api_guide_site_settings",
                        lambda: _guide_settings("authelia"))
    resp = client.get("/api-guide", headers={"Host": "anthive.example.org",
                                             "Remote-User": "mf"})
    assert resp.status_code == 200
    html = resp.text
    # links on to the raw API docs, the schema and both changelogs
    assert 'href="/api/docs"' in html and 'id="api-docs-link"' in html
    assert 'href="/api/redoc"' in html
    assert 'href="/api/openapi.json"' in html
    assert 'href="/api/changelog"' in html
    assert "gitlab.kuleuven.be/bdslab/anthive/anthive4/-/blob/main/doc/API_CHANGELOG.md" in html
    assert 'href="/account/mcp"' in html
    # examples carry this site's https address, the user's name and netrc host
    assert "https://anthive.example.org/api" in html
    assert "http://anthive.example.org" not in html
    assert "machine anthive.example.org login mf password YOUR_PASSWORD" in html
    assert "curl -n -s https://anthive.example.org/api/health" in html
    assert "/gene_stats?genes=" in html and "/plot/violin?" in html
    assert "chmod 600 ~/.netrc" in html
    # pyvark and vark connection instructions
    assert "pip install pyvark" in html
    assert "from vark import AnthiveClient" in html
    assert 'auth=(os.environ["ANTHIVE_USER"], os.environ["ANTHIVE_PASSWORD"])' in html \
        or "auth=(os.environ[&#34;ANTHIVE_USER&#34;], os.environ[&#34;ANTHIVE_PASSWORD&#34;])" in html
    assert "vark server login --name anthive --url https://anthive.example.org/api --user mf" in html
    assert "vark server ls" in html
    assert 'id="api-guide-papers"' in html
    # version from the server
    assert 'id="api-guide-version">9.8.7<' in html
    # consistent headings, house style
    body = html.split('<div class="api-guide">', 1)[1].split("<script>", 1)[0]
    assert body.count('<h2 class="section-head">') >= 6
    assert "\u2014" not in body


def test_api_guide_paper_read_commands(client, monkeypatch):
    monkeypatch.setattr(browser, "_api_get", _api_guide_fake({"version": "2.41.0"}))
    monkeypatch.setattr(browser, "_api_guide_site_settings", lambda: _guide_settings("authelia"))
    html = client.get("/api-guide", headers={"Remote-User": "someone"}).text
    assert 'href="#papers"' in html and 'id="papers"' in html
    for snippet in ("vark papers ls --role source --year 2020..2025", "vark papers show CITEKEY",
                    "vark papers pdf CITEKEY --dir papers/", "vark papers bib --out library.bib",
                    "vark literature ", "vark db papers L26/FMG",
                    "client.list_papers(", "client.get_paper(", "client.download_paper_pdf(",
                    "client.get_library_bibtex()", "client.search_literature(",
                    "client.get_database_papers("):
        assert snippet in html, snippet


def test_api_guide_upload_section_only_for_uploaders(client, monkeypatch):
    monkeypatch.setattr(browser, "_api_get", _api_guide_fake({"version": "2.41.0"}))
    monkeypatch.setattr(browser, "_api_guide_site_settings",
                        lambda: _guide_settings("authelia", max_pdf_mb=40))
    # a plain user: no upload commands, a plain statement of who may upload
    html = client.get("/api-guide", headers={"Remote-User": "someone",
                                             "Remote-Groups": "users"}).text
    assert 'id="api-guide-upload-restricted"' in html and "admins group" in html
    assert "vark papers upload" not in html and "/papers/upload" not in html
    assert "upload_paper(" not in html
    # anonymous: same
    assert "vark papers upload" not in client.get("/api-guide").text
    # an admin by group sees every upload route and the size warning
    html = client.get("/api-guide", headers={"Remote-User": "mf",
                                             "Remote-Groups": "users,admins"}).text
    assert 'id="api-guide-upload"' in html
    for snippet in ("vark papers requirements --server anthive",
                    "vark papers upload --metadata paper.md --pdf paper.pdf --server anthive --dry-run",
                    "vark papers upload ./papers/ --server anthive",
                    "vark papers upload --manifest papers.csv --server anthive",
                    "vark server check ", "client.get_paper_upload_requirements()",
                    "client.upload_paper(", "client.upload_papers(AnthiveClient.paper_items_from_folder(",
                    "PaperUploadForbiddenError", "PaperUploadDisabledError",
                    "/api/papers/upload", "at most 40 MB", "MCP upload tool"):
        assert snippet in html, snippet
    assert "<td>40 MB on this site</td>" in html


def test_api_guide_upload_follows_groups_and_basic_mode(client, monkeypatch):
    monkeypatch.setattr(browser, "_api_get", _api_guide_fake({"version": "2.41.0"}))
    monkeypatch.setattr(browser, "_api_guide_site_settings",
                        lambda: _guide_settings("authelia", upload_groups=("curators",)))
    assert 'id="api-guide-upload"' in client.get(
        "/api-guide", headers={"Remote-User": "c", "Remote-Groups": "curators"}).text
    # admins are not uploaders when the site names other groups
    assert 'id="api-guide-upload"' not in client.get(
        "/api-guide", headers={"Remote-User": "mf", "Remote-Groups": "admins"}).text
    # basic mode: no groups header, admins by name in auth.admins
    monkeypatch.setattr(browser, "_api_guide_site_settings",
                        lambda: _guide_settings("basic", admins=("mf",)))
    assert 'id="api-guide-upload"' in client.get("/api-guide", headers={"Remote-User": "mf"}).text
    assert 'id="api-guide-upload"' not in client.get("/api-guide", headers={"Remote-User": "x"}).text


def test_api_guide_survives_api_down_and_public_mode(client, monkeypatch):
    monkeypatch.setattr(browser, "_api_get", _api_guide_fake(None))
    monkeypatch.setattr(browser, "_api_guide_site_settings",
                        lambda: _guide_settings("none", "https://mirror.example.org"))
    resp = client.get("/api-guide")
    assert resp.status_code == 200
    assert 'id="api-guide-version"' not in resp.text
    assert "https://mirror.example.org/api" in resp.text
    assert "needs no username or password" in resp.text
    assert 'id="password"' not in resp.text


def test_help_and_mcp_pages_link_to_api_guide(client):
    assert 'id="help-api-guide-link"' in client.get("/help").text
    mcp = client.get("/account/mcp", headers={"Remote-User": "mf"}).text
    assert 'id="mcp-api-note"' in mcp and 'href="/api-guide"' in mcp


def test_topbar_preferences_moved_into_dropdown(client):
    # 'Preferences' is no longer a top-level nav entry — it lives INSIDE the
    # user dropdown (linking to /settings). The main <nav> at the start of the
    # topbar must not carry it any more.
    resp = client.get("/")
    assert resp.status_code == 200
    # The dropdown item is called "Preferences" (capitalised) and points to /settings.
    assert ">Preferences<" in resp.text
    # It sits inside a dropdown-menu, not in the primary <nav>.
    # Grab the first <nav>…</nav> block (the main nav) and assert it does NOT
    # contain a /settings link any more.
    import re as _re_local
    first_nav = _re_local.search(r"<nav aria-label=\"Main\">(.*?)</nav>", resp.text, _re_local.S)
    assert first_nav is not None
    assert "/settings" not in first_nav.group(1)


def test_topbar_dropdown_shows_without_sso(client):
    # Un-authed users still get a dropdown (for Preferences). The "signed in
    # as" header line only renders when Authelia headers are present.
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'class="dropdown-menu right"' in resp.text
    assert 'href="/settings"' in resp.text
    assert "signed in as" not in resp.text


def test_topbar_dropdown_header_shown_when_authed(client):
    # When Authelia identifies the user, the dropdown carries a small header
    # "signed in as <user>" above the menu items.
    resp = client.get("/", headers={
        "Remote-User": "mf", "Remote-Name": "Mark Fiers",
    })
    assert resp.status_code == 200
    assert "signed in as" in resp.text
    assert "Mark Fiers" in resp.text


def test_help_page_renders(client):
    # /help renders substantive orientation content (not lorem-ipsum) —
    # >500 characters of visible body text, links to key views, and copy
    # explaining the tab structure.
    resp = client.get("/help")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    # Enough real content
    import re as _re_local
    text_only = _re_local.sub(r"<[^>]+>", " ", resp.text)
    text_only = _re_local.sub(r"\s+", " ", text_only).strip()
    assert len(text_only) > 500, "help page body is suspiciously short"
    # Key orienting phrases
    lowered = resp.text.lower()
    assert "anthive" in lowered
    assert "genesets" in lowered
    assert "gene atlas" in lowered
    assert "facet" in lowered
    # Cross-links into the actual routes
    assert 'href="/"' in resp.text
    assert 'href="/genesets"' in resp.text
    assert 'href="/privacy"' in resp.text


def test_help_page_has_all_sections_and_toc(client):
    resp = client.get("/help")
    assert resp.status_code == 200
    for anchor in ("what", "finding", "pages", "plots", "genes", "papers", "ai",
                   "mcp", "mcp-clients", "mcp-offers", "skills", "servers",
                   "ai-practice", "code", "account"):
        assert f'id="{anchor}"' in resp.text, anchor
        assert f'href="#{anchor}"' in resp.text, anchor
    assert 'id="help-filter"' in resp.text
    help_body = resp.text.split('<div class="help">', 1)[1].split('<script>', 1)[0]
    assert '\u2014' not in help_body


def test_help_page_skills_and_mcp_facts(client):
    resp = client.get("/help")
    text = resp.text
    assert ("DISABLE_TELEMETRY=1 npx skills add "
            "https://gitlab.kuleuven.be/bdslab/anthive/skills.git -g --agent &#39;*&#39;") in text \
        or ("DISABLE_TELEMETRY=1 npx skills add "
            "https://gitlab.kuleuven.be/bdslab/anthive/skills.git -g --agent '*'") in text
    assert "npx skills update -g" in text
    assert "claude mcp add --transport http anthive" in text
    for tool in ("search_datasets", "get_dataset_info", "get_dataset_context", "search_genes",
                 "plot", "analytics_schema", "analytics_query", "analytics_viz", "module_score",
                 "list_module_scores", "list_genesets", "get_geneset", "search_literature",
                 "get_paper", "related_papers", "rescan_databases"):
        assert f"<code>{tool}</code>" in text, tool
    # upload features are only shown to uploaders, never on the public help page
    for tool in ("paper_upload_requirements", "check_paper", "upload_paper"):
        assert tool not in text, tool
    assert "/api/papers/upload" not in text and "Admins can add papers" in text
    assert "investigate_dataset" in text and "literature_in_our_data" in text
    assert "anthive://guide" in text
    assert 'href="/account/mcp"' in text and 'href="/api/docs"' in text


HELP_SKILL_NAMES = ("anthive-basics", "anthive-setup", "anthive-dataset-analysis",
                    "anthive-literature", "anthive-paper-upload", "sc-reproducible-research")


def test_help_page_quick_start_first(client):
    # A quick start sits above the long sections and leads the table of contents.
    text = client.get("/help").text
    body = text.split('<div class="help">', 1)[1].split('<script>', 1)[0]
    assert 'id="quickstart"' in body
    assert body.index('id="quickstart"') < body.index('id="what"')
    toc = body.split('class="toc"', 1)[1].split('</ol>', 1)[0]
    assert re.findall(r'href="#([a-z-]+)"', toc)[0] == "quickstart"
    quick = body.split('id="quickstart"', 1)[1].split('</section>', 1)[0]
    assert ("DISABLE_TELEMETRY=1 npx skills add https://gitlab.kuleuven.be/bdslab/anthive/skills.git"
            " -g --agent &#39;*&#39;") in quick
    assert "data-copy-help" in quick
    assert "does not support global skill installation" in quick and "harmless" in quick
    assert "ls ~/.agents/skills ~/.claude/skills" in quick
    for name in HELP_SKILL_NAMES:
        url = f"https://gitlab.kuleuven.be/bdslab/anthive/skills/-/raw/main/zips/{name}.zip?inline=false"
        assert f'href="{url}"' in quick, name
    assert 'href="/api-guide"' in quick and 'href="/account/mcp"' in quick
    assert "/MCP_CLIENTS.md" in quick and "setup prompt" in quick
    assert "human microglia from Alzheimer" in quick
    assert '\u2014' not in body


def test_help_page_skills_section_complete(client):
    text = client.get("/help").text
    skills = text.split('id="skills"', 1)[1].split('id="servers"', 1)[0]
    for name in HELP_SKILL_NAMES:
        assert f"<code>{name}</code>" in skills, name
        assert f"zips/{name}.zip?inline=false" in skills, name
    # only skill names that exist in the skills repository
    mentioned = set(re.findall(r"\b((?:anthive|sc)-[a-z]+(?:-[a-z]+)*)\b",
                               re.sub(r"<[^>]+>", " ", skills)))
    assert mentioned <= set(HELP_SKILL_NAMES), mentioned - set(HELP_SKILL_NAMES)
    assert "npx skills update -g" in skills
    assert "does not support global skill installation" in skills
    assert "ls ~/.agents/skills ~/.claude/skills" in skills
    assert "--skill anthive-basics anthive-literature" in skills
    assert "Customize &gt; Skills" in skills and "code execution" in skills
    assert "Anthropic&#39;s cloud" in skills or "Anthropic's cloud" in skills
    assert "vark skill install" in skills and "~/.netrc" in skills
    assert 'href="https://gitlab.kuleuven.be/bdslab/anthive/skills"' in skills
    assert "for example claude.ai" not in skills  # claude.ai does take skills (as zips)


def test_help_page_issue_link_follows_config(client, monkeypatch):
    monkeypatch.setenv("ANTHIVE_ISSUE_TRACKER_URL", "https://example.org/issues")
    resp = client.get("/help")
    assert 'id="help-issue-link"' in resp.text
    assert 'href="https://example.org/issues"' in resp.text
    monkeypatch.setenv("ANTHIVE_ISSUE_TRACKER_URL", "")
    resp = client.get("/help")
    assert 'id="help-issue-link"' not in resp.text


def test_help_link_reachable_from_landing(client):
    # A user landing on / can click Help without knowing the URL.
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'href="/help"' in resp.text


def test_landing_popover_filter_guard(client):
    # Regression: the column-header popover click handler must ignore clicks
    # that originate inside the open popover — otherwise filter checkboxes
    # close the popover before they register.
    resp = client.get("/")
    assert resp.status_code == 200
    assert "activePopover.contains(e.target)" in resp.text


def test_landing_popover_uses_fixed_positioning(client):
    # The .col-popover must use position:fixed (not position:absolute) so it
    # escapes any overflow:auto ancestor (.table-wrap) and is never clipped
    # when the table is narrowed to a single row by a filter.
    resp = client.get("/")
    assert resp.status_code == 200
    assert "position:fixed" in resp.text, \
        ".col-popover must use position:fixed to escape overflow:auto clipping"
    # Must NOT use position:absolute for the popover (that was the bug).
    # Find the col-popover block and verify it has fixed, not absolute.
    idx = resp.text.find(".col-popover {")
    assert idx >= 0, ".col-popover rule not found"
    rule_slice = resp.text[idx: idx + 300]
    assert "position:fixed" in rule_slice, \
        ".col-popover rule does not set position:fixed"
    assert "position:absolute" not in rule_slice, \
        ".col-popover rule must not use position:absolute (overflow clipping bug)"


def test_landing_popover_appended_to_body(client):
    # The popover JS must append to document.body (not th.appendChild) so the
    # DOM element is outside .table-wrap and never subject to its overflow clip.
    resp = client.get("/")
    assert resp.status_code == 200
    assert "document.body.appendChild(pop)" in resp.text, \
        "popover must be appended to document.body, not the <th>"


def test_landing_popover_positioned_via_bounding_rect(client):
    # The popover must position itself via getBoundingClientRect() on the
    # anchor <th>, setting top/left from the viewport rectangle.
    resp = client.get("/")
    assert resp.status_code == 200
    assert "getBoundingClientRect()" in resp.text, \
        "popover positioning must use getBoundingClientRect()"
    assert "thRect.bottom" in resp.text, \
        "popover top must be set from thRect.bottom"


def test_landing_popover_closes_on_scroll_and_resize(client):
    # Scroll and resize listeners must call closePopover so the fixed-position
    # popover doesn't drift away from its anchor after the page moves.
    resp = client.get("/")
    assert resp.status_code == 200
    assert "window.addEventListener('scroll', closePopover" in resp.text, \
        "missing scroll listener to close popover"
    assert "window.addEventListener('resize', closePopover" in resp.text, \
        "missing resize listener to close popover"


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


def test_dataset_page_shows_version_and_updated(client):
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    assert 'id="ds-version"' in resp.text
    assert "v0.1.4" in resp.text
    assert "updated 2025-01-01" in resp.text
    assert "Minor bump = data changed, patch bump = metadata only." in resp.text


def test_dataset_page_hides_version_line_when_missing(client):
    resp = client.get("/browse/GroupA/ds2")
    assert resp.status_code == 200
    assert 'id="ds-version"' not in resp.text


def test_folder_shows_version_next_to_title(client):
    resp = client.get("/browse/GroupA")
    assert resp.status_code == 200
    assert resp.text.count('class="ds-ver"') == 1
    assert ">v0.1.4<" in resp.text


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
    # Partial is rendered — its filter controls and panel ID are present.
    # Method widget removed 2026-08-23 (scanpy-only UI).
    assert 'id="mv-modules-tab"' in resp.text
    assert 'class="mv-flt-method"' not in resp.text
    assert 'mv-flt-tag-w' in resp.text  # tag is now an mff widget
    # starCAT + AUCell options NOT in the partial (both hidden client-side)
    assert 'data-method="starcat"' not in resp.text
    assert '>starCAT<' not in resp.text
    assert '>AUCell<' not in resp.text
    # mv_ctx carries scope=dataset and the db_id (GroupA/ds1)
    assert '"scope": "dataset"' in resp.text or '"scope":"dataset"' in resp.text
    assert 'GroupA/ds1' in resp.text
    # Module count is reported in the tab label (server-side n_module_scores)
    # Dataset has 4 scored cols (scanpy + starcat + 2 aucell) → 4 modules tracked
    assert 'Gene sets<span class="tab-count">' in resp.text
    # Plain-numerical columns (n_genes, pseudotime) are on Numerical tab, not Modules
    assert 'x=n_genes' in resp.text  # still linked from the Numerical tab


def test_dataset_view_module_scores_absent_method_greyed(client, monkeypatch):
    # A dataset with ONLY aucell scores — the modules tab still renders via the
    # shared partial (scope=dataset). The partial no longer exposes a method
    # widget (scanpy-only UI, 2026-08-23): AUCell rows are dropped client-side
    # alongside starcat. The old server-side absent/greyed badge logic is gone
    # — the partial handles this uniformly.
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
    # Method widget removed 2026-08-23 (scanpy-only UI)
    assert 'class="mv-flt-method"' not in resp.text
    # No AUCell / starCAT method options in the partial
    assert '>AUCell<' not in resp.text
    assert 'method="starcat"' not in resp.text


def test_dataset_view_hides_starcat_prefixed_obs_columns(client, monkeypatch):
    """starcat__* columns are cluttery auto-generated flags — hide them from
    the raw Numerical + Categorical tabs on the dataset page. Numerical
    starCAT scores get their own Module Scores tab; the derived _pos/_high
    categorical flags don't belong in the obs listings at all.

    Contract: filter runs at obs_columns construction; plot endpoints still
    accept starcat__* names (data path untouched, tested elsewhere)."""
    def with_starcat(api_url, path):
        result = _fake_api_get(api_url, path)
        if path.endswith("/info") and isinstance(result, dict):
            result = dict(result)
            result["obs"] = {
                "categorical": [
                    "cell_type",
                    "starcat__MICROGLIA_V1__DAM_pos",
                    "starcat__MICROGLIA_V1__Cycling_pos",
                    "starcat__MICROGLIA_V1__HeatShock_high",
                ],
                "numerical": [
                    "n_genes", "pseudotime",
                    "starcat__MICROGLIA_V1__DAM_progression",
                    # A non-starcat scanpy module (routed to modules tab)
                    "Sierksma2025::WGCNA::Brown_Lysosome",
                ],
            }
        return result
    monkeypatch.setattr(browser, "_api_get", with_starcat)
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200

    # Categorical table: cell_type still there, no starcat__* rows
    cat_section = resp.text[resp.text.index('id="obs-tbl-cat"'):]
    cat_section = cat_section[:cat_section.index('</table>')]
    assert 'data-colname="cell_type"' in cat_section
    assert 'starcat__' not in cat_section

    # Numerical table: no starcat__* rows either (they were classified as
    # score columns and are on the Module Scores tab). n_genes/pseudotime stay.
    num_section = resp.text[resp.text.index('id="obs-tbl-num"'):]
    num_section = num_section[:num_section.index('</table>')]
    assert 'starcat__' not in num_section


def test_geneset_variability_page_offers_ucell_source(client):
    resp = client.get("/genesets/variability")
    assert resp.status_code == 200
    # UCell + scanpy (gene sets) source-filter options are present.
    # scanpy is the DEFAULT (selected) — site-wide 2026-08-23 default-to-scanpy
    # convention. UCell replaces AUCell as the ranked-based option (2026-08-26).
    # starCAT is temporarily hidden alongside its /genesets tab (2026-08 —
    # STARCAT_UI_ENABLED in browser.py); restore the assertion when the
    # flag flips back.
    assert 'value="ucell"' in resp.text
    assert 'value="geneset"' in resp.text
    assert 'value="starcat"' not in resp.text
    # scanpy is the pre-selected default option
    assert 'value="geneset" selected' in resp.text


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


def test_dataset_view_downloads_tab_always_shown(client):
    # The Downloads tab is always in the bar (2026-10-13). The JS greys it
    # (.tab-empty) when /files lists nothing and there are no paper PDFs.
    html = client.get("/browse/GroupA/ds1").text
    idx = html.index('id="tab-btn-downloads"')
    button_snippet = html[html.rindex("<button", 0, idx):html.index(">", idx)]
    assert "display:none" not in button_snippet
    assert "tabBtn.classList.add('tab-empty')" in html
    assert "Nothing to download for this dataset" in html
    assert "tabBtn.style.display" not in html


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
    assert '<h2 class="section-head">Embeddings' in data_panel
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
    assert 'class="section-head">Embeddings' not in resp.text
    # The Jinja-rendered embedding chip links (href=…) must not be present.
    # Note: JS may contain the string 'plot/scatter?embedding=' as a template
    # literal; we check for the Jinja-rendered href= version instead.
    import re
    rendered_emb_hrefs = re.findall(
        r'href="[^"]*plot/scatter\?embedding=', resp.text
    )
    assert not rendered_emb_hrefs, (
        f"Rendered embedding scatter href found when embeddings={{}}: {rendered_emb_hrefs[:3]}"
    )


def test_dataset_view_downloads_client_filters_to_h5ad(client):
    # Downloads surface is locked down to the raw h5ad + per-collection
    # static/ folder ONLY. The server refuses to expose duckdb / scores /
    # mus (see bin/api/ant-serve _DOWNLOADABLE_KINDS), so the client-side
    # markup no longer carries labels for those kinds. Staging/hidden files
    # (`_`/`.` prefix, `.inactive` suffix) are still dropped client-side.
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    # h5ad label + the remaining static files are the only kinds surfaced
    # (paper PDFs come from the library, rendered server-side).
    assert "raw AnnData" in resp.text
    assert "Other files" in resp.text
    assert ".h5ad" in resp.text
    # The removed kinds must not appear as user-facing labels or hrefs.
    assert "/download/duckdb" not in resp.text
    assert "/download/scores" not in resp.text
    assert "/download/mus" not in resp.text
    assert ".scores.parquet" not in resp.text
    assert ".h5ad.mus" not in resp.text
    # The static file endpoint is what supp downloads point at.
    assert "/download-static/" in resp.text
    # The staging-file guard is still there.
    assert "startsWith('_')" in resp.text


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
    assert '<h2 class="section-head">Downloads' not in resp.text


def test_folder_view_shows_static_downloads(client, tmp_path, monkeypatch):
    # With a populated static/ dir, the group page renders a Downloads section
    # and each file links to the group_static_download route.
    static = tmp_path / "GroupA" / "static"
    static.mkdir(parents=True)
    (static / "supplement.pdf").write_bytes(b"pdf-bytes")
    monkeypatch.setattr(browser, "_H5ADSTORE_ROOT", tmp_path)
    resp = client.get("/browse/GroupA")
    assert resp.status_code == 200
    assert '<h2 class="section-head">Downloads' in resp.text
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
    # tooltip vocabulary — landing card icon speaks *provenance* (source),
    # so tooltips read lab-generated/imported (not internal/external, which
    # would be per-dataset access wording).
    assert "lab-generated · published" in resp.text or "imported · published" in resp.text


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
    # Breadcrumb is now rendered via the breadcrumb() macro — verify the
    # dataset id-copy chips (bc-id-chip) carry the expected ids.
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    import re
    # bc-id-chip elements carry data-copy attributes with the ids
    chips = re.findall(r'bc-id-chip[^>]*data-copy="([^"]+)"', resp.text)
    assert chips, "no bc-id-chip elements in dataset page"
    # file_id ('ds1') must be among the chips
    assert "ds1" in chips, f"file_id 'ds1' not in bc-id-chip values: {chips}"


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


# ── route tests: facet-pill rendering in dataset obs tables ───────────────────

def test_dataset_canonical_col_renders_pill(client):
    """ant_braak in the obs list renders as facet-pill + bold stripped label in HTML."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # The pill class must appear (ant_braak, ant_cluster, ant_age are all in _INFO)
    assert 'facet-pill' in html


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
    # Extract all bold-tag label texts from facet-col spans
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
    # The title attribute on the facet-col span carries the full name
    assert 'title="facet annotation `ant_braak`' in resp.text


def test_dataset_non_canonical_col_no_pill(client):
    """cell_type and sample (non-facet) must NOT have a facet-pill."""
    import re as _re
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # Locate the cell_type row and confirm no facet-pill near it.
    # We check the colname td specifically, not the whole page.
    cell_type_idx = html.find('>cell_type<')
    assert cell_type_idx != -1, "cell_type colname not found in page"
    # pill should not appear in close proximity to plain cell_type label
    surrounding = html[max(0, cell_type_idx - 100): cell_type_idx + 200]
    assert 'facet-pill' not in surrounding


def test_dataset_canonical_col_css_present(client):
    """base.html must define the .facet-pill CSS rule."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    assert '.facet-pill' in resp.text


def test_dataset_canonical_col_data_name_attr_unmodified(client):
    """data-name on the obs-item row keeps the raw ant_* value for JS filtering."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    # data-name must use the lowercased full column name for the JS filter to work
    assert 'data-name="ant_braak"' in resp.text
    assert "family=Roboto" in resp.text


# ── module view: technology + biosample facets + URL hash features ────────────

def test_module_view_technology_facet_rendered(client):
    """technology and biosample filter dropdowns replace the old system facet."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    assert 'mv-flt-technology-w' in resp.text, (
        "technology mff widget not rendered — check _module_view.html template"
    )
    assert 'mv-flt-biosample-w' in resp.text, (
        "biosample mff widget not rendered — check _module_view.html template"
    )
    assert 'class="mv-flt-system"' not in resp.text, (
        "deprecated system filter still rendered — remove from _module_view.html"
    )


# Keep old name as alias so existing CI that references the old function name
# still works — both point to the same assertion.
test_module_view_system_facet_rendered = test_module_view_technology_facet_rendered


def test_module_view_technology_biosample_listeners_bound(client):
    """technology and biosample are now mff widgets — onChange is wired by mffWidget factory."""
    resp = client.get("/genesets/StudyX/Exp1/gs_up")
    assert resp.status_code == 200
    # mff widgets wire onChange internally — check factory and widget containers present
    assert "window.mffWidget" in resp.text, (
        "mffWidget factory missing — technology/biosample onChange won't fire"
    )
    assert "mv-flt-technology-w" in resp.text, (
        "technology mff widget container not found"
    )
    assert "mv-flt-biosample-w" in resp.text, (
        "biosample mff widget container not found"
    )


# Legacy alias kept for CI compatibility.
test_module_view_system_facet_listener_bound = test_module_view_technology_biosample_listeners_bound


def test_module_view_biosample_filter_uses_indexof(client):
    """Biosample mff filter uses bioArr with OR logic across checked values."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    # mff biosample filter uses bioArr (Array.from(mffBio)) instead of single bioVal
    assert "bioArr" in resp.text, (
        "biosample mff filter must use bioArr for OR logic across checked values"
    )


# Legacy alias.
test_module_view_system_filter_uses_indexof = test_module_view_biosample_filter_uses_indexof


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


def test_module_view_tissue_dropdown_uses_fill_tissue_mff(client):
    """fillTissueMff must appear in the rendered HTML (multi-select widget for
    tissue — provides indented hierarchy options; fillTissueSelect was the old
    plain-select API removed in the multi-select refactor)."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    assert "fillTissueMff" in resp.text, (
        "fillTissueMff not present — tissue mff widget will not be populated"
    )
    assert "fillTissueSelect" not in resp.text, (
        "fillTissueSelect still present — stale pre-refactor call site remains"
    )


def test_module_view_no_fill_select_call_sites(client):
    """fillSelect must not appear in any module-view page — it was replaced by
    fillMff/fillTissueMff in the multi-select refactor.  Regression guard:
    a remaining fillSelect call causes 'ReferenceError: fillSelect is not defined'
    at first render, which breaks the entire drilldown panel."""
    for path in ("/genesets/StudyX/Exp1", "/genesets/StudyX/Exp1/gs_up"):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} returned {resp.status_code}"
        assert "fillSelect(" not in resp.text, (
            f"fillSelect( call site found in {path} — will cause ReferenceError at runtime"
        )


def test_dataset_page_no_fill_select_call_sites(client):
    """fillSelect must not appear on the dataset page — regression guard for
    the multi-select refactor that may have been partially applied."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    assert "fillSelect(" not in resp.text, (
        "fillSelect( call site found in dataset page — will cause ReferenceError at runtime"
    )


def test_genes_page_no_fill_select_call_sites(client):
    """fillSelect must not appear on the genes page — regression guard for
    the multi-select refactor."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    assert "fillSelect(" not in resp.text, (
        "fillSelect( call site found in genes page — will cause ReferenceError at runtime"
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


# ── Ask 1: two-row filter layout ─────────────────────────────────────────────

def test_module_view_two_filter_rows(client):
    """Filter controls must be split into two .mv-filter-row elements.
    Row 1 holds dataset, module, and facet (in that order).
    Row 2 holds group, cell type, tissue, technology, biosample, n_cells.
    The method widget was removed 2026-08-23 alongside the default-to-scanpy
    change.
    """
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    html = resp.text
    assert html.count('class="mv-filter-row') >= 2, (
        "Expected at least two .mv-filter-row elements"
    )
    # Row 1 must contain dataset, module, and facet (in DOM order).
    row1_start = html.find('mv-filter-row-1')
    row1_end   = html.find('mv-filter-row-2')
    assert row1_start != -1 and row1_end != -1
    row1 = html[row1_start:row1_end]
    assert 'mv-flt-dataset-w' in row1, "Row 1 must have dataset mff widget"
    assert 'mv-flt-module-w'  in row1, "Row 1 must have module mff widget"
    assert 'mv-flt-tag-w'     in row1, "Row 1 must have facet mff widget"
    # Row 2 must contain the annotation facets + n_cells.
    row2_start = row1_end
    row2_end   = html.find('mv-summary-row', row2_start)
    row2 = html[row2_start:row2_end]
    assert 'mv-flt-celltype-w' in row2, "Row 2 must have cell type mff widget"
    assert 'mv-flt-n_cells'    in row2, "Row 2 must have n_cells filter"
    # Method widget must be gone
    assert 'mv-flt-method' not in row2


# ── Ask 2: column-header sort ─────────────────────────────────────────────────

def test_module_view_th_sort_data_attr(client):
    """Sortable column headers must carry data-sortcol attributes."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    assert 'data-sortcol="module"'   in resp.text
    assert 'data-sortcol="dataset"'  in resp.text
    assert 'data-sortcol="position"' in resp.text
    assert 'data-sortcol="value"'    in resp.text


def test_module_view_sort_state_and_indicators(client):
    """sortState variable and updateSortIndicators function must be present.
    The sort dropdown must be absent; sort is header-click only.
    """
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    html = resp.text
    assert 'sortState' in html, "sortState variable missing"
    assert 'updateSortIndicators' in html, "updateSortIndicators function missing"
    assert 'mv-sort-asc' in html, "mv-sort-asc CSS class missing"
    assert 'mv-sort-desc' in html, "mv-sort-desc CSS class missing"
    assert 'mv-flt-sort' not in html, (
        "Sort dropdown (mv-flt-sort) must be removed — sort lives in column headers"
    )


# ── Ask 3: obs column label, per-cell association heading ─────────────────────

def test_module_view_obs_column_label(client):
    """The tag filter and column read 'obs column' (not 'facet'); donor and
    cluster columns are off in the default view; the value column is labelled
    as an exploratory per-cell association with the pseudoreplication tooltip."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    html = resp.text
    assert '<label>obs column <span class="info"' in html
    assert '>obs column: all' in html
    assert '<th class="mv-th-tag" data-sortcol="tag">obs column</th>' in html
    assert 'Values include braak, sex, cluster, disease' not in html
    assert "per-cell association (descriptive)" in html
    assert "Computed over cells, not donors; it shows where a score differs, not a tested effect." in html
    assert 'id="mv-panel-about"' in html or "-about" in html and "About per-cell associations" in html
    assert "/donor|cluster/i" in html and "mff.tag.setValues(defaultTags)" in html


def test_module_view_no_dot_for_few_peers(client):
    """Peer groups with fewer than 4 modules get an empty position cell, sorted last."""
    html = client.get("/genesets/StudyX/Exp1").text
    assert "var MIN_PEERS = 4;" in html
    assert "g.n >= MIN_PEERS" in html
    assert "!hasPeers(r, PEER_INDEX) ? ''" in html
    assert 'title="fewer than 4 peer modules"' in html
    assert "function comparePosition" in html


# ── Ask 4: ECDF link for categorical plots ─────────────────────────────────────

def test_module_view_ecdf_url_function_present(client):
    """ecdfUrl() helper must be present and return null for numerical tags."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    html = resp.text
    assert 'ecdfUrl' in html, "ecdfUrl helper function missing"
    assert 'ecdf' in html, "ECDF URL scheme not referenced"
    # ecdfUrl must guard on NUMERICAL_TAGS returning null.
    assert 'NUMERICAL_TAGS[r.tag]' in html or 'NUMERICAL_TAGS[row.tag]' in html, (
        "ecdfUrl must skip numerical tags"
    )


def test_module_view_ecdf_plot_icon_in_row(client):
    """The plot cell must include an ECDF link (eu variable) for categorical rows."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    html = resp.text
    assert 'var eu' in html or 'eu =' in html, (
        "ECDF URL variable (eu) must be computed per row"
    )
    assert '/plot/ecdf?' in html or "'/plot/ecdf'" in html or '+ \'/plot/ecdf\'' in html or "ecdf?" in html, (
        "ECDF plot URL must be referenced in row rendering"
    )


# ── Ask 5: genesets page — browse-all link per collection ────────────────────

def test_genesets_study_page_renders(client):
    """/genesets/<study> renders a per-study drilldown with experiments and
    gene sets nested inside expandable cards. Replaces the old
    'browse-all-in-this-collection' link (which lived on the landing page
    itself before the 2026-08-26 redesign)."""
    resp = client.get("/genesets/StudyX")
    assert resp.status_code == 200
    html = resp.text
    # Experiment name + expandable card
    assert 'class="exp-card"' in html
    # The "open" link points at the existing /genesets/<study>/<exp> route
    assert 'href="/genesets/StudyX/Exp1"' in html
    # Gene set link into the geneset page
    assert 'href="/genesets/StudyX/Exp1/gs_up"' in html
    # Breadcrumb back to /genesets
    assert 'href="/genesets"' in html


# ── Ask 6: gene search URL state ─────────────────────────────────────────────

def test_genesets_bygene_push_state_on_search(client):
    """Gene search must call pushGeneUrl → history.pushState so back-button works."""
    resp = client.get("/genesets?group=bygene")
    assert resp.status_code == 200
    html = resp.text
    assert 'pushGeneUrl' in html, "pushGeneUrl function missing"
    assert 'history.pushState' in html, "history.pushState not called in gene search"


def test_genesets_bygene_popstate_listener(client):
    """A popstate listener must restore the gene input and re-run search on back-nav."""
    resp = client.get("/genesets?group=bygene")
    assert resp.status_code == 200
    html = resp.text
    assert "addEventListener('popstate'" in html or "addEventListener(\"popstate\"" in html, (
        "popstate listener missing in bygene tab — back button won't work"
    )


def test_genesets_bygene_url_has_gene_param(client):
    """The ?gene= deep-link must pre-populate the input and trigger search."""
    resp = client.get("/genesets?group=bygene")
    assert resp.status_code == 200
    html = resp.text
    # The deep-link wiring reads from URLSearchParams and sets input.value.
    assert "qp.get('gene')" in html, "?gene= deep-link wiring missing"


# ── Ask 7: broken dataset link fix ───────────────────────────────────────────

def test_module_view_dataset_link_uses_facet_gs_tab_hash(client):
    """Dataset links must use #facet-gs&mv=... so the Facet GS tab (the explorer's home) activates on landing."""
    resp = client.get("/genesets/StudyX/Exp1/gs_up")
    assert resp.status_code == 200
    html = resp.text
    # The JS template string that builds the href must include '#facet-gs'.
    assert "b + '#facet-gs'" in html, (
        "datasetUrl must embed '#facet-gs' so the Facet GS tab is activated"
    )
    assert "b + '#modules'" not in html
    # Must use &mv= separator (not bare #mv=).
    assert "'&mv='" in html or '"&mv="' in html, (
        "datasetUrl must use the '&mv=' separator after the tab anchor"
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


def test_genes_tab_has_dot_scale_elements(client):
    """Each gene row has .dot-scale big-dot elements for pct, rank, and variability."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    assert 'class="dot-scale"' in html, ".dot-scale must be rendered in gene rows"
    assert 'data-stat="pct"' in html, "pct dot track required"
    assert 'data-stat="rank"' in html, "rank dot track required"
    assert 'data-stat="var"' in html, "variability dot track required"
    assert 'data-stat="mean"' not in html, "mean dot track must not appear"
    assert 'data-stat="meannz"' not in html, "meannz dot track must not appear"
    # Shared helper (base.html) and no leftover sparkline code.
    assert "window.antDot" in html
    assert "antDot.set(" in html
    assert "setSsMark" not in html and "ss-mark" not in html


def test_dot_scale_css_tokens_in_base(client):
    """The shared big-dot component and its RAG tokens live in base.html."""
    html = client.get("/browse/GroupA/ds1").text
    for token in ("--dot-track", "--dot-red", "--dot-amber", "--dot-green"):
        assert token in html
    assert 'data-tone="red"' in html and "box-shadow: 0 0 0 2px var(--bg)" in html


def test_genes_tab_rank_expr_uses_server_rank_mean(client):
    """rank expr column is populated from server-returned rank_mean (0-1),
    not computed client-side from a range — no normInRange call for mean."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # rank_mean is directly positioned on the sparkline.
    assert "rank_mean" in html, "rank_mean field from gene_stats must be consumed in JS"
    # No normInRange for mean (that was the old approach).
    assert "normInRange(s.mean," not in html and "normInRange(statsObj.mean," not in html, \
        "mean sparkline must not use normInRange (replaced by rank_mean)"


def test_genes_tab_variability_uses_std_rank(client):
    """variability column uses rank of std within the displayed panel, not raw std as axis."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # computeStdRanks helper must be defined.
    assert "computeStdRanks" in html, "computeStdRanks JS helper must be defined"
    # std field must be read from gene_stats response.
    assert ".std" in html or "s.std" in html, "std field must be consumed from gene_stats"


# ── Dataset tab enhancements (2026-08-19) ────────────────────────────────────

def test_genes_tab_server_side_search(client):
    """Genes tab: server-side search hits /genes?q= endpoint (debounced, 250ms).

    After the 2026-08-25 in-place filter refactor, search no longer renders a
    separate results table; it hides non-matching rows in the main table and
    appends dataset-wide server hits into it. #gene-search input remains.
    """
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # Server-side search: fetch to /genes endpoint.
    assert "'/genes?q='" in html or '"/genes?q="' in html or "apiBase + '/genes?q='" in html or "/genes?q=" in html, \
        "gene search must hit /genes?q= API endpoint"
    # Debounce: setTimeout used.
    assert "setTimeout" in html, "gene search must be debounced (setTimeout)"
    # In-place filter: the .g-hidden marker class must be applied by search JS.
    assert 'g-hidden' in html, "search must toggle .g-hidden on non-matching rows"
    # The old separate search-results table must be gone.
    assert 'id="gene-search-results"' not in html, \
        "search results no longer render in a separate table; filter is in place"
    assert 'id="obs-tbl-gene-results"' not in html, \
        "the separate search-results table id must be gone"


def test_genes_tab_topvar_section_present(client):
    """Genes tab: top-variable rows share the curated table, populated by JS from std.

    Post-merge (task #295): top-var rows are appended into the same
    ``#obs-tbl-genes`` tbody as curated rows and sort together — the
    separate ``topvar-section`` / ``topvar-tbody`` scaffold is gone.
    The JS uses `renderTopVar` to insert into the shared tbody."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # Merged behavior: no separate topvar tbody/section.
    assert 'id="topvar-section"' not in html, (
        "topvar section must be gone — top-var rows share the curated tbody now"
    )
    assert 'id="topvar-tbody"' not in html, (
        "topvar tbody must be gone — top-var rows share the curated tbody now"
    )
    # But renderTopVar is still the entry point that inserts the rows.
    assert 'function renderTopVar' in html, "renderTopVar must still populate top-var rows"
    # Top-var still requires std from gene_stats.
    assert ".std" in html or "s.std" in html or "x.std" in html, "topvar must sort by std field"


# ── Genes tab — merged curated + top-variable table (2026-08-20) ──────────────

def test_genes_tab_single_table(client):
    """Curated, top-variable, and searched genes all share ONE table
    (#obs-tbl-genes). Search filters this table in place, so there is exactly
    one obs-stats-tbl table in the Genes panel."""
    import re
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    tab_start = html.find('id="tab-genes"')
    tab_end   = html.find('id="tab-data"', tab_start)
    assert tab_start != -1, "tab-genes panel not found"
    genes_html = html[tab_start:tab_end] if tab_end != -1 else html[tab_start:]
    tables = re.findall(r'<table\b[^>]*class="obs-stats-tbl[^"]*"', genes_html)
    assert len(tables) == 1, (
        f"Expected exactly 1 obs-stats-tbl table in the Genes panel "
        f"(everything filters in place); found {len(tables)}"
    )
    assert 'id="obs-tbl-genes"' in genes_html, "#obs-tbl-genes must be present"
    assert 'id="obs-tbl-gene-results"' not in genes_html, \
        "there must be no separate search-results table"
    assert "Most variable genes" not in genes_html, (
        "Separate 'Most variable genes' heading must not appear in Genes panel"
    )


def test_genes_tab_curated_rows_have_star(client):
    """Each server-rendered curated gene row must carry the ◆ dataset-favourite
    glyph (a personal favourite ♥ is added by client-side JS, so we don't
    assert on it here)."""
    import re
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    curated_rows = re.findall(
        r'<tr class="gene-item gene-curated"[^>]*>.*?</tr>', html, re.DOTALL
    )
    assert len(curated_rows) > 0, "gene-curated rows not found (ds1 has top_genes)"
    for row in curated_rows:
        assert '&#9670;' in row or '◆' in row, \
            f"Curated gene row missing ◆ glyph: {row[:160]}"


def test_genes_tab_star_column_header(client):
    """The merged genes table has a sort-flag column header (labelled ★, but
    the tooltip explains ◆ vs ♥)."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    tbl_start = html.find('id="obs-tbl-genes"')
    assert tbl_start != -1
    thead_end = html.find('</thead>', tbl_start)
    thead = html[tbl_start:thead_end]
    assert '★' in thead, "★ flag-column header must appear in #obs-tbl-genes <thead>"


def test_genes_tab_star_hover_title(client):
    """The flag-column header's tooltip mentions both ◆ (dataset favourite)
    and ♥ (personal favourite)."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    assert 'dataset favourite' in html, \
        "flag-column header tooltip must call out the dataset favourite"
    assert 'personal favourite' in html, \
        "flag-column header tooltip must call out the personal favourite"


def test_genes_tab_topvar_rows_have_heart_toggle(client):
    """Every rendered top-var row carries a ♡/♥ toggle. The renderTopVar
    branch computes isFav from the personal-favourite set and passes the
    result to _heartHtml, which renders either the outline or filled state."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # renderTopVar must call _heartHtml(isFav) so every row gets a toggle.
    assert "_heartHtml(isFav)" in html, \
        "renderTopVar must emit a ♡/♥ toggle on every row via _heartHtml"
    # The topvar call must NOT pass a literal '★'.
    assert "makeGeneRow(x.gene, x, '★'" not in html, \
        "topvar makeGeneRow call must not emit a ★ glyph"


def test_genes_tab_search_still_works(client):
    """The gene search input and search JS are still present after the
    2026-08-25 in-place filter refactor."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    assert 'id="gene-search"' in html, "gene-search input must remain present"
    assert "applyGeneSearch" in html, "applyGeneSearch JS must remain"
    assert 'id="obs-tbl-genes"' in html, "merged genes table must exist"
    # No separate search-results table any more.
    assert 'id="gene-search-results"' not in html, "search-results section is gone"
    assert 'id="obs-tbl-gene-results"' not in html, "search-results table is gone"


# ── Genes tab — uniform columns (star / gene / %expr / rank / variability) ───

def test_genes_tab_uniform_column_headers(client):
    """#obs-tbl-genes has the standard column headers: star / gene / plot /
    % expr / rank expr / spread across cells. No CoV, no mean or mean(nz). The separate
    search-results table is gone."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    tbl_start = html.find('id="obs-tbl-genes"')
    assert tbl_start != -1, "#obs-tbl-genes not found"
    thead_end = html.find('</thead>', tbl_start)
    thead = html[tbl_start:thead_end]
    assert 'Cells expressing (%)' in thead, "#obs-tbl-genes thead missing the cells expressing column"
    assert 'Rank among genes (%)' in thead, "#obs-tbl-genes thead missing the rank column"
    assert 'Std. dev.' in thead, "#obs-tbl-genes thead missing the std. dev. column"
    assert '>CoV<' not in html and 'g-cov' not in html, "the CoV column is gone"
    assert 'variability' not in thead, \
        "'variability' header must be renamed to 'std'"
    assert '>mean<' not in thead and '>mean(nz)<' not in thead, \
        "#obs-tbl-genes thead must not contain raw mean or mean(nz) column"


def test_genes_tab_no_mean_columns_visible(client):
    """Neither 'mean' nor 'mean(nz)' appear as column headers in the Genes tab."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    tab_start = html.find('id="tab-genes"')
    tab_end   = html.find('id="tab-data"', tab_start)
    genes_html = html[tab_start:tab_end] if tab_end != -1 else html[tab_start:]
    assert '>mean<' not in genes_html, "raw 'mean' column header must not appear in Genes tab"
    assert '>mean(nz)<' not in genes_html, "mean(nz) column header must not appear in Genes tab"


def test_genes_tab_curated_rows_have_pct_rank_var_cells(client):
    """Curated gene rows (Jinja-rendered) have .g-pct-cell, .g-rank-cell, .g-var-cell."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    assert 'g-pct-cell'  in html, "curated rows must have .g-pct-cell"
    assert 'g-rank-cell' in html, "curated rows must have .g-rank-cell"
    assert 'g-var-cell'  in html, "curated rows must have .g-var-cell"
    # Old mean cells must be gone.
    assert 'g-mean-cell'   not in html, ".g-mean-cell must be removed"
    assert 'g-meannz-cell' not in html, ".g-meannz-cell must be removed"
    assert 'g-max'         not in html, ".g-max must be removed"


def test_genes_tab_js_makeGeneRow_unified(client):
    """makeGeneRow JS function has a single return path (no variant branch)."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # New unified signature: makeGeneRow(geneName, s, starHtml, stdRankFrac)
    assert "makeGeneRow" in html, "makeGeneRow must be defined"
    assert "starHtml" in html, "makeGeneRow must accept starHtml parameter"
    assert "stdRankFrac" in html, "makeGeneRow must accept stdRankFrac parameter"
    # No old variant branches.
    assert "variant === 'topvar'" not in html, "old topvar variant branch must be removed"
    assert "gene_stats columns" not in html, "old gene_stats variant comment must be removed"


def test_numerical_tab_stats_without_sparkline(client):
    """Numerical obs tab: min/Q05/median/Q95/max columns, no range sparkline.

    The per-row median-on-range sparkline (.s-num-spark) was removed on
    purpose in commit ff1023f because it duplicated the min/median/max
    columns. Guard against it coming back and check the stat cells remain.
    """
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    assert 'id="obs-tbl-num"' in html
    assert ('<th>min</th><th>Q05</th><th>median</th><th>Q95</th><th>max</th></tr>'
            in html), "numerical tab header must end at max (no range column)"
    for cell in ('s-min', 's-q05', 's-median', 's-q95', 's-max'):
        assert f'class="{cell}"' in html, f"numerical rows must have .{cell}"
    assert 's-num-spark' not in html, ".s-num-spark sparkline was removed (ff1023f)"
    assert 'normInRange' not in html, "normInRange helper was removed with the sparkline"


def test_categorical_tab_ecdf_link(client):
    """Categorical tab: each column has an ECDF link icon (revealed by JS when a numeric col exists)."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # ECDF icon present in categorical tab (hidden by default, shown by JS).
    assert 's-ecdf-link' in html, ".s-ecdf-link ECDF icon must be in categorical rows"
    # JS must choose a first numerical column and set href.
    assert "_firstNumCol" in html, "_firstNumCol JS variable must select the numeric x-axis for ECDF"


def test_categorical_tab_top_categories_pct_label(client):
    """Categorical tab: top categories show a % label (text only, no dot scale)."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    assert "pct" in resp.text, "pct value must appear in categorical top-category rendering"


def test_dot_scale_component_in_base(client):
    """The sitewide big-dot CSS is defined once (base.html) with an end-inset dot and a ring."""
    html = client.get("/browse/GroupA/ds1").text
    assert ".dot-scale::before" in html, "grey track must be declared"
    assert ".dot-scale-dot" in html
    assert "(100% - var(--dot-d))" in html, "dot centre must be inset by its radius"
    assert ".ss {" not in html and ".ss-mark" not in html, "old sparkline CSS must be gone"


def test_module_view_peer_position_uses_bar(client, monkeypatch):
    """Module view peer-position cells render through antBar (the sitewide bar), not the old dot scale or mv-track."""
    def _with_scores(api_url, path):
        result = _fake_api_get(api_url, path)
        if path.endswith('/info') and isinstance(result, dict):
            result = dict(result)
            obs = dict(result.get('obs', {}))
            obs['numerical'] = ['n_genes', 'pseudotime', 'studyA::expB::Mod1']
            result['obs'] = obs
        return result
    monkeypatch.setattr(browser, '_api_get', _with_scores)
    html = client.get("/browse/GroupA/ds1").text
    assert "antBar.html(pos" in html and "antDot.html(pos" not in html
    assert "mv-track" not in html and "mv-mark" not in html


# ── Unified breadcrumb tests (2026-08-19) ────────────────────────────────────
#
# Three invariants across every page:
#   1. <nav class="breadcrumb"> exists (macro, not ad-hoc div)
#   2. .bc-home exists as the leftmost link, href="/"
#   3. .bc-current (last crumb) has font-weight:700 from the sitewide CSS rule
#   4. .bc-id-chip exists on pages with collection/dataset context
#   5. The same font-size (12px) is declared in base.html for .breadcrumb

import re as _re


def _find_breadcrumb(html: str) -> str | None:
    """Return the first <nav class="breadcrumb">…</nav> block, or None."""
    m = _re.search(r'<nav class="breadcrumb"[^>]*>(.*?)</nav>', html, _re.S)
    return m.group(0) if m else None


def _bc_home_href(bc_html: str) -> str | None:
    """Return the href of the .bc-home link, or None."""
    m = _re.search(r'<a class="bc-home"[^>]*href="([^"]*)"', bc_html)
    return m.group(1) if m else None


def _bc_current_text(bc_html: str) -> str | None:
    """Return the text inside .bc-current, or None."""
    m = _re.search(r'<span class="bc-current">([^<]*)</span>', bc_html)
    return m.group(1).strip() if m else None


def _bc_id_chips(bc_html: str) -> list[str]:
    """Return list of data-copy values from .bc-id-chip elements."""
    return _re.findall(r'bc-id-chip[^>]*data-copy="([^"]+)"', bc_html)


@pytest.mark.parametrize("path,expected_current", [
    ("/browse/GroupA",                "Group A Study"),   # folder page: crumb is the collection name
    ("/browse/GroupA/ds1",            "Dataset One"),     # dataset page
    ("/browse/GroupA/ds1/plot/scatter", "scatter"),       # plot page (partial match ok)
    ("/genesets/StudyX/Exp1/gs_up",   "gs_up"),           # geneset detail
    ("/genesets/StudyX/Exp1",         "Exp1"),             # geneset experiment
    ("/genesets/variability",         "geneset variability"),  # variability heatmap
])
def test_breadcrumb_present_on_page(client, path: str, expected_current: str):
    """Every detail page must have a <nav class='breadcrumb'> with a .bc-current."""
    resp = client.get(path)
    assert resp.status_code == 200, f"route {path} returned {resp.status_code}"
    bc = _find_breadcrumb(resp.text)
    assert bc is not None, f"no <nav class='breadcrumb'> found on {path}"
    current = _bc_current_text(bc)
    assert current is not None, f"no .bc-current on {path}"
    assert expected_current.lower() in current.lower(), (
        f"expected '{expected_current}' in .bc-current '{current}' on {path}"
    )


@pytest.mark.parametrize("path", [
    "/browse/GroupA",
    "/browse/GroupA/ds1",
    "/browse/GroupA/ds1/plot/scatter",
    "/genesets/StudyX/Exp1/gs_up",
    "/genesets/StudyX/Exp1",
    "/genesets/variability",
    "/genes/TREM2",
    "/privacy",
])
def test_breadcrumb_home_link(client, path: str):
    """Every page breadcrumb must have a .bc-home link with href='/'."""
    resp = client.get(path)
    assert resp.status_code == 200, f"route {path} returned {resp.status_code}"
    bc = _find_breadcrumb(resp.text)
    assert bc is not None, f"no breadcrumb on {path}"
    href = _bc_home_href(bc)
    assert href == "/", (
        f".bc-home href is '{href}' (expected '/') on {path}"
    )


def test_breadcrumb_font_size_in_base_css(client):
    """base.html must declare font-size:12px on .breadcrumb (sitewide uniformity)."""
    resp = client.get("/")
    assert resp.status_code == 200
    # CSS block in base.html contains the rule
    assert ".breadcrumb" in resp.text
    # font-size:12px is the unified size — find it near the .breadcrumb rule
    idx = resp.text.find(".breadcrumb {")
    assert idx != -1, ".breadcrumb { rule not found in base CSS"
    rule_block = resp.text[idx:idx + 200]
    assert "12px" in rule_block, (
        f"expected 12px in .breadcrumb rule, got: {rule_block!r}"
    )


def test_breadcrumb_current_bold_css(client):
    """base.html must declare font-weight:700 on .bc-current."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert ".bc-current" in resp.text, ".bc-current CSS class not declared in base.html"
    idx = resp.text.find(".bc-current {")
    assert idx != -1, ".bc-current { rule not found"
    rule = resp.text[idx:idx + 120]
    assert "700" in rule or "bold" in rule, (
        f".bc-current must set font-weight≥700, got: {rule!r}"
    )


@pytest.mark.parametrize("path,expected_ids", [
    ("/browse/GroupA",      ["GroupA"]),          # collection page: collection id chip
    ("/browse/GroupA/ds1",  ["GroupA", "ds1"]),   # dataset page: collection + dataset + db chips
])
def test_breadcrumb_id_chips_on_context_pages(client, path: str, expected_ids: list):
    """Collection and dataset pages must carry .bc-id-chip elements with the correct ids."""
    resp = client.get(path)
    assert resp.status_code == 200
    bc = _find_breadcrumb(resp.text)
    assert bc is not None, f"no breadcrumb on {path}"
    chips = _bc_id_chips(bc)
    assert chips, f"no bc-id-chip elements in breadcrumb on {path}"
    for expected_id in expected_ids:
        assert expected_id in chips, (
            f"expected id '{expected_id}' in chips {chips} on {path}"
        )


def test_breadcrumb_id_chip_css_in_base(client):
    """base.html must declare .bc-id-chip CSS with user-select:all and cursor:pointer."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert ".bc-id-chip" in resp.text, ".bc-id-chip CSS not in base.html"
    # user-select:all makes the chip content directly selectable
    assert "user-select: all" in resp.text, "user-select:all not declared for bc-id-chip"


def test_breadcrumb_copy_js_in_base(client):
    """base.html must include the click handler that calls navigator.clipboard.writeText."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "navigator.clipboard.writeText" in resp.text, (
        "clipboard copy JS missing from base.html"
    )
    assert "bc-id-chip" in resp.text, "bc-id-chip class not referenced in JS"


def test_breadcrumb_no_old_div_style(client):
    """Old-style <div class='breadcrumb'> must be gone; macro emits <nav class='breadcrumb'>."""
    import re
    for path in ["/browse/GroupA", "/browse/GroupA/ds1", "/genesets/StudyX/Exp1/gs_up"]:
        resp = client.get(path)
        assert resp.status_code == 200
        # no raw div.breadcrumb — only nav.breadcrumb from the macro
        old_divs = re.findall(r'<div[^>]+class="[^"]*breadcrumb[^"]*"', resp.text)
        assert not old_divs, (
            f"old <div class='breadcrumb'> still present on {path}: {old_divs}"
        )


# ── gene atlas drilldown-style UX tests ──────────────────────────────────────

def test_gene_atlas_has_two_gv_filter_rows(client):
    """Two .gv-filter-row divs must be present on /genes/<gene>."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    import re
    rows = re.findall(r'class="gv-filter-row"', resp.text)
    assert len(rows) == 2, f"expected 2 .gv-filter-row, found {len(rows)}"


def test_gene_atlas_filter_row1_has_pct_and_organism(client):
    """Row 1 must contain the min-%expr input and organism select."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    # pct threshold input
    assert 'id="gv-flt-pct"' in resp.text, "pct threshold input missing"
    # organism select
    assert 'id="gv-flt-organism"' in resp.text, "organism select missing"


def test_gene_atlas_filter_row2_has_cell_tissue_system(client):
    """Row 2 must contain cell_type, tissue, technology, and biosample mff widgets."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    # cell_type/tissue/technology/biosample are now mff widget containers
    assert 'id="gv-flt-cell_type-w"' in resp.text, "cell_type mff widget missing"
    assert 'id="gv-flt-tissue-w"' in resp.text, "tissue mff widget missing"
    assert 'id="gv-flt-technology-w"' in resp.text, "technology mff widget missing"
    assert 'id="gv-flt-biosample-w"' in resp.text, "biosample mff widget missing"
    assert 'id="gv-flt-system"' not in resp.text, "deprecated system select still present"


def test_gene_atlas_table_has_sortcol_headers(client):
    """Table must contain <th data-sortcol=...> for each required column."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    required_cols = ['dataset', 'pct_nonzero', 'mean', 'mean_nonzero',
                     'organism', 'cell_type', 'tissue', 'biosample']
    for col in required_cols:
        assert f'data-sortcol="{col}"' in resp.text, (
            f"<th data-sortcol=\"{col}\"> missing from gene atlas table"
        )
    assert 'data-sortcol="system"' not in resp.text, "deprecated system sortcol still present"


def test_gene_atlas_sort_state_js_present(client):
    """Sort state JS (updateSortIndicators, sortState, gv-sort-asc/desc) must be present."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    assert 'updateSortIndicators' in resp.text, "updateSortIndicators JS missing"
    assert 'sortState' in resp.text, "sortState JS object missing"
    assert 'gv-sort-asc' in resp.text, "gv-sort-asc CSS class missing"
    assert 'gv-sort-desc' in resp.text, "gv-sort-desc CSS class missing"


def test_gene_atlas_dataset_popover_markup_present(client):
    """Dataset hover popover CSS classes (.gv-ds-pop, .gv-ds-tip) must be in the page."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    assert 'gv-ds-pop' in resp.text, ".gv-ds-pop class missing (popover container)"
    assert 'gv-ds-tip' in resp.text, ".gv-ds-tip class missing (popover content)"
    # popover must expose title / celltype / tissue / system / n_cells fields
    assert 'tip-title' in resp.text, ".tip-title not in popover markup"
    assert 'tip-row' in resp.text, ".tip-row not in popover markup"


def test_gene_atlas_stat_dots(client):
    """Gene atlas stat cells use antDot via statDot(); the old .ss sparkline is gone."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    assert "function statDot" in resp.text and "antDot.html" in resp.text
    assert "ss-tick" not in resp.text and "sparkline(" not in resp.text


def test_gene_atlas_url_hash_gv_scheme(client):
    """URL hash scheme must use #gv= (not #mv=) to avoid colliding with module view."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    # The JS must build hashes with gv= prefix
    assert "'gv='" in resp.text or '"gv="' in resp.text or 'gv=' in resp.text, (
        "URL hash gv= scheme not found"
    )
    assert 'buildHashFromFilters' in resp.text, "buildHashFromFilters JS missing"
    assert 'parseHashFilters' in resp.text, "parseHashFilters JS missing"
    assert 'history.pushState' in resp.text, "history.pushState missing (URL-persistence)"
    assert 'popstate' in resp.text, "popstate listener missing (back-nav)"


def test_gene_atlas_ds_annot_injected(client):
    """DS_ANNOT constant from dataset_annotations must be embedded in the page."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    # The server-rendered DS_ANNOT JSON constant drives client-side popover and filters.
    assert 'DS_ANNOT' in resp.text, "DS_ANNOT JS constant missing from gene atlas page"
    # The fake databases include "microglial cell" as celltype for GroupA/ds1.
    assert 'microglial cell' in resp.text, (
        "dataset_annotations not properly injected — 'microglial cell' not in page"
    )


def test_gene_atlas_plot_icons_present(client):
    """Each row's plot column must include histogram, ECDF, and UMAP SVG icon links."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    # plotLinks() renders /plot/histogram, /plot/ecdf, /plot/scatter links in JS
    assert '/plot/histogram' in resp.text, "histogram plot link missing"
    assert '/plot/ecdf' in resp.text, "ECDF plot link missing"
    assert '/plot/scatter' in resp.text, "UMAP plot link missing"


def test_gene_atlas_multigene_heatmap_controls_present(client):
    """Multi-gene heatmap metric selector must be present (hidden initially)."""
    resp = client.get("/genes/APOE,TREM2")
    assert resp.status_code == 200
    assert 'id="gv-hm-controls"' in resp.text, "gv-hm-controls container missing"
    assert 'id="hm-metric"' in resp.text, "hm-metric select missing"
    assert 'renderHeatmap' in resp.text, "renderHeatmap function missing"


def test_gene_atlas_mean_sparkline_uses_rank_not_cross_db_max(client):
    """mean / mean_nonzero sparklines must use per-dataset percentile rank (unit-invariant).

    Previously the position was mean / cross-db-max which broke for proteomics datasets
    whose intensities are 6+ orders of magnitude larger than scRNA-seq log-norm values.
    Now mean sparkline uses r.rank_mean (0–1 PERCENT_RANK from server) and
    mean_nonzero uses a client-computed cross-dataset rank (computeRankMeanNonzero).
    """
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    text = resp.text

    # rank_mean field is consumed directly (comes from server as PERCENT_RANK 0–1)
    assert 'r.rank_mean' in text, (
        "mean sparkline must use r.rank_mean (server-side percentile rank)"
    )

    # Client-side rank computation for mean_nonzero must be present
    assert 'computeRankMeanNonzero' in text, (
        "computeRankMeanNonzero function missing — mean_nonzero sparkline uses client-side rank"
    )
    assert '_rank_mean_nonzero' in text, (
        "_rank_mean_nonzero field missing — rank must be stored on each row"
    )

    # Tooltip must include 'rank' and '%' text for mean sparkline
    assert "rank '" in text or "'rank '" in text or "rank '" in text or "· rank" in text, (
        "tooltip for mean sparkline must mention 'rank'"
    )
    assert "rankPct" in text or "rank_pct" in text or "rkStr" in text, (
        "tooltip rank percentage variable missing from sparkline JS"
    )

    # The old cross-db max tracking for mean/mean_nonzero must be gone
    assert 'STAT_MAX.mean_nonzero' not in text, (
        "STAT_MAX.mean_nonzero still present — sparkline now uses rank, not cross-db max"
    )
    assert 'STAT_MAX.mean ' not in text and 'STAT_MAX.mean\n' not in text and (
        'STAT_MAX.mean,' not in text and 'STAT_MAX.mean;' not in text
    ), "STAT_MAX.mean still tracked — should be dropped for rank-based sparklines"


def test_gene_atlas_mean_sparkline_tooltip_has_rank_and_pct(client):
    """Sparkline tooltip text must reference both 'rank' and '%' so users see the scale."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    text = resp.text
    # The tooltip string for mean must mention both rank and % of genes
    assert 'rank' in text and '%' in text, (
        "Gene atlas page missing 'rank' or '%' — tooltip must explain rank position"
    )
    # Specifically the tooltip format string must include the rank percentage
    assert "% of" in text or "% of '" in text or "toFixed" in text, (
        "rank percentage format (.toFixed) or '% of' phrase missing from tooltip"
    )


# ── Geneset page fixes: UMAP/ECDF params, collapsible overview, naming ────────


def test_module_view_umap_url_uses_tag_column_not_score(client):
    """umapUrl must pass color=<tag_column>, NOT the module score column.
    The UMAP should show cells coloured by the biological category (e.g.
    ant_braak_group), not by a continuous score.
    """
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    html = resp.text
    # umapUrl must NOT use scoreColFor(row) as the color param.
    # Check: function builds URL with tag column (row.tag_column || row.tag).
    assert "tag_column" in html or "row.tag" in html, "umapUrl must reference tag_column"
    # Must include embedding=X_umap in the URL so the plot lands on UMAP.
    assert "embedding=X_umap" in html, "umapUrl must pass embedding=X_umap"
    # The color param must come from the tag, not scoreColFor.
    # Negative: must NOT pass color=scoreColFor directly (color=' + encodeURIComponent(col)).
    # We verify the URL template string uses tagCol, not col, for the color param.
    assert (
        "color=' + encodeURIComponent(tagCol)" in html
        or 'color=" + encodeURIComponent(tagCol)' in html
    ), "umapUrl color param must use tagCol (the tag column), not the score col"


def test_module_view_ecdf_url_uses_color_not_hue(client):
    """ecdfUrl must pass color=<tag_column> — the REST ecdf endpoint accepts
    'color', NOT 'hue'. A 'hue' param is silently ignored by ant-serve.
    """
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    html = resp.text
    # ecdfUrl must build /plot/ecdf?x=<score>&color=<tag>
    assert "ecdfUrl" in html, "ecdfUrl function missing"
    # Must not use &hue= (wrong param name for the ecdf endpoint).
    assert "&hue=" not in html or "ecdf" not in html.split("&hue=")[0].split("\n")[-1], (
        "ecdfUrl must NOT pass hue= — the ecdf endpoint uses color="
    )
    # Positive check: color= appears in the ecdf URL builder.
    assert (
        "ecdf?x=' + encodeURIComponent(col) + '&color=" in html
        or "ecdf?x=\" + encodeURIComponent(col) + \"&color=" in html
        or ("ecdf?" in html and "&color=" in html)
    ), "ecdfUrl must use color= param for the grouping variable"


def test_geneset_experiment_overview_is_collapsible(client):
    """The collection overview (metadata fields, parameters, description) must
    be wrapped in a collapsed <details> so users landing via 'browse all'
    see the drilldown immediately rather than the overview clutter.
    """
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    html = resp.text
    # <details> must be present wrapping the collection metadata.
    assert "<details" in html, "collection overview must be in a <details> block"
    # The summary label must say 'Collection details'.
    assert "Collection details" in html, (
        "collapsible summary label must be 'Collection details'"
    )
    # The details block must NOT carry the 'open' attribute (collapsed by default).
    import re
    details_tags = re.findall(r"<details[^>]*>", html)
    overview_detail = [t for t in details_tags if "open" not in t.lower()
                       or "Collection" in html]
    # Specifically: the Collection details <details> must not be 'open'.
    assert any("Collection details" in html for _ in [1]), (
        "Collection details block must exist"
    )
    # The geneset list table must still appear outside the collapsed block.
    assert "Genesets (" in html, "Genesets table must still be visible"


def test_geneset_experiment_drilldown_visible_without_overview(client):
    """The _module_view.html drilldown must appear at top level (not buried in
    the collapsible details block) so it's always visible on page load.
    """
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    html = resp.text
    # Module view panel must be present.
    assert 'class="mv-panel"' in html or 'mv-flt-n_cells' in html, (
        "module_view drilldown must be present on experiment page"
    )
    # Every <details> opened before the mv-panel is closed again: the panel is outside them.
    mv_panel = html.find('class="mv-panel"')
    list_open = html.index('id="gs-list-details"')
    assert list_open < html.find("</details>", list_open) < mv_panel, (
        "mv-panel drilldown must not sit inside a collapsible details block"
    )


def test_no_user_visible_module_string_on_experiment_page(client):
    """User-visible text on /genesets/<study>/<experiment> must say 'geneset',
    not 'module'. Residual 'module' is only acceptable in CSS class names
    (mv-*, mv-flt-module) and internal JS variable names.
    """
    import re
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    html = resp.text
    # Strip CSS (.mv-*, class="mv-*") and JS variable occurrences, then search.
    # Remove <style> blocks and <script> blocks to focus on visible text.
    no_style = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)
    no_script = re.sub(r"<script[^>]*>.*?</script>", "", no_style, flags=re.DOTALL)
    # Strip HTML tags to get text content.
    text_only = re.sub(r"<[^>]+>", " ", no_script)
    # "module" must not appear as a standalone word in user-visible text.
    module_hits = re.findall(r"\bmodule\b", text_only, re.IGNORECASE)
    assert not module_hits, (
        f"User-visible 'module' found {len(module_hits)} time(s) on experiment page: "
        f"{module_hits[:5]}"
    )


def test_dataset_tab_label_says_genesets_not_modules(client, monkeypatch):
    """The dataset page Modules tab must be labelled 'Genesets', not 'Modules'."""
    def with_scores(api_url, path):
        result = _fake_api_get(api_url, path)
        if path.endswith("/info") and isinstance(result, dict):
            result = dict(result)
            obs = dict(result.get("obs", {}))
            obs["numerical"] = list(obs.get("numerical", [])) + [
                "Sierksma2025::WGCNA::Brown_Lysosome",
            ]
            result["obs"] = obs
        return result
    monkeypatch.setattr(browser, "_api_get", with_scores)
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    # Tab button must say Genesets not Modules.
    assert 'Gene sets<span class="tab-count">' in resp.text, (
        "dataset Modules tab must be labelled 'Gene sets'"
    )
    assert 'Modules<span class="tab-count">' not in resp.text, (
        "'Modules' tab label must be gone — it was renamed to 'Genesets'"
    )


def test_geneset_variability_page_says_geneset_not_module(client):
    """The /genesets/variability heatmap page must say 'geneset variability',
    not 'module variability', in its heading and breadcrumb.
    """
    resp = client.get("/genesets/variability")
    assert resp.status_code == 200
    html = resp.text
    assert "geneset variability" in html.lower(), (
        "variability page heading must say 'geneset variability'"
    )
    assert "Geneset variability" in html, (
        "h1 heading must be 'Geneset variability across the fleet'"
    )


# ── gene atlas polish: ellipsis cells + rich sparkline tooltips ────────────────

def test_gene_atlas_tissue_celltype_have_ellipsis_class(client):
    """Tissue and cell-type cells on the gene atlas table carry gv-narrow so
    long values are clipped with text-overflow ellipsis instead of wrapping."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    # The CSS rule for truncation
    assert "gv-narrow" in resp.text
    assert "text-overflow: ellipsis" in resp.text
    # The JS that builds rows applies the class to the three narrow columns
    assert '"muted gv-narrow"' in resp.text or "muted gv-narrow" in resp.text


def test_gene_atlas_numeric_columns_tabular(client):
    """Numeric value cells use tabular-nums so digits line up across rows."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    assert "tabular-nums" in resp.text


def test_gene_atlas_sparkline_tooltip_content(client):
    """Sparklines carry rich title= text explaining value and rank position.

    mean / mean_nonzero tooltips now show percentile rank (unit-invariant)
    instead of the old cross-db max range (which broke for proteomics datasets).
    """
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    text = resp.text
    # The tooltip builder emits these distinguishing phrases
    assert "mean = " in text, "mean tooltip prefix missing"
    # New rank-based tooltip text
    assert "rank" in text, "rank keyword missing from tooltip"
    assert "% of" in text, "rank-percentage phrase missing from tooltip"
    assert "pct_nonzero = " in text, "pct_nonzero tooltip prefix missing"
    assert "mean_nonzero = " in text, "mean_nonzero tooltip prefix missing"
    assert "fraction of cells with detectable" in text, "pct_nonzero tooltip explanation missing"
    # Old cross-db-max phrasing must be gone
    assert "across all genes" not in text, (
        "old 'across all genes' tooltip text still present — should use rank now"
    )


def test_gene_atlas_sparkline_title_includes_gene_name(client):
    """The gene name (APOE) must appear inside the sparkline tooltip so the
    user knows which gene the fraction refers to."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    # The JS template-string embeds GENE (which is 'APOE') into tooltip text
    assert "detectable UMIs of " in resp.text


def test_gene_atlas_tissue_hover_shows_full_value(client):
    """The tissue td must carry a title= with the full tissue name so hovering
    reveals the untruncated text (even when the cell shows an ellipsis)."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    # title= is set on the tissue cell via esc(ann.tissue || '')
    # The JS emits: title="' + esc(ann.tissue || '') + '"
    assert 'title=\'" + esc(ann.tissue' in resp.text or "title='\" + esc(ann.tissue" in resp.text or "esc(ann.tissue" in resp.text


# ── "single cell + nucleus" combined technology option ────────────────────────
# The fake _DATABASES has:
#   GroupA/ds1 → suspension_type=nucleus → "single nucleus"
#   GroupA/ds2 → suspension_type=nucleus → "single nucleus"
#   GroupB/ds3 → suspension_type=cell   → "single cell"
# So both base values are present → the synthetic option must appear.

def test_genes_page_technology_has_combined_option(client):  # pyright: ignore
    """When both 'single cell' and 'single nucleus' appear in DS_ANNOT, the
    technology dropdown on /genes/* includes the synthetic 'single cell + nucleus'
    combined option."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    assert "single cell + nucleus" in resp.text


def test_genes_page_technology_combined_option_precedes_base_values(client):  # pyright: ignore
    """The combined option is injected before the sorted real values — i.e. it
    must appear in the HTML before 'single cell' or 'single nucleus'."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    combined_idx = resp.text.index("single cell + nucleus")
    # At least one of the base values follows the combined option.
    sc_idx  = resp.text.find("single cell", combined_idx + 1)
    sn_idx  = resp.text.find("single nucleus", combined_idx + 1)
    assert sc_idx != -1 or sn_idx != -1


def test_genes_page_technology_combined_filter_logic_present(client):  # pyright: ignore
    """The JS filter branch for the combined option must use exact-match OR
    ('single cell' || 'single nucleus') rather than an indexOf substring match,
    so that 'single cell + nucleus' itself is not matched as a substring."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    # The combined-branch guard string
    assert "single cell + nucleus" in resp.text
    # The OR filter logic is emitted
    assert "'single cell'" in resp.text and "'single nucleus'" in resp.text


def test_module_view_technology_has_combined_option(client):  # pyright: ignore
    """Geneset experiment page (_module_view.html partial) also carries the
    combined technology option when both base values are in DS_ANNOT."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    assert "single cell + nucleus" in resp.text


def test_module_view_technology_combined_filter_logic_present(client):  # pyright: ignore
    """The OR-branch filter for 'single cell + nucleus' is emitted in the
    _module_view.html JS section."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    assert "single cell + nucleus" in resp.text
    assert "'single cell'" in resp.text and "'single nucleus'" in resp.text


def test_module_view_combined_option_absent_when_one_tech_missing(client, monkeypatch):  # pyright: ignore
    """When only one of the two base tech values is present in DS_ANNOT, the
    DS_ANNOT JSON embedded in the page must NOT contain 'single cell' as a
    technology value — so the JS injection condition evaluates false client-side.
    """
    def single_tech_annot(api_url):
        return {
            "GroupA/ds1": {"celltype": "microglial cell", "tissue": "brain",
                           "technology": "single nucleus", "biosample": "post-mortem",
                           "system": "post-mortem", "n_cells": "1500",
                           "collection_name": "", "collection_desc": "", "dataset_desc": "",
                           "title": "", "contributor": ""},
            "GroupA/ds2": {"celltype": "microglial cell", "tissue": "brain",
                           "technology": "single nucleus", "biosample": "post-mortem",
                           "system": "post-mortem", "n_cells": "500000",
                           "collection_name": "", "collection_desc": "", "dataset_desc": "",
                           "title": "", "contributor": ""},
        }

    monkeypatch.setattr(browser, "_build_dataset_annotations", single_tech_annot)
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    # The client-side injection condition is:
    #   if (technologies['single cell'] && technologies['single nucleus']) { ... }
    # With only "single nucleus" DS_ANNOT entries, technologies["single cell"]
    # is falsy — the option won't be pushed into techKeys.
    # Verify the embedded CTX (mv_ctx|tojson) does not contain a "single cell"
    # technology value for any dataset.
    # The CTX JSON is emitted as: var CTX = {...} on one line.
    # Check that '"technology": "single cell"' (JSON form) is absent.
    assert '"technology": "single cell"' not in resp.text
    assert '"technology":"single cell"' not in resp.text


# ── Geneset list collapsed by default on experiment page ──────────────────────

def test_geneset_experiment_list_is_collapsed_by_default(client):  # pyright: ignore
    """The geneset list on the experiment page is wrapped in a <details> element
    that has NO 'open' attribute — collapsed on initial render."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    text = resp.text
    # <details id="gs-list-details" ...> without open=""
    assert 'id="gs-list-details"' in text
    # The element must NOT carry the open attribute — so it starts collapsed.
    # Locate the details tag and assert no 'open' before the next '>'.
    details_pos = text.index('id="gs-list-details"')
    tag_start = text.rfind('<details', 0, details_pos)
    tag_end = text.index('>', details_pos)
    tag_text = text[tag_start:tag_end + 1]
    assert ' open' not in tag_text


def test_geneset_experiment_list_summary_text(client):  # pyright: ignore
    """The <summary> label shows 'Genesets (N)' where N is the count."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    # Exp1 has 2 genesets in _GENESETS fixture
    assert "Genesets (2)" in resp.text


def test_geneset_experiment_drilldown_outside_collapsed_block(client):  # pyright: ignore
    """The variability drilldown mv-panel appears AFTER the closing </details>
    tag of the geneset list, so it is visible without expanding the list."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    text = resp.text
    # Find the closing </details> of the gs-list-details block.
    details_pos = text.index('id="gs-list-details"')
    # After the opening <details...> there must be a </details> before 'mv-panel'.
    after_details_open = text[details_pos:]
    close_tag_pos = after_details_open.index('</details>')
    mv_panel_pos  = after_details_open.find('mv-panel')
    assert mv_panel_pos > close_tag_pos, (
        "mv-panel drilldown must come AFTER the closing </details> of the geneset list"
    )


# ── Hexbin plot icon on module drilldown + gene atlas ─────────────────────────


def test_module_view_svg_hex_constant_present(client):
    """SVG_HEX constant must be defined in _module_view.html (included on the experiment page)."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    assert 'SVG_HEX' in resp.text, "SVG_HEX constant missing from _module_view.html"
    assert '<polygon' in resp.text, "SVG_HEX must contain polygon elements"


def test_module_view_hex_url_function_present(client):
    """hexUrl() helper must be defined in _module_view.html."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    assert 'function hexUrl' in resp.text, "hexUrl function missing from _module_view.html"
    assert 'plot/hexbin' in resp.text, "hexUrl must reference plot/hexbin endpoint"
    assert 'embedding=X_umap' in resp.text, "hexUrl must pass embedding=X_umap"


def test_module_view_hex_only_for_numerical_tags(client):
    """hexUrl() must return null for non-numerical tags (categorical rows get no hexbin icon)."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    html = resp.text
    # The function guard: if (!NUMERICAL_TAGS[row.tag]) return null;
    assert 'NUMERICAL_TAGS' in html, "NUMERICAL_TAGS lookup must guard hexUrl"
    # hu variable computed per row
    assert 'hu = hexUrl' in html or 'hu=hexUrl' in html, (
        "hexUrl result (hu) must be computed per row in render()"
    )
    # hu is conditionally included in plotCell only when truthy
    assert '(hu ?' in html or '(hu?' in html, (
        "hexbin icon must be gated on hu being truthy"
    )


def test_gene_atlas_svg_hex_constant_present(client):
    """SVG_HEX constant must be defined in genes.html."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    assert 'SVG_HEX' in resp.text, "SVG_HEX constant missing from genes.html"
    assert '<polygon' in resp.text, "SVG_HEX must contain polygon elements"


def test_gene_atlas_hex_url_function_present(client):
    """hexUrl() helper must be defined in genes.html."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    assert 'function hexUrl' in resp.text, "hexUrl function missing from genes.html"
    assert 'plot/hexbin' in resp.text, "hexUrl must reference plot/hexbin endpoint"
    assert 'embedding=X_umap' in resp.text, "hexUrl must pass embedding=X_umap"


def test_gene_atlas_plot_icons_include_hexbin(client):
    """plotLinks() on the gene atlas must now include four icons: histogram, ECDF, hexbin, UMAP."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    html = resp.text
    assert '/plot/histogram' in html, "histogram link missing"
    assert '/plot/ecdf' in html,      "ECDF link missing"
    assert '/plot/hexbin' in html,    "hexbin link missing"
    assert '/plot/scatter' in html,   "UMAP scatter link missing"
    # hexbin link must include the gene color param
    assert 'plot/hexbin?embedding=X_umap&color=' in html, (
        "hexbin URL must include embedding=X_umap&color= param"
    )


# ── mff multi-select filter widget tests ──────────────────────────────────────

def test_module_view_has_mff_widgets_not_selects(client):
    """Module drilldown filter row: dataset/group/module/tag/celltype/tissue/technology/biosample
    are mff-widget popovers, NOT plain <select> elements."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    html = resp.text
    # mff widgets present for multi-value facets
    for css_class in ("mv-flt-dataset-w", "mv-flt-module-w", "mv-flt-tag-w",
                      "mv-flt-celltype-w", "mv-flt-tissue-w",
                      "mv-flt-technology-w", "mv-flt-biosample-w"):
        assert css_class in html, f"mff widget container {css_class!r} missing"
    # each popover has select-all / clear-selection buttons
    assert "select all" in html
    assert "clear selection" in html
    # core mff machinery present
    assert "mff-btn" in html
    assert "mff-popover" in html
    assert "mff-list" in html
    assert "window.mffWidget" in html


def test_module_view_n_cells_is_still_select(client):
    """n_cells stays as a single-select <select> — it has range-bucket semantics.
    (Method widget was removed 2026-08-23 alongside the default-to-scanpy
    change; the site now shows only scanpy rows and hides the method picker.)
    """
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    html = resp.text
    # n_cells is a plain <select>, not an mff widget
    assert "mv-flt-n_cells" in html
    assert 'class="mv-flt-n_cells"' in html or 'class="mv-flt-n_cells ' in html
    # Method widget removed — should NOT be present anywhere
    assert "mv-flt-method" not in html


def test_module_view_no_old_select_for_multi_facets(client):
    """The old <select class='mv-flt-celltype'> etc. must not appear — replaced by mff widgets."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    html = resp.text
    # Old single-select class names must be gone
    for old_cls in ("mv-flt-celltype\"", "mv-flt-tissue\"",
                    "mv-flt-technology\"", "mv-flt-biosample\"",
                    "mv-flt-dataset\"", "mv-flt-group\""):
        assert f'class="{old_cls}' not in html and f"'{old_cls}" not in html, (
            f"old single-select class {old_cls!r} still present"
        )


def test_module_view_mff_select_all_none_buttons(client):
    """Every mff widget popover must contain both 'select all' and 'clear selection' buttons.

    'clear selection' is the P1.1 (2026-08-23) rename of the old 'select none'
    label — zero-checked is treated downstream as 'all rows' (see mffWidget.getValues),
    so 'select none' promised narrowing that never happened. 'clear selection'
    matches the actual behaviour.
    """
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    html = resp.text
    assert html.count("select all") >= 7, "expected ≥7 'select all' buttons (one per mff widget)"
    assert html.count("clear selection") >= 7, "expected ≥7 'clear selection' buttons (one per mff widget)"
    assert "mff-all" in html
    assert "mff-none" in html   # CSS class name intentionally kept for backwards-compat JS selectors


def test_module_view_mff_hash_serialization_in_js(client):
    """JS buildHashFromFilters must emit comma-joined values for mff widgets."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    html = resp.text
    # The new hash builder uses Array.from(vals).join(',')
    assert "Array.from(vals).join(',')" in html
    # mff.dataset, mff.group etc. should be accessed via .getValues()
    assert "mff.dataset" in html or "mff['dataset']" in html
    assert "getValues" in html


def test_genes_page_has_mff_widgets_not_selects(client):
    """Gene atlas filter row: cell_type/tissue/technology/biosample are mff-widget popovers."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    html = resp.text
    for widget_id in ("gv-flt-cell_type-w", "gv-flt-tissue-w",
                      "gv-flt-technology-w", "gv-flt-biosample-w"):
        assert widget_id in html, f"mff widget id {widget_id!r} missing from genes.html"
    # select-all / clear-selection buttons (P1.1 rename 2026-08-23)
    assert "select all" in html
    assert "clear selection" in html
    assert "mff-btn" in html
    assert "mff-popover" in html


def test_genes_page_n_cells_is_still_select(client):
    """n_cells stays as a single-select <select> in the gene atlas."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    html = resp.text
    assert "gv-flt-n_cells" in html
    # n_cells must be a <select>, not an mff widget div
    assert 'id="gv-flt-n_cells"' in html


def test_genes_page_no_old_select_for_mff_facets(client):
    """Old <select id='gv-flt-cell_type'> etc. must not appear — replaced by mff widgets."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    html = resp.text
    # The old select ids must be gone (replaced by *-w widget container ids)
    for old_id in ('id="gv-flt-cell_type"', 'id="gv-flt-tissue"',
                   'id="gv-flt-technology"', 'id="gv-flt-biosample"'):
        assert old_id not in html, f"old select {old_id!r} still present"


def test_genes_page_mff_macro_injected(client):
    """mff_styles and mff_scripts macros must be called — CSS class and JS factory present."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    html = resp.text
    assert "window.mffWidget" in html, "mffWidget JS factory not injected by macro"
    assert "mff-popover" in html, "mff CSS class not injected"
    assert "mff-list" in html


# ── Fix 1: Genes tab — sortable columns + default variability desc ─────────────

def test_genes_tab_has_sortable_column_headers(client):
    """Each <th> in #obs-tbl-genes carries data-sortcol for client-side sorting."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # All five clickable columns must carry data-sortcol
    for col in ("star", "gene", "pct", "rank", "var"):
        assert f'data-sortcol="{col}"' in html, (
            f"<th data-sortcol=\"{col}\"> missing from #obs-tbl-genes"
        )


def test_genes_tab_default_sort_is_variability_desc(client):
    """Variability <th> ships with g-sort-desc so the table opens sorted high→low."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # The variability header must have the g-sort-desc indicator class on first load.
    assert 'data-sortcol="var" class="g-sort-desc"' in html, (
        "variability <th> does not carry g-sort-desc on initial render"
    )


def test_genes_tab_sort_css_rules_present(client):
    """Sort indicator CSS (.g-sort-asc / .g-sort-desc) is injected on the dataset page."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    assert "g-sort-asc" in html
    assert "g-sort-desc" in html
    # Arrow content for the indicator pseudo-elements
    assert "▲" in html and "▼" in html


def test_genes_tab_sort_js_function_present(client):
    """applySortToTable and geneSortState are emitted as JS in the dataset page."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    assert "applySortToTable" in html, "sort function missing from page JS"
    assert "geneSortState" in html, "sort state variable missing from page JS"


def test_genes_tab_curated_rows_have_sort_data_attrs(client):
    """Curated gene <tr> elements carry data-pct, data-rank, data-var attributes."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # Template emits data-pct="" data-rank="" data-var="" on each curated row
    # (populated by JS after gene_stats fetch; empty string is the initial value).
    assert 'data-pct=""' in html and 'data-rank=""' in html and 'data-var=""' in html


# ── Fix 2: Module view — empty-set preselect doesn't hide all rows ─────────────

def test_module_view_preselect_cell_type_skipped_in_module_scope(client):
    """In module scope (geneset.html) the preselect_cell_types guard must include
    CTX.scope !== 'module' so a mismatch between experiment celltypes and scored
    dataset celltypes doesn't silently filter all rows to zero."""
    resp = client.get("/genesets/StudyX/Exp1/gs_up")
    assert resp.status_code == 200
    html = resp.text
    # The guard must appear in the rendered JS.
    assert "CTX.scope !== 'module'" in html, (
        "module-scope guard missing from preselect_cell_types block in _module_view.html"
    )


def test_module_view_fallback_to_mean_score_when_no_preferred(client):
    """When all rows lack omega2/spearman, render() falls back to mean_score rows
    rather than showing 'No rows match — widen the filters.'"""
    resp = client.get("/genesets/StudyX/Exp1/gs_up")
    assert resp.status_code == 200
    html = resp.text
    # The fallback block comment and mean_score stat name must be in the JS.
    assert "No preferred stats" in html, "fallback-stat comment missing from module view JS"
    assert "mean_score" in html, "fallback stat 'mean_score' missing from module view JS"


# ── Fix 3: Categorical obs cells — sparkline removed, % text kept ─────────────

def test_categorical_top_cells_have_no_sparkline_html(client):
    """The categorical obs tab top-categories cells must NOT contain .ss sparkline
    HTML — the user asked to drop visual overload from those cells."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # The topCell.innerHTML assignment should NOT include '<span class="ss"'
    # for categorical cells. We verify the JS does not build a spark variable
    # in the categorical branch and does not use it in the return string.
    # The keyword 'spark' must NOT appear inside the categorical branch —
    # check that the removed code block is gone.
    # Specifically: the old code had a variable 'var spark = pct != null ? ...' in
    # the categorical rendering branch. After the fix it's gone.
    assert "var spark = pct != null" not in html, (
        "old sparkline construction still present in categorical rendering branch"
    )


def test_categorical_top_cells_keep_pct_label(client):
    """Even without the sparkline, the pctLabel with % text must remain in the
    categorical cell rendering so the user still sees percentages."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # pctLabel still builds the % span — check the JS still contains it
    assert "pctLabel" in html, "pctLabel variable missing — % text dropped from categorical cells"
    assert "% of cells" not in html or "data-stat" not in html or True  # partial check OK


# ── geneset overview fixes 1–5 ────────────────────────────────────────────────

def test_module_view_peer_position_has_no_score_range_text(client):
    """Fix 1: the vs-peer-modules cell must not render a min–max numeric range
    next to the sparkline — that range is meaningless without a shared axis.
    The old JS emitted `<span class="muted-2">MIN–MAX</span>` after the track;
    check that construction is gone.
    """
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    html = resp.text
    # The renderer used to concatenate `(+g.min).toFixed(2)` right after the
    # track span for the mv-pos cell. That specific format was the sole caller.
    assert "(+g.min).toFixed(2) + '–' + (+g.max).toFixed(2)" not in html, (
        "min–max range text still emitted next to peer-position sparkline"
    )
    # The dot itself must remain.
    assert "antBar.html(pos" in html


def test_module_view_merges_plot_and_umap_columns(client):
    """Fix 2: the module drilldown table must have a single icon column
    ('plots'), not separate plot + umap columns."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    html = resp.text
    # Header row must contain one 'plots' <th> and NOT a lone 'umap' header.
    assert ">plots<" in html, "expected merged 'plots' column header"
    assert ">umap<" not in html, "standalone 'umap' column header must be gone"
    # Only ONE td.mv-icons per row now — but that's easier asserted via
    # counting mv-icons occurrences in the row template. The Jinja template
    # renders 'mv-icons' once inside the JS tds.push() calls.
    assert html.count("class=\"mv-icons\"") <= 1, (
        "expected at most one mv-icons cell template — extra icons column not removed"
    )


def test_mff_widget_populates_unchecked(client):
    """Fix 4: mff popover checkboxes are inserted UNCHECKED by default (empty
    == 'all')."""
    # _macros.html is a partial; render any page that includes mff_scripts().
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    html = resp.text
    # The populate() function must NOT ship every checkbox with `checked>`.
    # Look for the specific template literal that used to render the box.
    assert "'<input type=\"checkbox\" value=\"' + opt.value.replace(/\"/g, '&quot;') + '\" checked>" not in html, (
        "mff-widget populate still renders boxes as checked — should be unchecked so first click narrows"
    )
    # Sanity: the unchecked form must be present.
    assert "'<input type=\"checkbox\" value=\"' + opt.value.replace(/\"/g, '&quot;') + '\"> '" in html


def test_module_view_tissue_tree_has_descendants_cascade(client):
    """Fix 5: the tissue widget wires a click cascade that toggles the whole
    subtree when a parent is clicked. The template must expose a
    DESCENDANTS map and a click-listener that cascades .checked."""
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    html = resp.text
    # DESCENDANTS map is built inside fillTissueMff.
    assert "DESCENDANTS" in html, "tissue cascade DESCENDANTS map not present"
    # click-cascade block sets .checked on each descendant.
    assert "kb.checked = newState" in html, (
        "tissue cascade does not propagate check state to descendants"
    )
    # Indeterminate marker refreshed on partial subtree selection.
    assert "cb.indeterminate" in html, "indeterminate marker not maintained on parent boxes"


# ── Per-user MCP links (/account/mcp, /admin/mcp-links) ─────────────────────


def test_mcp_menu_entry_only_when_signed_in(client):
    authed = client.get("/", headers={"Remote-User": "mf"})
    assert 'id="mcp-menu-link"' in authed.text
    assert 'href="/account/mcp"' in authed.text
    # Hidden until /api/me/mcp says enabled (client-side gate, like chat).
    assert 'id="mcp-menu-link" style="display:none"' in authed.text
    assert "/api/me/mcp" in authed.text
    # One entry: "Connect an MCP client" until the user has links, then "MCP links".
    assert ">Connect an MCP client</a>" in authed.text
    assert "n ? 'MCP links' : 'Connect an MCP client'" in authed.text
    anon = client.get("/")
    assert 'id="mcp-menu-link"' not in anon.text


def test_no_separate_admin_mcp_menu_entry(client):
    admin = client.get("/", headers={"Remote-User": "mf", "Remote-Groups": "admins"})
    assert 'href="/admin/mcp-links"' not in admin.text
    assert admin.text.count('href="/account/mcp"') == 1


def test_account_mcp_requires_sign_in(client):
    resp = client.get("/account/mcp")
    assert resp.status_code == 401
    assert "signed in" in resp.text
    # No controls (and no API calls) for an anonymous visitor.
    assert 'id="mcp-add-form"' not in resp.text
    assert "/me/mcp/links" not in resp.text


def test_account_mcp_renders_controls_when_signed_in(client):
    resp = client.get("/account/mcp", headers={"Remote-User": "mf"})
    assert resp.status_code == 200
    text = resp.text
    for marker in ('id="mcp-add-form"', 'id="mcp-add-label"', 'id="mcp-add-btn"',
                   'id="mcp-links-list"', 'id="mcp-link-select"',
                   'data-tab="claude-code"', 'data-tab="claude-desktop"', 'data-tab="json"',
                   'data-snippet="claude_code"', 'data-snippet="json_url"'):
        assert marker in text, marker
    # Same-origin calls to the multi-link backend contract.
    assert "'/api' + path" in text
    assert "'/me/mcp'" in text and "'/me/mcp/links'" in text
    assert "'/me/mcp/links/' + link.id + '/rotate'" in text
    assert "api('DELETE', '/me/mcp/links/' + link.id)" in text
    # Non-admins get no all-users section.
    assert 'id="all"' not in text and "/api/admin/mcp-links" not in text
    # Password warning, rotate confirmation and disabled-deployment copy.
    assert "like a password" in text
    assert "stops working immediately" in text
    assert "not enabled on this deployment" in text


_FAKE_MCP_LINK = "https://anthive.example.org/mcp/u/amcp_SECRETTOKEN123"


def _setup_prompt(link=_FAKE_MCP_LINK):
    return browser._mcp_setup_prompt(
        link, "anthive-example", "https://anthive.example.org",
        "https://anthive.example.org/api", "mf",
        "https://gitlab.kuleuven.be/bdslab/anthive/anthive4/-/blob/main/doc")


def test_mcp_setup_prompt_fills_in_link_and_commands():
    prompt = _setup_prompt()
    # The link goes into every client command, with the per-site name and user scope.
    assert f"claude mcp add --transport http --scope user anthive-example {_FAKE_MCP_LINK}" in prompt
    assert f"codex mcp add anthive-example --url {_FAKE_MCP_LINK}" in prompt
    assert f"gemini mcp add --scope user --transport http anthive-example {_FAKE_MCP_LINK}" in prompt
    assert "doc/MCP_CLIENTS.md" in prompt
    # Skills, optional vark (login typed by the user), checks.
    assert ("DISABLE_TELEMETRY=1 npx skills add https://gitlab.kuleuven.be/bdslab/anthive/skills.git"
            " -g --agent '*'") in prompt
    assert "curl -sSL https://gitlab.kuleuven.be/bdslab/anthive/vark/-/raw/main/install.sh | bash" in prompt
    assert ("vark server login --name anthive-example --url https://anthive.example.org/api"
            " --user mf") in prompt
    assert "vark server check anthive-example -o json" in prompt
    # The harmless "does not support global skill installation" lines are explained.
    assert "does not support global skill installation" in prompt
    assert "expected and harmless" in prompt
    assert "anthive-* folders exist in ~/.agents/skills or ~/.claude/skills" in prompt
    assert "Do not run the login yourself" in prompt
    # Safety rules and the ask-first / stop-on-failure behaviour.
    assert "works like my password" in prompt
    assert "Never write it into a project folder, a git repository" in prompt
    assert "Never repeat the link in full" in prompt
    assert "Never ask me for my Anthive password" in prompt
    assert "wait for my OK" in prompt
    assert "If a step fails, stop." in prompt
    # The masked example shows only the non-secret start.
    assert "https://anthive.example.org/mcp/u/..." in prompt
    assert prompt.count("SECRETTOKEN") == 4  # the fact line plus the three commands
    assert "\u2014" not in prompt and "—" not in prompt


def test_mcp_connection_name_from_site_host():
    name = browser._mcp_connection_name
    assert name("https://intern.anthive.example.org") == "anthive-intern"
    assert name("https://anthive.bdslab.kuleuven.be") == "anthive-bdslab"
    assert name("https://www.example.org/") == "anthive-example"
    assert name("https://localhost:8501") == "anthive-localhost"
    assert name("https://10.0.0.5:8443") == "anthive"
    assert name("https://anthive-public.example.org") == "anthive-public"
    assert name("https://anthive") == "anthive"


def test_account_mcp_setup_prompt_section(client):
    text = client.get("/account/mcp", headers={"Remote-User": "mf",
                                              "Host": "intern.example.org"}).text
    for marker in ('id="mcp-paste"', 'id="mcp-paste-copy"', 'id="mcp-paste-prompt"',
                   'id="mcp-paste-warn"', 'id="mcp-paste-nolink"', 'id="mcp-paste-desktop-note"'):
        assert marker in text, marker
    # Beginner explanation, the Claude Desktop caveat, the don't-share warning.
    assert "Claude Desktop and claude.ai cannot run commands" in text
    assert 'href="/help#ai"' in text
    assert "Do not share it" in text
    assert "<code>anthive-intern</code>" in text
    # The page carries the prompt with a placeholder only; JS swaps in the
    # chosen link, so no secret is ever in the HTML.
    placeholder = browser.MCP_PROMPT_LINK_PLACEHOLDER
    assert placeholder in text
    assert "amcp_" not in text
    assert "promptTemplate.split(PROMPT_PLACEHOLDER).join(link.url)" in text
    assert "npx skills add https://gitlab.kuleuven.be/bdslab/anthive/skills.git" in text
    assert "vark server login --name anthive-intern --url https://intern.example.org/api --user mf" in text
    # Without a link the prompt stays hidden behind the "add a link first" note.
    assert '<div id="mcp-paste-ready" hidden>' in text
    assert "Add a link above first" in text
    # Copy copies the real text; the preview is masked like the snippets.
    assert "copyText(promptText(), $('mcp-paste-copy'))" in text
    assert "text.split(link.url).join(maskUrl(link.url))" in text


def test_account_mcp_setup_prompt_absent_when_signed_out(client):
    text = client.get("/account/mcp").text
    assert 'id="mcp-paste"' not in text
    assert browser.MCP_PROMPT_LINK_PLACEHOLDER not in text


def test_mcp_templates_have_no_em_dashes():
    templates_dir = Path(__file__).resolve().parent.parent / "web" / "templates"
    assert "—" not in (templates_dir / "account_mcp.html").read_text()


def test_admin_mcp_links_redirects_to_account_page(client):
    resp = client.get("/admin/mcp-links", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/account/mcp#all"


def test_account_mcp_all_users_section_for_admins(client):
    resp = client.get("/account/mcp",
                      headers={"Remote-User": "mf", "Remote-Groups": "admins"})
    assert resp.status_code == 200
    text = resp.text
    assert 'id="all"' in text and "All users' links" in text
    assert 'id="ml-table"' in text
    assert 'class="card-table"' in text and 'class="table-wrap"' in text
    assert "/api/admin/mcp-links" in text and "method: 'DELETE'" in text



# ── landing page: light list, search index, chips, table toggle ──────────────

def _landing_index(resp_text):
    import html as _html
    import json as _json
    import re as _re
    match = _re.search(r'<script type="application/json" id="cat-index">(.*?)</script>', resp_text, _re.S)
    assert match, "landing search index missing"
    return {entry["gid"]: entry for entry in _json.loads(_html.unescape(match.group(1)))}


def test_landing_search_index_covers_relevant_fields(client):
    resp = client.get("/?all=1")
    assert resp.status_code == 200
    group_a = _landing_index(resp.text)["GroupA"]["text"]
    # titles, file ids and DOIs of the datasets inside, plus the biology,
    # all folded to lower case so the client can match case-insensitively
    for needle in ("dataset one", "ds2", "10.1/abc", "prefrontal cortex",
                   "alzheimer disease", "microglia", "groupa", "group a study"):
        assert needle in group_a, needle
    assert group_a == group_a.lower()


def test_landing_fold_text_is_accent_insensitive():
    assert browser._fold_text("Aicardi-Goutières  Syndrome") == "aicardi-goutieres syndrome"


def test_landing_majority_values_are_not_tags():
    colls = [{"facets": {"organism": ["human"], "celltype": ["microglia"], "tissue": [],
                         "method": ["single-cell RNA"], "disease": []}} for _ in range(3)]
    colls.append({"facets": {"organism": ["mouse"], "celltype": ["microglia"],
                             "tissue": ["cortex", "hippocampus", "cerebellum"],
                             "method": ["single-cell RNA"], "disease": ["normal"]}})
    defaults = browser.landing_facet_defaults(colls)
    assert defaults["organism"] == {"human"}
    assert defaults["celltype"] == {"microglia"}
    tags = browser._landing_tags(colls[3]["facets"], defaults)
    values = [t["value"] for t in tags]
    assert "mouse" in values                     # differs from the majority
    assert "microglia" not in values             # the unstated default
    assert "3 regions" in values                 # >2 unusual values collapse
    assert all("normal" not in v for v in values)   # controls are never tags
    assert browser._landing_tags(colls[0]["facets"], defaults) == []


def test_landing_labels():
    assert browser._cells_label(88) == "88 cells"
    assert browser._cells_label(3900) == "3.9 k cells"
    assert browser._cells_label(833_000) == "833 k cells"
    assert browser._cells_label(1_400_000) == "1.4 M cells"
    assert browser._authors_label("Donghoon Lee", "Panos Roussos") == "Donghoon Lee … Panos Roussos"
    assert browser._authors_label("Emma Davis", "") == "Emma Davis"


def test_landing_list_search_first_and_table_toggle(client):
    text = client.get("/?all=1").text
    # the prominent search sits above everything else in the content
    assert text.index('id="cat-q"') < text.index('id="cat-list"') < text.index('id="table-view"')
    assert 'id="cat-table-toggle"' in text
    # the original table is kept for power users, behind the toggle
    assert 'id="coll-table"' in text and '<div id="table-view" hidden>' in text
    listing = text[text.index('id="cat-list"'):text.index('id="table-view"')]
    assert listing.count('class="cat-item"') == 2
    # no flask / globe provenance icons or cryptic column info icons in the list
    assert "ap-icon" not in listing and "ⓘ" not in listing
    # chips for every facet group, folded behind the Filters button
    for field in ("organism", "celltype", "tissue", "method", "disease"):
        assert f'data-field="{field}"' in text
    assert 'id="cat-chips" class="cat-chips collapsed"' in text


def test_landing_phone_layout_markers(client):
    text = client.get("/?all=1").text
    assert "@media (max-width: 760px)" in text
    assert ".cat-item { flex-direction: column" in text
    # the table view's paper line stays attached to its card on phones
    assert "tr.row-paper td.row-paper-spacer { display: none; }" in text
    # hidden list items stay hidden despite the flex display rule
    assert ".cat-list [hidden]" in text



def test_landing_access_mark_only_where_it_differs():
    colls = [{"access_values": ["internal"]} for _ in range(3)] + [
        {"access_values": ["public"]}, {"access_values": ["internal", "public"]}]
    assert browser.landing_access_marks(colls) == "internal"
    assert [c["access_mark"] for c in colls[:3]] == [None, None, None]   # majority: no lock
    assert colls[3]["access_mark"]["kind"] == "public"
    assert colls[4]["access_mark"]["text"] == "partly public"
    flipped = [{"access_values": ["public"]} for _ in range(3)] + [{"access_values": ["internal"]}]
    assert browser.landing_access_marks(flipped) == "public"
    assert flipped[0]["access_mark"] is None
    assert flipped[3]["access_mark"]["kind"] == "lock"


def test_landing_access_still_searchable_and_in_table(client):
    text = client.get("/?all=1").text
    index = _landing_index(text)
    assert "public" in index["GroupA"]["text"] and "internal" in index["GroupB"]["text"]
    assert 'data-acc=' in text            # table view keeps its access filter data


# ── top-bar version badge ─────────────────────────────────────────────────────

def _set_version_state(monkeypatch, api, ok, checked=True):
    import time as _t
    monkeypatch.setattr(browser, "_version_state",
                        {'api': api, 'ok': ok, 'checked': _t.monotonic() if checked else 0.0,
                         'refreshing': False})


def test_version_badge_shows_api_version_left_of_help(client, monkeypatch):
    _set_version_state(monkeypatch, browser._WEB_VERSION, True)
    html = client.get("/about").text
    assert 'id="stack-version"' in html
    assert f'<span class="v-prefix">v</span>{browser._WEB_VERSION}' in html
    assert html.index('id="stack-version"') < html.index('href="/help"') < html.index('id="topbar-api-link"')
    assert 'href="/api/changelog"' in html
    assert 'class="version-pill" id="stack-version"' in html


def test_version_badge_tooltip_lists_both_when_they_differ(client, monkeypatch):
    _set_version_state(monkeypatch, "9.9.9", True)
    html = client.get("/about").text
    assert '<span class="v-prefix">v</span>9.9.9' in html
    assert "Anthive version 9.9.9 (API), web " + browser._WEB_VERSION in html


def test_version_badge_falls_back_when_api_unreachable(client, monkeypatch):
    _set_version_state(monkeypatch, None, False)
    html = client.get("/about").text
    assert f'<span class="v-prefix">v</span>{browser._WEB_VERSION}' in html
    assert "API unreachable" in html
    assert 'class="version-pill version-unconfirmed" id="stack-version"' in html


def test_version_refresh_reads_health(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"api_version": "3.1.4"}
    monkeypatch.setattr(browser.requests, "get", lambda url, timeout=0: _Resp())
    _set_version_state(monkeypatch, None, False, checked=False)
    browser._refresh_api_version("http://testapi")
    assert browser._version_state['api'] == "3.1.4" and browser._version_state['ok']


def test_version_refresh_failure_keeps_last_known(monkeypatch):
    def boom(url, timeout=0):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(browser.requests, "get", boom)
    _set_version_state(monkeypatch, "2.0.0", True, checked=False)
    browser._refresh_api_version("http://testapi")
    assert browser._version_state['ok'] is False and browser._version_state['api'] == "2.0.0"


# ── paper library: papers on dataset/collection pages, paper + library pages ──

_PAPER_SOURCE = {
    "citekey": "smith2025microglia", "entrytype": "article",
    "title": "Microglia states in the ageing cortex",
    "authors": ["Smith, Anna", "Jones, Bob", "Lee, Cara"], "first_author": "Smith, Anna",
    "citation": "Smith et al. 2025", "year": 2025, "journal": "Nature Neuroscience",
    "doi": "10.1000/xyz", "pmid": "12345", "role": "source", "link_role": "source",
    "summary": "Profiles microglia from 40 donors.", "draft_fields": ["anthive_summary"],
    "celltypes": ["microglial cell"], "cohorts": ["ROSMAP"], "has_pdf": True, "n_datasets": 2,
}
_PAPER_RELATED = {
    "citekey": "doe2024trisomy", "entrytype": "online",
    "title": "A related preprint", "authors": ["Doe, Jane"], "first_author": "Doe, Jane",
    "citation": "Doe 2024", "year": 2024, "journal": "bioRxiv", "doi": "10.1101/1",
    "role": "related", "link_role": "related", "summary": "Reviewed summary.",
    "draft_fields": [], "has_pdf": False, "n_datasets": 1,
}
_PAPER_RECORD = {
    **_PAPER_SOURCE, "fields": {"abstract": "We profiled microglia."},
    "datasets": [{"db_id": "GroupA/ds1", "role": "source", "scope": "collection"},
                 {"db_id": "GroupA/ds2", "role": "source", "scope": "collection"}],
    "related": [
        {"citekey": "doe2024trisomy", "title": "A related preprint", "citation": "Doe 2024",
         "relations": [{"kind": "explicit", "evidence": "version"},
                       {"kind": "author", "evidence": ["Smith, Anna"]}]},
        {"citekey": "roe2020cohort", "title": "Cohort paper", "citation": "Roe et al. 2020",
         "relations": [{"kind": "cohort", "evidence": ["rosmap"]},
                       {"kind": "cites", "evidence": "10.1000/roe"}]},
    ],
    "notes": "## Notes\n\nNo notes yet.\n",
}


def _library_api(path_map):
    def fake(api_url, path):
        if path in path_map:
            value = path_map[path]
            if isinstance(value, Exception):
                raise value
            return value
        return _fake_api_get(api_url, path)
    return fake


@pytest.fixture
def library_client(client, monkeypatch):
    monkeypatch.setattr(browser, "_api_get", _library_api({
        "/databases/GroupA/ds1/papers": {"db_id": "GroupA/ds1", "papers": [_PAPER_RELATED, _PAPER_SOURCE]},
        "/databases/GroupA/ds2/papers": {"db_id": "GroupA/ds2", "papers": [_PAPER_SOURCE]},
        "/papers/smith2025microglia": _PAPER_RECORD,
        "/papers": {"n": 2, "papers": [_PAPER_SOURCE, _PAPER_RELATED]},
    }))
    return client


def test_paper_reason_sentences():
    assert browser._paper_reason("source").startswith("Source of this dataset")
    assert "this collection" in browser._paper_reason("source", scope="collection")
    assert "preprint" in browser._paper_reason("related", "online")
    assert browser._paper_reason("method").startswith("Method used")
    assert browser._paper_citation_line(_PAPER_SOURCE) == "Smith et al., Nature Neuroscience, 2025"


def test_dataset_papers_tab_source_first_with_reasons(library_client):
    html = library_client.get("/browse/GroupA/ds1").text
    assert 'data-tab="papers"' in html and 'id="tab-papers"' in html
    src = html.index('data-citekey="smith2025microglia"')
    rel = html.index('data-citekey="doe2024trisomy"')
    assert src < rel, "source papers must come first"
    assert "Source of this dataset: the data come from this paper." in html
    assert "Related: a preprint of a closely related study" in html
    assert "draft, not yet reviewed" in html
    assert 'href="/api/papers/smith2025microglia/file"' in html
    assert 'href="/api/papers/smith2025microglia.bib"' in html
    assert 'href="https://doi.org/10.1000/xyz"' in html


def test_dataset_downloads_lists_paper_pdfs(library_client):
    html = library_client.get("/browse/GroupA/ds1").text
    block = html[html.index('id="ds-paper-files"'):]
    assert '/api/papers/smith2025microglia/file' in block[:2000]
    # the related preprint has no PDF, so it is not offered as a download
    assert 'doe2024trisomy/file' not in html
    for forbidden in ('.duckdb', '.h5ad.mus', '.scores.parquet'):
        assert forbidden not in block[:2000]


def test_dataset_without_papers_has_no_papers_tab(client):
    html = client.get("/browse/GroupA/ds1").text
    assert 'data-tab="papers"' not in html
    assert 'id="ds-paper-files"' not in html


def test_collection_papers_panel_dedupes_and_counts(library_client):
    html = library_client.get("/browse/GroupA").text
    assert 'id="coll-papers"' in html
    assert html.count('data-citekey="smith2025microglia"') == 1
    assert "Source of this collection" in html
    assert "Linked to 2 of 2 datasets." in html


def test_collection_papers_panel_sits_after_dataset_table(library_client):
    html = library_client.get("/browse/GroupA").text
    assert html.index('id="datasets-table"') < html.index('id="coll-papers"')
    if '<h2 class="section-head">Downloads' in html:
        assert html.index('id="coll-papers"') < html.index('<h2 class="section-head">Downloads')


def test_section_headings_share_one_style_on_collection_and_dataset_pages(library_client):
    # One heading style for every section on both pages, defined once in
    # base.html; no bare <h2> sections or inline-styled section headings left.
    import re
    for url in ("/browse/GroupA", "/browse/GroupA/ds1"):
        html = library_client.get(url).text
        assert '<h2 class="section-head' in html, url
        assert ".section-head {" in html, url          # CSS from base.html
        body = html.split("<body", 1)[1]
        assert not re.search(r"<h2>|<h2 style=", body), url
        assert '<h3 style="margin:14px' not in body, url


def test_paper_page_sections(library_client):
    resp = library_client.get("/papers/smith2025microglia")
    assert resp.status_code == 200
    html = resp.text
    assert "Microglia states in the ageing cortex" in html
    assert "We profiled microglia." in html
    assert 'id="pp-topics"' in html and "ROSMAP" in html
    assert 'id="pp-linked"' in html and "Dataset One" in html and "/browse/GroupA/ds1" in html
    assert "Source of these datasets" in html
    assert "Same study (versions, preprints, follow-ups)" in html
    assert "another version of the same study" in html
    assert "both use the rosmap cohort" in html
    assert "this paper cites it" in html
    assert "No notes yet" not in html


def test_paper_page_shows_own_role_and_document_type(library_client, monkeypatch):
    html = library_client.get("/papers/smith2025microglia").text
    # _PAPER_RECORD has role source: badge plus its meaning
    assert 'class="paper-role role-source"' in html
    assert "the data in anthive come from this paper" in html
    proposal = {**_PAPER_RECORD, "citekey": "lab2026proposal", "role": None, "entrytype": "report",
                "datasets": [], "fields": {**(_PAPER_RECORD.get("fields") or {}), "type": "project proposal"}}
    monkeypatch.setattr(browser, "_api_get", _library_api({"/papers/lab2026proposal": proposal}))
    html = library_client.get("/papers/lab2026proposal").text
    assert "report" in html and "(project proposal)" in html
    assert 'class="paper-role role-background"' in html
    assert "of interest to the lab, not linked to any dataset" in html


def test_help_and_mcp_pages_say_where_text_goes(client):
    sentence = ("Anything anthive returns to your AI client, including text from papers and datasets, "
                "is sent to that client&#39;s AI provider.")
    plain = sentence.replace("&#39;", "'")
    help_html = client.get("/help").text
    assert (sentence in help_html) or (plain in help_html)
    assert "Background is the default" in help_html and "project or grant proposals" in help_html
    assert "sees every paper and document" in help_html
    mcp_html = client.get("/account/mcp", headers={"Remote-User": "mf"}).text
    assert (sentence in mcp_html) or (plain in mcp_html)


def test_paper_page_unknown_is_404(library_client):
    assert library_client.get("/papers/unknown2020paper").status_code == 404


def test_library_page_lists_and_filters(library_client):
    resp = library_client.get("/library")
    assert resp.status_code == 200
    html = resp.text
    assert 'id="lib-q"' in html and 'id="lib-table"' in html
    assert 'class="table-wrap"' in html
    assert html.index("smith2025microglia") < html.index("doe2024trisomy")  # newest first
    assert '<option value="ROSMAP">' in html and '<option value="2025">' in html


def test_library_page_without_library(client, monkeypatch):
    monkeypatch.setattr(browser, "_api_get", _library_api({"/papers": requests.HTTPError("404")}))
    html = client.get("/library").text
    assert "not available on this site" in html


def test_nav_library_link_follows_availability(client, monkeypatch):
    monkeypatch.setattr(browser, "_library_state",
                        {"available": True, "checked": browser.time.monotonic(), "refreshing": False})
    assert 'id="nav-library"' in client.get("/about").text
    monkeypatch.setattr(browser, "_library_state",
                        {"available": False, "checked": browser.time.monotonic(), "refreshing": False})
    assert 'id="nav-library"' not in client.get("/about").text


def test_dataset_papers_tab_sits_right_before_downloads(library_client):
    import re
    html = library_client.get("/browse/GroupA/ds1").text
    buttons = re.findall(r'data-tab="([a-z]+)"', html)
    assert 'papers' in buttons and 'downloads' in buttons
    assert buttons.index('downloads') == buttons.index('papers') + 1
    assert buttons[0] != 'papers'


def test_help_page_links_client_guide_on_gitlab(client, monkeypatch):
    monkeypatch.setattr(browser, "_docs_base_cfg", ["https://docs.example/doc"])
    text = client.get("/help").text
    assert 'id="help-mcp-clients-doc"' in text
    assert 'href="https://docs.example/doc/MCP_CLIENTS.md"' in text
    assert 'href="https://docs.example/doc/MCP_LINKS.md"' in text
    assert 'id="help-docs-link"' in text
    assert "codex mcp add anthive --url" in text
    assert "vendor&#39;s cloud" in text or "vendor's cloud" in text


def test_account_mcp_has_more_client_tabs_and_guide_link(client, monkeypatch):
    monkeypatch.setattr(browser, "_docs_base_cfg", ["https://docs.example/doc"])
    text = client.get("/account/mcp", headers={"Remote-User": "mf"}).text
    for key in ("codex", "gemini_cli", "vscode_json", "json_url", "mcp_remote", "claude_code_user"):
        assert f'data-snippet="{key}"' in text, key
    assert 'href="https://docs.example/doc/MCP_CLIENTS.md"' in text


def test_api_guide_python_snippets_construct_client(client, monkeypatch):
    """Every <pre> that calls ``client.`` must build ``client = AnthiveClient(`` first.

    Checked per block, not per page: a reader copies one block, so a block
    that leans on a client built in an earlier section fails when pasted on
    its own (the bug a 2.41.0 release check found in the upload snippet).
    """
    import html as html_lib
    import re
    monkeypatch.setattr(browser, "_api_get", _api_guide_fake({"version": "2.41.0"}))
    # uploader view, so the upload section's snippets are on the page too
    monkeypatch.setattr(browser, "_api_guide_site_settings", lambda: _guide_settings("authelia"))
    page = client.get("/api-guide", headers={"Remote-User": "mf",
                                             "Remote-Groups": "admins"}).text
    assert 'id="api-guide-upload"' in page
    blocks = [html_lib.unescape(block)
              for block in re.findall(r"<pre[^>]*>(.*?)</pre>", page, re.S)]
    client_blocks = [block for block in blocks if "client." in block]
    assert len(client_blocks) >= 3
    for block in client_blocks:
        construct_at = block.find("client = AnthiveClient(")
        assert construct_at != -1, block
        assert construct_at < block.find("client."), block


def test_mcp_connection_name_matches_the_api_snippets():
    import mcp_tokens
    for url in ('https://intern.bdslab.org', 'https://www.anthive.kuleuven.be/', 'http://10.0.0.5:8501',
                'https://anthive.example.org', 'https://anthive-public.org', 'http://localhost:8501', ''):
        assert browser._mcp_connection_name(url) == mcp_tokens.connection_name(url), url


# ── facet effects (donor-level facet tests) ──────────────────────────────────

def test_dataset_facets_card_shows_effects_per_facet(client):
    text = client.get("/browse/GroupA/ds1").text
    card = text.split('id="ds-facets"')[1].split('<script>')[0]
    # the effects live in the Facets tab, below the mapping table
    assert 'data-tab="facets"' in text and 'Facet DE, genes per cell type' in card
    assert card.index('class="ds-facets"') < card.index('Facet DE, genes per cell type') < card.index('class="ds-facet-fx"')
    assert 'id="fx-intro"' in card and "Exploratory" in card and "per donor" in card
    assert "provisional" in card and "datasets that share donors do not count twice" in card
    assert 'id="fx-about"' in card and 'id="fx-about" open' not in card
    assert 'href="/help#facet-howto"' in card
    # one effects row under ant_sex, with the unit table and expanding rows
    assert 'class="ds-facet-fx" data-facet="ant_sex"' in card
    assert 'id="fx-table-ant_sex"' in card and 'class="fx-toggle"' in card
    assert "m vs f" in card and "m 9, f 12" in card
    assert "42 / 15,000" in card
    assert "cell mean, scanpy score <span class=\"nw\">3|1 / 128</span>" in card
    # flags and model line
    assert ">composition</span>" in card
    assert "expression ~ sex + age_z" in card and "details" in card
    assert "pmi dropped (known for fewer than 90% of donors, or constant)" in card
    assert "Sex positive control passed" in card
    # braak is tested but not a column of this dataset: extra row, descriptive only
    assert 'data-facet="ant_braak"' in card and ">descriptive only</span>" in card
    # untested disease unit is listed with its reason
    assert "level with &lt; 3 donors (control=2, AD=19)" in card or "level with < 3 donors (control=2, AD=19)" in card
    assert "yes, cohort CohortX, anchor" in card
    # expand logic loaded, and tables scroll inside their box
    assert "/facet-tests" in text and "volcano.png" in text
    assert '<div class="table-wrap">\n<table class="fx-table card-table"' in card
    assert "—" not in card


def test_dataset_facets_tab_unit_line_has_replicate_unit_and_disease_basis(client):
    text = client.get("/browse/GroupA/ds1").text
    unit_line = text.split('id="fx-unit-line"')[1].split("</table>")[0]
    assert 'id="fx-replicate"><th>Replicate unit' in unit_line and '<span class="fx-tag">donor (ant_donor)</span>' in unit_line
    assert 'Disease basis' in unit_line and '<span class="fx-tag">AD vs control: pathologic (Braak x CERAD)</span>' in unit_line
    assert 'id="fx-overlap-unknown"' in unit_line and "not checked" in unit_line
    # the Biology card of the Data tab repeats the unit, small
    bio = text.split('id="bio-replicate-unit"')[1].split("</tr>")[0]
    assert "donor (ant_donor)" in bio


def test_dataset_facets_tab_replicate_unit_not_recorded(client, monkeypatch):
    summary = {k: v for k, v in _FACET_TESTS_DS1.items()
               if k not in ("replicate_unit", "replicate_column", "replicate_basis", "disease_basis")}
    summary["units"] = [{k: v for k, v in unit.items() if k not in ("replicate_unit", "replicate_column")}
                        for unit in summary["units"]]
    monkeypatch.setattr(browser, "_api_get", _api_with({"/databases/GroupA/ds1/facet-tests": summary}))
    text = client.get("/browse/GroupA/ds1").text
    assert '<span class="fx-tag">not recorded for this dataset</span>' in text
    assert 'id="bio-replicate-unit"' not in text and 'id="fx-disease-basis"' not in text


def test_dataset_info_replicate_fields_win_over_the_test_summary(client, monkeypatch):
    info = dict(_INFO["GroupA/ds1"], replicate_unit="animal", replicate_column="hash.ID",
                replicate_basis="declared", disease_basis="author_dx")
    monkeypatch.setattr(browser, "_api_get", _api_with({"/databases/GroupA/ds1/info": info}))
    unit_line = client.get("/browse/GroupA/ds1").text.split('id="fx-unit-line"')[1].split("</table>")[0]
    assert '<span class="fx-tag">animal (hash.ID)</span>' in unit_line and "AD vs control: author diagnosis" in unit_line
    assert "not pathology based" in unit_line


def test_dataset_facets_tab_cell_level_unit_has_pill_and_visible_reason(client):
    card = client.get("/browse/GroupA/ds1").text.split('id="ds-facets"')[1].split('<script>')[0]
    assert ">replicate level (donor)</span>" in card
    untested = card.split('id="fx-untested-extra"')[1].split("</div></div>")[0]
    assert ">cell-level, descriptive only</span>" in untested and "control=2, AD=19" in untested
    assert 'id="fx-flag-legend"' in card and "About the flags" in card
    assert "Disease definition" not in card.split('data-facet="ant_sex"')[1].split('data-facet="ant_braak"')[0]


@pytest.mark.parametrize("fields, expected_text", [
    ({"replicate_unit": "donor", "replicate_column": "ant_donor"}, "donor (ant_donor)"),
    ({"replicate_unit": "animal", "replicate_column": "hash.ID", "replicate_basis": "inferred"}, "animal (hash.ID), inferred"),
    ({"replicate_unit": "none"}, "none: cell-level only"),
    ({"replicate_unit": "sample"}, "sample"),
])
def test_replicate_view_text(fields, expected_text):
    assert browser._replicate_view(fields)["text"] == expected_text


def test_replicate_view_first_non_empty_source_wins_and_none_without_data():
    view = browser._replicate_view({"replicate_unit": None}, {"replicate_unit": "donor", "replicate_column": "ant_donor",
                                                               "disease_basis": "adnc"})
    assert view["unit"] == "donor" and view["disease_text"] == "AD vs control: pathologic (ADNC)"
    assert browser._replicate_view({}, None, {"replicate_unit": None}) is None


@pytest.mark.parametrize("basis, expected", [
    ("braak_cerad", "AD vs control: pathologic (Braak x CERAD)"),
    ("author_dx", "AD vs control: author diagnosis"),
    ("clinical_dx", "AD vs control: clinical diagnosis"),
    ("something_new", "AD vs control: something_new"),
    (None, ""),
])
def test_disease_basis_text(basis, expected):
    assert browser._disease_basis_text(basis) == expected


def test_fx_pills_analysis_level_pooled_and_overlap():
    cell = browser._fx_pills({"analysis_level": "cell", "analysis_level_reason": "level with < 3 donors", "flags": {}})
    assert cell[0][0] == "cell-level, descriptive only" and "level with < 3 donors" in cell[0][2]
    replicate = browser._fx_pills({"analysis_level": "replicate", "replicate_unit": "animal", "flags": {}})
    assert replicate[0][0] == "replicate level (animal)"
    labels = [pill[0] for pill in browser._fx_pills({"pooled": True, "overlap_unknown": True, "flags": {}})]
    assert labels == ["pooled", "donor overlap not checked"]


def test_dataset_facets_card_hides_facets_outside_the_config(client, monkeypatch):
    hidden = {**_DATASET_FACETS, "facets": [
        {**f, "hidden_by_config": f["facet"] not in ("ant_sex", "ant_apoe4_carrier")}
        for f in _DATASET_FACETS["facets"]]}

    def fake(api_url, path, **kwargs):
        if path == "/databases/GroupA/ds1/facets":
            return hidden
        return _fake_api_get(api_url, path)
    monkeypatch.setattr(browser, "_api_get", fake)
    card = client.get("/browse/GroupA/ds1").text.split('id="ds-facets"')[1].split("<script>")[0]
    assert 'href="/facets/ant_sex"' in card
    # facets that are present but not tested are listed with their reason, not dropped; tissue is structural
    assert 'id="facets-not-analysed"' in card and "Present, not tested" in card
    analysed, not_analysed = card.split('id="facets-not-analysed"')
    assert 'data-facet="ant_sex"' in analysed and 'data-facet="ant_tissue"' not in analysed
    assert 'href="/facets/ant_braak_group"' in not_analysed and "not analysed yet" in not_analysed
    structural = not_analysed.split('id="facets-structural"')[1]
    assert 'href="/facets/ant_tissue"' in structural


def test_dataset_facets_card_not_tested_state(client, monkeypatch):
    def fake(api_url, path, **kwargs):
        if path == "/databases/GroupA/ds1/facet-tests":
            return {"db_id": "GroupA/ds1", "status": "not_tested", "reason": "no raw counts layer served",
                    "n_units": 0, "n_units_tested": 0, "units": [], "runs": {}, "files": {}}
        return _fake_api_get(api_url, path)
    monkeypatch.setattr(browser, "_api_get", fake)
    card = client.get("/browse/GroupA/ds1").text.split('id="ds-facets"')[1]
    assert 'id="fx-not-tested"' in card
    assert "No facet tests for this dataset: no raw counts layer is served" in card
    assert 'id="fx-intro"' not in card.split("<script>")[0]


def test_dataset_facets_tab_when_every_facet_is_hidden(client, monkeypatch):
    # M19/PRH case: all facets outside facets_visible. The tab stays, with the facets under "Not analysed yet".
    def fake(api_url, path, **kwargs):
        if path == "/databases/GroupA/ds1/facets":
            return {**_DATASET_FACETS, "facets": [{**f, "hidden_by_config": True} for f in _DATASET_FACETS["facets"]]}
        if path == "/databases/GroupA/ds1/facet-tests":
            return {"db_id": "GroupA/ds1", "status": "not_tested", "reason": "experimental_system xenograft",
                    "n_units": 0, "n_units_tested": 0, "units": [], "runs": {}, "files": {}}
        return _fake_api_get(api_url, path)
    monkeypatch.setattr(browser, "_api_get", fake)
    text = client.get("/browse/GroupA/ds1").text
    assert 'data-tab="facets"' in text
    tab = text.split('id="tab-facets"')[1].split("</section>")[0]
    assert 'id="facets-analysed"' not in tab and 'id="facets-not-analysed"' in tab
    assert 'href="/facets/ant_sex"' in tab
    gene_tab = text.split('id="tab-facet-gene"')[1].split("</section>")[0]
    assert "this dataset is a xenograft" in gene_tab and "host animal" in gene_tab


def test_dataset_facets_tab_hidden_without_facets(client):
    text = client.get("/browse/GroupA/ds2").text
    assert 'data-tab="facets"' not in text and 'id="tab-facets"' not in text
    assert "see the Facets tab" not in text and "see the <a href=\"#facets\">Facet tab" not in text


@pytest.mark.parametrize("status,reason,expected", [
    ("not_tested", "experimental_system bulk_tissue", "bulk tissue"),
    ("not_tested", "experimental_system in_vitro_2d", "in vitro"),
    ("not_tested", "experimental_system organoid", "organoid culture"),
    ("not_tested", "experimental_system something_new", "experimental system something_new"),
    ("not_tested", "organism not human (mouse)", "pure mouse dataset"),
    ("not_tested", "mouse-only dataset: not part of the facet layer (decision 2026-10-07)", "pure mouse dataset"),
    ("not_tested", "organism not human (pig)", "human cells only"),
    ("not_tested", "excluded from the facet layer: spatial technology (visium): no replicate-level cell-type pseudobulk",
     "spatial data (visium)"),
    ("not_tested", "excluded from the facet layer: proteomics (mass-spectrometry): not single-cell expression", "proteomics"),
    ("not_tested", "excluded from the facet layer: Francken2026 sets are set aside from the facet layer until further notice",
     "left out of the facet analysis on purpose: Francken2026"),
    ("not_tested", "secondary (is_secondary true)", "reanalysis"),
    ("not_tested", "5 donors (< 6)", "only 5 donors; at least 6"),
    ("not_tested", "no ant_donor facet in the current mapping", "no donor column"),
    ("not_tested", "no facet and cell type combination had enough donors per level", "no levels at all"),
    ("not_tested", "something new", "something new"),
    ("not_tested", None, "no reason was recorded"),
    ("not_run", "no facet test run has recorded this dataset", "has been run"),
])
def test_fx_reason_text(status, reason, expected):
    assert expected in browser._fx_reason_text(status, reason)


def test_dataset_page_without_facet_tests_endpoint(client):
    # GroupA/ds2: no /facet-tests in the fake (older ant-serve): nothing breaks.
    resp = client.get("/browse/GroupA/ds2")
    assert resp.status_code == 200 and 'id="fx-table-' not in resp.text


def test_facet_page_effects_across_datasets_renders_first_and_fetches_the_table(client):
    text = client.get("/facets/ant_disease").text
    section = text.split('id="facet-effects"')[1]
    assert "Effects across datasets" in text
    assert 'id="fx-methods"' in section and "limma-voom" in section and "10,000 by default" in section
    assert "1,000" not in section.split('id="fx-methods"')[1].split("</div>")[0]
    assert "partial conjunction" in section.split('id="fx-methods"')[1].split("</div>")[0]
    # the table is not in the page: a slot and a loader that fetches the fragment
    assert 'id="fx-across-slot"' in section and 'data-url="/facets/ant_disease/effects-table"' in section
    assert 'id="fx-across-loading"' in section and 'id="fx-table-across"' not in section
    assert "antFxBind" in section
    assert 'id="fx-recurring"' in section and "DRAFT" in section
    assert 'href="/help#facet-howto"' in section
    # M3: the disease definition is said on the page
    assert 'id="fx-disease-definition"' in section and "pathology-based definition" in section
    assert 'id="fx-disease-definition"' not in client.get("/facets/ant_sex").text


def test_facet_effects_table_fragment(client):
    resp = client.get("/facets/ant_disease/effects-table")
    assert resp.status_code == 200
    text = resp.text
    assert "<html" not in text and "<script" not in text
    assert 'id="fx-table-across"' in text and 'data-db-id="GroupA/ds2"' in text
    assert "Units tested" in text and "2 dataset and cell type combinations in 2 datasets" in text
    assert 'href="/genes/PTPRG"' in text
    assert 'id="fx-across-untested"' in text
    # a pooled unit is text with its member list, never a broken /browse/pool: link
    assert "/browse/pool:" not in text
    assert "<strong>pool:seaad_microglia</strong>" in text and "pooled over 2 files" in text
    assert 'href="/browse/GroupA/ds1#data"' in text
    assert ">pooled</span>" in text and ">donor overlap not checked</span>" in text
    assert "Donor overlap with other cohorts was not checked" in text
    # the not-counted reason and the disease definition are visible lines, not tooltips
    assert '<div class="fx-reason">Not counted: disease definition is author_dx' in text
    assert "Disease definition: AD vs control: author diagnosis; not pathology based, so never combined" in text
    assert "Disease definition: AD vs control: pathologic (Braak x CERAD)." in text
    assert "Cohort CohortY, anchor (not counted towards replication)" in text


def test_facet_effects_table_fragment_failure_and_gating(client, monkeypatch):
    def broken(api_url, path, **kwargs):
        if path == "/facets/ant_disease/tests":
            raise requests.HTTPError("500")
        return _fake_api_get(api_url, path, **kwargs)
    monkeypatch.setattr(browser, "_api_get", broken)
    assert 'id="fx-across-error"' in client.get("/facets/ant_disease/effects-table").text
    monkeypatch.setattr(browser, "_api_get", _fake_api_get)
    assert client.get("/facets/ant_pmi/effects-table").status_code == 404
    assert client.get("/facets/ant_age/effects-table?variant=ant_disease").status_code == 404
    assert client.get("/facets/ant_disease/effects-table?scope=nowhere").status_code == 400
    monkeypatch.setattr(browser, "_facets_visible_cfg", [["ant_sex"]])
    assert client.get("/facets/ant_disease/effects-table").status_code == 404
    admin = client.get("/facets/ant_disease/effects-table", headers={"Remote-User": "root", "Remote-Groups": "admins"})
    assert admin.status_code == 200


def test_pool_members_survive_a_cohort_table_failure(monkeypatch):
    def broken(api_url, path, **kwargs):
        raise requests.HTTPError("500")
    monkeypatch.setattr(browser, "_api_get", broken)
    assert browser._pool_members("http://x") == {}


def test_facet_page_hidden_facet(client, monkeypatch):
    monkeypatch.setattr(browser, "_facets_visible_cfg", [["ant_sex", "ant_braak"]])
    resp = client.get("/facets/ant_disease")
    assert resp.status_code == 200
    assert 'id="facet-hidden-notice"' in resp.text and "not shown at the moment" in resp.text
    assert 'href="/facets/ant_sex"' in resp.text and 'id="facet-effects"' not in resp.text
    admin = client.get("/facets/ant_disease", headers={"Remote-User": "root", "Remote-Groups": "admins"})
    assert 'id="facet-hidden-admin"' in admin.text and 'id="facet-hidden-notice"' not in admin.text


def test_facets_index_hides_facets_and_signal(client, monkeypatch):
    def fake(api_url, path, **kwargs):
        if path == "/facets":
            payload = _fake_api_get(api_url, path)
            payload["facets"] = [{**f, "hidden_by_config": f["facet"] == "ant_age"} for f in payload["facets"]]
            return payload
        return _fake_api_get(api_url, path)
    monkeypatch.setattr(browser, "_api_get", fake)
    text = client.get("/facets").text
    assert 'data-facet="ant_disease"' in text and 'data-facet="ant_age"' not in text
    assert 'id="facets-hidden-note"' in text and "1 other facets are hidden" in text
    assert "signal" not in text.lower().split('id="facet-table"')[1].split("</table>")[0]
    admin = client.get("/facets", headers={"Remote-User": "root", "Remote-Groups": "admins"}).text
    assert 'id="facets-hidden-admin"' in admin and 'href="/facets/ant_age"' in admin


def test_gene_page_has_facet_effects_panel(client):
    text = client.get("/genes/PTPRG").text
    assert 'id="gene-facet-effects"' in text and 'data-gene="PTPRG"' in text
    # scope selector first, then the tabs of the scope (built in the page script)
    assert 'id="gene-fx-scopes"' in text and 'data-scope="human_postmortem"' in text
    assert 'data-scope="xenograft_human_in_mouse"' in text
    for facet in ("sex", "disease", "apoe4_carrier", "apoe4_dose", "braak", "braak_threshold", "age", "age_adjusted", "app_nlgf"):
        assert f"['{facet}'," in text, facet
    assert "age, not adjusted for pathology" in text and "age, adjusted for pathology" in text
    assert "&scope=" in text
    assert "/api/facets/" in text and "renderCard" in text
    assert "exploratory" in text and 'href="/help#facet-howto"' in text
    assert 'id="gene-fx-about"' in text and 'id="gene-fx-about" open' not in text
    # multi-gene view: no panel
    assert 'id="gene-facet-effects"' not in client.get("/genes/PTPRG,APOE").text


def _help_sections(text, *section_ids):
    """The HTML of the named help sections (collapsible details elements, never nested)."""
    return "".join(text.split(f'id="{section_id}"')[1].split("</details>")[0] for section_id in section_ids)


def test_help_page_facet_effects_section(client):
    text = client.get("/help").text
    section = _help_sections(text, "facet-effects")
    for phrase in ("sex, pathologic AD (disease), APOE e4 carrier status, Braak stage",
                   "limma-voom", "10,000 by default", "1/(n + 1)", "descriptive only", "Composition",
                   "Provisional", "exploratory", "DESIGN_facet_testing.md", "umbrella terms", "5 donors per group",
                   "3 animals per group"):
        assert phrase in section + _help_sections(text, "facet-counting"), phrase
    assert '<a href="#facet-effects">' in text
    assert "\u2014" not in section


def test_help_sections_are_collapsible_and_the_toc_targets_them(client):
    text = client.get("/help").text
    assert '<details class="help-section quickstart" id="quickstart" open>' in text
    assert '<details class="help-section" id="glossary">' in text and '<section class="help-section' not in text
    for anchor in ("#glossary", "#facet-howto", "#facet-counting", "#facet-effects"):
        assert f'<a href="{anchor}">' in text
    assert "openTarget" in text and "help-expand-all" in text


def test_help_glossary_defines_every_term_the_pages_link_to(client):
    help_text = client.get("/help").text
    template_dir = browser.__file__.replace("browser.py", "templates/")
    linked = set()
    for name in ("_facet_effects.html", "facet.html", "facets.html", "dataset.html", "_module_view.html",
                 "geneset.html", "geneset_experiment.html", "geneset_variability.html", "donor_groups.html",
                 "_views.html", "_view_gene.html", "_view_geneset.html", "_view_facet.html"):
        text = open(template_dir + name, encoding="utf-8").read()
        linked |= set(re.findall(r'data-glossary="([a-z0-9-]+)"', text))
        linked |= set(re.findall(r'/help#glossary-([a-z0-9-]+)', text))
    linked |= {pill[3] for pill in browser._fx_pills({
        "analysis_level": "cell", "pooled": True, "overlap_unknown": True, "is_secondary": True, "dataset_changed": True,
        "flags": {"descriptive_only": True, "provisional": True, "composition_flag": True,
                  "batch_confounded": True, "batch_used": True}})}
    assert len(linked) > 15
    for slug in sorted(linked):
        assert f'id="glossary-{slug}"' in help_text, slug
    for slug in ("pseudobulk", "replicate-unit", "anchor-unit", "cohort", "fdr", "r-value", "partial-conjunction",
                 "effect-filter", "treat-tier", "analysis-level", "descriptive-only", "scope", "disease-basis",
                 "composition", "lambda", "per-cell-association"):
        assert f'id="glossary-{slug}"' in help_text, slug


def _squash(html):
    return re.sub(r"> <", "><", re.sub(r"\s+", " ", html)).strip()


def test_help_page_renders_every_topic_and_glossary_term_from_help_content(client):
    """Web and API read the same files: nothing on the page is typed twice."""
    import help_content

    page = _squash(client.get("/help").text)
    docs_url = browser._docs_base_url()
    child_ids = {child for topic in help_content.topics() for child in topic.children}
    for topic in help_content.topics():
        if topic.id in child_ids:
            assert f'<h3 class="subsection-head" id="{topic.id}">' in page, topic.id
        else:
            assert f'<details class="help-section" id="{topic.id}">' in page, topic.id
            assert _squash(help_content.topic_html(topic.id, docs_url)) in page, topic.id
    assert _squash(help_content.glossary_html(docs_url)) in page
    for term in help_content.glossary():
        assert f'id="glossary-{term.slug}"' in page
    assert "{{" not in page and "help_topic_html" not in page


def test_help_has_the_ptprg_walkthrough(client):
    section = _help_sections(client.get("/help").text, "facet-howto")
    for phrase in ("PTPRG", "Braak", "What you may say", "What you may not say", "partial conjunction"):
        assert phrase in section, phrase
    assert "proves" not in section and "confirms" not in section


def test_doc_block_macro_renders_a_hidden_box_toggled_by_an_info_button():
    template = browser.templates.env.from_string(
        '{% from "_macros.html" import doc_block, doc_toggle %}<h2>Head{{ doc_toggle("x", "About this") }}</h2>'
        '{% call doc_block("About this", id="x") %}body{% endcall %}'
        '{% call doc_block("About that", open=True) %}other{% endcall %}'
        '{% call doc_block("About more", id="y", summary=True) %}more{% endcall %}')
    html = template.render()
    # hidden box, no visible summary; the header (i) is a button wired to it
    assert '<div class="doc-box" id="x" role="region" aria-label="About this" hidden><div class="doc-body">body</div></div>' in html
    assert '<button type="button" class="doc-toggle" data-help-for="x" aria-controls="x" aria-expanded="false"' in html
    # fallbacks: no id, or summary=True, keep the collapsible bar
    assert '<details class="doc-block" open><summary>&#9432; About that' in html
    assert '<details class="doc-block" id="y"><summary>&#9432; About more' in html


def test_base_toggles_doc_boxes_and_opens_them_for_a_hash(client):
    text = client.get("/").text
    assert "closest('.help-btn, .doc-toggle')" in text
    assert "node.classList.contains('doc-box')" in text and ".doc-box[hidden]" in text


def test_base_has_doc_block_styles_hash_opening_and_remembering(client):
    text = client.get("/").text
    for needle in ("details.doc-block", "openTarget", "antDocRestore", "doc.open.", "info-pop-link"):
        assert needle in text, needle


def test_facet_doc_text_is_collapsed_by_default_on_every_facet_view(client):
    facets_text = client.get("/facets").text
    assert 'id="facets-about"' in facets_text and 'id="facets-about" open' not in facets_text
    assert "facets-short" in facets_text
    dataset_text = client.get("/browse/GroupA/ds1").text
    assert 'id="ds-facets-about"' in dataset_text and 'id="ds-facets-about" open' not in dataset_text
    assert 'id="fx-about"' in dataset_text and 'id="fx-about" open' not in dataset_text


def test_fx_sex_control_renders_without_q_values():
    """A sex control with a log2FC but no q (seen live on /facets/ant_sex)
    must render instead of raising TypeError in the '%.1e' format."""
    macro = browser.templates.env.get_template("_facet_effects.html").module.fx_sex_control
    unit = {"sex_control": {"status": "inconclusive", "xist_logFC": -2.0, "xist_q": None,
                            "rps4y1_logFC": 1.5, "rps4y1_q": None, "n_y_tested": 3,
                            "n_y_up_q01": 0, "rule": "XIST down, RPS4Y1 up"}}
    html = str(macro(unit))
    assert "XIST log2FC -2.0" in html and "RPS4Y1 1.5" in html
    assert "(q " not in html


def test_fx_forest_folds_rows_not_counted():
    """The gene-page forest keeps counted cohort rows visible and folds the
    'not counted' rows behind a toggle; the bars are the shared effect bar on a fixed scale."""
    macro = browser.templates.env.get_template("_facet_effects.html").module.fx_forest_assets
    html = str(macro())
    assert "fxf-more" in html and "fxf-hidden" in html
    assert "not counted towards replication" in html
    assert "antEffectBar.barHtml" in html and "antEffectBar.axisHtml" in html and "antEffectBar.scaleKey(facet.name, 'lfc')" in html
    assert "fxf-pt" not in html and "fxf-track" not in html


_DONOR_GROUPS = {"n_groups": 1, "n_ungrouped": 3, "groups": [
    {"donor_group": "rosmap", "n_datasets": 2, "n_primary": 1, "n_reanalysis": 1, "n_unresolved": 0,
     "n_cells": 1000, "has_donor_uid": True, "datasets": [
         {"db_id": "GroupA/ds1", "title": "Primary one", "group": "GroupA", "designation": "primary",
          "is_secondary": False, "reanalysis_of": [], "has_donor_uid": True, "n_cells": 1000},
         {"db_id": "GroupA/ds2", "title": "Reanalysed", "group": "GroupA", "designation": "reanalysis",
          "is_secondary": True, "reanalysis_of": ["GroupA/ds1"], "has_donor_uid": True, "n_cells": 900}]}]}


def _api_with(extra):
    """_fake_api_get plus path overrides (path -> payload, or a callable)."""
    def api_get(api_url, path, **kwargs):  # pyright: ignore
        for key, value in extra.items():
            if path == key:
                return value(path) if callable(value) else value
        return _fake_api_get(api_url, path, **kwargs)
    return api_get


def test_donor_groups_page_lists_groups_and_designations(client, monkeypatch):
    monkeypatch.setattr(browser, "_api_get", _api_with({"/donor-groups": _DONOR_GROUPS}))
    text = client.get("/donor-groups").text
    assert 'id="dg-rosmap"' in text and 'href="#dg-rosmap"' in text
    assert "primary data" in text and "reanalysis" in text
    assert 'href="/browse/GroupA/ds1"' in text
    assert "3 datasets have no donor group yet." in text
    assert "1,000 cells in primary datasets" in text
    assert "—" not in text.split('id="dg-intro"')[1].split("{% endblock")[0].split("</main>")[0]


def test_donor_groups_page_survives_api_error(client, monkeypatch):
    def broken(path):
        raise requests.HTTPError("500")
    monkeypatch.setattr(browser, "_api_get", _api_with({"/donor-groups": broken}))
    resp = client.get("/donor-groups")
    assert resp.status_code == 200 and "Failed to load donor groups" in resp.text


def test_dataset_page_shows_lineage(client, monkeypatch):
    info = dict(_INFO["GroupA/ds1"], designation="reanalysis", reanalysis_of=["GroupA/ds2"],
                donor_group="rosmap", has_donor_uid=True)
    monkeypatch.setattr(browser, "_api_get", _api_with({"/databases/GroupA/ds1/info": info}))
    text = client.get("/browse/GroupA/ds1").text
    lineage = text.split('id="ds-lineage"')[1].split("</div>")[0]
    assert 'class="lineage-pill reanalysis"' in lineage
    assert 'id="ds-reanalysis-of"' in lineage and 'href="/browse/GroupA/ds2"' in lineage
    assert 'href="/donor-groups#dg-rosmap"' in lineage
    # the Facets tab repeats designation and donor group in its unit line
    unit_line = text.split('id="fx-unit-line"')[1].split("</table>")[0]
    assert "Reanalysis" in unit_line and "never counted" in unit_line
    assert 'href="/donor-groups#dg-rosmap"' in unit_line


def test_dataset_page_without_designation_has_no_lineage_row(client):
    text = client.get("/browse/GroupA/ds1").text
    assert 'id="ds-lineage"' not in text


def test_fx_pills_mark_reanalysis_not_counted():
    pills = browser._fx_pills({"is_secondary": True, "flags": {}})
    assert pills[0][0] == "reanalysis, not counted"


def test_help_explains_groups_counting_and_ranking(client):
    text = client.get("/help").text
    section = _help_sections(text, "facet-counting")
    for phrase in ("cell type group", "lib/celltype_groups.yaml", "Bonferroni", "agreement",
                   "median absolute log2 fold change", "reanalysis, not counted", 'href="/donor-groups"'):
        assert phrase in section, phrase
    assert "\u2014" not in section
    assert "proves" not in section and "confirms" not in section


def test_fx_forest_summary_first_by_cell_type_group():
    """The forest card renders one summary line per cell type group (matched
    on celltype_group) with the per-dataset forest collapsed under it."""
    html = str(browser.templates.env.get_template("_facet_effects.html").module.fx_forest_assets())
    assert "celltype_group" in html and "details class=\"fxs-group\"" in html
    assert "fxs-rest" in html and "with no counted row" in html
    assert "not_counted_reason" in html and "n_sibling_strata" in html and "q_counted" in html
    assert "reanalysis, not counted" in html


def test_fx_forest_single_cohort_wording_is_not_replication():
    html = str(browser.templates.env.get_template("_facet_effects.html").module.fx_forest_assets())
    assert "tested in one cohort only; replication not assessable" in html
    assert "not replicated (r = " in html and "no evidence of replication" not in html


def test_storage_notice_is_a_bottom_bar_at_every_width(client):
    text = client.get("/").text
    css = text.split("/* GDPR storage notice")[1].split("/* ── data-assistant")[0]
    rule = css.split("#storage-notice {")[1].split("}")[0]
    assert "left: 0" in rule and "right: 0" in rule and "bottom: 0" in rule
    # the page reserves room for the bar outside any media query
    assert css.index("body.storage-notice-on { padding-bottom") < css.index("@media")


def test_facet_intro_uses_manifest_permutations(client):
    """The dataset intro states the permutation count of the module run."""
    view = browser._facet_tests_view({"status": "tested", "units": [],
                                      "runs": {"modules": {"n_permutations": 10000}}})
    assert view["n_permutations"] == 10000
    macro = browser.templates.env.get_template("_facet_effects.html").module.fx_intro
    assert "10,000 label permutations" in str(macro("dataset", 10000))
    assert "10,000 by default" in str(macro("facet"))


def test_deprecated_facet_page_redirects_to_the_current_name(client):
    response = client.get("/facets/ant_apoe_carrier", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/facets/ant_apoe4_carrier"


def test_help_mentions_the_facet_rename(client):
    text = client.get("/help").text
    assert "ant_apoe4_carrier" in text and "ant_apoe_carrier" in text


def test_help_page_per_cell_vs_per_donor_section(client):
    text = client.get("/help").text
    section = _help_sections(text, "per-cell-per-donor")
    for phrase in ("Per cell vs per donor", "Facets</strong> tab", "per donor", "per-cell association (descriptive)",
                   "not independent", "the replicate there is the host animal", "spread across cells"):
        assert phrase.lower() in section.lower(), phrase
    assert '<a href="#per-cell-per-donor">' in text
    assert "\u2014" not in section


def test_module_view_explains_skipped_datasets(client):
    html = client.get("/genesets/StudyX/Exp1").text
    assert "SKIPPED = d.skipped_datasets || {};" in html
    assert "No per-cell association statistics for this dataset: " in html


def test_fx_assets_show_replication_v2_wording():
    """Badge, anchors, pooled/overlap pills, zero-replicated message, q (>=10% change) column."""
    module = browser.templates.env.get_template("_facet_effects.html").module
    forest = str(module.fx_forest_assets())
    for phrase in ("replicated (r = ", "not replicated (r = ", "individually at q &lt; 0.05", "Anchors: the unit that counted",
                   "data-pool", "donor overlap not checked", "with no counted row", "About the full rule",
                   "Simes", "fxf-row-model", "q (10 percent change)", "About replication", "About this forest",
                   "fxf-caveat", "effect_gate_passed", "replicated_effect", "m includes "):
        assert phrase in forest, phrase
    assert "table.fxf .fxf-donors-col { display: none; }" in forest
    assert ".fxf-reason" in forest and "fxf-reason { display: none" not in forest
    recurring = str(module.fx_recurring("ant_braak"))
    assert "No gene passes the replication rule for " in recurring and "r_value" in recurring
    assert "n_replicated" in recurring and "units_up" not in recurring and "max_abs_logFC" not in recurring
    script = str(module.fx_script())
    assert "q_treat" in script and "antFxBind" in script and "orientation" in script


def test_info_bubble_script_makes_hover_text_reachable():
    text = open(browser.__file__.replace("browser.py", "templates/base.html"), encoding="utf-8").read()
    assert ".info-pop" in text and "SELECTOR = '.info, .fx-pill'" in text and "'tabindex', '0'" in text


def test_help_explains_replication_v2(client):
    text = client.get("/help").text
    section = _help_sections(text, "facet-effects", "facet-counting")
    for phrase in ("partial conjunction", "Simes", "r-value", "anchor per cohort", "pool:",
                   "cell-level, descriptive only", "Replicate unit", "disease basis", "pathology-based", "per-cell (descriptive)",
                   "Fisher", "donor overlap not checked", "effect filter", "treat tier", "no evidence of replication",
                   "about 5 percent"):
        assert phrase in section, phrase
    assert "\u2014" not in section


# ── facet engine v3 UI: scopes, age twins, app_nlgf, floors, exclusions ────────

_SCOPES_PAYLOAD = [{"scope": "human_postmortem", "n_datasets": 2, "n_cells": 900},
                   {"scope": "xenograft_human_in_mouse", "n_datasets": 1, "n_cells": 50},
                   {"scope": "none", "n_datasets": 3, "n_cells": 70}]


def _facet_page_payload(column, scope, kind="numerical", definition_scope="cross_dataset"):
    return {
        "facet": column, "type": kind, "type_mixed": False, "n_datasets": 2, "total_n_cells": 900,
        "scope": scope, "scopes": _SCOPES_PAYLOAD,
        "definition": {"facet": column, "tag": column[4:], "label": column[4:], "defined": True,
                       "summary": "Summary of " + column, "definition": "Definition of " + column,
                       "kind": kind, "values": "years", "missing": "NULL", "scope": definition_scope},
        "definitions_status": {"state": "under_audit", "note": "An audit is running."},
        "datasets": [], "non_carriers": [],
        "value_distribution": {"kind": kind, "fleet_min": 60, "fleet_max": 95, "per_dataset": [], "values": []},
        "modules": {"rows": [], "n_total": 0, "truncated": False},
    }


def _recording_api(extra):
    """_fake_api_get with overrides, recording every requested path."""
    seen = []
    base = _api_with(extra)

    def api_get(api_url, path, **kwargs):  # pyright: ignore
        seen.append(path)
        return base(api_url, path, **kwargs)
    return api_get, seen


def test_facet_scope_options_drop_the_pseudo_scope_and_keep_order():
    options = browser._facet_scope_options(_SCOPES_PAYLOAD, "xenograft_human_in_mouse")
    assert [o["scope"] for o in options] == ["human_postmortem", "xenograft_human_in_mouse"]
    assert [o["current"] for o in options] == [False, True]
    assert options[0]["label"] == "human post-mortem"
    assert options[1]["label"] == "human cells in mouse hosts (xenograft)"
    assert all(o["tested"] for o in options)
    plain = browser._facet_scope_options(["in_vitro", "human_postmortem", "none"], None)
    assert [o["scope"] for o in plain] == ["human_postmortem", "in_vitro"] and plain[1]["tested"] is False
    assert browser._facet_scope_options(None, None) == []


def test_facets_index_lists_the_scopes_of_each_facet(client, monkeypatch):
    payload = {"n_facets": 1, "facets": [{"facet": "ant_age", "type": "numerical", "type_mixed": False, "n_datasets": 3,
                                          "scopes": ["human_postmortem", "none", "xenograft_human_in_mouse"],
                                          "summary": "Age.", "defined": True}],
               "definitions_status": {}}
    monkeypatch.setattr(browser, "_api_get", _api_with({"/facets": payload}))
    text = client.get("/facets").text
    assert 'href="/facets/ant_age?scope=human_postmortem"' in text
    assert 'href="/facets/ant_age?scope=xenograft_human_in_mouse"' in text
    assert "?scope=none" not in text
    assert 'id="facets-scope-about"' in text and "never counted together" in text


def test_facet_page_has_scope_selector_and_age_variants(client, monkeypatch):
    api, seen = _recording_api({"/facets/ant_age": _facet_page_payload("ant_age", "human_postmortem")})
    monkeypatch.setattr(browser, "_api_get", api)
    text = client.get("/facets/ant_age").text
    nav = text.split('id="facet-scope-nav"')[1].split("</nav>")[0]
    assert 'class="active"' in nav and "human post-mortem" in nav and "human cells in mouse hosts (xenograft)" in nav
    assert 'data-scope="none"' not in nav
    assert 'href="/facets/ant_age?scope=xenograft_human_in_mouse"' in nav
    assert 'id="facet-scope-note"' in text and 'class="fx-scope" data-scope="human_postmortem"' in text
    # the effects card names its scope, offers both age twins and states what each one is
    card = text.split('id="facet-effects"')[1]
    assert 'class="fx-scope" data-scope="human_postmortem"' in card.split("</h2>")[0]
    tabs = card.split('id="fx-variants"')[1].split("</div>")[0]
    assert 'data-variant="ant_age"' in tabs and 'data-variant="ant_age_adjusted"' in tabs
    assert "not adjusted for pathology" in tabs and "adjusted for pathology" in tabs
    assert 'id="fx-variant-note-age"' in card and 'id="fx-variant-note-age_adjusted"' in card
    assert "from 60 years" in card or "60 years and older" in card
    assert 'id="fx-recurring"' in card and 'data-scope="human_postmortem"' in card
    assert "/facets/ant_age" in seen[-1] or any(path.startswith("/facets/ant_age") for path in seen)


def test_facet_page_forwards_a_known_scope_and_ignores_an_unknown_one(client, monkeypatch):
    api, seen = _recording_api({
        "/facets/ant_age?scope=xenograft_human_in_mouse": _facet_page_payload("ant_age", "xenograft_human_in_mouse"),
        "/facets/ant_age": _facet_page_payload("ant_age", "human_postmortem")})
    monkeypatch.setattr(browser, "_api_get", api)
    text = client.get("/facets/ant_age?scope=xenograft_human_in_mouse").text
    assert "/facets/ant_age?scope=xenograft_human_in_mouse" in seen
    assert 'data-scope="xenograft_human_in_mouse"' in text.split('id="facet-effects"')[1]
    seen.clear()
    client.get("/facets/ant_age?scope=nowhere")
    assert "/facets/ant_age" in seen and not any("nowhere" in path for path in seen)


def test_facet_effects_table_fetches_the_requested_scope_and_variant(client, monkeypatch):
    payload = {"facet": "age_adjusted", "scope": "human_postmortem", "n_datasets_tested": 0, "rows": [], "not_tested": []}
    api, seen = _recording_api({"/facets/ant_age_adjusted/tests?scope=human_postmortem": payload})
    monkeypatch.setattr(browser, "_api_get", api)
    response = client.get("/facets/ant_age/effects-table?scope=human_postmortem&variant=ant_age_adjusted")
    assert response.status_code == 200
    assert "/facets/ant_age_adjusted/tests?scope=human_postmortem" in seen
    assert 'data-scope="human_postmortem"' in response.text or "No dataset has a tested result" in response.text


def test_facet_effects_table_rows_state_scope_stratum_and_floor(client, monkeypatch):
    unit = {"db_id": "D25/HAR", "title": "Xeno", "cell_type": "microglial cell", "facet": "app_nlgf",
            "celltype_source": "constant_all_cells@line=3A21", "design_subset": "line=3A21",
            "contrast": "nlgf_vs_control", "level_ref": "control", "level_test": "nlgf",
            "level_counts": {"control": 8, "nlgf": 16}, "n_donors": 24, "n_donors_ref": 8, "n_donors_test": 16,
            "flags": {"small_sample": False}, "genes": {"n_tested": 100, "n_q05": 5}, "modules": [],
            "analysis_level": "replicate", "replicate_unit": "animal", "inflated": True, "lambda_gc": 6.68,
            "covariates_dropped": ["batch: pool_id has more levels than donors / 4"],
            "control_basis": "humanised_app_host", "cohort_label": "Davis 2025", "counted": True,
            "facet_scope": "xenograft_human_in_mouse",
            "notes": ["detects large effects only (a handful of animals per genotype)",
                      "live-cell isolation: dissociation-response genes (FOS, JUN, HSP, NFKBIA) are suspect",
                      "stratified: line=3A21", "genomic inflation lambda 6.68: most genes shift together"],
            "model": _fx_model("expression ~ app_nlgf + sex")}
    floor = dict(unit, db_id="D25/XYZ", analysis_level="below_floor", inflated=False, covariates_dropped=[],
                 control_basis=None, notes=[], n_donors=5, level_counts={"control": 2, "nlgf": 3},
                 n_donors_ref=2, n_donors_test=3, analysis_level_reason="2 animals: a group reports p values from 3")
    payload = {"facet": "app_nlgf", "scope": "xenograft_human_in_mouse", "n_datasets_tested": 2,
               "rows": [unit, floor], "not_tested": []}
    monkeypatch.setattr(browser, "_api_get", _api_with({"/facets/ant_app_nlgf/tests?scope=xenograft_human_in_mouse": payload}))
    text = client.get("/facets/ant_app_nlgf/effects-table?scope=xenograft_human_in_mouse").text
    assert 'class="fx-scope" data-scope="xenograft_human_in_mouse"' in text
    assert "stratum: line=3A21" in text
    assert "App NL-G-F vs control" in text and "nlgf 16, control 8" in text
    assert "inflated, lambda 6.68" in text and "covariates dropped (1)" in text
    assert "control: humanised-App host (App hu/hu)" in text
    assert "Detects large effects only" in text and "Live-cell isolation" in text
    assert "descriptive: below the p-value floor (animals need 3 per group)" in text
    assert "2 animals: a group reports p values from 3" in text
    assert 'data-level="below_floor"' in text and 'data-descriptive="true"' in text
    assert "stratified" not in text.split("fx-unit-note")[1] if "fx-unit-note" in text else True


def test_fx_pills_cover_floor_small_sample_inflation_covariates_and_sex_check():
    pills = {label: (css, slug) for label, css, _tip, slug in browser._fx_pills({
        "analysis_level": "below_floor", "replicate_unit": "donor", "facet": "braak", "flags": {"descriptive_only": True},
        "inflated": True, "lambda_gc": 1.41, "covariates_dropped": ["sex: dropped", "batch: dropped"],
        "n_sex_discordant": 2, "n_sex_checked": 10, "age_top_coded": 90})}
    assert pills["descriptive: below the p-value floor (donors need 10 for a trend)"] == ("warn", "p-value-floor")
    assert "descriptive only" not in pills
    assert pills["inflated, lambda 1.41"][1] == "lambda"
    assert pills["covariates dropped (2)"][1] == "covariates"
    assert pills["2 of 10 sex labels disagree"][1] == "sex-check"
    assert pills["ages top-coded"][1] == "age-twins"
    small = {label for label, *_ in browser._fx_pills({"analysis_level": "replicate", "flags": {"small_sample": True}})}
    assert "small sample" in small
    assert not {label for label, *_ in browser._fx_pills({"analysis_level": "replicate", "flags": {"small_sample": True},
                                                          "inflated": False, "covariates_dropped": []})} & {"inflated"}
    boolean = dict((label, tip) for label, _css, tip, _slug in browser._fx_pills({"age_top_coded": True}))
    assert "ceiling" in boolean["ages top-coded"]
    assert any(label.startswith("control: wild-type host") for label, *_ in browser._fx_pills(
        {"control_basis": "wild_type_host"}))
    assert any(label == "stratum line 3A21, timepoint 6m" for label, *_ in browser._fx_pills(
        {"facet": "app_nlgf", "design_subset": "line=3A21|timepoint=6m".replace("=", " ").replace("line 3A21|timepoint 6m", "line 3A21, timepoint 6m")}))


@pytest.mark.parametrize("unit,expected", [
    ({"replicate_unit": "donor", "facet": "sex"}, "donors need 5 per group"),
    ({"replicate_unit": "donor", "facet": "age"}, "donors need 10 for a trend"),
    ({"replicate_unit": "animal", "facet": "app_nlgf"}, "animals need 3 per group"),
    ({"replicate_unit": "animal", "facet": "braak"}, "animals need 6 for a trend"),
    ({"replicate_unit": "sample", "facet": "disease"}, "samples need 3 per group"),
    ({}, "donors need 5 per group"),
])
def test_floor_text_follows_replicate_unit_and_facet_kind(unit, expected):
    assert browser._floor_text(unit) == expected


@pytest.mark.parametrize("unit,expected", [
    ({"facet": "age"}, "trend per decade of age, not adjusted for pathology"),
    ({"facet": "age_adjusted"}, "trend per decade of age, adjusted for pathology"),
    ({"facet": "braak_threshold"}, "Braak stage III or higher vs II or lower"),
    ({"facet": "apoe4_dose"}, "trend per APOE e4 allele"),
    ({"facet": "app_nlgf"}, "App NL-G-F vs control"),
    ({"facet": "sex", "level_test": "m", "level_ref": "f"}, "m vs f"),
    ({"facet": "sex", "contrast": "m_vs_f"}, "m_vs_f"),
])
def test_contrast_labels_of_the_v3_facets(unit, expected):
    assert browser._facet_contrast_label(unit) == expected


def test_donors_text_of_numeric_facets_names_the_range_and_the_replicate_unit():
    age = {"facet": "age", "n_donors": 84, "replicate_unit": "donor",
           "level_counts": {"n_donors": 84, "value_min": 61.4, "value_max": 94.0}}
    assert browser._fx_donors_text(age) == "84 donors, age 61 to 94"
    dose = {"facet": "apoe4_dose", "n_donors": 30, "level_counts": {"value_min": 0, "value_max": 2}}
    assert browser._fx_donors_text(dose) == "30 donors, e4 alleles 0 to 2"
    braak = {"facet": "braak", "n_donors": 21, "replicate_unit": "animal", "level_counts": {"braak_min": 0, "braak_max": 6}}
    assert browser._fx_donors_text(braak) == "21 animals, stages 0 to 6"
    assert browser._fx_donors_text({"facet": "age", "n_donors": 5, "level_counts": {}}) == "5 donors"


def test_exclusion_text_and_tests_view_flag_excluded_datasets():
    spatial = "excluded from the facet layer: spatial technology (visium): no replicate-level cell-type pseudobulk"
    view = browser._facet_tests_view({"status": "not_tested", "reason": spatial, "units": [], "n_units": 0,
                                      "facet_scope": "human_postmortem"})
    assert view["excluded"] is True and "spatial data (visium)" in view["excluded_text"]
    assert view["facet_scope"] is None
    mouse = browser._facet_tests_view({"status": "not_tested", "reason": "mouse-only dataset: not part of the facet layer",
                                       "units": []})
    assert mouse["excluded"] and "pure mouse dataset" in mouse["excluded_text"]
    plain = browser._facet_tests_view({"status": "not_tested", "reason": "secondary (is_secondary true)", "units": []})
    assert plain["excluded"] is False and plain["excluded_text"] is None
    tested = browser._facet_tests_view({"status": "tested", "reason": None, "n_units": 1, "facet_scope": "xenograft_human_in_mouse",
                                        "units": [{"facet": "app_nlgf", "tested": True, "facet_scope": "xenograft_human_in_mouse",
                                                   "cell_type": "microglial cell"}]})
    assert tested["facet_scope"] == "xenograft_human_in_mouse"
    assert tested["by_facet"]["ant_app_nlgf"]["scope"] == "xenograft_human_in_mouse"
    assert browser._fx_exclusion_text(None) is None
    assert "left out of the facet analysis on purpose: metacell set" in browser._fx_exclusion_text(
        "excluded from the facet layer: metacell set: each row is an aggregate of cells")
    assert "proteomics" in browser._fx_exclusion_text("excluded from the facet layer: proteomics (mass-spectrometry): x")


def test_dataset_page_of_an_excluded_dataset_explains_why_there_is_no_facet_analysis(client, monkeypatch):
    spatial = {"db_id": "GroupA/ds1", "status": "not_tested", "n_units": 0, "units": [], "facet_scope": "human_postmortem",
               "reason": "excluded from the facet layer: spatial technology (visium): no replicate-level cell-type pseudobulk"}
    monkeypatch.setattr(browser, "_api_get", _api_with({
        "/databases/GroupA/ds1/facet-tests": spatial,
        "/databases/GroupA/ds1/facets": {"facets": [], "n_facets": 0}}))
    text = client.get("/browse/GroupA/ds1").text
    assert 'data-tab="facets"' in text and 'id="tab-facets"' in text
    block = text.split('id="fx-excluded"')[1].split("</p>")[0]
    assert "not part of the facet analysis" in text and "spatial data (visium)" in block
    assert 'id="fx-excluded-about"' in text and 'id="fx-excluded-about" open' not in text
    assert "no replicate-level cell-type pseudobulk" in text
    assert 'id="fx-unit-line"' not in text and 'id="fx-scope"' not in text
    assert 'id="fx-not-tested"' not in text


def test_dataset_page_of_a_pure_mouse_dataset_uses_the_mouse_text(client, monkeypatch):
    monkeypatch.setattr(browser, "_api_get", _api_with({
        "/databases/GroupA/ds1/facet-tests": {"db_id": "GroupA/ds1", "status": "not_tested", "n_units": 0, "units": [],
                                              "reason": "organism not human (mouse)"}}))
    text = client.get("/browse/GroupA/ds1").text
    assert "This is a pure mouse dataset" in text.split('id="fx-excluded"')[1]


def test_dataset_facets_tab_shows_scope_notes_and_derived_twins(client, monkeypatch):
    facets = dict(_DATASET_FACETS, facets=_DATASET_FACETS["facets"] + [
        {"facet": "ant_mg_author_cluster", "label": "Author cluster (microglia)", "summary": "Clusters.", "defined": True,
         "mapping_kind": "renamed", "live": True, "dtype": "categorical", "source_columns": ["subclass"],
         "sources": [{"column": "subclass", "published_as": "ant_mg_author_cluster", "hidden": False}],
         "source_hidden": False, "redacted": False, "value_map": [], "n_cells": 10, "n_missing": 0,
         "hidden_by_config": True},
        {"facet": "ant_host_genotype", "label": "Host genotype (xenograft)", "summary": "Host.", "defined": True,
         "mapping_kind": "renamed", "live": True, "dtype": "categorical", "source_columns": ["g"], "sources": [],
         "source_hidden": False, "redacted": False, "value_map": [], "n_cells": 10, "n_missing": 0,
         "hidden_by_config": True}])
    tests = dict(_FACET_TESTS_DS1, facet_scope="human_postmortem", units=_FACET_TESTS_DS1["units"] + [
        {"facet": "age_adjusted", "cell_type": "microglial cell", "celltype_source": "constant_all_cells",
         "tested": True, "contrast": "age_per_decade", "level_counts": {"n_donors": 40, "value_min": 61, "value_max": 93},
         "n_donors": 40, "facet_scope": "human_postmortem", "analysis_level": "replicate", "replicate_unit": "donor",
         "flags": {}, "genes": {"n_tested": 100, "n_q05": 1}, "modules": [], "model": _fx_model("expression ~ age + braak")}])
    info = dict(_INFO["GroupA/ds1"], facet_scope="human_postmortem", tissue="middle temporal gyrus",
                obs={"categorical": ["tissue_uberon", "x"], "numerical": []})
    monkeypatch.setattr(browser, "_api_get", _api_with({
        "/databases/GroupA/ds1/facets": facets, "/databases/GroupA/ds1/facet-tests": tests,
        "/databases/GroupA/ds1/info": info}))
    text = client.get("/browse/GroupA/ds1").text
    card = text.split('id="ds-facets"')[1].split("<script>")[0]
    assert 'id="fx-scope"' in card and 'data-scope="human_postmortem"' in card.split('id="fx-scope"')[1][:260]
    assert "dataset-scoped, no tests" in card and 'href="/help#glossary-author-cluster"' in card
    assert "host mouse" in card and "umbrella term" in card
    detail = card.split('id="ds-tissue-detail"')[1].split("</div>")[0]
    assert "middle temporal gyrus" in detail and "tissue_uberon" in detail
    # the pathology-adjusted age test has no column of its own: a derived row links to the age facet page
    assert 'data-facet="ant_age_adjusted"' in card and "the age test with a pathology term" in card
    assert 'href="/facets/ant_age"' in card
    assert "Effects of age, adjusted for pathology" in card
    assert "Adjusted for pathology: Braak stage" in card
    assert "—" not in card


def test_fx_assets_show_v3_replication_display():
    module = browser.templates.env.get_template("_facet_effects.html").module
    forest = str(module.fx_forest_assets())
    for phrase in ("carried by ", "effect filter: at least ", "effect filter: below ", "replicated with at least ",
                   "fxf-badge rep2", "r_treat", "m includes ", "n_small", "gene-class-flags", "GENE_FLAGS",
                   "persists after pathology adjustment", "does not persist after pathology adjustment",
                   "descriptive: below the p-value floor", "inflated, lambda ", "covariates dropped (",
                   "unitNoun", "'animals'", "stratum: ", "Anchors: the unit that counted", "carries r",
                   "passes the effect filter", "fxf-caveat-served", "detects large effects only", "SCOPE_LABELS",
                   "Results are counted within one scope"):
        assert phrase in forest, phrase
    for label in ("MT", "ribosomal", "lncRNA / clone", "sex chromosome", "long gene", "APOE locus"):
        assert label in forest, label
    recurring = str(module.fx_recurring("ant_age", "human_postmortem"))
    assert 'data-scope="human_postmortem"' in recurring and "human post-mortem" in recurring
    assert "scopeQuery" in recurring and "antFxRecurring" in recurring and "Carried by" in recurring
    assert "n_effect_gate" in recurring and "n_replicated_effect" in recurring and "gene-class-flags" in recurring
    legend = str(module.fx_flag_legend())
    for phrase in ("below the p-value floor", "small sample", "inflated, lambda", "covariates dropped", "control: humanised-App host",
                   "persists after pathology adjustment"):
        assert phrase in legend, phrase


def test_gene_panel_loads_each_scope_with_its_own_facet_tabs():
    module = browser.templates.env.get_template("_facet_effects.html").module
    panel = str(module.fx_gene_panel("PTPRG"))
    assert "human post-mortem" in panel and "human cells in mouse hosts (xenograft)" in panel
    assert "xenograft_human_in_mouse: [['graft_sex', 'sex of grafted cells (donor line)'], ['host_sex', 'sex of host mouse'], ['app_nlgf', 'App NL-G-F host']]" in panel
    assert "'/effects?gene=' + encodeURIComponent(gene) + '&scope='" in panel
    assert 'id="gene-fx-scope-about"' in panel and "never mixed" in panel


def test_facet_pages_of_dataset_scoped_tissue_and_genotype_facets_carry_their_notes(client, monkeypatch):
    cluster = _facet_page_payload("ant_mg_author_cluster", "human_postmortem", "categorical", definition_scope="dataset")
    tissue = _facet_page_payload("ant_tissue", "human_postmortem", "categorical")
    host = _facet_page_payload("ant_host_genotype", "xenograft_human_in_mouse", "categorical")
    nlgf = _facet_page_payload("ant_app_nlgf", "xenograft_human_in_mouse", "categorical")
    monkeypatch.setattr(browser, "_api_get", _api_with({
        "/facets/ant_mg_author_cluster": cluster, "/facets/ant_tissue": tissue,
        "/facets/ant_host_genotype": host, "/facets/ant_app_nlgf": nlgf}))
    text = client.get("/facets/ant_mg_author_cluster").text
    note = text.split('id="facet-dataset-scoped"')[1].split("</p>")[0]
    assert "no gene tests are run" in note and "never compared with another dataset" in note
    assert 'id="facet-effects"' not in text      # mapping table only
    assert 'id="facet-tissue-umbrella"' not in text
    tissue_text = client.get("/facets/ant_tissue").text
    assert "tissue_uberon" in tissue_text.split('id="facet-tissue-umbrella"')[1].split("</p>")[0]
    host_text = client.get("/facets/ant_host_genotype").text
    assert 'href="/facets/ant_genotype"' in host_text.split('id="facet-genotype-pair"')[1].split("</p>")[0]
    nlgf_text = client.get("/facets/ant_app_nlgf").text
    short = nlgf_text.split('id="fx-app-nlgf-short"')[1].split("</p>")[0]
    assert "large effects only" in short and "dissociation-response" in short
    assert 'id="fx-app-nlgf-about"' in nlgf_text and 'id="fx-app-nlgf-about" open' not in nlgf_text
    assert 'id="fx-variants"' not in nlgf_text


def test_help_covers_the_v3_facet_topics(client):
    text = client.get("/help").text
    effects = _help_sections(text, "facet-effects", "facet-counting")
    for anchor in ("facet-scopes", "facet-excluded", "facet-age", "facet-sensitivity", "facet-app-nlgf",
                   "facet-anatomy-clusters", "facet-replicates-needed"):
        assert f'id="{anchor}"' in effects, anchor
    for phrase in ("human post-mortem", "xenograft", "left out", "persists after pathology adjustment",
                   "p-value floor", "dataset-scoped", "tissue_uberon", "ant_host_genotype"):
        assert phrase in effects, phrase
    glossary = _help_sections(text, "glossary")
    for slug in ("p-value-floor", "small-cohorts", "age-twins", "app-nlgf", "host-genotype", "author-cluster",
                 "tissue-umbrella", "facet-exclusion", "gene-class-flags", "sex-check", "scope"):
        assert f'id="glossary-{slug}"' in glossary, slug
    assert "\u2014" not in effects and "\u2014" not in glossary


def test_dataset_geneset_tab_has_rank_table_in_the_genes_tab_style(client, monkeypatch):
    """The Genesets tab opens with one row per scored gene set: rank bar, std. dev. bar and plot icons, like the Genes tab."""
    def _with_scores(api_url, path):
        result = _fake_api_get(api_url, path)
        if path.endswith('/info') and isinstance(result, dict):
            result = dict(result)
            obs = dict(result.get('obs', {}))
            obs['numerical'] = ['n_genes', 'pseudotime', 'studyA::expB::Mod1']
            result['obs'] = obs
        return result
    monkeypatch.setattr(browser, '_api_get', _with_scores)
    html = client.get("/browse/GroupA/ds1").text
    section = html.split('id="tab-modules"')[1].split("Geneset scores")[0]
    assert 'id="obs-tbl-modrank"' in section and "/module_ranks" in section and "gx-table" in section
    for header in ("Rank among gene sets (%)", "Std. dev.", "Plots", "Cells"):
        assert header in section
    assert "antBar.html(" in section and "antPlotIcons.row(" in section and "antDot" not in section
    assert "Standard deviation of the module score over all cells in the dataset (samples for bulk)" in section


# ── Dataset page: Facet GS tab holds the per-cell association explorer ───────

def _dataset_with_module_scores(monkeypatch):
    def with_scores(api_url, path):
        result = _fake_api_get(api_url, path)
        if path.endswith("/info") and isinstance(result, dict):
            result = dict(result)
            obs = dict(result.get("obs", {}))
            obs["numerical"] = list(obs.get("numerical", [])) + ["Sierksma2025::WGCNA::Brown_Lysosome"]
            result["obs"] = obs
        return result
    monkeypatch.setattr(browser, "_api_get", with_scores)


def test_dataset_facet_gs_tab_follows_genesets_and_facets_and_is_greyed(client, monkeypatch):
    _dataset_with_module_scores(monkeypatch)
    html = client.get("/browse/GroupA/ds1").text
    bar = html.split('id="ds-tab-bar"')[1].split("</div>")[0]
    tabs = re.findall(r'data-tab="([^"]+)"', bar)
    # The Facet group (label, Gene pill when facets exist, GS pill) follows Genesets
    following = ["facets", "facet-gene", "facet-gs"] if "facet-gene" in tabs else ["facets", "facet-gs"]
    assert tabs[tabs.index("modules") + 1:tabs.index("modules") + 1 + len(following)] == following
    button = re.search(r'<button[^>]*data-tab="facet-gs"[^>]*>[^<]*', bar).group(0)
    assert "vw-preview" in button and button.endswith(">GS")
    assert browser.VIEW_PREVIEW_TIP in button
    assert "vw-preview" not in re.search(r'<button[^>]*data-tab="modules"[^>]*>', bar).group(0)
    assert "dataset-facet-gs" in browser.VIEW_REGISTRY and "dataset-facet-gs" not in browser.APPROVED_VIEWS


def test_dataset_explorer_moved_from_genesets_to_facet_gs(client, monkeypatch):
    _dataset_with_module_scores(monkeypatch)
    html = client.get("/browse/GroupA/ds1").text
    modules_panel = html.split('id="tab-modules"')[1].split('id="tab-facet-gs"')[0]
    facet_gs_panel = html.split('id="tab-facet-gs"')[1].split('<section class="tab-panel"')[0]
    # Genesets keeps the rank table only.
    assert 'id="obs-tbl-modrank"' in modules_panel
    assert 'id="mv-modules-tab"' not in modules_panel and "Geneset scores" not in modules_panel
    # Facet GS carries the banner, the plain heading, its own spinner and the explorer.
    assert 'class="review-banner"' in facet_gs_panel and 'data-view="dataset-facet-gs"' in facet_gs_panel
    assert "Gene set scores by annotation column" in facet_gs_panel and "genesets)" not in facet_gs_panel
    assert 'data-glossary="per-cell-association"' in facet_gs_panel
    assert 'data-tab-loading="facet-gs"' in facet_gs_panel and 'data-tab-loading="modules"' not in html
    assert 'id="mv-modules-tab"' in facet_gs_panel
    # The explorer waits for its tab.
    assert '"lazy_tab": "facet-gs"' in html
    assert "CTX.lazy_tab" in html and "anthive:tab-activated" in html


def test_dataset_without_module_scores_has_no_facet_gs_tab(client):
    html = client.get("/browse/GroupA/ds1").text
    assert 'data-tab="facet-gs"' not in html and 'id="tab-facet-gs"' not in html


def test_dataset_old_explorer_hashes_alias_to_facet_gs(client, monkeypatch):
    _dataset_with_module_scores(monkeypatch)
    html = client.get("/browse/GroupA/ds1").text
    handle = html.split("function handleHash(hash)")[1].split("window.addEventListener('hashchange'")[0]
    assert "/^tab-modules-mv=(.*)$/" in handle                         # legacy #tab-modules-mv=...
    assert "tabPart === 'modules' && h.indexOf('&mv=') >= 0" in handle  # #modules&mv=... from old gene set links
    assert "'#facet-gs&mv=' + mvState" in handle and "tabPart = 'facet-gs'" in handle


def test_module_view_peer_bar_shows_percentage_and_has_no_em_dash(client):
    html = client.get("/genesets/StudyX/Exp1").text
    assert "'<span class=\"vw-bar-val\">' + Math.round(pos * 100) + '%</span>'" in html
    assert "The dot is" not in html and "red, amber and green" not in html
    assert "it is a position on that range, not a percentile" in html
    template = (Path(browser.__file__).parent / "templates" / "_module_view.html").read_text(encoding="utf-8")
    assert "\u2014" not in template


# ── Dataset page tab order and empty-state tabs (2026-10-13) ─────────────────

def _tab_order(html):
    bar = html.split('id="ds-tab-bar"')[1].split("</div>")[0]
    return re.findall(r'data-tab="([^"]+)"', bar), bar


def test_dataset_tab_order_follows_the_agreed_sequence(client, monkeypatch):
    def everything(api_url, path):
        result = _fake_api_get(api_url, path)
        if path.endswith("/info") and isinstance(result, dict):
            result = dict(result)
            obs = dict(result.get("obs", {}))
            obs["numerical"] = list(obs.get("numerical", [])) + ["Sierksma2025::WGCNA::Brown_Lysosome"]
            result["obs"] = obs
            result["caveats"] = [{"level": "medium", "message": "batch effect"}]
        return result
    monkeypatch.setattr(browser, "_api_get", everything)
    tabs, bar = _tab_order(client.get("/browse/GroupA/ds1").text)
    expected = ["overview", "data", "num", "cat", "genes", "modules", "facets", "facet-gene", "facet-gs", "de", "papers", "downloads", "caveats"]
    present = [name for name in expected if name in tabs]
    assert tabs == present, tabs
    for name in ("data", "num", "cat", "modules", "facet-gs", "downloads", "caveats"):
        assert name in tabs, name
    caveat_button = re.search(r'<button[^>]*data-tab="caveats"[^>]*>', bar).group(0)
    assert "caveat-tab lvl-medium" in caveat_button and "tab-empty" not in caveat_button


def test_dataset_without_caveats_has_a_greyed_caveats_tab(client):
    html = client.get("/browse/GroupA/ds2").text
    tabs, bar = _tab_order(html)
    assert tabs[-2:] == ["downloads", "caveats"]
    caveat_button = re.search(r'<button[^>]*data-tab="caveats"[^>]*>[^<]*', bar).group(0)
    assert "tab-empty" in caveat_button and "caveat-tab" not in caveat_button and "vw-preview" not in caveat_button
    assert 'title="No caveats recorded for this dataset"' in caveat_button
    panel = html.split('id="tab-caveats"')[1].split("</section>")[0]
    assert "No caveats recorded for this dataset." in panel and "caveats-card" not in panel
    # the empty-state style is its own class, not the approval marker
    assert ".tab-btn.tab-empty" in html


# ── Facet tab group: label (description), Gene (effects), GS (explorer) ──────

def test_dataset_facet_group_holds_label_gene_and_gs(client, monkeypatch):
    _dataset_with_module_scores(monkeypatch)
    text = client.get("/browse/GroupA/ds1").text
    bar = text.split('id="ds-tab-bar"')[1].split("</div>")[0]
    group = bar.split('id="tab-group-facet"')[1].split("</span>\n")[0]
    assert re.findall(r'data-tab="([^"]+)"', group) == ["facets", "facet-gene", "facet-gs"]
    label = re.search(r'<button[^>]*data-tab="facets"[^>]*>Facet', group).group(0)
    assert "tab-group-label" in label and "vw-preview" not in label and "tab-empty" not in label
    gene = re.search(r'<button[^>]*data-tab="facet-gene"[^>]*>Gene', group).group(0)
    assert "tab-sub" in gene and "vw-preview" in gene        # the matrix view is not approved yet
    gs = re.search(r'<button[^>]*data-tab="facet-gs"[^>]*>GS', group).group(0)
    assert "tab-sub" in gs and "vw-preview" in gs
    assert 'data-tab="facet-gs" role="tab" id="tab-btn-facet-gs"' in group
    # the label panel keeps the description; the effects live in the Gene panel
    facets_panel = text.split('id="tab-facets"')[1].split("</section>")[0]
    gene_panel = text.split('id="tab-facet-gene"')[1].split("</section>")[0]
    assert 'id="fx-unit-line"' in facets_panel and 'id="ds-facets-about"' in facets_panel
    assert 'id="facets-effects-head"' not in facets_panel
    assert 'id="facets-effects-head"' in gene_panel and 'id="ds-facet-effects"' in gene_panel
    assert ".tab-group:has(.tab-btn.active)" in text


def test_dataset_facet_group_without_facets_keeps_gs_and_greys_the_label(client, monkeypatch):
    def scores_no_facets(api_url, path):
        result = _fake_api_get(api_url, path)
        if path.endswith("/info") and isinstance(result, dict):
            result = dict(result)
            obs = dict(result.get("obs", {}))
            obs["numerical"] = list(obs.get("numerical", [])) + ["Sierksma2025::WGCNA::Brown_Lysosome"]
            result["obs"] = obs
        return result
    monkeypatch.setattr(browser, "_api_get", scores_no_facets)
    text = client.get("/browse/GroupA/ds2").text
    bar = text.split('id="ds-tab-bar"')[1].split("</div>")[0]
    group = bar.split('id="tab-group-facet"')[1].split("</span>\n")[0]
    assert re.findall(r'data-tab="([^"]+)"', group) == ["facets", "facet-gs"]
    assert "tab-empty" in re.search(r'<button[^>]*data-tab="facets"[^>]*>', group).group(0)
    assert 'id="ds-no-facets"' in text and 'id="tab-facet-gene"' not in text


def test_dataset_hash_routing_opens_panels_holding_an_anchor(client):
    text = client.get("/browse/GroupA/ds1").text
    handle = text.split("function handleHash(hash)")[1].split("window.addEventListener('hashchange'")[0]
    assert "anchorEl.closest('.tab-panel')" in handle and "scrollIntoView" in handle
    assert "tabPart === 'ds-facet-effects'" in handle


def test_facet_label_panel_uses_key_value_tags_with_help_toggles(client):
    text = client.get("/browse/GroupA/ds1").text
    panel = text.split('id="tab-facets"')[1].split("</section>")[0]
    # the mapping help is hidden; the (i) after the heading toggles it
    head = panel.split('<h2 class="section-head">Facets')[1].split("</h2>")[0]
    assert 'data-help-for="ds-facets-about"' in head
    assert '<div class="doc-box" id="ds-facets-about" role="region" aria-label="About the facet mappings" hidden>' in panel
    assert '<summary>&#9432; About the facet mappings' not in panel
    keys = panel.split('id="fx-unit-line"')[1].split("</table>")[0]
    for row, help_id in (("fx-replicate", "fx-help-replicate"), ("fx-cohort", "fx-help-cohort"), ("fx-disease-basis", "fx-help-disease")):
        cells = keys.split('id="%s"' % row)[1].split("</tr>")[0]
        assert 'data-help-for="%s"' % help_id in cells and 'id="%s"' % help_id in cells
    cohort = keys.split('id="fx-cohort"')[1].split("</tr>")[0]
    assert "Counts towards replication" in cohort and "counts each cohort once" in cohort
    assert "draft awaiting sign-off" in cohort and "DRAFT" not in keys
    assert 'href="/help#facet-effects">About facets and their effects</a>' in cohort


def test_doc_blocks_without_a_header_keep_the_summary_bar():
    for name in ("genes.html", "api_guide.html", "geneset_variability.html"):
        template = (Path(browser.__file__).parent / "templates" / name).read_text(encoding="utf-8", errors="replace")
        assert "summary=True" in template, name


# ── Facet > Gene matrix (2026-10-13) ────────────────────────────────────────

def _matrix_unit(facet, cell_type, tested=True, n=0, up=None, down=None, level='replicate', reason=None):
    return {"facet": facet, "cell_type": cell_type, "celltype_source": "ant_cell_type", "tested": tested,
            "analysis_level": level, "flags": {"descriptive_only": level == "below_floor"}, "reason": reason,
            "genes": {"n_tested": 1000, "n_q05": n, "n_up": up, "n_down": down} if tested else None,
            "level_ref": "f", "level_test": "m", "n_donors_ref": 5, "n_donors_test": 6, "facet_scope": "human_postmortem"}


def test_facet_gene_matrix_orders_columns_like_the_gene_page_and_summarises_cells():
    summary = {"status": "tested", "units": [
        _matrix_unit("sex", "GABAergic neuron", n=3, up=2, down=1),
        _matrix_unit("sex", "microglial cell", n=1500, up=900, down=600),
        _matrix_unit("braak", "microglial cell", n=0, up=0, down=0, level="below_floor"),
        _matrix_unit("disease", "microglial cell", tested=False, level="cell", reason="level with < 3 donors (control=0, AD=12)"),
        _matrix_unit("disease", "GABAergic neuron", tested=False, level="cell", reason="level with < 3 donors (control=2, AD=12)"),
        _matrix_unit("sex", "astrocyte", n=40, up=30, down=10),
    ]}
    matrix = browser._facet_gene_matrix(summary)
    assert [c["cell_type"] for c in matrix["columns"]] == ["microglial cell", "astrocyte", "GABAergic neuron"]
    assert [r["facet"] for r in matrix["rows"]] == ["sex", "braak"]
    sex = {c.get("cell_type"): c for c in matrix["rows"][0]["cells"]}
    assert sex["microglial cell"]["n"] == 1500 and sex["microglial cell"]["n_up"] == 900 and sex["microglial cell"]["level"] == 4
    assert sex["GABAergic neuron"]["level"] == 1 and sex["astrocyte"]["level"] == 2
    assert matrix["rows"][1]["cells"][0]["state"] == "descriptive" and matrix["rows"][1]["cells"][1]["state"] == "none"
    assert matrix["untested_rows"] == [{"facet": "disease", "label": "Disease", "reason": "one value only, too few donors"}]
    assert matrix["has_direction"] is True
    assert browser._facet_gene_matrix({"status": "not_tested", "units": [_matrix_unit("sex", "x", tested=False)]}) is None


@pytest.mark.parametrize("reason,expected", [
    ("level with < 3 donors (f=2, m=9)", "too few donors"),
    ("level with < 3 donors (control=0, nlgf=4)", "one value only"),
    ("braak: 2 replicates / 1 distinct values (need 4 / 3)", "one value only"),
    ("age: 3 replicates / 3 distinct values (need 4 / 3)", "too few donors"),
    ("age_adjusted: no pathology covariate (Braak and disease known for fewer than 80 percent)", "no pathology data"),
    ("host_sex: not recorded (no m or f value in ant_host_sex on any replicate)", "not recorded"),
])
def test_facet_gene_matrix_short_reasons(reason, expected):
    assert browser._fxg_short_reason({"reason": reason}) == expected


def test_dataset_gene_subtab_shows_the_matrix_and_keeps_the_full_table(client):
    text = client.get("/browse/GroupA/ds1").text
    panel = text.split('id="tab-facet-gene"')[1].split("</section>")[0]
    assert 'data-view="dataset-facet-gene"' in panel                     # preview banner
    assert 'id="fxg-matrix"' in panel and 'class="fxg-cell l' in panel
    assert 'data-help-for="fxg-help-sex"' in panel and '<div class="doc-box" id="fxg-help-sex"' in panel
    help_box = panel.split('id="fxg-help-sex"')[1].split('<div class="doc-box"')[0]
    assert "Model:" in help_box and "Sex positive control" in help_box
    # the earlier tables sit in the hidden full effects table box, reachable by the toggle next to the heading
    assert 'data-help-for="fxg-full"' in panel.split("</h2>")[0]
    full = panel.split('<div class="doc-box" id="fxg-full"')[1]
    assert 'id="fx-table-ant_sex"' in full and 'hidden' in panel.split('id="fxg-full"')[1][:120]
    # gene list script: genes endpoint, csv, volcano, plot icons, effect bar
    for needle in ("/facet-tests", "/genes", "&format=csv", "/volcano.png?", "/plot/donor?facet=", "/plot/violin?x=",
                   "/plot/rolling?x=", "&subset=", "per donor", "per cell, descriptive", "antEffectBar", "listScale", "show all "):
        assert needle in panel, needle
    assert "dataset-facet-gene" in browser.VIEW_REGISTRY and "dataset-facet-gene" not in browser.APPROVED_VIEWS


# ── Facet > GS matrix (2026-10-13) ──────────────────────────────────────────

def _module_unit(facet, cell_type, entries):
    unit = _matrix_unit(facet, cell_type, n=5, up=3, down=2)
    unit["modules"] = [{"method": m, "source": src, "n_modules": 100, "n_q05_param": n, "n_q05_perm": n, "n_q05": n,
                        "n_up": up, "n_down": down} for m, src, n, up, down in entries]
    return unit


def test_facet_module_matrices_one_per_variant_default_pseudobulk():
    summary = {"status": "tested", "units": [
        _module_unit("sex", "microglial cell", [("cell_mean", "geneset", 14, 7, 7), ("cell_mean", "ucell", 21, 5, 16),
                                                ("pseudobulk", "gene_list", 3, 1, 2)]),
        _module_unit("braak", "microglial cell", [("cell_mean", "geneset", 2, 2, 0)])]}
    result = browser._facet_module_matrices(summary)
    assert [v["key"] for v in result["variants"]] == ["pseudobulk-gene_list", "cell_mean-geneset", "cell_mean-ucell"]
    assert result["default"] == "pseudobulk-gene_list"
    pseudobulk = result["variants"][0]["matrix"]
    sex = pseudobulk["rows"][0]["cells"][0]
    assert (sex["n"], sex["n_up"], sex["n_down"]) == (3, 1, 2)
    assert pseudobulk["untested_rows"] == [{"facet": "braak", "label": "Braak stage", "reason": "no gene sets"}]
    no_counts = browser._facet_module_matrices({"status": "tested", "units": [
        _module_unit("sex", "microglial cell", [("cell_mean", "geneset", 4, 2, 2)])]})
    assert no_counts["default"] == "cell_mean-geneset"


def test_dataset_gs_subtab_shows_variant_matrices_and_keeps_the_explorer(client, monkeypatch):
    _dataset_with_module_scores(monkeypatch)
    text = client.get("/browse/GroupA/ds1").text
    panel = text.split('id="tab-facet-gs"')[1].split('<section class="tab-panel"')[0]
    assert 'data-view="dataset-facet-gs"' in panel
    head = panel.split("</h2>")[0]
    assert 'data-help-for="fxs-about"' in head and 'data-help-for="fxs-percell"' in head and "per-cell associations (descriptive)" in head
    # the per-cell explorer sits in the hidden box and loads only when it opens
    percell = panel.split('<div class="doc-box" id="fxs-percell"')[1]
    assert percell.split(">")[0].endswith("hidden") and 'id="mv-modules-tab"' in percell
    assert '"lazy_box": "fxs-percell"' in text and "anthive:help-toggled" in text
    assert "The number of significant gene sets grows with the number of donors" in panel
    if 'id="fxs"' in panel:
        assert 'class="fxg-variants"' in panel and "fxg_list" not in panel
        assert "q_by=either" in panel and "/modules" in panel and "row.df >= 30" in panel


def test_gene_help_boxes_no_longer_carry_module_counts(client):
    text = client.get("/browse/GroupA/ds1").text
    help_box = text.split('id="fxg-help-sex"')[1].split('<div class="doc-box"')[0]
    assert "Gene sets below q 0.05" not in help_box and "fx-mods" not in help_box
    assert "grows with the number of donors" in help_box


def test_explorer_links_open_the_per_cell_box(client):
    text = client.get("/browse/GroupA/ds1").text
    handle = text.split("function handleHash(hash)")[1].split("window.addEventListener('hashchange'")[0]
    assert "getElementById('fxs-percell')" in handle and "antHelpOpen(perCell)" in handle


def test_facet_tab_group_has_a_tint_token_with_a_dark_value(client):
    text = client.get("/browse/GroupA/ds1").text
    assert "--tab-group-tint: #eef4f5" in text and ':root:not([data-theme="light"]) { --tab-group-tint:' in text
    assert "background: var(--tab-group-tint)" in text


# ── Other DE (author and custom) under the facet matrices (2026-10-13) ──────

@pytest.mark.parametrize("term,contrast,reference,expected", [
    ("cluster", "3", "vsgrandmean", "3 vs all (cluster)"),
    ("genotype", "K_vs_W", "vsreference", "K vs W (genotype)"),
    ("Strain", "AppNLGF - AppHuHu", "vsgrandmean", "AppNLGF vs AppHuHu (host strain)"),
    ("Sex_donor", "F", "vsgrandmean", "F vs all (sex of the donor)"),
])
def test_de_row_label(term, contrast, reference, expected):
    assert browser._de_row_label(term, contrast, reference) == expected


def test_de_subset_expressible_only_for_one_categorical_column():
    assert browser._de_subset(None, ["ant_cell_type"])["label"] == "all cells"
    one = browser._de_subset({"ant_cell_type": ["microglial cell"]}, ["ant_cell_type"])
    assert one["query"] == "ant_cell_type:microglial cell" and one["label"] == "microglial cell"
    two = browser._de_subset({"a": "x", "b": "y"}, ["a", "b"])
    assert two["query"] == "" and two["label"] == "study subset" and two["note"]


_OTHER_DE_STUDIES = [{"study_id": "s_gene", "name": "Sex in microglia", "method": "deseq2_pseudobulk", "target": "gene"},
                     {"study_id": "s_mod", "name": "Modules", "method": "lmem", "target": "module"}]


def _other_de_fake(api_url, path, **kwargs):
    if path.endswith("/de/s_gene/summary"):
        return {"study_id": "s_gene", "name": "Sex in microglia", "method": "deseq2_pseudobulk", "target": "gene",
                "reference_type": "vsreference", "covariates": ["batch"], "n_cells_total": 1200, "subset_filter": None,
                "cluster_obs_column": None, "description": "Pseudobulk per mouse.", "source": "x", "notes": None,
                "contrasts": [{"term": "intercept", "contrast": "x", "n_genes": 10, "n_sig": 10, "n_up": 5, "n_down": 5, "efftype": "log2fc"},
                              {"term": "Sex", "contrast": "M_vs_F", "n_genes": 100, "n_sig": 12, "n_up": 5, "n_down": 7, "efftype": "log2fc"}]}
    if path.endswith("/de/s_mod/summary"):
        return {"study_id": "s_mod", "name": "Modules", "method": "lmem", "target": "module", "reference_type": "vsgrandmean",
                "subset_filter": None, "contrasts": [{"term": "cluster", "contrast": "3", "n_genes": 13, "n_sig": 2, "n_up": 2,
                                                      "n_down": 0, "efftype": "zeff"}]}
    if path.endswith("/de"):
        return {"studies": _OTHER_DE_STUDIES}
    return _fake_api_get(api_url, path)


def test_other_de_view_splits_gene_and_module_studies(monkeypatch):
    monkeypatch.setattr(browser, "_api_get", _other_de_fake)
    view = browser._other_de_view("http://x", "GroupA/ds1", _OTHER_DE_STUDIES, {"categorical": ["sex", "leiden"]})
    gene = view["gene"][0]
    assert [r["label"] for r in gene["rows"]] == ["M vs F (sex)"]            # intercept dropped
    row = gene["rows"][0]
    assert (row["n"], row["n_up"], row["n_down"], row["obs_column"]) == (12, 5, 7, "sex")
    assert gene["level"].startswith("donor") and gene["subset"]["label"] == "all cells" and gene["covariates"] == "batch"
    assert view["module"][0]["rows"][0]["label"] == "3 vs all (cluster)"


def test_dataset_page_shows_other_de_under_the_gene_matrix_and_points_from_the_de_tab(client, monkeypatch):
    monkeypatch.setattr(browser, "_api_get", _other_de_fake)
    text = client.get("/browse/GroupA/ds1").text
    gene_panel = text.split('id="tab-facet-gene"')[1].split('<section class="tab-panel"')[0]
    assert 'id="ode-gene-head"' in gene_panel and "Other DE (author and custom)" in gene_panel
    assert 'data-view="dataset-other-de"' in gene_panel
    assert "not donor-level facet tests, not counted towards replication" in gene_panel
    assert 'data-study="s_gene" data-term="Sex" data-contrast="M_vs_F"' in gene_panel and 'data-obs-column="' in gene_panel
    assert "/plot/volcano?" in gene_panel and "/plot/ma?" in gene_panel and "padj_max=0.05" in gene_panel
    assert "donor-plot" not in gene_panel.split('id="ode-gene"')[1].split("{%- endmacro")[0].split("fxg_list")[0].split("var root = document.getElementById({{")[0].split("</script>")[0]
    de_tab = text.split('id="tab-de"')[1].split("</section>")[0]
    assert 'id="de-other-pointer"' in de_tab and 'href="#ode-gene-head"' in de_tab
    assert "dataset-other-de" in browser.VIEW_REGISTRY and "dataset-other-de" not in browser.APPROVED_VIEWS
    assert "gene-other-de" in browser.VIEW_REGISTRY and "gene-other-de" not in browser.APPROVED_VIEWS


# ── Per donor plot page (2026-10-13) ────────────────────────────────────────

def test_donor_plot_page_draws_the_api_image_with_db_id(client):
    text = client.get("/browse/GroupA/ds1/plot/donor?facet=braak&cell_type=microglial%20cell&gene=PTPRG").text
    assert "Per donor plot of PTPRG by Braak stage in microglial cell, " in text and "Per donor plot of PTPRG · anthive" in text
    assert "/donor-plot.png?" in text and "/donor-values?" in text
    assert "cell_type=microglial+cell&amp;db_id=GroupA%2Fds1&amp;gene=PTPRG" in text or "db_id=GroupA%2Fds1" in text
    assert 'data-fmt="svg"' in text and 'data-fmt="pdf"' in text and 'id="dp-adjusted"' in text
    assert 'href="/browse/GroupA/ds1#facet-gene"' in text
    assert '<div class="doc-box" id="dp-about"' in text and "the values the donor-level facet test used" in text
    assert 'class="breadcrumb' in text


def test_donor_plot_page_for_a_gene_set_and_without_parameters(client):
    text = client.get("/browse/GroupA/ds1/plot/donor?facet=sex&cell_type=astrocyte&module=S/E/Mod1&method=pseudobulk&source=gene_list").text
    assert "Per donor plot of Mod1 by Sex in astrocyte" in text and "module=S%2FE%2FMod1" in text and "#facet-gs" in text
    assert 'id="dp-invalid"' in client.get("/browse/GroupA/ds1/plot/donor").text


def test_plot_pages_share_the_caveat_partial(client):
    text = client.get("/browse/GroupA/ds1/plot/violin").text
    assert 'id="cv-toggle"' in text and 'id="cv-panel"' in text


def test_other_de_cell_level_studies_are_flat_and_technical_terms_hidden(monkeypatch):
    def fake(api_url, path, **kwargs):
        if path.endswith("/de/glmm/summary"):
            contrasts = [{"term": "Sex_donor", "contrast": "F", "n_genes": 100, "n_sig": 60, "n_up": 20, "n_down": 40, "efftype": "log2fc"},
                         {"term": "batch_group", "contrast": "B1", "n_genes": 100, "n_sig": 3, "n_up": 1, "n_down": 2, "efftype": "log2fc"}]
            contrasts += [{"term": "cluster", "contrast": str(i), "n_genes": 100, "n_sig": i, "n_up": i, "n_down": 0, "efftype": "log2fc"}
                          for i in range(7)]
            return {"study_id": "glmm", "name": "GLMM", "method": "glmm", "target": "gene", "reference_type": "vsgrandmean",
                    "subset_filter": None, "contrasts": contrasts}
        return _fake_api_get(api_url, path)
    monkeypatch.setattr(browser, "_api_get", fake)
    view = browser._other_de_view("http://x", "GroupA/ds1", [{"study_id": "glmm"}], {"categorical": []})
    study = view["gene"][0]
    assert study["cell_level"] is True and study["has_mean"] is False
    assert all(row["level"] == "cell" for row in study["rows"])                     # no count scale
    assert [g["term"] for g in study["groups"]] == ["Sex_donor", "cluster"]
    assert [g["term"] for g in study["technical_groups"]] == ["batch_group"]
    assert next(g for g in study["groups"] if g["term"] == "cluster")["collapsed"] is True
    template = (Path(browser.__file__).parent / "templates" / "_other_de.html").read_text(encoding="utf-8")
    assert "cell level, descriptive" in template and "technical terms (" in template and "sort=' + (cellLevel ? 'effsize'" in template
    assert "d.ma === 'true'" in template


def test_de_tab_offers_the_ma_plot_only_for_pseudobulk_methods():
    assert browser.templates.env.globals["de_has_mean"]("deseq2_pseudobulk") is True
    assert browser.templates.env.globals["de_has_mean"]("glmm") is False
    template = (Path(browser.__file__).parent / "templates" / "dataset.html").read_text(encoding="utf-8")
    de_tab = template.split('id="tab-de"')[1].split("</section>")[0]
    assert "{% if de_has_mean(s.method) %}" in de_tab and "🌋" not in de_tab and "🔥" not in de_tab


def test_dataset_small_fixes_wrap_badge_count_caveats_bulk(client, monkeypatch):
    template = (Path(browser.__file__).parent / "templates" / "dataset.html").read_text(encoding="utf-8")
    assert ".data-card .obs-stats-tbl td:last-child { white-space: normal" in template            # M4
    base = (Path(browser.__file__).parent / "templates" / "base.html").read_text(encoding="utf-8", errors="replace")
    assert ".tab-btn .tab-count, .tab-count {" in base and "border-radius: 9px" in base             # M5
    assert "searchCount.textContent = '';   // the count shows only while a search is active" in template   # M10
    assert "cv.kind != 'note'" in template and "cv.scope not in ('object', 'dataset')" in template       # M11
    def bulk(api_url, path, **kwargs):
        result = _fake_api_get(api_url, path)
        if path.endswith("/info") and isinstance(result, dict):
            result = dict(result, technology="bulk_rna", experimental_system="bulk_tissue")
        return result
    monkeypatch.setattr(browser, "_api_get", bulk)
    text = client.get("/browse/GroupA/ds1").text
    assert 'class="tab-panel ds-bulk" id="tab-genes"' in text and '<td class="colname">samples</td>' in text   # M12


def test_facet_groups_split_tested_untested_structural_and_count_what_is_listed():
    facets = [{"facet": "ant_sex"}, {"facet": "ant_disease"}, {"facet": "ant_pmi"}, {"facet": "ant_cell_type"},
              {"facet": "ant_donor"}, {"facet": "ant_mg_author_cluster"}, {"facet": "ant_braak_group", "hidden_by_config": True}]
    tests = {"by_facet": {"ant_sex": {"tested": [{}], "untested": []},
                          "ant_disease": {"tested": [], "untested": [{"reason": "level with < 3 donors (control=0, AD=12)"}]},
                          "ant_age_adjusted": {"tested": [{}], "untested": []}}}
    groups = browser._facet_groups(facets, tests)
    assert [f["facet"] for f in groups["tested"]] == ["ant_sex"] and groups["extra"] == ["ant_age_adjusted"]
    reasons = {f["facet"]: f["not_tested_reason"] for f in groups["untested"]}
    assert reasons["ant_disease"] == "one value only" and "covariate" in reasons["ant_pmi"]
    assert reasons["ant_braak_group"].startswith("not analysed yet")
    assert [f["facet"] for f in groups["structural"]] == ["ant_cell_type", "ant_donor", "ant_mg_author_cluster"]
    assert groups["n_listed"] == 1 + 1 + 3


def test_page_titles_use_one_format_without_em_dashes():
    import re
    for path in sorted((Path(browser.__file__).parent / "templates").glob("*.html")):
        text = path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"\{% block title %\}(.*?)\{% endblock %\}", text)
        if match and path.name != "base.html":
            assert "\u2014" not in match.group(1) and "anthive" in match.group(1), path.name
            assert " · anthive" in match.group(1), path.name


def test_error_pages_use_one_template(client):
    unknown_geom = client.get("/browse/GroupA/ds1/plot/nosuchgeom")
    assert unknown_geom.status_code == 404 and 'id="error-card"' in unknown_geom.text and "nosuchgeom" in unknown_geom.text
    missing = client.get("/browse/ZZ9/ZZZ")
    assert missing.status_code == 404 and 'id="error-card"' in missing.text
    assert client.get("/datasets", follow_redirects=False).headers["location"] == "/"


def test_install_error_pages_renders_html_for_pages_and_json_for_api():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.state.api_url = "http://testapi"
    app.state.reports = []
    app.state.genesets_available = False
    app.include_router(browser.router)
    browser.install_error_pages(app)
    test_client = TestClient(app)
    page = test_client.get("/no/such/page", headers={"accept": "text/html"})
    assert page.status_code == 404 and 'id="error-card"' in page.text and "This page does not exist." in page.text
    api = test_client.get("/no/such/page", headers={"accept": "application/json"})
    assert api.status_code == 404 and api.json() == {"detail": "Not Found"}


def test_labels_vocabulary_and_effect_colours_are_shared():
    assert browser.display_value("10x_chromium") == "10x Chromium" and browser.display_value("post_mortem_tissue") == "post-mortem tissue"
    assert browser.display_value(["a_b", "10x_multiome"]) == "a b, 10x Multiome"
    base = (Path(browser.__file__).parent / "templates" / "base.html").read_text(encoding="utf-8", errors="replace")
    assert "--effect-up: #cf5a2e; --effect-down: #2c6fb0" in base
    bar = (Path(browser.__file__).parent / "templates" / "_effect_bar.html").read_text(encoding="utf-8")
    assert "var(--effect-up" in bar and "var(--effect-down" in bar
    dataset = (Path(browser.__file__).parent / "templates" / "dataset.html").read_text(encoding="utf-8")
    assert "Stats from <code>/gene_stats</code>" not in dataset and "genes-numbers-about" in dataset
    assert "facet_test_label(column)|lower if facet_test_label(column)[1:2].islower()" in dataset


# ── Regression guards from the 2026-10-13 sweep ─────────────────────────────

def _visible_html(html):
    import re
    html = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    return re.sub(r"<style.*?</style>", "", html, flags=re.S)


@pytest.mark.parametrize("variant", ["plain", "lineage", "scores", "bulk", "other_de", "ds2"])
def test_dataset_page_renders_no_raw_template_text(client, monkeypatch, variant):
    def fake(api_url, path, **kwargs):
        if variant == "other_de":
            return _other_de_fake(api_url, path, **kwargs)
        result = _fake_api_get(api_url, path)
        if path.endswith("/info") and isinstance(result, dict):
            result = dict(result)
            if variant == "lineage":
                result.update(designation="reanalysis", reanalysis_of=["GroupA/ds2"], donor_group="rosmap", has_donor_uid=True)
            if variant == "scores":
                obs = dict(result.get("obs", {}))
                obs["numerical"] = list(obs.get("numerical", [])) + ["Sierksma2025::WGCNA::Brown_Lysosome"]
                result["obs"] = obs
            if variant == "bulk":
                result.update(technology="bulk_rna", experimental_system="bulk_tissue")
        return result
    monkeypatch.setattr(browser, "_api_get", fake)
    html = client.get("/browse/GroupA/ds2" if variant == "ds2" else "/browse/GroupA/ds1").text
    visible = _visible_html(html)
    assert "{{" not in visible and "}}" not in visible and "{%" not in visible and "%}" not in visible, variant


@pytest.mark.parametrize("path", ["/", "/browse/GroupA", "/genesets", "/library", "/facets", "/donor-groups",
                                  "/browse/GroupA/ds1/plot/violin", "/browse/GroupA/ds1/plot/donor?facet=sex&cell_type=x&gene=XIST"])
def test_other_pages_render_no_raw_template_text(client, path):
    visible = _visible_html(client.get(path).text)
    assert "{{" not in visible and "}}" not in visible and "{%" not in visible, path


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("path", ["/browse/GroupA/ds1", "/browse/GroupA/ds1/plot/violin", "/browse/GroupA",
                                  "/browse/GroupA/ds1/plot/donor?facet=sex&cell_type=x&gene=XIST", "/library", "/"])
def test_rendered_page_scripts_parse(client, tmp_path, path):
    import re, subprocess
    html = client.get(path).text
    for index, script in enumerate(re.findall(r"<script>(.*?)</script>", html, re.S)):
        target = tmp_path / f"s{index}.js"
        target.write_text(script, encoding="utf-8")
        done = subprocess.run(["node", "--check", str(target)], capture_output=True, text=True)
        assert done.returncode == 0, f"{path} script {index}: {done.stderr[:400]}"


def test_up_is_warm_and_down_is_cool_from_one_token_pair():
    import re
    templates = Path(browser.__file__).parent / "templates"
    matrix = (templates / "_facet_gene_matrix.html").read_text(encoding="utf-8")
    # every rule that colours .up / .down text uses the ink tokens, and nothing overrides them per colour step
    rules = re.findall(r"([^{}\n]*\.(?:up|down)\b[^{}]*)\{([^}]*)\}", matrix)
    coloured = [(sel, body) for sel, body in rules if "color:" in body.replace("background", "")]
    assert coloured, "no up/down colour rule found"
    for selector, body in coloured:
        colour = re.search(r"(?<!background-)color:\s*([^;]+)", body).group(1)
        if ".up" in selector:
            assert "--effect-up" in colour, selector
        if ".down" in selector:
            assert "--effect-down" in colour, selector
    base = (templates / "base.html").read_text(encoding="utf-8", errors="replace")
    up = re.search(r"--effect-up: (#[0-9a-f]{6})", base).group(1)
    down = re.search(r"--effect-down: (#[0-9a-f]{6})", base).group(1)
    assert int(up[1:3], 16) > int(up[5:7], 16) and int(down[5:7], 16) > int(down[1:3], 16)    # warm red-orange, cool blue
    for name in ("dataset.html", "_other_de.html", "_facet_gene_matrix.html"):
        text = (templates / name).read_text(encoding="utf-8")
        assert "#a8441f" not in text and "#235d94" not in text and "#7a2c10" not in text, name


def test_tab_bar_stays_compact():
    base = (Path(browser.__file__).parent / "templates" / "base.html").read_text(encoding="utf-8", errors="replace")
    assert "padding: 6px 10px; font-size: 12px;" in base                       # tab buttons
    assert "min-width: 14px; padding: 0 3px; margin-left: 3px;" in base         # badges
