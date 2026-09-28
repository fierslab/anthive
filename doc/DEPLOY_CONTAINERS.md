# Deploy an anthive site

This is the one supported way to run anthive: a docker compose stack,
described by one file (`site.yaml`) and driven by one tool
(`bin/ops/ant-stack`). Follow the steps in order. Every command below was run
end to end on a fresh checkout for release 2.37.2.

Plan about an hour, plus the time it takes to download the data.

**What you end up with:** a web site at an address you choose, with a login
page, the dataset browser, the REST API that vark and pyvark use, and personal
MCP links for AI clients. The data stays on the server's disk; the software
comes as two published images.

---

## 1. Before you start

Tick every box. Most failed installs skip one of these.

**The server**

- [ ] Linux on x86_64 (amd64). The images are not built for ARM.
- [ ] Docker Engine with the compose plugin. `docker compose version` must
      print v2 or newer. Install guide: https://docs.docker.com/engine/install/
- [ ] Your user may run docker without sudo: `docker ps` works. If not:
      `sudo usermod -aG docker $USER`, then log out and back in.
- [ ] `git` is installed.
- [ ] `uv` is installed (`ant-stack` runs with it):
      `curl -LsSf https://astral.sh/uv/install.sh | sh`, then open a new shell
      and check `uv --version`.
- [ ] Disk: about 10 GB for the images, plus the data you pull. The complete
      catalogue is about 1.7 TB (roughly 0.8 TB the server reads to serve,
      1 TB of raw `.h5ad`). Start with one or two collections; see step 5.
- [ ] A free port for the web site (the example uses 8443).

**An address users will type**

- [ ] A host name that has a dot in it (`anthive.mylab.org`) or an IP
      address (`10.33.1.20`). **Plain `localhost` does not work** with the
      login portal; `ant-stack check` refuses it. For a trial on your own
      machine use `https://127.0.0.1:8443`.

**Access you need to ask for** (ask the anthive maintainers)

- [ ] Read access to the catalogue repo `h5adstore`
      (`gitlab.kuleuven.be/bdslab/anthive/h5adstore` or `github.com/badslab/h5adstore`).
      It holds the dataset descriptions (`.md`, `.h5ad.mus`), not the data.
- [ ] Read access to the gene-set repo
      (`gitlab.kuleuven.be/bdslab/genesetdbs/anthive-genesetdb`). Optional:
      without it the gene-set pages are empty.
- [ ] S3 keys for the data bucket (an access key id and a secret). Without
      them you can still run a site, but only with data you convert yourself.

The software itself needs no access: the code and the images are public.

---

## 2. Get the code

Pick a base folder; this guide uses `/srv/anthive`.

```sh
sudo mkdir -p /srv/anthive && sudo chown $USER /srv/anthive
cd /srv/anthive
git clone https://gitlab.kuleuven.be/bdslab/anthive/anthive4.git
cd anthive4
git checkout v2.37.2
```

Always check out a release tag, and use the same number as the image tag in
step 4. The checkout only provides `ant-stack` and its templates; the
programs themselves run from the images.

---

## 3. Get the catalogue and gene sets

```sh
cd /srv/anthive
git clone git@gitlab.kuleuven.be:bdslab/anthive/h5adstore.git
git clone git@gitlab.kuleuven.be:bdslab/genesetdbs/anthive-genesetdb.git genesetstore
```

`h5adstore/` now contains one folder per collection with small text files.
The actual data (`.h5ad`, `.duckdb`, parquet) is downloaded in step 5.

---

## 4. Configure the site

```sh
cd /srv/anthive/anthive4
bin/ops/ant-stack init
```

The first `init` only writes `site.yaml` (a copy of `site.example.yaml`).
Open it and change these lines. Everything else can stay as it is.

```yaml
site:
  name: mylab-anthive                         # letters, digits, dashes
  public_url: https://anthive.mylab.org:8443  # what users type (see step 1)

data:
  h5adstore: /srv/anthive/h5adstore           # absolute paths
  genesetstore: /srv/anthive/genesetstore     # or null if you have no access

state_dir: /srv/anthive/state                 # secrets, users, caches: back this up

auth:
  mode: authelia                              # login page; see "Login modes" below
  admins: [admin]
  users:
    - name: admin
      displayname: Administrator
      email: you@mylab.org
      groups: [admins]
    - name: jdoe                              # one entry per person
      displayname: Jane Doe
      email: jane@mylab.org
      groups: []

ports:
  https: 8443                                 # the port in public_url

images:
  registry: registry.gitlab.kuleuven.be/bdslab/anthive/anthive4
  tag: 2.37.2                                 # same number as the git tag

s3:                                           # skip if you have no S3 keys
  enabled: true
  endpoint: fsn1.your-objectstorage.com
  region: eu-central
  bucket: anthive4a
```

Then run `init` a second time. It creates `state_dir`, all secrets and a
password for every user:

```sh
bin/ops/ant-stack init
```

The passwords are printed once and kept in
`/srv/anthive/state/secrets/initial-passwords.txt`.

