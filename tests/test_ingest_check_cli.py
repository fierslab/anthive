"""bin/ingest/ant-ingest-check: argument handling, outputs, exit codes."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

import ingest_check
from tests.ingest_helpers import Submission, snapshot

SCRIPT = Path(__file__).parent.parent / 'bin' / 'ingest' / 'ant-ingest-check'


@pytest.fixture(scope='module')
def cli():
    loader = importlib.machinery.SourceFileLoader('ant_ingest_check', str(SCRIPT))
    spec = importlib.util.spec_from_loader('ant_ingest_check', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def submission(tmp_path, monkeypatch):
    sub = Submission(tmp_path).build()
    monkeypatch.setenv('ANTHIVE_H5ADSTORE', str(sub.store))
    monkeypatch.setenv('ANTHIVE_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('ANTHIVE_TMP_DIR', str(tmp_path / 'tmp'))
    return sub


def run(cli, sub, *arguments, target=None):
    return cli.main([str(target or sub.group_dir), '--no-convert', *arguments])


def test_list_checks_prints_the_ids(cli, capsys):
    assert cli.main(['--list-checks']) == 0
    assert capsys.readouterr().out.split() == ingest_check.check_ids()


def test_target_is_required(cli):
    with pytest.raises(SystemExit):
        cli.main([])


def test_clean_run_exits_zero_and_writes_both_outputs(cli, submission, tmp_path, capsys):
    json_path, md_path = tmp_path / 'out' / 'report.json', tmp_path / 'out' / 'report.md'
    json_path.parent.mkdir()
    code = run(cli, submission, '--checks', 'files,meta.md_parse', '--json', str(json_path), '--md-summary', str(md_path))
    assert code == 0
    report = json.loads(json_path.read_text())
    assert report['schema'] == 'anthive.ingest_check/1' and report['result'] == 'pass' and report['audience'] == 'admin'
    assert {entry['id'] for entry in report['checks']} == {'files.sha256', 'files.size', 'files.filename', 'meta.md_parse'}
    assert md_path.read_text().startswith('# Ingest check: TestGroup/dataset')
    assert '# Ingest check' in capsys.readouterr().out


def test_error_exits_one(cli, submission, tmp_path):
    (submission.group_dir / 'dataset.md').write_text('---\nid: [broken\n---\n')
    assert run(cli, submission, '--checks', 'meta.md_parse') == 1


def test_audience_curator_drops_admin_detail(cli, submission, tmp_path):
    json_path = tmp_path / 'r.json'
    assert run(cli, submission, '--checks', 'files', '--audience', 'curator', '--json', str(json_path)) == 0
    assert 'admin_detail' not in json.loads(json_path.read_text())
    run(cli, submission, '--checks', 'files', '--json', str(json_path))
    assert 'admin_detail' in json.loads(json_path.read_text())


def test_json_to_stdout_sends_the_summary_to_stderr(cli, submission, capsys):
    assert run(cli, submission, '--checks', 'files', '--json', '-') == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)['result'] == 'pass' and '# Ingest check' in captured.err


def test_skip_option(cli, submission, tmp_path):
    json_path = tmp_path / 'r.json'
    run(cli, submission, '--checks', 'files', '--skip', 'files.size', '--json', str(json_path))
    status = {entry['id']: entry['status'] for entry in json.loads(json_path.read_text())['checks']}
    assert status['files.size'] == 'skip' and status['files.sha256'] == 'pass'


def test_file_target_and_dataset_option(cli, submission, tmp_path):
    json_path = tmp_path / 'r.json'
    assert cli.main([str(submission.group_dir / 'dataset.h5ad'), '--no-convert', '--checks', 'files', '--json', str(json_path)]) == 0
    assert json.loads(json_path.read_text())['dataset']['basename'] == 'dataset'
    second = submission.group_dir / 'other.h5ad'
    second.write_bytes((submission.group_dir / 'dataset.h5ad').read_bytes())
    with pytest.raises(SystemExit, match='holds 2 h5ad files'):
        run(cli, submission)
    assert run(cli, submission, '--dataset', 'other', '--checks', 'files', '--json', str(json_path)) == 0
    assert json.loads(json_path.read_text())['dataset']['basename'] == 'other'


def test_missing_target_exits(cli, tmp_path):
    with pytest.raises(SystemExit, match='neither a folder nor'):
        cli.resolve_target(str(tmp_path / 'nope'), None)


def test_declared_sha256_is_checked(cli, submission):
    assert run(cli, submission, '--checks', 'files.sha256', '--sha256', '0' * 64) == 1


def test_mode_existing_and_explicit_duckdb(cli, submission, tmp_path):
    json_path = tmp_path / 'r.json'
    database = submission.duckdb()
    assert run(cli, submission, '--mode', 'existing', '--duckdb', str(database), '--checks', 'version.initial,facets.braak_range',
               '--json', str(json_path)) == 0
    status = {entry['id']: entry['status'] for entry in json.loads(json_path.read_text())['checks']}
    assert status == {'facets.braak_range': 'pass', 'version.initial': 'skip'}


@pytest.mark.parametrize('where', ['group', 'store'])
def test_outputs_inside_the_submission_or_store_are_refused(cli, submission, where):
    folder = submission.group_dir if where == 'group' else submission.store
    before = snapshot(folder)
    with pytest.raises(SystemExit, match='refusing to write'):
        run(cli, submission, '--json', str(folder / 'report.json'))
    with pytest.raises(SystemExit, match='refusing to write'):
        run(cli, submission, '--md-summary', str(folder / 'sub' / 'report.md'))
    assert snapshot(folder) == before


def test_profile_only_writes_the_obs_profile(cli, submission, tmp_path):
    json_path = tmp_path / 'profile.json'
    assert run(cli, submission, '--profile-only', '--json', str(json_path)) == 0
    profile = json.loads(json_path.read_text())
    assert profile['schema'] == 'anthive.obs_profile/1' and profile['n_obs'] == 60
    columns = {column['name']: column for column in profile['obs_columns']}
    assert columns['donor']['n_unique'] == 8 and columns['donor']['top_values'][0]['n_cells'] > 0
    assert columns['age']['numeric']['min'] == 60.0 and columns['age']['numeric']['quantiles']['q50'] > 60.0
    assert 'ant_sex' in profile['candidate_facets'] and 'sex_raw' in profile['candidate_facets']['ant_sex']
    assert {layer['name'] for layer in profile['layers']} == {'X', 'counts'}
    assert [layer['integer_valued'] for layer in profile['layers'] if layer['name'] == 'counts'] == [True]


def test_profile_only_prints_to_stdout_and_needs_the_h5ad(cli, submission, capsys):
    assert run(cli, submission, '--profile-only') == 0
    assert json.loads(capsys.readouterr().out)['n_obs'] == 60
    (submission.group_dir / 'dataset.h5ad').unlink()
    with pytest.raises(SystemExit, match='not found'):
        run(cli, submission, '--profile-only', '--dataset', 'dataset')


def test_a_crashing_check_exits_two(cli, submission, monkeypatch):
    original = ingest_check._REGISTRY['files.size']
    def boom(ctx):
        raise RuntimeError('bug')
    monkeypatch.setitem(ingest_check._REGISTRY, 'files.size', ingest_check.Check(
        original.id, original.family, boom, original.needs, original.docs))
    assert run(cli, submission, '--checks', 'files.size') == 2


def test_tool_failure_exits_two(cli, submission, monkeypatch, capsys):
    monkeypatch.setattr(cli, 'run_checks', lambda *a, **k: (_ for _ in ()).throw(OSError('disk gone')))
    assert run(cli, submission) == 2
    assert 'disk gone' in capsys.readouterr().err


def test_a_full_run_leaves_submission_and_store_untouched(cli, submission, tmp_path):
    (submission.store / 'Other').mkdir()
    (submission.store / 'Other' / 'index.md').write_text('---\ngroup: Other\ngroup_id: QQ1\n---\n')
    folders = (submission.group_dir, submission.store)
    before = [snapshot(folder) for folder in folders]
    code = run(cli, submission, '--duckdb', str(submission.duckdb()), '--json', str(tmp_path / 'r.json'))
    assert code in (0, 1)
    assert [snapshot(folder) for folder in folders] == before
