# JupyterLite Build Configuration

## Overview

JupyterLite is built using `uvx` (uv's executable runner) which handles dependencies dynamically without needing a traditional requirements.txt file.

## Build Command

The build is configured in the Makefile:

```bash
make build-jupyter
```

This runs:
```bash
uvx --from jupyterlite-core \
    --with jupyterlite-pyodide-kernel \
    --with jupyterlab-widgets \
    --with jupyter-server \
    jupyter-lite build \
    --contents frontend/jupyter/jupyter \
    --output-dir webapp/jupyter
```

## Dependencies

### Core Build Dependencies (via uvx)

- **jupyterlite-core** - Core JupyterLite build system
- **jupyterlite-pyodide-kernel** - Pyodide kernel for running Python in browser
- **jupyterlab-widgets** - ipywidgets support (interactive widgets)
- **jupyter-server** - Server components

### Runtime Dependencies (in-browser)

These are loaded by Pyodide in the browser:
- numpy
- pandas
- matplotlib
- ipywidgets (via jupyterlab-widgets)
- Any other packages users install with `%pip install` in notebooks

## Configuration

**jupyter-lite.json** - Main configuration file
- Sets app name and version
- Configures theme
- Lists federated extensions (including widgets)
- Build settings

**frontend/jupyter/jupyter/** - Source content
- Contains notebooks that will be available
- antclient.py is copied here during build
- anthelper.py provides helper functions

## Adding Extensions

To add a JupyterLab extension to the build:

1. Add `--with extension-name` to the uvx command in Makefile
2. Or add the extension URL to `federated_extensions` in jupyter-lite.json

Example:
```bash
uvx --from jupyterlite-core \
    --with jupyterlite-pyodide-kernel \
    --with jupyterlab-widgets \
    --with jupyterlab-plotly \
    --with jupyter-server \
    jupyter-lite build ...
```

## ipywidgets Support

ipywidgets (interactive widgets) are now included via:
1. `--with jupyterlab-widgets` in the build command
2. CDN URL in jupyter-lite.json federated_extensions

Users can now use widgets in notebooks:
```python
import ipywidgets as widgets
slider = widgets.IntSlider(min=0, max=100, value=50)
slider
```

## Troubleshooting

### Widgets not appearing
- Rebuild: `make build-jupyter`
- Check browser console for errors
- Verify jupyterlab-widgets is in the build command

### Build fails
- Check uvx is available: `uvx --version`
- Try with verbose: `uvx --verbose ...`
- Check network connectivity (downloads packages)

### Packages not available in notebooks
- Packages must be Pyodide-compatible
- Install in notebook: `%pip install package-name`
- Some packages with C extensions may not work

## References

- [JupyterLite Documentation](https://jupyterlite.readthedocs.io/)
- [Pyodide Package Index](https://pyodide.org/en/stable/usage/packages-in-pyodide.html)
- [ipywidgets Documentation](https://ipywidgets.readthedocs.io/)
