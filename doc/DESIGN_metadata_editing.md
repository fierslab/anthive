# DESIGN — end-user metadata editing (OPTIONAL / exploratory)

Status: **proposal, not scheduled.** Captures options for letting non-technical
curators edit dataset catalogue metadata with review + audit, instead of editing
`.md` files directly on the forge. Nothing here is built. Cross-ref:
`doc/DESIGN_annotation_updates.md` (experimental obs-column annotation — a
*different* problem; this doc is only about the curated catalogue fields).

## 1. Problem

Today the catalogue metadata lives as YAML frontmatter in per-dataset `.md`
files in the data repo (`badslab/h5adstore`, moving to Codeberg/Forgejo). The
only edit path is "open the `.md` on the forge and hand-edit raw YAML." That
means:

- No content control — free-typed vocab, typos, schema drift.
- Hostile to non-technical curators (raw YAML, git, branches).
- No structured review beyond eyeballing a diff.

## 2. Goals / constraints

- **Non-technical-friendly**: forms, vocab dropdowns, markdown editor — not YAML.
- **Reviewable + controlled**: nothing reaches the canonical catalogue unchecked.
- **Easy to maintain & merge**; **low custom code** preferred.
- **Per-edit attribution** (who + when).
- **A separate tool next to `/browse` is fine** — it need not live inside the
  web UI.
- Scope = **`.md` catalogue fields only** (organism, celltype, tissue, disease,
  technology, suspension_type, year, doi/pmid/accession/link, license,
  contributor, access, source, a one-line summary, the markdown body).
  Experimental annotation (`.h5ad.mus`, obs columns) is out of scope here.

## 3. Existing building blocks (don't rebuild these)

- **`lib/metadata.py`** already parses + **writes** `.md` frontmatter
  (`write_md_file`, `ensure_in_list/mapping`, `validate_annotation_field`), and
  `bin/meta/ant-md` is a working CLI that sets/validates catalogue fields.
- **A `.md` validator already runs on every commit**: h5adstore
  `scripts/validate_md.py` via a `pre-commit` hook (+ `VALIDATION.md`). → Content
  control is *half-solved already*; it just isn't enforced server-side or in CI.
- **Authelia** forwards `Remote-User` / `Remote-Groups` → identity + admin
  gating for free.
- **`POST /admin/rescan`** re-reads the catalogue after a change.
- `.md` is git-tracked and small — ideal for git-as-database editing tools.

What does **not** exist today: any server-side git automation (everything is
manual `git`), and any write endpoint on ant-serve.

## 4. Design principles (agreed in discussion)

1. **The server never pushes `main`.** Edits are *proposals*; a human (admin)
   integrates them. This removes auto-merge/auto-conflict resolution — the one
   genuinely risky component.
2. **Never git-mutate the live serving tree.** Apply edits in a dedicated
   clone/worktree; the deployed checkout only `pull`s at publish time.
3. **Share *semantic* diffs, not branches/patches.** Record
   `{dataset, field, base_value, new_value, user, timestamp}`. "Apply" = re-run
   the field-set on current `main` (idempotent; conflict only if the *same*
   field changed meanwhile, which `base_value` detects). Forge-agnostic;
   nothing extra accumulates in the canonical repo.
4. **Proposals queue lives outside the official repo** → merged/rejected ones
   are simply deleted; the canonical history only ever gains clean commits.
5. **Attribution via git**: the commit author = the Authelia user, timestamp =
   edit time → `git blame` is the permanent audit trail. Don't pollute the
   frontmatter with `last_edited_by`.
6. **"Save" ≠ "live."** Edited datasets/fields are marked **pending/dirty** in
   the browse UI until an admin publishes (then rescan).
7. **Validation is a cross-cutting gate** (`validate_md.py`), enforced on the
   proposal *and* as required CI — independent of which editor is used.

## 5. Implementation options (easiest first)

The big lever for "easy": **don't build a bespoke editor+git-bot if an
off-the-shelf git-CMS or the forge's own tooling already does it.**

### Option A — off-the-shelf git-backed CMS (recommended easy path)

