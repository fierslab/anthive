# frontend/jupyter/ — webapp source content

This directory contains source content for webapp components that get
built into `../webapp/`.

## Structure

- **`index.html`** — Main webapp landing page.
  Copied to `../webapp/index.html` during `make build-webapp`.

- **`jupyter/`** — JupyterLite notebooks and content.
  Source for the browser-based Python notebook environment. Built to
  `../webapp/jupyter/` by `make build-jupyter`. Contains:
  - `Getting_Started.ipynb`, `Empty.ipynb`, `Emily.ipynb`
  - `examples/` — themed notebooks
  - `vark/` — vendored copy of the Python client
    ([`codeberg.org/mfiers/pyvark`](https://codeberg.org/mfiers/pyvark))

## Build process

```bash
# Build JupyterLite from jupyter/ content
make build-jupyter

# Build the complete webapp (JupyterLite + landing page)
make build-webapp
```

## Notes

- The `vark/` package is **vendored** under `jupyter/vark/`. Pyodide
  treats it as a top-level package — `from vark import AnthiveClient`
  works without `micropip.install('pyvark')`. To bump the client,
  copy the latest `vark/*.py` files from a checkout of
  [`pyvark`](https://codeberg.org/mfiers/pyvark) over this directory.
- Build output goes to `../webapp/` (gitignored).
- The Streamlit dashboard (`frontend/streamlit/`) was removed
  2026-06-22 — `/streamlit` on the reverse proxy continues to return
  410 for anyone with a bookmark.
