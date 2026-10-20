"""ant ingest-check: registry (frozen ids), runner, report model, markdown."""
import json

import pytest

import ingest_check
from ingest_check import (FAIL, HELD_CONTROLLED, PASS, SKIP, WARN, CheckResult, Outcome, check, run_checks, tool_version)
from ingest_check.report import AUDIENCES, build_report, curator_view, render_markdown, scrub
from tests.ingest_helpers import Submission

#: The ids are a contract with the curator skill (doc/DESIGN_dataset_ingestion.md section 8.3): never rename.
FROZEN_IDS = [
    'files.sha256', 'files.size', 'files.filename', 'h5ad.structure', 'h5ad.obs_columns', 'duckdb.integrity',
    'meta.md_parse', 'meta.catalog', 'meta.required_keys', 'meta.index_md', 'mus.vocab', 'version.initial',
    'layers.counts', 'layers.x_lognorm', 'layers.consistency', 'genes.ids',
    'facets.namespace', 'facets.mapping_static', 'facets.mapping_data', 'facets.unmapped_values',
    'facets.braak_range', 'facets.core_present',
    'donor.uid_form', 'donor.uid_resolves', 'donor.overlap_ids', 'donor.overlap_demographics',
    'cells.overlap',
    'design.designation', 'design.replicate_unit', 'design.replicates_per_group', 'design.disease_basis',
    'access.level', 'access.controlled', 'access.licence',
    'scores.modules_complete',
]


def test_check_ids_are_frozen():
    assert sorted(ingest_check.check_ids()) == sorted(FROZEN_IDS)
    assert len(set(FROZEN_IDS)) == 35


def test_every_check_has_a_docstring_and_a_family():
    for item in ingest_check.all_checks():
        assert item.fn.__doc__, item.id
        assert item.family == item.id.split('.')[0]
        assert item.docs == f'reference/checks.md#{item.id}'


def test_duplicate_registration_is_refused():
    with pytest.raises(ValueError):
        check('files.sha256')(lambda ctx: None)


def test_tool_version_matches_the_library_version():
    from pathlib import Path
    import re
    text = (Path(__file__).parent.parent / 'lib' / '__init__.py').read_text()
    assert tool_version() == re.search(r'__version__ = "([^"]+)"', text).group(1)


@pytest.fixture
def submission(tmp_path):
    return Submission(tmp_path).build()


def test_run_checks_selects_by_id_and_family(submission):
    with submission.context() as ctx:
        only = run_checks(ctx, only=['files']) 
        assert {item.id for item in only} == {'files.sha256', 'files.size', 'files.filename'}
        one = run_checks(ctx, only=['files.size'])
        assert [item.id for item in one] == ['files.size']
        skipped = run_checks(ctx, only=['files'], skip_ids=['files.sha256'])
        assert {item.id: item.outcome.status for item in skipped}['files.sha256'] == SKIP


def test_run_checks_skips_checks_that_need_missing_inputs(tmp_path):
    sub = Submission(tmp_path).build()
    (sub.group_dir / 'dataset.h5ad').unlink()
    with sub.context() as ctx:
        statuses = {item.id: item.outcome.status for item in run_checks(ctx)}
    assert statuses['files.sha256'] == SKIP and statuses['layers.counts'] == SKIP
    assert statuses['duckdb.integrity'] == SKIP        # no duckdb and no h5ad to convert
    assert statuses['meta.md_parse'] == PASS


def test_no_convert_skips_duckdb_checks_without_a_database(submission):
    with submission.context(convert=False) as ctx:
        statuses = {item.id: item.outcome.status for item in run_checks(ctx, only=['facets'])}
    assert statuses['facets.mapping_data'] == SKIP and statuses['facets.unmapped_values'] == SKIP


def test_a_crashing_check_becomes_a_failure_with_the_traceback_for_admins_only(submission, monkeypatch):
    def boom(ctx):
        raise RuntimeError('secret internals')
    original = ingest_check._REGISTRY['files.size']
    monkeypatch.setitem(ingest_check._REGISTRY, 'files.size', ingest_check.Check(
        original.id, original.family, boom, original.needs, original.docs))
    with submission.context() as ctx:
        results = run_checks(ctx, only=['files'])
        report = build_report(ctx, results, 'admin', tool_version())
    crashed = [entry for entry in report['checks'] if entry['id'] == 'files.size'][0]
    assert crashed['status'] == FAIL and crashed['details'] == {'internal_error': True}
    assert 'secret internals' not in json.dumps(curator_view(report))
    assert 'secret internals' in json.dumps(report['admin_detail'])


