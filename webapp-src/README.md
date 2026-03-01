# Webapp Source Content

This directory contains source content for webapp components that get built into `../webapp/`.

## Structure

- **`index.html`** - Main webapp landing page
  - Copied to `../webapp/index.html` during build
  - Entry point for the Anthive webapp

- **`jupyter/`** - JupyterLite notebooks and content
  - Source for the browser-based Python notebook environment
  - Built to `../webapp/jupyter/` by `make build-jupyter`
  - Contains: Getting_Started.ipynb, examples, and helpers

- **`stlite/`** - Streamlit Lite source (planned)
  - Source for browser-based Streamlit apps
  - Will be built to `../webapp/stlite/`

## Build Process

```bash
# Build JupyterLite from jupyter/ content
make build-jupyter

# Build complete webapp (JupyterLite + landing page)
make build-webapp
```

## Notes

- `antclient.py` is copied from `../antclient/` during build (don't edit the copy)
- Build output goes to `../webapp/` (gitignored)
