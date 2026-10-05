"""Common pathology-based disease definition: the Braak x CERAD 2x2 rule.

``ant_disease`` means different things across studies (author labels,
clinical diagnoses, ADNC grades). The harmonised pathology definition used for
cross-cohort replication is:

* **AD**: Braak stage III or higher AND CERAD neuritic plaque score probable or definite
  (moderate or frequent);
* **control**: Braak stage II or lower AND CERAD none or sparse;
* **NULL**: everything else (discordant combinations such as PART-like Braak >= III with no
  plaques or amyloid-only Braak <= II with frequent plaques, missing values, reference donors).

The rule is declared per dataset in the ``.md`` key ``disease_derivation`` and applied
by ``bin/annot/ant-derive-disease`` (stored obs column) next to ``disease_basis: braak_cerad``.
Pure functions, no I/O; ``tests/test_disease_rule.py``.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional

import pandas as pd

RULE = 'braak_cerad'
BRAAK_AD_MIN = 3.0
BRAAK_CONTROL_MAX = 2.0
CERAD_LEVELS = ('none', 'sparse', 'probable', 'definite')
CERAD_AD = frozenset({'probable', 'definite'})
CERAD_CONTROL = frozenset({'none', 'sparse'})
LABEL_AD = 'AD'
LABEL_CONTROL = 'control'
DISEASE_BASES = ('nia_reagan', 'braak_cerad', 'adnc', 'clinical_dx', 'author_dx')


class DiseaseRuleError(ValueError):
    """A malformed ``disease_derivation`` spec."""


def classify(braak: Optional[float], cerad: Optional[str]) -> Optional[str]:
    """``AD``, ``control`` or None for one donor's Braak stage and CERAD level.

    Args:
        braak: Braak stage 0 to 6 (None or NaN when unknown).
        cerad: One of ``CERAD_LEVELS`` (None when unknown).
    """
    if braak is None or cerad is None:
        return None
    try:
        stage = float(braak)
    except (TypeError, ValueError):
        return None
    if math.isnan(stage):
        return None
    if stage >= BRAAK_AD_MIN and cerad in CERAD_AD:
        return LABEL_AD
    if stage <= BRAAK_CONTROL_MAX and cerad in CERAD_CONTROL:
        return LABEL_CONTROL
    return None


def validate_spec(spec: Any) -> Dict[str, Any]:
    """The checked ``disease_derivation`` mapping.

    Required: ``rule`` (``braak_cerad``), ``column`` (the stored column written),
    ``braak`` and ``cerad`` mappings with a ``column`` and an optional ``map``
    (source value to number for Braak, to one of ``CERAD_LEVELS`` or None for CERAD).

    Raises:
        DiseaseRuleError: the spec is not a valid rule declaration.
    """
    if not isinstance(spec, Mapping) or spec.get('rule') != RULE:
        raise DiseaseRuleError(f'disease_derivation.rule must be {RULE!r}')
    if not isinstance(spec.get('column'), str) or not spec['column']:
        raise DiseaseRuleError('disease_derivation.column must name the stored column to write')
    for part in ('braak', 'cerad'):
        entry = spec.get(part)
        if not isinstance(entry, Mapping) or not isinstance(entry.get('column'), str):
            raise DiseaseRuleError(f'disease_derivation.{part}.column is required')
    for source_value, level in (spec['cerad'].get('map') or {}).items():
        if level is not None and level not in CERAD_LEVELS:
            raise DiseaseRuleError(f'CERAD map {source_value!r} -> {level!r}: not one of {CERAD_LEVELS}')
    for source_value, stage in (spec['braak'].get('map') or {}).items():
        if stage is not None and not isinstance(stage, (int, float)):
            raise DiseaseRuleError(f'Braak map {source_value!r} -> {stage!r}: not a number')
    return dict(spec)


def _mapped(series: pd.Series, mapping: Optional[Mapping[Any, Any]]) -> pd.Series:
    if not mapping:
        return series
    keyed = {str(key): value for key, value in mapping.items()}

    def lookup(value: Any) -> Any:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        text = str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
        return keyed.get(text)

    return series.map(lookup)


def classify_frame(braak: pd.Series, cerad: pd.Series, spec: Mapping[str, Any]) -> pd.Series:
    """Per-cell labels from the raw Braak and CERAD columns and a validated spec.

    Args:
        braak: Raw Braak column of the dataset.
        cerad: Raw CERAD column of the dataset.
        spec: ``validate_spec`` result (the maps are applied here).

    Returns:
        Object series of ``AD`` / ``control`` / None with the index of ``braak``.
    """
    stages = pd.to_numeric(_mapped(braak, spec['braak'].get('map')), errors='coerce')
    levels = _mapped(cerad, spec['cerad'].get('map'))
    return pd.Series([classify(stage, level) for stage, level in zip(stages, levels)],
                     index=braak.index, dtype=object)
