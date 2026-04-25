#!/usr/bin/env python3
"""Subset an h5ad file to microglia only.

Reads the h5ad, identifies microglia cells by scanning cell type annotation
columns, removes monocytes and macrophages, and writes a new h5ad.

Usage:
    python subset_microglia.py <input.h5ad> <output.h5ad> [--column COLUMN]
"""

import argparse
import sys
from pathlib import Path

import anndata as ad
import numpy as np


# Patterns that indicate microglia (case-insensitive substring match)
MICROGLIA_PATTERNS = [
    "microglia", "mg", "micro",
]

# Patterns for cell types to EXCLUDE (monocytes, macrophages, etc.)
EXCLUDE_PATTERNS = [
    "monocyte", "mono",
    "macrophage", "macro", "mdm",
    "dendritic", "dc",
    "neutrophil",
    "cam",  # CNS-associated macrophages
    "plasmablast",
    "t cell", "trm", "cd4", "cd8",
    "b cell",
    "nk cell",
    "erythrocyte",
    "endothelial", "pericyte", "fibroblast", "smc",
    "oligodendrocyte", "opc",
    "astrocyte", "bergmann",
    "neuron", "excitatory", "inhibitory", "granule",
    "schwann",
    "tumor",
]


def find_celltype_column(adata):
    """Find the most likely cell type annotation column."""
    candidates = []
    for col in adata.obs.columns:
        col_lower = col.lower()
        if any(kw in col_lower for kw in [
            "celltype", "cell_type", "cell.type",
            "annotation", "cluster", "ident",
            "supercluster", "type", "label",
        ]):
            n_unique = adata.obs[col].nunique()
            if 2 <= n_unique <= 500:
                candidates.append((col, n_unique))
    # Prefer columns with fewer unique values (higher-level annotations)
    candidates.sort(key=lambda x: x[1])
    return candidates


def is_microglia(value):
    """Check if a cell type label looks like microglia."""
    val_lower = str(value).lower().strip()
    return any(pat in val_lower for pat in MICROGLIA_PATTERNS)


def is_excluded(value):
    """Check if a cell type label is a non-microglia type to exclude."""
    val_lower = str(value).lower().strip()
    return any(pat in val_lower for pat in EXCLUDE_PATTERNS)


def main():
    parser = argparse.ArgumentParser(description="Subset h5ad to microglia only")
    parser.add_argument("input", help="Input h5ad file")
    parser.add_argument("output", help="Output h5ad file")
    parser.add_argument("--column", help="Cell type column name (auto-detected if omitted)")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be kept, don't write")
    args = parser.parse_args()

    print(f"Loading {args.input}...")
    adata = ad.read_h5ad(args.input)
    print(f"  {adata.n_obs} cells x {adata.n_vars} genes")

    if args.column:
        celltype_col = args.column
    else:
        candidates = find_celltype_column(adata)
        if not candidates:
            print("ERROR: No cell type column found. Use --column to specify.")
            sys.exit(1)
        print(f"\nCandidate cell type columns:")
        for col, n in candidates:
            print(f"  {col}: {n} unique values")
        celltype_col = candidates[0][0]

    print(f"\nUsing column: {celltype_col}")
    labels = adata.obs[celltype_col].astype(str)
    value_counts = labels.value_counts()

    print(f"\nAll labels in '{celltype_col}':")
    for label, count in value_counts.items():
        tag = ""
        if is_microglia(str(label)):
            tag = " <-- KEEP (microglia)"
        elif is_excluded(str(label)):
            tag = " <-- REMOVE"
        else:
            tag = " <-- KEEP (unknown, not excluded)"
        print(f"  {label}: {count}{tag}")

    # Keep microglia + anything not explicitly excluded
    # For a myeloid atlas, we want strict microglia-only: keep only microglia matches
    mask = labels.apply(is_microglia)
    n_keep = mask.sum()

    print(f"\nMicroglia cells: {n_keep} / {adata.n_obs} ({100*n_keep/adata.n_obs:.1f}%)")

    if n_keep == 0:
        print("WARNING: No microglia found. Check column name and labels.")
        print("Try --column with one of:", ", ".join(adata.obs.columns[:20]))
        sys.exit(1)

    if args.dry_run:
        print("Dry run — not writing output.")
        return

    adata_micro = adata[mask].copy()
    print(f"Writing {args.output}...")
    adata_micro.write_h5ad(args.output)
    print(f"Done. {adata_micro.n_obs} cells x {adata_micro.n_vars} genes")
    print(f"File size: {Path(args.output).stat().st_size / 1024 / 1024:.0f} MB")


if __name__ == "__main__":
    main()
