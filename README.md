# anthive

anthive is a browser and API for curated single-cell RNA-seq datasets. It
takes `.h5ad` files, turns them into fast DuckDB databases, and serves them
to people (a web site), to code (a REST API with Python and Go clients), to
notebooks (JupyterLite in the browser) and to AI assistants (MCP).

The main deployment holds 186 datasets in 63 collections, about 17 million
cells, mostly human microglia in health and Alzheimer's disease (September
2026, release 2.37.2).

This repository is the home of the whole project: the server, the web UI,
the ingest and annotation tools, the deployment tooling and the
documentation. The other repositories below hold the data catalogue, the
gene sets and the client libraries.

---

## What is in it

| Part | What it does | Where |
| --- | --- | --- |
| Web browser | collections, dataset pages, interactive plots, gene atlas, gene-set and module views, differential expression, admin editors | `web/` |
| REST API | datasets, cells, genes, plots (PNG/SVG/PDF), module scores, DE results, sandboxed SQL analytics | `bin/api/ant-serve` |
| MCP server | the same data as tools for AI clients (Claude, Cursor, VS Code, ...), with a private link per user | `bin/api/ant-mcp` |
| Notebooks | JupyterLite in the browser, preloaded with the Python client | `frontend/jupyter/` |
| Data assistant | optional in-app chat that can query and plot (via OpenRouter) | `lib/chat_*.py` |
| Ingest | check an `.h5ad`, convert it to DuckDB, merge annotations back | `bin/ingest/` |
| Annotation | module scores (scanpy, UCell), curation helpers | `bin/annot/` |
| Catalogue tools | dataset metadata (`.md`, `.h5ad.mus`), validators, gene sets | `bin/meta/` |
| Operations | deployment (`ant-stack`), S3 sync, supervisor | `bin/ops/` |
| Shared core | the DuckDB query layer, plotting, config; used by everything above | `lib/` |

Everything is started through one command dispatcher, `bin/ant`
(`ant convert ...`, `ant sync ...`, `ant score ...`).

## How the data flows

```
 .h5ad ──ant convert──▶ .duckdb + parquet ──ant-serve──▶ REST API ──▶ web UI / MCP / notebooks / vark / pyvark
   ▲                         │
   └──ant merge-h5ad─────────┘  annotations (module scores, curated columns) are written
                                 to the .duckdb first and merged back into the .h5ad later
```

The `.h5ad` is the long-term source of truth. The `.duckdb` and its parquet
sidecars are what the server reads. Data files live on S3 and are synced
with `ant sync`; dataset descriptions live in git (the `h5adstore` repo).

---

## Deploy a site

Sites run as a docker compose stack from published images, configured by
one `site.yaml`. The step-by-step guide, tested end to end:

**[doc/DEPLOY_CONTAINERS.md](doc/DEPLOY_CONTAINERS.md)**

In short, on a Linux server with Docker and uv:

```sh
git clone https://gitlab.kuleuven.be/bdslab/anthive/anthive4.git && cd anthive4
git checkout v2.37.2
bin/ops/ant-stack init        # writes site.yaml; edit it, then run init again
bin/ops/ant-stack check
bin/ops/ant-stack pull
bin/ops/ant-stack up
```

Read the guide before you start: you also need the catalogue, the data and a
suitable address, and it explains each of those.

Converting data on a compute cluster (no web stack needed):
**[doc/HPC.md](doc/HPC.md)**.

Images (public, linux/amd64):
`registry.gitlab.kuleuven.be/bdslab/anthive/anthive4/anthive-app` and
`.../anthive-ingest`, tagged with the release number.

---

## Use it

- **In the browser:** open the site and log in.
- **From an AI client:** in the web UI, open your account menu, choose
  "Connect an AI client (MCP)", and paste your personal link into Claude,
  Cursor or VS Code ([doc/MCP_LINKS.md](doc/MCP_LINKS.md)).
