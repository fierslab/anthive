"""Tests for lib/metadata.py — YAML frontmatter parsing, writing, and helpers."""

import pytest

from metadata import (
    MetadataError,
    ensure_in_list,
    ensure_in_mapping,
    get_default_embedding_name,
    get_obs_column_assignment,
    initialize_collection_metadata,
    initialize_file_metadata,
    parse_md_file,
    remove_from_lists,
    validate_annotation_field,
    write_md_file,
)


# ── parse_md_file ─────────────────────────────────────────────────────────────

class TestParseMdFile:

    def test_valid_preamble(self, tmp_path):
        md = tmp_path / "test.md"
        md.write_text("---\ntitle: Hello\nyear: 2025\n---\n\nBody text.\n")
        yaml_dict, body = parse_md_file(md)
        assert yaml_dict["title"] == "Hello"
        assert yaml_dict["year"] == 2025
        assert body == "Body text."

    def test_no_preamble(self, tmp_path):
        md = tmp_path / "test.md"
        md.write_text("Just plain markdown.\n")
        yaml_dict, body = parse_md_file(md)
        assert yaml_dict == {}
        assert body == "Just plain markdown.\n"

    def test_empty_preamble(self, tmp_path):
        """An empty preamble (---\\n---\\n) has no content between delimiters,
        so the regex doesn't match and the file is treated as plain markdown."""
        md = tmp_path / "test.md"
        md.write_text("---\n---\n\nBody here.\n")
        yaml_dict, body = parse_md_file(md)
        assert yaml_dict == {}
        # No match for empty preamble regex, returns full content
        assert "Body here." in body

    def test_nonexistent_file(self, tmp_path):
        yaml_dict, body = parse_md_file(tmp_path / "nope.md")
        assert yaml_dict == {}
        assert body == ""

    def test_malformed_preamble_no_closing(self, tmp_path):
        md = tmp_path / "test.md"
        md.write_text("---\ntitle: Test\nBody text\n")
        yaml_dict, body = parse_md_file(md)
        # No closing '---', so treated as no preamble
        assert yaml_dict == {}

    def test_nested_yaml(self, tmp_path):
        md = tmp_path / "test.md"
        md.write_text(
            "---\nannotation:\n  organism: human\n  sex: female\n---\n\nBody\n"
        )
        yaml_dict, body = parse_md_file(md)
        assert yaml_dict["annotation"]["organism"] == "human"
        assert yaml_dict["annotation"]["sex"] == "female"

    def test_list_in_yaml(self, tmp_path):
        md = tmp_path / "test.md"
        md.write_text("---\nitems:\n  - a\n  - b\n  - c\n---\n\nBody\n")
        yaml_dict, _ = parse_md_file(md)
        assert yaml_dict["items"] == ["a", "b", "c"]

    def test_unicode_content(self, tmp_path):
        md = tmp_path / "test.md"
        md.write_text("---\ntitle: Muller-Gartner\n---\n\nBody\n")
        yaml_dict, _ = parse_md_file(md)
        assert "Muller" in yaml_dict["title"]


# ── write_md_file ─────────────────────────────────────────────────────────────

class TestWriteMdFile:

    def test_roundtrip(self, tmp_path):
        md = tmp_path / "test.md"
        data = {"title": "Test", "year": 2025}
        body = "Hello world"
        write_md_file(md, data, body)
        parsed_data, parsed_body = parse_md_file(md)
        assert parsed_data["title"] == "Test"
        assert parsed_data["year"] == 2025
        assert parsed_body == "Hello world"

    def test_unicode_roundtrip(self, tmp_path):
        md = tmp_path / "test.md"
        data = {"title": "Zurcher Studie"}
        write_md_file(md, data, "Body")
        parsed_data, _ = parse_md_file(md)
        assert parsed_data["title"] == "Zurcher Studie"

    def test_adds_trailing_newline(self, tmp_path):
        md = tmp_path / "test.md"
        write_md_file(md, {"a": 1}, "No newline")
        content = md.read_text()
        assert content.endswith("\n")

    def test_body_with_newline_not_doubled(self, tmp_path):
        md = tmp_path / "test.md"
        write_md_file(md, {"a": 1}, "Has newline\n")
        content = md.read_text()
        assert not content.endswith("\n\n")


# ── validate_annotation_field ─────────────────────────────────────────────────

