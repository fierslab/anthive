.PHONY: help test build-reports build-webapp build-jupyter clean-webapp serve serve-nginx serve-api serve-browse serve-streamlit serve-mcp serve-all serve-debug serve-stop caddy-passwd caddy-passwd-clear dev-jupyter sync-push sync-pull sync-pull-all sync-status build-stale sync-update serve-status serve-restart

help:
	@echo "Anthive4 Development Commands"
	@echo "=============================="
	@echo ""
	@echo "Webapp:"
	@echo "  make build-webapp   - Build complete webapp (JupyterLite + index)"
	@echo "  make build-jupyter  - Build only JupyterLite to webapp/jupyter"
	@echo "  make clean-webapp   - Clean webapp build directory"
	@echo "  make serve          - Serve webapp with Caddy (port 8501, API proxied)"
	@echo "  make serve-nginx    - Serve webapp with Nginx (port 8501, API proxied)"
	@echo ""
	@echo "All-in-one:"
	@echo "  make serve-all     - Start all services (deployment, nginx already running)"
	@echo "  make serve-debug   - Start all services + Caddy on port 8501"
	@echo "  make serve-status  - Show supervisor state + child PIDs"
	@echo "  make serve-stop    - Stop the running supervisor + all children"
	@echo "  make serve-restart SVC=browse        - Restart a single service"
	@echo "  make serve-restart SVC='api browse'  - Restart several at once"
	@echo "  make serve-restart SVC=all           - Restart every child"
	@echo "  make caddy-passwd       - Add basic-auth to Caddy on :8501 (interactive)"
	@echo "  make caddy-passwd-clear - Remove the auth, reopening :8501 to all"
	@echo ""
	@echo "Development:"
	@echo "  make serve-api         - Run REST API server (port 8080)"
	@echo "  make serve-browse      - Run web browser server (port 8081)"
	@echo "  make serve-streamlit   - Run Streamlit server (port 8082)"
	@echo "  make serve-mcp         - Run MCP server for LLM integration (port 8090)"
	@echo "  make dev-jupyter       - Run JupyterLab on frontend/jupyter/jupyter (port 9090)"
	@echo ""
	@echo "Sync (S3) — the maintenance workflow:"
	@echo "  make sync-update     - Full cycle: pull h5ad → rebuild stale duckdb → push"
	@echo "  make sync-pull       - Pull .h5ad ONLY from S3 (duckdb is rebuilt locally)"
	@echo "  make build-stale     - Rebuild .duckdb for any h5ad newer than its duckdb"
	@echo "  make sync-push       - Push .h5ad and .duckdb to S3 (mtime metadata included)"
	@echo "  make sync-status     - Show local vs S3 sync status (size + mtime drift)"
	@echo "  make sync-pull-all   - Pull .h5ad AND .duckdb (rarely needed)"
	@echo ""
	@echo "Tools:"
	@echo "  ./bin/ant check        - Validate h5ad files / refresh .md"
	@echo "  ./bin/ant convert      - Convert h5ad to DuckDB"
	@echo "  ./bin/ant build-all    - Refresh .md + rebuild stale .duckdb across tree"
	@echo "  ./bin/ant serve        - Start REST API server"
	@echo "  ./bin/api/ant-mcp          - MCP server (LLM tool integration)"

# Run the test suite (lib/ + API tests)
test:
	PYTHONPATH=lib python -m pytest tests/ -v

# Pull static report sites from git repos (defined in anthive.yaml)
build-reports:
	@./bin/ops/ant-build-reports

clean-webapp:
	@echo "Cleaning webapp directory..."
	@rm -rf webapp
	@echo "✓ Clean complete!"

build-jupyter: antclient/antclient.py
	@echo "Building JupyterLite to webapp/jupyter..."
	@echo ""
	@# Ensure source dir is writable (may be read-only from a previous run)
	@chmod -R u+rw frontend/jupyter/jupyter
	@# Clear output directory so no stale files survive the build
	@rm -rf webapp/jupyter
	@mkdir -p webapp
	@# Copy antclient.py and anthelper.py into jupyter content before build
	@# echo "Copying antclient.py and anthelper.py to frontend/jupyter/jupyter..."
	@# cp antclient/antclient.py frontend/jupyter/jupyter/antclient.py
	@# cp antclient/anthelper.py frontend/jupyter/jupyter/anthelper.py
	@# Make source files read-only so JupyterLite cannot modify them
	@chmod -R a-w frontend/jupyter/jupyter
	@chmod -R a+rX frontend/jupyter/jupyter
	@DOIT_DB=tmp/.jupyterlite.doit.db \
	# uvx \
	# 	--from jupyterlite-core \
	# 	--with jupyterlite-pyodide-kernel \
	# 	--with jupyter-server \
	# 	--with jupyterlab-widgets \
	# 	jupyter-lite build \
	# 		--force \
	# 		--contents frontend/jupyter/jupyter \
	# 		--output-dir webapp/jupyter
	uvx --from jupyterlite-core \
		--with jupyterlite-pyodide-kernel \
		--with jupyterlab-widgets \
		--with notebook \
		--with jupyter-server \
		jupyter-lite build --force \
			--contents frontend/jupyter/jupyter \
			--output-dir webapp/jupyter 
	@# Restore read+write so files can be edited again
	@chmod -R u+rw frontend/jupyter/jupyter
	@echo "✓ JupyterLite build complete!"
	@echo ""
	@echo "Location: webapp/jupyter/"
	@echo "Client: webapp/jupyter/files/antclient.py"

