# Easy-deploy for anthive — one-command install for non-technical users

## Goal

A user on a Linux/macOS box with Docker installed runs **one command**:

```bash
curl -fsSL https://anthive.example/install.sh | bash
```

…and ends up with:

1. A running anthive stack (REST API + web UI + optional MCP) on a chosen port.
2. Two persistent data folders (`h5adstore`, `genesetstore`) at a user-chosen host path.
3. A working `ant` command for maintenance (ingest / score / annotate) that
   dispatches into the running container.
4. A `vark` binary in `~/.local/bin/`, preconfigured to point at the local
   server with the just-generated credentials.
5. A single browser tab open at `http://localhost:PORT/` with a
   working login.

The user is prompted for **three** things during the install:
- Data folder host path (default: `~/anthive-data/`)
- Port (default: `8501`)
- Whether they want anthive password-protected (default: yes → auto-generated password shown at end)

Anything else is either sensible-default or optional-later.

---

## What already exists (survey)

| Piece | State | File |
| --- | --- | --- |
| Docker image | Built, runs supervisor + api + browse + caddy in one container | `docker/Dockerfile`, `docker/entrypoint.sh` |
| Container README | Env-var and volume contract documented | `docker/README.md` |
| Prod deploy recipe | nginx-fronted with Authelia — for mfiers-specific setup | `doc/DEPLOY.md` |
| Ingest pipeline | `bin/ingest/ant-convert`, `ant-md`, `ant-refresh-views` | `bin/ingest/`, `bin/meta/`, `bin/ops/` |
| Web UI | Self-contained Jinja+FastAPI under `web/` | `web/` |
| Basic-auth via Caddy | Env-var driven — `ANTHIVE_BASIC_AUTH_USER/PASSWORD` | `docker/entrypoint.sh` |
| S3 sync | Optional — env-var driven | `bin/ops/ant-sync` |
| vark (Go CLI) | Published to codeberg releases; single static binary | `codeberg.org/mfiers/vark` |

The **image already runs headlessly with sensible defaults** — `docker run -p 8501:8501 -v /path/to/data:/data/h5adstore anthive:latest` works today. The gap is the *ergonomic wrapper* + first-run wizard + credential plumbing + vark install.

---

## Proposed approach

### Layer 1 — the install script (`install-anthive.sh`)

Idempotent bash script. Runs on fresh Ubuntu / Debian / macOS.

Behaviour:

1. **Preflight** — check `docker` + `docker compose` are installed and the daemon is reachable; fail with a copy-pasteable install hint per OS if not.
2. **Wizard** — interactive prompts (skip via `--data-dir X --port 8501 --auth basic` flags for CI):
   - Data folder host path
   - Port
   - Auth mode: `none` / `basic` (default) / `advanced` (link to `DEPLOY.md`)
   - S3 backup: `no` (default) / `yes` (then prompt bucket + endpoint + creds; can defer)
3. **Materialise** — create `~/.anthive/` config dir:
   - `~/.anthive/config.env` — the docker-compose env file (port, mounts, auth, optional S3)
   - `~/.anthive/docker-compose.yml` — a small compose file pinning the released image
   - Auto-generate a random basic-auth password (if auth=basic) — write to `~/.anthive/PASSWORD.txt` chmod 600
4. **Pull image + start**:
   ```
   docker compose --project-directory ~/.anthive up -d
   ```
5. **Install `ant` wrapper** — write `~/.local/bin/ant` that does `docker compose exec anthive ant "$@"`. Same for `ant-md`, `ant-serve` (read-only introspection), etc.
6. **Install `vark`** — download the matching-version binary from codeberg to `~/.local/bin/vark`, seed a `~/.config/vark/servers.yaml` pointing at `http://localhost:PORT/api` with the just-generated credentials.
7. **Health check** — `curl` the local API's `/version` and print PASS/FAIL. If FAIL, `docker compose logs anthive` tail.
8. **Show the summary** — URL, username, password (also in `~/.anthive/PASSWORD.txt`), path to data folder, next-step hints (`ant convert my.h5ad`, `vark databases list`).

### Layer 2 — a small `docker-compose.yml` template

