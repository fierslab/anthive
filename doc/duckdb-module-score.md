# Single-Cell Module Scoring Directly in DuckDB

## Context

When your single-cell count matrix lives in DuckDB in melted/long format (one row per cell-gene pair), you can compute gene module scores (equivalent to Scanpy's `sc.tl.score_genes` or Seurat's `AddModuleScore`) entirely in SQL without ever pivoting to a dense matrix. This is fast, memory-efficient, and scales well to millions of cells.

## Prerequisites

- A converted Anthive DuckDB with:
  - `__cells (cell_id, cell_name)`
  - `__genes (gene_id, gene_name)`
  - `__layer__X (cell_id, gene_id, value)` — log-normalized expression layer
  - `__gene_bins (gene_id, mean_expr, expr_bin)` — precomputed by `ant-duckdb-check`
- The `__gene_bins` table must have been built from the **log-normalized** layer
  (i.e. the `default_layer` set in the `.md` file)

## What a module score actually is

For each cell:
1. Compute the mean expression of genes in your module
2. Subtract the mean expression of a background/control gene set matched by average expression level

The control subtraction corrects for cells that are generally high-expressing. Both Scanpy and Seurat do this, differing only in minor details of how control genes are sampled.

## Step 1: Precompute gene expression bins (done by ant-duckdb-check)

`ant-duckdb-check` automatically builds `__gene_bins` based on the `default_layer`
detected from the database. The table has one row per gene (same count as `__genes`),
including genes with zero expression across all cells (they get `mean_expr = 0`, bin 1).

```sql
-- Structure created by ant-duckdb-check:
CREATE TABLE __gene_bins AS
WITH gene_means AS (
    SELECT g.gene_id, COALESCE(AVG(l.value), 0.0) AS mean_expr
    FROM __genes g
    LEFT JOIN __layer__X l USING (gene_id)
    GROUP BY g.gene_id
)
SELECT
    gene_id,
    mean_expr,
    NTILE(25) OVER (ORDER BY mean_expr) AS expr_bin
FROM gene_means
```

Only rebuild if the underlying count matrix changes.

## Step 2: Scoring function

```python
def score_module(con, module_genes: list[str], n_control: int = 100, seed: int = 42) -> pd.DataFrame:
    """
    Compute a module score per cell, equivalent to sc.tl.score_genes.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
    module_genes : list of gene names in the module
    n_control : number of control genes sampled per expression bin occupied
                by any module gene (see n_control semantics below)
    seed : integer seed for reproducible control gene selection

    Returns
    -------
    pandas DataFrame with columns [cell_id, module_score]
        All cells are present; cells expressing no module gene receive score 0.0.

    Requires
    --------
    Tables: __cells, __genes, __layer__X (or default_layer), __gene_bins
    """
    # Translate gene names → gene_ids (avoids SQL injection, uses integers throughout)
    placeholders = ', '.join('?' for _ in module_genes)
    gene_id_rows = con.execute(
        f"SELECT gene_id FROM __genes WHERE gene_name IN ({placeholders})",
        module_genes
    ).fetchall()
    module_gene_ids = [row[0] for row in gene_id_rows]

    if not module_gene_ids:
        raise ValueError(f"None of the module genes found in database: {module_genes}")

    missing = len(module_genes) - len(module_gene_ids)
    if missing:
        import warnings
        warnings.warn(f"{missing} module gene(s) not found in database and will be ignored")

    gene_id_list = ', '.join(str(gid) for gid in module_gene_ids)

    return con.execute(f"""
        WITH control AS (
            -- Sample n_control genes per occupied expression bin, excluding module genes.
            -- Uses md5() for deterministic, version-stable ordering (see notes below).
            SELECT gene_id FROM (
                SELECT
                    gb.gene_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY gb.expr_bin
                        ORDER BY md5(CAST(gb.gene_id AS VARCHAR) || CAST({seed} AS VARCHAR))
                    ) AS rn
                FROM __gene_bins gb
                WHERE gb.expr_bin IN (
                    SELECT DISTINCT expr_bin
                    FROM __gene_bins
                    WHERE gene_id IN ({gene_id_list})
                )
                AND gb.gene_id NOT IN ({gene_id_list})
            )
            WHERE rn <= {n_control}
        ),
        module_score AS (
            -- Mean expression of module genes per cell
            SELECT cell_id, AVG(value) AS m
            FROM __layer__X
            WHERE gene_id IN ({gene_id_list})
            GROUP BY cell_id
        ),
        control_score AS (
            -- Mean expression of control genes per cell
            SELECT cell_id, AVG(value) AS c
            FROM __layer__X
            WHERE gene_id IN (SELECT gene_id FROM control)
            GROUP BY cell_id
        )
        -- LEFT JOIN from __cells ensures all cells appear in output.
        -- Cells expressing no module gene get score 0.0.
        SELECT
            c.cell_id,
            COALESCE(ms.m, 0.0) - COALESCE(cs.c, 0.0) AS module_score
        FROM __cells c
        LEFT JOIN module_score  ms ON c.cell_id = ms.cell_id
        LEFT JOIN control_score cs ON c.cell_id = cs.cell_id
        WHERE ms.cell_id IS NOT NULL OR cs.cell_id IS NOT NULL
    """).fetchdf()
```