build-webapp: build-jupyter frontend/jupyter/index.html
	@echo ""
	@echo "Copying main index.html..."
	@cp frontend/jupyter/index.html webapp/index.html
	@echo "✓ Index page copied"
	@echo ""
	@echo "╔═══════════════════════════════════════════════════════════╗"
	@echo "║  ✓ Webapp built successfully!                            ║"
	@echo "╚═══════════════════════════════════════════════════════════╝"
	@echo ""
	@echo "Contents:"
	@echo "  - webapp/index.html             (landing page)"
	@echo "  - webapp/jupyter/               (JupyterLite)"
	@echo ""
	@echo "To serve with Caddy (recommended):"
	@echo "  Terminal 1: make serve-api"
	@echo "  Terminal 2: make serve"
	@echo "  Then open: http://localhost:8501"
	@echo ""

serve:
	@echo "Starting Anthive webapp with Caddy on http://localhost:8501"
	@echo ""
	@echo "This serves:"
	@echo "  - Browser interface at /"
	@echo "  - JupyterLite at /jupyter"
	@echo "  - Streamlit at /streamlit"
	@echo "  - API at /api"
	@echo "  - MCP at /mcp"
	@echo ""
	@echo "Make sure backend servers are running (in other terminals):"
	@echo "  Terminal 1: make serve-api (port 8080)"
	@echo "  Terminal 2: make serve-browse (port 8081)"
	@echo "  Terminal 3: make serve-streamlit (port 8082)"
	@echo "  Terminal 4: make serve-mcp (port 8090)"
	@echo "  Terminal 5: make serve (this - port 8501)"
	@echo ""
	@caddy run --config config/Caddyfile

serve-nginx:
	@echo "Starting Anthive webapp with Nginx on http://localhost:8501"
	@echo ""
	@echo "This serves:"
	@echo "  - Browser interface at /"
	@echo "  - JupyterLite at /jupyter"
	@echo "  - Streamlit at /streamlit"
	@echo "  - API at /api"
	@echo "  - MCP at /mcp"
	@echo ""
	@echo "Make sure backend servers are running (in other terminals):"
	@echo "  Terminal 1: make serve-api (port 8080)"
	@echo "  Terminal 2: make serve-browse (port 8081)"
	@echo "  Terminal 3: make serve-streamlit (port 8082)"
	@echo "  Terminal 4: make serve-mcp (port 8090)"
	@echo "  Terminal 5: make serve-nginx (this - port 8501)"
	@echo ""
	@nginx -c $(PWD)/config/nginx.conf -p $(PWD)

serve-api:
	@echo "Starting Anthive REST API (host/port from anthive.yaml — default localhost:8080)"
	@echo ""
	@# Host & port come from anthive.yaml (rest_api_host / rest_api_port).
	@# To bind on all interfaces, set rest_api_host: 0.0.0.0 in anthive.yaml
	@# or pass --host 0.0.0.0 on the CLI.
	@./bin/api/ant-serve --data-folder ./data/h5adstore

serve-browse:
	@echo "Starting Anthive Web Browser (host/port from anthive.yaml — default localhost:8081)"
	@echo ""
	@# Same convention as serve-api: host/port from anthive.yaml
	@# (browse_host / browse_port).
	@./bin/web/ant-browse

serve-mcp:
	@echo "Starting Anthive MCP server on http://localhost:8090"
	@echo ""
	@echo "Connect from any MCP client with:"
	@echo "  URL:  http://localhost:8090/mcp"
	@echo "  Type: streamable-http"
	@echo ""
	@./bin/api/ant-mcp

serve-all:
	@echo "Starting Anthive (deployment mode — nginx serves port 8501)"
	@./bin/ops/ant-supervisor

serve-debug:
	@echo "Starting Anthive with Caddy on http://localhost:8501"
	@./bin/ops/ant-supervisor --caddy

# Restart one or more children of a running supervisor.
#   make serve-restart SVC=browse
#   make serve-restart SVC='api browse'
#   make serve-restart SVC=all       (everything)
SVC ?= all
serve-restart:
	@./bin/ops/ant-supervisor restart $(SVC)

serve-status:
	@./bin/ops/ant-supervisor status

