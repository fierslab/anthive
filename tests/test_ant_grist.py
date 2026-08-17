"""Tests for bin/meta/ant-grist.

Covers:
  - Config loading (env var, missing key)
  - GristClient with mocked HTTP
  - Diff builder (synthetic local vs remote)
  - New-row filter (append-only logic)
  - .md writer preservation (only changed field mutated)
  - .h5ad.mus writer preservation (only changed field mutated)
  - _to_grist_value / _from_grist_value round-trip for list fields
  - Graceful handling of: row in Grist with no local files;
    column in Grist unknown to ant-grist; multi-value fields

All Grist HTTP calls are intercepted via unittest.mock.patch on
requests.Session — no live network traffic.
"""
from __future__ import annotations

import importlib
import os
import sys
import types
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest
import yaml

# ── Repo root + lib on path ───────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = REPO_ROOT / 'lib'
sys.path.insert(0, str(LIB_DIR))

# ── Import the module under test ──────────────────────────────────────────────
# ant-grist has a hyphen so importlib.util.spec_from_file_location won't find
# a loader for it (the shebang line trips the spec detector). Read the source
# and exec it into a fresh module object instead.

def _import_ant_grist():
    script_path = REPO_ROOT / 'bin' / 'meta' / 'ant-grist'
    source = script_path.read_text()
    # Strip the uv-script shebang + PEP-723 header so we can exec it cleanly
    mod = types.ModuleType('ant_grist')
    mod.__file__ = str(script_path)
    mod.__spec__ = None
    # The script does sys.path.insert at module level using __file__ — that
    # will resolve correctly because we set mod.__file__ above.
    exec(compile(source, str(script_path), 'exec'), mod.__dict__)
    return mod


ant_grist = _import_ant_grist()


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_h5adstore(tmp_path, monkeypatch):
    """Stand up a minimal fake h5adstore with two study folders."""
    store = tmp_path / 'h5adstore'
    store.mkdir()

    # Study A — two datasets
    study_a = store / 'StudyAlpha2024'
    study_a.mkdir()
    (study_a / 'index.md').write_text(
        "---\n"
        "group: StudyAlpha2024\n"
        "group_id: SA24\n"
        "authors:\n"
        "  - Alice Smith\n"
        "year: 2024\n"
        "paper_title: A great microglia paper\n"
        "journal: Nature Neuroscience\n"
        "doi: 10.1234/test.2024\n"
        "accession: GSE111111\n"
        "publication_status: published\n"
        "---\n\n# StudyAlpha2024\n"
    )
    (study_a / 'Dataset1.md').write_text(
        "---\n"
        "id: D1\n"
        "title: My Dataset One\n"
        "---\n\n# Dataset1\n"
    )
    h5ad_1 = study_a / 'Dataset1.h5ad'
    h5ad_1.touch()
    (study_a / 'Dataset1.h5ad.mus').write_text(
        "access=public\n"
        "cell_type=microglial cell\n"
        "disease=Alzheimer disease\n"
        "organism=human\n"
        "source=internal\n"
        "suspension_type=nucleus\n"
        "technology=10x_chromium\n"
        "tissue=brain\n"
        "experimental_system=post_mortem_tissue\n"
    )

    (study_a / 'Dataset2.md').write_text(
        "---\n"
        "id: D2\n"
        "title: My Dataset Two\n"
        "---\n\n# Dataset2\n"
    )
    h5ad_2 = study_a / 'Dataset2.h5ad'
    h5ad_2.touch()
    (study_a / 'Dataset2.h5ad.mus').write_text(
        "access=internal\n"
        "cell_type=microglial cell\n"
        "disease=Alzheimer disease,Parkinson disease\n"
        "organism=human\n"
        "source=internal\n"
        "suspension_type=cell\n"
        "technology=10x_chromium\n"
        "tissue=brain,cortex\n"
        "experimental_system=post_mortem_tissue\n"
    )

    # Study B — one dataset (to test no-local-file case)
    study_b = store / 'StudyBeta2025'
    study_b.mkdir()
    (study_b / 'index.md').write_text(
        "---\n"
        "group: StudyBeta2025\n"
        "group_id: SB25\n"
        "authors:\n"
        "  - Bob Jones\n"
        "year: 2025\n"
        "---\n\n# StudyBeta2025\n"
    )
    # No data .md — simulates a group with only an index

    # Patch the module's _H5ADSTORE_ROOT to point here
    monkeypatch.setattr(ant_grist, '_H5ADSTORE_ROOT', store)
    return store


