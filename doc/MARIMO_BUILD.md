# marimo (in-browser WASM notebook)

A reactive Python notebook that runs entirely in the user's browser via
Pyodide. Sibling to the JupyterLite build at `webapp/jupyter/`.

## Source

```
webapp-src/marimo/explore.py        # the notebook (a regular .py file
                                    # with @app.cell-decorated functions)
```

`explore.py` uses the `antclient` package (installed at first run with
`micropip`) and `mo.query_params()` so that URL parameters
(`?db_id=…&gene=…&api=…`) drive the initial state.  Widget changes are
written back to the URL on the fly, so any view is one copy-paste away
from sharing.

## Build

```bash
make build-marimo
```

Wraps:

```bash
uvx marimo export html-wasm \
    webapp-src/marimo/explore.py \
    -o webapp/marimo --mode run
```

`--mode run` builds a read-only view (no editor UI).  Swap to
`--mode edit` if you want the notebook to be editable in the browser
(adds the marimo editor; larger bundle).

## Output

```
webapp/marimo/
  index.html        # standalone HTML — opens & runs in the browser
  assets/           # bundled JS, CSS, fonts
  files/            # marimo runtime assets
  *.png  *.ico      # PWA icons
```

Bundle size ≈ 27 MB — same order as the JupyterLite build.

`make build-marimo` strips `webapp/marimo/CLAUDE.md` (marimo's default
template scaffolding) on every build.

## How it's surfaced

`webapp-src/index.html` and `webapp/index.html` both have an
`<a href="marimo/">` tile next to JupyterLite.  `build-webapp` now
depends on both `build-jupyter` and `build-marimo`.

## Query parameters

| key      | default                  | example                        |
| -------- | ------------------------ | ------------------------------ |
| `db_id`  | `P26/HMI`                | `?db_id=M24/M23`               |
| `gene`   | `TREM2`                  | `?gene=APOE`                   |
| `api`    | `https://data.nevel.eu/api` | `?api=http://localhost:8080/api` |

A real-world link looks like:

```
https://data.nevel.eu/marimo/?db_id=P26%2FHMI&gene=TREM2
```

`/` must be percent-encoded as `%2F` in db_ids — most browsers handle it
automatically.

## What the notebook does

1. Installs `antclient` + `matplotlib` via `micropip` (first cell only).
2. Reads the URL parameters once.
3. Renders a database picker (dropdown, searchable) and a gene-name text box
   bound to the URL parameters.
4. Shows a metadata card for the chosen dataset (n_cells / n_genes / layers /
   obs).
5. Searches the gene name against the dataset's gene index.
6. Shows per-gene statistics from `/gene_stats`.
7. Pulls a 5 000-cell sample and plots: a histogram + a per-category box plot
   of expression (using the first categorical obs column as colour).
8. Mirrors the current widget state back into the URL so the view is
   reproducible.

## Limitations

- Pyodide bundle download is ~10 MB on cold load; cached for subsequent
  visits.
- Some packages (heavy C extensions) don't build for Pyodide.  Keep
  dependencies minimal — `antclient` + `pandas` + `matplotlib` is the
  current full set.
- For a "live edit" experience, swap the build mode to `edit`.
