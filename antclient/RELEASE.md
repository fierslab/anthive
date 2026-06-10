# antclient — release process

Publishing a new version of antclient to PyPI. Document is kept short
and procedural; the per-release diff lives in `CHANGELOG.md`.

## What's in this directory at release time

| File | Purpose |
| --- | --- |
| `pyproject.toml`, `setup.py`, `MANIFEST.in` | packaging |
| `antclient.py`, `anthelper.py`, `__init__.py` | the actual package (single-file core + helper) |
| `CHANGELOG.md` | version history (Keep-a-Changelog style) |
| `LICENSE` | MIT |
| `README.md` | the package's PyPI front page |
| `dist/` | built artifacts (`*.whl`, `*.tar.gz`) — ready for `twine upload` |
| `tests/` | offline + smoke tests (see below) |

## Version policy

semver. Bump in three places (and only these three):

1. `antclient/antclient.py:__version__`
2. `antclient/pyproject.toml` → `version = "..."`
3. `antclient/CHANGELOG.md` — new top-level entry, dated.

Convention:

- **patch** (1.7.x) — bug fix, no API change.
- **minor** (1.x.0) — additive — new methods, new optional parameters
  with defaults. Existing callers keep working.
- **major** (x.0.0) — breaking: rename, drop, change return shape.

## Build

From `antclient/`:

```sh
# Clean any old artifacts
rm -rf dist build *.egg-info

# Build wheel + sdist — uv calls into setuptools via pyproject.toml
uv build

# Verify the artifacts pass twine's metadata check
uv run --with twine --no-project twine check dist/*

# Verify the wheel installs + imports cleanly into a fresh env
uv run --no-project --with dist/antclient-<NEW_VERSION>-py3-none-any.whl python -c "
import antclient
print(antclient.__version__)
"
```

Output should be exactly the new version string.

## Test before tagging

```sh
# Offline — no server needed
uv run --with pytest --with requests --no-project \
    python -m pytest tests/test_offline.py -v

# Smoke — round-trip against a running ant-serve
ANTHIVE_TEST_URL=http://localhost:8080/api \
    uv run --with pytest --with requests --with pandas --no-project \
    python -m pytest tests/test_smoke.py -v

# Add auth via env vars if your test server has basic auth:
#   ANTHIVE_TEST_USER=bds  ANTHIVE_TEST_PASSWORD=astrocyte  ANTHIVE_TEST_URL=... pytest ...
```

Both should be all-green. The smoke tests gracefully skip themselves
when `ANTHIVE_TEST_URL` isn't set, so the offline suite is what gets
run on every commit; the smoke suite is what gets run before release.

## Publish

```sh
# Sanity-check what twine sees:
uv run --with twine --no-project twine check dist/*

# Upload — needs PyPI credentials. Use a TestPyPI dry-run first for
# a major release; subsequent releases can go straight to PyPI.
uv run --with twine --no-project twine upload \
    --repository testpypi dist/*           # dry run on TestPyPI
uv run --with twine --no-project twine upload dist/*   # real upload

# Verify on the public index after a minute or two:
pip install --upgrade antclient
python -c "import antclient; print(antclient.__version__)"
```

Credentials: configure `~/.pypirc` once, or pass `--username
__token__ --password <pypi-api-token>` on each upload.

## Tag

```sh
git tag antclient-v<NEW_VERSION>
git push origin antclient-v<NEW_VERSION>
```

(The repo tag is independent of the PyPI release — it just makes
the source state easy to reconstruct.)

## Migration / compatibility notes

- The single-file design (`antclient.py` + `anthelper.py`) is
  deliberate — Pyodide / micropip clients (the in-browser
  JupyterLite notebooks at `frontend/jupyter/`) pull the file
  directly. Don't split into submodules without coordinating
  with the JupyterLite build.
- Server compatibility: each `1.x.0` entry in `CHANGELOG.md`
  lists the REST API version it requires. The current 1.7.0
  needs **REST API 2.3+** for the DE wrappers (graceful 404 on
  older servers; `get_version` synthesises a 2.2-compatible
  fallback from `/health.api_version`).
