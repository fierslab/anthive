"""Obs profile of a submission: what a curator needs to write the facet mapping.

Columns with dtype, distinct counts, NA fraction, the most frequent values
with counts (categorical) or quantiles (numeric), the columns whose names
suggest a facet, and a summary of the layers. Read from the backed h5ad; no
database needed. Counts of the curator's own data only: no crosswalk content.
"""
from __future__ import annotations

from typing import Any, Dict, List

import numpy as np
import pandas as pd

TOP_VALUES = 30
MAX_COLUMNS = 400
SCHEMA = 'anthive.obs_profile/1'


def _column_profile(name: str, series: pd.Series) -> Dict[str, Any]:
    entry: Dict[str, Any] = {'name': str(name), 'dtype': str(series.dtype), 'n_unique': int(series.nunique(dropna=True)),
                             'na_fraction': round(float(series.isna().mean()), 4)}
    if pd.api.types.is_bool_dtype(series) or not pd.api.types.is_numeric_dtype(series) or series.nunique() <= 20:
        counts = series.astype(object).where(series.notna(), None).dropna().astype(str).value_counts().head(TOP_VALUES)
        entry['top_values'] = [{'value': str(value), 'n_cells': int(count)} for value, count in counts.items()]
    if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
        values = series.dropna().astype(float)
        if len(values):
            quantiles = np.quantile(values, [0.05, 0.25, 0.5, 0.75, 0.95])
            entry['numeric'] = {'min': float(values.min()), 'max': float(values.max()),
                                'quantiles': {label: float(value) for label, value in
                                              zip(('q05', 'q25', 'q50', 'q75', 'q95'), quantiles)}}
    return entry


def candidate_facets(columns: List[str]) -> Dict[str, List[str]]:
    """Facet -> obs columns whose names suggest it (``facet_mapping.candidate_columns``)."""
    from facet_mapping import candidate_columns  # noqa: PLC0415
    from obscol_tags import facet_definition, registered_facet_tags  # noqa: PLC0415

    found: Dict[str, List[str]] = {}
    for tag in registered_facet_tags():
        keywords = facet_definition(tag)['hint_keywords']
        hits = candidate_columns(columns, keywords)
        if hits:
            found[f'ant_{tag}'] = hits
    return found


def build_profile(ctx: Any) -> Dict[str, Any]:
    """The obs profile of the submission in ``ctx`` (needs the h5ad)."""
    from . import sampling  # noqa: PLC0415

    adata = ctx.adata
    obs = adata.obs
    columns = [str(name) for name in obs.columns][:MAX_COLUMNS]
    layers = []
    paths = sampling.matrix_paths(ctx.h5ad_path)
    for name in paths:
        _, sample = ctx.sample_matrix(name, 500, 0)
        layers.append({'name': name, 'min': float(sample.min()) if sample.size else None,
                       'max': float(sample.max()) if sample.size else None,
                       'integer_valued': bool(sample.size and np.all(sample == np.round(sample)))})
    return {'schema': SCHEMA, 'n_obs': int(adata.n_obs), 'n_vars': int(adata.n_vars),
            'obs_columns': [_column_profile(name, obs[name]) for name in columns],
            'candidate_facets': candidate_facets(columns),
            'layers': layers, 'obsm_keys': list(adata.obsm.keys()),
            'var_columns': [str(name) for name in adata.var.columns][:50],
            'var_names_sample': [str(name) for name in adata.var_names[:20]]}
