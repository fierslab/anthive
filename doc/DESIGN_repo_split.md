# Design: splitting anthive4 into public repos (minimal)

**Status:** proposed, **deferred** — not executing yet. This records the plan
so it's ready when we publish.

## Why

We want to open-source anthive4 without leaking deployment details. The
working tree is already sanitized (no real hostnames/IPs/paths/secrets), but
**git history still contains the pre-sanitization values**. Rather than rewrite
history (`filter-repo` + force-push across every clone — disproportionate, and
disruptive to the dev box + other checkouts), we publish by **carving the repo
into a few pieces, each seeded as a fresh `git init` with a single initial
commit** from the sanitized tree. Every published repo therefore starts with a
clean history; this monorepo stays private/archived as the source of record.

## Granularity decision: minimal

We keep the runtime-/build-coupled application together and only peel off what
is genuinely independent. The architectural seams for `web/` and
`frontend/jupyter/` are clean (HTTP-only to the REST API), but the **deploy**
coupling is not: `bin/ops/ant-supervisor` launches `./web/ant-browse` and
`make build-jupyter` builds `frontend/` into `webapp/`. Splitting those out
would force submodule/sibling assembly + a supervisor path tweak for little
gain right now. So:

| Repo | Contents | Notes |
| --- | --- | --- |
| **anthive** (engine + UI) | `lib/` + `bin/` + `web/` + `frontend/` + `tests/` + `Makefile(.data)` + `config/` (generic infra only) + `envs/anthive-data.yml` + `bash_completion/` + root files | The deployable application: REST API + MCP + ingest/annotation + browser UI + JupyterLite. `lib/`↔`bin/` are inseparable (the `sys.path` shim); web/frontend stay because the supervisor + Makefile assemble them. |
| **anthive-docs** | `doc/` | Pure docs (mkdocs); trivial to peel, no code coupling. Optional — may stay in `anthive`. |

### Already separate (leave as-is)
- **pyvark** — the Python REST client (`from vark import AnthiveClient`).
- **h5adstore**, **genesetstore** — the data repos (`.md` tracked; `.h5ad`/`.duckdb` in S3).
- **config/server/** — private per-instance deploy configs (hostnames, units, vhosts).

### Deferred / future option: fine-grained
If assembly is later worthwhile, peel `web/` → `anthive-web` and
`frontend/jupyter/` → `anthive-notebooks` (both clean HTTP seams), and `src/`
(dataset-prep recipes) → `anthive-dataprep` or into the data repo. Each needs:
- supervisor: make the web launcher path configurable (env/flag) instead of the
  hardcoded `./web/ant-browse`;
- Makefile: point `build-jupyter` at the notebooks checkout;
- assemble via git submodules in `anthive`, or sibling checkouts.

## Publishing procedure (per repo, when we do it)

1. From the sanitized monorepo tree, copy the repo's subtree into a fresh dir.
2. `git init` + add a clean `.gitignore`, `README.md`, LICENSE → single initial
   commit (no imported history).
3. Create the public remote, push.
4. Keep this monorepo private as the source of record; develop here, re-sync the
   public repos on release (export, not history-merge).

## Leak posture
- Published repos: clean history by construction (fresh init).
- This monorepo: history is **not** scrubbed (accepted while private).
- Re-verify `git ls-files | xargs grep` for the sanitized-token set before each
  publish, in case new commits reintroduce a real value.