If you enabled S3, put the two keys in these files (one line each, nothing
else) and lock them down:

```sh
nano /srv/anthive/state/secrets/s3_access_key_id
nano /srv/anthive/state/secrets/s3_secret_access_key
chmod 600 /srv/anthive/state/secrets/s3_*
```

Now let `check` look for mistakes:

```sh
bin/ops/ant-stack check
```

It prints the resolved plan and ends with `OK: ready for ant-stack up`, or it
lists each `PROBLEM` with the fix. Repeat until it says OK. A `WARNING` about
datasets without a local `.duckdb` is expected at this point; step 5 fixes it.

---

## 5. Download the images and the data

```sh
bin/ops/ant-stack pull
```

This fetches the app, Caddy and Authelia images (about 2 GB). The ingest
image (about 4.5 GB) is downloaded the first time you use it below.

Download the data through the ingest container. Start with one collection to
see that everything works:

```sh
bin/ops/ant-stack ingest -- ant sync pull --include-duckdb 'Lloyd2024/*'
```

Then pull what you want to serve: more collections by name, or everything:

```sh
bin/ops/ant-stack ingest -- ant sync pull --include-duckdb 'Sierksma2025/*'
bin/ops/ant-stack ingest -- ant sync pull --include-duckdb     # the whole catalogue, ~1.7 TB
```

`--include-duckdb` matters: without it only the raw `.h5ad` files come down
and the site has nothing it can serve quickly. A dataset whose files are not
local is still listed; it is read from S3 over the network, which is slow.
Running the same pull again only downloads what changed.

---

## 6. Start it

```sh
bin/ops/ant-stack up
```

The first start takes a few minutes: the API scans the catalogue before it
reports healthy, and `up` waits for that. It ends with
`up: https://anthive.mylab.org:8443`.

Open that address. With the default `tls.mode: internal` the browser warns
about the certificate the first time, because the site signs its own; see
"Certificates" below to make the warning go away. Log in as `admin` with the
password from step 4, then change it:

```sh
bin/ops/ant-stack passwd admin
```

---

## 7. Check that it works

- [ ] `bin/ops/ant-stack ps` shows `api`, `web`, `mcp`, `authelia` as
      `healthy`, `caddy` as up, and `static` as exited (it only copies files
      at start).
- [ ] The address shows the login page when you are logged out.
- [ ] After login, the dataset list shows the collections you pulled, and a
      dataset page opens with its plots.
- [ ] Your account menu has "Connect an AI client (MCP)"; creating a link
      there works.
- [ ] From another machine, the API answers with a login:
      `curl -u jdoe:PASSWORD https://anthive.mylab.org:8443/api/health`

If one of these fails, see Troubleshooting at the end.

---

## Everyday tasks

Run these from `/srv/anthive/anthive4`.

| I want to... | Do this |
| --- | --- |
| add a person | add them under `auth.users` in `site.yaml`, run `bin/ops/ant-stack init` (prints their password), give it to them. Works without a restart. |
| reset a password | `bin/ops/ant-stack passwd <name>` |
| make someone admin | add them to `auth.admins` and give them `groups: [admins]`, then `bin/ops/ant-stack init` |
| get new datasets | `cd ../h5adstore && git pull && cd ../anthive4`, then `bin/ops/ant-stack ingest -- ant sync pull --include-duckdb 'Collection/*'`, then `bin/ops/ant-stack restart api` |
| see what runs | `bin/ops/ant-stack ps` |
| read logs | `bin/ops/ant-stack logs api` (or `web`, `mcp`, `caddy`, `authelia`) |
| restart one part | `bin/ops/ant-stack restart api` |
| stop everything | `bin/ops/ant-stack down` (keeps data, users and settings) |
| start again | `bin/ops/ant-stack up` |
| convert my own .h5ad | put it in a collection folder with its `.md`, then `bin/ops/ant-stack ingest -- ant convert /srv/anthive/h5adstore/MyStudy/file.h5ad` |

### Upgrade to a new release

```sh
cd /srv/anthive/anthive4
git fetch --tags
git checkout vX.Y.Z             # the new release
nano site.yaml                  # images: tag: X.Y.Z
bin/ops/ant-stack pull
bin/ops/ant-stack up
```

Users, passwords, MCP links and caches live in `state_dir` and carry over.
To go back, check out the old tag, put the old image tag back and run the
same two commands.

### Back up

Back up `state_dir` (`/srv/anthive/state`). It holds everything that is not
in the images or the data: secrets, the user database, MCP links, caches. It
is small. Copy it while the stack is stopped (`ant-stack down`), or copy the
SQLite files inside it with `sqlite3 <file> ".backup <copy>"`.

The data itself is already backed up: the files come from S3 and the
catalogue from git.

---

## Troubleshooting

