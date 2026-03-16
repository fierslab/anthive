# Web App Development Workflow

## Two Deployment Targets

### 1. Server-Based Streamlit (webapp-streamlit/)
**For: Development and testing**

- Traditional Python Streamlit server
- Fast reload, easy debugging
- Full Python environment
- Requires: Python server running

**Start:**
```bash
# Terminal 1: API
bin/ant serve

# Terminal 2: Streamlit
bin/dev-serve
```

**Access:** http://localhost:8501

### 2. Browser-Based stlite (webapp-src/stlite/)
**For: Production deployment**

- Streamlit runs in browser via Pyodide/WebAssembly
- No Python server needed!
- Static HTML/JS files
- Can be hosted on any web server (nginx, Apache, S3, etc.)

**Served by:** The FastAPI browser endpoint at `/browser`

## Development Workflow

```
┌─────────────────────┐
│  1. Develop         │
│  webapp-streamlit/  │  ← Work here (fast, easy debug)
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│  2. Test            │
│  localhost:8501     │  ← Test server version
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│  3. Sync            │
│  bin/sync-to-stlite │  ← Copy changes
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│  4. Deploy          │
│  webapp-src/stlite/ │  ← Browser version ready
└─────────────────────┘
```

## Step-by-Step

### Development (Daily Work)

1. **Edit** files in `webapp-streamlit/`:
   - `components.py`
   - `pages/*.py`
   - `streamlit_app.py`
   - `util.py`

2. **Test** at http://localhost:8501
   - Auto-reload on save
   - See changes immediately

3. **Iterate** until working

### Syncing to stlite (When Ready)

When your changes are tested and working:

```bash
bin/sync-to-stlite
```

This will:
- Show which files changed
- Ask for confirmation
- Create a backup
- Copy files to `webapp-src/stlite/`

### Testing stlite Version

After syncing, test the browser version:

1. Make sure API is running: `bin/ant serve`
2. Open: http://localhost:8080/browser
3. Verify everything works in the browser

## Key Differences

### Server Streamlit (webapp-streamlit/)
- ✅ Fast reload
- ✅ Full Python debugging
- ✅ All Python libraries available
- ❌ Requires Python server
- ❌ Not suitable for static hosting

### Browser stlite (webapp-src/stlite/)
- ✅ No server needed
- ✅ Can host anywhere (S3, GitHub Pages, etc.)
- ✅ Lower hosting costs
- ❌ Slower initial load (downloads Pyodide)
- ❌ Limited Python packages
- ❌ Harder to debug

## Important Notes

### Don't Edit stlite Directly

❌ **Don't develop in `webapp-src/stlite/`**
- Slower iteration
- Harder to debug
- Browser console for errors

✅ **Develop in `webapp-streamlit/`**
- Fast reload
- Python debugger
- Better error messages

### API URL Differences

**Server Streamlit:**
- Uses `ANTHIVE_API_URL` environment variable
- Points to `http://localhost:8080`
- Direct HTTP requests

**Browser stlite:**
- Uses browser's current origin
- Points to `/api` (same domain)
- Goes through browser CORS

### Keeping in Sync

The two versions will naturally diverge during development. That's OK!

**When to sync:**
- ✅ After completing a feature
- ✅ Before committing to git
- ✅ When preparing a release
- ❌ Not after every small change

## File Structure

```
webapp-streamlit/              # Development version
├── streamlit_app.py          # Main entry
├── components.py             # UI components
├── util.py                   # Utilities
├── pages/
│   ├── 1_gene.py            # Gene page
│   ├── 2_umap.py            # UMAP page
│   └── 3_rolling.py         # Rolling page
├── antclient/               # Symlink to ../antclient
└── .streamlit/
    └── config.toml          # Server config

webapp-src/stlite/            # Production version
├── index.html               # Browser loader
├── streamlit_app.py         # (synced from above)
├── components.py            # (synced from above)
├── util.py                  # (synced from above)
├── antclient.py             # Standalone file
└── pages/
    ├── 1_gene.py           # (synced from above)
    ├── 2_umap.py           # (synced from above)
    └── 3_rolling.py        # (synced from above)
```

## Git Workflow

```bash
# After syncing
git status

# Should show changes in both:
# - webapp-streamlit/
# - webapp-src/stlite/

# Commit both together
git add webapp-streamlit/ webapp-src/stlite/
git commit -m "Update UMAP visualization"
```

## Troubleshooting

### Changes not showing in stlite?

1. Did you run `bin/sync-to-stlite`?
2. Hard refresh browser: Ctrl+Shift+R
3. Check browser console for errors

### stlite loading forever?

- Check browser console
- Pyodide download might be slow
- Check if API is accessible at `/api`

### Different behavior in stlite vs server?

- Check if you're using a package not available in Pyodide
- Check API URL configuration
- Look for browser-specific issues (CORS, localStorage, etc.)

## Summary

✅ **Develop** in `webapp-streamlit/` (fast, easy)
✅ **Sync** with `bin/sync-to-stlite` (when ready)
✅ **Deploy** from `webapp-src/stlite/` (no server needed)
✅ **Commit** both together (keep in sync)
