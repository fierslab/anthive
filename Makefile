.PHONY: help test test-jupyter test-live-site vendor-pyvark build-reports build-webapp build-jupyter clean-webapp serve serve-nginx serve-api serve-browse serve-mcp serve-all serve-debug serve-prod serve-stop dev-jupyter refresh-viewer symlink sync-push sync-pull sync-plan sync-pull-evict sync-evict sync-pull-duckdb sync-pull-de sync-pull-all sync-status build-stale sync-update serve-status serve-restart h5adstore-push push-all ingest ingest-dry ingest-watch ingest-watch-once ingest-watch-status manifest cache-warm docker-build docker-run docker-smoketest docker-clean lock images sif push-images ship stack-check

help:
	@echo "Anthive4 Development Commands"
	@echo "=============================="
	@echo ""
	@echo "Webapp:"
	@echo "  make build-webapp   - Build complete webapp (JupyterLite + index)"
	@echo "  make build-jupyter  - Build only JupyterLite to webapp/jupyter"
	@echo "  make vendor-pyvark  - Re-vendor the pyvark client into JupyterLite (PYVARK=../pyvark)"
	@echo "  make test-jupyter   - JupyterLite end to end in headless Chromium (slow)"
	@echo "  make test-live-site URL=... - JupyterLite + client checks on a deployed site"
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
	@echo "Cache (gene atlas):"
	@echo "  make cache-warm        - Pre-populate tmp/gene_stats_cache.db on THIS host (curated panel, ~150 genes)"
	@echo "  make cache-warm GENES=APOE,TREM2,…   - Warm a custom gene list (CSV or @file.txt)"
	@echo ""
	@echo "Sync (S3) — the maintenance workflow:"
	@echo "  make sync-update     - Full cycle: pull h5ad → rebuild stale duckdb → push"
	@echo "  make sync-pull         - Pull per sync_policy in anthive.yaml (legacy default: h5ad + de.parquet)"
	@echo "  make refresh-viewer  - Full cycle for THIS instance: upgrade → restart → pull datastores"
	@echo "  make symlink TARGET=Sierksma2025  - Share a dataset from the main store into THIS viewer (symlinks)"
	@echo "  make sync-plan         - Dry-run: show the ordered priority fill plan (no transfers)"
	@echo "  make sync-pull-evict   - sync-pull + evict local .duckdbs below the policy cutoff"
	@echo "  make sync-evict        - Evict only (requires sync_policy.mode='priority')"
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
            --with pandas --with pyarrow --with h5py --with scipy --with scanpy \
            --with fastapi --with httpx --with jinja2 \
            --with markdown --with markdown-it-py --with requests --with anndata --with bcrypt \
            --with uvicorn --with matplotlib --with seaborn \
            --with 'bibtexparser>=1.4,<2' --with python-multipart --with tqdm --with boto3 \
            --with nbformat

test:
	uv run $(TEST_DEPS) env PYTHONPATH=lib python -m pytest tests/ -v

## test-jupyter - JupyterLite end to end in headless Chromium (slow)
# in:  frontend/jupyter/ sources; a running ant-serve (JUPYTER_API, default
#      http://localhost:8080); network to the Pyodide CDN; Chromium for
#      playwright (`uv run --with playwright playwright install chromium`)
# out: nothing kept; builds JupyterLite into a temporary directory, serves
#      it with an /api proxy on one origin and runs tests/test_jupyterlite_e2e.py.
#      Skips (with the reason) when the browser or the network is missing.
JUPYTER_API ?= http://localhost:8080
test-jupyter:
	@tmp_build=$$(mktemp -d) && trap 'rm -rf "$$tmp_build"' EXIT && \
	$(MAKE) --no-print-directory build-jupyter JUPYTER_OUT=$$tmp_build/jupyter >/dev/null && \
	ANTHIVE_TEST_JUPYTER=1 ANTHIVE_JUPYTER_BUILD=$$tmp_build ANTHIVE_JUPYTER_API=$(JUPYTER_API) \
	uv run --with pytest --with playwright --with nbformat env PYTHONPATH=lib:tests \
		python -m pytest -rs -v tests/test_jupyterlite_e2e.py tests/test_jupyterlite_static.py

