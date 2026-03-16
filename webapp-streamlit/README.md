# Anthive Streamlit Application

Interactive web application for exploring single-cell expression databases.

## Features

- **Gene Expression**: Plot gene expression distributions (ECDF, KDE, histogram)
- **UMAP Visualization**: Interactive UMAP embeddings colored by genes, numerical or categorical metadata
- **Rolling Average**: Visualize rolling averages along sorted dimensions

## Quick Start

**Terminal 1** - Start API:
```bash
bin/ant serve
```

**Terminal 2** - Start Streamlit:
```bash
bin/dev-serve
```

**Access**: http://localhost:8501

## Architecture

```
┌─────────────┐
│   Browser   │  http://localhost:8501
└──────┬──────┘
       │
       ▼
┌──────────────┐
│  Streamlit   │  :8501
│   (Python)   │
└──────┬───────┘
       │
       │ ANTHIVE_API_URL
       │ http://localhost:8080
       │
       ▼
┌──────────────┐
│   REST API   │  :8080
└──────────────┘
```

No reverse proxy needed - Streamlit talks directly to the API.

## Development Workflow

1. **Start API**: `bin/ant serve` (terminal 1)
2. **Start Streamlit**: `bin/dev-serve` (terminal 2)
3. **Edit code**: Files in `webapp-streamlit/`
4. **Auto-reload**: Streamlit reloads on save
5. **Refresh browser**: See your changes

## Key Components

### Pages

- `pages/1_gene.py` - Gene expression distributions
- `pages/2_umap.py` - UMAP embedding visualizations
- `pages/3_rolling.py` - Rolling average plots

### Shared Code

- `components.py` - Reusable UI components
  - `database_selector()` - Database/group/layer picker
  - `gene_selector()` - Gene autocomplete
  - `categorical_selector()` - Categorical metadata picker
  - `numerical_selector()` - Numerical metadata picker
  - `showplot()` - Plot wrapper with download options

- `util.py` - Utilities for persistent URL parameters

### API Client

- `antclient/` (symlink to `../antclient`)
  - Auto-detects API URL from `ANTHIVE_API_URL` environment variable
  - Falls back to `http://localhost:8080` if not set

## Configuration

### API URL

Set via environment variable:
```bash
export ANTHIVE_API_URL="http://localhost:8080"
```

The `bin/dev-serve` script sets this automatically.

### Streamlit Config

Edit `.streamlit/config.toml` to customize:
- Theme colors
- Server port (default: 8501)
- Browser behavior

## Data Flow

1. User selects database/gene in UI
2. Streamlit calls `client.get_databases()`
3. antclient makes request to `http://localhost:8080/databases`
4. API returns data
5. Streamlit displays results

## Caching

Streamlit uses `@st.cache_data` for:
- Database list (TTL: 60s)
- Database info (TTL: 60s)
- Gene lists (TTL: 3600s)
- Expression data (automatically cached by Streamlit)

Clear cache: Streamlit menu → "Clear cache"

## Tips

### Hot Reload

Streamlit watches for file changes and prompts to rerun.

### Debug Mode

In `.streamlit/config.toml`:
```toml
[server]
runOnSave = true  # Auto-rerun on save

[logger]
level = "debug"   # Verbose logging
```

### Performance

- Use `@st.cache_data` for expensive operations
- Limit data fetching with `limit` parameter
- Use filters to reduce data transfer

## Troubleshooting

### API Connection Errors

Check API is running:
```bash
curl http://localhost:8080/health
```

Check environment variable:
```bash
echo $ANTHIVE_API_URL
# Should show: http://localhost:8080
```

### Streamlit Not Reloading

1. Check file was saved
2. Look for "Source file changed" prompt
3. Click "Rerun" or press R
4. Try "Clear cache" from menu

### Port Conflicts

```bash
lsof -i :8501  # Check what's using the port
pkill -f streamlit  # Kill existing Streamlit
```

## Testing

### Manual Testing

1. Start services: `bin/ant serve` + `bin/dev-serve`
2. Open http://localhost:8501
3. Test each page:
   - Select database
   - Select gene/embedding/metadata
   - Generate plots
   - Download data

### API Testing

```bash
# List databases
curl http://localhost:8080/databases | jq

# Get database info
curl http://localhost:8080/databases/h5adstore/Sierksma2025/.../info | jq

# Search genes
curl http://localhost:8080/databases/h5adstore/.../genes?q=CD4 | jq
```

## Dependencies

Installed via `uv` (see `bin/streamlit-serve`):
- streamlit
- pandas
- matplotlib
- seaborn
- scipy
- requests

## License

BSD-3-Clause