@pytest.fixture
def fake_grist_records():
    """Grist rows that match the fake h5adstore above, with one differing field."""
    return [
        {
            'id': 1,
            'fields': {
                'db_id': 'SA24/D1',
                # Grist says 'cortex'; local says 'brain'  → diff expected
                'tissue': 'cortex',
                'cell_type': 'microglial cell',
                'disease': 'Alzheimer disease',
                'organism': 'human',
                'access': 'public',
                'source': 'internal',
                'suspension_type': 'nucleus',
                'technology': '10x_chromium',
                'experimental_system': 'post_mortem_tissue',
                'is_primary': '',
                'derived_from': '',
                'condition': '',
                'sex': '',
                'age': '',
                'contributor': '',
                'title': 'My Dataset One',
                'doi': '10.1234/test.2024',
                'journal': 'Nature Neuroscience',
                'paper_title': 'A great microglia paper',
                'year': '2024',
                'accession': 'GSE111111',
                'publication_status': 'published',
                'first_author': 'Alice Smith',
            },
        },
        {
            'id': 2,
            'fields': {
                'db_id': 'SA24/D2',
                'tissue': 'brain | cortex',  # multi-value, matches local
                'cell_type': 'microglial cell',
                'disease': 'Alzheimer disease | Parkinson disease',
                'organism': 'human',
                'access': 'internal',
                'source': 'internal',
                'suspension_type': 'cell',
                'technology': '10x_chromium',
                'experimental_system': 'post_mortem_tissue',
                'is_primary': '',
                'derived_from': '',
                'condition': '',
                'sex': '',
                'age': '',
                'contributor': '',
                'title': 'My Dataset Two',
                'doi': '10.1234/test.2024',
                'journal': 'Nature Neuroscience',
                'paper_title': 'A great microglia paper',
                'year': '2024',
                'accession': 'GSE111111',
                'publication_status': 'published',
                'first_author': 'Alice Smith',
            },
        },
    ]


# ── Config and env-var tests ──────────────────────────────────────────────────

class TestConfigLoading:

    def test_missing_api_key_exits(self, monkeypatch, tmp_path):
        monkeypatch.delenv('GRIST_API_KEY', raising=False)
        with pytest.raises(SystemExit) as exc_info:
            ant_grist.require_api_key()
        assert exc_info.value.code == 1

    def test_api_key_read_from_env(self, monkeypatch):
        monkeypatch.setenv('GRIST_API_KEY', 'test-secret-key')
        key = ant_grist.require_api_key()
        assert key == 'test-secret-key'

    def test_missing_grist_section_exits(self, tmp_path, monkeypatch):
        cfg = {'s3': {'endpoint': 'test'}}
        monkeypatch.setattr(ant_grist, '_ANTHIVE_YAML', tmp_path / 'anthive.yaml')
        (tmp_path / 'anthive.yaml').write_text(yaml.dump(cfg))
        loaded = ant_grist.load_config()
        with pytest.raises(SystemExit) as exc_info:
            ant_grist.load_grist_config(loaded)
        assert exc_info.value.code == 1

    def test_grist_config_validates_required_keys(self, tmp_path, monkeypatch):
        cfg = {'grist': {'url': 'https://grist.example.com', 'doc_id': 'ABCD'}}
        monkeypatch.setattr(ant_grist, '_ANTHIVE_YAML', tmp_path / 'anthive.yaml')
        (tmp_path / 'anthive.yaml').write_text(yaml.dump(cfg))
        loaded = ant_grist.load_config()
        with pytest.raises(SystemExit) as exc_info:
            ant_grist.load_grist_config(loaded)
        assert exc_info.value.code == 1  # missing 'table' key

    def test_valid_grist_config_passes(self, tmp_path, monkeypatch):
        cfg = {'grist': {'url': 'https://g.example.com', 'doc_id': 'X', 'table': 'T'}}
        monkeypatch.setattr(ant_grist, '_ANTHIVE_YAML', tmp_path / 'anthive.yaml')
        (tmp_path / 'anthive.yaml').write_text(yaml.dump(cfg))
        loaded = ant_grist.load_config()
        grist_cfg = ant_grist.load_grist_config(loaded)
        assert grist_cfg['doc_id'] == 'X'


