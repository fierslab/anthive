---
name: anthive
description: >
  Query Anthive single-cell / bulk / spatial transcriptomics browser.
  Use when the user asks about gene expression, cell types, datasets,
  conditions, diseases, or any biological dataset question.
allowed-tools: Bash(curl *)
---

# Anthive REST API

Anthive is a single-cell, bulk, and spatial transcriptomics data browser.
The REST API serves DuckDB-backed datasets with gene expression, cell
metadata, embeddings, and server-side plotting.

## Authentication

All curl calls MUST use `-n` (reads `~/.netrc`). This works transparently
whether or not the server requires credentials:

```
curl -n -s ...
```

## Base URL

```
API=http://localhost:8080/api
```

Always set this variable at the start of a session. If the user provides
a different host, use that instead.

---

## Endpoints reference

### Discovery

**List all datasets**
```
curl -n -s "$API/databases" | jq .
```
Returns `{databases: [...], total: N}`. Each entry has `id`, `title`,
`n_cells`, `n_genes`, `layers`, `collection` (group, experiment, authors,
annotation).

**Dataset details** (always call before plotting or querying)
```
curl -n -s "$API/databases/$DB/info" | jq .
```
Returns layers, embeddings, obs fields (categorical + numerical), gene
count, collection metadata, annotation (organism, tissue, disease, etc.).

**Search genes**
```
curl -n -s "$API/databases/$DB/genes?q=APOE&limit=20" | jq .
```
Use `exact=true` for exact match. Always confirm gene names before use.

**Gene stats** (expression statistics for specific genes)
```
curl -n -s "$API/databases/$DB/gene_stats?genes=APOE,TREM2,CLU" | jq .
```
Returns per-gene: mean, std, pct_nonzero, median_nonzero, rank_mean, cv.

**Gene stats across ALL datasets**
```
curl -n -s "$API/gene_stats_all?genes=APOE,TREM2" | jq .
```

### Cell data

**Get cell table** (expression + metadata)
```
curl -n -s "$API/databases/$DB/cells?genes=APOE,TREM2&metadata=cell_type,condition&layer=X&limit=100" | jq .
```
- `metadata=*` for all fields
- `filter=cell_type:Microglia` for filtering
- `format=csv` or `format=parquet` for bulk download

### Plotting (returns image bytes)

The plot endpoint returns **raw image bytes** (PNG/SVG/PDF), not JSON.

**CRITICAL: how to handle plot responses**

1. Save to a temp file, then show the user:
```bash
curl -n -s "$API/databases/$DB/plot?geom=scatter&color=cell_type&sample=50000" \
  -o /tmp/plot.png
```
Then read the file with the Read tool to display it to the user.

2. The response header `X-Plot-Caption` contains a text description of
   the plot.

**Plot parameters:**
- `geom` (required): `scatter` | `violin` | `box` | `bar` | `histogram` | `ecdf` | `kde` | `heatmap`
- `x`: X-axis field (gene name, categorical column, embedding, or numerical column)
- `y`: Y-axis field
- `color`: Color-encoding field
- `layer`: Expression layer (default: `X`)
- `format`: `png` (default) | `svg` | `pdf`
- `width`, `height`, `dpi`: Figure dimensions
- `sample=N`: Random subsample (DuckDB reservoir sampling, applied after filters)
- `subset`: Repeatable cell filter. Formats:
  - `subset=sex:female` (exact match)
  - `subset=celltype:Microglia,Astrocyte` (IN list)
  - `subset=nCount_RNA:500..8000` (range)
  - Multiple subsets combine with AND

**Plot examples:**
```bash
# UMAP colored by cell type (subsampled)
curl -n -s "$API/databases/$DB/plot?geom=scatter&color=cell_type&sample=50000" -o /tmp/umap.png

# Violin plot of gene expression by condition
curl -n -s "$API/databases/$DB/plot?geom=violin&x=condition&y=APOE&layer=X" -o /tmp/violin.png

# Bar plot of cell counts per cluster
curl -n -s "$API/databases/$DB/plot?geom=bar&x=cell_type" -o /tmp/bar.png

# Heatmap of top genes by cell type
curl -n -s "$API/databases/$DB/plot?geom=heatmap&y=cell_type&n_genes=20" -o /tmp/heatmap.png

# Subset to female AD patients only
curl -n -s "$API/databases/$DB/plot?geom=violin&x=cell_type&y=TREM2&subset=sex:female&subset=condition:AD" -o /tmp/subset.png
```

