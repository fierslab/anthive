# marimo (in-browser WASM notebooks)

Reactive Python notebooks that run entirely in the user's browser via
Pyodide. Sibling to the JupyterLite build at `webapp/jupyter/`.

## Source

```
webapp-src/marimo/
  index.html          # landing page listing notebooks (no Python)
  gene.py             # single-gene distribution (hist/ecdf/kde) + stats
  umap.py             # 2-D embedding scatter, coloured by gene/num/cat
  category.py         # box/violin/bar split by a categorical obs field
  numerical.py        # distribution of a numerical obs field
```

Each `.py` is a stand-alone marimo notebook with `@app.cell`-decorated
functions. They use the `antclient` package (installed at first run
with `micropip`) and `mo.query_params()` so URL parameters
(`?db_id=…&gene=…&api=…`) drive the initial state. Widget changes are
written back to the URL on the fly, so any view is one copy-paste away
from sharing.

## Build

```bash
make build-marimo
```

Wraps a per-notebook loop:

```bash
for nb in gene umap category numerical; do
  uvx marimo export html-wasm \
      webapp-src/marimo/$nb.py \
      -o webapp/marimo/$nb --mode run
done
cp webapp-src/marimo/index.html webapp/marimo/index.html
```

`--mode run` builds a read-only view (no editor UI).  Swap to
`--mode edit` if you want a notebook to be editable in the browser
(adds the marimo editor; larger bundle).

## Output

```
webapp/marimo/
  index.html          # landing page (lists the four notebooks)
  gene/index.html     # gene notebook + its assets
  gene/assets/
  gene/files/
  umap/…
  category/…
  numerical/…
```

Each notebook is ~10 MB compressed. The landing page is plain HTML
(<2 KB) with relative links to each notebook subfolder.

`make build-marimo` strips `webapp/marimo/<nb>/CLAUDE.md` (marimo's
default template scaffolding) on every build.

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
https://data.nevel.eu/marimo/gene/?db_id=P26%2FHMI&gene=TREM2
https://data.nevel.eu/marimo/umap/?db_id=P26%2FHMI&gene=APOE
https://data.nevel.eu/marimo/category/?db_id=S25%2FPhM&gene=TREM2
https://data.nevel.eu/marimo/numerical/?db_id=S25%2FPhM&field=Sierksma2025::WGCNA::Black_Mito
```

`/` must be percent-encoded as `%2F` in db_ids — most browsers handle it
automatically.

## What each notebook does

**gene/** — gene expression distribution
1. Picks a dataset and a gene.
2. Pulls `client.get_cells(db, genes=[gene], format='dataframe')`.
3. Renders histogram / ECDF / KDE (selector) of expression.
4. Summary stats table (mean, std, quantiles, % non-zero).

**umap/** — embedding scatter
1. Filters datasets to those that have a stored embedding.
2. Picks the embedding, then a colour mode (Gene / Numerical / Categorical).
3. Fetches the embedding coords + the colour values, joins by cell.
4. Renders a 2-D scatter with viridis (continuous) or tab20 (categorical).

**category/** — categorical breakdown
1. Picks a categorical obs field and a Y axis (Gene / Numerical / Count).
2. Pulls cells with the relevant columns.
3. Renders box / violin / bar split by the categorical field.
4. Per-category summary stats table.

**numerical/** — numerical obs distribution
1. Picks a numerical obs field (QC metric, module score, …).
2. Optional grouping by a categorical field.
3. Histogram / ECDF / KDE (no grouping) or box / violin / bar (with grouping).

All four mirror the current widget state into the URL on every change.

## Limitations

- Pyodide bundle download is ~10 MB per notebook on cold load; cached
  for subsequent visits (per-notebook subfolders cache independently).
- Some packages (heavy C extensions) don't build for Pyodide. Keep
  dependencies minimal — current set is `antclient + pandas +
  matplotlib + seaborn`.
- For a "live edit" experience, swap the build mode to `edit`.
