"""Donor-count thresholds shared by the facet tests and ``ant ingest-check``.

One definition, so a dataset that passes the ingest check for "enough
donors per facet level" is judged by the same numbers that decide whether
``bin/annot/ant-facet-test`` tests it.
"""

#: Smallest number of cells for a donor x cell type sample to enter a test.
MIN_CELLS_PER_SAMPLE = 10
#: Fewer donors than this in a dataset and the facet tests skip it.
MIN_DONORS_DATASET = 6
#: A facet level (for example one disease group) with fewer donors is dropped.
MIN_DONORS_PER_LEVEL = 3
#: Any level below this makes a result descriptive only.
N_LT5_THRESHOLD = 5
#: Numeric facets (Braak as a number): two halves of at least 5 donors.
NUMERIC_MIN_DONORS_FOR_P = 10
#: Age and PMI covariates need at least this many donors.
MIN_DONORS_FOR_AGE_PMI = 15
#: Share of donors that must carry a covariate for it to enter the model.
MIN_COVARIATE_COVERAGE = 0.9
#: Braak trend test: donors and distinct stages needed.
BRAAK_MIN_DONORS = 6
BRAAK_MIN_DISTINCT = 3

__all__ = [
    'MIN_CELLS_PER_SAMPLE', 'MIN_DONORS_DATASET', 'MIN_DONORS_PER_LEVEL', 'N_LT5_THRESHOLD',
    'NUMERIC_MIN_DONORS_FOR_P', 'MIN_DONORS_FOR_AGE_PMI', 'MIN_COVARIATE_COVERAGE',
    'BRAAK_MIN_DONORS', 'BRAAK_MIN_DISTINCT',
]