# ── GristClient with mocked HTTP ──────────────────────────────────────────────

class TestGristClient:

    def _make_client(self) -> ant_grist.GristClient:
        return ant_grist.GristClient(
            base_url='https://grist.example.com',
            doc_id='DOC123',
            table_id='Sheet1',
            api_key='fake-key',
        )

    def test_fetch_all_records_returns_list(self):
        client = self._make_client()
        fake_response = MagicMock()
        fake_response.json.return_value = {
            'records': [
                {'id': 1, 'fields': {'db_id': 'A/B', 'tissue': 'brain'}},
            ]
        }
        fake_response.raise_for_status = MagicMock()

        with patch.object(client._session, 'get', return_value=fake_response) as mock_get:
            records = client.fetch_all_records()

        mock_get.assert_called_once()
        assert len(records) == 1
        assert records[0]['fields']['db_id'] == 'A/B'

    def test_add_records_posts_correct_shape(self):
        client = self._make_client()
        fake_response = MagicMock()
        fake_response.json.return_value = {'records': [{'id': 42}]}
        fake_response.raise_for_status = MagicMock()

        field_dicts = [{'db_id': 'X/Y', 'tissue': 'cortex'}]
        with patch.object(client._session, 'post', return_value=fake_response) as mock_post:
            result = client.add_records(field_dicts)

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        body = call_kwargs[1]['json']
        assert body == {'records': [{'fields': {'db_id': 'X/Y', 'tissue': 'cortex'}}]}
        assert result == {'records': [{'id': 42}]}

    def test_fetch_raises_on_http_error(self):
        client = self._make_client()
        fake_response = MagicMock()
        fake_response.raise_for_status.side_effect = Exception('403 Forbidden')

        with patch.object(client._session, 'get', return_value=fake_response):
            with pytest.raises(Exception, match='403'):
                client.fetch_all_records()


# ── Value conversion helpers ──────────────────────────────────────────────────

class TestValueConversion:

    def test_to_grist_none_returns_empty_string(self):
        assert ant_grist._to_grist_value(None) == ''

    def test_to_grist_list_pipe_joins(self):
        assert ant_grist._to_grist_value(['brain', 'cortex']) == 'brain | cortex'

    def test_to_grist_bool_true(self):
        assert ant_grist._to_grist_value(True) == 'yes'

    def test_to_grist_bool_false(self):
        assert ant_grist._to_grist_value(False) == 'no'

    def test_to_grist_scalar_string(self):
        assert ant_grist._to_grist_value('human') == 'human'

    def test_from_grist_none_returns_none(self):
        assert ant_grist._from_grist_value('tissue', None) is None

    def test_from_grist_empty_string_returns_none(self):
        assert ant_grist._from_grist_value('tissue', '') is None

    def test_from_grist_list_field_with_pipe_splits(self):
        result = ant_grist._from_grist_value('tissue', 'brain | cortex')
        assert result == ['brain', 'cortex']

    def test_from_grist_list_field_single_value_stays_scalar(self):
        # Single value in a list field — no pipe → stays string
        result = ant_grist._from_grist_value('tissue', 'brain')
        assert result == 'brain'

    def test_from_grist_scalar_field(self):
        result = ant_grist._from_grist_value('technology', '10x_chromium')
        assert result == '10x_chromium'

    def test_round_trip_multi_value_disease(self):
        """List → grist string → back to list must be lossless."""
        original = ['Alzheimer disease', 'Parkinson disease']
        grist_str = ant_grist._to_grist_value(original)
        recovered = ant_grist._from_grist_value('disease', grist_str)
        assert recovered == original


