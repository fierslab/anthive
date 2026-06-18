# Authelia setup — handover for another host

This is a self-contained walkthrough for setting up Authelia auth in front of
an anthive4 instance (or any HTTP app) on a fresh Linux host, sitting behind
Caddy OR nginx. Written to be readable cold by another Claude Code session.

## What you're building

```
browser ──https──> caddy/nginx :8501 ──forward_auth──> authelia :9091
                          │
                          └─> ant-serve / ant-browse / your app
```

Authelia owns the login portal + session cookies. The reverse proxy calls
Authelia on every request via `forward_auth` (Caddy) or `auth_request`
(nginx); Authelia returns 200 (allow) or 302 (redirect to portal).

**Per-instance auth, no SSO between instances.** Each host has its own users
+ its own secrets. By design — see the conversation that produced this doc.

## Per-host prerequisites

- Linux x86_64
- Caddy 2.x (`caddy version`) or nginx ≥ 1.18 with `--with-http_auth_request_module`
- A free port for Authelia (default 9091)
- The reverse proxy MUST serve HTTPS to the user. Authelia v4.39+ hard-requires
  `https://` in cookie URLs — there is no flag to bypass. If you don't have a
  real cert, Caddy's `tls internal` directive issues self-signed certs from
  its local CA (works fine for private networks; users accept the warning once
  or run `caddy trust`).

---

## 1. Install the Authelia binary

```bash
cd /path/to/anthive4
mkdir -p envs/authelia

LATEST=$(curl -sS https://api.github.com/repos/authelia/authelia/releases/latest \
         | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])')

curl -sSL "https://github.com/authelia/authelia/releases/download/${LATEST}/authelia-${LATEST}-linux-amd64.tar.gz" \
     -o /tmp/authelia.tar.gz

tar -xzf /tmp/authelia.tar.gz -C envs/authelia/
mv envs/authelia/authelia-linux-amd64 envs/authelia/authelia
chmod +x envs/authelia/authelia

envs/authelia/authelia --version   # sanity check
```

## 2. Generate per-host secrets

**These MUST be unique per host.** Never share across machines.

```bash
mkdir -p config/authelia/secrets
openssl rand -hex 64 > config/authelia/secrets/jwt
openssl rand -hex 64 > config/authelia/secrets/session
openssl rand -hex 32 > config/authelia/secrets/encryption
chmod 600 config/authelia/secrets/*
```

Make sure `.gitignore` excludes them:

```
config/authelia/secrets/
config/authelia/users_database.yml
config/authelia/db.sqlite3
config/authelia/notification.txt
envs/authelia/
```

## 3. Write `config/authelia/configuration.yml`

Replace the **two placeholders** before saving:

- `PRIMARY_HOST` — user-facing hostname; **MUST have a dot OR be an IP** (Authelia rejects `localhost` for cookie scoping). Use `localhost.localdomain` for purely-local boxes, the Nebula/LAN IP for private network, or a real DNS name for production.
- `PORT` — the reverse proxy's HTTPS port (e.g. `8501`).

If the host has multiple access paths (e.g. `localhost.localdomain` + Nebula
IP), repeat the `cookies:` list entry once per domain.

```yaml
theme: dark

server:
  # Path-aware base URL: Authelia knows it lives at <host>/authelia/.
  address: 'tcp://127.0.0.1:9091/authelia'

log:
  level: 'info'
  format: 'text'
  file_path: './tmp/authelia.log'
  keep_stdout: true

authentication_backend:
  file:
    path: './config/authelia/users_database.yml'
    watch: true            # hot-reload on file change, no daemon restart
    password:
      algorithm: 'argon2'

access_control:
  default_policy: 'one_factor'   # 'two_factor' to require TOTP for everyone

session:
  name: 'authelia_session'
  same_site: 'lax'
  expiration: '1 hour'
  inactivity: '15 minutes'
  remember_me: '1 month'
  cookies:
    - domain: 'PRIMARY_HOST'
      authelia_url: 'https://PRIMARY_HOST:PORT/authelia/'
      default_redirection_url: 'https://PRIMARY_HOST:PORT/'
    # add more domains if multiple access paths:
    # - domain: '10.42.0.3'
    #   authelia_url: 'https://10.42.0.3:PORT/authelia/'
    #   default_redirection_url: 'https://10.42.0.3:PORT/'

regulation:
  max_retries: 5
  find_time: '2 minutes'
  ban_time: '10 minutes'

storage:
  local:
    path: './config/authelia/db.sqlite3'

notifier:
  filesystem:
    filename: './config/authelia/notification.txt'
```