## test-live-site - JupyterLite + client checks against a deployed site
# in:  URL=<site public_url> (required); credentials in ANTHIVE_LIVE_USER /
#      ANTHIVE_LIVE_PASSWORD or a ~/.netrc entry for the host; Chromium for
#      playwright and network to the Pyodide CDN for the browser part
# out: nothing kept; runs tests/live/ (doc/RELEASING.md, scratch-site check)
test-live-site:
	@test -n "$(URL)" || { echo "usage: make test-live-site URL=https://host[:port]"; exit 2; }
	ANTHIVE_LIVE_URL=$(URL) uv run --with pytest --with playwright --with requests \
		--with nbformat env PYTHONPATH=lib:tests python -m pytest -rs -v tests/live/

## vendor-pyvark - Copy pyvark's vark/*.py into the JupyterLite contents
# in:  PYVARK=<pyvark checkout> (default ../pyvark), committed
# out: frontend/jupyter/jupyter/vark/*.py, frontend/jupyter/vendored_pyvark.json
PYVARK ?= ../pyvark
vendor-pyvark:
	python3 frontend/jupyter/vendor_pyvark.py $(PYVARK)

# Pull static report sites from git repos (defined in anthive.yaml)
build-reports:
	@./bin/ops/ant-build-reports

clean-webapp:
	@echo "Cleaning webapp directory..."
	@rm -rf webapp
	@echo "✓ Clean complete!"

# build-jupyter packages the JupyterLite notebooks.
# Output: $(JUPYTER_OUT) (default webapp/jupyter, which the dev Caddy serves;
# `make test-jupyter` builds into a temporary directory instead).
JUPYTER_OUT ?= webapp/jupyter
# The `vark/` package is vendored under frontend/jupyter/jupyter/ — that's
# the source of truth for what the notebooks see (Pyodide treats it as a
# top-level package, no PyPI install needed). The canonical pyvark repo
# lives at codeberg.org/mfiers/pyvark — re-vendor with `make vendor-pyvark`
# (records version + commit + sha256 in frontend/jupyter/vendored_pyvark.json,
# which tests/test_jupyterlite_static.py checks).
build-jupyter:
	@echo "Building JupyterLite to $(JUPYTER_OUT)..."
	@echo ""
	@# Ensure source dir is writable (may be read-only from a previous run)
	@chmod -R u+rw frontend/jupyter/jupyter
	@# Clear output directory so no stale files survive the build
	@rm -rf $(JUPYTER_OUT)
	@mkdir -p $(dir $(JUPYTER_OUT))
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
			--output-dir $(JUPYTER_OUT)
	@# Restore read+write so files can be edited again
	@chmod -R u+rw frontend/jupyter/jupyter
	@echo "✓ JupyterLite build complete!"
	@echo ""
	@echo "Location: $(JUPYTER_OUT)/"
	@echo "Client:   $(JUPYTER_OUT)/files/vark/  (vendored from codeberg.org/mfiers/pyvark)"

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
	@echo "(expects nginx in front; see doc/DEPLOY.md and config/examples/nginx-vhost.*.conf.example)"
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

# Default pull. Behaviour depends on sync_policy.mode in anthive.yaml:
#   all       → legacy: h5ad + .de.parquet (.duckdb rebuilt via make build-stale)
#   duckdb    → all .duckdbs
#   priority  → only .duckdbs that fit under the priority + size cap
#               (see ant-priority for editing per-dataset priority).
sync-pull:
	@./bin/ops/ant-sync pull

# Dry-run: print the full ordered fill plan (priority desc → newest first) with
# per-dataset size, cumulative, projected free space, and action
# (HERE/TOUCH/PULL/PULL*/EVICT/SKIP). Transfers/deletes nothing.
sync-plan:
	@./bin/ops/ant-sync plan