class TestValidateAnnotationField:

    @pytest.mark.parametrize("value", ["male", "female", "mixed"])
    def test_sex_valid(self, value):
        assert validate_annotation_field("sex", value) is None

    def test_sex_invalid(self):
        result = validate_annotation_field("sex", "unknown")
        assert result is not None
        assert "sex" in result

    def test_sex_case_insensitive(self):
        assert validate_annotation_field("sex", "Male") is None

    def test_organism_lowercase_enforced(self):
        result = validate_annotation_field("organism", "Human")
        assert result is not None
        assert "lowercase" in result

    def test_organism_lowercase_passes(self):
        assert validate_annotation_field("organism", "human") is None

    def test_unknown_field_no_validation(self):
        assert validate_annotation_field("custom_field", "anything") is None


# ── ensure_in_list ────────────────────────────────────────────────────────────

class TestEnsureInList:

    def test_add_to_empty(self):
        d = {}
        assert ensure_in_list(d, "items", "a") is True
        assert d["items"] == ["a"]

    def test_add_to_existing(self):
        d = {"items": ["a"]}
        assert ensure_in_list(d, "items", "b") is True
        assert d["items"] == ["a", "b"]

    def test_no_duplicate(self):
        d = {"items": ["a"]}
        assert ensure_in_list(d, "items", "a") is False
        assert d["items"] == ["a"]

    def test_coerces_non_list(self):
        d = {"items": "not a list"}
        ensure_in_list(d, "items", "a")
        assert d["items"] == ["a"]


# ── ensure_in_mapping ─────────────────────────────────────────────────────────

class TestEnsureInMapping:

    def test_add_to_empty(self):
        d = {}
        assert ensure_in_mapping(d, "layers", "X", "raw counts") is True
        assert d["layers"]["X"] == "raw counts"

    def test_no_overwrite(self):
        d = {"layers": {"X": "original"}}
        assert ensure_in_mapping(d, "layers", "X", "new") is False
        assert d["layers"]["X"] == "original"

    def test_coerces_non_dict(self):
        d = {"layers": "not a dict"}
        ensure_in_mapping(d, "layers", "X", "val")
        assert d["layers"]["X"] == "val"


# ── remove_from_lists ─────────────────────────────────────────────────────────

class TestRemoveFromLists:

    def test_removes_from_specified_lists(self):
        d = {"a": ["x", "y"], "b": ["x", "z"]}
        remove_from_lists(d, ["a", "b"], "x")
        assert "x" not in d["a"]
        assert "x" not in d["b"]

    def test_ignores_missing_keys(self):
        d = {"a": ["x"]}
        remove_from_lists(d, ["a", "missing"], "x")
        assert d["a"] == []

    def test_ignores_non_list(self):
        d = {"a": "not a list"}
        remove_from_lists(d, ["a"], "x")
        assert d["a"] == "not a list"


# ── get_obs_column_assignment ─────────────────────────────────────────────────

class TestGetObsColumnAssignment:

    def test_found_in_obsnum(self):
        d = {"obsnum_include": ["n_counts"]}
        assert get_obs_column_assignment(d, "n_counts") == "obsnum_include"

    def test_found_in_obscat(self):
        d = {"obscat_include": ["cell_type"]}
        assert get_obs_column_assignment(d, "cell_type") == "obscat_include"

    def test_found_in_exclude(self):
        d = {"obs_exclude": ["batch"]}
        assert get_obs_column_assignment(d, "batch") == "obs_exclude"

    def test_not_found(self):
        d = {"obsnum_include": ["a"], "obscat_include": ["b"]}
        assert get_obs_column_assignment(d, "c") is None


# ── get_default_embedding_name ────────────────────────────────────────────────

class TestGetDefaultEmbeddingName:

    def test_x_umap(self):
        assert get_default_embedding_name("X_umap") == "Umap"

    def test_x_pca(self):
        assert get_default_embedding_name("X_pca") == "Pca"

    def test_no_prefix(self):
        assert get_default_embedding_name("tsne") == "Tsne"


# ── initialize_file_metadata ──────────────────────────────────────────────────

class TestInitializeFileMetadata:

    def test_has_required_keys(self):
        meta = initialize_file_metadata("test_data", "1.0", "abc123")
        assert meta["title"] == "test_data"
        assert meta["version"] == "1.0"
        assert meta["sha256"] == "abc123"
        assert "annotation" in meta
        assert "obsnum_include" in meta
        assert "obscat_include" in meta
        assert isinstance(meta["annotation"], dict)

    def test_annotation_has_all_fields(self):
        meta = initialize_file_metadata("test", "1.0", "abc")
        ann = meta["annotation"]
        for field in ["organism", "tissue", "celltype", "disease", "sex", "doi"]:
            assert field in ann


# ── initialize_collection_metadata ────────────────────────────────────────────

class TestInitializeCollectionMetadata:

    def test_has_required_keys(self):
        meta = initialize_collection_metadata("TestGroup")
        assert meta["group"] == "TestGroup"
        assert "experiment" in meta
        assert "authors" in meta
        assert "annotation" in meta