## Usage example

```python
import duckdb

con = duckdb.connect("my_atlas.db")

# Score a DAM/microglial activation module
dam_score = score_module(con, ["TREM2", "APOE", "SPP1", "CD9", "LPL", "CST7"])

# Score multiple modules
modules = {
    "dam":         ["TREM2", "APOE", "SPP1", "CD9", "LPL", "CST7"],
    "homeostatic": ["P2RY12", "CX3CR1", "TMEM119", "CSF1R"],
    "interferon":  ["IFIT1", "IFIT3", "ISG15", "MX1", "OAS1"],
}

import pandas as pd
scores = pd.DataFrame({"cell_id": dam_score["cell_id"]})
for name, genes in modules.items():
    s = score_module(con, genes, seed=hash(name) % 10000)
    scores = scores.merge(s.rename(columns={"module_score": name}), on="cell_id")
```

## Why this is fast

- A module score reduces to two `GROUP BY cell_id` aggregations on filtered row subsets, then a join.
- For a module of 10 genes + 100 control genes per bin, you scan maybe 0.5–3% of total rows.
- DuckDB's columnar engine parallelizes all of this automatically.
- You never materialize the full cells × genes dense matrix, so memory stays flat.

## Design notes

### md5() for reproducible control gene ordering
The original design used `HASH()` for deterministic shuffling. DuckDB's `HASH()` is an
internal function (currently MurmurHash64) and is **not guaranteed stable across DuckDB
versions**. Upgrading DuckDB could silently change which control genes are selected for
any given module, making results non-reproducible across environments.

`md5()` is a well-defined cryptographic standard, implemented identically in every DuckDB
version and every platform. Using `md5(CAST(gene_id AS VARCHAR) || CAST(seed AS VARCHAR))`
gives a stable hex string that sorts consistently. The seed still provides per-module
variation; changing it gives a different but equally valid draw.

### n_control semantics
`n_control` is the number of control genes sampled **per expression bin occupied by any
module gene** — not a total fixed count. If your module genes fall in 3 different
expression bins, the control set contains up to `3 × n_control` genes in total. In
practice, bins near the edges of the expression range may have fewer than `n_control`
eligible genes, so the actual count may be somewhat less.

This matches Scanpy's behavior (`sc.tl.score_genes` default: 50 control genes per bin).
Seurat's `AddModuleScore` samples control genes per individual module gene, which can
produce duplicates across bins; for most use cases the results are near-identical.

Recommended values:
- `n_control=50` — Scanpy default, faster
- `n_control=100` — Seurat default, slightly smoother background estimate

### Scanpy vs Seurat behavior
This implements the Scanpy-style approach (control genes sampled per expression bin
occupied by any module gene). For most practical purposes the results are near-identical
to Seurat.

### 25 bins
Matches the Scanpy default (`NTILE(25)`). Adjust in `ant-duckdb-check` if needed.

### Missing genes
If a module gene is absent from `__genes`, it is excluded before the SQL runs
(the Python layer translates names → IDs first and warns about missing genes).
Genes present in `__genes` but with zero expression in all cells are included in
`__gene_bins` at bin 1 with `mean_expr = 0`.

### REST API sanitization
When exposing module scoring via a REST endpoint, gene names must be passed as
parameterized query arguments (using `?` placeholders) — never interpolated directly
into the SQL string. The function above already does this for the name→ID lookup;
the subsequent SQL uses integer gene IDs only, which are safe to interpolate.