A config-driven CMS (**Decap CMS**, **Sveltia CMS**, **Pages CMS**, or **Tina**)
pointed straight at the catalogue repo. These exist precisely for "non-technical
users edit markdown-with-frontmatter in a git repo, with a review workflow." Our
catalogue *is* markdown-with-frontmatter — a native fit.

- You write **one config** mapping: collection = datasets, fields = our schema
  (select widgets for vocab, text for the one-liner, a markdown widget for the
  body). The CMS renders friendly forms automatically.
- **Editorial workflow** = drafts as branches + PRs; admin clicks merge. Review,
  diff, audit all handled by the forge.
- Runs as a **static app** (e.g. served at `/edit` or a subdomain), gated behind
  Authelia; commits via a forge OAuth app (per-user authorship) or a single bot
  token (simpler, coarser authorship).
- **Effort**: low — mostly config + auth wiring. Almost no custom code.
- **Maintenance**: low (upstream tool).
- **Cons / to verify**: Forgejo/Gitea backend support varies by CMS (Decap has a
  Gitea backend; Sveltia is adding Forgejo; otherwise a small git-gateway/OAuth
  shim). Vocab validation in-form is best-effort — back it with the CI validator
  (§5, validator-as-CI) as the hard gate.
- **Meets goals**: ✔ non-technical forms, ✔ review/merge, ✔ low code/maintenance,
  ✔ separate tool, ✔ attribution. This is the strongest "out of the box" answer.

### Option B — "edit on the forge" deep links + branch protection + CI validator

Near-zero code stopgap. Each dataset page gets an **"✎ edit metadata"** link to
the forge's in-browser file editor for that `.md` (Forgejo supports
`/_edit/<branch>/<path>` deep links). Protect `main` (require review); promote
`validate_md.py` to **required CI** so bad frontmatter can't merge.

- **Effort**: hours. **Maintenance**: ~none.
- **Cons**: users still edit *raw YAML* (not non-technical-friendly), needs forge
  accounts, no vocab dropdowns. Good for technical curators / as an interim
  while Option A or C is built.
- **Meets goals**: partial (review ✔, content-control-via-CI ✔, non-technical ✗).

### Option C — standalone form app → proposals queue in ant-serve (the integrated build)

The fully in-house design from the discussion: a schema-driven edit UI (its own
page next to `/browse`, or inline on the dataset page) → an authenticated
ant-serve `PATCH /databases/{id}/metadata` → a **server-side semantic-diff
queue** → admin review/approve UI → apply-on-approve (commit authored as editor,
push once, delete proposal, rescan).

- **Effort**: medium (write endpoint is mostly `metadata.py`; the queue + admin
  review UI + apply-on-approve git step are new). **Maintenance**: medium (ours).
- **Pros**: tightest integration (vocab dropdowns from our own vocab, the
  pending/dirty markers come for free from the queue, no third-party tool, no
  forge accounts for users — Authelia identity suffices, forge-agnostic).
- **Cons**: the most code; we own the git step (kept simple by principles 1–4).
- **Meets goals**: ✔ all, at the cost of build effort. Best if Option A's forge
  backend proves awkward or you want zero external dependencies.

### Option D — spreadsheet round-trip (great for bulk)

Export the whole catalogue to a sheet/CSV (one row per dataset, columns =
fields); curators edit in a familiar spreadsheet; an **import job validates +
writes back to `.md` + opens one PR**. Complements (not replaces) A/C.

- **Effort**: low–medium. **Pros**: bulk vocab cleanups, very familiar UI,
  trivial validation on import. **Cons**: round-trip (not real-time), the sheet
  risks becoming a shadow source of truth — treat it as a transient import, not
  a store.

### Option E — out-of-the-box lighteners (compose with the above)

- **Schema-in-the-repo**: keep the field list + vocab as a YAML in the data repo;
  whatever form tool (A or C) reads it → the form self-maintains as the schema
  evolves, no app redeploy to add a field.
- **"Suggest an edit" → structured issue → bot PR**: lowest friction for truly
  non-technical contributors — they fill a Forgejo issue *form template*; a small
  Forgejo Action turns it into a validated PR. No editor app at all.