# Combined: pull per policy AND evict local .duckdbs that fell below the cutoff,
# filling highest-priority-first until ~min_free_space remains. Slave semantics
# (S3 canonical): evicts freely, never pushes. Run `make sync-plan` first to
# preview. Held .duckdbs aren't freed until ant-serve releases them — see
# `make serve-restart`.
sync-pull-evict:
	@./bin/ops/ant-sync pull --evict

# Evict only, no pull. Useful when you've already pulled what you want and
# just need to free disk space. Requires sync_policy.mode='priority'.
sync-evict:
	@./bin/ops/ant-sync evict

# ─────────────────────────────────────────────────────────────────────────────
# REFRESH-VIEWER — the full maintenance cycle for THIS viewer instance, in the
# ONLY correct order:
#   [1] upgrade  — git pull --ff-only (so the NEW ant-sync runs the pull below)
#   [2] restart  — reload the whole stack: new server code goes live, the
#                  catalog is rebuilt, and any deleted-but-held duckdb handles
#                  are released (reclaiming disk)
#   [3] pull     — ONLY if sync_policy.mode=='priority' (the data-owning viewer):
#                  sync-pull-evict, filling highest-priority-first to ~min_free.
#                  On a viewer WITHOUT a priority policy (mode 'all'/absent) the
#                  pull is SKIPPED — otherwise it would fetch the whole catalog.
#                  Those viewers curate their datastore by hand; the cycle is
#                  just upgrade + restart for them.
# Order matters: upgrading before pulling ensures correct code; restarting
# between them means the pull runs against real free space and a new server.
#
# Single viewer only (runs in the current clone). To refresh every viewer on a
# host, run `make refresh-viewer` in each clone — the multi-instance loop is
# deployment-specific and deliberately kept OUT of this (public) Makefile.
#
# The systemd unit is derived from the folder name (<name>.bdslab.org →
# anthive-<name>); the restart uses sudo systemctl (prompts once). Without a
# unit (e.g. a dev box) it falls back to a non-sudo supervisor child-restart.
#
# NB: if the upgrade pulled web/ or frontend/ changes, also rebuild the static
# bundles: `make build-jupyter` and/or `make build-webapp`.
ANTHIVE_UNIT := anthive-$(firstword $(subst ., ,$(notdir $(CURDIR))))

# refresh-views — rebuild every duckdb's obscat/obsnum view from the
# current .md api: sections, then rescan the API catalog. Use after
# hand-edited .md changes (text editor / git pull) that ant-serve
# hasn't seen yet.
#
# Pass FILTER=<substring> to limit scope, e.g.:
#   make refresh-views FILTER=Sierksma2026
#   make refresh-views FILTER=F26/
refresh-views:
	@./bin/ops/ant-refresh-views $(if $(FILTER),--filter $(FILTER))

refresh-viewer:
	@echo "==> [1/3] upgrade — git pull --ff-only origin main"
	@git pull --ff-only origin main
	@echo "==> [2/3] restart stack ($(ANTHIVE_UNIT))"
	@if [ -f /etc/systemd/system/$(ANTHIVE_UNIT).service ]; then \
		echo "    sudo systemctl restart $(ANTHIVE_UNIT) (loads new code, frees held fds, fresh catalog)"; \
		sudo systemctl restart $(ANTHIVE_UNIT); \
		printf '    waiting for API'; for i in 1 2 3 4 5 6 7 8; do sleep 1; printf '.'; done; echo; \
	else \
		echo "    no systemd unit $(ANTHIVE_UNIT) — restarting children via supervisor (non-sudo)"; \
		./bin/ops/ant-supervisor restart all; \
		sleep 4; \
	fi
	@if grep -qE '^[[:space:]]*mode:[[:space:]]*priority' anthive.yaml 2>/dev/null; then \
		echo "==> [3/3] pull datastores — sync-pull-evict (sync_policy.mode=priority)"; \
		$(MAKE) --no-print-directory sync-pull-evict; \
	else \
		echo "==> [3/3] SKIP data pull — sync_policy.mode is NOT 'priority' in anthive.yaml."; \
		echo "         This viewer does not own a bounded priority datastore, so pulling"; \
		echo "         would fetch the WHOLE catalog. Only the data-owning instance"; \
		echo "         (mode: priority) auto-pulls here; manage this viewer's data by hand."; \
	fi
	@echo "==> refresh-viewer complete ($(ANTHIVE_UNIT))"

