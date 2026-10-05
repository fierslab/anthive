"""Config helpers for ingestion and the donor-count rules shared with the facet tests."""
import importlib.machinery
import importlib.util
from pathlib import Path

import config
import facet_rules
from ingest_check import thresholds

REPO = Path(__file__).parent.parent


def test_ingest_dirs_follow_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv('ANTHIVE_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.delenv('ANTHIVE_INGEST_STATE_DIR', raising=False)
    monkeypatch.delenv('ANTHIVE_INGEST_INDEX_DIR', raising=False)
    assert config.get_ingest_state_dir({'state_dir': 'x'}) == tmp_path / 'state' / 'ingest'
    assert config.get_ingest_index_dir({}) == tmp_path / 'state' / 'ingest' / 'index'
    assert (tmp_path / 'state' / 'ingest' / 'index').is_dir()


def test_ingest_dirs_can_be_moved_by_env_and_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv('ANTHIVE_INGEST_STATE_DIR', str(tmp_path / 'elsewhere'))
    assert config.get_ingest_state_dir({}) == tmp_path / 'elsewhere'
    assert config.get_ingest_index_dir({}) == tmp_path / 'elsewhere' / 'index'
    monkeypatch.delenv('ANTHIVE_INGEST_STATE_DIR')
    assert config.get_ingest_index_dir({'ingest_index_dir': str(tmp_path / 'idx')}) == tmp_path / 'idx'
    monkeypatch.setenv('ANTHIVE_INGEST_INDEX_DIR', str(tmp_path / 'env_idx'))
    assert config.get_ingest_index_dir({'ingest_index_dir': str(tmp_path / 'idx')}) == tmp_path / 'env_idx'


def test_create_false_does_not_make_directories(tmp_path, monkeypatch):
    monkeypatch.setenv('ANTHIVE_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.delenv('ANTHIVE_INGEST_STATE_DIR', raising=False)
    monkeypatch.delenv('ANTHIVE_INGEST_INDEX_DIR', raising=False)
    assert config.get_ingest_index_dir({}, create=False) == tmp_path / 'state' / 'ingest' / 'index'
    assert not (tmp_path / 'state').exists()


def test_scratch_dir_is_under_tmp_dir(tmp_path, monkeypatch):
    monkeypatch.setenv('ANTHIVE_TMP_DIR', str(tmp_path / 'tmp'))
    assert config.get_ingest_scratch_dir({}) == tmp_path / 'tmp' / 'ingest_check'
    assert (tmp_path / 'tmp' / 'ingest_check').is_dir()


def test_dataset_ingest_settings_defaults_and_overrides():
    defaults = config.get_dataset_ingest_settings({})
    assert defaults == {'max_upload_gb': 50.0, 'auto_publish_public': False, 'default_access': 'internal'}
    custom = config.get_dataset_ingest_settings({'datasets_upload_max_gb': '12.5', 'datasets_auto_publish_public': True,
                                                 'datasets_default_access': 'public'})
    assert custom == {'max_upload_gb': 12.5, 'auto_publish_public': True, 'default_access': 'public'}
    assert config.get_dataset_ingest_settings({'datasets_upload_max_gb': 'lots'})['max_upload_gb'] == 50.0


def test_default_yaml_carries_the_documented_keys():
    defaults = config.load_default_config()
    for key, value in (('datasets_upload_max_gb', 50), ('datasets_auto_publish_public', False),
                       ('datasets_default_access', 'internal'), ('ingest_state_dir', ''), ('ingest_index_dir', '')):
        assert defaults[key] == value
    documented = (REPO / 'doc' / 'CONFIG.md').read_text()
    for key in ('datasets_upload_max_gb', 'datasets_auto_publish_public', 'datasets_default_access',
                'ingest_state_dir', 'ingest_index_dir'):
        assert f'#### `{key}`' in documented


def test_facet_test_uses_the_shared_rules():
    loader = importlib.machinery.SourceFileLoader('ant_facet_test_rules', str(REPO / 'bin' / 'annot' / 'ant-facet-test'))
    spec = importlib.util.spec_from_loader('ant_facet_test_rules', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    for name in facet_rules.__all__:
        assert getattr(module, name) == getattr(facet_rules, name), name


def test_ingest_thresholds_reuse_the_facet_rules():
    assert thresholds.MIN_UNITS_TOTAL == facet_rules.MIN_DONORS_DATASET == 6
    assert thresholds.MIN_UNITS_PER_LEVEL == facet_rules.MIN_DONORS_PER_LEVEL == 3
