"""ant ingest-check: scratch conversion, hash cache and the private copy for ant-duckdb-check."""
import os
import stat
import types

import pytest

from ingest_check import checks_files, scratch
from ingest_check.scratch import ConvertError
from tests.ingest_helpers import Submission, make_duckdb, snapshot

FAKE_CONVERT = """#!/bin/sh
echo "$1 staging=$ANTHIVE_CONVERT_STAGING_DIR spill=$ANTHIVE_DUCKDB_TEMP_DIR" >> "{log}"
touch "${{1%.h5ad}}.duckdb"
"""

FAILING_CONVERT = """#!/bin/sh
echo "line one" >&2
echo "ERROR: boom" >&2
exit 3
"""


def script(path, body, **format_arguments):
    path.write_text(body.format(**format_arguments))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def submission(tmp_path):
    return Submission(tmp_path).build()


def convert(sub, convert_script, **metadata_changes):
    metadata = dict(sub_md(sub), **metadata_changes)
    return scratch.scratch_convert(sub.group_dir / 'dataset.h5ad', sub.group_dir / 'dataset.md', sub.group_dir / 'index.md',
                                   sub.group_dir / 'dataset.h5ad.mus', metadata, 'a' * 64, sub.scratch, convert_script,
                                   sub.group_dir.name)


def sub_md(sub):
    from metadata import parse_md_file
    return parse_md_file(sub.group_dir / 'dataset.md')[0]


def test_scratch_convert_never_touches_the_submission_folder(submission, tmp_path):
    log = tmp_path / 'convert.log'
    fake = script(tmp_path / 'fake-convert', FAKE_CONVERT, log=log)
    before = snapshot(submission.group_dir)
    result = convert(submission, fake)
    assert snapshot(submission.group_dir) == before                              # nothing new, nothing changed
    assert result.exists() and submission.scratch in result.parents
    scratch_h5ad = result.with_suffix('.h5ad')
    assert os.stat(scratch_h5ad).st_ino == os.stat(submission.group_dir / 'dataset.h5ad').st_ino   # hard link, no copy
    assert (result.parent / 'dataset.md').read_text() == (submission.group_dir / 'dataset.md').read_text()
    assert (result.parent / 'index.md').exists() and (result.parent / 'dataset.h5ad.mus').exists()
    text = log.read_text()
    assert str(submission.scratch / 'staging') in text and str(submission.scratch / 'spill') in text
    assert str(submission.group_dir) not in text


def test_scratch_convert_is_cached_by_hash_and_convert_relevant_keys(submission, tmp_path):
    log = tmp_path / 'convert.log'
    fake = script(tmp_path / 'fake-convert', FAKE_CONVERT, log=log)
    first = convert(submission, fake)
    assert convert(submission, fake, title='another title', version='2.0.0') == first         # metadata-only edit
    assert len(log.read_text().splitlines()) == 1
    other = convert(submission, fake, obscat_include=['donor'])                                # changes what convert reads
    assert other != first and len(log.read_text().splitlines()) == 2


def test_scratch_copy_of_md_follows_metadata_only_edits(submission, tmp_path):
    fake = script(tmp_path / 'fake-convert', FAKE_CONVERT, log=tmp_path / 'log')
    first = convert(submission, fake)
    (submission.group_dir / 'dataset.md').write_text((submission.group_dir / 'dataset.md').read_text() + '\n# edited\n')
    assert convert(submission, fake) == first
    assert 'edited' in (first.parent / 'dataset.md').read_text()


def test_failed_convert_raises_with_the_last_lines(submission, tmp_path):
    fake = script(tmp_path / 'bad-convert', FAILING_CONVERT)
    with pytest.raises(ConvertError, match='(?s)exited 3.*boom'):
        convert(submission, fake)


def test_context_exposes_the_scratch_database_or_the_error(submission, tmp_path):
    fake = script(tmp_path / 'fake-convert', FAKE_CONVERT, log=tmp_path / 'log')
    with submission.context(convert=True, convert_script=fake) as ctx:
        assert ctx.duckdb is not None and submission.scratch in ctx.duckdb.parents and ctx.convert_error is None
    bad = script(tmp_path / 'bad-convert', FAILING_CONVERT)
    other = Submission(tmp_path / 'other').build(md={'obscat_include': ['donor']})       # another cache key
    with other.context(convert=True, convert_script=bad) as ctx:
        assert ctx.duckdb is None and 'boom' in ctx.convert_error


def test_existing_sibling_duckdb_is_used_only_when_fresh(submission):
    sibling = submission.group_dir / 'dataset.duckdb'
    sibling.write_bytes(b'')
    os.utime(sibling, (1, 1))
    with submission.context() as ctx:
        assert ctx.existing_duckdb_path() is None                      # older than the h5ad
    os.utime(sibling, None)
    with submission.context() as ctx:
        assert ctx.existing_duckdb_path() == sibling


