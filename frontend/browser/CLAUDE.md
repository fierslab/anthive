# frontend/browser/ — server-rendered web UI source

Jinja2 templates + static assets + FastAPI router for `bin/web/ant-browse`.

## Layout

```
frontend/browser/
  browser.py        # the FastAPI router (mounted at /)
  templates/        # Jinja2 templates
    base.html       # shared chrome (nav, footer, storage notice)
    index.html      # landing — collection list with context pills
    folder.html     # one collection — per-dataset table
    dataset.html    # one dataset — info, biology, provenance, obs cols, viz
    plot.html       # in-app plot UI (form + preview + downloads)
    genesets.html, geneset.html, geneset_experiment.html
  static/           # CSS, JS, fonts (loaded via /static)
```

## How it's loaded

`bin/web/ant-browse` imports `browser.py` dynamically:
```python
browser_dir = script_dir.parent.parent / 'frontend' / 'browser'
spec = importlib.util.spec_from_file_location(
    "frontend_browser",
    browser_dir / "browser.py",
)
```
The Python module name `frontend_browser` is just an internal label — no
on-disk `__init__.py` needed.

`templates_dir` resolves relative to this folder via
`Path(__file__).parent / 'templates'`.

## What lives where

| Concern | File |
| --- | --- |
| Routes, response-shape, page context building | `browser.py` |
| HTML layout, blocks | `templates/*.html` |
| Per-page client-side JS (filter, group, prefill) | inline in the matching `.html` |
| Sitewide styles | `static/anthive.css` (loaded by `base.html`) |
| The plot form + form-prefill logic | `plot.html` (uses `plot_meta.top_genes/top_cat/top_num`) |

## Vocabulary shared across pages

- **Colored "context" pills** (organism / tech / cell / tissue / disease /
  prep / access / source / year) appear on the landing, the folder page,
  and the dataset page. Palette is `.ctx-pill` in `index.html`, mirrored
  by `.grp-pill` in `folder.html` and `.ds-ctx .pill` in `dataset.html`.
- **Abbreviation maps** are in `browser.py` (`_CELLTYPE_ABBREV`,
  `_TISSUE_ABBREV`, `_DISEASE_ABBREV`, `_ORGANISM_ABBREV`,
  `_TECHNOLOGY_ABBREV`, `_ACCESS_ABBREV`, `_SOURCE_ABBREV`,
  `_SUSPENSION_ABBREV`). Hover-tooltip carries the full term.
- **Collapsible obs-prefix groups** on the dataset page: items sharing a
  prefix (delimited by `::` or `__`) collapse into one `[+] prefix:: (N)`
  chip. Implementation is purely client-side JS in `dataset.html`.
