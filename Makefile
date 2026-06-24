.PHONY: help test build-reports build-webapp build-jupyter clean-webapp serve serve-nginx serve-api serve-browse serve-mcp serve-all serve-debug serve-prod serve-stop caddy-passwd caddy-passwd-clear dev-jupyter sync-push sync-pull sync-pull-duckdb sync-pull-de sync-pull-all sync-status build-stale sync-update serve-status serve-restart h5adstore-push push-all ingest ingest-dry manifest

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
	@echo "  make serve-debug   - Start all services + Caddy on port 8501 (dev, TLS internal)"
	@echo "  make serve-prod    - Start all services + Caddy on 127.0.0.1:8500 (prod, nginx in front)"
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
	@echo "  make serve-mcp         - Run MCP server for LLM integration (port 8090, OPT-IN)"
	@echo "  make dev-jupyter       - Run JupyterLab on frontend/jupyter/jupyter (port 9090)"
	@echo ""
	@echo "Ingest (new deposits + local rebuilds):"
	@echo "  make ingest          - End-to-end: S3 __incoming/ → catalog (.h5ad + DE), then rebuild any local stale duckdb"
	@echo "  make ingest FILTER=Foo2025  - Restrict to one data project"
	@echo "  make ingest-dry      - Show what would happen without writing anything"
	@echo "  make manifest        - Rebuild + upload s3://anthive4a/_catalog/manifest.json (used by mus's sidecar validation)"
	@echo ""
	@echo "Sync (S3) — the maintenance workflow:"
	@echo "  make sync-update     - Full cycle: pull h5ad → rebuild stale duckdb → push"
	@echo "  make sync-pull         - Pull .h5ad + .de.parquet from S3 (duckdb rebuilt locally)"
	@echo "  make sync-pull-duckdb  - Pull ONLY .duckdb files from S3 (skip h5ad / de.parquet)"
	@echo "  make sync-pull-de      - Pull ONLY .de.parquet sidecars from S3"
	@echo "  make build-stale       - Rebuild .duckdb for any h5ad newer than its duckdb"
	@echo "  make sync-push         - Push .h5ad and .duckdb to S3 (mtime metadata included)"
	@echo "  make sync-status       - Show local vs S3 sync status (size + mtime drift)"
	@echo "  make sync-pull-all     - Pull .h5ad AND .duckdb AND .de.parquet (rarely needed)"
	@echo ""
	@echo "Tools:"
	@echo "  ./bin/ant check        - Validate h5ad files / refresh .md"
	@echo "  ./bin/ant convert      - Convert h5ad to DuckDB"
	@echo "  ./bin/ant build-all    - Refresh .md + rebuild stale .duckdb across tree"
	@echo "  ./bin/ant serve        - Start REST API server"
	@echo "  ./bin/api/ant-mcp          - MCP server (LLM tool integration)"

# Run the test suite (lib/ + API + web tests)
#
# Run via `uv run` with an explicit dependency set rather than a bare
# `python -m pytest`: there is no shared venv (every ant-* tool carries its
# own PEP-723 inline deps), so the test runner needs its own. uv resolves
# these from cache on repeat runs. PYTHONPATH=lib mirrors the sys.path shim
# the tools use at runtime.
TEST_DEPS = --with pytest --with duckdb --with pyyaml --with numpy \
            --with pandas --with h5py --with scipy \
            --with fastapi --with httpx --with jinja2 \
            --with markdown --with requests

test:
	uv run $(TEST_DEPS) env PYTHONPATH=lib python -m pytest tests/ -v

# Pull static report sites from git repos (defined in anthive.yaml)
build-reports:
	@./bin/ops/ant-build-reports

clean-webapp:
	@echo "Cleaning webapp directory..."
	@rm -rf webapp
	@echo "✓ Clean complete!"

