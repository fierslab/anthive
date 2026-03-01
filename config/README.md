# Server Configuration Files

This directory contains configuration files for serving the Anthive webapp.

## Files

- **`Caddyfile`** - Caddy web server configuration
  - Serves static files from `../webapp/`
  - Reverse proxies `/api/*` to REST API on port 8080
  - Auto HTTPS (if exposed publicly)
  - Usage: `make serve` or `caddy run --config config/Caddyfile`

- **`nginx.conf`** - Nginx web server configuration
  - Alternative to Caddy
  - Same routing: static files + API proxy
  - Usage: `make serve-nginx` or `nginx -c $(PWD)/config/nginx.conf -p $(PWD)`

## Usage

**With Caddy (recommended):**
```bash
# Terminal 1: Start API server
make serve-api

# Terminal 2: Start Caddy
make serve

# Open: http://localhost:8501
```

**With Nginx (alternative):**
```bash
# Terminal 1: Start API server
make serve-api

# Terminal 2: Start Nginx
make serve-nginx

# Open: http://localhost:8501
```

## Log Files

Log files are written to the project root directory (not in config/):
- `caddy.log` - Caddy access/error logs
- `nginx_access.log` - Nginx access logs
- `nginx_error.log` - Nginx error logs
- `nginx.pid` - Nginx process ID

All log files are gitignored.
