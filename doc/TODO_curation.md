# TODO — AI-driven curation roadmap

Status: parking lot for the curation work. **Lead item**: `ant-curate` (see
§Scoped first). Architecture-wide: the chat-agent loop is the substrate;
every curation pass runs as a tool-use LLM against the existing REST API.

Companion docs:
- `doc/DESIGN_metadata_editing.md` / `_cms.md` — the human-review UI
- `data/h5adstore/CONVENTIONS.md` — the .md schema
- `lib/_mus_vocab.yaml` — the .h5ad.mus controlled vocab (gate for anything
  the LLM proposes against `organism / cell_type / tissue / disease / …`)

## Scoped first — `ant-curate` (this commit kicks it off)

A CLI + REST endpoint that runs an agentic pass over ONE dataset and emits
**proposals** (never direct writes). The metadata editor surfaces them as
"AI suggests …" with per-field accept/reject. See `§ant-curate` below.

Goal of phase-1: prove the loop end-to-end on one cell-type field. Once the
review UI works, the same plumbing scales to every other task in this doc.

## The big list (priority ↓ within each section)

### 1. Annotation & integration (Mark's stated focus)

- [ ] **Cell-type harmonization across datasets** — map each dataset's cell-
      type categorical column to a canonical ontology term (Cell Ontology
      CL:xxxx, or a smaller curated set). Different papers call microglia
      "MG / HM / Homeostatic / microglia / mglia / Mic_1…" — harmonizing is
      the single biggest unlock for cross-dataset queries.
      - sub-task: pick the canonical vocab (Cell Ontology subset vs custom
        microglia-state taxonomy)
      - sub-task: write `cell_type__co` (Cell-Ontology-mapped) as a new obs
        column, preserve the original verbatim
      - sub-task: chat / analytics_query learns the new column name

- [ ] **Considering starcat** — for label transfer + harmonized cell-state
      calls, `ant-starcat` already runs the MICROGLIA_V1 catalog reference
      and writes the `starcat__MICROGLIA_V1__*` obs columns. Question to
      resolve: does starcat replace LLM harmonization for the microglia
      subset, or complement it? Likely complement — starcat gives state
      scores (continuous), LLM gives ontology buckets (discrete). Both
      useful, different consumers.

- [ ] **`.h5ad.mus` field completion** — for sidecars with missing or
      `null` organism / tissue / cell_type / disease / suspension_type /
      technology, LLM reads the paper abstract (via DOI fetch) + obs sample
      and proposes values. Gated by `lib.mus.validate_value()` — any
      hallucinated term rejects. Bulk job, runs ~daily.

- [ ] **DE study + contrast biological labels** — every `__de_study` row
      has cryptic contrast names (`F`, `m`, `ko_v_wt`, `2_vs_1`). LLM
      reads the study's sidecar + paper context, drafts a one-line bio
      tag. Surfaces in `de_browser.html` as "F (female donors, vs male
      reference)" instead of bare `F`.

### 2. Cross-dataset findings (the "anthive becomes a research engine" tier)

- [ ] **Module-score sweep + LLM summary** — for a curated geneset (e.g.
      IFN signature, DAM markers), run `module_score` across all relevant
      datasets, hand the result table to the LLM, get back a 3-sentence
      summary + a flag list of "datasets where this signature is
      surprisingly elevated / depleted". Cache. Surface as a per-geneset
      "findings" tab.

- [ ] **Per-cell-type cross-study aggregator** — once cell-type
      harmonization is in, ask: "what's the median TREM2 expression in
      CL:0000129 microglia across all AD-disease datasets?" — a single
      query, dispatched via analytics_query, with LLM summary of
      heterogeneity.

- [ ] **Trajectory consistency check** — for datasets with a pseudotime
      column, run `rolling` plot for the same gene set across datasets,
      LLM compares for consistency. Useful sanity check before publishing.

### 3. Per-dataset catalog metadata

- [ ] **Auto-description** — for datasets where `.md` has `description:
      null`, LLM proposes a one-line summary from the abstract + study
      metadata. Surfaces in editor with accept/reject.

- [ ] **top_genes / top_cat / top_num panels** — replace `ant-suggest-
      tops`'s hardcoded marker panels with LLM-proposed lists per
      dataset, informed by organism + cell_type + actual obs/var. Keep
      the human review step.

- [ ] **Title polish** — for datasets with cryptic basenames like
      `prs.singleline.teset.counts`, propose a human title. (`description`
      probably more useful than retitling.)

### 4. Per-obs / per-DE structured tags (Mark's other ask)

