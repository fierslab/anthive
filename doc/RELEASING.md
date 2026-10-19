# Releasing anthive

For maintainers. Deployers only need [DEPLOY_CONTAINERS.md](DEPLOY_CONTAINERS.md).

## Version numbers

The release version is the REST API version: `API_VERSION` in
`bin/api/ant-serve`, with its history in `doc/API_CHANGELOG.md`.
`lib/__init__.py` carries the same number; `tests/test_version_sync.py`
fails if they drift. A running server reports it in `/api/health`
(`api_version`), so you can see which release a site runs.

- patch (2.37.x): fixes only, no API change
- minor (2.x.0): additions (new endpoints or parameters with defaults)
- major (x.0.0): anything that breaks an existing client

## Before the tag

Tick every box before step 1 of "Cutting a release".

**Code merged**

- [ ] Every branch and worktree for this release is merged into `main`;
      `git worktree list` shows only the main checkout, and nothing is left
      uncommitted that belongs in the release.

**API clean**

- [ ] `doc/API_CHANGELOG.md` matches the code: every new or changed
      endpoint, parameter and response field of this release has an entry,
      and no entry describes something that did not ship.
- [ ] Breaking changes are marked BREAKING, with what a client must do.
- [ ] The version follows the rule above (patch: fixes only; minor:
      additions; major: breaks), or the changelog says why not.
- [ ] Versions in sync: `API_VERSION` in `bin/api/ant-serve`, `lib/__init__.py`
      (`tests/test_version_sync.py`), `API_COMPATIBLE_PYVARK`.
- [ ] Deprecations whose announced removal date or release has passed are
      removed now, or their date is moved with a changelog line.

**Clients synced**

- [ ] pyvark (`codeberg.org/mfiers/pyvark`) and vark cover the new and
      changed endpoints, or the gap is written down.
- [ ] MCP tools in `bin/api/ant-mcp` match the REST changes.
- [ ] JupyterLite bundles the current pyvark (`make vendor-pyvark` after the
      pyvark release commit) and `make test-jupyter` passes against a running
      ant-serve of this release (headless Chromium, needs network for
      Pyodide; `doc/JUPYTERLITE_BUILD.md`).
- [ ] The agent skills (`data/skills`) are current, including the help sync
      (`make help-sync ANTHIVE=<this checkout>` in `data/skills`, review the
      diff, rebuild).

**Docs current**

- [ ] `lib/help_content/` and `web/templates/help.html` describe every
      user-facing change (`tests/test_help_content.py`).
- [ ] `web/CLAUDE.md` and the other scoped `CLAUDE.md` files name the new
      pieces.
- [ ] For any change to how a site is set up or run: `doc/DEPLOY_CONTAINERS.md`
      (including the upgrade table row), `site.example.yaml` and
      `ant-stack check` are updated.
- [ ] Non-container guide (`doc/DEPLOY.md`): an upgrade section for changes
      that affect host installs (nginx vhost, `anthive.yaml` keys, services
      to restart together, scripts and cron jobs), with the nginx examples in
      `config/examples/` updated and passing `nginx -t`. Containers stay the
      preferred way; this section only keeps existing hosts safe.
- [ ] Release numbers are bumped in `doc/DEPLOY_CONTAINERS.md` (the "run
      end to end ... for release X.Y.Z" line, `git checkout vX.Y.Z`,
      `tag: X.Y.Z` in step 4) and `images.tag` in `site.example.yaml`.

**Data pushed**

- [ ] `ant sync push` has run for every dataset, facet result and library
      change the release relies on.
- [ ] When they changed: the gene stats and the module variability indices
      are rebuilt and pushed, and the literature index (below).

## Cutting a release

From a clean checkout of `main` on a machine with Docker:

