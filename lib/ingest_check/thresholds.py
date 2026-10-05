"""Thresholds of ``ant ingest-check``, in one place.

The donor-count rules come from ``lib/facet_rules.py`` (shared with the facet
tests); everything else is specific to the ingest checks. Tune here, not in
the check functions.
"""
from facet_rules import MIN_DONORS_DATASET, MIN_DONORS_PER_LEVEL

# Layers: how many cells are sampled, and with which seed (reproducible).
SAMPLE_CELLS = 2000
SAMPLE_SEED = 0
CONSISTENCY_CELLS = 1000

# layers.x_lognorm
LOGNORM_MAX_VALUE = 20.0            # a log1p scale value above this is not log-normalised
LOGNORM_ROWSUM_CV_MAX = 0.1         # CV of expm1(X) row sums for a library-size normalisation

# layers.consistency: median Pearson r of log1p(CP10k(counts)) against X
CONSISTENCY_PASS = 0.99
CONSISTENCY_WARN = 0.95
CONSISTENCY_TARGET_SUM = 1e4

# genes.ids: share of var_names found in the gene universe of the organism
GENES_KNOWN_PASS = 0.90
GENES_KNOWN_WARN = 0.70
GENES_UNIVERSE_MIN_DATASETS = 3     # fewer datasets of the organism and the share is not judged

# files
MIN_DESCRIPTION_CHARS = 200

# donor.uid_resolves
UID_FALLBACK_WARN_FRACTION = 0.20

# donor.overlap_demographics: candidate rule and the cohort-level decision
DEMOGRAPHIC_AGE_TOLERANCE_YEARS = 1.0
DEMOGRAPHIC_PMI_TOLERANCE_HOURS = 1.0
AGE_CENSOR_THRESHOLD = 90.0         # ages at or above this are treated as censored (90+)
DEMOGRAPHIC_HELD_FRACTION = 0.50    # share of donors with a unique match in one cohort: held (if also beyond chance)
DEMOGRAPHIC_WARN_FRACTION = 0.20      # share beyond the chance level that earns a warning (and the held floor)
DEMOGRAPHIC_WARN_MIN_EXCESS = 15    # or at least this many donors beyond chance
DEMOGRAPHIC_CLOSE_DISTANCE = 0.25   # a candidate this close (in tolerances) counts as the match when it is the only one
DEMOGRAPHIC_MIN_DONORS = 5          # fewer new donors and the fraction is not judged

# cells.overlap
CELLS_SAME_FRACTION = 0.95          # new and existing cells both this far inside each other: same cells
CELLS_REANALYSIS_FRACTION = 0.50    # at least this share of the new cells inside one dataset: reanalysis
CELLS_MIN_CELLS = 100               # smaller datasets are not compared by cells

# design.replicates_per_group
MIN_UNITS_TOTAL = MIN_DONORS_DATASET
MIN_UNITS_PER_LEVEL = MIN_DONORS_PER_LEVEL

# Values that mean "not known" in a licence field
LICENCE_UNKNOWN = frozenset({'', 'unknown', 'none', 'na', 'n/a', 'tbd', 'todo', 'unspecified'})