# ─────────────────────────────────────────────────────────────────────────────
# symlink — share one dataset (group or sub-path) from the data-owning viewer's
# h5adstore into THIS viewer's store, as symlinks (no data duplication). Run in
# the sharing viewer, e.g. review:
#     make symlink TARGET=Sierksma2025                 # a whole group
#     make symlink TARGET=Sierksma2025/Village.6M      # one sub-path/dataset
# Recreates the folder tree and symlinks EVERY file (.duckdb, .md, .h5ad.mus,
# .de.parquet, index.md, …) with ABSOLUTE links so they always resolve; then
# best-effort /admin/rescan so ant-serve picks the dataset up. Refuses if the
# target already exists here (remove it first to re-link).
# NB: `.inactive` status IS inherited from the source. ant-serve checks the
# sidecar at the symlink-RESOLVED (source) path, so a dataset marked inactive
# in the main store stays inactive here regardless of the viewer-local files.
# To serve an otherwise-inactive dataset here, copy it in as real files (own
# `.inactive` control) or drop the marker in the source store.
#
# MAIN_STORE points at the data-owning viewer's store; override if it's not the
# default sibling. DO NOT run this in the data-owning viewer itself.
MAIN_STORE ?= ../intern.bdslab.org/data/h5adstore
symlink:
	@test -n "$(TARGET)" || { echo "usage: make symlink TARGET=<Group>[/<sub>]  [MAIN_STORE=<path>]"; exit 2; }
	@src="$(abspath $(MAIN_STORE))/$(TARGET)"; dest="data/h5adstore/$(TARGET)"; \
	test "$(abspath $(MAIN_STORE))" != "$(abspath data/h5adstore)" || { echo "error: MAIN_STORE is THIS viewer's own store — nothing to share"; exit 2; }; \
	test -e "$$src" || { echo "error: not found in main store: $$src"; exit 2; }; \
	if [ -e "$$dest" ] || [ -L "$$dest" ]; then echo "error: '$$dest' already exists here — remove it first to re-link"; exit 2; fi; \
	mkdir -p "$$(dirname "$$dest")"; \
	cp -rs "$$src" "$$dest"; \
	n=$$(find -L "$$dest" -type f 2>/dev/null | wc -l); \
	d=$$(find -L "$$dest" -name '*.duckdb' 2>/dev/null | wc -l); \
	echo "symlinked $$n file(s) ($$d duckdb) into $$dest → $$src"; \
	port=$$(awk '/^rest_api_port:/{print $$2}' anthive.yaml 2>/dev/null); \
	if [ -n "$$port" ] && curl -fsS -X POST "http://127.0.0.1:$$port/admin/rescan" >/dev/null 2>&1; then \
		echo "rescanned ant-serve on :$$port — dataset live"; \
	else \
		echo "(restart the viewer or POST /admin/rescan to make it visible)"; \
	fi

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

# ── S3 watch route (doc/DATASET_INGESTION.md, "Depositing with mus") ────────
# ingest-watch: run bin/ingest/ant-ingest-watch in the foreground (dev). It
#   polls S3 ingest_watch.prefix (default __incoming/) every
#   ingest_watch.interval_seconds and publishes clean deposits into the
#   configured store. Needs ingest_watch.enabled: true in anthive.yaml; use
#   `make ingest-watch-once` for one poll. On a server, run it as its own
#   systemd unit (config/examples/anthive-ingest-watch.service.example),
#   never under ant-supervisor.
#   Its runtime dirs must lie outside the checkout: INGEST_WATCH_DIR (default
#   ../anthive4_state next to the repo) holds the state, the work files and
#   the tmp dir of the tools it runs.
#   in:  S3 deposits, anthive.yaml; out: store files, $(INGEST_WATCH_DIR)/ingest/*
INGEST_WATCH_DIR ?= $(abspath $(CURDIR)/../anthive4_state)
INGEST_WATCH_ENV = ANTHIVE_INGEST_STATE_DIR=$(INGEST_WATCH_DIR)/ingest \
                   ANTHIVE_INGEST_WORK_DIR=$(INGEST_WATCH_DIR)/ingest_work \
                   ANTHIVE_TMP_DIR=$(INGEST_WATCH_DIR)/tmp
