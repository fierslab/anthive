"""End-to-end tests for bin/ant-annotate via subprocess."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'lib'))
sys.path.insert(0, str(REPO_ROOT / 'tests'))

from test_annotation_writer import build_synthetic_duckdb, CELL_NAMES  # noqa: E402

ANT_ANNOTATE = REPO_ROOT / 'bin' / 'ant-annotate'


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    """Invoke ant-annotate with absolute path; capture output."""
    return subprocess.run(
        [str(ANT_ANNOTATE), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _write_tsv(path: Path, mapping: dict) -> None:
    with open(path, 'w') as fh:
        for k, v in mapping.items():
            fh.write(f"{k}\t{v}\n")


def test_add_num_then_status_then_rollback(tmp_path):
    """Full lifecycle: add → status shows it → rollback drops it."""
    db = tmp_path / "lifecycle.duckdb"
    build_synthetic_duckdb(db)

    tsv = tmp_path / "scores.tsv"
    _write_tsv(tsv, {name: float(i) for i, name in enumerate(CELL_NAMES)})

    # add-num
    result = _run_cli(
        'add-num', str(db), 'my_score', str(tsv),
        '--catalog', 'TEST_V1', '--run-id', 'r-aaa',
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert 'committed' in result.stdout

    # status
    result = _run_cli('status', str(db))
    assert result.returncode == 0
    assert 'my_score' in result.stdout
    assert 'TEST_V1' in result.stdout
    assert 'r-aaa' in result.stdout

    # rollback
    result = _run_cli('rollback', str(db), '--run-id', 'r-aaa', '-y')
    assert result.returncode == 0, (result.stdout, result.stderr)

    # status again — should be clean
    result = _run_cli('status', str(db))
    assert 'No pending columns' in result.stdout


def test_add_cat(tmp_path):
    db = tmp_path / "cat.duckdb"
    build_synthetic_duckdb(db)

    tsv = tmp_path / "labels.tsv"
    _write_tsv(tsv, {name: ('A' if i < 10 else 'B')
                     for i, name in enumerate(CELL_NAMES)})

    result = _run_cli('add-cat', str(db), 'cluster', str(tsv))
    assert result.returncode == 0, (result.stdout, result.stderr)

    con = duckdb.connect(str(db), read_only=True)
    try:
        df = con.execute('SELECT cluster, COUNT(*) AS n FROM obscat '
                         'GROUP BY cluster ORDER BY cluster').fetchdf()
    finally:
        con.close()
    assert dict(zip(df['cluster'], df['n'])) == {'A': 10, 'B': 10}


def test_add_num_conflict_fails_without_overwrite(tmp_path):
    db = tmp_path / "conflict.duckdb"
    build_synthetic_duckdb(db)

    tsv = tmp_path / "v1.tsv"
    _write_tsv(tsv, {name: 1.0 for name in CELL_NAMES})

    r1 = _run_cli('add-num', str(db), 'foo', str(tsv))
    assert r1.returncode == 0

    # Second add without --overwrite must fail
    r2 = _run_cli('add-num', str(db), 'foo', str(tsv))
    assert r2.returncode != 0
    assert 'already exists' in r2.stderr

    # With --overwrite it succeeds
    r3 = _run_cli('add-num', str(db), 'foo', str(tsv), '--overwrite')
    assert r3.returncode == 0


def test_rollback_unknown_run_id_fails(tmp_path):
    db = tmp_path / "norollback.duckdb"
    build_synthetic_duckdb(db)
    result = _run_cli('rollback', str(db), '--run-id', 'nonexistent', '-y')
    assert result.returncode != 0
    assert 'no pending columns' in result.stderr.lower()


def test_status_on_clean_db(tmp_path):
    db = tmp_path / "clean.duckdb"
    build_synthetic_duckdb(db)
    result = _run_cli('status', str(db))
    assert result.returncode == 0
    assert 'clean' in result.stdout
