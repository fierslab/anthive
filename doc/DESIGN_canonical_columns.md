# Design — canonical columns as `ant_*`

Status: **implemented (2026-08); namespace closed and registry moved to the vocab 2026-10-05, see the update at the end**
Date: 2026-08-18

## Problem

Anthive treats a handful of column names as *canonical annotations* across
datasets — `braak`, `sex`, `disease`, `cluster`, `condition`, `braak_group`,
`age`, `pmi`, `donor_id`, etc. Two failure modes:

1. A user-supplied `.h5ad` may already contain a column literally named
   `braak` with values in some non-canonical shape (`'?'`, `'nan'`, roman
   numerals, floats). The current pipeline can be led to treat that column
   as canonical when it shouldn't.
2. A `.md`'s `api.obs_rename` mapping quietly renames a user column to
   `braak` in the DuckDB view. From the user's point of view, the
   post-download `.h5ad` no longer has the meaning they expected under
   the name `braak` (the physical `__obscat` table keeps the original
   column, but the semantics are confusing).

**Root cause:** the canonical *tag* namespace and the user's *column-name*
namespace overlap.

## Decision

Canonical annotation columns are exposed under an **`ant_` prefix** in the
public `obsnum` / `obscat` VIEWs.

- Tag vocabulary (`lib/obscol_tags_vocab.yaml`) stays as-is — short semantic
  names: `braak`, `sex`, `cluster`, `age`, …
- Canonical column names in the DuckDB VIEWs become `ant_braak`, `ant_sex`,
  `ant_cluster`, `ant_age`, `ant_disease`, `ant_condition`, `ant_braak_group`,
  `ant_pmi`, `ant_donor_id`, `ant_apoe`, `ant_region`.
- A user's original obs column (whatever its name) is **never overwritten** —
  it stays in the physical `__obscat`/`__obsnum` table under its original
  name and is exposed through the view alongside its `ant_*` alias.
- The `api.obs_rename` mapping in each `.md` becomes the explicit,
  auditable declaration of "this user column IS the canonical `ant_*`
  column" (possibly with value coercion via `obs_value_rename`).

## Why the prefix is cheap

The recon (2026-08-18) confirmed:

- **Canonical columns don't physically exist in the DuckDB.** They live only
  in the `obscat` / `obsnum` VIEWs, created at ingest by
  `build_obs_view_sql()` from each `.md`'s `api.obs_rename` dict.
- **No h5ad rebuild** is needed. The physical `__obscat`/`__obsnum` tables
  keep the user's original names. The migration is DROP VIEW + CREATE VIEW
  per dataset — a metadata operation.
- **Only one production file hardcodes canonical column names**:
  `bin/annot/ant-module-variability` (`_CANONICAL_TAGS`, `NUMERICAL_TAGS`,
  `CATEGORICAL_TAGS`, lines 67–159). `ant-serve` and the web UI discover
  columns dynamically from `information_schema`, so they pick up whatever
  the view exposes.

## Public surface — what does and doesn't change

**Stays the same:**

- Tag vocabulary in `obscol_tags_vocab.yaml` (`braak`, `sex`, `cluster`, …)
- REST query params by tag: `?tag=braak` still works (aliased server-side
  for one release; deprecated in the release after)
- User-supplied obs columns in the .h5ad (never touched)
- Physical `__obscat`/`__obsnum` table schemas
- Module-variability API path and shape

**Changes:**

- View columns: `braak → ant_braak`, `sex → ant_sex`, etc.
- Every `.md`'s `api.obs_rename` targets migrate to `ant_*`
- `ant-module-variability`'s `_CANONICAL_TAGS` dict values become
  `ant_*` (tag keys stay bare)
- Module-variability parquet is rebuilt (tag_column values change from
  `braak` → `ant_braak`)
- Web UI displays `ant_*` columns as **bold `braak`** with a small
  `[canonical]` pill and a hover-ⓘ showing the real column name (per
  `web/CLAUDE.md` info-hover convention)
- Column-name filters in `?color=`, `?x=`, `?tag_column=` — must be
  aliased for the one back-compat release

## Naming rule

- Prefix `ant_`, snake_case (matches DuckDB unquoted-identifier convention).
- One-to-one with the tag vocabulary: `ant_<tag>` for every tag in
  `obscol_tags_vocab.yaml`.
