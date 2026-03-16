# Anthive Development Guide

Quick reference for running the Anthive development environment.

## Quick Start

**Step 1**: Start the REST API (in a separate terminal):
```bash
bin/ant serve
```

**Step 2**: Start Streamlit + Caddy:
```bash
bin/dev-serve
```

Then open: **http://localhost:8000**

## Architecture Overview

```
┌─────────────────────────────────────────┐
│         Browser (localhost:8000)        │
└────────────────┬────────────────────────┘
                 │
                 ▼
┌────────────────────────────────────────┐
│              Caddy                     │  Reverse Proxy
│                                        │
│  Routes:                               │
│  • /          → Streamlit (:8501)     │
│  • /api/*     → REST API  (:8080)     │
└────────────────────────────────────────┘
                 │
        ┌────────┴────────┐
        ▼                 ▼
┌──────────────┐  ┌──────────────┐
│  Streamlit   │  │   REST API   │
│   (:8501)    │  │   (:8080)    │
└──────────────┘  └──────────────┘
```

## Services

### Combined Development Server

**Command**: `bin/dev-serve`

**Prerequisites**: REST API must already be running on port 8080

Starts:
- Streamlit (port 8501)
- Caddy reverse proxy (port 8000)

**Access**: http://localhost:8000

**Features**:
- Uses your existing API server
- Automatic health checks
- Clean shutdown with Ctrl+C
- Logs to `logs/` directory
- No CORS issues

**Why separate API**: This allows you to restart/modify the API independently without restarting Streamlit

**Logs**:
```bash
# Watch logs in real-time
tail -f logs/api.log        # REST API
tail -f logs/streamlit.log  # Streamlit
tail -f logs/caddy.log      # Caddy
```

### Individual Services

#### REST API Only
```bash
bin/ant serve
```
Access: http://localhost:8080

#### Streamlit Only
```bash
bin/streamlit-serve
```
Access: http://localhost:8501

Note: Set API URL when running standalone:
```bash
export ANTHIVE_API_URL=http://localhost:8080
bin/streamlit-serve
```

## Common Development Tasks

### Make Changes to Streamlit App

1. Edit files in `webapp-streamlit/`
2. Streamlit auto-reloads on save
3. Refresh browser to see changes

### Make Changes to REST API

1. Edit files in `bin/ant-serve` or `lib/`
2. In the API terminal: Ctrl+C to stop
3. Restart: `bin/ant serve`
4. Streamlit continues running (no need to restart)

### Make Changes to antclient

1. Edit `antclient/antclient.py`
2. Restart Streamlit (or use Streamlit's "Rerun")
3. webapp-streamlit has a symlink to antclient

### Update Streamlit App from stlite Source

Copy changes from source:
```bash
cp webapp-src/stlite/*.py webapp-streamlit/
cp webapp-src/stlite/pages/*.py webapp-streamlit/pages/
```

### Clear Streamlit Cache

In browser: Click hamburger menu → "Clear cache"

Or programmatically: Add `st.cache_data.clear()` in code

### Debug Mode

Edit `webapp-streamlit/.streamlit/config.toml`:
```toml
[server]
runOnSave = true  # Auto-rerun on save

[logger]
level = "debug"   # Verbose logging
```

## URLs

| Service      | Direct Access       | Via Caddy            |
|--------------|---------------------|----------------------|
| Streamlit    | :8501               | :8000/               |
| REST API     | :8080               | :8000/api            |
| API Health   | :8080/health        | :8000/api/health     |
| API Docs     | :8080/docs          | :8000/api/docs       |

## Configuration Files

- `Caddyfile.dev` - Caddy reverse proxy config
- `webapp-streamlit/.streamlit/config.toml` - Streamlit settings
- `bin/dev-serve` - Combined startup script
- `bin/streamlit-serve` - Streamlit launcher
- `bin/ant-serve` - REST API server

## Troubleshooting

### Port Already in Use

Check what's using the port:
```bash
lsof -i :8000  # Caddy
lsof -i :8080  # API
lsof -i :8501  # Streamlit
```

Kill process:
```bash
kill -9 <PID>
```

### Streamlit Shows Old Code

1. Clear cache: Menu → "Clear cache"
2. Hard refresh: Cmd+Shift+R (Mac) or Ctrl+Shift+R (Linux)
3. Restart Streamlit

### API Connection Errors

Check API is running:
```bash
curl http://localhost:8080/health
```

Check Caddy routing:
```bash
curl http://localhost:8000/api/health
```

### Caddy Not Found

Install Caddy:
```bash
# macOS
brew install caddy

# Debian/Ubuntu
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install caddy
```

### Changes Not Showing

1. Check file was saved
2. Check correct file was edited (not stlite source)
3. Check Streamlit reloaded (watch terminal)
4. Try manual rerun (R key in Streamlit)
5. Restart dev-serve

## Performance Tips

1. **Cache Data**: Use `@st.cache_data` for expensive operations
2. **Session State**: Use `st.session_state` for user data
3. **Lazy Loading**: Load data only when needed
4. **Database Queries**: Minimize API calls, use limits

## Testing

### Test API
```bash
# Health check
curl http://localhost:8000/api/health

# List databases
curl http://localhost:8000/api/databases | jq

# Get database info
curl http://localhost:8000/api/databases/Sierksma2025/dataset/info | jq
```

### Test Streamlit
1. Open http://localhost:8000
2. Select database
3. Navigate through pages
4. Test all visualizations

## Production Deployment

For production, use:
- Gunicorn/Uvicorn for REST API
- Streamlit Cloud or Docker for Streamlit
- Nginx or Caddy for reverse proxy
- systemd for process management

See `DEPLOYMENT.md` (to be created) for details.

## License

BSD-3-Clause