- **From Python:** [`pyvark`](https://codeberg.org/mfiers/pyvark)
  (`pip install pyvark`, then `from vark import AnthiveClient`).
- **From the shell:** [`vark`](https://codeberg.org/mfiers/vark), a single
  static Go binary for scripting and batch work.
- **Over HTTP:** the API documents itself at `/api/docs` on any site;
  changes per release are in [doc/API_CHANGELOG.md](doc/API_CHANGELOG.md).

---

## The repositories

| Repository | Holds |
| --- | --- |
| **anthive4** (this one): [GitHub](https://github.com/badslab/anthive4), mirror on [KU Leuven GitLab](https://gitlab.kuleuven.be/bdslab/anthive/anthive4) | all code, deployment tooling, documentation, the container images |
| **h5adstore**: GitHub `badslab/h5adstore`, mirror `gitlab.kuleuven.be/bdslab/anthive/h5adstore` (private) | the dataset catalogue: one `.md` + `.h5ad.mus` per dataset, one `index.md` per collection. No data files. |
| **anthive-genesetdb**: `gitlab.kuleuven.be/bdslab/genesetdbs/anthive-genesetdb` (private) | the gene sets and modules used for scoring |
| **pyvark**: [codeberg.org/mfiers/pyvark](https://codeberg.org/mfiers/pyvark) | the Python client |
| **vark**: [codeberg.org/mfiers/vark](https://codeberg.org/mfiers/vark) | the Go command-line client |
| **mus**: [codeberg.org/mfiers/mus](https://codeberg.org/mfiers/mus) | the controlled vocabulary for dataset annotation (`.h5ad.mus`) |

The data files themselves (`.h5ad`, `.duckdb`, parquet) are in an S3 bucket,
not in any repository.

---

## Work on the code

Start with [CLAUDE.md](CLAUDE.md): it explains how the pieces fit together,
and each subfolder has its own `CLAUDE.md` with the details for that part.
The most important rule: `lib/` is shared by every tool and the server, so a
change there affects all of them.

- Every tool in `bin/<area>/ant-*` is a self-contained `uv run --script`
  with its dependencies in its own header; there is no shared virtualenv.
- Tests: `make test`.
- Run the full stack on a development machine without containers:
  `bin/ops/ant-supervisor --caddy` (see [doc/DEVELOPMENT.md](doc/DEVELOPMENT.md)).
- Making a release (version bump, images, tags):
  [doc/RELEASING.md](doc/RELEASING.md).

### Documentation map

| Topic | Document |
| --- | --- |
| deploying a site | [DEPLOY_CONTAINERS.md](doc/DEPLOY_CONTAINERS.md) |
| cluster ingest | [HPC.md](doc/HPC.md) |
| releasing | [RELEASING.md](doc/RELEASING.md) |
| architecture | [ARCHITECTURE.md](doc/ARCHITECTURE.md) |
| DuckDB layout | [DATABASE_SCHEMA.md](doc/DATABASE_SCHEMA.md) |
| checking and converting `.h5ad` | [CHECK_PROCESS.md](doc/CHECK_PROCESS.md), [CONVERT_PROCESS.md](doc/CONVERT_PROCESS.md) |
| dataset metadata files | [METADATA.md](doc/METADATA.md), [METADATA_STRUCTURE.md](doc/METADATA_STRUCTURE.md) |
| writing annotations back | [DESIGN_annotation_updates.md](doc/DESIGN_annotation_updates.md) |
| configuration keys | [CONFIG.md](doc/CONFIG.md), `anthive.default.yaml` |
| API changes | [API_CHANGELOG.md](doc/API_CHANGELOG.md) |
| MCP links | [MCP_LINKS.md](doc/MCP_LINKS.md) |
| image internals | [DOCKER.md](doc/DOCKER.md) |
| old host install (no containers) | [DEPLOY.md](doc/DEPLOY.md), [INSTALL.md](doc/INSTALL.md) |

---

## Status

Release **2.37.2** (September 2026). The release number is the REST API
version; a running site reports it at `/api/health`.

Maintained by the BADS lab, KU Leuven (Mark Fiers). License: not yet
specified; ask before reusing the code.
