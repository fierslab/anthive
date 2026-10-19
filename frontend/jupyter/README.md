# frontend/jupyter/ — webapp source content

This directory contains source content for webapp components that get
built into `../webapp/`.

## Structure

- **`index.html`** — Main webapp landing page.
  Copied to `../webapp/index.html` during `make build-webapp`.

- **`jupyter/`** — JupyterLite notebooks and content.
  Source for the browser-based Python notebook environment. Built to
  `../webapp/jupyter/` by `make build-jupyter`. Contains:
  - `Getting_Started.ipynb`
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
  treats it as a top-level package, so `from vark import AnthiveClient`
  works without `micropip.install('pyvark')`. Notebooks must install
  `requests` (vark's HTTP layer) with micropip first. To bump the client,
  run `make vendor-pyvark PYVARK=<pyvark checkout>`; it records version,
  commit and hashes in `vendored_pyvark.json`.
- `make test` runs the static checks, `make test-jupyter` the headless
  browser check and `make test-live-site URL=...` the deployed-site check
  (`doc/JUPYTERLITE_BUILD.md`).
- Build output goes to `../webapp/` (gitignored).
- The Streamlit dashboard (`frontend/streamlit/`) was removed
  2026-06-22 — `/streamlit` on the reverse proxy continues to return
  410 for anyone with a bookmark.