def test_prune_removes_only_old_builds(tmp_path):
    root = tmp_path / 'convert'
    (root / 'old').mkdir(parents=True)
    (root / 'new').mkdir()
    os.utime(root / 'old', (1, 1))
    assert scratch.prune_scratch(root) == 1
    assert not (root / 'old').exists() and (root / 'new').exists()
    assert scratch.prune_scratch(tmp_path / 'missing') == 0


def test_sha256_is_cached_by_size_and_mtime(tmp_path, monkeypatch):
    path = tmp_path / 'f.bin'
    path.write_bytes(b'abc')
    calls = []
    import checksum
    real = checksum.calculate_sha256
    monkeypatch.setattr(checksum, 'calculate_sha256', lambda p, chunk_size=8192: calls.append(p) or real(p, chunk_size))
    cache = tmp_path / 'scratch' / 'cache.json'
    first = scratch.sha256_cached(path, cache)
    assert scratch.sha256_cached(path, cache) == first and len(calls) == 1
    path.write_bytes(b'abcd')
    assert scratch.sha256_cached(path, cache) != first and len(calls) == 2
    cache.write_text('not json')
    assert scratch.sha256_cached(path, cache) and len(calls) == 3


def test_link_or_copy_falls_back_to_a_copy(tmp_path, monkeypatch):
    source = tmp_path / 'a'
    source.write_text('x')
    monkeypatch.setattr(os, 'link', lambda *a: (_ for _ in ()).throw(OSError('cross-device')))
    scratch.link_or_copy(source, tmp_path / 'b')
    assert (tmp_path / 'b').read_text() == 'x' and os.stat(tmp_path / 'b').st_ino != os.stat(source).st_ino


def test_isolated_copy_leaves_the_original_alone_and_links_the_parquet(tmp_path):
    store_dir = tmp_path / 'store' / 'G'
    store_dir.mkdir(parents=True)
    original = make_duckdb(store_dir / 'ds.duckdb', Submission(tmp_path / 's').obs)
    (store_dir / 'ds.md').write_text('---\nid: ABC\n---\n')
    (store_dir / 'ds.layer.counts.by_gene.parquet').write_bytes(b'p')
    (store_dir / 'ds.scores.parquet').write_bytes(b's')
    before = snapshot(store_dir)
    copy = scratch.isolated_copy_for_check(original, store_dir / 'ds.md', tmp_path / 'scratch')
    assert snapshot(store_dir) == before
    assert copy != original and tmp_path / 'scratch' in copy.parents
    assert (copy.parent / 'ds.md').read_text().startswith('---') and (copy.parent / 'ds.layer.counts.by_gene.parquet').is_symlink()
    assert (copy.parent / 'ds.scores.parquet').is_symlink()
    assert scratch.isolated_copy_for_check(original, store_dir / 'ds.md', tmp_path / 'scratch') == copy      # cached


def test_isolated_copy_returns_scratch_databases_unchanged(tmp_path):
    inside = tmp_path / 'scratch' / 'convert' / 'k' / 'G' / 'ds.duckdb'
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b'')
    assert scratch.isolated_copy_for_check(inside, inside.with_suffix('.md'), tmp_path / 'scratch') == inside.resolve()


def test_duckdb_integrity_runs_the_tool_on_a_copy_in_the_scratch_dir(submission, monkeypatch, tmp_path):
    original = submission.duckdb()
    seen = {}

    def fake_run(command, **options):
        seen['command'], seen['cwd'] = command, options['cwd']
        class Done:
            returncode, stdout, stderr = 0, '', ''
        return Done()
    monkeypatch.setattr(checks_files, 'subprocess', types.SimpleNamespace(run=fake_run))
    before = (original.stat().st_size, original.stat().st_mtime_ns)
    with submission.context(duckdb_path=original) as ctx:
        outcome = checks_files.duckdb_integrity(ctx)
    assert outcome.status == 'pass'
    target = seen['command'][1]
    assert target != str(original) and str(submission.scratch) in target and seen['cwd'] == submission.scratch
    assert (original.stat().st_size, original.stat().st_mtime_ns) == before


def test_duckdb_integrity_reports_findings_and_warnings(submission, monkeypatch):
    banner = '=' * 10
    outputs = iter([(1, f'{banner}\nWARNINGS & ERRORS\n{banner}\n✗ Missing required tables:\n  - x\n{banner}\n'),
                    (0, f'{banner}\nWARNINGS & ERRORS\n{banner}\n⚠ minor thing\n{banner}\n')])

    def fake_run(command, **options):
        code, text = next(outputs)
        class Done:
            returncode, stdout, stderr = code, text, ''
        return Done()
    monkeypatch.setattr(checks_files, 'subprocess', types.SimpleNamespace(run=fake_run))
    with submission.context(duckdb_path=submission.duckdb()) as ctx:
        failed = checks_files.duckdb_integrity(ctx)
        warned = checks_files.duckdb_integrity(ctx)
    assert failed.status == 'fail' and failed.details['findings'] == ['✗ Missing required tables:', '- x']
    assert warned.status == 'warn' and warned.details['findings'] == ['⚠ minor thing']
