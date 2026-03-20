.PHONY: help build-webapp build-jupyter clean-webapp serve serve-api serve-streamlit dev-jupyter

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
	@echo "Development:"
	@echo "  make serve-api         - Run REST API server (port 8080)"
	@echo "  make serve-browse      - Run web browser server (port 8081)"
	@echo "  make serve-streamlit   - Run Streamlit server (port 8082)"
	@echo "  make dev-jupyter       - Run JupyterLab on webapp-src/jupyter (port 9090)"
	@echo ""
	@echo "Sync:"
	@echo "  make sync-to-bds-dedata      - Push h5adstore to storage box (rclone)"
	@echo "  make sync-bds-dedata-to-foam - Push h5adstore to storage box (rclone)"
	@echo "  make sync-webapp-only        - Push webapp to storage box (rclone)"
	@echo "  make pull-from-storagebox-to-prs    - Pull h5adstore from storage box (rclone)"
	@echo "  make sync-to-cbd3            - Sync to cbd3 server (rsync)"
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

build-jupyter: antclient/antclient.py
	@echo "Building JupyterLite to webapp/jupyter..."
	@echo ""
	@# Ensure source dir is writable (may be read-only from a previous run)
	@chmod -R u+rw webapp-src/jupyter
	@# Clear output directory so no stale files survive the build
	@rm -rf webapp/jupyter
	@mkdir -p webapp
	@# Copy antclient.py and anthelper.py into jupyter content before build
	@# echo "Copying antclient.py and anthelper.py to webapp-src/jupyter..."
	@# cp antclient/antclient.py webapp-src/jupyter/antclient.py
	@# cp antclient/anthelper.py webapp-src/jupyter/anthelper.py
	@# Make source files read-only so JupyterLite cannot modify them
	@chmod -R a-w webapp-src/jupyter
	@chmod -R a+rX webapp-src/jupyter
	@DOIT_DB=tmp/.jupyterlite.doit.db \
	# uvx \
	# 	--from jupyterlite-core \
	# 	--with jupyterlite-pyodide-kernel \
	# 	--with jupyter-server \
	# 	--with jupyterlab-widgets \
	# 	jupyter-lite build \
	# 		--force \
	# 		--contents webapp-src/jupyter \
	# 		--output-dir webapp/jupyter
	uvx --from jupyterlite-core \
		--with jupyterlite-pyodide-kernel \
		--with jupyterlab-widgets \
		--with notebook \
		--with jupyter-server \
		jupyter-lite build --force \
			--contents webapp-src/jupyter \
			--output-dir webapp/jupyter 
	@# Restore read+write so files can be edited again
	@chmod -R u+rw webapp-src/jupyter
	@echo "✓ JupyterLite build complete!"
	@echo ""
	@echo "Location: webapp/jupyter/"
	@echo "Client: webapp/jupyter/files/antclient.py"

build-webapp: build-jupyter webapp-src/index.html
	@echo ""
	@echo "Copying main index.html..."
	@cp webapp-src/index.html webapp/index.html
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
	@echo ""
	@echo "Make sure backend servers are running (in other terminals):"
	@echo "  Terminal 1: make serve-api (port 8080)"
	@echo "  Terminal 2: make serve-browse (port 8081)"
	@echo "  Terminal 3: make serve-streamlit (port 8082)"
	@echo "  Terminal 4: make serve (this - port 8501)"
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
	@echo ""
	@echo "Make sure backend servers are running (in other terminals):"
	@echo "  Terminal 1: make serve-api (port 8080)"
	@echo "  Terminal 2: make serve-browse (port 8081)"
	@echo "  Terminal 3: make serve-streamlit (port 8082)"
	@echo "  Terminal 4: make serve-nginx (this - port 8501)"
	@echo ""
	@nginx -c $(PWD)/config/nginx.conf -p $(PWD)

serve-api:
	@echo "Starting Anthive REST API on http://localhost:8080"
	@echo ""
	@./bin/ant-serve --data-folder ./data/h5adstore --host 0.0.0.0 --port 8080

serve-browse:
	@echo "Starting Anthive Web Browser on http://localhost:8081"
	@echo ""
	@./bin/ant-browse --api-url http://localhost:8080 --host 0.0.0.0 --port 8081

serve-streamlit:
	@echo "Starting Anthive Streamlit server on http://localhost:8082"
	@echo ""
	@cd webapp-streamlit && uvx \
		--with streamlit \
		--with matplotlib \
		--with seaborn \
		--with scipy \
		--with numpy \
		--with pandas \
		--with requests \
		streamlit run streamlit_app.py

dev-jupyter:
	@./bin/dev-jupyter

sync-to-cbd3:
	rsync -arv ./webapp cbd3:/data/anthive4/
	rsync -arv data/h5adstore/ cbd3:/data/anthive4/data/h5adstore/


pull-h5ad-from-storagebox-to-foam:
	@echo "Pulling h5adstore from storage box (resilient settings)..."
	@echo ""
	@echo "Downloading h5adstore..."
	rclone copy bds_dedata:/ data/h5adstore/ \
		--exclude "**/.*/**" \
		--exclude ".*/**" \
		--exclude "**/*.partial" \
		--exclude "**/*.duckdb" \
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

sync-to-bds-dedata:
	@echo "Copying to storage box with rclone (resilient settings)..."
	@echo ""
	@echo "Copying h5adstore..."
	rclone sync data/h5adstore/ bds_dedata:/ \
		--exclude "**/.*/**" \
		--exclude ".*/**" \
		--exclude "**/*.partial" \
		--exclude "**/*.duckdb.wal" \
		--exclude "**/*.duckdb_xxx" \
		--exclude "**/*.h5ad_xxx" \
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

pull-from-storagebox-to_prs:
	@echo "Pulling h5adstore from storage box (resilient settings)..."
	@echo ""
	@echo "Downloading h5adstore..."
	rclone copy bds_dedata:/ data/h5adstore/ \
		--exclude "**/.*/**" \
		--exclude ".*/**" \
		--exclude "**/*.partial" \
		--exclude "**/*.h5ad" \
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
