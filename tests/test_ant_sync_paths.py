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


def test_the_artefact_store_is_never_pushed_or_pulled():
    """Review S2 (2.47): a push would re-upload artefact objects without x-amz-meta-sha256 and could bring back a
    purged version from a stale checkout; the watcher alone writes them (A4 adds pull rules)."""
    ant_sync = _load()
    for rel in ('Sun2026/static/artefacts/SLM/figure/volcano/artefact.md',
                'Sun2026/static/artefacts/SLM/figure/volcano/v1/manifest.json',
                'Sun2026/static/artefacts/SLM/figure/volcano/v1/files/volcano.png',
                'Sun2026/static/artefacts/_group/table/atlas/v2/files/panels/a.tsv'):
        assert not ant_sync._is_syncable(rel), rel
        assert not ant_sync._is_syncable(rel, include_static=False), rel
        assert not ant_sync._is_priority_global(rel), rel
    assert ant_sync._is_syncable('Sun2026/static/paper.pdf')
    assert ant_sync._is_syncable('Sun2026/static/artefacts.pdf')
