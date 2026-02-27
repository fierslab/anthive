.PHONY: help build-webapp build-jupyter clean-webapp serve serve-api

help:
	@echo "Anthive4 Development Commands"
	@echo "=============================="
	@echo ""
	@echo "Webapp:"
	@echo "  make build-webapp      - Build complete webapp (JupyterLite + index)"
	@echo "  make build-jupyter     - Build only JupyterLite to webapp/jupyter"
	@echo "  make clean-webapp      - Clean webapp build directory"
	@echo "  make serve             - Serve webapp with Caddy (port 8501, API proxied)"
	@echo "  make serve-nginx       - Serve webapp with Nginx (port 8501, API proxied)"
	@echo ""
	@echo "Development:"
	@echo "  make serve-api         - Run REST API server (port 8080)"
	@echo ""
	@echo "Sync:"
	@echo "  make sync-to-bds_dedata   - Push h5adstore to storage box (rclone)"
	@echo "  make sync-webapp-only     - Push webapp to storage box (rclone)"
	@echo "  make pull-from-storagebox - Pull h5adstore from storage box (rclone)"
	@echo "  make sync-to-cbd3         - Sync to cbd3 server (rsync)"
	@echo ""
	@echo "Tools:"
	@echo "  ./bin/ant check        - Validate h5ad files"
	@echo "  ./bin/ant convert      - Convert h5ad to DuckDB"
	@echo "  ./bin/ant build-all    - Process entire datastore with parallel conversion"
	@echo "  ./bin/ant serve        - Start REST API server"

clean-webapp:
	@echo "Cleaning webapp directory..."
	@rm -rf webapp
	@echo "✓ Clean complete!"

build-jupyter:
	@echo "Building JupyterLite to webapp/jupyter..."
	@echo ""
	@# Create webapp directory
	@mkdir -p webapp
	@# Copy antclient.py into notebooks before build
	@echo "Copying antclient.py to notebooks..."
	@cp antclient/antclient.py notebooks/antclient.py
	@# Build JupyterLite with notebooks as content using uvx
	@uvx --from jupyterlite-core --with jupyterlite-pyodide-kernel --with jupyter-server jupyter-lite build --contents notebooks --output-dir webapp/jupyter
	@echo "✓ JupyterLite build complete!"
	@echo ""
	@echo "Location: webapp/jupyter/"
	@echo "Client: webapp/jupyter/files/antclient.py"

build-webapp: build-jupyter
	@echo ""
	@echo "Copying main index.html..."
	@cp webapp-index.html webapp/index.html
	@echo "✓ Index page copied"
	@echo ""
	@echo "Creating stlite placeholder..."
	@mkdir -p webapp/stlite
	@echo "<html><body><h1>Streamlit Lite - Coming Soon</h1><p>This section will contain the Streamlit Lite app.</p></body></html>" > webapp/stlite/index.html
	@echo "✓ Stlite placeholder created"
	@echo ""
	@echo "╔═══════════════════════════════════════════════════════════╗"
	@echo "║  ✓ Webapp built successfully!                            ║"
	@echo "╚═══════════════════════════════════════════════════════════╝"
	@echo ""
	@echo "Contents:"
	@echo "  - webapp/index.html           (landing page)"
	@echo "  - webapp/jupyter/             (JupyterLite)"
	@echo "  - webapp/jupyter/files/antclient.py  (symlinked client)"
	@echo "  - webapp/stlite/              (placeholder for Streamlit)"
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
	@echo "  - Static webapp files from ./webapp"
	@echo "  - API proxied from http://localhost:8080 at /api/*"
	@echo ""
	@echo "Make sure the API server is running first (in another terminal):"
	@echo "  make serve-api"
	@echo ""
	@caddy run --config Caddyfile

serve-nginx:
	@echo "Starting Anthive webapp with Nginx on http://localhost:8501"
	@echo ""
	@echo "This serves:"
	@echo "  - Static webapp files from ./webapp"
	@echo "  - API proxied from http://localhost:8080 at /api/*"
	@echo ""
	@echo "Make sure the API server is running first (in another terminal):"
	@echo "  make serve-api"
	@echo ""
	@nginx -c $(PWD)/nginx.conf -p $(PWD)

serve-api:
	@echo "Starting Anthive REST API on http://localhost:8080"
	@echo ""
	@./bin/ant-serve --data-folder ./data/h5adstore --host 0.0.0.0 --port 8080

sync-to-cbd3:
	rsync -arv ./webapp cbd3:/data/anthive4/
	rsync -arv data/h5adstore/ cbd3:/data/anthive4/data/h5adstore/

sync-to-bds_dedata:
	@echo "Copying to storage box with rclone (resilient settings)..."
	@echo ""
	@echo "Copying h5adstore..."
	rclone sync data/h5adstore/ bds_dedata:/anthive4/h5adstore \
		--exclude "**/.*/**" \
		--exclude ".*/**" \
		--exclude "**/*.partial" \
		--exclude "**/*.duckdb.wal" \
		--transfers 2 \
		--checkers 4 \
		--timeout 500s \
		--contimeout 60s \
		--retries 10 \
		--low-level-retries 10 \
		--stats 30s \
		--progress
	@echo ""
	@echo "✓ Copy complete!"

sync-webapp-only:
	@echo "Copying only webapp to storage box..."
	rclone sync ./webapp bds_dedata:/anthive4/webapp \
		--exclude "**/.*/**" \
		--exclude ".*/**" \
		--exclude "**/*.partial" \
		--transfers 2 \
		--checkers 4 \
		--timeout 300s \
		--contimeout 60s \
		--retries 10 \
		--low-level-retries 10 \
		--stats 30s \
		--sftp-set-modtime=false \
		--sftp-use-fstat=false \
		--progress
	@echo "✓ Webapp copy complete!"

pull-from-storagebox:
	@echo "Pulling h5adstore from storage box (resilient settings)..."
	@echo ""
	@echo "Downloading h5adstore..."
	rclone copy bds_dedata:/anthive4/h5adstore data/h5adstore/ \
		--exclude "**/.*/**" \
		--exclude ".*/**" \
		--exclude "**/*.partial" \
		--exclude "**/*.duckdb.wal" \
		--exclude "**/*.duckdb.deleted" \
		--exclude "**/*.duckdb.wal.deleted" \
		--transfers 2 \
		--checkers 4 \
		--timeout 500s \
		--contimeout 60s \
		--retries 10 \
		--low-level-retries 10 \
		--stats 30s \
		--progress
	@echo ""
	@echo "✓ Pull complete!"
