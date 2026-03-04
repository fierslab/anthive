"""
Layer statistics calculation for anthive

Functions to compute statistics on expression layers
"""

import numpy as np
from typing import Dict, Any


def calculate_layer_statistics(layer_data, layer_name: str) -> Dict[str, Any]:
    """
    Calculate statistics for an expression layer

    Args:
        layer_data: Layer data (numpy array or sparse matrix)
        layer_name: Name of the layer

    Returns:
        Dictionary with statistics
    """
    # Handle sparse matrices
    if hasattr(layer_data, 'toarray'):
        # Sparse matrix
        data_values = layer_data.data  # Non-zero values only
        total_elements = layer_data.shape[0] * layer_data.shape[1]
        num_zeros = total_elements - layer_data.nnz
        pct_zeros = (num_zeros / total_elements * 100) if total_elements > 0 else 0.0

        if len(data_values) > 0:
            min_nonzero = float(np.min(data_values))
            max_value = float(np.max(data_values))
            median = float(np.median(data_values))
            q90 = float(np.percentile(data_values, 90))

            # For sparse, we need to consider zeros for overall median/q90
            # But we report statistics on non-zero values separately
        else:
            # All zeros
            min_nonzero = 0.0
            max_value = 0.0
            median = 0.0
            q90 = 0.0

        min_value = 0.0  # Sparse matrices have implicit zeros
        dtype = str(layer_data.dtype)
    else:
        # Dense array
        flat_data = layer_data.flatten()
        num_zeros = int(np.sum(flat_data == 0))
        total_elements = len(flat_data)
        pct_zeros = (num_zeros / total_elements * 100) if total_elements > 0 else 0.0

        min_value = float(np.min(flat_data))
        max_value = float(np.max(flat_data))
        median = float(np.median(flat_data))
        q90 = float(np.percentile(flat_data, 90))

        # Min non-zero value
        nonzero_values = flat_data[flat_data != 0]
        if len(nonzero_values) > 0:
            min_nonzero = float(np.min(nonzero_values))
        else:
            min_nonzero = 0.0

        dtype = str(layer_data.dtype)

    return {
        'name': layer_name,
        'dtype': dtype,
        'min_value': min_value,
        'min_nonzero': min_nonzero,
        'max_value': max_value,
        'median': median,
        'q90': q90,
        'num_zeros': num_zeros,
        'pct_zeros': pct_zeros
    }


def format_number(value: float) -> str:
    """
    Format number for display in table

    Uses regular notation for most numbers, scientific notation only for
    very large (>= 1e6) or very small (< 1e-3 and != 0) numbers.

    Args:
        value: Number to format

    Returns:
        Formatted string
    """
    if value == 0:
        return "0"

    abs_value = abs(value)

    # Use scientific notation for very large or very small numbers
    if abs_value >= 1e6 or (abs_value < 1e-3 and abs_value != 0):
        return f"{value:.2e}"

    # For regular numbers, check if they're effectively integers
    if abs(value - round(value)) < 1e-9:
        # Value is essentially an integer
        return f"{int(round(value)):,}.0"
    else:
        # Has decimal component
        # Use appropriate decimal places based on magnitude
        if abs_value >= 100:
            return f"{value:,.1f}"
        elif abs_value >= 10:
            return f"{value:,.2f}"
        elif abs_value >= 1:
            return f"{value:,.3f}"
        else:
            return f"{value:,.4f}"


def format_statistics_table(stats_list: list) -> str:
    """
    Format layer statistics as a markdown table

    Args:
        stats_list: List of statistics dictionaries

    Returns:
        Markdown formatted table
    """
    if not stats_list:
        return ""

    lines = []
    lines.append("## Layer Statistics\n")
    lines.append("| Layer | dtype | Min | Min (non-zero) | Max | Median | Q90 | Zeros |")
    lines.append("|-------|-------|-----|----------------|-----|--------|-----|-------|")

    for stats in stats_list:
        lines.append(
            f"| {stats['name']} | {stats['dtype']} | "
            f"{format_number(stats['min_value'])} | {format_number(stats['min_nonzero'])} | "
            f"{format_number(stats['max_value'])} | {format_number(stats['median'])} | "
            f"{format_number(stats['q90'])} | {stats['num_zeros']:,} |"
        )

    return "\n".join(lines)
