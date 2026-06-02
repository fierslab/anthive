# Single-cell `.h5ad` ingestion specification

**Audience:** anyone preparing a single-cell `.h5ad` file for deposit into the
downstream catalog. Read top to bottom before exporting an h5ad — the hard
requirements are non-negotiable and a wrong file will be rejected at intake.

**Scope:** the structure of the `.h5ad` file itself and the metadata that
must accompany it. Stops at the point where the file leaves your hands.

---

## 1. Hard requirements

A deposit is rejected outright if any of these are missing or wrong. They
are checked mechanically; there is no manual override.

### 1.1 Expression matrices — STRICT

Two matrices must exist, in two specific places:

| Slot | Content | Dtype |
|--------------------------|----------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------|
| `adata.X` | **log-normalized expression** (typically `log1p(CPM/scale_factor)` from scanpy / Seurat `NormalizeData`) | float32 or float64. Lowest value MUST be zero! |
| `adata.layers["counts"]` | **raw integer counts** | int (int32 / int64), or float-typed but integer-valued. Lowest value MUST be zero! |

Why both:
- Log-norm in `X` powers expression visualisation and gene-set scoring.
- Raw counts in `layers["counts"]` power any tool that performs its own
  normalisation (starCAT, scVI, scrublet, doublet detection, …) — feeding
  them log-norm produces nonsense.

Hard refusals:
- `adata.X is None` (Seurat → AnnData converters sometimes leave this
  empty — populate it before export).
- `layers["counts"]` missing.
- `layers["counts"]` exists but values are not integer-valued (e.g. it
  contains SCTransform output by mistake).
- `X` looks integer-valued (i.e. raw counts were left in `X` and not
  normalised).

### 1.2 Embeddings

A 2-D embedding for visualisation must exist:

- `adata.obsm["X_umap"]` or `adata.obsm["X_tsne"]` — one of these is required.
- Other embeddings (`X_pca`, `X_scvi`, …) are welcome but optional.

Why: every viewer / report assumes `X_umap`. A dataset without it cannot be
spatially inspected and will be rejected.

### 1.3 Cell and gene identifiers

- `adata.obs_names` must be **unique strings**. Duplicates break joins and
  every downstream tool.
- `adata.var_names` must be **unique strings** carrying gene symbols (HGNC
  for human, MGI for mouse). Use `adata.var_names_make_unique()` before
  export.
- No obs or var column may use these reserved names: `cell_id`, `cell_name`,
  `gene_id`, `gene_name`.
- `obs_names` and `var_names` are stable across re-exports of the same
  dataset (cell order may change but the identifier set should not). Rename
  cells *before* the first deposit, not after.

### 1.4 File format

- File is a valid `.h5ad` readable by `anndata.read_h5ad()` from anndata
  ≥ 0.10.
- File extension is `.h5ad` (lowercase).
- One coherent biological dataset per file. Multi-batch concatenations are
  allowed only if all batches share the same preparation (e.g. all
  snRNA-seq from the same study); never mix nuclei and whole-cell in one
  file.

### 1.5 Metadata sidecar

Every deposit ships with a sibling **`<basename>.h5ad.mus`** sidecar — one
per `.h5ad`, same directory. Optional folder-level **`.env`** files at any
parent folder carry cascade-able defaults (deeper overrides shallower;
sidecar values **replace** cascade values per key).

Both files are flat **`KEY=VALUE`**, one per line. List values are
comma-separated; items containing commas are double-quoted. See **§4** for
the schema and template; fields marked `# REQUIRED` there are hard
requirements (deposit refused if missing). Soft fields are still expected
unless there is a good reason to omit them.

---

## 2. Soft requirements (strongly recommended; warnings on deposit)

These are not blockers but they materially limit what downstream analyses
can do. Provide them unless there is a good reason not to.