# ── Diff builder ──────────────────────────────────────────────────────────────

class TestComputeDiff:

    def test_diff_detects_tissue_mismatch(self, tmp_h5adstore, fake_grist_records):
        # Grist says tissue='cortex'; local has tissue='brain'
        diffs = ant_grist.compute_diff(fake_grist_records)
        tissue_diffs = [(db_id, field, lv, gv) for db_id, field, lv, gv in diffs
                        if field == 'tissue' and db_id == 'SA24/D1']
        assert len(tissue_diffs) == 1
        _db_id, _field, local_val, grist_val = tissue_diffs[0]
        assert local_val == 'brain'
        assert grist_val == 'cortex'

    def test_diff_no_difference_for_matching_row(self, tmp_h5adstore, fake_grist_records):
        # SA24/D2's multi-value disease and tissue match Grist exactly
        diffs = ant_grist.compute_diff(fake_grist_records)
        d2_diffs = [(db_id, field, lv, gv) for db_id, field, lv, gv in diffs
                    if db_id == 'SA24/D2' and field in ('tissue', 'disease')]
        assert d2_diffs == [], f"Unexpected diffs for SA24/D2: {d2_diffs}"

    def test_diff_with_only_filter(self, tmp_h5adstore, fake_grist_records):
        # Only SA24/D1 should appear
        diffs = ant_grist.compute_diff(fake_grist_records, only_db_id='SA24/D1')
        db_ids = {d[0] for d in diffs}
        assert 'SA24/D2' not in db_ids

    def test_diff_skips_row_with_no_local_files(self, tmp_h5adstore, capsys):
        # A Grist row whose db_id has no matching local files
        orphan_records = [
            {'id': 99, 'fields': {'db_id': 'XX99/ZZZ', 'tissue': 'liver'}}
        ]
        diffs = ant_grist.compute_diff(orphan_records)
        assert diffs == []
        captured = capsys.readouterr()
        assert 'XX99/ZZZ' in captured.err
        assert 'no local files' in captured.err

    def test_diff_skips_unknown_grist_columns(self, tmp_h5adstore, fake_grist_records):
        # Add a column that ant-grist does not know about (user-added in Grist)
        fake_grist_records[0]['fields']['user_notes'] = 'some custom annotation'
        # Should not raise and should not include 'user_notes' in diffs
        diffs = ant_grist.compute_diff([fake_grist_records[0]])
        diff_fields = {d[1] for d in diffs}
        assert 'user_notes' not in diff_fields

    def test_diff_skips_readonly_grist_cols(self, tmp_h5adstore, fake_grist_records):
        # n_cells, has_braak etc should never appear in diffs
        fake_grist_records[0]['fields']['n_cells'] = 99999
        fake_grist_records[0]['fields']['has_braak'] = 'yes'
        diffs = ant_grist.compute_diff([fake_grist_records[0]])
        readonly_diffs = [d for d in diffs if d[1] in ant_grist._READONLY_GRIST_COLS]
        assert readonly_diffs == []


# ── Writer preservation tests ─────────────────────────────────────────────────

