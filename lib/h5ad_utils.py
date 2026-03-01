"""
H5AD file utilities for anthive

Functions for loading, validating, and analyzing h5ad files
"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import anndata
import numpy as np
import pandas as pd


class H5ADError(Exception):
    """H5AD file related errors"""
    pass


def validate_filename(filename: str) -> Tuple[bool, Optional[str]]:
    """
    Validate h5ad filename meets requirements

    Requirements:
    - No spaces
    - No special characters (only alphanumeric, dash, underscore, dot)
    - Basename (without .h5ad) <= 50 characters

    Args:
        filename: Name of the file (with or without path)

    Returns:
        Tuple of (is_valid, error_message)
        If valid, error_message is None
        If invalid, error_message explains the problem
    """
    basename = Path(filename).stem  # Remove .h5ad extension

    # Check for spaces
    if ' ' in filename:
        suggested = filename.replace(' ', '_')
        return False, f"Filename contains spaces. Suggested: {suggested}"

    # Check for special characters
    if not re.match(r'^[a-zA-Z0-9._-]+$', filename):
        return False, "Filename contains special characters. Only alphanumeric, dash, underscore, and dot allowed"

    # Check basename length
    if len(basename) > 50:
        return False, f"Basename too long ({len(basename)} characters). Maximum 50 characters allowed"

    return True, None


def load_h5ad(file_path: Path) -> anndata.AnnData:
    """
    Load h5ad file with error handling

    Args:
        file_path: Path to h5ad file

    Returns:
        Loaded AnnData object

    Raises:
        H5ADError: If file cannot be loaded
    """
    if not file_path.exists():
        raise H5ADError(
            f"ERROR: [ant-check] File not found\n"
            f"Location: {file_path}\n"
            f"Action: Check file path and try again"
        )

    try:
        adata = anndata.read_h5ad(file_path)
        return adata
    except Exception as error:
        raise H5ADError(
            f"ERROR: [ant-check] Failed to load h5ad file\n"
            f"Details: File may be corrupted or not a valid h5ad file\n"
            f"Location: {file_path}\n"
            f"Error: {error}\n"
            f"Action: Verify file integrity and format"
        )


def validate_structure(adata: anndata.AnnData) -> List[str]:
    """
    Validate h5ad file has required structure

    Args:
        adata: Loaded AnnData object

    Returns:
        List of error messages (empty if valid)
    """
    errors = []

    # Check for .X or layers (at least one must exist)
    has_X = adata.X is not None
    has_layers = adata.layers is not None and len(adata.layers) > 0

    if not has_X and not has_layers:
        errors.append("Missing both .X and .layers (need at least one expression matrix)")

    # Check for .obs
    if adata.obs is None or len(adata.obs) == 0:
        errors.append("Missing or empty .obs (cell annotations)")

    # Check for .var
    if adata.var is None or len(adata.var) == 0:
        errors.append("Missing or empty .var (gene annotations)")

    return errors


def check_layer_sparsity(layer_data, layer_name: str) -> Tuple[bool, Dict[str, Any]]:
    """
    Check if layer has 0 as most common value (sparse data)

    Args:
        layer_data: Layer data (numpy array or sparse matrix)
        layer_name: Name of the layer for reporting

    Returns:
        Tuple of (is_sparse, info_dict)
        info_dict contains: most_common_value, most_common_count, total_values
    """
    # Handle sparse matrices
    if hasattr(layer_data, 'toarray'):
        # Count zeros efficiently for sparse matrices
        total_values = layer_data.shape[0] * layer_data.shape[1]
        nonzero_count = layer_data.nnz
        zero_count = total_values - nonzero_count

        # Get most common non-zero value
        data_values = layer_data.data
        if len(data_values) > 0:
            unique, counts = np.unique(data_values, return_counts=True)
            most_common_nonzero_idx = np.argmax(counts)
            most_common_nonzero = unique[most_common_nonzero_idx]
            most_common_nonzero_count = counts[most_common_nonzero_idx]

            # Zero is most common if it has more occurrences
            if zero_count >= most_common_nonzero_count:
                return True, {
                    'most_common_value': 0,
                    'most_common_count': zero_count,
                    'total_values': total_values
                }
            else:
                return False, {
                    'most_common_value': most_common_nonzero,
                    'most_common_count': most_common_nonzero_count,
                    'total_values': total_values
                }
        else:
            # All zeros
            return True, {
                'most_common_value': 0,
                'most_common_count': zero_count,
                'total_values': total_values
            }
    else:
        # Dense array
        unique, counts = np.unique(layer_data, return_counts=True)
        most_common_idx = np.argmax(counts)
        most_common_value = unique[most_common_idx]
        most_common_count = counts[most_common_idx]

        is_sparse = (most_common_value == 0)

        return is_sparse, {
            'most_common_value': most_common_value,
            'most_common_count': most_common_count,
            'total_values': layer_data.size
        }


def check_column_cardinality(series: pd.Series, max_values: int) -> Tuple[int, bool]:
    """
    Check if a column has acceptable cardinality for categorical storage

    Args:
        series: Pandas Series (column data) - assumed to be a proper Series
        max_values: Maximum allowed unique values

    Returns:
        Tuple of (unique_count, is_acceptable)
    """
    unique_count = int(series.nunique())
    is_acceptable = bool(unique_count <= max_values)
    return unique_count, is_acceptable


def check_for_na(series: pd.Series) -> Tuple[bool, int, float]:
    """
    Check if column contains NA values

    Args:
        series: Pandas Series (column data) - assumed to be a proper Series

    Returns:
        Tuple of (has_na, na_count, na_percentage)
    """
    # Check for NA values - use bool() to ensure we get a Python bool
    na_mask = series.isna()
    has_na = bool(na_mask.any())
    na_count = int(na_mask.sum()) if has_na else 0
    na_percentage = (na_count / len(series) * 100) if len(series) > 0 else 0.0
    return has_na, na_count, na_percentage


def is_numerical_column(series: pd.Series) -> bool:
    """
    Check if column is numerical

    Args:
        series: Pandas Series (column data)

    Returns:
        True if numerical, False if categorical/text
    """
    return pd.api.types.is_numeric_dtype(series)


def get_h5ad_info(adata: anndata.AnnData) -> Dict[str, Any]:
    """
    Get summary information about h5ad file structure

    Args:
        adata: Loaded AnnData object

    Returns:
        Dictionary with summary information
    """
    info = {
        'n_cells': adata.n_obs,
        'n_genes': adata.n_vars,
        'has_X': adata.X is not None,
        'n_obs_columns': len(adata.obs.columns) if adata.obs is not None else 0,
        'n_var_columns': len(adata.var.columns) if adata.var is not None else 0,
        'layers': list(adata.layers.keys()) if adata.layers else [],
        'obsm_keys': list(adata.obsm.keys()) if adata.obsm else [],
        'varm_keys': list(adata.varm.keys()) if adata.varm else [],
        'uns_keys': list(adata.uns.keys()) if adata.uns else []
    }

    return info