Validate before continuing:

```bash
AUTHELIA_SESSION_SECRET_FILE=$(pwd)/config/authelia/secrets/session \
AUTHELIA_STORAGE_ENCRYPTION_KEY_FILE=$(pwd)/config/authelia/secrets/encryption \
AUTHELIA_IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET_FILE=$(pwd)/config/authelia/secrets/jwt \
./envs/authelia/authelia validate-config --config config/authelia/configuration.yml
```

Should print "Configuration parsed and loaded with warnings:" + one warning
about `default_policy`. That warning is fine.

## 4. Create the first user

```bash
USERNAME=mf      # short, no spaces
PASSWORD='change-me-now-and-change-it-again-in-the-portal'

HASH=$(./envs/authelia/authelia crypto hash generate argon2 --password "$PASSWORD" \
       | grep -oE '\$argon2id\$[^ ]+')

cat > config/authelia/users_database.yml <<EOF
users:
  ${USERNAME}:
    displayname: 'Display Name'
    password: '${HASH}'
    email: 'user@example.com'
    groups: ['admins']
EOF
chmod 600 config/authelia/users_database.yml
```

The user changes their password via the portal's Settings → Reset Password
once logged in. Adding more users later: same `crypto hash generate` command,
append to `users_database.yml`. Authelia watches the file → no restart needed.

## 5. Wire the reverse proxy

### 5a. Caddy

Append to `config/Caddyfile` (replace `PRIMARY_HOST`, `PORT`, and the
upstream service ports as needed):

```caddy
{
    # Auto-HTTPS provisioning is needed for `tls internal` to issue certs.
    # `disable_redirects` skips the :80 redirect listener (which fails as
    # non-root anyway).
    auto_https disable_redirects
}

# Plain-HTTP companion port. Catches users typing http:// and redirects.
# Same listener cannot serve both http and https — separate port required.
http://PRIMARY_HOST:8500, http://10.42.0.3:8500 {
    redir https://{host}:8501{uri} 301
}

PRIMARY_HOST:8501, 10.42.0.3:8501 {
    bind {$ANTHIVE_CADDY_BIND:0.0.0.0 ::}

    # Self-signed cert via Caddy's internal CA. For real production,
    # replace with `tls /path/cert.pem /path/key.pem` or a Let's Encrypt
    # block.
    tls internal

    # Authelia portal — always reachable, never gated by forward_auth.
    handle /authelia/* {
        reverse_proxy localhost:9091
    }

    # Routes that bypass auth (e.g. MCP-discovery, public endpoints).
    @bypass_auth path /.well-known/* /register

    # Everything else: forward_auth → Authelia.
    @protected not path /authelia/* /.well-known/* /register
    forward_auth @protected localhost:9091 {
        uri /authelia/api/authz/forward-auth
        copy_headers Remote-User Remote-Groups Remote-Name Remote-Email
    }

    # ── your app handlers below ──
    reverse_proxy localhost:8080   # adapt to your upstream
}
```

Validate + reload:

```bash
caddy validate --config config/Caddyfile
# If caddy is running under your process supervisor:
./bin/ops/ant-supervisor restart caddy
```

### 5b. nginx

nginx uses `auth_request` instead of `forward_auth`. Same concept.

