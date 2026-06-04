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

- **`stlite/`** - Streamlit Lite source
  - Source for browser-based Streamlit apps
  - Built to `../webapp/stlite/` by `make build-stlite`
  - Contains: streamlit_app.py, components.py, pages/, etc.

## Build Process

```bash
# Build JupyterLite from jupyter/ content
make build-jupyter

# Build Streamlit Lite from stlite/ content
make build-stlite

# Build complete webapp (JupyterLite + Streamlit Lite + landing page)
make build-webapp
```

## Notes

- `antclient.py` is auto-copied from `../antclient/` to both `jupyter/` and `stlite/` during build
- Don't edit the copied `antclient.py` files - edit the source in `../antclient/`
- Both `jupyter/antclient.py` and `stlite/antclient.py` are gitignored
- Build output goes to `../webapp/` (gitignored)
