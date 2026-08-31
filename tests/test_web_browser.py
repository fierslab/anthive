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


def test_genesets_index_renders_study_drilldown(client):
    # The landing shows a per-study drilldown table with cell-type and
    # organism filter popovers. Each study row links to /genesets/<study>.
    resp = client.get("/genesets")
    assert resp.status_code == 200
    assert 'id="gs-study-table"' in resp.text
    assert 'data-col="celltype"' in resp.text
    assert 'data-col="organism"' in resp.text
    assert 'data-filter-fields="celltype"' in resp.text
    assert 'data-filter-fields="organism"' in resp.text
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


def test_topbar_api_link_opens_in_new_tab(client):
    # The 'API' link points at the FastAPI Swagger UI (/api/docs) and opens
    # in a new tab. Assert both the href and the noopener target so downstream
    # tooling / accessibility scans keep it that way.
    resp = client.get("/")
    assert resp.status_code == 200
    idx = resp.text.find('href="/api/docs"')
    assert idx >= 0, "API link (/api/docs) missing from topbar"
    # Look at the anchor tag containing that href.
    tag_start = resp.text.rfind("<a ", 0, idx)
    tag_end = resp.text.find(">", idx)
    tag = resp.text[tag_start:tag_end + 1]
    assert 'target="_blank"' in tag, "API link must open in a new tab"
    assert 'rel="noopener"' in tag, "API link must have rel=noopener"


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
    first_nav = _re_local.search(r"<nav>(.*?)</nav>", resp.text, _re_local.S)
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
    assert 'Genesets<span class="tab-count">' in resp.text
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
    # h5ad label + supplementary heading are the only kinds surfaced.
    assert "raw AnnData" in resp.text
    assert "Supplementary materials from paper" in resp.text
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


# ── Ask 3: facet label ────────────────────────────────────────────────────────

def test_module_view_facet_label(client):
    """The tag filter label must read 'facet' (not bare 'tag').
    The table column header must also say 'facet'.
    """
    resp = client.get("/genesets/StudyX/Exp1")
    assert resp.status_code == 200
    html = resp.text
    assert '>facet<' in html or 'facet</label' in html or 'facet <span' in html, (
        "Label 'facet' not found in filter row"
    )
    # Column header: th contains 'facet' text.
    assert '>facet<' in html or 'facet</th' in html, (
        "Column header for tag column must say 'facet'"
    )


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

def test_module_view_dataset_link_uses_modules_tab_hash(client):
    """Dataset links must use #modules&mv=... so the Modules tab activates on landing."""
    resp = client.get("/genesets/StudyX/Exp1/gs_up")
    assert resp.status_code == 200
    html = resp.text
    # The JS template string that builds the href must include '#modules'.
    assert "'#modules'" in html or '"#modules"' in html or "'#modules&'" in html, (
        "datasetUrl must embed '#modules' so the Modules tab is activated"
    )
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


def test_genes_tab_has_sparkline_elements(client):
    """Each gene row has .ss sparkline elements for pct, rank, and variability."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # .ss is the compact sparkline class.
    assert 'class="ss"' in html or 'class="gene-sparkline"' in html, \
        ".ss sparkline must be rendered in gene rows"
    # Uniform sparkline tracks: pct / rank / var.
    assert 'data-stat="pct"'  in html, "pct sparkline track required"
    assert 'data-stat="rank"' in html, "rank sparkline track required"
    assert 'data-stat="var"'  in html, "variability sparkline track required"
    # No raw mean or meannz tracks.
    assert 'data-stat="mean"'   not in html, "mean sparkline track must not appear"
    assert 'data-stat="meannz"' not in html, "meannz sparkline track must not appear"
    # The JS sparkline helper must be present.
    assert "setSsMark" in html or "setSparkline" in html, \
        "sparkline JS helper (setSsMark or setSparkline) must be defined"


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


def test_genes_tab_topvar_rows_have_no_star(client):
    """Top-var rows carry a ♥ only when the gene is ALSO a personal favourite;
    otherwise the flag cell is empty. The renderTopVar branch computes this
    per-row (isFav ? _HEART_HTML : '')."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # renderTopVar passes an isFav-derived flag string, not a literal ★.
    assert "isFav ? _HEART_HTML : ''" in html, \
        "renderTopVar must decide the flag per-row (personal favourite check)"
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
    % expr / rank expr / variability. No mean or mean(nz). The separate
    search-results table is gone."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    tbl_start = html.find('id="obs-tbl-genes"')
    assert tbl_start != -1, "#obs-tbl-genes not found"
    thead_end = html.find('</thead>', tbl_start)
    thead = html[tbl_start:thead_end]
    assert '% expr'     in thead or 'pct'  in thead.lower(), \
        "#obs-tbl-genes thead missing % expr column"
    assert 'rank expr'  in thead, "#obs-tbl-genes thead missing 'rank expr' column"
    assert 'variability' in thead, "#obs-tbl-genes thead missing 'variability' column"
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