# Drop a basic-auth credential into config/caddy-auth-basic.conf, gitignored.
# The Caddyfile imports `caddy-auth-*.conf` automatically — present = auth on.
# Username defaults to `admin` if you press enter.
# Caddy must be restarted afterwards (printed at the end).
caddy-passwd:
	@printf "Username [admin]: " > /dev/stderr; \
	read -r CADDY_USER; \
	CADDY_USER=$${CADDY_USER:-admin}; \
	echo "Generating bcrypt hash with caddy hash-password..." > /dev/stderr; \
	HASH=$$(caddy hash-password); \
	if [ -z "$$HASH" ]; then \
		echo "ERROR: caddy hash-password returned empty — aborting." > /dev/stderr; exit 1; \
	fi; \
	printf '%s\n' \
		'@protected {' \
		'	not path /mcp* /.well-known/* /register' \
		'}' \
		'' \
		"basic_auth @protected {" \
		"	$$CADDY_USER $$HASH" \
		'}' \
		> config/caddy-auth-basic.conf; \
	echo "" > /dev/stderr; \
	echo "✓ Wrote config/caddy-auth-basic.conf  (user='$$CADDY_USER')" > /dev/stderr; \
	echo "  Restart Caddy to activate: make serve-restart SVC=caddy" > /dev/stderr

# Remove the auth file so /:8501 is open again.
caddy-passwd-clear:
	@rm -f config/caddy-auth-basic.conf config/caddy-auth-*.conf
	@echo "✓ Removed config/caddy-auth-*.conf"
	@echo "  Restart Caddy: make serve-restart SVC=caddy"

# SIGTERM the running supervisor — it cascades to every child (api, browse,
# mcp, streamlit, caddy).  Re-run `make serve-debug` or `make serve-all`
# afterwards to bring everything back up.
serve-stop:
	@PID=$$(./bin/ops/ant-supervisor status 2>/dev/null | awk '/^supervisor pid:/ {print $$3; exit}'); \
	if [ -n "$$PID" ]; then \
		echo "stopping supervisor pid $$PID (cascades to all children)"; \
		kill $$PID; \
	else \
		echo "no supervisor running"; \
	fi

serve-streamlit:
	@echo "Starting Anthive Streamlit server on http://localhost:8082"
	@echo ""
	@cd frontend/streamlit && uvx \
		--with streamlit \
		--with matplotlib \
		--with seaborn \
		--with scipy \
		--with numpy \
		--with pandas \
		--with requests \
		streamlit run streamlit_app.py

dev-jupyter:
	@./bin/ops/dev-jupyter

# ── Sync workflow (S3 = canonical for .h5ad; .duckdb is rebuilt locally) ──

# Default pull: h5ad only. .duckdb is derived; rebuild it via `make build-stale`.
sync-pull:
	@./bin/ops/ant-sync pull

# Force pull duckdb too (disaster-recovery / mass-rebuild bootstrap).
sync-pull-all:
	@./bin/ops/ant-sync pull --include-duckdb

# Rebuild any .duckdb older than its .h5ad. Also refreshes the .md if the
# h5ad is newer (sha256, n_obs, layer_stats), via ant-h5ad-check.
build-stale:
	@./bin/ingest/ant-build-all --data-folder ./data/h5adstore

# Push h5ad + duckdb to S3. The mtime header is set on every upload so
# `make sync-status` can detect content-without-size-change drift later.
sync-push:
	@./bin/ops/ant-sync push

sync-status:
	@./bin/ops/ant-sync status

# Full maintenance cycle: bring everything into a consistent state.
sync-update:
	@echo "── 1/3  pull .h5ad from S3 ─────────────────────────────────"
	@$(MAKE) sync-pull
	@echo ""
	@echo "── 2/3  rebuild any stale .duckdb (and refresh stale .md) ──"
	@$(MAKE) build-stale
	@echo ""
	@echo "── 3/3  push .h5ad and rebuilt .duckdb back to S3 ──────────"
	@$(MAKE) sync-push

# ── Legacy / disabled sync targets (kept for reference, commented out) ────
# All of these have been superseded by ant-sync (boto3-based, mtime-aware,
# `_`-folder-aware). They reference rclone or rsync against hosts that may
# no longer be configured. Uncomment + adapt if you need them again.
#
# sync-to-cbd3:
# 	rsync -arv ./webapp cbd3:/data/anthive4/
# 	rsync -arv data/h5adstore/ cbd3:/data/anthive4/data/h5adstore/
#
# pull-h5ad-from-storagebox-to-foam:
# 	rclone copy bds_dedata:/ data/h5adstore/ \
# 	    --exclude "**/*.duckdb" --exclude "**/*.duckdb.wal" \
# 	    --transfers 2 --checkers 4 --timeout 500s --progress
#
# sync-to-bds-dedata:
# 	rclone sync data/h5adstore/ bds_dedata:/ \
# 	    --exclude "**/*.duckdb.wal" --exclude "**/*.partial" \
# 	    --transfers 2 --checkers 4 --timeout 500s --progress
#
# sync-webapp-only:
# 	rclone sync ./webapp bds_dedata:/anthive4/webapp \
# 	    --transfers 2 --checkers 4 --timeout 300s --progress
#
# pull-from-storagebox-to_prs:
# 	rclone copy bds_dedata:/ data/h5adstore/ \
# 	    --exclude "**/*.h5ad" --exclude "**/*.duckdb.wal" \
# 	    --transfers 2 --checkers 4 --timeout 500s --progress
