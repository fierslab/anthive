# UX Consistency Punch List (2026-08-18)

Audit scope: `index.html`, `folder.html`, `dataset.html`, `plot.html`,
`genesets.html`, `geneset.html`, `geneset_experiment.html`,
`geneset_starcat.html`, `geneset_variability.html`, `genes.html`,
`de_browser.html`, `about.html`.

Items **already fixed in this session** are marked ✓.

---

## Fixed

- ✓ Breadcrumb separator uniformity — all pages now use `<span class="bc-sep">/</span>`
  (was a mix of `&rsaquo;`, ` / ` literal, and `bc-sep`; `.bc-sep` moved to
  `base.html` so it's sitewide).
- ✓ `geneset.html` breadcrumb — `study` was plain text; now links to
  `/genesets/{study}/{experiment}` with proper `bc-sep` separators.
- ✓ `geneset_experiment.html` breadcrumb — same fix.
- ✓ `geneset_starcat.html` table-wrap — `<table class="wtable">` was inside a
  plain `<div id="sc-body">`; wrapped in `<div class="table-wrap">`.
- ✓ `_module_view.html` `mv-body` — added `overflow-x:auto` (was `overflow-y`
  only); wide table would overflow on narrow screens.

## Pending ant-serve endpoints (web UI graceful-fallback, implement in API session)

8. ✓ **Genes tab: genome-wide top-variable genes** — DONE (2026-08-20). Added
   `?top=N` to `/databases/{id}/gene_stats`; `dataset.html` JS now calls it for
   datasets with no curated panel.

## Remaining (non-trivial or out of scope for this session)

1. **`genesets.html` has no `<h1>` / page title** (`geneset_variability.html`,
   `geneset_starcat.html`, and all dataset pages have one). The tabs serve as
   the visual title but there's no semantic heading. `genesets.html:24`
   (`stats-row`) is the first content.

2. **`_module_view.html` filter state not persisted via `antRemember`**
   (`web/CLAUDE.md` says selection state that isn't in the URL should survive
   reload). The tag/method/sort selects reset on navigation. Complication:
   the module dropdown is pre-seeded from context on the geneset page, so a
   global `antRemember` key would conflict across catalogs. Needs a
   context-qualified key like `mv.module.{catalog}`.

3. **`genes.html` multi-gene heatmap metric selector** — not `antRemember`-
   persisted (`genes.html:~330`). Single-gene organism/cell/tissue filters
   already are.

4. **`about.html` has no breadcrumb** — it's a standalone page; minor but
   inconsistent with `privacy.html` which has one.

5. **`geneset_variability.html` heatmap table** (`<div class="mv-wrap">`) — its
   inner table is not in `<div class="table-wrap">`; it has custom overflow
   CSS instead (`geneset_variability.html:~22`). Functional but diverges from
   the convention. Refactor after confirming the custom scroll behaviour is
   still needed.

6. **`index.html` collection table filter popovers** — click-outside-to-close
   guard is load-bearing (see `web/CLAUDE.md` gotchas). Any refactor of that
   block must preserve the `activePopover.contains(e.target)` early-return.

7. **`geneset_experiment.html` parameters table** — `<td class="muted mono">`
   applies both classes inline; could be `class="muted-2 mono"` to use the
   DRY utility (minor).

---

## Deferred MED/LOW items from 2026-08-20 independent audit

9. **Tab labels glued to count badge** — `Genesets42`, `Numerical7` etc. appear
   without a space before the count badge. Add `gap` or a space before `<span
   class="tab-count">`. Affects `dataset.html` tab bar.

10. **Folder header cryptic `L` / `E` columns** — `L` = layer count, `E` =
    embedding count. Column headers in `folder.html` are single letters with
    tooltip; add full `<th title="layers">L</th>` labels or expand to words.
    See `folder.html:133–134`.

11. **Score cells `0.341ω²` missing separator** — in `_module_view.html` the
    score value and its unit (ω²/eta²/R²) are concatenated without a space.

12. **No breadcrumb on `/` and `/genesets`** — landing and gene-set pages have
    no `<nav>` breadcrumb (they're top-level, so this is debatable; a "home"
    marker is the minimum). `about.html` also lacks one.

13. **Plot page missing visualize dropdown** — the `plot.html` geom-switcher
    has no dropdown that maps to the dataset's full `visualize ▾` menu. Users
    navigating from a plot page can't see what other geoms are available without
    returning to the dataset page.

14. **Dataset title duplicate link icons** — when a dataset has both a `doi`
    and an `accession`, both 🔗 icons appear immediately after the title in
    `folder.html`. They are distinguishable by tooltip only; add a short label
    or replace with `DOI` / `accession` text links.

15. **Landing popover missing select-all / none** — column filter popovers in
    `index.html` have per-value checkboxes but no "select all" / "clear" shortcut
    inside the popover body (only a per-column `clear` link at the bottom).

16. **Chat widget 401 pollution** — when Authelia is active, the chat status
    probe (`GET /api/chat/status`) occasionally triggers a 401 in the browser
    console before the Authelia cookie is forwarded. Not a bug (the probe
    gracefully hides the launcher), but it pollutes devtools. Fix: add a 1-retry
    after 500 ms before giving up, or gate the probe on `document.cookie` presence.
