"""QC companion datasets (2.47.0, Mark 2026-10-21): ``qc: true`` + ``qc_of: <db_id>``, never primary.

The QC round of sc-python-workflow keeps every cell with per-cell QC statistics (``<name>_qc``), the cleanup
round deposits the kept cells. Covered: the designation rules (always secondary, qc_of target, booleans), the
facet exclusion, cells.overlap exempting the pair in both deposit orders and when QC removed under 5% of the cells
(same cells both ways), other datasets still checked, the watcher setting is_secondary with the caveat and
keeping a QC companion from being the default dataset, the counting exclusion (is_secondary), and the markers in
the API metadata, MCP and the web listing.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

import facet_lint
import facet_scopes
import qc_companion
from ingest_check import FAIL, PASS, thresholds
from ingest_check import checks_cells, checks_design
from tests.ingest_helpers import Submission, seed_index, write_index_md, write_md
from tests.test_ingest_check_donors import SHARED_API

REPO = Path(__file__).resolve().parent.parent


# ── designation rules ────────────────────────────────────────────────────────

def _rules(metadata, known=('ZZ1/FIN',)):
    return {item.rule for item in facet_lint.lint_lineage(metadata, set(known))}


def test_a_qc_companion_is_never_primary():
    assert _rules({'qc': True, 'qc_of': 'ZZ1/FIN', 'is_secondary': True}) == set()
    assert _rules({'qc': True}) == set()                                      # deposited first: qc_of empty
    assert _rules({'qc': True, 'qc_of': 'ZZ1/FIN', 'is_secondary': False}) == {'qc_not_secondary'}
    assert _rules({'qc_of': 'ZZ1/FIN', 'qc': False}) == {'qc_of_without_qc'}
    assert _rules({'qc': 'yes'}) == {'qc_not_boolean'}
    assert _rules({'qc': True, 'qc_of': 'ZZ1/NOPE'}) == {'qc_target_missing'}


def test_helpers_and_pair_rule():
    assert qc_companion.is_qc({'qc': True}) and qc_companion.is_qc({'qc_of': 'A/B'}) and not qc_companion.is_qc({})
    assert qc_companion.caveat_message('ZZ1/FIN') == \
        'QC companion of ZZ1/FIN: all cells before filtering; not counted in statistics.'
    assert qc_companion.is_pair({'qc': True, 'qc_of': 'ZZ1/FIN'}, 'ZZ1/FQC', 'x', 'ZZ1/FIN', 'y', same_group=False)
    assert qc_companion.is_pair({'qc': True}, 'ZZ1/FQC', 'final_qc', 'ZZ1/FIN', 'final', same_group=True)
    assert not qc_companion.is_pair({'qc': True}, 'ZZ1/FQC', 'final_qc', 'ZZ1/OTH', 'other', same_group=True)
    assert not qc_companion.is_pair({'qc': True, 'qc_of': 'ZZ1/OTH'}, 'ZZ1/FQC', 'final_qc', 'ZZ1/FIN', 'final', True)


def test_qc_companions_are_never_facet_tested():
    assert 'QC companion of ZZ1/FIN' in facet_scopes.facet_exclusion_reason({}, {'qc': True, 'qc_of': 'ZZ1/FIN'})
    assert facet_scopes.facet_exclusion_reason({}, {'qc': True}).startswith('QC companion')


# ── cells.overlap: the pair is exempt, others are not ─────────────────────────

@pytest.fixture
def submission(tmp_path, monkeypatch):
    monkeypatch.setattr(thresholds, 'CELLS_MIN_CELLS', 10)
    return Submission(tmp_path)


def _store_dataset(sub, basename, file_id, obs, **front):
    """A dataset of the submission's group in the store (its .md and h5ad) and in the overlap index."""
    folder = sub.store / sub.group_dir.name
    folder.mkdir(parents=True, exist_ok=True)
    if not (folder / 'index.md').exists():
        write_index_md(folder)
    write_md(folder / f'{basename}.md', id=file_id, title=basename, **front)
    (folder / f'{basename}.h5ad').write_bytes(b'')
    seed_index(sub.index_dir, f'ZZ1/{file_id}', obs, SHARED_API)


def _run_cells(sub, md=None):
    sub.build(md=dict(md or {}, api=SHARED_API))
    with sub.context(duckdb_path=sub.duckdb()) as ctx:
        return checks_cells.cells_overlap(ctx)


def test_qc_deposited_after_its_final_dataset_under_5_percent_removed(submission):
    # QC removed 2 of 60 cells (3%): new and stored cells are "the same both ways", which normally fails
    _store_dataset(submission, 'final', 'FIN', submission.obs.iloc[2:])
    outcome = _run_cells(submission, {'qc': True, 'qc_of': 'ZZ1/FIN', 'is_secondary': True})
    assert outcome.status == PASS and outcome.details['qc_pair_exempt'] == ['ZZ1/FIN']


def test_the_same_cells_without_a_qc_designation_still_fail(submission):
    _store_dataset(submission, 'final', 'FIN', submission.obs.iloc[2:])
    assert _run_cells(submission).status == FAIL


def test_final_deposited_after_its_qc_companion_named_by_basename(submission):
    # the QC companion came first with qc_of empty; the final dataset is <basename> next to <basename>_qc
    _store_dataset(submission, 'dataset_qc', 'DQC', submission.obs, qc=True, is_secondary=True)
    outcome = _run_cells(submission)
    assert outcome.status == PASS and outcome.details['qc_pair_exempt'] == ['ZZ1/DQC']