class TestMdWriterPreservation:
    """Verify that applying a one-field change doesn't clobber other fields."""

    def test_apply_changes_only_target_field_in_md(self, tmp_path):
        from metadata import parse_md_file, write_md_file

        md_path = tmp_path / 'test.md'
        md_path.write_text(
            "---\n"
            "title: Original Title\n"
            "id: T1\n"
            "sha256: abc123\n"
            "year: 2024\n"
            "---\n\nBody text.\n"
        )

        # Simulate applying a title change
        meta, body = parse_md_file(md_path)
        assert meta['title'] == 'Original Title'
        meta['title'] = 'Updated Title'
        write_md_file(md_path, meta, body)

        # Re-read and verify
        meta2, body2 = parse_md_file(md_path)
        assert meta2['title'] == 'Updated Title'
        assert meta2['id'] == 'T1'       # untouched
        assert meta2['sha256'] == 'abc123'  # untouched
        assert meta2['year'] == 2024      # untouched
        assert body2 == 'Body text.'

    def test_apply_changes_only_target_field_in_mus(self, tmp_path):
        from mus import parse_mus_file, write_mus_file

        mus_path = tmp_path / 'data.h5ad.mus'
        mus_path.write_text(
            "access=public\n"
            "cell_type=microglial cell\n"
            "disease=Alzheimer disease\n"
            "organism=human\n"
            "tissue=brain\n"
        )

        ann = parse_mus_file(mus_path)
        assert ann['tissue'] == 'brain'
        ann['tissue'] = 'cortex'
        write_mus_file(mus_path, ann)

        ann2 = parse_mus_file(mus_path)
        assert ann2['tissue'] == 'cortex'
        assert ann2['cell_type'] == 'microglial cell'  # untouched
        assert ann2['organism'] == 'human'             # untouched
        assert ann2['access'] == 'public'               # untouched
        assert ann2['disease'] == 'Alzheimer disease'  # untouched

    def test_mus_write_is_atomic(self, tmp_path):
        """write_mus_file does tmp-then-rename; file content is always complete."""
        from mus import write_mus_file, parse_mus_file

        mus_path = tmp_path / 'data.h5ad.mus'
        ann = {'organism': 'human', 'tissue': ['brain', 'cortex'], 'access': 'public'}
        write_mus_file(mus_path, ann)

        recovered = parse_mus_file(mus_path)
        assert recovered['organism'] == 'human'
        assert recovered['tissue'] == ['brain', 'cortex']


# ── New-row filter tests ──────────────────────────────────────────────────────