- No `ant_*` name is emitted unless the `.md`'s `api.obs_rename` explicitly
  declares the mapping — a user h5ad with a column literally named
  `ant_braak` (unlikely) would either error at ingest or be silently
  passed through under its own name; TBD in the sanity check.

## UI

- The `obs` column listings and plot-form dropdowns render `ant_*` columns
  with `font-weight: 600` and a small pill `canonical` after the label.
- The label shown to the user drops the `ant_` prefix: `braak` (bold),
  `cluster` (bold), etc.
- Hover ⓘ (sitewide `.info` pattern) — "canonical annotation `ant_braak`,
  standardised across datasets".
- The user's original column (if any, e.g., `Braak_Stage`) shows unformatted
  in the same list.

## Back-compat contract

**One-release window** (this migration + one point release afterwards):

- `ant-serve` accepts `?tag=braak` and `?tag_column=braak` (bare) and
  transparently rewrites to `ant_braak` internally.
- `ant-serve` accepts `?color=braak`, `?x=braak`, etc. and rewrites to
  `ant_braak` **iff** `braak` is not itself a column in the view.
- `/api/module-variability` accepts both `braak` and `ant_braak` on input;
  emits `ant_braak` on output.
- Bookmarks in the wild (Sierksma Fig 2 hexbin links, user notebooks,
  vark/pyvark scripts) keep working through the alias.

The next release removes the alias. `vark` and `pyvark` releases can
retire their bare references in the interim.

## Migration stages

1. **Design + vocabulary lock** (this doc, awaiting sign-off)
2. **`lib/`** — add `CANONICAL_TAGS` list + `canonical_column(tag)` helper
   returning `f'ant_{tag}'`. Single source of truth.
3. **Ingest** — `ant-convert` is unchanged (it already reads `obs_rename`
   from the `.md`). Sanity check in `ant-h5ad-check` optional: warn if a
   user h5ad already has a `ant_*` column.
4. **`.md` migration** — mechanical rewrite of `api.obs_rename` targets
   in 105 dataset `.md` files: `braak → ant_braak`, `sex → ant_sex`, etc.
   Script: `bin/ops/ant-migrate-obs-rename` (one-shot; leaves a git commit).
5. **`ant-module-variability`** — update `_CANONICAL_TAGS`,
   `NUMERICAL_TAGS`, `CATEGORICAL_TAGS` to use `ant_*` column identifiers.
   Query logic: dynamic (`SELECT ant_braak FROM obscat`), no other changes.
6. **`ant-serve` back-compat alias** — a small `_alias_canonical(name)`
   helper wraps tag/column params in the relevant endpoints for one
   release. Deprecation-warn header.
7. **Web UI** — render `ant_*` columns bold + `[canonical]` pill + hover.
   Strip prefix on display. Also on the module-variability facet dropdowns.
8. **DuckDB VIEW rebuild** — one-shot script that iterates every `.duckdb`
   in the store, DROP + CREATE the `obscat`/`obsnum` views with the new
   `.md`. Fast (metadata only).
9. **Module-variability parquet rebuild** — re-run `ant-module-variability`
   fleet-wide. Produces new tag_column values (`ant_braak`, …). ~15 min.
10. **E2E test on one dataset** — MY4/MTH: fresh ingest, view rebuild,
    module-variability rebuild, browse the dataset page, verify obs list,
    plot with `?color=ant_braak`, module view, back-compat with
    `?color=braak`.
11. **Batch rebuild views + parquet** across all ~184 datasets.
12. **Test suite** — full run.

## Rollback

- Every stage is a separate commit.
- The `.md` rewrite is git-tracked (`data/h5adstore` is a git repo).
- The DuckDB view rebuild is idempotent — running the pre-migration
  view-build against the pre-migration `.md` restores the old shape.
- The parquet is regenerated from the DuckDB, not authoritative.
- Original user obs columns are never touched, so h5ad round-trip is safe
  throughout.

## Open questions

1. **`obs_derive` columns** — several `.md` files use `obs_derive:` to
   compute a categorical (e.g., `braak_group` from `braak`). Should the
   `braak_group` target be renamed to `ant_braak_group`? Yes — same
   principle; the migration script handles it uniformly.