**Recommended sample sizes:**

| geom | sample | reason |
|------|--------|--------|
| scatter / UMAP | 50000 | visually indistinguishable from full |
| violin / kde / ecdf | 20000 | distribution shape preserved |
| bar / heatmap | 10000 | adequate for means/counts |

### SQL analytics

For custom queries beyond what the standard endpoints provide.

**1. Inspect schema** (always call first)
```
curl -n -s "$API/databases/$DB/analytics/schema" | jq .
```
Returns all tables with column names, types, row counts, and sample rows.

**2. Run a query** (SELECT only, results saved server-side as Parquet)
```
curl -n -s -X POST "$API/databases/$DB/analytics/query" \
  -H "Content-Type: application/json" \
  -d '{"sql": "SELECT cell_type, AVG(value) as mean_expr FROM layer__X WHERE gene_name = '\''APOE'\'' GROUP BY 1"}' \
  | jq .
```
Returns `session_id`, column names, dtypes, row_count, and a preview.
Use the `session_id` in the next step for visualization.

**3. Visualize query results** (sandboxed Python: pandas, matplotlib, seaborn, numpy, scipy, sklearn)
```
curl -n -s -X POST "$API/analytics/viz" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "SESSION_ID", "code": "import seaborn as sns\nsns.barplot(data=df, x=\"cell_type\", y=\"mean_expr\")\nplt.savefig(OUTPUT_PATH, bbox_inches=\"tight\", dpi=150)"}' \
  | jq .
```
Returns `{figure_b64: "...", mime_type: "image/png"}`. To display:
```bash
curl -n -s -X POST "$API/analytics/viz" \
  -H "Content-Type: application/json" \
  -d '...' | jq -r .figure_b64 | base64 -d > /tmp/viz.png
```
Then read `/tmp/viz.png` with the Read tool.

The script has `df` (your query result as a DataFrame), `DATA_PATH`,
and `OUTPUT_PATH` pre-injected. The script MUST save to `OUTPUT_PATH`.

### Module scoring

Compute per-cell module scores (equivalent to Scanpy `score_genes` / Seurat `AddModuleScore`).
Returns summary statistics and a `session_id` for downstream visualization.

```bash
curl -n -s -X POST "$API/databases/$DB/module_score" \
  -H "Content-Type: application/json" \
  -d '{"genes": ["TREM2", "APOE", "SPP1", "CD9", "LPL", "CST7"]}' \
  | jq .
```

Returns: `session_id`, `genes_found`, `genes_missing`, `n_cells`, `n_control_genes`, `summary` (mean/std/median/min/max/q25/q75).

**Parameters:**
- `genes` (required): list of gene names in the module
- `layer`: expression layer (default `X`, should be log-normalized)
- `n_control`: control genes per expression bin (default 100)
- `seed`: random seed for reproducible control selection (default 42)
- `subset`: cell subset filters (same syntax as plot endpoint)

**Visualization:** Use the returned `session_id` with `analytics_viz` to plot:
```bash
curl -n -s -X POST "$API/analytics/viz" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "SESSION_ID", "code": "sns.histplot(df, x=\"module_score\", bins=50)\nplt.savefig(OUTPUT_PATH, bbox_inches=\"tight\", dpi=150)"}' \
  | jq -r .figure_b64 | base64 -d > /tmp/module_score.png
```

---

## Standard workflow

1. `$API/databases` — find the dataset
2. `$API/databases/$DB/info` — learn available fields, layers, embeddings
3. `$API/databases/$DB/genes?q=...` — confirm gene names
4. Plot or query as needed
5. For plots: save to `/tmp/`, then Read the file to show the user
6. For analytics_viz: decode base64 to file, then Read to show the user

## Important notes

- Gene names are case-sensitive in most operations. Use `search_genes`
  to confirm the exact spelling (APOE vs Apoe).
- The `$DB` variable is a path like `S25/SGM` or `Z25/L23`. URL-encode
  if needed, but curl handles `/` in paths correctly.
- Plot responses are binary images, not JSON. Always use `-o file.png`.
- analytics_viz returns base64 JSON, not binary. Decode with
  `jq -r .figure_b64 | base64 -d > file.png`.
