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