class TestNewRowFilter:

    def _make_catalog_entry(self, db_id: str, n_cells: int) -> Dict:
        return {'id': db_id, 'n_cells': n_cells}

    def test_existing_db_id_skipped(self, tmp_h5adstore):
        existing = {'SA24/D1', 'SA24/D2'}
        catalog = [
            self._make_catalog_entry('SA24/D1', 10000),
            self._make_catalog_entry('SA24/D2', 5000),
        ]
        rows = ant_grist.build_new_rows(existing, catalog)
        assert rows == []

    def test_new_db_id_included(self, tmp_h5adstore):
        # D1 in catalog, not in Grist
        existing: set = set()
        catalog = [self._make_catalog_entry('SA24/D1', 15000)]
        rows = ant_grist.build_new_rows(existing, catalog)
        db_ids = [r['db_id'] for r in rows]
        assert 'SA24/D1' in db_ids

    def test_xenograft_below_threshold_excluded(self, tmp_h5adstore):
        # Patch D1's mus to be xenograft with only 5000 cells
        mus_path = tmp_h5adstore / 'StudyAlpha2024' / 'Dataset1.h5ad.mus'
        mus_path.write_text(
            "access=public\n"
            "cell_type=microglial cell\n"
            "disease=Alzheimer disease\n"
            "organism=human\n"
            "source=internal\n"
            "suspension_type=nucleus\n"
            "technology=10x_chromium\n"
            "tissue=brain\n"
            "experimental_system=xenograft\n"
        )
        existing: set = set()
        catalog = [self._make_catalog_entry('SA24/D1', 5000)]
        rows = ant_grist.build_new_rows(existing, catalog)
        assert all(r['db_id'] != 'SA24/D1' for r in rows)

    def test_xenograft_above_threshold_included(self, tmp_h5adstore):
        mus_path = tmp_h5adstore / 'StudyAlpha2024' / 'Dataset1.h5ad.mus'
        mus_path.write_text(
            "access=public\n"
            "cell_type=microglial cell\n"
            "organism=human\n"
            "source=internal\n"
            "suspension_type=nucleus\n"
            "technology=10x_chromium\n"
            "tissue=brain\n"
            "experimental_system=xenograft\n"
        )
        existing: set = set()
        catalog = [self._make_catalog_entry('SA24/D1', 25000)]
        rows = ant_grist.build_new_rows(existing, catalog)
        assert any(r['db_id'] == 'SA24/D1' for r in rows)

    def test_non_microglia_excluded(self, tmp_h5adstore):
        mus_path = tmp_h5adstore / 'StudyAlpha2024' / 'Dataset1.h5ad.mus'
        mus_path.write_text(
            "access=public\n"
            "cell_type=neuron\n"  # not microglia
            "organism=human\n"
            "source=internal\n"
            "suspension_type=nucleus\n"
            "technology=10x_chromium\n"
            "tissue=brain\n"
            "experimental_system=post_mortem_tissue\n"
        )
        existing: set = set()
        catalog = [self._make_catalog_entry('SA24/D1', 50000)]
        rows = ant_grist.build_new_rows(existing, catalog)
        assert all(r['db_id'] != 'SA24/D1' for r in rows)

    def test_non_human_excluded(self, tmp_h5adstore):
        mus_path = tmp_h5adstore / 'StudyAlpha2024' / 'Dataset1.h5ad.mus'
        mus_path.write_text(
            "access=public\n"
            "cell_type=microglial cell\n"
            "organism=mouse\n"  # not human
            "source=internal\n"
            "suspension_type=nucleus\n"
            "technology=10x_chromium\n"
            "tissue=brain\n"
            "experimental_system=post_mortem_tissue\n"
        )
        existing: set = set()
        catalog = [self._make_catalog_entry('SA24/D1', 50000)]
        rows = ant_grist.build_new_rows(existing, catalog)
        assert all(r['db_id'] != 'SA24/D1' for r in rows)

    def test_excluded_system_in_vitro_2d_rejected(self, tmp_h5adstore):
        mus_path = tmp_h5adstore / 'StudyAlpha2024' / 'Dataset1.h5ad.mus'
        mus_path.write_text(
            "access=public\n"
            "cell_type=microglial cell\n"
            "organism=human\n"
            "source=internal\n"
            "suspension_type=nucleus\n"
            "technology=10x_chromium\n"
            "tissue=brain\n"
            "experimental_system=in_vitro_2d\n"
        )
        existing: set = set()
        catalog = [self._make_catalog_entry('SA24/D1', 50000)]
        rows = ant_grist.build_new_rows(existing, catalog)
        assert all(r['db_id'] != 'SA24/D1' for r in rows)

    def test_row_fields_match_expected_columns(self, tmp_h5adstore):
        """Each new row must have the full column set from v8."""
        expected_cols = {
            'db_id', 'group', 'file', 'url', 'n_cells', 'n_individuals',
            'cells_per_ind_avg', 'tissue', 'cell_type', 'disease', 'condition',
            'experimental_system', 'suspension_type', 'technology', 'sex', 'age',
            'has_braak', 'first_author', 'paper_title', 'year', 'journal', 'doi',
            'accession', 'cohort_bank', 'publication_status', 'is_primary',
            'derived_from', 'source', 'access', 'contributor', 'n_caveats',
            'max_caveat_level', 'caveats_summary',
        }
        existing: set = set()
        catalog = [self._make_catalog_entry('SA24/D1', 15000)]
        rows = ant_grist.build_new_rows(existing, catalog)
        if rows:
            assert set(rows[0].keys()) == expected_cols

    def test_myeloid_cell_passes_filter(self, tmp_h5adstore):
        """'myeloid cell' is an alternative cell type that should pass."""
        mus_path = tmp_h5adstore / 'StudyAlpha2024' / 'Dataset1.h5ad.mus'
        mus_path.write_text(
            "access=public\n"
            "cell_type=myeloid cell\n"
            "organism=human\n"
            "source=internal\n"
            "suspension_type=nucleus\n"
            "technology=10x_chromium\n"
            "tissue=brain\n"
            "experimental_system=post_mortem_tissue\n"
        )
        existing: set = set()
        catalog = [self._make_catalog_entry('SA24/D1', 15000)]
        rows = ant_grist.build_new_rows(existing, catalog)
        assert any(r['db_id'] == 'SA24/D1' for r in rows)