2. **Non-canonical `obs_rename` targets** — some datasets rename user
   columns to non-canonical short names (e.g., `Cell_type_full → celltype`).
   Those stay untouched — only tags in the canonical vocabulary get the
   `ant_` prefix.
3. **`api.obs_hidden`** — untouched.
4. **`__pending_cols` audit trail** — its rows reference column names
   that are then renamed. Cross-check that the audit path isn't broken.
   Likely benign (`__pending_cols` records the PHYSICAL column name in
   the underscore table, not the view alias).

## Vocabulary — the initial set

Canonical tag → view column mapping (source of truth: `lib/CANONICAL_TAGS`):

| tag              | view column           |
|------------------|-----------------------|
| `braak`          | `ant_braak`           |
| `braak_group`    | `ant_braak_group`     |
| `sex`            | `ant_sex`             |
| `age`            | `ant_age`             |
| `cluster`        | `ant_cluster`         |
| `disease`        | `ant_disease`         |
| `condition`      | `ant_condition`       |
| `pmi`            | `ant_pmi`             |
| `donor_id`       | `ant_donor_id`        |
| `apoe`           | `ant_apoe`            |
| `region`         | `ant_region`          |
| `genotype`       | `ant_genotype`        |
| `host_sex`       | `ant_host_sex`        |

`ant_donor` vs `ant_donor_id`: the tag is `donor`, the column becomes
`ant_donor` (following `ant_<tag>` exactly). One-to-one, no exceptions.
`donor_id` in the current code is a legacy misnomer — the migration
canonicalises to `ant_donor`.

## Sign-off checklist before I execute stage 4+

- [ ] Naming (`ant_<tag>`) and display (bold + `[canonical]` pill) confirmed
- [ ] Vocabulary table above (initial 13 tags) confirmed complete
- [ ] Back-compat alias for one release confirmed acceptable
- [ ] `ant_donor` vs `ant_donor_id` — is losing the `_id` suffix OK?
- [ ] Ready to run the mechanical `.md` rewrite across 105 files

## Update 2026-10-05: closed namespace, central facets, lint (audit L1, L2)

The facet audit (decisions L1 and L2, approved by Mark on 2026-10-04) closed
the `ant_*` namespace and moved the vocabulary table above out of
`lib/canonical_columns.py`.

**Single source.** `lib/obscol_tags_vocab.yaml` is the registry. A tag is a
facet when its `facet:` block has `status: registered` or `status:
deprecated`; the block also carries `scope` (`cross_dataset` or `dataset`),
`replaced_by`, `controlled_values`, `ordered`, `range`, `integer` and
`computed_from`. `canonical_columns.CANONICAL_TAGS`, `REGISTERED_TAGS`,
`DEPRECATED_TAGS` and `NUMERICAL_TAGS` are derived from it
(`obscol_tags.facet_registry`). The one-release bare-name REST alias and the
one-shot migration scripts keep the frozen 2026-08 list
(`LEGACY_BARE_TAGS`), because facets registered later never had a bare form.

| Status | Facets |
| --- | --- |
| registered | donor, sex, host_sex, age, pmi, disease, braak, braak_group, apoe, apoe4_carrier, cell_type, tissue, genotype, author_cluster (dataset scoped), cognitive_status, comorbidity, adnc, donor_uid |
| deprecated | cluster (use author_cluster), condition (disease, genotype; treatment arms to a plain column), region (tissue), ad_stage (adnc), author_cell_type (a plain column) |

**Treatment is not a facet** (Mark, 2026-10-05). Treatments differ too
much between experiments for one shared column to mean anything across
datasets, so `treatment` has no `facet:` block and `ant_treatment` is an
unregistered target. A dataset that has treatment arms publishes them as a
plain, dataset-local `treatment` column (an `obs_derive` or `obs_rename`
target without the `ant_` prefix) with explicit labels: `vehicle` (saline,
DMSO or another vehicle), `isotype_control` (for example an IgG1 isotype
antibody), `untreated`, `unknown_control` (the paper does not describe the
control arm) and the treatment names themselves (`LPS`, `semagacestat`,
`lecanemab`, ...). The dataset's `.md` names the reference level of its
comparison in the front-matter key `treatment_reference`. Treatment is not
in `/facets`, the facet UI, the MCP facet tools or `ant-facet-test`.

