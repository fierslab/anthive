# Prod cutover handoff — Caddy-as-single-source-of-routing refactor

**For:** the Claude session running on the prod box (`intern.bdslab.org`).
**From:** the dev-box Claude session, 2026-06-23.
**Status:** Dev verified, two commits pushed (`82ecb16` + `f19966c`),
prod is untouched.
**Outage budget:** 30–90 seconds during the supervisor restart.

---

## Read first — full context (don't skip)

This is a structural refactor of the prod deployment topology, not a
routine restart. Before doing anything, read these in order:

1. This file (you're in it).
2. `config/Caddyfile` (the new single source of path routing).
3. `config/nginx-intern-anthive.conf` (the new slim prod vhost).
4. `bin/ops/ant-supervisor` lines 22–32 and 134–162 (prod-mode env wiring).
5. `doc/DEPLOY.md` if it exists (older deploy notes; may have stale
   bits but the cert paths are still useful).

Two commits to look at:
```
git show 82ecb16 --stat    # the refactor itself
git show f19966c           # the dev-side TLS fix that came right after
```

---

## What this refactor does (1-minute mental model)

**Before:** nginx on prod terminated TLS + did Authelia gate + did ALL
path routing — one ~200-line vhost with `location` blocks for /api,
/browse, /jupyter, /mcp, /artifacts, /authelia, /.well-known, /register,
/streamlit, each forwarding to a different localhost port. Dev used
Caddy with its own copy of the same routing, hand-kept-in-sync.

**After:** nginx on prod becomes a thin shim — terminates TLS, runs
Authelia `auth_request`, forwards EVERY non-Authelia path to Caddy on
`127.0.0.1:8500`. Caddy owns ALL path routing (single `Caddyfile`,
same on dev + prod). Service-port routing (`:8080` api, `:8081` browse,
`:8090` mcp, `:9091` authelia) lives in Caddy.

The dev/prod split is now driven by env vars consumed by `config/Caddyfile`:

| env var                       | dev default              | prod (set by `--prod`)            |
|-------------------------------|--------------------------|-----------------------------------|
| `ANTHIVE_LISTEN`              | (multi-host on :8501)    | `http://127.0.0.1:8500`           |
| `ANTHIVE_TLS_SNIPPET`         | `caddy-tls.dev`          | `caddy-tls.prod` (empty)          |
| `ANTHIVE_AUTH_SNIPPET`        | `caddy-auth.dev`         | `caddy-auth.prod` (trust headers) |
| `ANTHIVE_HTTP_REDIRECT_SNIPPET` | `caddy-http-redirect.dev` | `caddy-http-redirect.prod` (empty) |
| `ANTHIVE_CADDY_BIND`          | `0.0.0.0 ::`             | `127.0.0.1`                       |

`bin/ops/ant-supervisor --prod` (or `ANTHIVE_PROD=1`) injects these.

The `caddy-*.prod` snippet files are intentionally empty — see comments
inside each one. They MUST exist; Caddy errors on a missing `import`.

---

## Prod is on systemctl — what to update

Find the unit. Likely candidates:

```bash
systemctl list-units --all | grep -iE 'anthive|ant-supervisor|ant-serve'
sudo systemctl status anthive
sudo systemctl status anthive-supervisor
sudo find /etc/systemd/system /lib/systemd/system -name '*anthive*' -o -name '*ant-*' 2>/dev/null
```

Read the existing unit file. Note the current `ExecStart`, `WorkingDirectory`,
`User`, `Environment*` lines.

### The change to the unit

The old `ExecStart` was probably one of:

- `./bin/ops/ant-supervisor` (no flags → dev-style, no Caddy)
- `make serve-all`
- A handful of separate units per service (api / browse / mcp / authelia)

The new `ExecStart` should be:

```ini
ExecStart=/data/anthive/anthive4/bin/ops/ant-supervisor --prod --caddy
```

(adjust path to wherever the checkout actually lives on prod).

Equivalent alternative if the unit can set env:
```ini
Environment=ANTHIVE_PROD=1
ExecStart=/data/anthive/anthive4/bin/ops/ant-supervisor --caddy
```

`--prod` and `ANTHIVE_PROD=1` do the same thing — pick whichever style
fits the rest of the unit.

**Critical:** ensure `WorkingDirectory=` is the repo root. The supervisor
uses relative paths (`./envs/caddy/caddy`, `./config/Caddyfile`, etc.).
If the unit doesn't set `WorkingDirectory`, the spawn will fail with
"file not found".

**Critical:** if the unit has separate `[Service]`s for api / browse /
mcp / authelia (the old pre-supervisor pattern), **disable those first**
and run them all under the supervisor instead:

```bash
sudo systemctl disable --now anthive-api anthive-browse anthive-mcp anthive-authelia 2>/dev/null
```

Running both the old per-service units AND the new supervisor will
cause port conflicts. The supervisor is now the single parent.

---

## Pre-flight checklist (before touching anything live)

```bash
# Where I am, what's the user
pwd
whoami
hostname

# Repo state
cd <repo>
git status                     # working tree should be clean
git log -1 --oneline           # remember this hash — rollback point
git pull origin main
git log -1 --oneline           # expect: f19966c caddy: keep dev hostnames…

# Files that must now be present
ls config/Caddyfile config/caddy-tls.{dev,prod} \
   config/caddy-auth.{dev,prod} \
   config/caddy-http-redirect.{dev,prod} \
   config/nginx-intern-anthive.conf

# Caddy binary — may NOT be present on prod yet
ls -l envs/caddy/caddy 2>/dev/null && ./envs/caddy/caddy version
# If missing → see "Install Caddy" below

# Validate the new Caddyfile compiles in prod-mode env
ANTHIVE_PROD=1 \
  ANTHIVE_LISTEN=http://127.0.0.1:8500 \
  ANTHIVE_TLS_SNIPPET=caddy-tls.prod \
  ANTHIVE_AUTH_SNIPPET=caddy-auth.prod \
  ANTHIVE_HTTP_REDIRECT_SNIPPET=caddy-http-redirect.prod \
  ANTHIVE_CADDY_BIND=127.0.0.1 \
  ./envs/caddy/caddy validate --config config/Caddyfile
# Expect: "Valid configuration"

# Current systemd state
sudo systemctl status anthive                       # whatever the unit is named
sudo cat /etc/systemd/system/<unit-name>.service    # remember the OLD ExecStart

# Current nginx state
sudo nginx -t                                       # should pass before we touch anything
sudo ls /etc/nginx/sites-enabled/                   # find the intern.bdslab.org vhost
sudo cat /etc/nginx/sites-enabled/<intern vhost>    # remember the OLD vhost

# What's listening now
ss -tlnp | grep -E ':80|:443|:8080|:8081|:8090|:8500|:9091'
```

Write down (in your scratchpad or a file):
- Old git hash
- Old ExecStart line (verbatim)
- Old vhost filename + path

That's the rollback recipe.

---

## Install Caddy if missing

Two paths, both fine:

**Option A — copy the dev-box binary (recommended):**
```bash
# From the dev box (mark's laptop), not prod:
scp envs/caddy/caddy <prod-host>:<repo>/envs/caddy/caddy
chmod +x envs/caddy/caddy
```
The binary is statically linked x86_64 — runs anywhere.

**Option B — download official:**
```bash
mkdir -p envs/caddy
curl -L 'https://caddyserver.com/api/download?os=linux&arch=amd64' -o envs/caddy/caddy
chmod +x envs/caddy/caddy
./envs/caddy/caddy version
```

---

## Cutover sequence

Do these in order. Don't skip the verifications between steps.

### Step 1 — Update the systemd unit

```bash
sudo systemctl edit --full anthive    # or whatever the unit is named
# Update ExecStart per "The change to the unit" above.
# Save + close.
sudo systemctl daemon-reload
```

DO NOT restart yet.

### Step 2 — Stop the old supervisor

```bash
sudo systemctl stop anthive    # OR the per-service units if that's what was running
ss -tlnp | grep -E ':8080|:8081|:8090|:8500|:9091'
# Expect: empty (no anthive ports listening)
```

**Outage starts now.** Nginx will return 502 on any request because
its upstreams (8080/8081/etc.) are gone. Move quickly.

### Step 3 — Start the supervisor in prod mode

```bash
sudo systemctl start anthive
sudo systemctl status anthive --no-pager
journalctl -u anthive -n 50 --no-pager
```

Look for:
- `starting anthive — prod (caddy on 127.0.0.1:8500, nginx terminates TLS)`
- One line per child: `api`, `browse`, `authelia`, `caddy`, …
- No crash loops in the journal.

```bash
ss -tlnp | grep -E ':8080|:8081|:8090|:8500|:9091'
# Expect: 8080 (api), 8081 (browse), 8090 (mcp if enabled),
#         8500 (caddy bound to 127.0.0.1 only), 9091 (authelia)
```

If `:8500` is bound to `0.0.0.0` instead of `127.0.0.1`, the
`ANTHIVE_CADDY_BIND=127.0.0.1` env didn't get through. Check the
unit's WorkingDirectory and that `--prod` is on ExecStart.

### Step 4 — Verify Caddy locally (nginx still pointing at old upstreams!)

```bash
curl -s http://127.0.0.1:8500/api/health
# Expect: {"status":"healthy","api_version":"2.13.0",…}

curl -sI http://127.0.0.1:8500/browse/
# Expect: 200

curl -sI http://127.0.0.1:8500/jupyter/lab/index.html
# Expect: 200 OR 404 if /jupyter bundle wasn't built — see "Build JupyterLite" below

curl -sI http://127.0.0.1:8500/authelia/
# Expect: 200 (Authelia login UI HTML)
```

**Do not proceed to step 5 if any of these fail.** Fix Caddy first.
The outage continues but at least nginx still gives a clean 502 instead
of a partially-broken site.

### Step 5 — Swap the nginx vhost

**Before copying — edit two things in the new file:**

```bash
diff config/nginx-intern-anthive.conf /etc/nginx/sites-available/<old vhost>
```

Look at lines 34–36 of `config/nginx-intern-anthive.conf`:
```
ssl_certificate         /etc/nginx/cert/star_bdslab_org.crt;
ssl_certificate_key     /etc/nginx/cert/star_bdslab_org.key;
ssl_trusted_certificate /etc/nginx/cert/DigiCertCA.crt;
```

If the old vhost uses different cert paths (Let's Encrypt commonly uses
`/etc/letsencrypt/live/intern.bdslab.org/fullchain.pem` etc.), **edit
the new file to match the old paths before copying**.

Also check line 76: `proxy_pass http://127.0.0.1:9091/authelia/api/authz/auth-request;`
Confirm the old vhost used the same Authelia internal endpoint. If the
old one used a different path (e.g. `/api/verify` for older Authelia),
match that — but Authelia v4.39 uses the path in the new file.

Then atomic swap:

```bash
sudo cp /etc/nginx/sites-available/<old vhost> \
        /etc/nginx/sites-available/<old vhost>.bak-$(date +%Y%m%d-%H%M)
sudo cp config/nginx-intern-anthive.conf /etc/nginx/sites-available/<old vhost>
sudo nginx -t            # MUST pass
sudo systemctl reload nginx
```

**Don't `systemctl restart nginx`** — `reload` is graceful. Restart
drops in-flight connections.

### Step 6 — Smoke test from outside

From your laptop, not prod:

```bash
# Unauthed — should redirect to login
curl -kI https://intern.bdslab.org/api/health
# Expect: 302 → /authelia/?rd=...

# Authelia portal — should serve login HTML
curl -kI https://intern.bdslab.org/authelia/
# Expect: 200

# Basic-auth path — Authelia accepts it
curl -k -u '<user>:<pass>' https://intern.bdslab.org/api/health
# Expect: {"status":"healthy","api_version":"2.13.0",…}

# Jupyter static
curl -kI -u '<user>:<pass>' https://intern.bdslab.org/jupyter/lab/index.html
# Expect: 200

# Catalog page
curl -kI -u '<user>:<pass>' https://intern.bdslab.org/browse/
# Expect: 200 (or 302 if browser-cookie path is enforced for browse — also fine)
```

Then open `https://intern.bdslab.org/` in a real browser, log in via
Authelia, click around: catalog ▸ a dataset ▸ a plot ▸ a JupyterLite
notebook. Two-minute smoke test. **Outage ends here.**

---

## Build JupyterLite (only if step 4's `/jupyter/lab/index.html` was 404)

```bash
make build-jupyter    # builds frontend/jupyter into webapp/jupyter/
# Then re-check:
curl -sI http://127.0.0.1:8500/jupyter/lab/index.html
```

The bundle ships as a static export; Caddy `file_server` serves it
from `./webapp/jupyter/`.

---

## Rollback

Two independent reversals; do whichever is needed.

**Rollback nginx alone (if step 6 fails but Caddy works):**
```bash
sudo cp /etc/nginx/sites-available/<old vhost>.bak-YYYYMMDD-HHMM \
        /etc/nginx/sites-available/<old vhost>
sudo nginx -t && sudo systemctl reload nginx
```
You're now back on the OLD nginx config talking to the new supervisor.
Old nginx pointed at `:8080`, `:8081`, etc. directly — those ports are
still served by the new supervisor, so this should still work.

**Rollback the supervisor (if step 3 fails):**
```bash
sudo systemctl edit --full anthive
# Restore the OLD ExecStart line
sudo systemctl daemon-reload
sudo systemctl restart anthive
```

**Rollback the code (last resort):**
```bash
git reset --hard <old hash>
sudo systemctl restart anthive
```

---

## Gotchas / known issues

| Symptom                                    | Likely cause                                                                                       | Fix                                                                                                |
|--------------------------------------------|----------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------|
| `502 Bad Gateway` everywhere               | Caddy not running on `127.0.0.1:8500`                                                              | `ss -tlnp \| grep 8500`; `journalctl -u anthive -n 100`                                            |
| `503` on `/authelia/` only                 | Authelia not running on `:9091`                                                                    | `ss -tlnp \| grep 9091`; check supervisor included `authelia` in DEFAULT_SERVICES                  |
| Login loops on `/authelia/?rd=...`         | Authelia cookie domain doesn't match `intern.bdslab.org`                                           | Check `config/authelia/configuration.yml` `session.cookies` block                                  |
| `/jupyter/` 404                             | Static bundle not built on prod                                                                   | `make build-jupyter`                                                                               |
| Caddy refuses to start, "file not found"   | `WorkingDirectory` in systemd unit isn't the repo root                                             | Add/fix `WorkingDirectory=/data/anthive/anthive4` (or wherever)                                    |
| `:8500` bound to `0.0.0.0` not `127.0.0.1` | `ANTHIVE_CADDY_BIND` env not propagated                                                            | Confirm `--prod` (or `ANTHIVE_PROD=1`) on ExecStart                                                |
| `tlsv1 alert internal error` from browser  | (Should not happen on prod — TLS is nginx's job there. Only would happen if dev hostnames leaked.) | Check `ANTHIVE_LISTEN=http://127.0.0.1:8500` not the dev default                                   |
| `:8500 → :8501` redirect loop              | Old HTTP-redirect block leaked                                                                     | Confirm `ANTHIVE_HTTP_REDIRECT_SNIPPET=caddy-http-redirect.prod`                                   |

---

## Post-cutover housekeeping

1. **Commit the systemd unit change** if it lives in a config repo
   somewhere. If the unit is hand-edited on the box, add a note in the
   project memory: `feedback_prod_supervisor_systemd.md`.

2. **Tell the dev session** the cutover succeeded. Via pika:
   ```bash
   pika message send --to anthive4 --from anthive-prod \
     "Cutover done. ant-serve 2.13.0 + Caddy 2.11.4 + new nginx vhost live on intern.bdslab.org. Outage was N seconds."
   ```

3. **If anything was surprising**, save a `project_*` memory in
   `~/.claude/projects/-data-1-users-mark-anthive4/memory/` so future
   sessions inherit the knowledge.

4. **Long-term TODO** (from dev session): system Caddy on the dev box is
   in a bind-conflict loop. `sudo systemctl disable --now caddy` there.
   This does NOT apply to prod — prod doesn't run a system Caddy.

---

## If you get stuck

Send a pika message to the dev session:
```bash
pika message send --to anthive4 --from anthive-prod \
  "Stuck at step N: <one-line symptom>. Logs: <relevant journalctl/nginx error>."
```

Or write the question to a file and email it:
```bash
pika mail send --to mark@... --subject "anthive prod cutover blocked" \
  --body "<details>"
```

Don't guess at the systemd unit shape — read what's actually there
first. Don't bypass `nginx -t` if it fails — investigate.

---

**End of handoff.** Read top to bottom, then execute.