# ── _find_dataset_paths tests ─────────────────────────────────────────────────

class TestFindDatasetPaths:

    def test_resolves_valid_db_id(self, tmp_h5adstore):
        result = ant_grist._find_dataset_paths('SA24/D1')
        assert result is not None
        index_md, data_md, mus_path = result
        assert index_md.name == 'index.md'
        assert data_md.name == 'Dataset1.md'
        assert mus_path.name == 'Dataset1.h5ad.mus'

    def test_returns_none_for_unknown_db_id(self, tmp_h5adstore):
        result = ant_grist._find_dataset_paths('XX99/NOPE')
        assert result is None

    def test_returns_none_for_malformed_db_id(self, tmp_h5adstore):
        result = ant_grist._find_dataset_paths('nodash')
        assert result is None


# ── Apply pull integration test ───────────────────────────────────────────────

class TestApplyPull:

    def test_apply_writes_grist_value_to_mus(self, tmp_h5adstore):
        """apply_pull must write the Grist value to the local .h5ad.mus."""
        from mus import parse_mus_file

        diffs = [('SA24/D1', 'tissue', 'brain', 'cortex')]

        with patch.object(ant_grist, '_refresh_view', return_value=None):
            ant_grist.apply_pull(diffs)

        mus_path = tmp_h5adstore / 'StudyAlpha2024' / 'Dataset1.h5ad.mus'
        ann = parse_mus_file(mus_path)
        assert ann['tissue'] == 'cortex'
        # Other fields must be untouched
        assert ann['organism'] == 'human'
        assert ann['access'] == 'public'

    def test_apply_writes_grist_value_to_index_md(self, tmp_h5adstore):
        """apply_pull must write journal change to index.md."""
        from metadata import parse_md_file

        diffs = [('SA24/D1', 'journal', 'Nature Neuroscience', 'Cell')]

        with patch.object(ant_grist, '_refresh_view', return_value=None):
            ant_grist.apply_pull(diffs)

        index_md = tmp_h5adstore / 'StudyAlpha2024' / 'index.md'
        meta, _ = parse_md_file(index_md)
        assert meta['journal'] == 'Cell'
        # Other fields untouched
        assert meta['group_id'] == 'SA24'
        assert meta['doi'] == '10.1234/test.2024'

    def test_apply_calls_refresh_view(self, tmp_h5adstore):
        diffs = [('SA24/D1', 'tissue', 'brain', 'cortex')]
        with patch.object(ant_grist, '_refresh_view') as mock_refresh:
            ant_grist.apply_pull(diffs)
        mock_refresh.assert_called_once_with('SA24/D1')

    def test_apply_skips_nonexistent_db_id(self, tmp_h5adstore, capsys):
        diffs = [('XX99/NOPE', 'tissue', 'brain', 'cortex')]
        ant_grist.apply_pull(diffs)
        captured = capsys.readouterr()
        assert 'XX99/NOPE' in captured.err
