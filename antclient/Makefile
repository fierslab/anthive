# antclient — build + release targets.
#
# Top-level workflow:
#
#   make test          - offline tests (no server)
#   make test-smoke    - smoke tests against a running ant-serve
#                        (needs ANTHIVE_TEST_URL=http://host/api)
#   make build         - wheel + sdist into dist/
#   make check         - `twine check` on dist/*
#   make ship-test     - upload to TestPyPI (rehearsal before make ship)
#   make ship          - upload to PyPI (test → build → check → confirm → upload)
#   make clean         - remove dist/, build/, *.egg-info
#
# Versioning convention: see RELEASE.md.  Bump in three places before
# running `make ship`:
#   __version__ in antclient.py
#   version    in pyproject.toml
#   add a new entry to CHANGELOG.md
# (`make ship` re-checks they agree.)

.PHONY: help test test-smoke build check ship ship-test clean version-check

# Single source of truth for the version, read from pyproject.toml.
VERSION := $(shell grep -E '^version' pyproject.toml | head -1 | sed -E 's/.*"([^"]+)".*/\1/')

help:
	@echo "antclient $(VERSION) — release commands"
	@echo ""
	@echo "  make test          Run offline tests (no server, ~1s)"
	@echo "  make test-smoke    Run smoke tests against a live ant-serve"
	@echo "                     (set ANTHIVE_TEST_URL=http://host/api)"
	@echo "  make build         Build wheel + sdist into dist/"
	@echo "  make check         Verify dist/* metadata via twine check"
	@echo "  make ship-test     Upload to TestPyPI (rehearsal)"
	@echo "  make ship          Upload to PyPI (the real thing)"
	@echo "  make clean         Remove dist/, build/, *.egg-info"
	@echo ""
	@echo "Before \`make ship\`: bump __version__, pyproject.toml version,"
	@echo "and add a CHANGELOG.md entry — see RELEASE.md."

# ── tests ─────────────────────────────────────────────────────────────────────

test:
	uv run --with pytest --with requests --no-project \
	    python -m pytest tests/test_offline.py -v

test-smoke:
	@if [ -z "$$ANTHIVE_TEST_URL" ]; then \
	    echo "ERROR: ANTHIVE_TEST_URL is not set."; \
	    echo "  Example:  ANTHIVE_TEST_URL=http://localhost:8080/api make test-smoke"; \
	    exit 2; \
	fi
	uv run --with pytest --with requests --with pandas --no-project \
	    python -m pytest tests/test_smoke.py -v

# ── version + build ───────────────────────────────────────────────────────────

# Verify __version__ in antclient.py matches the pyproject.toml version
# we just parsed — catches the "I bumped one and forgot the other" mistake.
version-check:
	@SRC_VERSION=$$(grep '__version__ = ' antclient.py | sed -E 's/.*"([^"]+)".*/\1/'); \
	if [ "$$SRC_VERSION" != "$(VERSION)" ]; then \
	    echo "ERROR: version mismatch."; \
	    echo "  pyproject.toml: $(VERSION)"; \
	    echo "  antclient.py:   $$SRC_VERSION"; \
	    echo "Bump both before building."; \
	    exit 1; \
	fi
	@CHANGELOG_HAS=$$(grep -c "^## \[$(VERSION)\]" CHANGELOG.md); \
	if [ "$$CHANGELOG_HAS" = "0" ]; then \
	    echo "WARNING: no CHANGELOG.md entry for [$(VERSION)] — add one before shipping."; \
	fi

clean:
	rm -rf dist build *.egg-info

build: version-check clean
	uv build
	@echo ""
	@ls -lh dist/

check:
	@if [ ! -d dist ] || [ -z "$$(ls -A dist 2>/dev/null)" ]; then \
	    echo "ERROR: dist/ is empty — run \`make build\` first."; \
	    exit 1; \
	fi
	uv run --with twine --no-project twine check dist/*

# ── ship ──────────────────────────────────────────────────────────────────────
#
# Both ship-test and ship run the same gates: tests → version-check → build
# → twine check.  ship asks for confirmation before the real upload because
# PyPI uploads can't be undone (you can yank but not delete).  ship-test
# is unattended.

ship-test: version-check test build check
	@echo ""
	@echo "==> Uploading antclient $(VERSION) to TestPyPI..."
	uv run --with twine --no-project twine upload --repository testpypi dist/*
	@echo ""
	@echo "Verify the rehearsal with:"
	@echo "  pip install --index-url https://test.pypi.org/simple/ --upgrade antclient"

ship: version-check test build check
	@echo ""
	@echo "═════════════════════════════════════════════════════"
	@echo " About to upload to PyPI (the real, public index):"
	@echo "   antclient version: $(VERSION)"
	@echo "   wheel: dist/antclient-$(VERSION)-py3-none-any.whl"
	@echo "   sdist: dist/antclient-$(VERSION).tar.gz"
	@echo "═════════════════════════════════════════════════════"
	@echo ""
	@echo "PyPI uploads cannot be undone (only yanked).  Did you:"
	@echo "  - bump __version__ + pyproject.toml + CHANGELOG.md?"
	@echo "  - run \`make test\` and \`make test-smoke\`?"
	@echo "  - rehearse with \`make ship-test\`?"
	@echo ""
	@read -p "Type the version ($(VERSION)) to confirm: " confirm; \
	if [ "$$confirm" != "$(VERSION)" ]; then \
	    echo "Aborted — confirmation didn't match $(VERSION)."; \
	    exit 1; \
	fi
	uv run --with twine --no-project twine upload dist/*
	@echo ""
	@echo "✓ uploaded antclient $(VERSION) to PyPI."
	@echo ""
	@echo "Tag the release in git:"
	@echo "  git tag antclient-v$(VERSION)"
	@echo "  git push origin antclient-v$(VERSION)"
	@echo ""
	@echo "Verify (give PyPI a minute to index):"
	@echo "  pip install --upgrade antclient"
	@echo "  python -c 'import antclient; print(antclient.__version__)'"
