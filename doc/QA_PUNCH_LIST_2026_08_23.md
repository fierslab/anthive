# Pre-production QA punch list — 2026-08-23

Consolidated from 4 parallel `ruthless-qa-critic` audits (landing/collections,
dataset page, geneset browsers, gene atlas + settings + plot + DE).

## Critical — MUST fix before production push

- **P0.1** `/api/gene_stats_all` **hangs >60s** — flagship gene-atlas broken cold. Needs: server-side speedup OR client AbortController + skeleton state.
  - `web/templates/genes.html:836` (fetch); `bin/api/ant-serve:3276` (endpoint)
- **P0.2** `/api/gene_stats?top=20` also slow (60s on L26/FMG, 33s L26/PSY, 8-15s others). Same class as P0.1 — need spinner + timeout.
  - `web/templates/dataset.html:1373` (`fetchTopVarGenomeWide()`)
- **P0.3** **Stored XSS in favourite genes** — sanitised on write, NOT on read; `dataset.html:1201-1220` concatenates favourites into raw HTML. Any same-origin write (bookmarklet, extension) → XSS on Genes tab.
  - `web/templates/dataset.html` (favourite concat); `web/templates/base.html:antSettings.favouriteGenes()`
- **P0.4** L26/PSY Genesets tab shows raw internal command: `"No module-variability rows in module_variability.parquet for L26/PSY. Regenerate via ant module-variability and POST /module-variability/rescan."` — leak of implementation into UI. Downgrade to user-facing message.
  - `web/templates/_module_view.html` empty-state
- **P0.5** `/module-variability?db_id=WA4/MPF&min_abs=0` returns **500**. Verify endpoint OR the client never falls through to it silently.

## High — should fix before push

- **P1.1** `mff-none` "select none" button label lies — clicking sets zero checkboxes but downstream code treats null-selection as "all rows" → shows every row. Fix: either rename to "clear selection" (semantic honesty) or change downstream to interpret null = zero rows.
  - `web/templates/_macros.html:161-168` (button), `_macros.html:96-97` (getValues null path)
- **P1.2** Empty-state row `colspan=8` always, but module/dataset scopes render only 7 columns → wrong-width empty message.
  - `web/templates/_module_view.html`
- **P1.3** `preselect_cell_types` sent by server, but client-side `CTX.scope !== 'module'` guard blocks it → microglia preselect never fires on single-geneset drilldown (breaks intent of commit c2b42c1).
  - `web/templates/_module_view.html:1030-1044`
- **P1.4** Deep-link `#modules&mv=…` — hash parsed on cold load but never wired into widgets → filter row shows "all", table shows all, but URL says filtered. Silent divergence.
  - `web/templates/_module_view.html:586-611`
- **P1.5** `n_module_scores` silent 500-row cap. Tab label says "138 genesets" but tbody shows 500 rows and no truncation hint.
  - `web/templates/_module_view.html` (the `.slice(0, 500)` cap)
- **P1.6** Folder `L`/`E` cryptic column headers (deferred from previous audit).
  - `web/templates/folder.html:133-134`
- **P1.7** Folder pages have NO column popovers at all — inconsistent with landing (which does); header cursor:pointer hints at interactivity but nothing happens.
  - `web/templates/folder.html`
- **P1.8** Landing card-mode: empty `<td>` with whitespace fails `:empty` CSS collapse → bare `DISEASE :` label rows on mobile.
  - `web/templates/index.html:199-201` (and Org/Tech/Cell)
- **P1.9** No loading indicator on Genes/Numerical/Categorical/Genesets tabs — combined with 8-60s latency reads as broken.
  - `web/templates/dataset.html`
- **P1.10** Silent `.catch(function(){})` on `fetchTopVarGenomeWide` and `fetchAndHydrateCurated` — HTTP failures swallowed.
  - `web/templates/dataset.html:1373`