- **Validator-as-CI is the real content-control win** and is independent of the
  editor: promote `scripts/validate_md.py` to a required check (Forgejo Actions)
  so *every* path (CMS, web editor, manual) is gated identically. Do this first —
  it's cheap and immediately fixes "no real control of contents."

## 6. Recommendation / phasing

1. **Now, cheap, high-value**: make `validate_md.py` a **required CI check** on
   the catalogue repo (Option E) + branch-protect `main`. Fixes content control
   regardless of editor; unblocks everything else.
2. **Fast win for non-technical editing**: stand up **Option A** (a git-CMS)
   against the Codeberg repo, gated behind Authelia. Lowest effort to "forms +
   review + merge," separate from `/browse`. Spike the Forgejo backend first to
   confirm it's smooth.
3. **If A's backend is awkward, or you want full integration / dirty-markers in
   `/browse`**: build **Option C** (proposals queue) — reuse `metadata.py`,
   apply principles 1–4 to keep the git step trivial.
4. **Anytime**: **Option D** for bulk edits.

The browse UI's only required change for any option is the **pending/dirty
badge** (§4.6) — driven by the open-proposals list (C) or open-PRs/branches (A/B).

## 7. Open decisions

- **Forge backend** for a CMS (Option A) on Codeberg/Forgejo — verify support /
  pick the CMS accordingly.
- **Authorship granularity**: per-user forge OAuth (clean `git blame`) vs a
  single bot token (simpler, coarser) — Authelia user can still be recorded in
  the proposal/commit message either way.
- **Confirm field home**: every editable field is a `.md` catalogue field (git),
  not an `.h5ad.mus` experimental field (different propagation). Check
  `h5adstore/CONVENTIONS.md`.
- **One-line summary** likely needs a *new* per-dataset frontmatter field (a
  per-collection description exists; a per-dataset one may not). Trivial schema
  add + a `validate_md.py` rule.

## 8. Ownership

- Validator-as-CI + forge config: **ops / data-repo**.
- Option A (CMS): mostly config + auth — **ops/web**.
- Option C (queue + write endpoint + git step): **ant-serve / anthive4**; the
  edit UI + dirty badges: **web**.
- `lib/metadata.py` / `ant-md` validators: shared, already exist.

---

# Implementation plan (DEFERRED — decided 2026-06-22, not scheduled)

This supersedes the exploratory tool-ranking in §5/Option A. Captured so it can
be picked up cold later.

## Decision: Decap CMS (not Sveltia) for the flat editable subset

Recon (June 2026) flipped the earlier pick:
- **Decap CMS has *shipped* native Gitea/Forgejo backend** — PKCE auth (no OAuth
  proxy needed), **editorial workflow (opens PRs)**. Needs Forgejo ≥ 1.21.4 +
  **CORS enabled on the instance**. (Codeberg runs recent Forgejo, so the
  version is fine; CORS is the open question — see spike.)
- **Sveltia's Forgejo backend is NOT built** (sveltia-cms issue #381, blocked on
  upstream Forgejo API, parked at 1.0-RC). Nicer tool, but unusable on Codeberg
  today → revisit later.
