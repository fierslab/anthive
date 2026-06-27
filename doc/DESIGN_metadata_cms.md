# DESIGN — Metadata editing CMS (build design)

Status: **approach proven, ready to schedule.** The make-or-break spike passed
live on 2026-06-27 (CORS + PKCE login + browser-direct write to Codeberg — see
`DESIGN_metadata_editing.md` for the exploration trail, options comparison, and
spike log). This doc is the build-ready design: what to build, who owns each
piece, and in what order. Nothing here is built yet.

Companion docs: `DESIGN_metadata_editing.md` (why Decap, why Codeberg, spike
log); `data/h5adstore/CONVENTIONS.md` (the `.md` field model the CMS edits);
h5adstore `scripts/validate_md.py` (the validator that becomes CI).

## 1. Goal & scope

Let non-technical curators edit dataset **catalogue** metadata through a forms UI
(dropdowns, not raw YAML), with a review gate before anything reaches the
canonical catalogue, and per-edit attribution.

**In scope** — the curated `.md` frontmatter fields:
organism · celltype · tissue · disease · technology · suspension_type · year ·
doi / pubmed / accession / link · license · contributor · access · source ·
**description (one-line — NEW field)** · the markdown body. Both the per-dataset
`*.md` and the per-collection `index.md`.

**Out of scope** — experimental annotation (`*.h5ad.mus`, obs columns); that's a
different problem (`DESIGN_annotation_updates.md`) with its own tooling (`mus`).
Bulk/programmatic edits stay on `ant-md`. No data (`.h5ad`/`.duckdb`) is touched.

## 2. Proven foundation (the spike)

- Decap CMS (static admin page) ↔ Codeberg Forgejo API, **browser-direct, no
  proxy, no self-hosted Forgejo**. CORS is open (`*`, all methods, `Authorization`
  allowed); PKCE login works; edit → Save commits to the configured branch.
- **The one constraint that shapes everything below:** the Decap **Gitea backend
  has no `editorial_workflow`** (no PR-per-save). It commits directly to a
  branch. So the review gate is a *branch*, not per-edit PRs (§5).

## 3. Architecture

```
 admin curator (in the Authelia admin group)
   │  browser → https://<host>/cms/         (static Decap page, Authelia-gated)
   ▼
 Decap CMS  ── reads /cms/config.yml (field schema + controlled vocab)
   │  OAuth PKCE login to Codeberg (per-user) ; token in Authorization
   ▼
 Codeberg (Forgejo)  ── commits to the `cms-staging` branch of the data repo
   │
   │  validate_md.py runs as CI on cms-staging  (blocks bad frontmatter)
   ▼
 maintainer merges cms-staging → main  (forge PR, the review gate)
   │
   ▼
 main = canonical catalogue  ──▶  server pulls h5adstore + ant-serve rescan ──▶ live
```

Key point: an edit is **not live** until (a) it's merged to `main` and (b) the
server pulls the data repo and ant-serve rescans. That propagation step is an
ops concern (§8), not instant.

## 4. Editable content model (Decap collections + widgets)

Two Decap collections, both `extension: md`, `format: yaml-frontmatter`:

- **`datasets`** → the per-dataset `*.md` files (folder = the dataset glob).
- **`collections`** → the per-collection `index.md` files.

Widgets, chosen so curators pick rather than type:

| Field | Widget | Notes |
|---|---|---|
| organism, celltype, tissue, disease, technology, suspension_type, license, access, source | `select` | options from the **controlled vocab** (§7), `multiple` where the field is list-valued |
| description | `string` | the new one-liner; `pattern` to cap length |
| year | `number` | min/max guard |
| doi, pubmed, accession, link, contributor | `string` | `pattern` validation where formats are known (DOI, URL) |
| body | `markdown` | the prose section |

**Field set must be easily extensible** — adding a field = one entry in the
generated config (§7), no code.

## 5. Repository & branch model (the review gate)

Because there's no per-save PR:

- Decap's `config.yml` sets **`branch: cms-staging`**. Every Save commits there;
  `main` is never written by the CMS.
- `cms-staging` is **branch-protected** on Codeberg (no direct pushes except the
  CMS/curators; required CI status = `validate_md`).
- A maintainer periodically opens/merges a **`cms-staging → main` PR** on the
  forge — this is the human review gate. Batched, not per-edit (acceptable;
  arguably simpler to operate).