```nginx
server {
    listen 8501 ssl http2;
    server_name PRIMARY_HOST;

    ssl_certificate     /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    # Authelia portal — proxy the path-prefixed routes
    location /authelia/ {
        proxy_pass         http://127.0.0.1:9091/authelia/;
        proxy_set_header   Host                 $host;
        proxy_set_header   X-Real-IP            $remote_addr;
        proxy_set_header   X-Forwarded-For      $remote_addr;
        proxy_set_header   X-Forwarded-Proto    $scheme;
        proxy_set_header   X-Forwarded-Host     $host;
        proxy_set_header   X-Forwarded-Uri      $request_uri;
    }

    # Internal endpoint nginx will call via auth_request.
    location = /internal/authelia-verify {
        internal;
        proxy_pass                    http://127.0.0.1:9091/authelia/api/authz/auth-request;
        proxy_pass_request_body       off;
        proxy_set_header              Content-Length        "";
        proxy_set_header              Host                  $host;
        # REQUIRED by the auth-request endpoint — without it Authelia
        # logs "missing required X-Original-URL header" and the gate fails.
        proxy_set_header              X-Original-URL        $scheme://$host$request_uri;
        proxy_set_header              X-Original-Method     $request_method;
        proxy_set_header              X-Forwarded-Method    $request_method;
        proxy_set_header              X-Forwarded-Proto     $scheme;
        proxy_set_header              X-Forwarded-Host      $host;
        proxy_set_header              X-Forwarded-Uri       $request_uri;
        proxy_set_header              X-Forwarded-For       $remote_addr;
    }

    # All other paths: gated by Authelia.
    location / {
        auth_request /internal/authelia-verify;

        # On 401 from auth_request, redirect to the portal.
        error_page 401 = @authelia_redirect;

        # Pass authenticated user info to upstream.
        auth_request_set $auth_user   $upstream_http_remote_user;
        auth_request_set $auth_groups $upstream_http_remote_groups;
        auth_request_set $auth_email  $upstream_http_remote_email;
        proxy_set_header Remote-User    $auth_user;
        proxy_set_header Remote-Groups  $auth_groups;
        proxy_set_header Remote-Email   $auth_email;

        proxy_pass http://localhost:8080;   # your upstream
    }

    location @authelia_redirect {
        return 302 https://$host/authelia/?rd=$scheme://$host$request_uri;
    }
}

# Optional: plain-HTTP companion port that redirects to HTTPS.
server {
    listen      8500;
    server_name PRIMARY_HOST;
    return 301  https://$host:8501$request_uri;
}
```

Validate + reload:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

## 6. Start the stack (the right command)

**Use the supervisor — NOT `make serve-debug`.** `serve-debug` runs services
in the foreground for tail-tweaking and exits as soon as the terminal dies.
For a normal run (services persist across SSH disconnects, Caddy + Authelia
included, auto-restart on crash):

```bash
# From the anthive4 repo root:
nohup ./bin/ops/ant-supervisor --caddy > /tmp/ant-supervisor.log 2>&1 &
```

Add `authelia` to the supervisor's `DEFAULT_SERVICES` so it starts with the
rest (see `bin/ops/ant-supervisor`, look for `DEFAULT_SERVICES = [...]`). If
this host needs Nebula+localhost-only binding:

```bash
export ANTHIVE_CADDY_BIND="127.0.0.1 10.42.0.3"
# then start the supervisor as above
```

Stop everything:

```bash
make serve-stop
```

Restart just one service (Authelia or Caddy, e.g. after config edit):

```bash
./bin/ops/ant-supervisor restart authelia
./bin/ops/ant-supervisor restart caddy
```

The supervisor watches its children and respawns them with back-off, so
crashes self-heal. Logs go to `/tmp/ant-supervisor.log` (or whatever you
pipe to).

## 7. Verify

```bash
# Authelia is alive (direct, bypassing the proxy):
curl -fsS http://127.0.0.1:9091/authelia/api/health
# → {"status":"OK"}

# Through the reverse proxy:
curl -kfsS https://PRIMARY_HOST:8501/authelia/api/health
# → {"status":"OK"}

# Unauthenticated GET / should redirect to portal:
curl -ksS -o /dev/null -w "%{http_code}  %{redirect_url}\n" \
     https://PRIMARY_HOST:8501/
# → 302  https://PRIMARY_HOST:8501/authelia/?rd=...

# Full login round-trip:
COOKIE_JAR=/tmp/auth-test.txt
rm -f "$COOKIE_JAR"
curl -ksS -c "$COOKIE_JAR" -b "$COOKIE_JAR" \
     -X POST -H "Content-Type: application/json" \
     -d '{"username":"USERNAME","password":"PASSWORD","keepMeLoggedIn":false,"targetURL":"https://PRIMARY_HOST:8501/"}' \
     https://PRIMARY_HOST:8501/authelia/api/firstfactor
# → {"status":"OK","data":{"redirect":"https://PRIMARY_HOST:8501/"}}

# Authenticated request now passes through:
curl -ksS -b "$COOKIE_JAR" -o /dev/null -w "%{http_code}\n" \
     https://PRIMARY_HOST:8501/
# → 200
```

