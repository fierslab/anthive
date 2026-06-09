# Design — `.var` data and differential expression

**Status:** draft for review.
**Date:** 2026-06-09.
**Scope:** add two new types of gene-level data to the anthive stack:

1. **`.var` data** — per-gene scalar annotations (one row per gene).
   Infrastructure already exists in the duckdb (`__varnum` / `__varcat`);
   only API + UI + curation are missing.
2. **Differential expression** — sets of per-(study, term, contrast, gene)
   results from statistical models. New tables, new API, new tooling.

A concrete data sample (`/data/1/project/PRS/de_merge/merged_DE.csv`,
~3 M rows from a GLMM on Sierksma2025 Village.6M) drives the schema
decisions below — see [§ Worked example](#worked-example).

## Why two domains, not one

`.var` is a **wide** shape: each gene has scalar attributes (highly
variable, mean expression, biotype, chromosome, gene length, "is in
geneset X"). One row per gene; one column per attribute. Mirrors `.obs`
exactly.

DE is a **long** shape: each gene has results across many model terms
× contrasts, with rich per-result metadata (effect size, p-value,
confidence interval, sex-regressed parallels, etc.). Trying to flatten
DE into `.var` columns ("logFC_cluster_0", "logFC_cluster_1", … ×
N methods × N studies) explodes the column count and loses the
per-study metadata that makes the results interpretable.

So: separate tables, separate code paths, separate API endpoints. The
two domains share no schema and minimal code.

---

## Part 1 — `.var` scalars

### Storage (already implemented)

`bin/ingest/ant-convert` creates these tables when the `.md` has
`var_include: [...]`:

```
__varnum(gene_id INTEGER PRIMARY KEY, <numeric col>, …)
__varcat(gene_id INTEGER PRIMARY KEY, <varchar col>, …)
```

Plus views joined to `__genes`:

```sql
CREATE VIEW varnum AS
  SELECT g.gene_name, v.*  FROM __varnum v  JOIN __genes g USING (gene_id);
CREATE VIEW varcat AS
  SELECT g.gene_name, v.*  FROM __varcat v  JOIN __genes g USING (gene_id);
```

This mirrors `obsnum` / `obscat`. Already-working `ant-convert` code:
[`bin/ingest/ant-convert:675-780`](../bin/ingest/ant-convert).

### What's missing (small)

- **`/info` endpoint** currently always returns `var: {columns: [], total: 0}`. It needs to read `__varnum` / `__varcat` and report `var: { numerical: [...], categorical: [...], total: N }`.
- **`/databases/{id}/genes/{name}`** — extend the gene-info response with the `.var` columns for that gene.
- **`/databases/{id}/var/{field}`** — distribution of a single `.var` field (categorical → value counts; numerical → quantile summary). Mirror `/databases/{id}/obs/{field}`.
- **UI**: a "gene info" panel on `dataset.html` listing the `.var` columns; gene-side suggestion chips on `plot.html` (parallel to `top_cat` / `top_num`, curated via new `top_var_num` / `top_var_cat` `.md` fields).

### Catalog fields to add to the `.md`

Already present:

- `var_include: [...]` — which `.var` columns to ingest (existing).

To add:

- `top_var_num: [...]` — curated favourites for the gene-side chips (numerical).
- `top_var_cat: [...]` — curated favourites (categorical).

### Ingestion

The `.var` columns are read out of the h5ad's `adata.var` DataFrame
during `ant-convert`. No new ingester needed — the user adds the
columns to `var_include` in the `.md` and re-runs `ant-convert` once.

**`.var` does NOT support the same incremental-write workflow as
`.obs`**. Per-gene annotations are usually static properties of the
dataset (computed once during analysis), so the rebuild cost is paid
once. If incremental writes become a real need, mirror the
`AnnotationWriter` pattern from `lib/anthive_db.py` for `__varnum` /
`__varcat`.

---

## Part 2 — Differential expression

### Storage

Two new tables per duckdb. **One** rows table for ALL DE results
across ALL studies (not one table per study — see
[§ Why one rows table](#why-one-rows-table)).

```sql
__de_study  (one row per DE analysis)
─────────────────────────────────────────────
study_id        VARCHAR PRIMARY KEY,   -- slug, e.g. 'glmm_vsgrandmean_genes'
name            VARCHAR,               -- human-readable, e.g. 'DE interpretable glmm vs grandmean'
description     VARCHAR,               -- free-text method/intent description
method          VARCHAR,               -- 'wilcoxon' | 'glmm' | 'lmem' | 'deseq2_pseudobulk' | 'edger' | …
target          VARCHAR,               -- 'gene' | 'module' — which __genes table the gene_id resolves against
reference_type  VARCHAR,               -- 'vsgrandmean' | 'vsreference' | 'pairwise'
covariates      VARCHAR,               -- JSON list of model covariates (informational; for display)
n_tests_total   INTEGER,               -- e.g. 10000 genes or 13 modules tested
n_cells_total   INTEGER,               -- cells included in the fit
subset_filter   VARCHAR,               -- nullable; JSON describing any cell subset used
source          VARCHAR,               -- 'merged_DE.csv:DE interpretable glmm vs grandmean' | 'h5ad.uns.rank_genes_groups[leiden_r0.8]' | …
created_at      TIMESTAMP,
notes           VARCHAR
)


__de  (one row per (study, term, contrast, gene))
─────────────────────────────────────────────────────────
study_id   VARCHAR NOT NULL,           -- FK → __de_study
term       VARCHAR NOT NULL,           -- model term: 'patient_geno_short_relevel', 'batch_group', 'interaction', …
contrast   VARCHAR NOT NULL,           -- level within the term: 'Aipt_33', 'ASI021-024', 'AppNLGF - AppHuHu', …
gene_id    INTEGER NOT NULL,           -- FK → __genes (or modules in WGCNA_modules duckdbs)

-- Core stats (first-class, indexed)
log2fc     DOUBLE,                     -- nullable
zeff       DOUBLE,                     -- nullable
effsize    DOUBLE,                     -- COALESCE(log2fc, zeff) at ingest, for uniform sorts/filters
efftype    VARCHAR,                    -- 'log2fc' | 'zeff'
pvalue     DOUBLE,
padj       DOUBLE,

-- Original-scale model output (linear-predictor / model-native scale)
estimate   DOUBLE,                     -- model estimate before any transform (e.g. log-odds for GLMM)
SE         DOUBLE,                     -- standard error
ci_low     DOUBLE,                     -- 95% CI lower (estimate scale)
ci_high    DOUBLE,                     -- 95% CI upper

-- Method-specific extras — preserved but not first-class
stats_extra JSON,                       -- e.g. {coef_sexreg: ..., pval_sexreg: ..., stat: ...}

PRIMARY KEY (study_id, term, contrast, gene_id)
)

CREATE INDEX __de_padj   ON __de (study_id, term, contrast, padj);
CREATE INDEX __de_effect ON __de (study_id, term, contrast, effsize);
CREATE INDEX __de_gene   ON __de (gene_id, study_id);
```

Plus a denormalised view for clients:

```sql
CREATE VIEW de AS
  SELECT s.name AS study_name, s.method, s.target,
         d.term, d.contrast,
         g.gene_name,
         d.log2fc, d.zeff, d.effsize, d.efftype,
         d.pvalue, d.padj, d.estimate, d.SE, d.ci_low, d.ci_high,
         d.stats_extra
  FROM __de d
  JOIN __de_study s USING (study_id)
  JOIN __genes g USING (gene_id);
```

### Why three coordinates: `study_id` × `term` × `contrast`

In the worked example below, a single GLMM run produces **14 terms**
at once (intercept, batch_group, sex_mouse, Strain,
patient_geno_short_relevel, interaction, plus the `_sexreg` parallels).
Each term has 1–29 levels. Without a `term` column, that one analysis
would have to fragment into 14 separate "studies" — losing the natural
grouping and ballooning `__de_study`.

The decision: **`term` is the model covariate / predictor / interaction
the row applies to. `contrast` is the level within that term.** Both
are simple `VARCHAR`s — no enums, no schema-locked vocabulary.

### Decision on sex-regressed parallel terms (option A)

The example dataset has `*_sexreg` parallel results — the same
contrasts re-run with sex regressed out. Two ways to model these:

**A. Keep them as distinct `term` values.** A row's
`term = 'patient_geno_short_relevel'` and a paired row's
`term = 'patient_geno_short_relevel_sexreg'` coexist in the same study.
Queries that want both filter `WHERE term LIKE 'patient_geno_short_relevel%'`.

**B. Promote `sex_regressed` to a `BOOLEAN` column on `__de`.**

**Going with A.** It generalises (any "modeling variant" — sex-regressed,
covariate-adjusted, etc. — slots in as a term suffix without a schema
change), and the `_sexreg` naming convention is already established
upstream. The cost is `WHERE term LIKE '%_sexreg'` rather than
`WHERE sex_regressed = true`; acceptable.

### Why one rows table

- **Schema stability.** Adding a study is `INSERT INTO __de`, not `CREATE TABLE`. 10 or 100 studies, schema doesn't change.
- **Cross-study queries are SQL.** "Show me TREM2 across every analysis on this dataset" is one query against `__de`.
- **Two indexes cover the two main access patterns**: `(study_id, term, contrast, padj)` for "top genes for this contrast", `(gene_id, study_id)` for "this gene across all analyses". DuckDB's columnar storage + indexes handle ~3 M rows trivially.
- **No table proliferation.** A dataset with 50 DE studies still has exactly 2 DE tables.

### What's stored where (module-DE vs gene-DE)

DE rows index against the duckdb's `__genes` table via `gene_id`.

- **Gene-DE** rows live in the **gene duckdb** (e.g. `Village.6M.duckdb`).
- **Module-DE** rows live in the **modules duckdb** (e.g. `Village.6M.WGCNA_modules.duckdb`), where `__genes` holds the module names.

This keeps each duckdb self-contained — no cross-duckdb foreign keys —
and matches the existing pattern where gene-expression and module-score
datasets live in separate duckdbs.

`__de_study.target` is informational (`'gene'` or `'module'`); the
authoritative answer to "which `__genes` table?" is "this duckdb's".

---

## Part 3 — Persistence and the h5ad ⇄ duckdb relationship

### Decision: DE lives in the duckdb, NOT in the h5ad

For `.obs` annotations the pattern is:

```
ant-score writes to duckdb (fast)  ──▶  __pending_cols  ──▶  ant-merge-h5ad later
```

The duckdb is the **live** copy; the h5ad is the **eventual** copy of
record. This is justified for `.obs` because (1) downstream R / scanpy
workflows expect annotations on the h5ad and (2) `.obs` is small.

DE is different:

- It's **large** (the worked example is ~600 MB compressed for one
  Village.6M analysis).
- It does **not** naturally live in the h5ad. Scanpy stores its own
  `rank_genes_groups` in `adata.uns`, but external R / DESeq2 / GLMM
  pipelines produce CSV / parquet sidecars. There's no obvious h5ad
  field for "DE from any model".
- Round-tripping it through the h5ad would balloon the h5ad and require
  an arbitrary in-`uns` schema that nothing else reads.

**So DE does NOT round-trip to the h5ad.** No `__pending_cols` entry,
no `ant-merge-h5ad` reflection.

### Backup story — `.de.parquet` sidecar

The duckdb is **rebuilt from the h5ad by `ant-convert`** — so if DE
data lived only in the duckdb, a routine `ant-convert` (e.g. after
adding `.var` columns to `var_include`) would erase it. Unacceptable.

Mechanism:

1. When DE is ingested into the duckdb (`ant-de-ingest`, see below),
   the same data is **also** written to a sidecar:

   ```
   <basename>.de.parquet          ← per-study + per-row, all DE for this dataset
   ```

   Schema: same as the `de` view above. Single parquet file per
   dataset; the `study_id` column carries the study membership.

2. On `ant-convert`, after the normal rebuild, **if a
   `<basename>.de.parquet` sidecar exists**, ant-convert ingests it
   automatically — re-populating `__de_study` + `__de` from the
   sidecar. Same path as the manual ingester (`ant-de-ingest` below);
   ant-convert just calls it.

3. `ant-sync` includes `.de.parquet` in the S3 set (alongside `.h5ad`,
   `.duckdb`). The sidecar is the **durable** copy; the duckdb's DE
   tables are the **served** derivative.

End result: DE survives duckdb rebuilds, gets backed up to S3, and is
trivial to inspect outside anthive (it's just a parquet file —
`pd.read_parquet()`, `duckdb.read_parquet()`, etc.).

### Why a sidecar parquet and not just the duckdb

Three reasons:

1. **Survives rebuilds.** ant-convert wipes and recreates the duckdb's
   tables. A sidecar is the only safe place to keep DE results when
   the duckdb is regenerated.
2. **Portable.** A consumer (a notebook, an R analyst) can read the
   parquet directly without going through ant-serve.
3. **Versionable.** Compressed parquet is small enough to keep
   multiple versions if needed (e.g. `<basename>.de.v1.parquet`).

---

## Part 4 — Tooling

Three new scripts under `bin/annot/`, all with the same `db_id`-based
CLI and the same coordination dance with `ant-serve` (`POST
/admin/release/{db_id}` → write → `POST /admin/restore/{db_id}`).

### `ant-de-wilcoxon` — scanpy Wilcoxon DE on a categorical column

```sh
ant-de-wilcoxon S25/V6W \
    --group-by leiden_r0.8 \
    [--reference rest] \
    [--method wilcoxon|t-test|logreg]   # passed through to sc.tl.rank_genes_groups
    [--n-top-genes 10000]                # limit; default: all genes
    [--name leiden_r0.8_wilcoxon]        # study_id; default: '<group_by>_<method>'
    [--subset cell_type:Microglia]       # optional cell subset
```

Steps:

1. Load the h5ad backed; subset cells if `--subset` given.
2. Call `sc.tl.rank_genes_groups(adata, groupby=GROUP_BY, method=METHOD, reference=REFERENCE)`.
3. Reshape the output (`adata.uns['rank_genes_groups']`) into the long
   `__de` row format. One `__de_study` row, N contrasts (one per group
   level), M genes each → ~N×M rows.
4. Write the sidecar `<basename>.de.parquet` (appending to any existing
   one — same `study_id` upserts, not duplicates).
5. Ingest into the duckdb via `ant-de-ingest` (below).

Trivial dependencies: just `scanpy` + `pandas` + `pyarrow`. No R, no
DESeq2.

### `ant-de-pseudobulk` — DESeq2 pseudobulk DE on a categorical column

```sh
ant-de-pseudobulk S25/V6W \
    --group-by Strain \
    --pseudobulk-by patient_geno_short  # sum counts per donor before DE
    [--covariates sex_mouse,batch_group]
    [--method deseq2|edger]              # default: deseq2 via pydeseq2
    [--reference AppHuHu]
    [--name strain_pseudobulk_deseq2]
```

Steps:

1. Load the h5ad backed. Read the raw counts layer (`layers['counts']`,
   per the ingestion contract).
2. Aggregate cells by `--pseudobulk-by` (typically donor-level) → a
   `(n_donors × n_genes)` count matrix.
3. Run DESeq2 via [`pydeseq2`](https://pydeseq2.readthedocs.io/) — pure
   Python, default. `--engine deseq2-r` (R shell-out) is reserved as a
   future opt-in for projects that need bit-for-bit R-DESeq2
   reproducibility; not implemented in v1.
4. Reshape, write sidecar, ingest — same as `ant-de-wilcoxon` step 4–5.

Single-cell DE is increasingly recommended to be done on **pseudobulk**
(Squair et al. 2021, Nat Comms — "Confronting false discoveries in
single-cell differential expression"). This tool is the easy path to
that recommended workflow.

### `ant-de-ingest` — friendly ingestion of a DE result file

The shared backend; the two analysis tools above feed into it, plus it
handles ad-hoc imports from user-supplied CSV / parquet (DESeq2 / edgeR
/ scanpy outputs, the worked example below, etc.).

#### UX principles

The tool is **friendly but never guesses**. Two rules:

1. **Be helpful** — for every required input, surface what valid values
   look like and give one clear example. Built-in presets cover the
   common cases (default DESeq2 / edgeR / scanpy outputs).
2. **Be explicit about ambiguity** — if a column could mean two things,
   stop and ask. Don't silently pick. The error message names the
   column, lists the candidates, and shows the exact flag to set.

This means: errors include *the next command to run*, not just *what
went wrong*.

#### Built-in presets

```sh
ant-de-ingest <db_id> --preset <name> --from <file> --study-id <slug> [...]
```

Presets ship column-name mappings for the common DE-tool outputs.
Each preset can be overridden field-by-field with `--map`; presets are
just shortcuts for "the column-name conventions this tool emits".

| Preset | Source | Notes |
| --- | --- | --- |
| `deseq2` | `pydeseq2` / DESeq2 R `results()` (CSV per contrast) | One file = one contrast; user passes `--term` and `--contrast` explicitly or supplies a folder of files via `--from-dir` (filename = contrast). |
| `edger` | edgeR `topTags()$table` (CSV per contrast) | Same shape as DESeq2; different column names (`logFC`, `PValue`, `FDR`). |
| `scanpy` | `sc.tl.rank_genes_groups()` exported via `sc.get.rank_genes_groups_df()` | Has a `group` column, so one file = many contrasts; `--term` is the obs column tested, `contrast` reads from `group`. |
| `long` | A long-format CSV with explicit `study/term/contrast/gene/...` columns (the merged_DE.csv shape) | The "I built my own format" path; relies on `--map` for renames. |

The preset list is **discoverable**:

```sh
ant-de-ingest --list-presets
```

prints each preset's expected columns, the canonical anthive column it
maps to, and a one-line description.

#### CLI shape

```sh
ant-de-ingest <db_id>                          # e.g. S25/V6W (required)
    --from <file>                              # CSV or parquet (required)
    [--from-dir <dir>]                         # alternative: a directory of per-contrast files (DESeq2/edgeR style)
    --preset deseq2|edger|scanpy|long          # which tool produced the file (REQUIRED — no default; we don't guess)
    [--study-id <slug>]                        # default: file stem ('merged_DE.csv' → 'merged_DE'). Override with --study-id.
    [--study-id-from <column>]                 # for long-format files with a 'model'/'study' column — one input file → many studies
    [--method <name>]                          # e.g. 'wilcoxon', 'deseq2', 'glmm'. Required for new studies; preset suggests a default.
    [--target gene|module]                     # default: 'gene'.
    [--reference-type vsgrandmean|vsreference|pairwise]   # default depends on preset (vsreference for deseq2/edger, none for scanpy rank_genes_groups)
    [--term <name>]                            # required for presets where the file is "one term, many contrasts" (deseq2, edger, scanpy)
    [--contrast <name>]                        # required for presets where the file is a single contrast (one-file deseq2/edger)
    [--name <human-readable>]                  # default: --study-id
    [--description <text>]                     # default: empty
    [--covariates <csv-list>]                  # informational, stored on __de_study
    [--map src=dst,src=dst,...]                # explicit column rename, overrides the preset's defaults field-by-field
    [--filter "<SQL WHERE clause>"]            # row-level filter applied at read time (e.g. --filter "model = 'glmm'")
    [--dry-run]                                # parse + validate only, don't write
    [--force-mismatch]                         # bypass the gene-namespace bail-out (use only when you're sure)
    [--replace]                                # delete an existing study_id before writing
```

Plus two informational subcommands:

```sh
ant-de-ingest --list-presets                   # print each preset's expected columns + a one-liner
ant-de-ingest --help-preset deseq2             # detailed help for one preset (mapping, required flags, example)
```

#### Strict-but-helpful validation

The ingester runs validation **before** any write and exits clean on
failure with a message that tells you the next step.

What it checks (in order):

1. **Preset chosen**: `--preset` is required. If omitted, error names
   all available presets and tells you which one fits your file based
   on its column names. (Detection is informational only; the user
   still has to pass the flag explicitly.)

2. **Required columns present**: per the preset, the file must have
   the expected source columns. Missing columns produce:

   ```
   ant-de-ingest: preset 'deseq2' expects columns
                  ['gene', 'log2FoldChange', 'pvalue', 'padj', 'baseMean', 'lfcSE']
                  but your file is missing: ['baseMean'].
                  If your file uses a different column name for the
                  base-mean expression, map it with:
                    --map baseMean=<your_column>
                  Or, if your file doesn't have it, that's fine — pass:
                    --map baseMean=
                  to drop it.
   ```

3. **Required CLI args**: if the preset is `deseq2` or `edger` (one
   file = one contrast), the ingester refuses unless `--term` and
   `--contrast` are passed. The error names both flags and tells you
   to look at the file's column / filename to pick values.

4. **`--method` required for new studies**: if the resolved `study_id`
   doesn't exist in `__de_study` yet, `--method` is required. The error
   lists the methods the preset suggests (`'deseq2'` for `--preset
   deseq2`, etc.) so the user can just copy-paste.

5. **Gene-namespace sanity**: validate gene names against the duckdb's
   `__genes`. If <10% match, fail with:

   ```
   ant-de-ingest: only 47 / 30000 gene names matched the dataset
                  (expected ≥10%).  Likely causes:
                    - DE was computed against a different gene-name
                      namespace (Ensembl ID vs HGNC symbol).
                    - --target should be 'module' not 'gene' (or vice versa).
                  Examples of unmatched DE genes: ENSG00000142192, ENSG00000142168, ...
                  Examples of dataset __genes:     APP, APOE, TREM2, ...
                  To bypass this check (use carefully):
                    --force-mismatch
   ```

   Warning (not error) if 10–95% match — partial matches probably mean
   a gene-filter step but worth flagging the count.

6. **Contrast / term resolution**: for `scanpy` / `long` presets, if the
   file has a `term` value that names an obs column, validate that the
   `contrast` values are a subset of that obs column's levels. Mismatch
   is a warning, not an error (model terms like `interaction` legitimately
   don't map to obs levels).

7. **`study_id` collision**: if `study_id` already exists and the new
   data has different `__de_study` metadata (different method, different
   covariates), fail with:

   ```
   ant-de-ingest: study_id 'leiden_r0.8_wilcoxon' already exists with
                  method='wilcoxon', n_tests_total=18000, created_at=...
                  Your new input would change those values. Either:
                    --study-id <new-slug>          # ingest as a separate study
                    --replace                      # delete & re-ingest this study
   ```

   Re-ingesting the same data into the same `study_id` (idempotent
   UPSERT) is fine and silent.

#### Steps (after validation passes)

1. Read CSV / parquet via DuckDB (no pandas memory blow-up).
2. Apply the preset's column mapping plus any `--map` overrides.
3. Compute `effsize` = `COALESCE(log2fc, zeff)` at ingest.
4. For `long` presets with `--study-id-from <col>`, split the input by
   that column and ingest as multiple studies (each file pass creates
   N study rows).
5. Write rows to `__de_study` + `__de`. UPSERT on
   `(study_id, term, contrast, gene_id)`.
6. Write the sidecar `<basename>.de.parquet` (append; the parquet has
   `study_id` so multiple studies coexist in one file).
7. Print a summary:

   ```
   ✓ Ingested 2,980,000 rows into __de (study='glmm_vsgrandmean_genes',
     14 terms, 154 contrasts, 10,000 genes).
   ✓ Wrote sidecar: data/h5adstore/Sierksma2025/Village.6M/Village.6M.de.parquet (632 MB).
     `ant-sync push` will upload it.
   ```

### Optional: `ant-de-export` — read DE out

```sh
ant-de-export S25/V6W [--study STUDY_ID] [--format csv|parquet] [-o OUT.csv]
```

Dumps the `de` view to a file. Reverse of `ant-de-ingest`. Useful for
sanity checks and for re-deriving the parquet sidecar if it's lost.

---

## Part 5 — API surface

```
GET /databases/{id}/var
   → {numerical: [...], categorical: [...], total: N}
   (or fold into /info as an enriched `var` block)

GET /databases/{id}/var/{field}
   → distribution of one .var column (value-counts | quantile summary)

GET /databases/{id}/de
   → list of __de_study rows
   → [{study_id, name, method, target, reference_type, n_tests_total, n_cells_total,
       n_terms, n_contrasts, created_at, ...}]

GET /databases/{id}/de/{study_id}
   → study metadata + list of (term, n_contrasts) tuples

GET /databases/{id}/de/{study_id}/terms/{term}
   → list of contrasts within this term + per-contrast n_sig counts

GET /databases/{id}/de/{study_id}/terms/{term}/contrasts/{contrast}
   ?sort=padj|effsize|estimate&direction=up|down|both
   ?padj_max=0.05&abs_log2fc_min=0.5
   ?limit=200&offset=0
   → paged gene results (sortable, filterable)

GET /databases/{id}/de/by-gene/{gene}
   → all rows for one gene across every study/term/contrast on this dataset
   (forest-plot data)

GET /databases/{id}/de/top
   ?study_id=&term=&contrast=&n=20&direction=up|down|both
   → top-N convenience endpoint (combines /terms/{term}/contrasts/{contrast} + sort + limit)
```

These slot into the per-geom endpoint design (REST API 2.2) — new
prefix, same conventions (subset filtering, format selection, etc.).

---

## Part 6 — Plot integration

Three new geoms unlock once DE is queryable:

- **`volcano`** — `x=log2fc`, `y=-log10(padj)`. Inputs: `study_id`,
  `term`, `contrast`. Top-N gene labels above thresholds.
- **`ma`** — `x=base_mean` (from `__de.stats_extra` or
  `__genes.mean_expression` if present), `y=log2fc`. Same inputs.
- **`forest`** — one gene across many contrasts. `y=estimate ± ci`,
  one row per (study, term, contrast). Inputs: a gene name.

These follow the per-geom endpoint pattern:

```
GET /databases/{id}/plot/volcano?study=…&term=…&contrast=…&pvalue_threshold=…
GET /databases/{id}/plot/ma?study=…&term=…&contrast=…
GET /databases/{id}/plot/forest?gene=…&studies=…
```

---

## Part 7 — UI integration (`web/`)

- **`dataset.html`**: a new "DE" panel below the obs / var panels.
  Lists available studies with their key metadata (method, n_terms,
  n_contrasts). Each entry deep-links into a study browser.
- **A new `de_browser.html` page** at
  `/browse/{group}/{file}/de/{study}` showing the term → contrast
  hierarchy + a sortable / filterable gene table for the selected
  contrast. Top-N genes deep-link to:
  - `/plot/volcano` (the contrast's volcano)
  - `/plot/scatter?color=GENE` (gene expression on the embedding)
- **`plot.html`** gains the three new geom forms. The form for
  `volcano` / `ma` requires `study_id` + `term` + `contrast` selectors,
  populated from `/de/{study}/terms` on dataset load.

---

## Part 8 — Curation

New `.md` catalog fields:

```yaml
top_var_num: []     # gene-side numerical chips
top_var_cat: []     # gene-side categorical chips

default_de_study: leiden_r0.8_wilcoxon   # which study to show on the dataset page by default
top_de_terms:                            # which terms appear in the dataset summary
  - patient_geno_short_relevel
  - interaction
```

These are advisory — the API exposes everything; the curation just
shapes the default UI.

---

## Worked example

`/data/1/project/PRS/de_merge/merged_DE.csv` — a real GLMM run on
Sierksma2025 Village.6M (`S25/V6W`):

- **2 models** → 2 `__de_study` rows
  - `glmm_vsgrandmean_genes` (method=`glmm`, target=`gene`, n_tests_total=10000)
  - `lmem_vsgrandmean_modules` (method=`lmem`, target=`module`, n_tests_total=13)
- **14 terms** per study: intercept, batch_group, sex_mouse, Strain,
  patient_geno_short_relevel, interaction, intplusstrain, plus 5
  `_sexreg` variants and 2 `*_huhu_specific`, `*_nlgf_specific`.
- **1–29 contrasts** per term (depends on the term).
- ~3 M rows total in `__de` (across both studies).

Cross-validation against the `.h5ad`:

- 10,000 DE genes ⊆ 25,826 h5ad `var_names`: **all present**.
- 29 `patient_geno_short_relevel` levels == 29 unique values in h5ad
  `obs.patient_geno_short`: **exact match**.
- 5 `batch_group` levels (DE) + reference `ASI017-020` == 6 h5ad obs
  values: **clean, reference omitted as expected**.
- 13 module names == `WGCNA_modules.h5ad.var_names`: **exact match**.

This file is **a one-off custom format** (not something a tool produced
out of the box) — it goes through the `long` preset with explicit column
maps. We will import it as a test case, but this exact shape is not
expected to recur and the ingester is not tuned for it.

Ingestion path:

1. Place `merged_DE.csv` alongside the dataset (or anywhere reachable).
2. Run the ingester for the gene-DE rows:

   ```sh
   ant-de-ingest S25/V6W \
       --preset long \
       --from /data/1/project/PRS/de_merge/merged_DE.csv \
       --study-id-from model \
       --target gene \
       --method glmm \
       --reference-type vsgrandmean \
       --map gene=gene,term=variable,contrast=level,\
log2fc=log2fc,pvalue=pval,padj=pval_adj,\
estimate=estimate_og,SE=SE_og,\
ci_low=ninetyfiveL_og,ci_high=ninetyfiveH_og \
       --filter "model = 'DE interpretable glmm vs grandmean'"
   ```

3. Same again for the module-DE rows, against the modules duckdb:

   ```sh
   ant-de-ingest S25/V6W_WGCNA_modules \
       --preset long \
       --from /data/1/project/PRS/de_merge/merged_DE.csv \
       --study-id-from model \
       --target module \
       --method lmem \
       --reference-type vsgrandmean \
       --map [same as above] \
       --filter "model = 'Module DE interpretable lmem vs grandmean'"
   ```

4. The ingester:
   - Reads the CSV via DuckDB (handles the 1 GB file without pandas
     memory blow-up).
   - Splits into 2 studies (one per unique `model`).
   - Validates gene names against each duckdb's `__genes`.
   - Writes the `.de.parquet` sidecar next to each h5ad.
5. The data is now queryable via `/api/databases/S25/V6W/de`.

(The `--filter` flag — a SQL `WHERE` clause applied at read-time — is
the way to subset a long-format file. Useful here because gene-DE and
module-DE share one CSV but route to different duckdbs.)

Patient-level data (`patientleveldata.parquet`, 31 rows × 189 cols) is
**out of scope** for this design. It's per-donor PRS + summary data,
not DE. Best home: a separate "donor metadata" facility (TBD).

---

## Out of scope

- **Per-donor / per-sample annotation** (the `patientleveldata.parquet`
  case). Needs its own design.
- **Cross-dataset DE comparisons.** Joining DE across datasets requires
  symbol-based gene matching (not the dataset-local `gene_id`) and a
  meta-store; defer.
- **Incremental DE updates** in place. DE rows are append-only; if you
  want to re-run a study, re-ingest with the same `study_id` (UPSERT
  semantics replace the rows).

---

## Resolved design decisions

1. **Pseudobulk engine.** `pydeseq2` is the default and only engine in
   v1. The R shell-out (`--engine deseq2-r`) is reserved as a future
   opt-in for projects that need bit-for-bit R-DESeq2 reproducibility;
   the CLI flag is documented but exits with a "not yet implemented"
   message in v1.

2. **Sidecar location.** Right next to the `.h5ad`:
   `<basename>.de.parquet`. Matches the existing `.h5ad.mus`, `.md`,
   `.duckdb` symmetry.

3. **`study_id` default.** **The file stem.** `merged_DE.csv`
   → `study_id='merged_DE'`. Override with `--study-id <slug>`.
   For files with a study/model column (long format),
   `--study-id-from <column>` splits the file into multiple studies
   named after the column values.

4. **Format support — presets, not auto-detection.** v1 ships four
   built-in presets covering the standard cases:

   - `deseq2` — pydeseq2 / R DESeq2 `results()` output.
   - `edger` — edgeR `topTags()$table` output.
   - `scanpy` — `sc.get.rank_genes_groups_df()` output.
   - `long` — arbitrary long-format CSV with explicit columns.

   `--preset` is **required**; the tool does NOT try to guess from
   file contents. If the user doesn't pass it, the error names the
   four presets and shows which expected columns are present in the
   file (so picking is mechanical, not divination).

   Extensibility for new variants: a new preset is ~20 lines (a dict
   of source-name → anthive-name mappings + a method default + a
   shape declaration: "one file = one contrast" vs "one file = many
   contrasts"). Drop it into `lib/de_presets.py`, list it in `--help`,
   done.

5. **UX rules — friendly defaults, explicit choices, no guessing.**
   - For every required input, errors show *the next command to run*
     (named flag + valid values + example), not just *what went wrong*.
   - Ambiguity is fatal until resolved by the user. "Two columns could
     be the gene name" → stop, list both, point at `--map gene=<your_column>`.
   - `--dry-run` parses + validates the full pipeline without writing,
     so users can iterate on flags cheaply.
   - The presets give defaults for the **column mapping**, never for
     the **semantic decisions** (study_id, term, method). Those stay
     user-supplied; the preset only suggests defaults in error
     messages.