# build-jupyter packages the JupyterLite notebooks.
# The `vark/` package is vendored under frontend/jupyter/jupyter/ — that's
# the source of truth for what the notebooks see (Pyodide treats it as a
# top-level package, no PyPI install needed). The canonical pyvark repo
# lives at codeberg.org/mfiers/pyvark — re-vendor by copying the new
# vark/*.py over the vendored copy when bumping the client version.
build-jupyter:
	@echo "Building JupyterLite to webapp/jupyter..."
	@echo ""
	@# Ensure source dir is writable (may be read-only from a previous run)
	@chmod -R u+rw frontend/jupyter/jupyter
	@# Clear output directory so no stale files survive the build
	@rm -rf webapp/jupyter
	@mkdir -p webapp
	@# Make source files read-only so JupyterLite cannot modify them
	@chmod -R a-w frontend/jupyter/jupyter
	@chmod -R a+rX frontend/jupyter/jupyter
	@# Version pins (DO NOT BUMP without re-checking the matrix below):
	@#   jupyterlite-core 0.7.6 ships JupyterLab 4.5.7 inside its app shell
	@#   (see webapp/jupyter/lab/package.json `resolutions` after a build).
	@#   `notebook` ships @jupyter-notebook/lab-extension as a federated
	@#   module that bakes in a JupyterLab patch-level requirement at
	@#   build time. The webpack-mf encoding `[1,4,5,N]` corresponds to
	@#   `~4.5.N` (= `>=4.5.N <4.6.0`):
	@#     notebook 7.5.6 → `~4.5.7`  ✓ matches host's 4.5.7
	@#     notebook 7.5.7 → `~4.5.8`  ✗ host 4.5.7 < 4.5.8
	@#     notebook 7.6.0 → `~4.6.0`  ✗ wrong minor
	@#   A mismatch makes the shared-module getter for
	@#   `@jupyterlab/docregistry` fail to initialise (#RUNTIME-012) and
	@#   `INotebookPathOpener` from the lab-extension throws before
	@#   registering, which silently breaks `?fromURL=` deep links — the
	@#   lab UI loads but never fetches the requested notebook.
	@#   So we pin notebook to exactly 7.5.6 against jupyterlite-core 0.7.6.
	@#   When bumping either, re-check the matrix above and adjust both.
	@# jupyterlab-open-url-parameter adds a robust ?fromURL=<url> handler
	@# that hooks into JupyterLab's standard `filebrowser:open-url` command,
	@# bypassing the brittle INotebookPathOpener path from
	@# @jupyter-notebook/lab-extension. Required for the "Open in JupyterLite"
	@# button on plot pages (ant-serve /databases/{id}/plot/{geom}/notebook.ipynb).
	@DOIT_DB=tmp/.jupyterlite.doit.db \
	uvx --from 'jupyterlite-core==0.7.6' \
		--with jupyterlite-pyodide-kernel \
		--with jupyterlab-widgets \
		--with 'notebook==7.5.6' \
		--with jupyter-server \
		--with jupyterlab-open-url-parameter \
		jupyter-lite build --force \
			--contents frontend/jupyter/jupyter \
			--output-dir webapp/jupyter
	@# Restore read+write so files can be edited again
	@chmod -R u+rw frontend/jupyter/jupyter
	@echo "✓ JupyterLite build complete!"
	@echo ""
	@echo "Location: webapp/jupyter/"
	@echo "Client:   webapp/jupyter/files/vark/  (vendored from codeberg.org/mfiers/pyvark)"

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
	@echo "  - API at /api"
	@echo ""
	@echo "Make sure backend servers are running (in other terminals):"
	@echo "  Terminal 1: make serve-api (port 8080)"
	@echo "  Terminal 2: make serve-browse (port 8081)"
	@echo "  Terminal 3: make serve (this - port 8501)"
	@echo ""
	@caddy run --config config/Caddyfile

serve-nginx:
	@echo "Starting Anthive thin-nginx shim on http://localhost:8501"
	@echo ""
	@echo "Post-refactor: nginx forwards EVERY path to Caddy on 127.0.0.1:8500."
	@echo "Caddy owns path routing — Caddyfile is the single source of truth."
	@echo ""
	@echo "Make sure Caddy is running in PROD mode (in another terminal):"
	@echo "  Terminal 1: make serve-prod      (or: ANTHIVE_PROD=1 ./bin/ops/ant-supervisor --caddy)"
	@echo "  Terminal 2: make serve-nginx     (this - port 8501)"
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
	@./web/ant-browse

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
	@echo "Starting Anthive with Caddy on https://localhost:8501 (dev, TLS internal)"
	@./bin/ops/ant-supervisor --caddy

# Production mode: Caddy listens on 127.0.0.1:8500 (plain HTTP), nginx in
# front of it terminates TLS + Authelia gate. See doc/DEPLOY.md.
serve-prod:
	@echo "Starting Anthive in PROD mode — Caddy on http://127.0.0.1:8500"
	@echo "(expects nginx in front; see doc/DEPLOY.md and config/nginx-intern-anthive.conf)"
	@./bin/ops/ant-supervisor --prod --caddy

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
# mcp, caddy).  Re-run `make serve-debug` or `make serve-all`
# afterwards to bring everything back up.
serve-stop:
	@PID=$$(./bin/ops/ant-supervisor status 2>/dev/null | awk '/^supervisor pid:/ {print $$3; exit}'); \
	if [ -n "$$PID" ]; then \
		echo "stopping supervisor pid $$PID (cascades to all children)"; \
		kill $$PID; \
	else \
		echo "no supervisor running"; \
	fi

# NOTE: the Streamlit dashboard was removed 2026-06-22 along with
# frontend/streamlit/. ant-browse owns the interactive plot UI now
# (per-dataset, autocompleting, URL-shareable). /streamlit on the
# reverse proxy continues to return 410 for anyone with a bookmark.

dev-jupyter:
	@./bin/ops/dev-jupyter

# ── Sync workflow (S3 = canonical for .h5ad; .duckdb is rebuilt locally) ──

