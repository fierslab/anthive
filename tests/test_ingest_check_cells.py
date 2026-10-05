"""ant ingest-check: barcode normalisation, cell hashes and cell overlap classification."""
import numpy as np
import pytest

from ingest_check import FAIL, PASS, SKIP, WARN, thresholds
from ingest_check import checks_cells
from ingest_check.overlap_index import cell_hashes, normalize_barcode
from tests.ingest_helpers import Submission, barcode
from tests.test_ingest_check_donors import SHARED_API


@pytest.mark.parametrize('name,expected', [
    ('AAACCTGAGAAACCAT-1', 'AAACCTGAGAAACCAT'),
    ('aaacctgagaaaccat', 'AAACCTGAGAAACCAT'),
    ('sample7_AAACCTGAGAAACCAT-1', 'AAACCTGAGAAACCAT'),
    ('AAACCTGAGAAACCAT-1-3', 'AAACCTGAGAAACCAT'),
    ('AAACCTGAGAAACCAT.1', 'AAACCTGAGAAACCAT'),
    ('batch2:AAACCTGAGAAACCATGG', 'AAACCTGAGAAACCATGG'),
    ('cell_0001-2', 'CELL_0001'),
    ('plate1_A01', 'PLATE1_A01'),
])
def test_normalize_barcode(name, expected):
    assert normalize_barcode(name) == expected


def test_hashes_are_sorted_unique_and_depend_on_the_donor():
    names = ['AAACCTGAGAAACCAT-1', 'sample_AAACCTGAGAAACCAT-1', 'CCCCCCCCCCCCCCCC-1']
    strict, loose = cell_hashes(names, ['d1', 'd1', 'd2'])
    assert strict.dtype == np.uint64 and len(strict) == 2 and len(loose) == 2     # first two collapse
    assert list(strict) == sorted(strict)
    other_strict, other_loose = cell_hashes(names, ['d9', 'd9', 'd2'])
    assert not np.array_equal(strict, other_strict) and np.array_equal(loose, other_loose)


def test_compare_and_classify():
    base = np.arange(100, dtype=np.uint64)
    assert checks_cells.classify(checks_cells.compare_cells(base, base, base, base)) == 'same'
    inside = checks_cells.compare_cells(base[:60], base[:60], base, base)
    assert checks_cells.classify(inside) == 'reanalysis'
    assert inside['strict_of_new'] == 1.0 and inside['strict_of_old'] == 0.6
    other = np.arange(1000, 1100, dtype=np.uint64)
    assert checks_cells.classify(checks_cells.compare_cells(base, base, other, other)) == 'none'
    relabelled = checks_cells.compare_cells(base, base, other, base)
    assert checks_cells.classify(relabelled) == 'relabelled'


@pytest.fixture
def submission(tmp_path, monkeypatch):
    monkeypatch.setattr(thresholds, 'CELLS_MIN_CELLS', 10)
    return Submission(tmp_path)


def stored(sub, db_key='OLD/AAA', obs=None, api=SHARED_API):
    from tests.ingest_helpers import seed_index
    seed_index(sub.index_dir, db_key, sub.obs if obs is None else obs, api)


def run(sub, md=None, api=SHARED_API):
    sub.build(md=dict(md or {}, api=api))
    with sub.context(duckdb_path=sub.duckdb()) as ctx:
        return checks_cells.cells_overlap(ctx)


def test_skipped_without_an_index(submission):
    assert run(submission).status == SKIP


def test_small_datasets_are_not_compared(submission, monkeypatch):
    stored(submission)
    monkeypatch.setattr(thresholds, 'CELLS_MIN_CELLS', 1000)
    assert run(submission).status == SKIP


def test_disjoint_cells_pass(submission):
    other = submission.obs.copy()
    other.index = [barcode(index + 5000) for index in range(len(other))]
    stored(submission, obs=other)
    assert run(submission).status == PASS


def test_same_cells_fail_and_the_dataset_is_named_for_admins_only(submission):
    stored(submission)
    outcome = run(submission)
    assert outcome.status == FAIL
    assert outcome.details['n_same_cells_datasets'] == 1
    assert outcome.admin_detail['datasets']['OLD/AAA']['verdict'] == 'same'
    assert 'OLD' not in outcome.summary + outcome.fix + str(outcome.details)
    assert 'update' in outcome.fix


def test_reanalysis_needs_a_declaration(submission):
    stored(submission, obs=submission.obs.iloc[:30])
    outcome = run(submission)
    assert outcome.status == FAIL and 'reanalysis_of' in outcome.fix
    declared = run(submission, md={'is_secondary': True, 'reanalysis_of': ['OLD/AAA']})
    assert declared.status == PASS and declared.info
    wrong_target = run(submission, md={'is_secondary': True, 'reanalysis_of': ['OTHER/BBB']})
    assert wrong_target.status == FAIL


def test_same_barcodes_with_other_donor_ids_warn(submission):
    relabelled = submission.obs.copy()
    relabelled['donor'] = relabelled['donor'].map(lambda label: 'x' + label)
    stored(submission, obs=relabelled)
    outcome = run(submission)
    assert outcome.status == WARN and outcome.details['n_relabelled_datasets'] == 1


def test_own_entry_in_the_index_is_ignored(submission):
    stored(submission, db_key='ZZ1/ABC')
    assert run(submission).status == PASS
