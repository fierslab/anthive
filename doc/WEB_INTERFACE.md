# Web Interface for Anthive4

Simple web interface that provides a dynamic way into the provided data.

## Architecture

App runs off the REST API and is reverse proxied as '/' under the caddy & nginx.

**Comments:**
- ✓ Good: Reuses existing REST API (`/api/*`)
- ✓ Proxying at root means webapp is the "front door" to Anthive
- **Question**: Should JupyterLite move from `/jupyter` to `/notebooks`? More intuitive naming.
- **Consider**: Caching strategy for folder/file listings (filesystem scans can be slow with many files)

## Navigation Structure

App is minimal and simple, but does provide a lot of information.

### 1. Folder View (Home/Index)
- Overview of all studies organized per top-level folder
- Shows: folder name, number of datasets, total cells, maybe last updated

**Comments:**
- ✓ Good hierarchical entry point
- **Suggest**: Add sorting options (name, size, date, cell count)
- **Suggest**: Quick stats summary at top (total datasets, total cells across all studies)
- **Question**: Show folder description from `index.md` if it exists?

### 2. Folder Detail View
- Per selected folder - dataset in there
- If there are subfolders - group all datasets together
- Shows: list of h5ad files with key metrics

**Comments:**
- ✓ Grouping by subfolder makes sense for organization
- **Suggest**: Breadcrumb navigation (Home > Folder > Subfolder)
- **Suggest**: Show dataset table with columns:
  - Dataset name
  - Cells × Genes
  - Layers available
  - Status (✓ has DuckDB, ⚠ needs conversion, ✗ broken)
  - Size (h5ad / DuckDB)
  - Last checked date
- **Question**: Allow filtering (e.g., only show datasets with >10k cells, or with specific layers)?

### 3. Dataset Detail View
- Per selected database - stats on the datasets, obs-columns, etc
- The markdown rendered (from .md file)

**Comments:**
- ✓ Good: Reuse existing .md metadata
- **Suggest structure**:
  - Header: Dataset name, quick stats (cells, genes, size)
  - Tab/section navigation:
    1. **Overview** - rendered markdown (study description, layer stats table)
    2. **Observations** - list obs columns (numerical, categorical) with cardinality
    3. **Variables** - var columns if any
    4. **Embeddings** - available embeddings (UMAP, PCA, etc.) with dimensions
    5. **Layers** - available expression layers with sparsity info
    6. **Actions** - Download h5ad, Open in JupyterLite, API endpoints
  - Show file checksums, last updated, schema version
- **Question**: Show warnings from check report prominently?
- **Question**: Preview capability (first 10 rows of obs data)?

## Design Aesthetic

App has a minimal layout - information dense - reminds me of old-day bbs' - but do not make it too retro looking. DO make it simple looking.

**Comments:**
- ✓ Information-dense is good for power users
- **Interpretation**:
  - Think terminal/command-line aesthetic but in a browser
  - Monospace fonts for data tables
  - Clear hierarchy with whitespace
  - High contrast, readable
  - No unnecessary decorations or animations
  - Fast loading, minimal JavaScript
- **Inspiration**:
  - GitHub file browser (clean, functional)
  - Old man pages / info pages (structured, dense)
  - Modern terminal UIs (htop, lazygit) - clear sections, good use of color for status
- **Colors**:
  - Neutral background (off-white or very light gray, OR dark mode with near-black)
  - Accent colors only for status (green=OK, yellow=warning, red=error)
  - Monochrome for text and structure

## Framework Recommendation

**Option 1: FastAPI + Jinja2 + htmx** (Recommended)
- Extend existing `ant-serve` with HTML endpoints
- Server-side rendering with Jinja2 templates
- htmx for dynamic content without full page reloads
- Minimal JavaScript, great performance
- Single codebase, single server process
- **Pros**: Reuses existing FastAPI, minimal dependencies, server-rendered
- **Cons**: Mixes API and web rendering (but cleanly separable)

**Option 2: Flask + Jinja2** (Alternative)
- Separate lightweight server just for web interface
- Consumes REST API as external service
- Very simple, focused
- **Pros**: Clean separation of concerns, Flask is minimal
- **Cons**: Extra server process to manage, duplicate API calls

**Option 3: Static site generator (e.g., mkdocs-material)**
- Pre-generate HTML from markdown files
- Purely static, extremely fast
- **Pros**: No runtime overhead, deployable anywhere
- **Cons**: Not dynamic (needs rebuild when data changes), can't show real-time DB status

**My recommendation: Option 1 (FastAPI + Jinja2 + htmx)**
- Natural extension of existing codebase
- Server-side rendering = fast, accessible, works without JS
- htmx adds interactivity without SPA complexity
- Can progressively enhance (works without JS, better with JS)
- Fits "simple, information-dense" aesthetic perfectly

## Implementation Notes

**Tech Stack (if using Option 1):**
- FastAPI (already have it)
- Jinja2 templates
- htmx (CDN, ~14KB)
- Plain CSS or Pico CSS (~10KB, classless, clean minimal styling)
- No build step needed

**File Structure:**
```
webapp-browser/
  templates/
    base.html
    index.html (folder list)
    folder.html (folder detail)
    dataset.html (dataset detail)
  static/
    style.css
    htmx.min.js (optional, can use CDN)
  routes.py (or add to ant-serve)
```

**Key Features:**
- Breadcrumb navigation
- Responsive tables (scroll on mobile)
- Status indicators (✓ ⚠ ✗)
- Search/filter (client-side for simplicity)
- Dark mode toggle (user preference)

## Questions to Resolve

1. Should this replace JupyterLite at `/` or coexist?
   - Suggestion: Browser at `/`, JupyterLite at `/notebooks`, API at `/api`

2. Authentication/access control needed?
   - Suggestion: Start without, add later if needed

3. Should it support file uploads (for users to add their own h5ad files)?
   - Suggestion: Read-only initially, keep it simple

4. Preview/visualization of embeddings?
   - Suggestion: Maybe later - focus on metadata browsing first

---

## Confirmed Decisions ✓

**Framework:** Option 1 - FastAPI + Jinja2 + htmx
- Extends existing `ant-serve` FastAPI server
- Server-side rendering with Jinja2
- htmx for dynamic updates
- Light mode aesthetic (not dark)

**Routing:**
- `/` - Web browser interface (NEW)
- `/jupyter` - JupyterLite (existing)
- `/stlite` - Streamlit Lite (existing)
- `/api/*` - REST API (existing)

**Features:**
- ✓ Read-only (no file uploads)
- ✓ Authentication via nginx basic auth (not app-level)
- ✓ Light mode UI
- ✗ Embedded visualizations (deferred to later)

**Tech Stack:**
- FastAPI (existing)
- Jinja2 templates
- htmx (~14KB from CDN)
- Pico CSS or plain CSS for minimal styling
- No build step required

---

**Status**: ✓ Implemented

## Implementation Summary

**Architecture:** Dual-port (Option A)
- Browser server: `bin/ant-browse` on port 8081
- API server: `bin/ant-serve` on port 8080
- Reverse proxy: Caddy/nginx on port 8501

**Components:**
- `webapp-browser/` - Browser application code
  - `templates/` - Jinja2 HTML templates with Pico CSS
  - `browser.py` - FastAPI routes with URL encoding
- `bin/ant-browse` - Browser server executable
- Updated configs in `config/`

**Usage:**
```bash
make serve-api      # Terminal 1: API (port 8080)
make serve-browse   # Terminal 2: Browser (port 8081)
make serve          # Terminal 3: Caddy (port 8501)
```

Open: http://localhost:8501

See `webapp-browser/README.md` for details.