def make_results(*outcomes):
    return [CheckResult(f'x.check{index}', 'x', 'docs', outcome) for index, outcome in enumerate(outcomes)]


def test_report_counts_publish_and_held_reasons(submission):
    clean = make_results(Outcome(PASS, 'ok'), Outcome(PASS, 'note', info=True), Outcome(SKIP, 'skipped'))
    held = make_results(Outcome(PASS, 'ok'), Outcome(WARN, 'public', held=[HELD_CONTROLLED]))
    failed = make_results(Outcome(FAIL, 'bad', fix='fix it'), Outcome(WARN, 'meh', held=[HELD_CONTROLLED]))
    with submission.context() as ctx:
        report = build_report(ctx, clean, 'admin', '1.2.3')
        assert (report['result'], report['publish'], report['held_reasons']) == ('pass', 'auto', [])
        assert report['counts'] == {'error': 0, 'warning': 0, 'info': 1}
        assert [entry['severity'] for entry in report['checks']] == ['none', 'info', 'none']
        report = build_report(ctx, held, 'admin', '1.2.3')
        assert (report['result'], report['publish'], report['held_reasons']) == ('pass', 'held', [HELD_CONTROLLED])
        report = build_report(ctx, failed, 'admin', '1.2.3')
        assert (report['result'], report['publish']) == ('fail', 'held')
        assert report['counts'] == {'error': 1, 'warning': 1, 'info': 0}
        assert report['held_reasons'] == [HELD_CONTROLLED]


def test_report_schema_and_dataset_block(submission):
    with submission.context() as ctx:
        results = run_checks(ctx, only=['files', 'meta.md_parse'])
        report = build_report(ctx, results, 'curator', '9.9.9')
    assert report['schema'] == 'anthive.ingest_check/1' and report['tool_version'] == '9.9.9'
    assert report['dataset']['group'] == 'TestGroup' and report['dataset']['basename'] == 'dataset'
    assert len(report['dataset']['sha256']) == 64
    assert 'admin_detail' not in report
    assert set(report) >= {'schema', 'created', 'dataset', 'result', 'publish', 'held_reasons', 'counts', 'checks'}
    assert set(report['checks'][0]) == {'id', 'status', 'severity', 'summary', 'details', 'fix', 'docs'}
    json.dumps(report)


def test_report_rejects_an_unknown_audience(submission):
    with submission.context() as ctx, pytest.raises(ValueError):
        build_report(ctx, [], 'public', '1')
    assert AUDIENCES == ('curator', 'admin')


def test_curator_view_drops_admin_detail():
    admin = {'schema': 's', 'audience': 'admin', 'admin_detail': {'x': 1}, 'checks': []}
    view = curator_view(admin)
    assert 'admin_detail' not in view and view['audience'] == 'curator' and 'admin_detail' in admin


def test_scrub_without_tokens_changes_nothing():
    report = {'a': 'text'}
    assert scrub(report, []) == 0 and report == {'a': 'text'}


def test_markdown_lists_problems_first_with_their_fix(submission):
    results = make_results(Outcome(PASS, 'fine'), Outcome(FAIL, 'broken | pipe', fix='do this'),
                           Outcome(WARN, 'hmm', held=[HELD_CONTROLLED]))
    with submission.context() as ctx:
        text = render_markdown(build_report(ctx, results, 'admin', '1'))
    assert text.index('## To fix') < text.index('## All checks')
    assert '### FAIL x.check1' in text and 'Fix: do this' in text and '### WARN x.check2' in text
    assert 'broken / pipe' in text and 'controlled_access' in text


def test_markdown_without_problems_has_no_fix_section(submission):
    with submission.context() as ctx:
        text = render_markdown(build_report(ctx, make_results(Outcome(PASS, 'fine')), 'admin', '1'))
    assert '## To fix' not in text and '| x.check0 | pass | fine |' in text


def test_to_json_converts_numpy_paths_and_sets():
    import numpy as np
    from pathlib import Path
    from ingest_check.report import to_json
    text = to_json({'a': np.float32(1.5), 'b': np.int64(3), 'c': Path('/x'), 'd': {'z', 'y'}, 'e': np.bool_(True)})
    assert json.loads(text) == {'a': 1.5, 'b': 3, 'c': '/x', 'd': ['y', 'z'], 'e': True}