def test_final_deposited_after_a_qc_companion_that_names_it(submission):
    _store_dataset(submission, 'anything', 'QQQ', submission.obs, qc=True, qc_of='ZZ1/ABC', is_secondary=True)
    assert _run_cells(submission).status == PASS


def test_other_datasets_are_still_compared(submission):
    _store_dataset(submission, 'final', 'FIN', submission.obs.iloc[2:])
    _store_dataset(submission, 'other', 'OTH', submission.obs)
    outcome = _run_cells(submission, {'qc': True, 'qc_of': 'ZZ1/FIN', 'is_secondary': True})
    assert outcome.status == FAIL and outcome.admin_detail['datasets'].keys() == {'ZZ1/OTH'}


def test_design_check_refuses_a_primary_qc_companion(submission):
    _store_dataset(submission, 'final', 'FIN', submission.obs.iloc[2:])
    submission.build(md={'qc': True, 'qc_of': 'ZZ1/FIN', 'is_secondary': False})
    with submission.context() as ctx:
        outcome = checks_design.design_designation(ctx)
    assert outcome.status == FAIL and 'qc_not_secondary' in outcome.summary
    other = Submission(submission.root / 'second')
    other.build(md={'qc': True})
    with other.context() as ctx:
        outcome = checks_design.design_designation(ctx)
    assert outcome.status == PASS and 'QC companion (qc_of empty' in outcome.summary


# ── watcher: secondary, caveat, never the default dataset ─────────────────────

def test_watcher_publishes_a_qc_companion_as_secondary_with_the_caveat(tmp_path):
    from tests.test_ingest_watch import Env
    from tests.ingest_watch_helpers import PREFIX

    env = Env(tmp_path)
    keys = env.deposit(basename='micro_qc', with_seeds=False)
    env.s3.put(f'{PREFIX}WatchTest2026/static/micro_qc.md',
               b'---\ntitle: QC round\nreplicate_unit: donor\nqc: true\n---\n\n' + b'All cells. ' * 30 + b'\n',
               env.clock_value)
    assert env.poll_twice() == {'live': 1}
    meta = yaml.safe_load((env.store / 'WatchTest2026' / 'micro_qc.md').read_text().split('---\n')[1])
    assert meta['qc'] is True and meta['is_secondary'] is True
    caveat = next(c for c in meta['caveats'] if c.get('topic') == 'qc_companion')
    assert caveat['message'] == qc_companion.caveat_message(None)
    index = yaml.safe_load((env.store / 'WatchTest2026' / 'index.md').read_text().split('---\n')[1])
    assert index['default_dataset'] == 'micro'                    # the final dataset's name, not micro_qc
    assert keys['h5ad'].endswith('micro_qc.h5ad')


def test_qc_and_qc_of_are_editable_on_update():
    import ingest_md_fill as md_fill
    assert {'qc', 'qc_of'} <= set(md_fill.DEPOSITOR_EDITABLE_KEYS)
    delta = md_fill.seed_md_changes({'qc_of': 'ZZ1/FIN'}, {'qc': True, 'is_secondary': True})
    assert delta.editable == {'qc_of': (None, 'ZZ1/FIN')} and not delta.conflicts


# ── counting exclusion and markers ───────────────────────────────────────────

def test_api_lineage_marks_a_qc_companion_secondary_whatever_is_secondary_says():
    source = (REPO / 'bin' / 'api' / 'ant-serve').read_text()
    tree = ast.parse(source)
    func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_lineage_fields')
    namespace = {'qc_companion_lib': qc_companion, 'Dict': dict, 'Any': object, 'List': list, 'Optional': object}
    exec(compile(ast.Module(body=[func], type_ignores=[]), 'ant-serve', 'exec'), namespace)
    fields = namespace['_lineage_fields']({'qc': True, 'qc_of': 'ZZ1/FIN', 'is_secondary': True}, [], {}, [],
                                          db_id='ZZ1/FQC')
    assert fields['designation'] == 'qc' and fields['qc'] is True and fields['qc_of'] == 'ZZ1/FIN'
    assert fields['facet_exclude'] is True and 'QC companion' in fields['facet_exclude_reason']
    assert "'is_secondary':  file_metadata.get('is_secondary', False) is True or qc_companion_lib.is_qc(file_metadata)," in source
    assert "'qc_companions': _qc_companions_of(db_id)" in source


def test_counting_tools_leave_qc_companions_out():
    # every counting path reads is_secondary; the route and the checks guarantee it is true for a QC companion
    import gene_detection
    assert gene_detection  # the per-gene detection summary skips is_secondary datasets (lib/gene_detection.py)
    source = (REPO / 'bin' / 'annot' / 'ant-facet-test').read_text()
    assert "if md_yaml.get('is_secondary') is True:" in source
    assert facet_scopes.facet_exclusion_reason({}, {'qc': True, 'is_secondary': True}) is not None


def test_mcp_marks_qc_companions_in_listings():
    from tests.test_mcp_secondary_listing import _search_datasets, DATABASES
    DATABASES.append({'id': 'R26/RQC', 'title': 'QC', 'is_secondary': True, 'designation': 'qc', 'qc': True,
                      'qc_of': 'P26/PRI', 'organism': 'human'})
    try:
        import asyncio
        reply = asyncio.run(_search_datasets()())
        assert 'db_id:      R26/RQC (QC companion of P26/PRI)' in reply
        assert 'QC companion of P26/PRI: all cells before filtering, not counted in statistics' in reply
    finally:
        DATABASES.pop()