```sh
# 1. bump both version strings, set API_VERSION_RELEASED (today, YYYY-MM-DD) and
#    API_COMPATIBLE_PYVARK in bin/api/ant-serve, and date the changelog heading;
#    set images.tag in site.example.yaml and the release numbers in
#    doc/DEPLOY_CONTAINERS.md to X.Y.Z
$EDITOR bin/api/ant-serve lib/__init__.py doc/API_CHANGELOG.md site.example.yaml doc/DEPLOY_CONTAINERS.md
make test                                   # no new failures
git commit -am "X.Y.Z: <summary>"
git tag -a vX.Y.Z -m "anthive X.Y.Z: <summary>"

# 2. build the images FROM THE TAG and verify them
make images BUILD_REF=vX.Y.Z

# 3. publish (GitLab registry only)
docker login registry.gitlab.kuleuven.be     # token with read_registry + write_registry
make push-images BUILD_REF=vX.Y.Z
R=registry.gitlab.kuleuven.be/bdslab/anthive/anthive4
for img in anthive-app anthive-ingest; do
  docker tag $R/$img:X.Y.Z $R/$img:latest && docker push $R/$img:latest
done

# 4. publish the code (origin pushes to GitHub and the GitLab mirror)
git push origin main && git push origin vX.Y.Z
```

`make images` builds from `git archive <ref>`, so only committed, tracked
files enter the image, never local state such as the Authelia user database,
logs or nested checkouts. After each build, `docker/check-image` compares the
image with the git tree and scans for data and secret-type files; `make
push-images` runs it again and refuses to push a failing image. Do not bypass
it with a plain `docker build .`.

Tracked files that have no place in an image (reports, tests, audits) are
listed in `docker/image-excludes`. The build leaves them out of the
`git archive` context and `check-image` refuses an image that contains any of
them. `.dockerignore` does not help here: Docker does not apply it to a tar
context piped in on stdin. Images up to 2.40.2 shipped `report/` and `tests/`
for that reason. Anything private that is tracked in git must go on that list,
or better, out of the repo.

If the library gained papers since the last release, rebuild the literature
index and publish it first (it is data, not part of the image):

```sh
ant lib-index --library data/library --store data/h5adstore   # incremental
ant sync push --library-only                                  # PDFs + library_chunks.parquet
```

The app image bakes the query-embedding model (`EMBEDDING_MODEL` build arg in
`docker/Dockerfile`, default `BAAI/bge-small-en-v1.5`); the index must be
built with the same model. Changing the model means a full index rebuild
(`ant lib-index --full`) and updating `IMAGE_EMBEDDING_MODEL` in
`bin/ops/ant-stack`.

## After the tag

- [ ] **Scratch-site check.** Before announcing a release, deploy it once on
      a scratch site following DEPLOY_CONTAINERS.md word for word (fresh
      `state_dir`, anonymous pulls with an empty `DOCKER_CONFIG`). Also change
      a setting afterwards (for example `auth.internal_requires_auth`) and
      check with `ant-stack up` that it takes effect. Release 2.37.2 found
      four real problems that way, 2.45.0 found that `up` ignored config-only
      changes. After `up`, run the live checks from this checkout against the
      scratch site with a seeded account:
      `ANTHIVE_LIVE_USER=admin ANTHIVE_LIVE_PASSWORD=... make test-live-site URL=<public_url>`
      (JupyterLite loads after the login, serves this release's vark and
      notebooks, its kernel reaches the API; see `doc/JUPYTERLITE_BUILD.md`).
- [ ] **pyvark on PyPI.** The maintainer publishes the matching pyvark
      release to PyPI.
- [ ] **vark.** The maintainer runs `make ship` in the vark repo.

## Where images live

Images are published **only** to the KU Leuven GitLab registry, next to the
GitLab mirror of the source:

- `registry.gitlab.kuleuven.be/bdslab/anthive/anthive4/anthive-app`
- `registry.gitlab.kuleuven.be/bdslab/anthive/anthive4/anthive-ingest`

They are public (anonymous pulls). `site.example.yaml` and the Makefile
default to this registry. There is no CI build yet: releases are built and
pushed by hand as above. A `.gitlab-ci.yml` on the GitLab mirror is the
natural place for it if KU Leuven runners with Docker are available.

The Apptainer image for HPC is not published; users build it from the
ingest image ([HPC.md](HPC.md)). `make sif` builds it locally, and it can be
pushed to the same registry as an OCI artifact with
`apptainer push anthive-ingest-X.Y.Z.sif oras://$R/anthive-ingest-sif:X.Y.Z`
after `apptainer registry login --username <user> oras://registry.gitlab.kuleuven.be`.

Self-hosting a registry (zot behind Caddy) is on the long-term list; nothing
in the code would change except `REGISTRY` and `images.registry`.