- After merge, `cms-staging` is kept in sync with `main` (fast-forward or reset)
  so the next batch starts clean.

If per-edit PRs ever become a hard requirement: add a small bot-token **server
proxy** that opens one PR per save; the editor stays Decap. Not planned.

## 6. Auth & access (two independent layers)

1. **Who can reach the CMS page** — Authelia. `/cms/*` is gated to an admin
   group (e.g. `metadata-admins` in `users_database.yml`, enforced in
   `access_control:`). Non-admins get nothing — no nav link, route 403/404. This
   is the "must remain fully invisible to anybody else" requirement.
2. **Who Decap commits as** — Codeberg OAuth (PKCE). For **per-edit attribution**
   each curator logs in with their **own Codeberg account**, which needs write
   access to the data repo (a Codeberg team on the repo). Fallback if curators
   lack Codeberg accounts: a **shared bot token** (loses git-level attribution,
   but Authelia still records who reached the page). *Open decision (§11).*

A prod Codeberg **OAuth2 app** is registered once (redirect URI = `https://<host>/cms/`,
Confidential Client off). Client ID lives in `config.yml` (not secret).

## 7. Content validation & vocab — single source of truth

The risk is **drift**: Decap's dropdown options vs. what `validate_md.py`
accepts. Avoid it:

- The controlled vocabularies live **once** (in `validate_md.py` / the h5adstore
  conventions, or `lib/metadata.py`). A small **generator** emits the Decap
  `config.yml` `select` options from that same source. Regenerate when the vocab
  changes; never hand-edit option lists.
- **`validate_md.py` runs as CI** on `cms-staging` (and the staging→main PR) — the
  authoritative gate. Decap widgets give *fast* feedback (dropdowns, patterns);
  CI is the *binding* check.

## 8. Hosting / deployment

- **Static `/cms/`** served by Caddy (the single routing source), behind the
  Authelia admin gate — the "separate tool next to `/browse`". No coupling to
  ant-browse's render path.
- **Vendor `decap-cms.js` locally** under `/cms/` — do **not** depend on `unpkg`
  in prod (internal box; matches the repo's self-contained convention). The
  spike's CDN load was a throwaway shortcut.
- **Propagation:** on merge to `main`, the server must `git pull` the h5adstore
  repo and trigger an ant-serve rescan. Either a Codeberg webhook → a small
  endpoint, or a scheduled pull. Until then edits aren't visible in the browser.

## 9. Implementation phases

1. **Vocab generator** → `config.yml` select options from the canonical vocab.
2. **`/cms/` static page** (vendored Decap) + generated `config.yml`; Caddy route
   + Authelia admin-group gate.
3. **Codeberg prod OAuth app** + curator write access (or bot token).
4. **`cms-staging` branch** + protection + **`validate_md.py` as CI** on the data
   repo.
5. **Propagation** — pull + rescan on merge to `main` (webhook or scheduled).
6. **Polish** — field help text, Decap preview, surface the new `description`
   field in the web UI (dataset page + list).

## 10. Ownership (multi-domain — needs coordination)

- **web session:** the `/cms/` static page, vendored Decap, the generated
  `config.yml`, the Caddy `/cms/` route, surfacing `description` in the UI.
- **anthive4 / data:** the canonical vocab + `validate_md.py` (h5adstore), the
  `description` field in the `.md` schema + `ant-md`, the ant-serve rescan
  trigger, the vocab generator if it lives in `lib/`.
- **ops:** Codeberg prod OAuth app, curator Codeberg accounts/team, `cms-staging`
  branch protection + CI runner, Authelia `metadata-admins` group, the
  pull+rescan automation on the server.

## 11. Open decisions

- **Per-user Codeberg accounts vs. a shared bot token** for commit identity
  (attribution vs. onboarding friction). §6.
- **Batch review cadence** — who owns merging `cms-staging → main`, how often.
- **Propagation trigger** — webhook vs. scheduled pull+rescan.
- **`description` rollout** — add to the `.md` schema + validator + surface in the
  UI independently of the CMS (it's useful on its own).
- **Where the vocab generator lives** (`lib/` vs. h5adstore script).

## 12. Spike teardown

When scheduling the real build, delete the throwaway: `~/cms-spike`, the
Codeberg `cms-test` repo, and the `cms-spike` OAuth app.