ingest-watch:
	$(INGEST_WATCH_ENV) ./bin/ingest/ant-ingest-watch

# ingest-watch-once: one poll, then exit.
ingest-watch-once:
	$(INGEST_WATCH_ENV) ./bin/ingest/ant-ingest-watch --once

# ingest-watch-status: the items the watcher knows and their states.
ingest-watch-status: the items the watcher knows and their states.
ingest-watch-status:
	$(INGEST_WATCH_ENV) ./bin/ingest/ant-ingest-watch status

# ── Gene-atlas cache warm-up ────────────────────────────────────────────────
# Pre-populate tmp/gene_stats_cache.db by firing /api/gene_stats_all calls
# against the local ant-serve. The cache is host-local (lives outside
# data/h5adstore, NOT synced by ant-sync), so this target should run on
# whichever host serves users — typically prod.
#
# What gets warmed:
#   - Per-dataset distribution blob (~800 ms cold layer scan) for every
#     ingested dataset. Cached the FIRST time any gene query lands on
#     that dataset, then reused for every subsequent gene.
#   - Per-(db_id, gene) stats rows for the curated panel (~150 genes:
#     AD GWAS + microglia identity/DAM/IFN + house-keeping).
#
# After this runs once, every dataset's "any gene" query is sub-second.
#
# Adding a new dataset later does NOT invalidate existing cache rows —
# the new dataset's distribution + per-gene rows fill in lazily on first
# query. Cache keys are (db_id, gene_lc, fingerprint), so other datasets'
# rows are independent and stay valid. No need to re-run cache-warm
# unless you want the new dataset pre-warmed too (pass GENES=…).
#
# Custom panel:
#   make cache-warm GENES=APOE,TREM2,P2RY12
#   make cache-warm GENES=@my_genes.txt
GENES ?= DEFAULT
cache-warm:
	@./bin/ops/ant-warm-cache --genes '$(GENES)'

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


# ============================================================================
# Containers: the deployment path. See doc/DEPLOY_CONTAINERS.md (sites),
# doc/HPC.md (Apptainer / uv on clusters) and doc/DOCKER.md (image internals).
# ============================================================================

REGISTRY     ?= registry.gitlab.kuleuven.be/bdslab/anthive/anthive4
# Images are built from `git archive $(BUILD_REF)`: only committed, tracked
# files enter the build context, never local state, logs or secrets.
BUILD_REF    ?= HEAD
TAG          ?= $(patsubst v%,%,$(shell git describe --tags --always $(BUILD_REF) 2>/dev/null || echo dev))
APP_IMAGE    ?= $(REGISTRY)/anthive-app:$(TAG)
INGEST_IMAGE ?= $(REGISTRY)/anthive-ingest:$(TAG)
SIF          ?= anthive-ingest-$(TAG).sif
# Tracked paths kept out of the images (reports, tests, audits): see
# docker/image-excludes, also enforced by docker/check-image.
IMAGE_PATHSPEC := . $(shell sed -e '/^\#/d' -e '/^$$/d' -e "s/^\(.*\)$$/':(exclude,glob)\1'/" docker/image-excludes)

## lock - Regenerate docker/requirements-app.lock from the scripts' PEP-723 headers
# in:  bin/*/ant-*, web/ant-browse, docker/lock-overrides.txt
# out: docker/requirements-app.{in,lock}
lock:
	docker/gen-lock

