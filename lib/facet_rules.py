"""Replicate-count thresholds shared by the facet tests and ``ant ingest-check``.

One definition, so a dataset that passes the ingest check for "enough replicates
per facet level" is judged by the same numbers that decide whether
``bin/annot/ant-facet-test`` tests it.

Facet engine v3 (2026-10-06): test eligibility is the statistical minimum, not a
comfort level. Every tested cohort takes part in replication (no donor-count filter
for counting); the earlier dataset rule (at least 6 donors), the descriptive-only
rule (a level below 5 donors, a numeric facet below 10) and the Braak minimum of 6
donors are gone as blockers. What stays is what the statistics need:

* two-group facets: at least ``MIN_REPLICATES_PER_LEVEL`` (3) replicates per level;
* numeric facets (Braak, age): at least ``NUMERIC_MIN_REPLICATES`` (4) replicates with at
  least ``NUMERIC_MIN_DISTINCT`` (3) distinct values;
* the model keeps at least ``MIN_RESIDUAL_DF`` (2) residual degrees of freedom: when the
  covariates would take them, the covariates are dropped, never the test.

``N_LT5_THRESHOLD`` and ``NUMERIC_MIN_DONORS_FOR_P`` survive only as the informational
``small_sample`` flag of a unit (``n_lt5`` in the result files): a small unit is tested,
counted and flagged, never silenced.
"""
import math

#: Smallest number of cells for a donor x cell type sample to enter a test.
MIN_CELLS_PER_SAMPLE = 10
#: Replicates per level of a two-group facet needed for a test.
MIN_DONORS_PER_LEVEL = 3
MIN_REPLICATES_PER_LEVEL = MIN_DONORS_PER_LEVEL
#: Numeric facets (Braak, age): replicates with a value, and distinct values among them.
NUMERIC_MIN_REPLICATES = 4
NUMERIC_MIN_DISTINCT = 3
#: A dataset with fewer replicates than the smallest numeric test cannot be tested for any facet.
MIN_DONORS_DATASET = NUMERIC_MIN_REPLICATES
#: Residual degrees of freedom the model must keep; covariates are dropped before the test is.
MIN_RESIDUAL_DF = 2
#: Informational small-sample flag (never blocks): a level below this, or a numeric facet below the second value.
N_LT5_THRESHOLD = 5
NUMERIC_MIN_DONORS_FOR_P = 10
#: Age and PMI covariates need at least this many replicates in the unit (power rule, never blocks the test).
MIN_DONORS_FOR_AGE_PMI = 15
#: Share of replicates that must carry a covariate for it to enter the model.
MIN_COVARIATE_COVERAGE = 0.9
#: Numeric trend tests: the old Braak names, kept for callers that import them.
BRAAK_MIN_DONORS = NUMERIC_MIN_REPLICATES
BRAAK_MIN_DISTINCT = NUMERIC_MIN_DISTINCT

#: Effect gate of a replicated gene (limma treat tau): log2(1.1) for two-group contrasts, the same
#: 10 percent spread over three stages for a Braak slope, and 10 percent per decade for age.
#: Defined here so that the test tool, the result reader and the docs share one number.
TAU_TWO_GROUP = math.log2(1.1)
TAU_BRAAK_PER_STAGE = math.log2(1.1) / 3
TAU_AGE_PER_DECADE = math.log2(1.1)
#: Age is modelled per decade (age / 10); the coefficient is the log2 fold change per 10 years.
AGE_UNIT_YEARS = 10.0


def gate_tau(facet: str) -> float:
    """Effect-gate threshold (log2 fold change) of a gene facet: per Braak stage, per decade of age or per contrast."""
    return {'braak': TAU_BRAAK_PER_STAGE, 'age': TAU_AGE_PER_DECADE}.get(facet, TAU_TWO_GROUP)


#: Effect gate of a replicated module: |standardised effect| (estimate / SD of the donor-level score).
MODULE_STD_EFFECT_GATE = 0.2

__all__ = [
    'MIN_CELLS_PER_SAMPLE', 'MIN_DONORS_DATASET', 'MIN_DONORS_PER_LEVEL', 'MIN_REPLICATES_PER_LEVEL',
    'N_LT5_THRESHOLD', 'NUMERIC_MIN_DONORS_FOR_P', 'NUMERIC_MIN_REPLICATES', 'NUMERIC_MIN_DISTINCT',
    'MIN_RESIDUAL_DF', 'MIN_DONORS_FOR_AGE_PMI', 'MIN_COVARIATE_COVERAGE', 'BRAAK_MIN_DONORS',
    'BRAAK_MIN_DISTINCT', 'TAU_TWO_GROUP', 'TAU_BRAAK_PER_STAGE', 'TAU_AGE_PER_DECADE', 'AGE_UNIT_YEARS',
    'MODULE_STD_EFFECT_GATE', 'gate_tau',
]
