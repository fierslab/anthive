"""ant-sync path filter: the facet-test results are synced, logs and scripts are not."""
import importlib.machinery
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / 'bin' / 'ops' / 'ant-sync'


def _load():
    loader = importlib.machinery.SourceFileLoader('ant_sync_script', str(SCRIPT))
    spec = importlib.util.spec_from_loader('ant_sync_script', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_facet_tests_are_synced_but_not_logs_or_scripts():
    ant_sync = _load()
    syncable = ant_sync._is_syncable
    assert syncable('_facet_tests/Gabitto2024/Seattle_MTG_MG.facet_tests.parquet')
    assert syncable('_facet_tests/Gabitto2024/Seattle_MTG_MG.facet_run_manifest.json')
    assert syncable('_facet_tests/cohort_selection.DRAFT.tsv')
    assert syncable('_facet_tests/_pooled/madrc_myeloid.facet_tests.parquet')
    assert not syncable('_facet_tests/run_2026-10-07.log')
    assert not syncable('_facet_tests/summary_2026-10-05e_rerun/donor_counts.py')
    assert not syncable('_facet_tests_backup_2026-10-05f/Gabitto2024/x.facet_tests.parquet')
    assert not syncable('_synapse_raw/ROSMAP_clinical.csv')