Anything else under `ant_` (the singletons `ant_cause_of_death`,
`ant_lesion`, `ant_ms_type`, `ant_subtype`, `ant_source_institution`,
`ant_ethnicity`, `ant_technology`, `ant_group`) is to become a plain column.

**Checker.** `ant md-check` (`lib/facet_lint.py`) reports an unregistered
`ant_*` target as a warning while the store migrates
(`facet_lint.NAMESPACE_DEFAULT_MODE = 'warn'`) and as an error with
`--strict-namespace`; flip the default once the strict run passes. It also
lints the mapping (rename chains, derive dtype against the facet kind,
non-numeric or out-of-range values in numerical facets, Braak outside 0 to 6,
controlled vocabularies, the Braak group rule) and, with `--with-data`, reads
each `.duckdb` read-only for missing source columns and values with no map
entry and no default. The store's own pre-commit hook
(`data/h5adstore/scripts/validate_md.py`) does not run these rules yet.

**Central facets.** `anthive_db.build_obs_view_sql` computes two facets when
the `.md` declares neither the facet nor a stored column of that name:

* `ant_braak_group` from the published `ant_braak`: 0 to 2 `low`, 3 to 4
  `medium`, 5 to 6 `high`; NULL for anything else (NULL, text, 7, 2.5).
* `ant_apoe4_carrier` from the published `ant_apoe`: a value of the form
  `E<a>/E<b>` (a, b in 2, 3, 4) is `E4+` when it holds E4, else `E4-`; any
  other value (a single allele such as `E4`, an author code) is NULL.

A declared map wins; the checker warns when a declared `ant_braak_group` map
breaks the rule. `facet_mapping` reports these columns with `mapping_kind:
computed`. Stored views change only when they are rebuilt (re-ingest or
`/admin/refresh-views`).

**Builder warnings.** A derive whose source column is in neither obs table
is still left out of the view, but the builder now logs a warning naming the
derive, the source and the dataset. `ant-convert` builds both obs views
after both tables exist and passes the sibling table, so a derive whose
source lives in the other table (a numerical `ant_braak` from a text Braak
column) survives ingest; before, such derives were dropped at ingest and
only appeared after a view refresh.

## Update 2026-10-05: `ant_donor_uid` from a template or a crosswalk join

Approved by Mark on 2026-10-05. `ant_donor_uid` was written into 33 dataset
`.md` files as a value map generated from `data/h5adstore/donor_crosswalk.tsv`
(hundreds of donor ids per file, regenerated by hand whenever the table
changed). Two new `obs_derive` forms replace the map; both are categorical and
are implemented in `lib/obs_derive_forms.py`.

**Template.** `ant_donor_uid: {source: <donor column>, template:
'{group_id}:{value}'}`. Placeholders are `{group_id}` (the dataset's
three-character group id) and `{value}` (required). Anything else, a format
spec or a conversion is refused. For datasets whose cohort is not in the
crosswalk: the group id stands in for the cohort, so a donor is the same
person in every dataset of that collection.

**Join.** `ant_donor_uid: {source: <donor column>, join: donor_crosswalk.tsv,
cohort: <cohort>, fallback_template: '{group_id}:{value}'}` (the fallback is
optional). The source value is looked up as an `alias` of `cohort` in the
crosswalk and replaced by its `canonical_uid`. Values without an entry get the
fallback template, or NULL without one. The table is resolved in the store
root through `config.get_store_file` (ant-serve uses the store it serves); an
absolute path or `..` is refused. Cohort names compare case-insensitively. An
alias with two different uids in one cohort is a conflict: it is left out
(not found) and `ant md-check` warns.

**Values.** The source value is cast to text and trimmed; an integer written as
a float (`1234.0`, also as text) loses its `.0`; an empty string and a
numerical NaN count as NULL. NULL in gives NULL out in both forms; the
fallback does not apply to NULL. Crosswalk aliases are normalised the same
way.