### 2.1 Gene-identifier consistency
- ≥ 95 % of `var_names` should be canonical gene symbols of the declared
  organism. Mixed (some Ensembl IDs that never mapped to symbols) is
  tolerated but flagged. A 100 % Ensembl-only file should be remapped
  *before* deposit. Ensembl id's can be stored in .var.

### 2.2 Provenance
- `doi` of the publication if any.
- `year` of data generation.
- `contributor` — who prepared this deposit.

### 2.3 Variable annotations
- `adata.var` columns describing genes (e.g. `gene_biotype`,
  `highly_variable`, `mt`) — pass through if present, don't strip.

---

## 3. Controlled vocabulary

The canonical source of truth is the **mus joint vocabulary**:

> **https://codeberg.org/mfiers/mus/raw/branch/main/vocabulary/main.yaml**
> (schema_version 2)

This single file is shared across labs and tooling; do not maintain a
local fork. The `mus` CLI fetches it with a 24-hour cache; you can
inspect the active vocabulary with:

```
mus vocab
mus vocab --refresh   # force re-pull from upstream
```

### Matching rules
- **Case-insensitive.**
- `_` ≡ ` ` (space) ≡ `-`. So `prefrontal_cortex`, `prefrontal cortex`,
  and `Prefrontal-Cortex` all match the same entry.
- Any value under vocabulary control that isn't in its list is a HARD
  finding. Don't invent values — open a PR to mus instead.

### Field summary (full lists in the canonical YAML)

| Field | Controlled? | Notes |
| --- | --- | --- |
| `organism` | yes | human, mouse, rat, macaque, zebrafish, drosophila |
| `tissue` | yes | UBERON-aligned (brain, prefrontal cortex, hippocampus, …) |
| `cell_type` | yes | CL-aligned (microglial cell, astrocyte, neuron, …) |
| `technology` | yes | 10x_chromium_3p_v3, Smart-seq2, NovaST, MERFISH, bulk_RNA-seq, … |
| `suspension_type` | yes | `cell` / `nucleus` / `na` |
| `access` | yes | `internal` / `public` |
| `source` | yes | `internal` / `external` |
| `sex` | yes | `male` / `female` / `mixed` / `unknown` |
| `disease` | yes | MONDO-aligned (normal, Alzheimer disease, …) |
| `condition` | no | freeform |
| `age`, `strain`, `accession`, `doi`, `pmid`, `year`, `contributor`, `title`, `link`, `license`, `source_description` | no | freeform |

---

## 4. Metadata layout (`.h5ad.mus` + `.env`)

Two file types carry the metadata. Both are flat `KEY=VALUE`, one per
line. List values are comma-separated; items containing commas must be
double-quoted (e.g. MONDO labels like `"leukoencephalopathy, diffuse
hereditary, with spheroids 1"`). Comments start with `#`.

### 4.1 Per-folder defaults: `.env`

Drop a `.env` file in each folder where you want to set defaults that
apply to every deposit under it. Cascades top-down (deeper folders'
`.env` overrides shallower; the sidecar overrides the cascade per key).

```ini
# Sierksma2025/.env — applies to every dataset in this study
access=internal
source=internal
year=2026
organism=human
contributor=Jane Doe <jane@lab.example>
```

Removal in cascade (rare): a list value prefixed with `-` removes a
previously-added item, e.g. `disease=-normal` drops `normal` from an
inherited list.

### 4.2 Per-file sidecar: `<basename>.h5ad.mus`

Sits next to each `.h5ad` data file with the same basename plus the
`.mus` suffix (so `Pharmacogenomics.h5ad` → `Pharmacogenomics.h5ad.mus`).
A key present here **replaces** the cascade value — sidecar is per-file
authority.