## images - Build anthive-app and anthive-ingest as $(APP_IMAGE) / $(INGEST_IMAGE)
# in:  `git archive $(BUILD_REF)` (tracked files only), docker/requirements-app.lock,
#      docker/anthive-data.linux-64.explicit.txt
# out: two local docker images
images:
	docker/gen-lock --check
	@git diff --quiet HEAD -- || echo "note: uncommitted changes are NOT in the image (built from $(BUILD_REF))"
	git archive --format=tar $(BUILD_REF) -- $(IMAGE_PATHSPEC) | docker build --pull -f docker/Dockerfile -t $(APP_IMAGE) -
	git archive --format=tar $(BUILD_REF) -- $(IMAGE_PATHSPEC) | docker build -f docker/Dockerfile.ingest --build-arg APP_IMAGE=$(APP_IMAGE) -t $(INGEST_IMAGE) -
	docker/check-image $(APP_IMAGE) $(BUILD_REF)
	docker/check-image $(INGEST_IMAGE) $(BUILD_REF)

## sif - Build the Apptainer image for HPC ingest from the local ingest image
# in:  $(INGEST_IMAGE) in the local docker daemon
# out: $(SIF)
sif:
	apptainer build --force --build-arg IMAGE=$(INGEST_IMAGE) $(SIF) docker/anthive-ingest.def

## push-images - Push both images (and the .sif as an OCI artifact if built) to $(REGISTRY)
# needs: `docker login $(REGISTRY)` (and `apptainer registry login` for the sif)
push-images:
	docker/check-image $(APP_IMAGE) $(BUILD_REF)
	docker/check-image $(INGEST_IMAGE) $(BUILD_REF)
	docker push $(APP_IMAGE)
	docker push $(INGEST_IMAGE)
	@if [ -f "$(SIF)" ]; then \
	    apptainer push $(SIF) oras://$(REGISTRY)/anthive-ingest-sif:$(TAG); \
	else echo "no $(SIF): skipping the Apptainer artifact (make sif)"; fi

## ship - Build, test and push a release: make ship TAG=v1.2.3
ship: test images push-images

## stack-check - Validate ./site.yaml and print the resolved deployment plan
stack-check:
	bin/ops/ant-stack check

# ── legacy single-container targets (docker run ... all) ───────────────────
DOCKER_IMAGE ?= $(REGISTRY)/anthive-app
DOCKER_TAG   ?= $(TAG)
DOCKER_PORT  ?= 8501

## docker-build - Build only the app image (use `make images` for both)
docker-build:
	docker build --pull -f docker/Dockerfile -t $(DOCKER_IMAGE):$(DOCKER_TAG) .

## docker-run - Run the image against ./data/h5adstore + ./data/genesetstore on $(DOCKER_PORT)
docker-run:
	@[ -d "$(PWD)/data/h5adstore" ] || (echo "no data/h5adstore/ in cwd — set H5ADSTORE for a different path" >&2; exit 1)
	@[ -d "$(PWD)/data/genesetstore" ] || (echo "no data/genesetstore/ in cwd — set GENESETSTORE for a different path" >&2; exit 1)
	docker run --rm -it \
	    --name anthive-$(DOCKER_TAG) \
	    -p $(DOCKER_PORT):$(DOCKER_PORT) \
	    -e ANTHIVEPORT=$(DOCKER_PORT) \
	    -v "$(PWD)/data/h5adstore:/data/h5adstore" \
	    -v "$(PWD)/data/genesetstore:/data/genesetstore" \
	    $(DOCKER_IMAGE):$(DOCKER_TAG) all

## docker-smoketest - Boot the image, hit /api/health, tear down. Returns non-zero on failure.
docker-smoketest:
	@bash docker/smoketest.sh $(DOCKER_IMAGE):$(DOCKER_TAG) $(DOCKER_PORT)

## docker-clean - Remove the local anthive image
docker-clean:
	docker image rm -f $(DOCKER_IMAGE):$(DOCKER_TAG) || true