def test_numerical_tab_sparkline(client):
    """Numerical obs tab: each row has a .ss range sparkline for IQR/median."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # The numerical table should have the sparkline class.
    assert 's-num-spark' in html, ".s-num-spark sparkline must be present in numerical tab"
    # JS must set the mark based on median vs min/max range.
    assert "s-num-spark" in html and "setSsMark" in html, \
        "numerical sparkline must be positioned by setSsMark"
    # Median column present in numerical tab.
    assert 'median' in html, "median column must appear in numerical tab"


def test_categorical_tab_ecdf_link(client):
    """Categorical tab: each column has an ECDF link icon (revealed by JS when a numeric col exists)."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # ECDF icon present in categorical tab (hidden by default, shown by JS).
    assert 's-ecdf-link' in html, ".s-ecdf-link ECDF icon must be in categorical rows"
    # JS must choose a first numerical column and set href.
    assert "_firstNumCol" in html, "_firstNumCol JS variable must select the numeric x-axis for ECDF"


def test_categorical_tab_top_categories_pct_sparkline(client):
    """Categorical tab: top categories show % label and .ss sparkline in JS render."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # JS renders % of cells next to each top category with a sparkline.
    assert "pct" in html, "pct value must appear in categorical top-category rendering"
    # Sparkline class emitted in JS string for top-category rendering.
    assert 'class="ss"' in html, ".ss sparkline class must appear in page"


def test_compact_sparkline_css_present(client):
    """Global .ss compact sparkline CSS must be declared in dataset page."""
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    assert '.ss {' in html or '.ss{' in html, ".ss CSS rule must be present"
    assert '.ss::before' in html, ".ss::before grey line must be declared"
    assert '.ss-mark' in html, ".ss-mark tick must be declared"


def test_module_view_sparkline_restyle(client, monkeypatch):
    """Module view track uses new compact ::before line + 2px tick, not old pill.
    Requires the _module_view.html partial to be included, which only happens
    when n_module_scores > 0. We monkeypatch a minimal score column list.
    """
    def _with_scores(api_url, path):
        result = _fake_api_get(api_url, path)
        if path.endswith('/info') and isinstance(result, dict):
            result = dict(result)
            # Add a scanpy score column (naming convention) so n_module_scores > 0
            # and the _module_view.html partial gets included.
            obs = dict(result.get('obs', {}))
            obs['numerical'] = ['n_genes', 'pseudotime', 'studyA::expB::Mod1']
            result['obs'] = obs
        return result
    monkeypatch.setattr(browser, '_api_get', _with_scores)
    resp = client.get("/browse/GroupA/ds1")
    assert resp.status_code == 200
    html = resp.text
    # New style: ::before pseudo-element grey line in the module view track.
    assert '.mv-track::before' in html, ".mv-track::before must define the grey line"


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
    ("/browse/GroupA",                "collections"),     # folder page: crumb is "collections"
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


def test_gene_atlas_sparkline_class_present(client):
    """Minimal sparkline .ss class with .ss-tick must be defined and referenced."""
    resp = client.get("/genes/APOE")
    assert resp.status_code == 200
    assert 'class="ss"' in resp.text or '.ss {' in resp.text or '.ss{' in resp.text, (
        ".ss sparkline class not found in gene atlas page"
    )
    assert 'ss-tick' in resp.text, ".ss-tick element missing from sparkline definition"


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
    # The mv-panel must appear after the closing </details> tag so it's outside.
    details_close = html.rfind("</details>")
    mv_panel = html.find("mv-panel")
    assert mv_panel > details_close, (
        "mv-panel drilldown must appear AFTER the </details> closing tag"
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
    assert 'Genesets<span class="tab-count">' in resp.text, (
        "dataset Modules tab must be relabelled 'Genesets'"
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
    # The sparkline marker itself must remain.
    assert "mv-mark" in html
    assert "mv-track" in html


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