```ini
# Sierksma2025/Pharmacogenomics.h5ad.mus

# --- Biology (controlled values, see §3) ------------------------------
organism=human                                          # REQUIRED
cell_type=microglial cell                               # REQUIRED — list ok (commas)
technology=10x_chromium_3p_v3                           # REQUIRED
suspension_type=nucleus                                 # REQUIRED — cell | nucleus | na
tissue=brain,prefrontal cortex                          # REQUIRED — list ok
disease=Alzheimer disease,normal                        # soft
sex=mixed
condition=control,APOE4 carrier                         # freeform list
age=65,72,80                                            # freeform list

# --- Provenance --------------------------------------------------------
access=public                                           # REQUIRED — internal | public
source=external                                         # REQUIRED — internal | external
accession=GSE123456                                     # GEO/SRA/BioProject/dbGaP id
doi=10.1038/s41586-024-XXXXX-X
license=CC-BY-4.0

# --- File-level metadata (depositor pre-fills) ------------------------
title=Microglia from PFC of AD patients
sha256=e910ea986e8b2d32103e5c8305fcd123751eacf7c719a0901ad982930c14c3fa
created=2026-05-28T10:00:00Z

# --- h5ad_* keys are stamped by `mus h5ad check` — do not hand-edit --
# h5ad_n_obs=73888
# h5ad_n_vars=24940
# h5ad_layers=counts
# h5ad_embedding=X_umap
# h5ad_inspected_at=2026-05-28T11:00:00Z
# h5ad_inspected_sha256=...
```

### 4.3 Hard-required at a glance

Seven controlled-vocabulary keys plus the structural h5ad rules from
§1.1–§1.2. The cascade plus the sidecar together must satisfy all of
the keys; either file can provide any of them.

| Hard key | Notes |
| --- | --- |
| `organism` | controlled — §3 |
| `tissue` | controlled, list allowed |
| `cell_type` | controlled, list allowed |
| `technology` | controlled |
| `suspension_type` | `cell` / `nucleus` / `na` |
| `access` | `internal` / `public` |
| `source` | `internal` / `external` |
| `adata.X` log-norm, min ≥ 0 | inside the `.h5ad` |
| `adata.layers["counts"]` raw int, min ≥ 0 | inside the `.h5ad` |
| `obsm["X_umap"]` or `obsm["X_tsne"]` | inside the `.h5ad` |

---

## 5. Pre-deposit validation checklist

Run this checklist before sending the file. Tools that perform these
checks are downstream; depositor sanity-checks first.

### Self-check (depositor)

```python
import anndata as ad
import numpy as np
import scipy.sparse as sp

a = ad.read_h5ad("mydata.h5ad")


def _data(matrix):
    """Return the underlying numpy array of a possibly-sparse matrix."""
    return matrix.data if sp.issparse(matrix) else np.asarray(matrix)


def looks_integer(matrix, n=2000):
    sample = matrix[:n] if not sp.issparse(matrix) else matrix
    arr = _data(sample[:n] if sp.issparse(sample) else sample)
    return bool(np.allclose(arr, np.rint(arr)))


# --- Hard requirements ---------------------------------------------------
assert a.X is not None, "adata.X must be populated (log-normalized)"
assert "counts" in a.layers, "layers['counts'] missing"

# Both matrices: minimum value must be zero (no negatives)
assert _data(a.X).min() >= 0, \
    "adata.X has negative values — not log-normalized expression " \
    "(maybe scale.data / z-scored / Pearson residuals?)"
assert _data(a.layers['counts']).min() >= 0, \
    "layers['counts'] has negative values — not raw counts " \
    "(maybe SCT residuals?)"

# X must NOT look like raw integers; counts MUST be integer-valued
assert looks_integer(a.layers["counts"]), \
    "layers['counts'] is not integer-valued"
assert not looks_integer(a.X), \
    "adata.X looks like raw integers — should be log-normalized"

# A 2-D embedding must be present (UMAP or t-SNE)
assert ("X_umap" in a.obsm) or ("X_tsne" in a.obsm), \
    "obsm must contain X_umap or X_tsne (a 2-D embedding)"

assert a.obs_names.is_unique, "obs_names are not unique"
assert a.var_names.is_unique, \
    "var_names are not unique — call var_names_make_unique()"

reserved = {"cell_id", "cell_name", "gene_id", "gene_name"}
assert not (reserved & set(a.obs.columns)), \
    f"reserved column names in obs: {reserved & set(a.obs.columns)}"
assert not (reserved & set(a.var.columns)), \
    f"reserved column names in var: {reserved & set(a.var.columns)}"

# --- Soft checks (warnings only) -----------------------------------------
for col in a.obs.columns:
    n = a.obs[col].nunique()
    if n > 10_000:
        print(f"  WARN: obs[{col!r}] has {n:,} unique values "
              f"(likely cell-id-like, consider dropping)")

print("✓ self-check passed")
```

