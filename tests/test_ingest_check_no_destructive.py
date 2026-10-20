"""ant ingest-check must never write into a submission or the store, nor call the checkers that rewrite .md files."""
import re
from pathlib import Path

import pytest

from ingest_check import run_checks
from tests.ingest_helpers import Submission, snapshot, write_index_md, write_md

ROOT = Path(__file__).parent.parent
SOURCES = sorted((ROOT / 'lib' / 'ingest_check').glob('*.py')) + [ROOT / 'bin' / 'ingest' / 'ant-ingest-check',
                                                                   ROOT / 'bin' / 'ingest' / 'ant-ingest-index']
FORBIDDEN = [r'h5ad-check', r'ant-check', r'fix\s*=\s*True', r'--clean-wal', r'write_md_file', r'ant-h5ad']


@pytest.mark.parametrize('source', SOURCES, ids=lambda path: path.name)
def test_sources_do_not_mention_the_destructive_tools(source):
    text = source.read_text()
    for pattern in FORBIDDEN:
        assert not re.search(pattern, text), f'{source.name} mentions {pattern}'


WRITERS = {'overlap_index.py', 'scratch.py', 'ant-ingest-check', 'ant-ingest-index'}


@pytest.mark.parametrize('source', [path for path in SOURCES if path.name not in WRITERS], ids=lambda path: path.name)
def test_only_the_scratch_index_and_output_modules_write_files(source):
    text = source.read_text()
    for pattern in (r'\.write_text\(', r'\.write_bytes\(', r"open\([^)]*['\"]w", r'write_mus_file', r'\.unlink\(', r'shutil\.(rmtree|move)'):
        assert not re.search(pattern, text), f'{source.name}: {pattern}'


def test_md_check_is_only_called_without_fixing():
    for match in re.finditer(r'check_md_folder\((.*?)\)\n', (ROOT / 'lib' / 'ingest_check' / 'checks_meta.py').read_text(), re.S):
        assert 'fix=False' in match.group(1)


@pytest.fixture
def populated(tmp_path):
    sub = Submission(tmp_path).build()
    other = sub.store / 'Other'
    other.mkdir()
    write_index_md(other, group_id='QQ1')
    write_md(other / 'x.md', id='XYZ', title='x')
    (other / 'x.h5ad').write_bytes(b'')
    (sub.store / 'donor_crosswalk.tsv').write_text('cohort\talias_type\talias\tcanonical_uid\tsource\tn_datasets\n')
    return sub


def test_running_every_check_changes_nothing_but_the_scratch_dir(populated):
    watched = {name: snapshot(folder) for name, folder in (('group', populated.group_dir), ('store', populated.store))}
    database = populated.duckdb()
    with populated.context(duckdb_path=database) as ctx:
        results = run_checks(ctx)
    assert len(results) == 35
    assert {name: snapshot(folder) for name, folder in (('group', populated.group_dir), ('store', populated.store))} == watched
    assert not any(populated.group_dir.glob('*.log')) and not (populated.group_dir / 'tmp').exists()
    assert populated.scratch.exists()                      # the only place anything was written


def test_the_database_given_with_duckdb_is_not_modified(populated):
    database = populated.duckdb()
    stat = (database.stat().st_size, database.stat().st_mtime_ns)
    with populated.context(duckdb_path=database) as ctx:
        run_checks(ctx, only=['duckdb', 'facets', 'donor', 'design', 'cells'])
    assert (database.stat().st_size, database.stat().st_mtime_ns) == stat


def test_scratch_convert_does_not_follow_the_link_back_into_the_submission(populated, tmp_path):
    """ant-convert resolves symlinks, so the scratch copy must be a hard link or a copy."""
    from ingest_check import scratch
    link_target = populated.scratch / 'convert' / 'k' / 'G' / 'dataset.h5ad'
    link_target.parent.mkdir(parents=True)
    scratch.link_or_copy(populated.group_dir / 'dataset.h5ad', link_target)
    assert not link_target.is_symlink() and link_target.resolve().parent == link_target.parent