**Served view.** `build_obs_view_sql` gets the group id from its caller
(`ant-convert` from the dataset's `index.md`, `/admin/refresh-views` and
`ant-rebuild-obs-views` from the db_id, `ant-facet-test` from the store
entry, `AnnotationWriter` from its db_id or the `index.md`). The join embeds
the cohort's alias table in the view as a scalar subquery over a `VALUES`
list, so the view never reads the crosswalk at query time. DuckDB plans it
as a hash join; on 2 million cells and 1,300 aliases a GROUP BY over the
column took 0.08 s, against 0.2 s for the equivalent 1,300-branch CASE of a
value map (DuckDB 1.5.6, one run each, so indicative only). Rebuilding a view re-reads the table (parsed tables are cached by
path, mtime and size). A derive the builder cannot build (the template needs
`{group_id}` and none is known, the table is missing, the cohort is unknown)
is left out of the view with a warning, like a derive whose source column is
missing. The forms never write a fallback silently in place of a missing
table.

**Mapping display.** `facet_mapping` reports the kinds `template` and `join`
(`GET /databases/{id}/facets`, `/facets/{name}`): the template, or the join
table, cohort, fallback and `n_crosswalk_aliases` (a count), and
`join_problem` when the view cannot be built. With counts, every observed
source value gets its published uid and a rule (`template`, `crosswalk`,
`fallback`, `unmatched`, `null`), summed in `rule_counts`.

**Checks.** `ant md-check` adds `derive_form` (malformed spec, mixed forms,
non-categorical dtype), `join_table_missing`, `join_cohort_unknown`,
`crosswalk_conflict`, `template_group_id` and, with `--with-data`,
`join_unmatched` (source values the crosswalk does not know and no fallback).
`source_missing` covers both forms; `unmapped_value` does not apply to them.

**Donor counting.** `lib/donor_keys.py` keys a donor on `ant_donor_uid`, else
`<group_id>:<ant_donor>`, which is exactly what the template form publishes,
so a dataset with the template and one without any uid count the same donors
once. `GET /donor-groups` reports distinct donors per group, and facet-test
replication reports `n_donors_sum` and `n_donors_distinct`
(`doc/DESIGN_facet_testing.md`). The fallback cannot join one person across
collections; only a cohort crosswalk can.

**Migration (data job, not yet run).** `bin/meta/ant-migrate-donor-uid
--dry-run` rewrites each value-map `ant_donor_uid` to the join form after
checking that the join reproduces every map entry, and adds the template form
to every dataset that publishes `ant_donor` through a plain rename. It edits
the front matter as text and verifies each edit by re-parsing. Dry run on the
store (2026-10-05): 168 dataset `.md` files; 30 value maps to join (leuven 2,
luchen 7, nph 1, rosmap 6, seaad 14); 93 templates added (10 on reanalyses);
38 without a declared `ant_donor`; 7 left for review: 3 Francken2026 maps
whose values span four cohorts (the join takes one cohort) and 4 datasets
whose `ant_donor` source has value renames (Prater2023, Prinz2026 x3).

**Text for `data/h5adstore/CONVENTIONS.md`** (replaces the last paragraph of
its `donor_crosswalk.tsv` section once the migration has run; the store is
edited by the data job):

> The `ant_donor_uid` facet is declared in each dataset `.md` as an
> `api.obs_derive` in one of two forms. Datasets of a cohort in the table use
> the join form, `ant_donor_uid: {source: <donor column>, join:
> donor_crosswalk.tsv, cohort: <cohort>}` (optionally `fallback_template:
> '{group_id}:{value}'` for donors without an entry); the view builder reads
> the table when the views are built and embeds the cohort's aliases in the
> view, so editing the table needs a view rebuild (`ant refresh-views`), not a
> regenerated `.md`. Every other dataset with a donor column uses the template
> form, `ant_donor_uid: {source: <donor column>, template:
> '{group_id}:{value}'}`, which makes the collection's group id the cohort.
> Value maps are still accepted but no longer written. Datasets whose donors
> span several cohorts keep a value map until the join form takes more than
> one cohort.

## Update 2026-10-05: `replicate_unit`, `replicate_column`, `disease_basis`

Three catalog keys of the dataset `.md` (top level, not `ant_*` obs columns) say what the
facet statistics may treat as independent and what `ant_disease` means.

| Key | Values | Role |
| --- | --- | --- |
| `replicate_unit` | `donor`, `animal`, `sample`, `none` | the independent replicate of the dataset. `none`: no replicate structure, nothing is tested between replicates (cell-level, descriptive only) |
| `replicate_column` | an obs column of the served view | identifies the replicate (`ant_donor` for the human cohorts, `hash.ID` / `Mouse_ID` / `mouse.id` for xenografts). `ant-md-check` errors when the column is not an obs column (stored or written by the `api:` section) and warns when `replicate_unit` is missing |
| `disease_basis` | `nia_reagan`, `braak_cerad`, `adnc`, `clinical_dx`, `author_dx` | what `ant_disease` is based on. Cross-cohort replication of the disease facet only combines the pathology-based values |

