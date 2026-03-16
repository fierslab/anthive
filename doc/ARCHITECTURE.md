# Anthive4 Architecture Overview

## System Components

### Core Services

1. **REST API** (`bin/ant-serve`)
   - Port: 8080
   - Purpose: Core data API for querying single-cell databases
   - Tech: FastAPI with DuckDB backend
   - Docs: `REST_API_SPEC.md`, `REST_API_COMPLETE.md`

2. **Browser Interface** (`bin/ant-browse`)
   - Port: 8081
   - Purpose: HTML-based dataset browser
   - Tech: FastAPI + Jinja2 templates + Pico CSS
   - Docs: `WEB_INTERFACE.md`

3. **Streamlit Dev Server** (`webapp-streamlit/`)
   - Port: 8501 (via `bin/dev-serve`)
   - Purpose: Development environment for Streamlit apps
   - Tech: Traditional server-based Streamlit
   - Docs: `WEBAPP-WORKFLOW.md`

### Web Applications

4. **Built Webapp** (`webapp/`)
   - Served via Caddy/nginx on port 8501
   - Contains:
     - JupyterLite (browser-based Jupyter)
     - Stlite (browser-based Streamlit via Pyodide)
   - Built from: `webapp-src/`
   - Docs: `WEBAPP-WORKFLOW.md`

### Libraries

5. **antclient** (`antclient/`)
   - Python library for API access
   - Auto-detects API URL (localhost:8080 or /api)
   - Used by all web interfaces
   - Docs: `ANTCLIENT_*.md`

## Port Map

| Port | Service | Purpose |
|------|---------|---------|
| 8080 | REST API | Data access |
| 8081 | Browser Interface | Dataset browsing |
| 8501 | Streamlit Dev OR Production Webapp | Development or production |

## Development Workflow

### For Streamlit Development

```bash
# Terminal 1: Start API
bin/ant serve

# Terminal 2: Start Streamlit dev server
bin/dev-serve
```

Access: http://localhost:8501

See: `WEBAPP-WORKFLOW.md` for details

### For Production Deployment

```bash
# Build webapp
make build-webapp

# Terminal 1: API
make serve-api

# Terminal 2: Browser interface
make serve-browse

# Terminal 3: Webapp (Caddy)
make serve
```

Access: http://localhost:8501

## Directory Structure

```
.
├── antclient/           # Python API client library
├── bin/                 # Executable scripts
├── config/              # Caddy/nginx configs
├── data/                # Data storage (h5adstore symlink)
├── doc/                 # All documentation
├── webapp/              # Built webapp (gitignored)
├── webapp-browser/      # Browser interface source
├── webapp-src/          # Source for built webapp
│   ├── jupyter/         # JupyterLite notebooks
│   └── stlite/          # Stlite source
└── webapp-streamlit/    # Streamlit dev environment
```

## Key Documentation

- **WEBAPP-WORKFLOW.md** - Development workflow for web apps
- **REST_API_SPEC.md** - API endpoints and schemas
- **DATABASE_SCHEMA.md** - DuckDB schema
- **DEVELOPMENT.md** - General development guide
- **METADATA.md** - Metadata handling
- **CONVERT_PROCESS.md** - h5ad to DuckDB conversion

## Recent Changes

See `attic/README.md` for information about archived old configurations and scripts.
