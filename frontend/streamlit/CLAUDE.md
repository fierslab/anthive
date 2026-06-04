# frontend/streamlit/ — Streamlit app source

Custom exploratory dashboards on top of the anthive REST API.

## Running

Production (managed by supervisor):
```
ant-supervisor   # boots streamlit at frontend/streamlit/streamlit_app.py
                 # baseUrlPath=streamlit, port=8082
                 # reached via Caddy at :8501/streamlit
```

Standalone:
```
make serve-streamlit             # uvx-based, no supervisor
./bin/ops/streamlit-serve        # equivalent
```

Public URL through Caddy: `http://localhost:8501/streamlit`

## Where API calls go

Through `antclient` (sibling project at repo root). The base URL is built
inside the Streamlit code — read it from there if you need to change
it.

## Mirror

`bin/ops/sync-to-stlite` keeps an in-browser stlite copy under
`frontend/jupyter/stlite/` in sync with the Python version here. Run it
manually after meaningful changes — it's not on any build target.