- [ ] **Restricted tag vocabulary for obs columns + DE contrasts** — a
      small curated list of axis-of-variation tags (`sex`, `age`,
      `disease_stage`, `genotype`, `treatment`, `donor`, `batch`,
      `region`, …). Tagging an obs column or DE contrast asserts what
      biological axis it represents. Lets queries say "show me all axes
      tagged 'sex' across the catalog" — a cross-dataset semantic index.
      - Vocab: where it lives (probably `lib/_axis_tags_vocab.yaml` next
        to the mus vocab; loaded by validate_md.py + the editor)
      - Storage: as a new field in the .md (per obs column) + the DE
        sidecar (per contrast). Both git-tracked.
      - LLM curation pass: propose tags for every obs column / DE
        contrast in the catalog, human reviews.
      - Editor: surface as a dropdown picker in the obs-columns table
        and the de_browser contrast list.
      - This is its own substantial workstream — separate from
        ant-curate's first cut.

### 5. Quality / hygiene

- [ ] **Duplicate detection** — flag datasets that share enough cells (by
      cell-name overlap, sha256 of expression matrices, etc) to suggest
      they're republished versions of the same data. Rule-based, not LLM.

- [ ] **Suspicious layer stats** — datasets where `.X` claims to be log-
      normalized but `min_value=0, max_value=15.something` matches raw
      counts. Rule-based; ant-h5ad-check already does some of this.

- [ ] **Orphan obs columns** — categorical columns that look like cell
      ids, batch hashes, or per-cell UMI counts polluting obscat.
      Rule-based first; LLM as backstop.

### 6. Genesets

- [ ] **Curated geneset import flow** — semi-automated MSigDB / KEGG /
      Reactome → genesetstore. LLM normalizes naming, categorizes (immune
      vs metabolic vs structural), proposes the YAML.

- [ ] **WGCNA-module auto-naming** — given a module's gene list, LLM
      proposes a name (Black_Mito-style) + 1-line function summary via
      GO enrichment context. Speeds up post-WGCNA write-up.

### 7. Ingest assistance

- [ ] **Dataset positioning at ingest** — when an h5ad lands in
      `__incoming/`, LLM reads its mus sidecar + sample obs and proposes
      which existing collection it belongs to (or that it's a new
      collection). Human confirms via the existing ant-ingest review
      step.

## What to NOT use LLMs for

- Anything that mutates raw `.h5ad` / `.duckdb` content (sha256, n_obs,
  layer_stats). Identity-defining fields stay machine-computed.
- Validate gates (`validate_md.py`, `lib.mus.validate_value()`). Those
  stay deterministic; LLM output runs THROUGH them, not around them.
- Statistical inference (DE testing, clustering, embedding). Those are
  the cell's job; LLM consumes their results, doesn't redo them.

## ant-curate (the lead item — see commit history)

Phase 1 design (scoped to ship as a working loop):

```
ant-curate <db_id> [--focus cell_type|description|top_panels|all]
                   [--apply | --dry-run (default)]

What it does:
  1. Loads .md + .h5ad.mus + obs sample (10 rows, no expression)
  2. Optionally fetches the paper abstract via DOI
  3. Runs an agent loop with a curation-mode system prompt against the
     existing REST API tools (analytics_query, get_dataset_info,
     module_score, search_genes, list_genesets, …)
  4. Outputs PROPOSALS as `tmp/curation/<db_id>.yaml`:
       { generated_at: ISO8601,
         model: <model id>,
         proposals: {
           field_path: {  # e.g. ".md.description" or ".mus.cell_type"
             current: <value>,
             proposed: <value>,
             confidence: 0.0..1.0,
             rationale: "<one line>",
             source: "abstract" | "obs_content" | "starcat" | "manual"
           }, ...
         } }
  5. Never writes to .md / .mus directly. Apply is the editor's job.

REST integration:
  GET /api/databases/{db_id}/curation → reads tmp/curation/{db_id}.yaml
  POST /api/databases/{db_id}/curation/run → enqueues a run (admin-gated)
  POST /api/databases/{db_id}/curation/apply {field_path} → applies one
    proposal to the .md (uses the existing PUT /source path under the hood)
```

Storage choice: `tmp/curation/` (gitignored, ephemeral, regenerable). A
proposal that gets stale because the user edited the .md just gets
overwritten by the next ant-curate run.

Editor integration (handed off to anthive-web):
  - GET /source response gains an optional `proposals` block that mirrors
    the curation.yaml format — the editor shows a "✨ AI suggests …" pill
    next to each field that has a proposal.
  - Accept: editor PUTs the .md as today, applying the proposed value.
  - Reject: just don't apply.
  - Regenerate: button calls POST /curation/run; editor re-fetches.
