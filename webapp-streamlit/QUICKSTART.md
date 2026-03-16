# Streamlit Development - Quick Start

## Simple Two-Step Startup

### Step 1: Start REST API

In terminal 1:
```bash
bin/ant serve
```

Wait for:
```
Starting Anthive REST API Server
  Databases: 48
  URL: http://localhost:8080
```

### Step 2: Start Streamlit

In terminal 2:
```bash
bin/dev-serve
```

This will:
- Check that API is running on :8080
- Set `ANTHIVE_API_URL=http://localhost:8080`
- Start Streamlit on :8501

## Access the App

Open: **http://localhost:8501**

The Streamlit app talks directly to the API at `http://localhost:8080`

## Making Changes

### Edit Streamlit Code

1. Edit files in `webapp-streamlit/`
2. Save the file
3. Streamlit auto-reloads
4. Refresh browser

### Edit API Code

1. In terminal 1: Press Ctrl+C to stop API
2. Make your edits
3. Restart: `bin/ant serve`
4. Streamlit stays running (will reconnect automatically)

## Stop Services

**Terminal 1 (API)**: Ctrl+C
**Terminal 2 (Streamlit)**: Ctrl+C

## Troubleshooting

### "REST API not running"

Make sure terminal 1 is running:
```bash
bin/ant serve
```

Check it's responding:
```bash
curl http://localhost:8080/health
```

### Port Already in Use

```bash
lsof -i :8080  # API
lsof -i :8501  # Streamlit
```

Kill if needed:
```bash
pkill -f ant-serve
pkill -f streamlit
```

### Connection Errors in Streamlit

Streamlit should automatically use `http://localhost:8080` for the API.

If you see errors, check the environment variable is set:
```bash
# In terminal 2, before running dev-serve:
export ANTHIVE_API_URL="http://localhost:8080"
bin/dev-serve
```

### No Databases Found

Check databases are accessible:
```bash
curl http://localhost:8080/databases | jq '.total'
# Should show: 48 (or similar)
```

## Files Structure

```
webapp-streamlit/
├── streamlit_app.py       # Main entry point
├── pages/
│   ├── 1_gene.py         # Gene expression page
│   ├── 2_umap.py         # UMAP visualization
│   └── 3_rolling.py      # Rolling average plot
├── components.py          # Reusable UI components
└── util.py               # Utility functions
```

All pages use:
```python
client = antclient.AnthiveClient()  # Auto-detects ANTHIVE_API_URL
```

## Advanced Usage

### Direct Streamlit (without dev-serve)

If you prefer to run Streamlit directly:
```bash
export ANTHIVE_API_URL="http://localhost:8080"
bin/streamlit-serve
```

### Custom API URL

```bash
export ANTHIVE_API_URL="http://my-server:8080"
bin/dev-serve
```

## Quick Commands

```bash
# Start everything
bin/ant serve          # Terminal 1
bin/dev-serve          # Terminal 2

# Check status
curl http://localhost:8080/health
curl http://localhost:8501/_stcore/health

# View logs
tail -f logs/streamlit.log

# Stop everything
# Ctrl+C in both terminals
```

Happy coding! 🧬
