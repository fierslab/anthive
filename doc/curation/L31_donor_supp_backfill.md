# Donor Braak / APOE backfill from paper supplements (L31, 2026-10-07)

`backfill_donor_supp.py` was a one-off curation script, kept for provenance and as a template. It is not part of the
shipped tools and has no tests. Run it with `envs/anthive-data/bin/python`, with the supplementary tables of the
papers in `$ANT_SUPP_DIR` (inputs are not stored in git; file names are listed in the script).

What it did, per dataset (`olah` O20, `gerrits` G21, `grubman` G19):

1. Read a per-donor table from the paper supplement (Braak stage, APOE genotype).
2. Joined it to the per-cell donor id of the dataset duckdb.
3. Wrote the obs columns `braak_supp`, `apoe_supp`, `braak_note_supp` with `AnnotationWriter` (one column per writer
   block, duckdb backed up first). The md then maps them to `ant_braak` and `ant_apoe`.

Dataset specifics:

- Olah2020: ROSMAP clinical table joined through the projid of Olah Supplementary Data 1 (sheet RUSH); the three
  epilepsy donors have no source.
- Grubman2019: the deposited `donor_id` is a library of two individuals. The individual of each cell was taken from
  the authors' per-cell table (source data of Figure 1) by barcode plus library (13,214 of 13,214 matched; 444 cells
  are undetermined and get NULL). It writes `individual_supp`, which the md maps to `ant_donor` (v0.2.9), and
  `apoe_supp`, mapped to `ant_apoe`. Assumption: "donor 1" in Table 1 is the first name of each pair (open question
  in the store's `ASK_THE_LAB.md`). For undetermined cells the Braak/APOE values are filled only when both members
  of the library pair agree (this gives `ant_apoe` for 67 Ct1 cells that have no `ant_donor`).

After a run: update the md (obs_rename, caveat, changelog, PATCH bump), `bin/ops/ant-rebuild-obs-views --only <db_id>`,
`bin/meta/ant-md-check --no-fix`, commit the store, `bin/ops/ant-sync push`.