## 8. Common gotchas

- **`localhost` rejected as cookie domain.** Use `localhost.localdomain` or
  an IP. Authelia v4.39 validates: "must have at least a single period or
  be an ip address".
- **HTTPS hard-required for cookie URLs.** No flag to bypass. Either real
  certs or `tls internal`.
- **`auto_https off` (Caddy) breaks `tls internal`.** Use
  `auto_https disable_redirects` — keeps cert provisioning, skips :80.
- **Cert warnings on first visit.** Browsers don't trust Caddy's internal
  CA out of the box. Either accept once, or run `caddy trust` on the
  server to install the CA into the OS trust store + sync to clients.
- **Caddy `tls internal` needs the hostnames enumerated in the site
  address** so it knows which certs to issue. `:8501` alone won't work
  — use `host1:8501, host2:8501 {…}`.
- **Migrating off an existing HTTP basic-auth gate? Make the browser
  endpoint cookie-only or you will ban every existing user.** Browsers
  cache basic-auth creds and keep sending `Authorization: Basic <old>`
  on every request to the same host. nginx forwards that to Authelia's
  default `auth-request` endpoint, which validates it as basic-auth,
  fails (old password ≠ Authelia password), and after `max_retries`
  trips regulation and bans the user. Fix: give the browser gate a
  cookie-only strategy so stale headers are ignored (clean 401 →
  portal redirect, no failed-login, no ban). Keep header/basic auth
  on a *separate* endpoint used only by your API/CLI route:

  ```yaml
  server:
    address: 'tcp://127.0.0.1:9091/authelia'
    endpoints:
      authz:
        auth-request:                # browser gate — cookie only
          implementation: 'AuthRequest'
          authn_strategies:
            - name: 'CookieSession'
        auth-request-api:            # API/CLI gate — cookie first, then basic
          implementation: 'AuthRequest'
          authn_strategies:
            - name: 'CookieSession'
            - name: 'HeaderAuthorization'
              schemes: ['basic']
  ```

  Point the API location at the second endpoint:
  `auth_request /internal/authelia-verify-api;` →
  `proxy_pass …/authelia/api/authz/auth-request-api;` and forward the
  `Authorization` header (`proxy_set_header Authorization $http_authorization;`).
  Cookie-first means a logged-in browser's same-origin XHR uses its
  session and ignores any stale header, while a CLI client with no
  cookie authenticates by basic. To unban during testing:
  `DELETE FROM banned_user; DELETE FROM banned_ip;` in
  `config/authelia/db.sqlite3` (the new ban tables; no sqlite3 binary?
  use `python3 -c "import sqlite3; …"`).

- **Each host gets its own secrets.** Sharing a secret = sharing the
  ability to sign sessions on the other host. Always regenerate.
- **The `env VAR=…` trick in the supervisor cmd** is how the secret-file
  paths get to Authelia without modifying the supervisor's own env. The
  three vars are: `AUTHELIA_SESSION_SECRET_FILE`,
  `AUTHELIA_STORAGE_ENCRYPTION_KEY_FILE`, and
  `AUTHELIA_IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET_FILE`.

## 9. Where to look when it breaks

- `/tmp/ant-supervisor.log` — multiplexed stdout of all supervised services
- `./tmp/authelia.log` — Authelia's own log (set in `configuration.yml`)
- `./tmp/caddy.log` — Caddy access log
- `./config/authelia/notification.txt` — password-reset emails (filesystem
  notifier writes here instead of SMTP)
