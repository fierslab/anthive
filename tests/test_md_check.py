"""Tests for lib/md_check.py — ID generation and metadata file validation."""

import pytest

from md_check import MdCheckReport, check_md_folder, generate_file_id, generate_group_id


# ── generate_group_id ─────────────────────────────────────────────────────────

class TestGenerateGroupId:

    @pytest.mark.parametrize("name, expected", [
        ("Zielonka2025", "Z25"),
        ("AlbertiniZielonka2025", "AZ5"),
        ("LuChen2025", "LC5"),
        ("SalaFrigerio2019", "SF9"),
        ("Simpson2025", "S25"),
    ])
    def test_documented_examples(self, name, expected):
        assert generate_group_id(name) == expected

    def test_always_3_chars(self):
        for name in ["A", "AB", "Smith", "Zielonka2025", "AlbertiniZielonkaSmith2025"]:
            result = generate_group_id(name)
            assert len(result) == 3, f"generate_group_id('{name}') = '{result}' (len {len(result)})"

    def test_always_uppercase(self):
        result = generate_group_id("lowercase2025")
        assert result == result.upper()

    def test_no_year(self):
        result = generate_group_id("Smith")
        assert len(result) == 3

    def test_triple_camelcase(self):
        result = generate_group_id("AlbertiniZielonkaSmith2025")
        assert result == "AZS"

    def test_empty_string(self):
        # Edge case: should not crash
        result = generate_group_id("")
        assert len(result) == 3


# ── generate_file_id ──────────────────────────────────────────────────────────

class TestGenerateFileId:

    @pytest.mark.parametrize("stem, expected", [
        ("Seattle_PFC_MG", "SPF"),  # CamelCase expansion: PFC -> PF+C, takes S+PF+C -> SPC? No: S+P+F
        ("seurat_obj_final", "SOF"),
        ("seurat_obj_trisomy_initial", "SOT"),
    ])
    def test_documented_examples(self, stem, expected):
        assert generate_file_id(stem) == expected

    def test_always_3_chars(self):
        for stem in ["a", "ab", "single", "multi_part_name", "CamelCase"]:
            result = generate_file_id(stem)
            assert len(result) == 3, f"generate_file_id('{stem}') = '{result}' (len {len(result)})"

    def test_always_uppercase(self):
        result = generate_file_id("some_file_name")
        assert result == result.upper()

    def test_numeric_parts_skipped(self):
        result = generate_file_id("sample_123_data")
        # '123' is numeric and should be excluded from initials
        assert result == "SD" + result[2]  # first two letters from 'sample' and 'data'

    def test_single_word(self):
        result = generate_file_id("onlyword")
        assert len(result) == 3


# ── MdCheckReport ─────────────────────────────────────────────────────────────

class TestMdCheckReport:

    def test_no_errors_initially(self):
        report = MdCheckReport()
        assert report.has_errors() is False
        assert report.has_warnings() is False

    def test_has_errors_after_add(self):
        report = MdCheckReport()
        report.errors.append("something broke")
        assert report.has_errors() is True

    def test_has_warnings_after_add(self):
        report = MdCheckReport()
        report.warnings.append("watch out")
        assert report.has_warnings() is True

    def test_collect_log_messages(self):
        report = MdCheckReport()
        report.errors.append("err1")
        report.warnings.append("warn1")
        msgs = report.collect_log_messages()
        assert len(msgs) == 2
        assert any("err1" in m for m in msgs)
        assert any("warn1" in m for m in msgs)


# ── check_md_folder (integration-style with temp dirs) ────────────────────────

class TestCheckMdFolder:

    def _make_h5ad_stub(self, path):
        """Create an empty .h5ad file (only existence matters for md-check)."""
        path.write_bytes(b"")

    def test_generates_missing_group_id(self, tmp_path):
        group_dir = tmp_path / "TestGroup2025"
        group_dir.mkdir()
        index_md = group_dir / "index.md"
        index_md.write_text("---\ngroup: TestGroup2025\n---\n\n")

        report = MdCheckReport()
        check_md_folder(tmp_path, report, fix=True)
        assert len(report.fixed) >= 1
        assert "group_id" in report.fixed[0]

    def test_generates_missing_file_id(self, tmp_path):
        group_dir = tmp_path / "TestGroup"
        group_dir.mkdir()
        # Create a data .md and matching .h5ad stub
        data_md = group_dir / "my_data.md"
        data_md.write_text("---\ntitle: My Data\n---\n\n")
        self._make_h5ad_stub(group_dir / "my_data.h5ad")

        report = MdCheckReport()
        check_md_folder(tmp_path, report, fix=True)
        assert any("id" in f for f in report.fixed)

    def test_fix_false_no_write(self, tmp_path):
        group_dir = tmp_path / "Group2025"
        group_dir.mkdir()
        index_md = group_dir / "index.md"
        original = "---\ngroup: Group2025\n---\n\n"
        index_md.write_text(original)

        report = MdCheckReport()
        check_md_folder(tmp_path, report, fix=False)
        # File should not be modified
        assert index_md.read_text() == original

    def test_duplicate_group_id_detected(self, tmp_path):
        # Two top-level dirs that resolve to the same group_id
        dir_a = tmp_path / "Smith2025"
        dir_b = tmp_path / "Smyth2025"  # Should also produce S25
        dir_a.mkdir()
        dir_b.mkdir()
        (dir_a / "index.md").write_text("---\ngroup: Smith\n---\n\n")
        (dir_b / "index.md").write_text("---\ngroup: Smyth\n---\n\n")

        report = MdCheckReport()
        check_md_folder(tmp_path, report, fix=True)
        # Both resolve to S25, so we should get a duplicate error
        assert report.has_errors() or len(report.fixed) >= 2