- **Pages CMS** = GitHub-only → out (we're moving to Codeberg).
- Refs: decapcms.org/docs/gitea-backend/ , decapcms.org/docs/backends-overview/ ,
  github.com/sveltia/sveltia-cms/issues/381.

## Scope (what the CMS owns)
Flat/global catalogue fields on **collection `index.md`** + **dataset `*.md`**
(and a few flat `*.mus` fields as a YAML data collection): organism, celltype,
tissue, disease, technology, suspension_type (vocab selects from a fixed global
list baked from `_mus_vocab.yaml`), year (int), authors (list), doi/pmid/
accession/link, license, contributor, access, source, **description (NEW
one-liner, both levels)**, markdown body.
- **Declare EVERY key** — including read-only ones (`sha256`, `n_obs`, `n_vars`,
  `version`, `created`) as `hidden` — or Decap drops undeclared frontmatter on
  save.
- **Out of CMS scope** (→ future in-house data-aware editor, §5 Option C):
  per-dataset column/layer/embedding assignments, `obs_exclude` complement,
  dependent fields, future per-column nice-names / per-category colours. A
  static-config CMS cannot know per-dataset structure or do cross-field logic.

## Auth / commit model
- **Gate the `/edit` app behind Authelia** by an `admin` group:
  - add the group in `config/authelia/users_database.yml` (users already carry
    `groups:`);
  - add an `access_control` rule (configuration.yml:`access_control:`) limiting
    the editor path to `subject: 'group:admin'`;
  - **map deny → 404 at the proxy** (Caddyfile/nginx) so it's *invisible* to
    non-admins, not just 403;
  - `/browse` hides the "✎ edit" affordances unless `Remote-Groups` (already
    forwarded) contains `admin`.
- **Commits**: native Decap PKCE → per-admin Codeberg OAuth (clean authorship).
  **Fallback if Codeberg CORS blocks browser calls**: a tiny same-origin
  **server-side API proxy holding a bot token** (forwards to codeberg.org/api),
  stamping the Authelia user as commit author. Self-hosted Forgejo (we control
  CORS) is the other fallback.

## Content control (do this first — cheap, editor-independent)
- Promote **`h5adstore/scripts/validate_md.py` to a required CI check** (Forgejo
  Actions) + branch-protect `main`. Bad frontmatter then can't merge no matter
  how it was edited.
- **Single schema source of truth**: one YAML in the repo (field → type, vocab,
  required, editable?) that drives BOTH `validate_md.py` AND the generated Decap
  config → adding a field = one edit, no drift. This is the "easily extensible"
  mechanism.
- Markdown sanitization at render (body is rendered `|safe`).

## Approval / submission route
Decap `publish_mode: editorial_workflow` → each save = a **branch + PR** on
Codeberg → CI runs the validator → **admin reviews diff + merges** → deployed
checkout `git pull` + `POST /admin/rescan` → live. Merged branches auto-deleted.
"Save ≠ live": edits are proposals until merged. Browse shows a **pending/dirty
badge** driven by open PRs (or the Option-C queue if that path is taken).

## THE SPIKE (make-or-break, ~1–2 h, throwaway, doesn't touch h5adstore)
The only real unknown is **CORS on `codeberg.org`** (Decap's Gitea backend calls
the Forgejo API directly from the browser).
1. Throwaway Codeberg repo (e.g. `mfiers/cms-test`) with one `content/foo.md`.
2. Register a Codeberg OAuth2 app (User Settings → Applications), redirect URI =
   the admin page origin, **uncheck "Confidential Client"**, copy Client ID.
3. Minimal `admin/index.html` (loads Decap) + `config.yml`:
   ```yaml
   backend:
     name: gitea
     repo: mfiers/cms-test
     base_url: https://codeberg.org
     api_root: https://codeberg.org/api/v1
     app_id: <client-id>
     auth_endpoint: login/oauth/authorize
     branch: main
   publish_mode: editorial_workflow
   collections:
     - {name: datasets, folder: content, extension: md, create: true,
        fields: [{name: title, widget: string},
                 {name: description, widget: string},
                 {name: body, widget: markdown}]}
   ```
4. Serve at the redirect origin; decisive checks (watch browser console):
   (a) PKCE login round-trips; (b) **file list loads ← the CORS test**;
   (c) Save → branch+PR created on Codeberg; (d) merge → commit lands.
- **All green** → native Decap-on-Codeberg viable, proceed.
- **CORS fails (a/b)** → switch to the **bot-token server proxy** (or self-host
  Forgejo). Editor stays Decap; only the transport changes.

## Phasing (when resumed)
0. **`description` field** (per-dataset + per-collection one-liner) — small,
   standalone, useful now: frontmatter key + `validate_md.py` rule + show on
   dataset/folder pages. Can land independently before any of the above.
1. `validate_md.py` → required CI + branch protection (content control).
2. Run THE SPIKE → decide native PKCE vs bot-proxy.
3. Stand up Decap at `/edit`, Authelia admin-gated + invisible; vocab selects
   from `_mus_vocab.yaml`; declare all keys (read-only as hidden).
4. `/browse` pending/dirty badges from open PRs.
5. (Later) Option C in-house data-aware editor for the structural/dependent/
   per-dataset-unique fields the CMS can't do.