Inference when `replicate_unit` is not declared (`lib/replicates.py`, served with
`replicate_basis: inferred`): human data with `ant_donor` is `donor` / `ant_donor`; mouse or
xenograft data with an animal id column (hash.ID, Mouse_ID, mouse.id, animal, ...) is `animal`.
In a xenograft `ant_donor` names the human donor line, not the replicate, so it is never used
as the animal; otherwise `none`. `bin/meta/ant-set-replicate` wrote the keys for all
datasets; its `DECISIONS` table holds the hand-checked xenograft, mouse, in vitro and bulk
entries, and the datasets it could not decide are `none` (listed in the 2026-10-05 report:
AZ5/NST, B26/SOT, F26 summary tables, G24/MEx and PEx, H25/HNO, L24/*, PM4/*, P26/MMA, SF9/CMR,
Si5/SSI, S26/DUN, TW5/CMA, V25/*, Z25/*).

`disease_basis: braak_cerad` is the common pathology definition: AD = Braak stage III or
higher and CERAD probable or definite; control = Braak II or lower and CERAD none or sparse;
every other combination (PART-like or amyloid-only donors, missing values, reference donors)
is NULL. It is declared in the `.md` as `disease_derivation` (rule, stored column
`disease_braak_cerad`, Braak and CERAD source columns with value maps; `lib/disease_rule.py`)
and applied by `bin/annot/ant-derive-disease`, which writes the stored column
(`AnnotationWriter`) and edits the mapping so `ant_disease` is renamed from it. Applied to
SEA-AD (G24, 14 files), ROSMAP (M23 x3, GF4, MY4, Z20; RADC `ceradsc` 3 = possible = sparse),
MADRC WA4 (6 files) and Prater (P23). Everything else keeps its own labels with
`disease_basis: author_dx` (MADRC astrocytes SP4 carry the NPDX1 neuropathologic diagnosis and
no CERAD). `clinical_dx` is reserved for datasets whose label is documented as an ante-mortem
clinical diagnosis (none verified yet). The ADNC facet `ant_adnc` is unchanged.

SEA-AD microglia (G24/MMG, G24/PMG): `ant_cell_type` is derived from the `Supertype` column
(microglia = the Micro-PVM_2, _2_1, _2_3, _3, _4 supertypes; Micro-PVM_1, Lymphocyte and Monocyte
are their own strata), so the microglia stratum no longer carries about 4 percent putative
perivascular macrophages.

## Update 2026-10-06: facet scopes, `app_nlgf` and `age` as tested facets

**Facet scopes** (`lib/facet_scopes.py`). Facets never mix human and mouse. A dataset has one scope,
derived from `.h5ad.mus` (`organism`, `experimental_system`) and the published columns:

| Scope | Cells | `experimental_system` | Datasets (2026-10-06) |
| --- | --- | --- | --- |
| `human_postmortem` | human, tissue from the subject | post_mortem_tissue, postmortem_tissue, fresh_biopsy, live_biopsy (or none) | 103 |
| `mouse_in_vivo` | mouse, tissue from the animal | post_mortem_tissue (the store's value for mouse tissue), none; mouse cells labelled xenograft (PM4, MZ4) | 16 |
| `xenograft_human_in_mouse` | human cells in a mouse host | xenograft, chimera, or any human dataset publishing `ant_host_sex` | 31 |
| `in_vitro` | cultures | in_vitro_2d, organoid | 7 (never tested) |

No scope (skipped by the tools, not linted): mixed or missing organism, bulk tissue (11). The five
Wachter2024 files without `organism` stay human (`ASSUME_HUMAN_DB_IDS`).

**Registry field `facet_scopes`** in the `facet:` block of `lib/obscol_tags_vocab.yaml` (not to be confused with the
older `scope: cross_dataset | dataset`): the scopes in which a dataset may publish the facet; `all` or no field means
every scope. `sex`: all; `disease`, `braak`, `braak_group`, `ad_stage`, `adnc`, `apoe4_carrier`,
`apoe_carrier`, `cognitive_status`, `comorbidity`, `pmi`, `age`: `human_postmortem`; `apoe`:
`human_postmortem` and `xenograft_human_in_mouse` (the APOE genotype of the iPSC donor lines);
`host_sex`: `xenograft_human_in_mouse`; `app_nlgf`: `mouse_in_vivo` and `xenograft_human_in_mouse`. Everything
else: all. `ant md-check` reports `facet_scope_mismatch` (error) when a dataset's `api:` section maps a facet outside
its scopes (`facet_lint.lint_facet_scope`). The two violations in the store were fixed with the change:
DeBaene2026 (DB6/CPF, xenograft) no longer publishes `ant_apoe4_carrier` and `ant_disease` (iPSC donor attributes;
the source columns `APOE` and `AD_status` are untouched; widen the scopes of those two facets if you want them back);
Hou2025 adata_qc (H25/AQC, labelled `in_vitro_2d`) no longer publishes `ant_host_sex` (an in vitro culture has no host;
if the dataset is a xenograft the `.h5ad.mus` label is wrong and the mapping can come back).

**`app_nlgf`** (`nlgf`, `control`). The App NL-G-F knock-in mouse, or the host mouse of a xenograft, against the
study's control (wild type, or the humanised-App AppHuHu / hApp control). Tested at replicate level with the animal
as the replicate, covariate sex (host sex in a xenograft). Mapped (`obs_derive: ant_app_nlgf`):

| Dataset | Source column | nlgf | control | Note |
| --- | --- | --- | --- | --- |
| B25/CNT (Borrie2025) | Mouse_model | NLGF | HuHu | replicate animal (Mouse) |
| D25/HAR (Davis2025) | mouse_genotype | NLGF | hApp | replicate animal (sample id) |
| DB6/CPF (DeBaene2026) | genotype | AppNLGF | AppHuHu | replicate animal (mouse_id) |
| M24/M23 (Mancuso2024), M24/RMF | mouse.genotype | APP-NLGF | APP-WT, WT | `APP-NLGF/ApoeKO` stays empty |
| S25/V6A (Sierksma2026 Village 6M), S25/V6W | Strain | AppNLGF | AppHuHu | V6W is a secondary; `sex_mouse` is now published as `ant_host_sex` (the covariate); `Strain` is an `obs_hidden` source, only the derived facet is visible |
| B26/SOT (Blackburn2026) | app_genotype | NLGF | hAPP | `replicate_unit: none`: published, not tested |
| SF9/CMR (SalaFrigerio2019) | genotype | K | W | `replicate_unit: none` (one plate per genotype x age x sex x region): published, not tested; D, P, C, E (Apoe cohort codes) stay empty. K is the knock-in line (K + W = the 10,801 Leuven-mice cells; confidence 95 percent) |

Not mapped, with the reason (candidates for later facets): AppNL-G-F hosts only, no control animal
(AZ5/SLM and AZ5/NST AlbertiniZielonka2025, Si5/SSI Simpson2025 sorl1, PM4/AST and PM4/MIC Preman2024 App NL-G-F x
Apoe-/-, H25/AQC Hou2025 PSEN lines); other models (S20/SIE Sierksma2020: APPswe/PS1 and Thy-TAU22; SH9/SMB Shah2019
TG vs WT astrocytes, model not stated; S26/AQR and AQM Sun2026 Control vs Treat, model not stated; MZ4/CMM and SSM
MallachZielonka2024 "AD" mouse, model not stated in the store, spatial); xenograft objects whose host genotype column is
absent or constant (S25/V3M, SGM, PhM, Z25/*); duplicate objects of mapped datasets that are not marked
secondary (S25/V6C, V6S, V6M and the other `prs.*` objects).

**`age`** is a tested facet in `human_postmortem` (`ant_age`, years; the model uses age / 10, see
`doc/DESIGN_facet_testing.md`). `ant_age` is not allowed in mouse datasets (their ages are strings such as `6m`).

`facets_visible` defaults to `ant_sex`, `ant_disease`, `ant_apoe4_carrier`, `ant_braak`, `ant_age`, `ant_app_nlgf`
(`anthive.default.yaml`, `lib/config.py`, `site.example.yaml`). The dev host's `anthive.yaml` has no
`facets_visible` key, so it follows the default.