- **P1.11** `<span class="info">ⓘ</span>` inside sortable `<th>` bubbles click → spurious sort toggle. Add `onclick="event.stopPropagation()"`.
  - `web/templates/_module_view.html:247` (peer-modules th)
- **P1.12** `/genesets` client-redirect trusts stale `antRemember.gs.tab` — invalid remembered tab → URL lies (server falls back but URL stays).
  - `web/templates/genesets.html:44-51`
- **P1.13** ★ favourite star only shows in single-gene mode of gene atlas, not multi-gene heatmap row headers.
  - `web/templates/genes.html`
- **P1.14** `/settings` topbar link gated on Authelia `remote-user` header — un-Authelia direct users can't reach a per-browser feature.
  - `web/templates/base.html:504-505`
- **P1.15** Deep-link tab hash toggles `display:none` but NOT `hidden` attr → screen readers announce all six panels as visible.
  - `web/templates/dataset.html:activateTab`
- **P1.16** Downloads section on dataset page only shows h5ad. `bin/api/CLAUDE.md` promises kinds `h5ad / duckdb / scores / mus`. Verify template or backend.

## Medium

- **P2.1** Chat launcher overlaps card content on 640px viewport.
- **P2.2** Multi-chip organism cell renders `"Hs\nMm"` with literal newline in a single `<td>` — screen-reader reads "newline", copy-paste keeps newline.
- **P2.3** Multi-gene heatmap metric selector not `antRemember`-persisted.
- **P2.4** DE browser has no MA link — first-class DE geom missing.
  - `web/templates/de_browser.html:300-313`
- **P2.5** Dangling `<hr class="tip-sep">` in dataset popover when collection name set but description empty.
- **P2.6** `_build_dataset_annotations` — collection_meta lookup lets `.tip-coll-name` be blank while `<hr>` still renders.
- **P2.7** `parseGenes()` in settings silently discards unicode-hyphen tokens (`MT‑CO1`) without feedback.
- **P2.8** `resetSettings()` doesn't nudge open pages to refresh.
- **P2.9** Silent truncation at `limit=5000` in variability heatmap fetch, no "truncated at N rows" hint.
  - `web/templates/geneset_variability.html:344-346`
- **P2.10** Landing `Ⓘ` glyph has no `aria-label`, screen readers announce Unicode name.

## Low / nitpicks

- **P3.1** Peer-position `.mv-mark` clamping — mark can be 3px LEFT of track at value=0. Clamp within track bounds.
- **P3.2** starCAT catalog group label rewrites `_` to space but `catalog.name` (dots-in-name) passes through raw — cosmetic inconsistency.
- **P3.3** `genset.html:29` — `parent_dataset.split('/')[0]/[1]` unguarded against 1- or 3-segment paths.
- **P3.4** Dead pending-fallback in `genesets.html:158` (bygene endpoint now shipped).
- **P3.5** LC5/XNB dataset page never reaches `networkidle` — some long-polling request loops.
- **P3.6** ★ marker has no aria-label beyond `title=`.
- **P3.7** `../index.md` link in L26/FMG overview markdown renders as inert text.

## Positive verifications (auditors confirmed working)

- starcat obs cols kept out of Numerical/Categorical (`5406570`)
- scanpy-only method column (`515d6b6`)
- canonical bold+pill (`1b308f1`)
- two UMAP icons per row (`f6f5fcd`)
- mff popovers open with all/none (`e25041e`, `a16e6be`)
- Caveats tab gated correctly
- DE tab gated correctly
- Deprecation header fires (`0509050`)
- Tab-count spacing OK (auditor 4 explicitly cleared this deferred item)
- Tissue tree parent cascade (`6a9ddcb`)
- Rich dataset popover (`56bc919`)
- Peer-position sparkline sans range (`7f6dc44`)
- n_cells buckets (`e72ab71`)
- URL hash `#mv=sort=col:dir` persists across reload
- 13 canonical tags in variability heatmap dropdown (`3e75c88`)