| What you see | Why | Fix |
| --- | --- | --- |
| `ant-stack: command not found` or `uv: No such file` | uv is missing or not on PATH | install uv (step 1), open a new shell |
| `permission denied ... docker.sock` | your user may not use docker | `sudo usermod -aG docker $USER`, log out and in |
| `check`: public_url host ... refused by Authelia | host without a dot, e.g. `localhost` | use a real host name or an IP address |
| `check`: s3.enabled ... missing or empty | S3 key files not filled | step 4, the two `nano` lines |
| `check`: port already in use | something else listens there | pick another `ports.https` and change `public_url` to match |
| `check`: store not readable by uid | the data folder belongs to someone else | `sudo chown -R $USER /srv/anthive`, or set `run_as` |
| browser: "connection is not private" | the site signs its own certificate | continue once, or trust it (see Certificates), or use `tls.mode: acme` |
| `up` waits a long time, then "unhealthy" | the API is still scanning a large, mostly remote catalogue | pull the data you serve (step 5), run `up` again; `logs api` shows progress |
| 502 right after `up --no-wait` | the API is still starting | wait a minute |
| logged in but admin pages say 403 | the user is not an admin | add to `auth.admins` and `groups: [admins]`, `ant-stack init` |
| a dataset is listed but its page is very slow | its files are not on this disk | pull that collection with `--include-duckdb` |
| anything else | | `bin/ops/ant-stack logs <service>`; the first error line usually names the problem |

---

## Reference

### Login modes (`auth.mode`)

| Mode | Use it when | How people log in |
| --- | --- | --- |
| `authelia` (default) | a normal site | a login page; scripts and vark/pyvark use the same name and password over HTTP basic on `/api` |
| `basic` | a small group or automation only | the browser's own password popup; logging out means closing the browser |
| `trusted-proxy` | your institute's web server already does the login (e.g. university SSO in front) | the upstream proxy; the site must only listen on 127.0.0.1 (`check` enforces it) and use `tls.mode: off` |
| `none` | a public read-only mirror | nobody logs in; admin features are switched off |

Only the Authelia container is extra; it only starts in `authelia` mode.
Personal MCP links work the same in every mode.

### Certificates (`tls.mode`)

- `internal` (default): the site creates its own certificate authority.
  Browsers warn until you trust it. To trust it, copy the root certificate
  out and install it on the computers that use the site:
  `docker cp <site.name>-caddy-1:/data/caddy/pki/authorities/local/root.crt anthive-root.crt`
- `acme`: a free Let's Encrypt certificate, no warnings. The host name must
  be reachable from the internet on ports 80 and 443, and you set
  `tls.acme_email`.
- `off`: plain HTTP on `ports.http`, only when another web server in front
  of anthive handles HTTPS.

### Published images

| Image | Contents |
| --- | --- |
| `registry.gitlab.kuleuven.be/bdslab/anthive/anthive4/anthive-app` | every service (API, web, MCP) in one image, ~1.7 GB |
| `registry.gitlab.kuleuven.be/bdslab/anthive/anthive4/anthive-ingest` | the app plus the R/Seurat tools for converting data, ~4.5 GB |

Tags are release numbers (`2.37.2`) plus `latest`. Always pin a release
number in `site.yaml`; `latest` changes under you. The images are public
(no login needed to pull), linux/amd64 only, and contain no data,
configuration or secrets: each image is checked against the git tree at
build time (`docker/check-image`).

### What runs where

```
                 ┌──────────────── server ───────────────┐
 browser ──TLS──▶│ caddy ──┬── /api/*    ──▶ api   (ant-serve) ── h5adstore (read-write)
 AI client       │         ├── /mcp*     ──▶ mcp   (ant-mcp)
                 │         ├── /authelia ──▶ authelia (login, optional) ── state_dir
                 │         └── everything else ─▶ web (ant-browse)
                 │  ingest: one-off `ant-stack ingest -- ...` ── h5adstore, scratch
                 └────────────────────────────────────────┘
```

Each part is its own container that restarts on its own if it crashes.
The data folders are mounted at the same absolute path inside the containers
as on the server, because each `.duckdb` refers to its parquet files by
absolute path.

### site.yaml keys

`site.example.yaml` documents every key. The main ones:

| Key | Decides |
| --- | --- |
| `site.public_url` | the address; used for cookies, redirects and MCP links |
| `site.hostnames` | extra names or IPs the site answers to |
| `data.h5adstore`, `data.genesetstore` | the data folders (absolute paths) |
| `data.readonly` | mount the data read-only (a mirror that never annotates) |
| `state_dir` | secrets, users, caches and the rendered config |
| `scratch_dir` | disk for temporary DuckDB files and conversions (default: a docker volume) |
| `auth.*`, `tls.*`, `ports.*` | see above |
| `run_as` | the user id the containers run as (default: whoever runs ant-stack) |
| `images.*` | registry, image names, tag |
| `services.mcp`, `services.chat` | optional parts |
| `resources.*` | threads, memory, open-dataset limit, plot cache, catalogue scan |

`ant-stack render` (run by `up`) writes everything derived from `site.yaml`
to `<state_dir>/stack/`. Do not edit those files; they are rewritten on
every `up`.

For command-line ingest on a cluster, see [HPC.md](HPC.md). For building and
publishing releases, see [RELEASING.md](RELEASING.md). Image internals are in
[DOCKER.md](DOCKER.md).