One service (`anthive`), two named volumes or bind mounts (`h5adstore`, `genesetstore`), `env_file: config.env`. Uses the **published** image from a registry (ghcr.io or codeberg's registry — decide when we set up CI).

### Layer 3 — updating the running stack (`anthive update` command)

Reuses `~/.anthive/docker-compose.yml`. `anthive update` pulls the newest tagged image and restarts. `anthive down` / `anthive up` / `anthive logs`. Wrapper for the compose invocation.

### Layer 4 — help + docs

`doc/INSTALL.md` (currently a stub) becomes the user-facing README the install script mirrors. `doc/DEPLOY.md` (the mfiers-specific nginx+Authelia recipe) is explicitly labelled "advanced / behind-an-existing-reverse-proxy".

---

## What still needs to be done

### Blocking for a v0.1 install-script release

- [ ] **Publish the anthive Docker image** to a registry the install script can pull from. Currently the image is built locally; there is no `ghcr.io/mfiers/anthive:latest` or similar. Options:
    - Codeberg's container registry (`codeberg.org/mfiers/anthive`)
    - Docker Hub
    - ghcr.io
  Decision needed before the install script has anywhere to pull from.
- [ ] **CI: build + push on tag** — GitHub / codeberg Actions workflow that tags images on git tags and pushes `latest` on `main`.
- [ ] **`install-anthive.sh` itself** — the script does not exist yet. Layer 1 above is the spec.
- [ ] **`~/.local/bin/ant` wrapper** — trivial (10 lines of bash) but needs writing.
- [ ] **vark server-registration** — pyvark and vark both currently accept `--server URL` and `--auth user:pass`; the wrapper needs to write these to their config files.
- [ ] **`docker/README.md` → `INSTALL.md` promotion** — a user-facing quickstart that isn't dev-facing.

### Nice-to-have for v0.2

- [ ] **`anthive` meta-command** (as opposed to `ant`) — one CLI on the host that does `up/down/update/logs/health`. Right now the user has to remember `docker compose` incantations.
- [ ] **TLS out of the box** — Caddy can auto-provision LetsEncrypt certs if the box has a public hostname. Wizard could ask; unset by default (localhost).
- [ ] **Windows support** — Docker Desktop + WSL2. Would need a `install-anthive.ps1` or "run this inside WSL". Skip for v0.1.
- [ ] **`ant restore` from S3** — pull a curated demo dataset (e.g. one small microglia h5ad) so the user has *something* to look at before they've ingested their own. Very high perceived-quality-of-onboarding for near-zero effort.

### Security posture — what to advise the user

The install script prints (at the end and in `~/.anthive/README.txt`):

```
YOUR ANTHIVE SERVER
===================
URL:            http://localhost:8501/
Username:       reviewer
Password:       <auto-generated 20 chars>   (also in ~/.anthive/PASSWORD.txt, chmod 600)
Data folder:    /home/you/anthive-data/

DEFAULT SECURITY POSTURE
========================
- Port 8501 is bound to localhost only. From this machine only.
- Basic-auth is on. Rotate the password with: anthive password rotate
- If you want to reach anthive from OTHER machines:
    - Use SSH tunnelling:   ssh -L 8501:localhost:8501 you@this-box
    - Or make it public with a reverse proxy + TLS — see doc/DEPLOY.md.
      Don't expose port 8501 directly to the internet — basic auth over
      HTTP is not enough.
- The password is unrecoverable if you lose it. Rotate, don't recover.
```

Key rule: **anthive is not designed to be internet-facing without a
reverse proxy** (Caddy inside the container will serve HTTP; TLS
termination should happen outside). The install script defaults to
`127.0.0.1:PORT`, not `0.0.0.0:PORT`, precisely to make casual exposure
harder.

---

## What is specific to mfiers's setup vs generic

Ranked by how tightly coupled to the current author's box.

### Definitely mfiers-specific — must be genericized before shipping the installer

| Thing | Where it lives now | How to genericize |
| --- | --- | --- |
| S3 credentials | `anthive.yaml.s3.*` | Move to env vars (`ANTHIVE_S3_*`); optional; installer prompts and writes to `config.env` |
| S3 bucket name (`anthive`) | `anthive.yaml.s3.bucket` | Per-user, prompted (default: no S3) |
| h5adstore path (`data/h5adstore/`) | `Makefile`, various | Already parameterised via `ANTHIVE_H5ADSTORE` env var in the container; installer wires host path |
| Authelia integration | `config/authelia/`, `config/caddy-auth.dev` | Skip in the easy-deploy path entirely. Advanced users follow `doc/DEPLOY.md`. |
| nginx-in-front | `config/nginx-intern-anthive.conf` | Same — advanced-only |
| The specific `data/h5adstore` git repo (`github.com/badslab/h5adstore`) | referenced in top-level `CLAUDE.md` | New users start with an empty h5adstore — don't clone this |
| The `data/reports/` build (research reports for mfiers papers) | `bin/ops/ant-build-reports` | Off by default; feature-flag env var |
| Anthive chat panel (Claude API key) | `anthive.yaml.chat.api_key` | Off by default; env var if enabled |
| starCAT store path | `STARCATSTORE` env var | Already env-var driven; installer asks "download the ~200MB starCAT catalog?" and does it if yes |

### Grey area — configurable but reasonable to ship defaults

- **Default palettes / plot styling** — hardcoded in `lib/plotting.py`. Fine as-is; a user who wants to change them edits their own config.
- **`duckdb_memory_limit: "128GB"`** in `anthive.yaml` — this is mfiers's box. Installer should introspect available RAM and default to `min(host_ram // 2, 128GB)`.
- **Default port `8501`** — Caddy's default; keep, but obviously configurable.
- **Chat model** (`claude-opus-4`) — reasonable default; user provides API key or feature stays off.

### Actually generic — no changes needed

- `bin/` toolchain (`ant-convert`, `ant-md`, `ant-score`, `ant-serve`, …) — these are dataset-agnostic.
- `lib/` shared core.
- `web/` UI — reads dataset list from `/databases`; renders whatever is there.
- The mus vocabulary bundled at `lib/_mus_vocab.yaml` — generic (mfiers is a contributor upstream, but the vocab is shared).

### Anti-pattern to avoid

Do **not** try to ship the install script with a curated set of demo datasets baked in. The h5adstore is measured in tens of gigabytes. A demo dataset should be **fetched on demand** by the installer if the user opts in (`ant demo download`), not embedded in the image.

---

## Rough timeline (fleshed if you say go)

- **Day 1**: Publish the image to a registry (codeberg or ghcr). Push CI to auto-build on tag.
- **Day 2**: Write `install-anthive.sh` + `docker-compose.yml` template + `ant` wrapper. Test on a fresh Debian 12 box + a fresh Ubuntu 24.04 box.
- **Day 3**: Auto-install of vark binary + credential plumbing. `anthive` meta-command (up/down/update/logs/health).
- **Day 4**: `doc/INSTALL.md` user-facing quickstart. Video-length demo. Buff `doc/DEPLOY.md` to explicitly say "this is the advanced path".
- **Day 5**: Test the full install on a fresh cloud VM. `ant demo download` fetch of one small human microglia h5ad so the user has a working plot before they've ingested their own data.

Post-v0.1, iterate on Windows/WSL, LetsEncrypt-in-a-box, and the `anthive update` UX.

---

## Decisions I need from you

1. **Registry for the image**: codeberg vs ghcr.io vs docker hub. Preference?
2. **Default auth mode**: `basic` (as I proposed) vs `none` for localhost-only? Basic feels safer even for localhost — leaks less if the port ends up bound to `0.0.0.0` by mistake.
3. **Demo dataset**: yes/no? If yes, one lightweight human microglia h5ad (say, S25/PhM at ~600 MB, already scored, ships nicely).
4. **`ant` on host vs inside-container-only**: the wrapper approach (host `ant` → `docker exec`) means users don't have Python/uv installed. Only downside: `ant convert bigfile.h5ad` streams the h5ad through docker's bind mount, but that's the same disk anyway. Any concern?
5. **Metric of success**: "install to first plot in under 5 minutes on a clean cloud VM" a reasonable v0.1 target?