# Default pull: h5ad + .de.parquet. .duckdb is derived; rebuild it via
# `make build-stale` (or pull it explicitly with sync-pull-duckdb / -all).
sync-pull:
	@./bin/ops/ant-sync pull

# Pull ONLY the .duckdb files from S3 — leave .h5ad / .de.parquet alone.
# Use when local .h5ad files are current but the .duckdb cache is stale
# (e.g. a peer rebuilt the duckdbs and pushed; you just want the rebuilt
# served-derivative without re-running `make build-stale` locally).
sync-pull-duckdb:
	@./bin/ops/ant-sync pull --only .duckdb

# Pull ONLY the .de.parquet sidecars — leave .h5ad / .duckdb alone.
# Use when a peer ingested a new DE study (or re-ingested an existing one)
# and you want the source-of-truth sidecar without re-pulling the
# multi-GB h5ad payload.
sync-pull-de:
	@./bin/ops/ant-sync pull --only .de.parquet

# Force pull h5ad + duckdb + de.parquet (disaster-recovery / mass-rebuild
# bootstrap).
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

# Commit + push all pending metadata in data/h5adstore/ to its own git
# repo (github.com:badslab/h5adstore). Auto-commit message uses an ISO
# timestamp; pre-commit validator (validate_md.py) still enforces the
# id / group_id requirements — a missing field on a new dataset stops
# the auto-commit, which is the right behaviour (human needs to assign
# the 3-char codes anyway).
h5adstore-push:
	@cd data/h5adstore && \
	  if [ -n "$$(git status --porcelain)" ]; then \
	    git add -A && \
	    git commit -m "auto: metadata snapshot $$(date -u +%Y-%m-%dT%H:%MZ)" && \
	    git push origin main; \
	  else \
	    echo "h5adstore: working tree clean — nothing to commit"; \
	    git fetch origin main >/dev/null 2>&1; \
	    ahead=$$(git rev-list --count origin/main..HEAD 2>/dev/null || echo 0); \
	    if [ "$$ahead" != "0" ]; then \
	      echo "h5adstore: $$ahead unpushed commit(s) — pushing"; \
	      git push origin main; \
	    fi; \
	  fi

# One-shot: S3 data + h5adstore git, in that order so a stale .md never
# blocks the S3 push that would have been fine anyway.
push-all: sync-push h5adstore-push

# ── Ingest ─────────────────────────────────────────────────────────────────
# End-to-end pipeline for new deposits + local rebuilds.
#
# Step 1 (`ant-ingest`):
#   Walks S3 __incoming/, pulls staged .h5ad + .h5ad.mus sidecars + DE files,
#   runs ant-h5ad-check → ant-convert → ant-duckdb-check on each h5ad,
#   runs ant-de-ingest on each DE file (deseq2 / edger / scanpy / long / …
#   preset autodetected from sidecar), promotes the result to the catalog
#   on S3, and re-stamps the sidecar with the catalog keys. Ledger at
#   data/h5adstore/.ingest-ledger.json records what each pass did.
#
# Step 2 (`ant-build-all`):
#   Catches the LOCAL case ant-ingest doesn't: an .h5ad already in the
#   catalog (not under __incoming/) whose .duckdb is stale because someone
#   edited the .md sidecar or hand-modified the h5ad. Refreshes the .md
#   (sha256, n_obs, layer_stats) via ant-h5ad-check and rebuilds the duckdb.
#
# Override the data project filter with FILTER=:
#   make ingest FILTER=Preman2024
#
# Filter is a STRING MATCH against __incoming/<DP>/ — exact (no globs).
FILTER ?=
ingest:
	@echo "── 1/2  S3 __incoming/ → catalog (.h5ad + DE) ──────────────"
	@./bin/ingest/ant-ingest --data-folder ./data/h5adstore $(if $(FILTER),--filter $(FILTER),)
	@echo ""
	@echo "── 2/2  rebuild any locally-stale .duckdb (and refresh .md) ─"
	@./bin/ingest/ant-build-all --data-folder ./data/h5adstore

# Plan-only variant — neither downloads, promotes, nor uploads. Useful before
# a long ingest to confirm what's about to happen.
ingest-dry:
	@./bin/ingest/ant-ingest --data-folder ./data/h5adstore --dry-run $(if $(FILTER),--filter $(FILTER),)
	@echo ""
	@echo "(ingest-dry: build-stale step skipped — run 'make ingest' for the real pass)"

# Rebuild + upload s3://<bucket>/_catalog/manifest.json. This file is the
# authoritative list of (data_project, group_id, file_id, basename, inactive)
# that mus uses to validate sidecar fields at `mus de check` / `mus s3 upload`
# time. ant-ingest writes it automatically at the end of every successful
# run; this target is the manual escape hatch for after a hand-edit of a
# .md or after toggling an .inactive sidecar.
manifest:
	@./bin/ops/ant-write-manifest --data-folder ./data/h5adstore

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