### Metadata check

The simplest end-to-end check is `mus h5ad check <file>`. It walks the
`.env` cascade up from the file's directory, applies the `.h5ad.mus`
sidecar, validates everything against the joint vocabulary, runs the
structural h5ad checks above, and exits 0 on PASS. The first PASS stamps
the sidecar with `h5ad_inspected_sha256`; subsequent runs short-circuit
unless the file changed.

Manual cross-check:
- All seven hard keys (§4.3) are present after cascade + sidecar merge:
  `mus -C <folder> config show -j` shows the cascade portion; eyeball the
  sidecar for what it adds or overrides.
- Controlled values match the joint vocabulary (§3): `mus vocab` lists
  the active lists.
- The sidecar's `sha256` agrees with the actual h5ad's checksum.

---

## 6. Common failure modes (informed by real ingestions)

These are the rejections seen most often. Reading this section avoids
re-discovering them.

| Symptom | Root cause | Fix before deposit |
| --- | --- | --- |
| Counts in `X`, log-norm absent | scanpy users sometimes export the raw matrix as `X` | Normalize → store in `X`, raw → `layers['counts']` |
| `adata.X` is `None` after Seurat → AnnData | sceasy or anndata2ri may leave X empty when only the "scale.data" assay was set | Ensure the active assay is `RNA` log-norm before conversion |
| `layers['counts']` is float32 with non-integer values | Someone stored SCT residuals there | Re-export the actual counts |
| `layers['counts']` is missing entirely | Seurat object was exported after SCTransform without keeping counts | Re-export from a snapshot that still has counts |
| `var_names` are all Ensembl IDs | Direct dump from CELLxGENE / 10X mtx | Remap to HGNC/MGI symbols via mygene before deposit |
| `X_umap` not in `obsm` | Embedding stored in a non-standard slot (e.g. `obs['UMAP_1']`) | Move the matrix into `obsm['X_umap']` |
| Mixed prep (whole cell + nuclei) in one file | Concatenation across studies | Split into separate `.h5ad` files |
| 20 k-cardinality `cell_barcode` left in obs | Forgot to drop the unique-per-cell column | Drop or move to `uns` before export |
| Missing `cell_type` / `suspension_type` despite a populated `.h5ad.mus` | Field name from an older spec (`celltype`, `preparation`) | Use the schema-v2 keys: `cell_type` not `celltype`, `suspension_type` not `preparation`, `access` not `internal` |
| Sidecar value silently ignored — cascade value wins | Mis-named sidecar file | Sidecar must be `<basename>.h5ad.mus` exactly (so `foo.h5ad` → `foo.h5ad.mus`, NOT `foo.mus`) |
| Brand-new tissue / cell_type rejected | Term genuinely not in the joint vocabulary yet | Open a PR on the mus repo at codeberg.org/mfiers/mus to add it — do not invent locally |

---

## 7. Out of scope

This document deliberately does not cover:
- Where files are stored or how they are versioned after deposit.
- How catalog-level metadata (cross-dataset views, search, etc.) is built.
- Downstream annotation runs (catalog scoring, module scoring, etc.).

Those decisions belong to the downstream catalog system and are not the
depositor's concern.
