# Deploy an anthive site

This is the one supported way to run anthive: a docker compose stack,
described by one file (`site.yaml`) and driven by one tool
(`bin/ops/ant-stack`). Follow the steps in order.

Every command below was run end to end on a fresh checkout for release 2.46.5.

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
- [ ] Enough memory, CPU and disk; see "Hardware" below.
- [ ] A free port for the web site (the example uses 8443), open in the
      firewall; see "Network" below.

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
- [ ] Read access to the paper library repo
      (`gitlab.kuleuven.be/bdslab/anthive/library_store`, private; it holds
      the paper records, the PDFs come from S3). Optional: without it the
      site has no papers, literature search or paper context for AI clients.
- [ ] S3 keys for the data bucket (an access key id and a secret). Without
      them you can still run a site, but only with data you convert yourself.
- [ ] For the three repos above: an SSH key of this server's user on your
      `gitlab.kuleuven.be` account (Preferences > SSH Keys; check with
      `ssh -T git@gitlab.kuleuven.be`), because step 3 clones over SSH. Without
      one, clone over HTTPS with a personal or project access token
      (`read_repository`) instead; step 3 shows both.

The software itself needs no access: the code and the images are public.

### Where the data comes from

There is no public download of the Anthive data. Everything a site serves
comes from the lab that maintains Anthive:

- the dataset descriptions (`h5adstore`), the gene sets (`anthive-genesetdb`)
  and the paper records (`library_store`) are private git repositories on
  `gitlab.kuleuven.be`;
- the data files (`.h5ad`, `.duckdb`, parquet) and the paper PDFs are in a
  private S3 bucket, reachable only with keys the maintainers hand out.

Ask the Anthive maintainers for access through the GitLab project
(https://gitlab.kuleuven.be/bdslab/anthive/anthive4, open an issue). Say
which collections you need; you get GitLab access to the repositories and
an S3 key pair, plus the endpoint, region and bucket name for step 4.

Datasets marked `access: public` are only public in the sense that a site
may show them to visitors who are not signed in (see "Internal datasets");
their files still come through the maintainers. `ant sync` has no
"public only" mode and always needs S3 keys.

**Without access.** You can install and start everything. From 2.46.0 the site
also comes up with a completely empty store (`/api/health` answers
`databases: 0, note: "empty catalog"`); release 2.45.1 did not: its api
reported itself unhealthy without any dataset, and `ant-stack up` stopped
with `container ...-api-1 is unhealthy` before the web site started. To try
the software with some data, put at least one dataset of your own in the store (an `.h5ad` with log-normalised `X` plus
its `.md`, converted with `ant convert`; see the table in "Everyday tasks"
and `doc/DATASET_INGESTION.md`). Keep `genesetstore`, `library` and
`library_index` at `null` and `s3.enabled: false`. The site then shows the
login page and your dataset, with empty gene set and paper pages.

### Hardware

Guidance, not measured limits. The numbers come from the settings and the
current catalogue; your load decides the rest.

| | Trial (one or two collections) | Full catalogue |
| --- | --- | --- |
| CPU | 4 cores | 16 cores or more |
| RAM | 16 GB | 64 GB or more |
| Disk for data | 20 to 50 GB | about 2 TB |
| Disk for images and scratch | 20 GB | 100 GB or more |

Where the numbers come from:

- **Memory.** Each dataset that is in use gets its own DuckDB instance.
  `resources.server_memory_limit: auto` lets one instance use up to 25% of
  the machine's RAM, capped at 16 GB, and up to `max_open_databases` (48)
  instances stay open. The cap is a ceiling, not a reservation: a few
  people browsing use far less. On top of that the facet caches of the api
  take above 1 GB each for signed-in and for anonymous visitors on a full
  store (see "Cache warm-up after a start"). On a small machine set
  `server_memory_limit` to a fixed value (for example `4GB`) and lower
  `max_open_databases` in `site.yaml` under `resources:`.
- **CPU.** `resources.server_threads: 8` DuckDB threads per open dataset;
  plots and facet tests are CPU-bound. More cores mostly help with several
  users at once.
- **Data disk.** Per dataset, the files the server reads (`.duckdb` plus
  parquet) are typically 0.3 GB, 90% are under 5 GB and the largest is
  about 130 GB. The raw `.h5ad` that `ant sync pull` also brings is
  typically 0.7 GB, at most about 150 GB. The whole catalogue is about
  0.8 TB to serve plus 1.1 TB of `.h5ad`. Use local SSD: dataset pages read
  many small pieces of these files.
- **Images and scratch.** The images take about 6.5 GB (app 1.7 GB, ingest
  4.5 GB, Caddy and Authelia small), under `/var/lib/docker` by default.
  DuckDB spills large queries and `ant convert` stages its work on the
  scratch disk (`scratch_dir`, by default a docker volume, also under
  `/var/lib/docker`); a conversion needs room for about the size of the
  `.h5ad` it converts. The api's facet cache there takes about 1.5 GB per
  kind of visitor on a full store (see "Cache warm-up after a start"). `state_dir` stays small (users, links, caches).

### Network

**Ports to open** in the server's firewall and in any network firewall in
front of it, for the people who will use the site:

| `tls.mode` | Open | Why |
| --- | --- | --- |
| `internal` (default) | `ports.https` (8443 in the example), TCP | the only published port |
| `acme` | 80 and 443, TCP, from the internet | Let's Encrypt checks the name on port 80; users come in on 443 |
| `off` | nothing extra | the reverse proxy in front reaches `ports.http` on 127.0.0.1 |

`ports.bind` (default `0.0.0.0`) is the host address the port listens on.
Docker publishes ports through its own firewall rules, which **bypass ufw
and firewalld**: a port that docker publishes is reachable even when ufw
says it is closed. To keep the site inside a network, set `ports.bind` to
an internal address of the server, or filter in the `DOCKER-USER` iptables
chain, or rely on a firewall outside the server.

**Outgoing connections** the server needs: `registry.gitlab.kuleuven.be`
and Docker Hub (images, step 5), `gitlab.kuleuven.be` on port 22 (SSH) or
443 (HTTPS) for the repositories, the S3 endpoint on 443, and for
`tls.mode: acme` Let's Encrypt.

**The name.** Pick the name users will type and point it at the server:
an `A` record (IPv4, and `AAAA` for IPv6) in your DNS. For `tls.mode: acme`
the name must be in public DNS and reach this server from the internet. For
an intranet site an internal DNS name is fine; an IP address also works,
but then everyone types the number. Extra names or addresses go in
`site.hostnames`. `site.public_url` must use exactly the name and port
people type, or the login fails.

**AI clients.** Terminal agents on a user's computer (Claude Code, Codex,
Gemini CLI) reach the MCP link from that computer, so a site inside a
network works for them. Web-based clients that add MCP connectors in their
own cloud (for example claude.ai connectors) call the link from the
provider's servers, which only works when the site is reachable from the
internet.

---

## 2. Get the code

Pick a base folder; this guide uses `/srv/anthive`.

```sh
sudo mkdir -p /srv/anthive && sudo chown $USER /srv/anthive
cd /srv/anthive
git clone https://gitlab.kuleuven.be/bdslab/anthive/anthive4.git
cd anthive4
git checkout v2.46.5
```

Always check out a release tag, and use the same number as the image tag in
step 4. The checkout only provides `ant-stack` and its templates; the
programs themselves run from the images.

---

## 3. Get the catalogue, gene sets and paper library

This step needs the access from step 1. Without it, make an empty folder
instead (`mkdir -p /srv/anthive/h5adstore`) and skip the clones. Since
2.46.0 the site starts with an empty store; `check` then says "no datasets
yet". Add a dataset of your own later ("Without access" in step 1).

```sh
cd /srv/anthive
git clone git@gitlab.kuleuven.be:bdslab/anthive/h5adstore.git
git clone git@gitlab.kuleuven.be:bdslab/genesetdbs/anthive-genesetdb.git genesetstore
git clone git@gitlab.kuleuven.be:bdslab/anthive/library_store.git library          # optional, see step 1
mkdir -p library_index                                                            # only with the library
```

These SSH clones need the SSH key from step 1 (`ssh -T git@gitlab.kuleuven.be`
greets you by name when it works). Without a key, use HTTPS with an access
token; git asks for a user name (your GitLab user name) and a password (the token):

```sh
git clone https://gitlab.kuleuven.be/bdslab/anthive/h5adstore.git
git clone https://gitlab.kuleuven.be/bdslab/genesetdbs/anthive-genesetdb.git genesetstore
git clone https://gitlab.kuleuven.be/bdslab/anthive/library_store.git library    # optional
```

`h5adstore/` now contains one folder per collection with small text files.
`library/papers/` holds one `.md` record per paper (`doc/LIBRARY.md`).
The actual data (`.h5ad`, `.duckdb`, parquet), the paper PDFs and the
precomputed literature index are downloaded in step 5.

The library repository (`bdslab/anthive/library_store`) is private: ask the
maintainers for read access. It holds only the paper records (`.md`); the
PDFs come from S3 in step 5. Without access, skip the library clone and the
`mkdir library_index`, and keep `library: null` and `library_index: null` in
`site.yaml` in step 4: the site runs without papers.

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
  library: /srv/anthive/library               # the paper library checkout, or null
  library_index: /srv/anthive/library_index   # literature index (filled in step 5), or null

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
  tag: 2.46.5                                 # same number as the git tag (step 2)

s3:                                           # skip if you have no S3 keys
  enabled: true
  endpoint: s3.example.org                    # host name only, no https://
  region: example-region
  bucket: example-bucket
```

The S3 values above are placeholders. The real endpoint, region and bucket
of the lab's data come from the maintainers together with the keys (see
"Where the data comes from").

`images.tag` must be the release you checked out in step 2 (`git describe
--tags` prints it). `site.example.yaml` carries the number of its own release,
so a fresh `site.yaml` starts right; when you upgrade, change it by hand.

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

These keys only reach the ingest container (`ant-stack ingest`, used for
downloads in step 5) and `ant-stack publish-library`. The running site (api,
web, mcp) gets no S3 keys at all, so it serves only data that is on this
disk. If the site should also read datasets you have not downloaded, hand
out downloads and PDFs that are only on S3, give the api a read-only key
pair of its own:

```yaml
s3:
  api_access: read      # none (default) | read | write
```

```sh
nano /srv/anthive/state/secrets/s3_read_access_key_id
nano /srv/anthive/state/secrets/s3_read_secret_access_key
chmod 600 /srv/anthive/state/secrets/s3_read_*
```

Ask whoever runs the bucket for a key that can only list and read.
`write` hands the api the read-write keys and is only needed for
`library.s3_autopush`, which you can leave off (see "Let people add
papers").

Now let `check` look for mistakes:

```sh
bin/ops/ant-stack check
```

It prints the resolved plan and ends with `OK: ready for ant-stack up`, or it
lists each `PROBLEM` with the fix. Repeat until the only problems left are the
two `image ... not present locally` lines (on a host that never had the images;
step 5's `pull` fixes them). A `WARNING` about datasets without a local
`.duckdb` is expected at this point; step 5 fixes it too.

`check` also compares the keys of `site.yaml` with `site.example.yaml`. A key it
does not know under `auth`, `tls`, `ports`, `s3`, `services`, `datasets` or
`data` is a `PROBLEM` (a misspelt `auth: mode` would otherwise be ignored and the
default used); anywhere else it is a `WARNING`. Both name the closest known key.

---

## 5. Download the images and the data

```sh
bin/ops/ant-stack pull
```

This fetches the app, ingest, Caddy and Authelia images (about 6.5 GB, of
which the ingest image is 4.5 GB) by the tag in `site.yaml` and ends with a
table of what it pulled and whether each image was new, updated or
unchanged. `--no-ingest` skips the ingest image, which you need for the
downloads below and for `publish-library`.

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
and the site has nothing it can serve quickly.

**A site with `sync_policy.mode: priority`** (set in the `anthive:` block
of `site.yaml`, usually with `role: slave`, `max_local_gb` or
`min_free_space`, for a disk smaller than the catalogue) pulls with plain
`bin/ops/ant-stack ingest -- ant sync pull` (add `--evict` to drop
datasets below the cutoff). It keeps the highest-priority datasets, each
as its `.duckdb` with its layer, DE and score parquets (on a primary, local
sidecars and shared files that differ from S3 are kept, never overwritten), plus the shared
files every site needs (`gene_stats_index.parquet`,
`module_variability.parquet` and the other root indexes, `_facet_tests/`,
the `_*_cache/` folders, `static/`), and never a raw `.h5ad`. There
`--include-duckdb` has no effect (before 2.46.1 it switched the policy
off and pulled every `.h5ad`; do not use it on such a site with an older
release). Try any pull first with `--dry-run`: it lists what would be
downloaded or evicted and changes nothing. Only catalogued datasets come
down: those with a `.md` in your h5adstore checkout, without a `.inactive`
sidecar and not in a `_` folder (since 2.46.5). Objects on S3 without their
`.md` (retired datasets) are listed as "on S3 but not catalogued (retired?)"
with their sizes, never pulled and never deleted from S3. A glob such as
`'Francken2026/*'` limits the run to the datasets and shared files it
matches (the budget is still planned over the whole catalogue). A file
whose S3 ETag shows the same content is never downloaded again, only its
mtime is realigned; a file that does differ is listed with the reason
before it is overwritten. A dataset whose files are not
local is only listed with `s3.api_access: read` (step 4); it is then read
from S3 over the network, which is slow. Running the same pull again only
downloads what changed.

Every `ant sync pull` also brings the paper PDFs (into the library checkout)
and the literature index (`library_chunks.parquet`, about 10 MB). To fetch
only those, for example after the library gained papers:

```sh
bin/ops/ant-stack ingest -- ant sync pull --library-only
```

`--no-library` does the opposite (data only). The library part runs in
both directions (`ant sync library`): new records come from git, new and
changed PDFs go both ways, and nothing is deleted unless you add
`--propagate-deletes` (`doc/LIBRARY.md`).

Where the library lives:

| What | Where | Synced by |
| --- | --- | --- |
| paper records (`.md`) | the library git repo | `git` (pulled and pushed by `ant sync library`) |
| paper PDFs | S3, under `_library/papers/<folder>/` in the same bucket and prefix as the data | `ant sync` (both directions) |
| literature index (`library_chunks.parquet` + `.json`) | S3, under `_library_index/` | `ant sync` (downloaded by every site with S3 unless `library.index_pull: false`; uploaded only by the site with `library_index_publisher: true`) |

The leading `_` keeps these folders out of the dataset sync. A site that
adds papers updates its own index locally after each sync, so it does not
need the published copy to be current.

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

It asks for the new password twice (at least 10 characters). In a script,
where there is no terminal to ask on, pass it with `--password '...'` or in
the environment variable `ANTHIVE_NEW_PASSWORD`. With `auth.mode: authelia`
the new password works within seconds; with `auth.mode: basic` run
`bin/ops/ant-stack restart caddy` afterwards.

---

## 7. Check that it works

- [ ] `bin/ops/ant-stack ps` shows `api`, `web`, `mcp`, `authelia` as
      `healthy`, `caddy` as up, and `static` as exited (it only copies files
      at start).
- [ ] The address shows the login page when you are logged out.
- [ ] After login, the dataset list shows the collections you pulled, and a
      dataset page opens with its plots.
- [ ] Your account menu has "Connect an MCP client" ("MCP links" once you
      have one); creating a link there works.
- [ ] From another machine, the API answers with a login:
      `curl -u jdoe:PASSWORD https://anthive.mylab.org:8443/api/health`.
      With `tls.mode: internal` curl does not know the site's certificate
      authority: copy `anthive-root.crt` out as in "Certificates" and add
      `--cacert anthive-root.crt`, or use `-k` (skips the check) for a quick
      trial.

If one of these fails, see Troubleshooting at the end.

---

## 8. After the first login

- **Users of the site** connect their AI app and install the Anthive skills
  with the client install guide:
  https://gitlab.kuleuven.be/bdslab/anthive/skills/-/blob/main/INSTALL.md
  Send them that link together with the site address and their password.
  The site's own "Connect an MCP client" page also gives each user a
  setup prompt to paste into their AI agent.
- **Admins** can let an AI agent with a shell walk them through this guide
  and through everyday upkeep with the `anthive-server-setup` skill
  (install it as in the client guide above; it reads this guide live).
- Add the other people who will use the site; see "Users and groups".

---

## Everyday tasks

Run these from `/srv/anthive/anthive4`.

| I want to... | Do this |
| --- | --- |
| add a person | add them under `auth.users` in `site.yaml`, run `bin/ops/ant-stack init` (prints their password), give it to them. Authelia mode: works without a restart; basic mode: then `restart caddy`. See "Users and groups". |
| reset a password | `bin/ops/ant-stack passwd <name>` (basic mode: then `restart caddy`) |
| make someone admin | see "Users and groups": a new user gets `groups: [admins]` in `site.yaml`; an existing one is changed in the Authelia user file |
| get new datasets | `cd ../h5adstore && git pull && cd ../anthive4`, then `bin/ops/ant-stack ingest -- ant sync pull --include-duckdb 'Collection/*'` (a `sync_policy.mode: priority` site: `ant sync pull` without a filter or `--include-duckdb`); the pull asks the api to rescan, else `bin/ops/ant-stack restart api` |
| see what runs | `bin/ops/ant-stack ps` |
| read logs | `bin/ops/ant-stack logs api` (or `web`, `mcp`, `caddy`, `authelia`) |
| restart one part | `bin/ops/ant-stack restart api` (several: `restart api web mcp`) |
| change a setting | edit `site.yaml`, then `bin/ops/ant-stack check` and `bin/ops/ant-stack up` (see "After editing site.yaml") |
| stop everything | `bin/ops/ant-stack down` (keeps data, users and settings) |
| start again | `bin/ops/ant-stack up` |
| convert my own .h5ad | put it in a collection folder with its `.md`, then `bin/ops/ant-stack ingest -- ant convert /srv/anthive/h5adstore/MyStudy/file.h5ad`, then `bin/ops/ant-stack restart api` (or, as an admin, `POST /api/admin/rescan`) so the api lists it |
| publish uploaded papers | `bin/ops/ant-stack publish-library` (better: from a timer, see "Let people add papers"; S3 keys only on the host: `--s3-host-config <path>`) |

### Users and groups

People and their groups are listed under `auth.users` in `site.yaml`:

```yaml
auth:
  users:
    - name: jdoe                  # the login name; basic mode allows letters, digits, . _ @ -
      displayname: Jane Doe
      email: jane@mylab.org
      groups: [librarians]        # [] for an ordinary user
```

**Add a person.** Add an entry like the one above and run
`bin/ops/ant-stack init`. It creates a password for every user who has none,
prints it once and appends it to
`<state_dir>/secrets/initial-passwords.txt`. Hand the password over
yourself; change it later with `bin/ops/ant-stack passwd <name>`.

- `auth.mode: authelia`: `init` adds the new user, with the groups from
  `site.yaml`, to Authelia's user file `<state_dir>/authelia/users_database.yml`.
  Authelia reloads that file by itself; no restart.
- `auth.mode: basic`: run `bin/ops/ant-stack restart caddy` after `init`, so
  Caddy reads the new password list.

**Change the groups of someone who already exists** (authelia mode).
`init` writes a user to Authelia's file only once, when the user is new;
after that the file belongs to Authelia, and editing `groups:` in
`site.yaml` for an existing user changes nothing. Edit the file instead:

```sh
nano /srv/anthive/state/authelia/users_database.yml
```

```yaml
users:
  jdoe:
    displayname: Jane Doe
    email: jane@mylab.org
    groups:
      - librarians
      - curators
    password: $2b$12$...          # leave as it is
    disabled: false
```

Keep `site.yaml` in step with it, so the two do not drift apart. Authelia
picks up the change within a few minutes, at the next login at the latest.
To lock someone out, set `disabled: true` there.

**The groups that mean something**:

| Group | Gives |
| --- | --- |
| `admins` | admin pages, rescans, everything; in basic mode also everyone in `auth.admins` |
| any group in `library.upload_groups` (for example `librarians`) | adding papers, when `services.paper_upload` is on (see "Let people add papers") |
| `curators` | with dataset deposits on (since 2.46.0), the default `datasets.upload_groups` is `[admins, curators]` (see "Let depositors add datasets"). |

Any other group name is accepted and has no effect.

**Basic mode has no groups.** Caddy only tells the api whether a user is an
admin: the users in `auth.admins` plus those with `admins` in `groups`. A
`librarians` or `curators` group does nothing there; use `auth.mode:
authelia` when you need groups.

### After editing site.yaml

Every change to `site.yaml` takes effect the same way:

```sh
bin/ops/ant-stack check     # validates the change and prints the plan
bin/ops/ant-stack up        # renders the config and recreates what changed
```

`up` recreates exactly the containers whose input changed. api, web and mcp
read their rendered config and secret files only at start, so the rendered
compose file carries a hash of those files for each of them
(`ANTHIVE_CONFIG_SHA`); when the hash changes, compose recreates that
container. Caddy, Authelia and the static copy are recreated on every `up`.
Since 2.45.1 this covers changes that only touch the rendered config, such as
`auth.internal_requires_auth` or `auth.mode`; with 2.45.0 and older, run
`bin/ops/ant-stack restart api web mcp` after such a change (or the old
setting stays in force). `restart` alone is not enough after a change of
mounts, ports or images: use `up`.

### Let people add papers

Off by default. To let users add papers to the library through the API and
their MCP link (`doc/LIBRARY.md`, "Adding papers through the API"):

```yaml
services:
  paper_upload: true
library:
  upload_groups: [admins]      # Authelia groups allowed to upload; basic mode: auth.admins
  max_pdf_mb: 25
  git_push: false              # leave off: publish-library pushes
  s3_autopush: false           # leave off: publish-library syncs the PDFs
  deploy_key: /srv/anthive/keys/library_deploy   # see below
  index_publisher: false       # true on ONE site only (normally the main one)
```

Then `bin/ops/ant-stack check` and `bin/ops/ant-stack up`. With
`paper_upload` on, `data.library` and `data.library_index` are mounted
read-write into the api container (otherwise read-only). `data.library`
must be a git checkout of the library repo and writable by the site's uid;
`check` warns when it is not. Each upload becomes a commit there, with the
uploader as author and `library.git_committer` as committer.

**Librarians.** To let people who are not admins add papers, give them a
group of their own and allow that group to upload. They get the upload
features (API, MCP tools, the upload part of `/api-guide`) and nothing else
an admin can do:

```yaml
auth:
  users:
    - name: jdoe
      groups: [librarians]
library:
  upload_groups: [admins, librarians]
```

Then `bin/ops/ant-stack init` and `bin/ops/ant-stack up`. `init` writes
the group to Authelia's user file only for a user who is new; for someone who
already exists, add the group in `<state_dir>/authelia/users_database.yml`
(see "Users and groups"). Authelia picks up the new group within a few
minutes, at the next login at the latest. In `auth.mode: basic` there are no
groups: only `auth.admins` can upload.

Getting those commits and PDFs out of the server is done on the host, not
in the api container, by `bin/ops/ant-stack publish-library`. It takes the
same lock the upload path holds (so no upload commits halfway), runs
`git pull --rebase` and `git push` in the library checkout with a deploy key
that can write to that one repository, and then syncs the PDFs and the
literature index with S3 through the ingest container
(`ant sync library --no-git`). The api container keeps no git credentials
and no S3 keys.

This is the recommended setup. Do it once:

1. Make a key without a passphrase, readable only by you:
   `mkdir -p /srv/anthive/keys && ssh-keygen -t ed25519 -N '' -C "$(hostname) anthive library" -f /srv/anthive/keys/library_deploy`
   and check `chmod 600 /srv/anthive/keys/library_deploy`.
2. Add `/srv/anthive/keys/library_deploy.pub` to the library repository as
   a deploy key with write access (GitLab: Settings > Repository > Deploy
   keys, "Grant write permissions"). Ask the maintainers if you cannot.
3. Make sure the checkout uses the SSH remote
   (`git -C /srv/anthive/library remote -v` shows `git@gitlab...`) and that
   the host key is known: `ssh-keyscan gitlab.kuleuven.be >> ~/.ssh/known_hosts`.
4. Set `library.deploy_key` in `site.yaml` (above), then try it by hand:
   `bin/ops/ant-stack publish-library --dry-run` contacts the remote with
   the key and says "remote: reachable with this key", or prints git's full
   error (a wrong key gives "Permission denied (publickey)", an unknown host
   "Host key verification failed"). Then run it without `--dry-run`.
5. Run it every 15 minutes. With systemd, fill in and install the two
   templates `config/examples/anthive-publish-library.{service,timer}.example`
   (instructions inside); with cron, use
   `config/examples/anthive-publish-library.cron.example`.

**S3 keys only on the host.** A site that keeps the S3 keys out of every
container (`s3.enabled: false` in `site.yaml`, the keys in an
`anthive.yaml` of the host's own) runs the PDF step on the host instead
of in the ingest container:
`bin/ops/ant-stack publish-library --s3-host-config /etc/anthive/anthive-host.yaml`.
It runs `bin/ops/ant-sync library --no-git` (needs `uv` on `PATH`) with
`ANTHIVE_CONFIG` set to that file, under the same lock, after the git
step. This replaces the older two steps (`publish-library --no-s3`
followed by a separate `ANTHIVE_CONFIG=... ant-sync library --no-git`).

Run it as the user that owns the library checkout (the `run_as` uid);
otherwise git refuses the checkout as "dubious ownership". The command exits
non-zero when the pull, the push or the S3 sync fails, so the timer shows up
as failed in `systemctl list-timers` / `journalctl`.

The older alternatives still work but put credentials in the container.
`git_push: true` makes the api container push after each upload; the
container has no SSH keys, so the remote must work without them, typically
an HTTPS remote with a project token that ends up in `.git/config` and is
readable by anyone who can read the checkout. `s3_autopush: true` pushes
each PDF at once and needs `s3.api_access: write`, which gives the api the
read-write S3 keys; `check` warns about both.

Other sites get the new papers with their next `ant sync pull` (or `ant sync
library`), which pulls the records from git and the PDFs from S3. See
`doc/LIBRARY.md` for conflicts and deletes.

### Let depositors add datasets (since 2.46.0)

Off by default. With it on, signed-in members of `datasets.upload_groups` deposit datasets without S3 keys of
their own: the api hands out presigned uploads into the incoming prefix of the S3 watcher, and the watcher
checks and publishes them (`doc/DATASET_INGESTION.md`, "Depositing through the API"; design section 22).

```yaml
services:
  dataset_deposit: true
datasets:
  upload_groups: [admins, curators]   # Authelia groups; basic mode: auth.admins
  max_gb: 200                         # largest deposit
  url_ttl_hours: 24
  max_open_per_user: 5
  max_pending_gb_per_user: 400
  update_policy: own                  # own | any: who may deposit a new version of a dataset
  site_id: null                       # staging folder __deposits/<site_id>/; default site.name
s3:
  enabled: true                       # required
```

1. Give the api a key pair to presign with:
   `<state_dir>/secrets/s3_incoming_access_key_id` and `s3_incoming_secret_access_key` (chmod 600). Limit it
   to `PutObject`, the multipart calls, `CopyObject`, `HeadObject`, `GetObject`, `DeleteObject` and listing on
   `__deposits/*` (where uploads land) and `__incoming/*` (where complete copies them) if your object store
   can scope keys. Every site that shares the bucket needs its own `site.name` (or `datasets.site_id`), and
   only one watcher may read an incoming prefix; `check` reminds you. Without these files the api presigns with the read-write pair
   (`s3_access_key_id`), and `check` warns that the api container then holds the catalogue's write key.
2. Run the watcher on the host, as the uid the containers run as: `bin/ops/ant-stack watch` (since 2.46.1;
   systemd: fill in `config/examples/anthive-ingest-watch-stack.service.example`). It starts
   `bin/ingest/ant-ingest-watch` with the watcher config (`stack/anthive-watcher.yaml`, printed by
   `bin/ops/ant-stack config-path --role watcher`): host paths under `scratch_dir/watcher` (else
   `state_dir/watcher`) for `ANTHIVE_TMP_DIR`, `ANTHIVE_LOG_DIR`, `ANTHIVE_CONVERT_STAGING_DIR` and
   `ANTHIVE_INGEST_WORK_DIR`, the real `state_dir` (so the internal secret in `state_dir/secrets/internal_secret`
   is found), `ANTHIVE_INGEST_STATE_DIR=<state_dir>/ingest`, the S3 prefixes and
   `ANTHIVE_API_URL=http://127.0.0.1:<ports.tools>`. `check` prints the same command with every variable spelled
   out. The api is reached through Caddy's tools listener: `up` publishes it on `127.0.0.1:<ports.tools>`
   (default 8079) while deposits are on; it drops every identity header and refuses requests without the
   internal secret, which the api then checks, so it is no way in for other users of the host. Do not use the
   ingest role's config on the host (it has container paths and `http://api:8080`: the publication then fails
   at the rescan). The api keeps the deposits in `<state_dir>/ingest/deposits.sqlite`
   and reads the watcher's progress and reports from the same folder. The incoming prefix
   (`datasets.incoming_prefix`, default `__incoming/`) and the catalog prefix (`datasets.catalog_prefix`,
   default `s3.prefix`, where ant-sync keeps the catalogue) are rendered into every config, so the api presigns
   into the folder the watcher reads and reuses published files from where the watcher pushes them. If the
   watcher has an `anthive.yaml` of its own, name it in `datasets.watcher_config`: `check` fails when its
   prefixes differ. See "The S3 ingest watcher" below for the rest of its setup.
3. `bin/ops/ant-stack check`, then `bin/ops/ant-stack up`. `check` prints
   `dataset deposits: on for admins, curators; presigning with the incoming key pair; run ant-ingest-watch ...`,
   or the problem (`s3.enabled` off, no key pair).
4. For e-mails to depositors (live, held, failed) set `ingest_watch.notify.smtp_host` in the watcher's config.
5. Give the bucket a lifecycle rule `AbortIncompleteMultipartUpload` (for example after 8 days, longer than
   `datasets.max_open_days`) on `__deposits/` and `__incoming/`. Expiry and withdraw abort the uploads they
   know; the rule catches the rest (a crashed server, an object store that lost an abort), which otherwise
   keep storage without showing in listings. With the AWS CLI against your endpoint:
   `aws s3api put-bucket-lifecycle-configuration --bucket <bucket> --endpoint-url https://<endpoint>
   --lifecycle-configuration '{"Rules": [{"ID": "abort-deposits", "Status": "Enabled", "Filter": {"Prefix":
   "__deposits/"}, "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 8}}]}'` (and a second rule for
   `__incoming/`). `ant-stack check` reminds you.
6. Datasets published before deposits existed have no owner, so only admins can deposit new versions of them.
   Name an owner with `ant-ingest-watch owner set <db_id> <user> --by <you>` on the watcher's host (or
   `POST /api/datasets/deposit/owners`); it adds a caveat to the dataset's `.md`, which you commit.
7. Once depositors use vark, set `ingest_watch.accept_unlinked: false` in the watcher's config so only
   deposits made with an Anthive login are published.

Depositors need the curators group (`auth.users[].groups`) and, once it exists, the vark client. Their MCP
link shows `my_deposits` and `deposit_status`; uploads never go through MCP.

### Let analysts deposit artefacts (since 2.47.0)

Artefacts are versioned analysis files (DE tables, figures, tables, notebooks, reports, models) that belong to a
dataset or to a collection. S3 holds them; the api checks who may see an artefact and answers a download with
a short-lived signed link (`doc/DESIGN_artefacts.md`). The read routes always serve what the store holds, so a
mirror sees the artefacts its h5adstore checkout brings. Deposits are off by default:

```yaml
artefacts:
  enabled: true                       # artefact and collection deposits (needs s3.enabled)
  upload_groups: [admins, analysts]   # who may deposit artefacts (Authelia groups; basic mode: auth.admins)
  collections_create_groups: [admins, analysts]   # who may create analysis collections
  max_file_gb: 100                    # largest file
  max_deposit_gb: 200                 # largest deposit
  max_stored_gb_per_user: 1000        # artefact storage per user, pending deposits included
  hold_public: true                   # an admin publishes every public artefact
s3:
  enabled: true
```

1. Artefact deposits use the dataset deposit machinery, so step 1 (the presigning key pair) and step 2 (the
   watcher on the host, `bin/ops/ant-stack watch`) of "Let depositors add datasets" apply as they are, also
   when `services.dataset_deposit` stays off: with `artefacts.enabled` the watcher config, the tools listener
   on `127.0.0.1:<ports.tools>` and the presigning pair are rendered too, and one watcher publishes datasets
   and artefacts. The lifecycle rule of step 5 covers artefact uploads (same prefixes).
2. Give the api a read-only key pair for artefact downloads: `<state_dir>/secrets/s3_artefacts_read_access_key_id`
   and `s3_artefacts_read_secret_access_key` (chmod 600; `HeadObject` and `GetObject` on the catalogue
   prefix are enough). The api HEADs each file against its manifest and signs a GET for that one key. Without
   the pair the api uses its own S3 keys (`s3.api_access: read`), and with neither only files with a local copy
   (figures and files up to 2 MB, which the watcher keeps in the store) can be downloaded; `check` says which.
3. Put the depositors in the `analysts` group (`auth.users[].groups` before `init`, or
   `authelia/users_database.yml`). Basic mode has no groups: only `auth.admins` can deposit.
4. In the h5adstore checkout, add these lines to `.gitignore` and commit them (the watcher commits head records
   and manifests by path; a hand `git add -A` would otherwise pick up artefact files):

   ```
   static/artefacts/**/files/
   static/artefacts/**/preview/
   ```
5. `bin/ops/ant-stack check`, then `bin/ops/ant-stack up`. `check` prints `artefact deposits: on for admins,
   analysts`, how artefact files are served, and fails for a half read key pair or `s3.enabled: false`.
6. Other sites: `ant sync pull` (`bin/ops/ant-stack ingest -- ant sync pull`) brings the inline files and
   previews of every live artefact version their checkout lists and deletes local copies of purged versions;
   `ant sync pull --artefacts 'S20/SIE/*'` also brings the large files of the artefacts the pattern names
   (`<group_id>/<scope>/<artefact_id>`, or `...@v<N>`). `ant sync push` never uploads artefacts. Head
   records and manifests come with `git pull` of the h5adstore.

Signed links never reach the access log: the Caddy log filter drops the query string of a redirect's
`Location` header (checked by `ant-stack check`).

### Send deposits to a central watcher (since 2.47.0)

A site can accept dataset and artefact deposits from its own users while one watcher on another host (the
central watcher) ingests them into the store. The site then runs no watcher; its uploads, a signed record of
each deposit and the incoming copies go to a separate ingestion bucket, and the watcher answers with a signed
result that the site shows and e-mails to the depositor (design section 24).

1. **Ingestion bucket and keys.** Ask the administrator of the central watcher for the ingestion bucket and a
   key pair for this site. Where the object store can scope keys, limit the pair to `__deposits/<site_id>/`
   (read and write) and `__incoming/` (write): every holder of a key for the whole bucket can read the other
   sites' deposit records and results (logins, file keys and checksums, redacted reports), and can rewrite
   their uploads (the watcher then holds both deposits for an admin). Write the pair to
   `<state_dir>/secrets/s3_ingest_access_key_id` and `<state_dir>/secrets/s3_ingest_secret_access_key`
   (mode 600). The main `s3` keys of the data bucket stay on the host (the api does not get them for deposits).
2. **The watcher's public key.** The administrator runs `ant ingest-watch result-key` on the watcher host and
   sends the line `ed25519:...`.
3. **site.yaml:**

   ```yaml
   services:
     dataset_deposit: true
   datasets:
     site_id: intern                 # unique among the sites of the ingestion bucket
     central_watcher:
       ingestion_bucket: anthive-ingest
       watcher_public_keys: ['ed25519:...']
       key_id: k1
   ```

4. **The site secret.** `ant-stack up` (or `init`) creates `<state_dir>/secrets/deposit_site_secret` on the
   host and binds it read-only into the api container only (web, mcp and ingest never get it). Send it to the
   administrator over a safe channel; it goes to the watcher host as `<sites_dir>/<site_id>.keys` (mode 600,
   owned by the watcher's user, in a folder only that user can write) with the line `k1 <secret>`.
   `ant-stack check` prints the exact lines and reports a missing secret. A site's admins are ordinary
   depositors at the central store unless the watcher's administrator lists the site in
   `ingest_watch.trusted_admin_sites`.
5. `ant-stack check`; `ant-stack watch` refuses on such a site. Approve, reject and artefact purges happen on
   the central site.

To rotate the site secret: write a new secret to `deposit_site_secret`, set `key_id: k2`, and ask the
administrator to add the line `k2 <new secret>` next to the old one; the old line can go once older deposits
are processed.

### A mirror with a hand-picked subset (symlinked stores and library)

A public mirror can serve a few curated datasets and papers that physically
live in another site's stores. Make the mirror's `h5adstore` and
`library/papers` folders of symlinks to the other site's files, and list
the folders the links point into under `data.extra_mounts`:

```yaml
data:
  h5adstore: /srv/mirror/h5adstore          # .md/.h5ad/.duckdb symlinks into /srv/intern/h5adstore
  library: /srv/mirror/library              # papers/<folder>/<citekey>.md + .pdf symlinks into /srv/intern/library
  library_index: /srv/mirror/library_index  # this site's OWN index, see below
  extra_mounts: [/srv/intern/h5adstore, /srv/intern/library]
  readonly: true
auth:
  mode: none
s3:
  enabled: false
```

Rules and limits:

- With `auth.mode: none` only `access: public` datasets are served (see
  "Internal datasets"); `ant-stack check` lists how many of the linked
  datasets are hidden. Link only public datasets into a public mirror anyway:
  the files are readable on the host.

- `extra_mounts` are absolute host paths, mounted **read-only at the same
  path** into api, web, mcp and ingest. `ant-stack check` fails when one does
  not exist or is inside (or contains) `state_dir`, and warns when it
  overlaps another mount or when a symlink in the stores or the library
  points outside every mounted path (it would dangle in the containers).
- A library that is not a git checkout works read-only. Leave
  `services.paper_upload` off (uploads answer 503) and do not set
  `library.deploy_key`. No git command is ever run in a folder without its own
  `.git`, not even when it sits inside another repository; `ant-stack
  publish-library` refuses it. The api notices added, removed or replaced
  record files within about 30 s (or on `POST /admin/rescan`), not only git
  commits. A record symlink whose target is gone is listed as an error in
  the admin `/library/status`; a PDF symlink that dangles counts as "no PDF".
- **The site builds its own literature index** from its own PDFs, never the
  shared one:

  ```sh
  bin/ops/ant-stack ingest -- ant lib-index --library /srv/mirror/library \
      --out /srv/mirror/library_index/library_chunks.parquet
  ```

  Nothing in `ant-stack` fetches the index: only `ant sync` does, and it
  needs an `s3:` block, which a site with `s3.enabled: false` does not get.
  On a site that does have S3 but selects its own papers, set
  `library.index_pull: false`. The api reads only `data.library_index`, and
  literature search answers only for papers that are in the site's own
  library, so passages of other papers could not appear even from a stray
  index.
- **Papers the site does not hold are invisible.** A `papers:` link on a
  dataset or collection to a citekey that is not in this site's library is
  dropped on every surface: dataset and collection pages, paper lists,
  `GET /databases/{id}/papers`, `/databases/{id}/context`, `related` lists,
  BibTeX and the MCP tools show nothing about it (no placeholder, citekey,
  title or metadata). Only admins see the list, in
  `GET /library/status` as `unresolved_references`; with `auth.mode: none`
  there are no admins, so nobody does. `ant lib-check` on the host still
  reports them.

### A public mirror: switch off facets, caveats, API pages and MCP

A public read-only site (for example a data website with `auth.mode: none`)
can leave out parts of the web UI with the `ui:` block. Every switch is
`true` by default, so an existing site changes nothing:

```yaml
auth:
  mode: none
ui:
  facets: false      # Facet tabs, /facets pages, facet DE on gene and gene set pages, per donor plots, facet help
  caveats: false     # Caveats tab and caveat markers (see the warning below)
  api: false         # API page, API links, code snippets and notebook buttons
  mcp: false         # MCP account page, MCP links and MCP help; no mcp service
services:
  mcp: false         # say so here too; ui.mcp: false wins anyway
```

What each switch does:

| Switch | Gone from the web UI | Closed on the server |
| --- | --- | --- |
| `ui.facets: false` | the `facets` menu entry, the Facet tab group (Facet, Gene, GS) of the dataset page, the Facet DE tab of gene and gene set pages, the "facet" label on `ant_*` column names, facet sections, topics and glossary terms on the help page | `/facets`, `/facets/<name>` and the per donor plot pages answer 404. The REST API keeps its facet endpoints (`/api/facets/...`, `/api/databases/<id>/facet-tests`) for vark and pyvark |
| `ui.caveats: false` | the Caveats tab of the dataset page, the caveat mark in collection lists, the caveat button of plot pages, the caveats topic on the help page | nothing: the REST API and MCP still return caveats (`/api/databases/<id>/info`, `get_dataset_context`) |
| `ui.api: false` | the API link in the top bar and the user menu, the changelog link of the version button, the notebook buttons, the REST API and notebook links on the about page, the programmatic access section of the help page | `/api-guide` answers 404; `/api/docs` and `/api/redoc` answer 404 to visitors who are not signed in. The REST API itself, `/api/openapi.json` and `/api/version` stay: the web UI calls the API from the browser, and the notebooks, vark and pyvark need it. JupyterLite (`/jupyter/`) is still served; nothing on the site links to it |
| `ui.mcp: false` | the "Connect an MCP client" menu entry and every mention of MCP on the help, about and API pages | `/account/mcp` answers 404, the API reports MCP links as disabled, the mcp container is not started (even with `services.mcp: true`) and `/mcp` answers a JSON 404 |

> **Caveats carry data-quality warnings.** They record curation decisions and
> known defects of a dataset (for example a broken embedding or contaminated
> cells). With `ui.caveats: false` visitors of the site see the data without
> those warnings. `ant-stack check` prints a warning when the switch is off.

`ant-stack check` lists the parts that are off (`ui: switched off: ...`) and
the plan shows them on the `ui off` line. After a change, `ant-stack up`
recreates the web, api and mcp containers with the new config.

### Run syncs from cron or a systemd timer

`ant-stack` and `ant-sync` start with `#!/usr/bin/env -S uv run ...`, so
`uv` must be on `PATH`. The installer puts it in `~/.local/bin`, which cron
(`PATH=/usr/bin:/bin`) and systemd services do not search; the job then fails
with `env: 'uv': No such file or directory` before any anthive code runs.
Set `PATH` in the crontab or the unit, as the templates do:

| Job | Templates in `config/examples/` |
| --- | --- |
| `git pull` of the h5adstore, then `ant-stack ingest -- ant sync pull` (metadata, data, library PDFs, `_facet_tests/`); **every hour** is the recommended interval, so datasets the ingest watcher publishes reach this site within the hour | `anthive-sync.cron.example`, `anthive-sync.service.example` (+ a timer like the next row) |
| `ant-stack publish-library` | `anthive-publish-library.{cron,service,timer}.example` |

`ant-stack check` prints where `uv` is and which directory cron needs on
`PATH`, and warns when `uv` is not found at all. Alternatively call uv by its
absolute path (`/home/you/.local/bin/uv run --script <repo>/bin/ops/ant-stack ...`), or prefix
the command with `bin/ops/ant-cron-run`, which adds the usual uv folders to
`PATH` and, when uv is still missing, prints that plainly and exits 127.

### The S3 ingest watcher (not in the compose stack yet)

`ant ingest-watch` (the S3 watch route of `doc/DATASET_INGESTION.md`) has no compose service and no
`site.yaml` key yet. With `services.dataset_deposit` on (since 2.46.0), start it with
`ANTHIVE_INGEST_STATE_DIR=<state_dir>/ingest` so the api and the watcher share the deposit state. It runs on the dev box as its own systemd unit
(`config/examples/anthive-ingest-watch.service.example`), off unless `ingest_watch.enabled: true` in that
host's `anthive.yaml`. Container sites receive what it publishes the usual way, through S3:
`ant-stack ingest -- ant sync pull --include-duckdb '<Group>/*'` and a rescan. All its paths already resolve
through `lib/config.py` (`ingest_state_dir`, `ingest_work_dir`, the store), so a later compose service only
needs the store mounted read-write, the state and tmp dirs, and the ingest S3 keys.

### Upgrade to a new release

```sh
cd /srv/anthive/anthive4
git fetch --tags
git checkout vX.Y.Z             # the new release
nano site.yaml                  # images: tag: X.Y.Z
bin/ops/ant-stack check         # flags new settings the release needs
bin/ops/ant-stack pull
bin/ops/ant-stack up            # right after pull
```

Run `up` right after `pull`. Since 2.46.1 `pull` and `check` only stage
the new config (`stack/staging/`) and `up` applies it together with the
new images. Before 2.46.1 `pull` and `check` wrote the new release's
config into the running stack at once, and an old api that restarted
before `up` stopped with "Unknown configuration parameters"; since 2.46.1
the api, web and mcp containers also only warn about keys they do not know
(except in `auth`, `s3`, `sync_policy` and the access settings, which are
always checked strictly). Users, passwords, MCP links and caches live in `state_dir` and carry
over. To go back, check out the old tag, put the old image tag back and run
the same two commands.

#### What changed in recent releases (read when upgrading an older site)

Read every entry between your current version (top-right of the site, or
`curl -sk https://<site>/api/health`) and the new one.

| Release | What it means for an existing site | Action |
| --- | --- | --- |
| **2.37.2** | Images are published only on the KU Leuven GitLab registry. Sites set up earlier may still point at `codeberg.org/mfiers`, which no longer has images. | in `site.yaml`: `images: registry: registry.gitlab.kuleuven.be/bdslab/anthive/anthive4` |
| **2.38.0** | Several named MCP links per user. Existing links keep working (migrated and named "default"). New "Report an issue" link in the user menu. | optional: `site.issue_tracker_url` (empty hides the link) |
| **2.39.0** | Paper library and literature search (REST, MCP tools, prompts). Without the library the site works as before. New front page. | optional: add the library, see below |
| **2.40.0** | Paper upload through the API and MCP, two-way library sync, version button in the top bar. Uploads are off by default. | optional: `services: paper_upload: true` (see "Let people add papers") |
| **2.40.1** | Paper PDFs moved from `*/static/` into the paper library (and deleted there on S3); new Papers tab, paper pages and Library page. | after `ant sync pull`, delete old local copies: `find <h5adstore> -path '*/static/*.pdf' -not -path '*/_*' -delete`; add the library if you have not (below) |
| **2.40.2** | Client setup guide for 22 AI clients on GitLab (`doc/MCP_CLIENTS.md`); the help page links to the GitLab docs. | optional: `site.docs_base_url` to point the help links at your own docs |
| **2.41.0** | Secrets reach only the containers that use them: S3 keys go to the ingest container alone, api and mcp no longer see `state_dir/secrets`. `library.s3_autopush` is off by default; papers are published from the host with `ant-stack publish-library`. Caddy believes `X-Forwarded-*` from private addresses when `tls.mode` is off, which fixes Authelia behind a TLS proxy. `ant-stack pull` now pulls the tag in `site.yaml`. In the library, the per-paper access and LLM fields (`anthive_access`, `anthive_llm`) are gone: everyone with site access sees the whole library, and snippets go to the AI providers of connected clients. Role defaults to `background`; document types; `papers/proposals/`; uploads answer 503 `disabled` when the library is read-only and are hidden from non-uploaders. Expression layers are read relative to each `.duckdb`, so stores that moved or were renamed work again. | read "Upgrading to 2.41.0" below; before upgrading, check that nothing in the library must stay hidden from people who can sign in (move it out of the library); `ant lib-check` warns about leftover fields |
| **2.42.0** | Hybrid literature search keeps paraphrase matches; upload index status lists the queue; help page quick start; one-paste setup prompt on the MCP page; MCP snippets use a per-site connection name. | none: set `images: tag: 2.42.0`, `ant-stack pull`, `ant-stack up` (from 2.40.x, first follow "Upgrading to 2.41.0") |
| **2.42.1** | A running server picks up records and a literature index that a sync changed on disk (within about 30 s). Needed on the site that publishes the index and on sites that download it. | none: set `images: tag: 2.42.1`, `ant-stack pull`, `ant-stack up` |
| **2.43.0** | Facet test engine v2: partial-conjunction replication, `apoe_carrier` renamed `apoe4_carrier` (the old name stays as an alias), dataset versions. | `git pull` the h5adstore, then `ant sync pull`; set `images: tag: 2.43.0`, `ant-stack pull`, `ant-stack up` |
| **2.44.0** | (1) **Facets**: `ant sync pull` now also brings `_facet_tests/` (the facet test results the API serves; logs and scripts stay out), and the h5adstore repo has many changed `.md` files (facet layer metadata, lineage fields). (2) **Mirror sites with symlinks**: new `data.extra_mounts` bind-mounts folders read-only at the same path into api, web, mcp and ingest, so symlinks from your stores into another site's stores resolve in the containers; `ant-stack check` validates them and warns about symlinks that would dangle. (3) **Hidden paper links**: on every site a `papers:` link to a citekey that is not in the site's own library is hidden everywhere (web, REST, MCP, BibTeX); `GET /library/status` shows `unresolved_references`, issue texts and paths to admins only (on `auth.mode: none` to nobody), non-admins get counts (`n_errors`, `n_warnings`). (4) **Library**: a plain, non-git library of symlinked records works read-only (no git is run in it); `file not found` warnings are computed from the disk on each call and no longer outlive the PDF's arrival; a symlinked record whose target is gone is an error entry, not a crash. (5) **Own literature index**: new `library.index_pull` (default true); `ant sync` never downloads the shared index when `s3.enabled` is false or `library.index_pull: false`. (6) **Facet engine v3 and help API**: facet scopes, new facets and exclusions, per-cell-type clusters, faster facet endpoints; `GET /api/help` and the MCP `explain` tool serve plain-language explanations; facet results are published by `ant sync push` and pulled by `ant sync pull`. (7) **Cron and systemd**: new `config/examples/anthive-sync.{cron,service}.example` set PATH for uv; `ant-stack check` says where uv is and warns when it is missing. | none required. `git pull` the h5adstore, then `ant-stack ingest -- ant sync pull` for `_facet_tests/`. Set `images: tag: 2.44.0`, `ant-stack pull`, `ant-stack up`. Optional: `data.extra_mounts`, `library.index_pull`; cron jobs: add `PATH=$HOME/.local/bin:...` (see "Run syncs from cron or a timer") |
| **2.45.0** | (1) **Internal datasets need a login**: datasets that are not `access: public` (also those without an `access` value) are hidden from callers without an identity: absent from every listing, 404 on their routes (new `auth.internal_requires_auth`, default true; see "Internal datasets"). Sites with a login see no change; on `auth.mode: none` internal datasets disappear, and a `trusted-proxy` site behind a vhost without a login must drop the `Remote-*` headers there. (2) `X-Remote-User` / `X-Remote-Groups` are ignored everywhere and dropped by the Caddy snippets. (3) Cache, index and rescan routes (`/admin/*-cache/*`, `/admin/*-index/*`, `/admin/api-config/*`, `POST /genesets/rescan`, `/module-variability/rescan`, `/catalogs/rescan`) need an admin, a host-local tool or the internal secret; other signed-in users get 403. (4) The web, mcp and api containers must run the same release (the web now forwards the visitor's identity to the api). (5) **Cache warm-up**: after a start the api fills the facet caches for signed-in and anonymous visitors, per-donor values and the gene set rank tables in the background (new `warmup` block of anthive.yaml, default on; see "Cache warm-up after a start"). The first start builds the rank tables (up to a minute per dataset with scores, one at a time). | `ant-stack check` (it prints how many datasets are internal and who sees them, and warns only for `internal_requires_auth: false` or `trusted-proxy`), set `images: tag: 2.45.0`, `ant-stack pull`, `ant-stack up`; then `ant-stack restart api web mcp` (2.45.0's `up` does not recreate them when only the config changed; fixed in 2.45.1). If your upstream proxy sends identity as `X-Remote-*` only, switch it to `Remote-User` / `Remote-Groups`. If you copied an nginx vhost from `config/examples/`, add the new `proxy_set_header ... ""` lines (the public vhost must drop `Remote-*`). Scripts that call the rescan routes need an admin account. |
| **2.45.1** | Fixes in `ant-stack`: `up` recreates api, web and mcp when their rendered config or secrets changed (before, a changed `auth.internal_requires_auth` or `auth.mode` did not take effect until a restart); `restart` takes several services; `passwd` without a terminal points to `--password`; `check` labels Caddy's in-container listener, prints the published host address and always prints the internal dataset count; `ant-stack logs api` shows the warm-up progress. | set `images: tag: 2.45.1`, `ant-stack pull`, `ant-stack up` |
| **2.46.0** | Dataset deposits through the API: signed-in members of `datasets.upload_groups` get presigned uploads into the S3 watcher's incoming prefix and follow their deposit in the API (and two read-only MCP tools). Off by default; nothing changes for a site that leaves it off. `ant sync push` now sets `x-amz-meta-sha256` on uploaded `.h5ad` objects. | optional: `services: dataset_deposit: true`, the `datasets:` block, a presigning key pair and the watcher on the host (see "Let depositors add datasets"); `ant-stack check`. The primary site, once, for metadata-only updates of datasets pushed before: `bin/ops/ant-stack ingest -- ant sync stamp-sha256 --dry-run`, then without `--dry-run` |
| **2.47.0** | Artefact store: versioned analysis files of datasets and collections, read routes under `/api/artefacts`, artefact and collection deposits (off by default), DE tables as `de_table` artefacts (the 2.46 DE-only deposit becomes an alias until 2.48.0). `ant sync push` never uploads `static/artefacts/`; `ant sync pull` brings inline artefact files and previews and deletes purged copies. A site can send its deposits to one central watcher on another host (`datasets.central_watcher`, a separate ingestion bucket, signed records and results); nothing changes for a site that leaves it unset. | none required. Optional: the `artefacts:` block, the read-only pair `secrets/s3_artefacts_read_*`, the `analysts` group and the h5adstore `.gitignore` lines (see "Let analysts deposit artefacts"); for a central watcher: `datasets.central_watcher`, `secrets/s3_ingest_*` and the site secret (see "Send deposits to a central watcher"); `ant-stack check` |
| **2.46.1** | Fixes. (1) `ant sync pull --dry-run` changes nothing on every path (before, with `--include-duckdb` or `--only` it downloaded and overwrote files); `ant sync push --dry-run` exists. (2) `sync_policy.mode: priority` pulls each kept dataset with its layer, DE and score parquets plus the shared indexes, caches, `_facet_tests/` and `static/`, never `.h5ad`; `--include-duckdb` no longer bypasses it. (3) `pull` and `check` stage the rendered config, `up` applies it; the api warns about unknown config keys instead of stopping. (4) `check` counts datasets like the api (store-root documents and `static/*.md` are not datasets) and says "no datasets yet" for an empty store. (5) Facet routes answer 503 `warming_up` with `Retry-After` while the start-up warm-up builds their caches, instead of blocking for a minute; the web says "warming up, try again in a minute". (6) `publish-library --s3-host-config <path>`; it says "records: up to date" when there was nothing to push. (7) Deposit watcher: `ant-stack watch`, a watcher config with host paths (`config-path --role watcher`), the api reachable through Caddy's tools listener on `127.0.0.1:<ports.tools>`, and the full command in `check`. (8) `POST /api/me/mcp/links` also accepts `{"name": ...}`. | `git checkout v2.46.1`, set `images: tag: 2.46.1`, `ant-stack check`, `ant-stack pull`, then `ant-stack up` at once. A priority site: replace any `ant sync pull --include-duckdb` in cron with `ant sync pull` (add `--evict` if wanted), try it with `--dry-run`. A deposit site: replace the watcher's `ANTHIVE_CONFIG=... --role ingest` setup with `ant-stack watch` (`config/examples/anthive-ingest-watch-stack.service.example`); `up` publishes `127.0.0.1:8079` (`ports.tools`). A site with S3 keys only on the host: replace the two-step library publishing with `publish-library --s3-host-config <path>`. |
| **2.46.2** | Security and scratch-site fixes. (1) The Caddy access log no longer writes the presigned query (signature) of a download redirect. (2) Validators refuse values with a trailing newline. (3) `check` refuses unknown keys in the `auth`, `tls`, `ports`, `s3`, `services`, `datasets` and `data` sections of `site.yaml` and warns about unknown keys elsewhere, with the closest known key. (4) An invalid rendered config is one `PROBLEM` line; `ant sync` with an unreachable S3 endpoint says so in one line. (5) The MCP instructions name `<public_url>/mcp`, not the container listener. (6) The deposit watcher gives up a listing of an unreachable endpoint within about a minute and logs every failed attempt; a second SIGTERM stops it at once. `ant-stack watch` prints the command it runs. | Fix every `site.yaml: unknown key` that `check` reports (typos there were silently ignored before), then `ant-stack pull` and `ant-stack up`. |
| **2.46.3** | JupyterLite works again. The `Getting_Started` notebook failed at its first cell (`No module named 'requests'`) and the bundled Python client was pyvark 0.9.0; it is now 0.11.0, and the dataset and plot notebooks no longer install an old pyvark from PyPI. No API or configuration change. | `ant-stack pull` and `ant-stack up`. Optionally run `make test-live-site URL=<public_url>` from a checkout of v2.46.3 to confirm JupyterLite after the login. |
| **2.46.4** | `ant-stack ingest`, `publish-library` and `watch` apply their own config before they run. In 2.46.1 to 2.46.3 S3 keys written after `init` stayed staged, and the first `ant-stack ingest -- ant sync pull` failed with `s3.access_key_id is not set`. No API change. | `ant-stack pull` and `ant-stack up`. (The workaround on older releases is `ant-stack render` after writing the key files.) |
| **2.46.5** | `ant sync pull` fixes, found on a priority slave. (1) Only catalogued datasets are pulled (a `.md` in the h5adstore checkout, no `.inactive`, not in a `_` folder), on every path; S3 objects without their `.md` are listed as "on S3 but not catalogued (retired?)" with sizes and left on S3. Before, a priority site planned to download every retired `.duckdb` (about 40 GB on the intern site). (2) The glob filter (`ant sync pull 'Francken2026/*'`) now holds under `sync_policy.mode: priority`; it was ignored. (3) Same-size files with the same content (S3 ETag, or `x-amz-meta-sha256`) are never downloaded again, only their mtime is realigned; before, a priority site re-downloaded small `_facet_tests/` and cache files whenever their S3 copy had been re-uploaded, and the generic slave pull re-downloaded every same-size file on every run. A file that does differ is listed with the reason before it is overwritten. No API change. Also new: New `ui:` block in `site.yaml` (`facets`, `caveats`, `api`, `mcp`, all `true` by default) to switch parts of the web UI off on a public mirror; each switch also closes the pages it hides. `ui.mcp: false` stops the mcp service and Caddy answers `/mcp` with a JSON 404 (also when `services.mcp` is false; before, `/mcp` gave a 502). See "A public mirror: switch off facets, caveats, API pages and MCP". Faster restarts: the facet results are cached on the scratch disk (parquet and JSON, no pickle) and module rank tables no longer pause after cache hits, so a restart with unchanged data finishes its warm-up in about 30 s instead of about 3 minutes; the first start after the upgrade rebuilds the caches once. | `git pull` the h5adstore, then `ant-stack pull` and `ant-stack up`. Try `ant-stack ingest -- ant sync pull --dry-run` and review the "not catalogued" list (restore a `.md` for anything that should still be served). A public mirror may add the `ui:` block. |

The in-app chat assistant is off and being phased out; leave
`services.chat: false`. AI use goes through the personal MCP links.

#### Upgrading to 2.41.0

1. Check out the tag, set `images: tag: 2.41.0` and run
   `bin/ops/ant-stack pull`. Check that its table lists the app image with
   the new tag. If an older `pull` left you on the old image, this is the
   release that fixes it.
2. S3. If the site serves only local data (the usual case), nothing to do:
   the api simply stops getting the keys. If datasets or PDFs should still
   come from S3 on demand, add a read-only key pair and
   `s3: api_access: read` (step 4). Sites that set S3 keys in the `anthive:`
   block of `site.yaml` should move them to `state_dir/secrets`; `check`
   warns about it.
3. Paper uploads (only with `services.paper_upload: true`). Uploaded PDFs no
   longer go to S3 by themselves. Set up the deploy key and the timer from
   "Let people add papers", and remove `git_push: true` and any token from
   the library checkout's `.git/config`
   (`git -C /srv/anthive/library remote set-url origin git@gitlab.kuleuven.be:bdslab/anthive/library_store.git`).
   To keep the old behaviour instead, set `s3_autopush: true` and
   `s3: api_access: write`.
4. Behind a reverse proxy with `tls.mode: off`: Caddy now trusts
   `X-Forwarded-*` from private addresses (`tls.trusted_proxies`, default
   `private_ranges`). Keep `ports.bind: 127.0.0.1` when the proxy runs on the
   same host, and make the proxy send `X-Forwarded-Proto` (nginx:
   `proxy_set_header X-Forwarded-Proto $scheme;`). The workaround of
   `tls: internal` with `proxy_pass https://...` and `proxy_ssl_verify off`
   can go: switch to `tls.mode: off`, `ports.http`, and `proxy_pass http://127.0.0.1:<ports.http>`.
5. `bin/ops/ant-stack check`. It now also warns when a secret file, a
   rendered config or the deploy key is readable by group or others; fix
   each with the `chmod` it prints.
6. `bin/ops/ant-stack up`. The api, web and mcp containers are recreated
   with the new mounts.

#### Adding the paper library to an existing site

1. Clone the library next to your other data (step 3):
   `git clone git@gitlab.kuleuven.be:bdslab/anthive/library_store.git /srv/anthive/library && mkdir -p /srv/anthive/library_index`.
2. Add both paths to `site.yaml` under `data:`:
   `library: /srv/anthive/library` and `library_index: /srv/anthive/library_index`.
3. Fetch the PDFs and the index from S3:
   `bin/ops/ant-stack ingest -- ant sync pull --library-only`.
4. `bin/ops/ant-stack check`, then `bin/ops/ant-stack up`.
5. Check: `https://<site>/api/library/status` reports the number of papers
   and `"loaded": true` for the index.

The site needs the S3 settings and keys (step 4) for step 3.

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
| `up` stops after a few minutes with `container ...-api-1 is unhealthy`, and `logs api` says `Found 0 databases` | release 2.45.1 with an empty store: its api never reports healthy without a dataset (fixed in 2.46.0) | upgrade, or clone the h5adstore and pull a collection, or add a dataset of your own (step 1, "Without access") |
| saving or purging in the browser says 403 `cross_site`, and `logs api` shows `cross-site request refused: ... Origin='https://host:8443' Host='host'` | a proxy in front of Caddy (nginx, a load balancer) drops the port from `Host` (since 2.47.0 the API compares the browser's `Origin` with `Host`, port included) | in nginx use `proxy_set_header Host $http_host;` (`config/examples/nginx-vhost.*`), or set `mcp.public_base_url` (the `anthive:` block) to the address users type, port included |
| a script's POST says 415 `unsupported_media_type` | since 2.47.0 request bodies must be JSON or a multipart upload | send `Content-Type: application/json` (vark, pyvark and the MCP server do) |
| `up` waits a long time, then "unhealthy" | the API is still scanning a large, mostly remote catalogue | pull the data you serve (step 5), run `up` again; `logs api` shows progress |
| 502 right after `up --no-wait` | the API is still starting | wait a minute |
| logged in but admin pages say 403 | the user is not an admin | basic mode: add to `auth.admins`, `ant-stack init`, `restart caddy`; authelia mode: add `admins` to the user's groups (see "Users and groups") |
| a dataset is listed but its page is very slow | its files are not on this disk | pull that collection with `--include-duckdb` (priority site: `ant sync pull`) |
| `logs api` says "Unknown configuration parameters" and the api restarts over and over | before 2.46.1: `pull` or `check` of a newer release rewrote the config while the old images still ran | run `ant-stack up` (or go back to the old tag and run `ant-stack render`); from 2.46.1 the config is only staged until `up`, and unknown keys are a warning |
| a facet panel says "warming up, try again in a minute" | the api was just restarted and builds its facet caches in the background (a minute or two) | wait; `ant-stack logs api` shows "warm-up: facet caches ..." lines |
| the deposit watcher fails at the rescan, or with PermissionError on `/scratch/...` | it runs with the ingest role's config (container paths, `http://api:8080`) | use `bin/ops/ant-stack watch` (2.46.1) |
| `check`: library_chunks.parquet is missing | the literature index was not downloaded | `bin/ops/ant-stack ingest -- ant sync pull --library-only` |
| `check`: index built with another model | the index and the image use different embedding models | literature search still works by keyword; rebuild the index with `ant lib-index --full` or use the matching release |
| literature search finds nothing in one paper | its PDF has no text layer (a scan) | the index build lists these; rebuild that paper with `ant lib-index --parser docling` (OCR) on a machine with the ingest image |
| upload returns 403 | the user is not in `library.upload_groups` (basic mode: not in `auth.admins`) | add the group to `library.upload_groups` and `ant-stack up`, or give the user the group (see "Users and groups") |
| upload returns 503 with status `disabled` | `services.paper_upload` is off (library mounted read-only) or the checkout is not writable by the site uid | set `services.paper_upload: true` and fix ownership, `ant-stack check`, `ant-stack up` |
| `check` says "host port ... is already in use" on a running site | another program holds the port (`check` skips this test when the site's own containers are up) | find it with `ss -ltnp`, or run `ant-stack check --running` |
| `ant sync pull` says "s3.bucket is not set" | S3 is off for this site | set `s3.enabled: true` and put the keys in `<state_dir>/secrets` (see "S3"), or copy the data by hand |
| upload warns "not a git repository" | `data.library` is a plain folder | clone the library repo there; the uploaded files are kept |
| upload warns "git push failed" | no remote, or the container cannot authenticate | leave `library.git_push: false` and use `ant-stack publish-library` (see "Let people add papers") |
| Authelia log: "has an insecure scheme 'http'" | `tls.mode: off` behind a proxy, and Caddy did not believe the proxy's `X-Forwarded-Proto` | check `tls.trusted_proxies` (default `private_ranges`) and that the proxy sends `X-Forwarded-Proto https`; release 2.41.0 or newer |
| `check` says the app image is missing right after `pull` | before 2.41.0, `pull` used the image tag of the previous render | upgrade the checkout; `pull` now renders first and lists every image it pulled |
| cron or timer job fails with `env: 'uv': No such file or directory` | cron and systemd do not have `~/.local/bin` on PATH | set `PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin` in the crontab or unit (templates in `config/examples/`), or call uv by absolute path; `ant-stack check` shows where uv is |
| `check`: "symlink(s) ... will dangle in the containers" | a store or library symlink points outside every mounted folder | add the target folder to `data.extra_mounts` |
| `publish-library`: "dubious ownership" or permission denied | the command runs as another user than the one owning the library checkout | run it as the `run_as` user (the `User=` of the systemd unit) |
| `publish-library`: git push failed, "Permission denied (publickey)" | the deploy key is not added to the repository with write access, or the remote is HTTPS | add the `.pub` as a write deploy key; `git remote set-url origin git@...` |
| `ant sync library` reports a conflict | the same PDF changed on this site and on S3 | keep one: `ant sync library --prefer local` or `--prefer remote`, then delete the `.conflict-*.pdf` copy |
| papers show but "PDF not available" | the PDFs were not pulled, or the library repo is not checked out at `data.library` | clone the library repo, then `ant sync pull --library-only` |
| anything else | | `bin/ops/ant-stack logs <service>`; the first error line usually names the problem |

---

## Reference

### Login modes (`auth.mode`)

| Mode | Use it when | How people log in |
| --- | --- | --- |
| `authelia` (default) | a normal site | a login page; scripts and vark/pyvark use the same name and password over HTTP basic on `/api` |
| `basic` | a small group or automation only | the browser's own password popup; logging out means closing the browser |
| `trusted-proxy` | your institute's web server already does the login (e.g. university SSO in front) | the upstream proxy; the site must only listen on 127.0.0.1 (`check` enforces it) and use `tls.mode: off` |
| `none` | a public read-only mirror | nobody logs in; admin features are switched off and only `access: public` datasets are served |

Only the Authelia container is extra; it only starts in `authelia` mode.
Personal MCP links work the same in every mode.

### Internal datasets (`auth.internal_requires_auth`)

A dataset whose annotation (`.h5ad.mus` or the `.env` cascade) does not say
`access: public` counts as internal; that includes datasets with no `access`
value at all. With `auth.internal_requires_auth: true` (the default) the API
serves internal datasets only to callers with an identity:

- signed-in users (the `Remote-User` that Authelia, basic auth or the
  trusted upstream proxy sets; the web app and personal MCP links pass it on);
- maintenance tools on the api host or with the internal secret
  (`ant sync`, `ant-refresh-views`, the scoring tools).

Everyone else does not see them: they are missing from every listing
(`/databases`, gene sets `scored_in`, facet results, donor variability,
module variability, paper dataset links, ...) and their routes answer
`404 Database '<id>' not found`, the same as for a dataset the site does not
have. On `auth.mode: none` nobody has an identity, so internal datasets are
hidden from everyone even when they sit in `data.h5adstore`. On
`trusted-proxy` the upstream proxy decides: a vhost without a login must drop
`Remote-User`, `Remote-Groups`, `Remote-Email` and `Remote-Name` (see
`config/examples/nginx-vhost.public.conf.example`), or a visitor could send
them. `X-Remote-User` and `X-Remote-Groups` are never an identity.

`ant-stack check` always prints how many datasets in `data.h5adstore` are
internal and who sees them, in every login mode (for example "97 of 159
datasets are internal: visible only to signed-in users"), and warns when they
could reach people without a login (`internal_requires_auth: false`, or
`trusted-proxy`, where the upstream proxy decides). Set
`internal_requires_auth: false` only for a site whose whole store may be
public; the files themselves stay readable to anyone with shell access to the
host, so keep internal data off a public host.

### Cache warm-up after a start

After every start the api container fills its slowest caches in the
background, at low CPU priority: the facet tables (for signed-in visitors
and, when internal datasets are hidden from them, for anonymous visitors,
because each kind sees other datasets), the per-donor values of TREM2 for
every facet, and the gene set rank tables behind `/genesets/.../rank` and
`/databases/{id}/module-ranks`. `/health` answers at once; a request that
comes first builds what it needs itself. The first pass after a fresh install
builds the rank tables from the score sidecars (up to a minute per dataset,
one at a time) and keeps them in `tmp/module_rank` on the scratch disk
(`scratch_dir`, a docker volume by default), so later starts
only read them. Tune it in `site.yaml` through the `anthive:` escape hatch:

```yaml
anthive:
  warmup:
    module_ranks: priority                 # all (default) | priority | off
    module_rank_datasets: [L26/PSY, S25/V6A]
    donor_genes: [TREM2, APOE]             # [] skips the per-donor step
    anonymous: false                       # login-only site: skip the anonymous caches
```

The facet tables, replication families and dataset summaries are kept on
the scratch disk too, in `tmp/facet_cache` (`/scratch/tmp/facet_cache` in
the containers; the `tmp_dir` setting moves it). A restart whose inputs did
not change loads them in seconds instead of building them again (about
100 s on a full store). Each entry is stored with a key over everything it was
built from: the release (any change of the server code), the facet result
files under `_facet_tests/` (by size and modification time), the cohort table
and every dataset attribute the tables carry. After `ant sync pull` brings a
new result file, the next start rebuilds the tables and only the replication
families that file is part of (for a human post-mortem dataset most of them,
for a xenograft dataset only the xenograft ones); after an upgrade the first start rebuilds
everything once. On a full store the folder takes about 1.5 GB per kind of
visitor (signed-in, anonymous). Nothing needs to be done to keep it right. To
clear it, which only costs one slow start, delete the folder while the stack
runs and restart the api: `bin/ops/ant-stack ingest -- rm -rf
/scratch/tmp/facet_cache`, then `bin/ops/ant-stack restart api` (with
`scratch_dir` set you can also delete `<scratch_dir>/tmp/facet_cache` on the
host). `tmp/module_rank` is cleared the same way. The cache files are
parquet tables with a JSON header, never Python pickles, so a file planted on
the shared scratch volume cannot run code in the api. It can still change
what the facet views show, like any write to the result files, so keep the
scratch volume to the anthive containers.

The anonymous facet tables cost memory in the api container, about as much
as the signed-in ones (above 1 GB on a full store); a site that only serves
signed-in people can set `anonymous: false`. `warmup.enabled: false` (or env
`ANTHIVE_WARMUP=0`) turns the warm-up off. Every key is explained in
`anthive.default.yaml`.

To watch the progress, follow the api log:
`bin/ops/ant-stack logs -f api` prints a `warm-up:` line per step with its
duration (`facet caches for signed-in callers`, the per-donor value sets, `module rank tables
of N datasets ready`). The same lines, with every other ant-serve message, are
in `ant-serve.log` under `logs/` on the scratch disk (`scratch_dir`, or the
docker volume `<site.name>_scratch`). Before 2.45.1 they were only in that
file.

### Certificates (`tls.mode`)

- `internal` (default): the site creates its own certificate authority.
  Browsers warn until you trust it. To trust it, copy the root certificate
  out and install it on the computers that use the site:
  `docker cp <site.name>-caddy-1:/data/caddy/pki/authorities/local/root.crt anthive-root.crt`
- `acme`: a free Let's Encrypt certificate, no warnings. The host name must
  be reachable from the internet on ports 80 and 443, and you set
  `tls.acme_email`.
- `off`: plain HTTP on `ports.http`, only when another web server in front
  of anthive handles HTTPS. That proxy must send `X-Forwarded-Proto` and
  `X-Forwarded-For`; Caddy believes them from the addresses in
  `tls.trusted_proxies` (default with `off`: `private_ranges`, which covers
  a proxy on the same host reaching the container through docker). Keep
  `ports.bind: 127.0.0.1` then, or narrow `tls.trusted_proxies` to the
  proxy's address; `check` warns otherwise.

### Published images

| Image | Contents |
| --- | --- |
| `registry.gitlab.kuleuven.be/bdslab/anthive/anthive4/anthive-app` | every service (API, web, MCP) in one image, ~1.7 GB |
| `registry.gitlab.kuleuven.be/bdslab/anthive/anthive4/anthive-ingest` | the app plus the R/Seurat tools for converting data, ~4.5 GB |

Tags are release numbers (`2.42.1`) plus `latest`. Always pin a release
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
as on the server. Since 2.41.0 the expression layers are read relative to
each `.duckdb`, so a store that moved still works; the same-path mount is
still needed because symlinks in the stores and the library point to
absolute host paths, and `data.extra_mounts` relies on it to make them
resolve inside the containers.

### site.yaml keys

`site.example.yaml` documents every key. The main ones:

| Key | Decides |
| --- | --- |
| `site.public_url` | the address; used for cookies, redirects and MCP links |
| `site.hostnames` | extra names or IPs the site answers to |
| `site.issue_tracker_url` | where the "Report an issue" entry in the user menu points (empty hides it) |
| `site.docs_base_url` | folder the help pages link to for detailed docs such as MCP client setup (default: the anthive4 `doc/` folder on GitLab) |
| `site.facets_visible` | the facets shown on the `/facets` pages and in the dataset page's facet card (default: `ant_sex`, `ant_disease`, `ant_apoe4_carrier`, `ant_braak`, `ant_age`, `ant_app_nlgf`, the six the replicate-level facet tests analyse; `[]` shows all). Other `ant_*` columns stay in the data and the API. Temporary, while the facet audit decisions are pending |
| `data.h5adstore`, `data.genesetstore` | the data folders (absolute paths) |
| `data.library`, `data.library_index` | the paper library checkout and the literature index folder (absolute paths; leave out to run without papers) |
| `data.readonly` | mount the data read-only (a mirror that never annotates) |
| `data.extra_mounts` | absolute host folders bind-mounted read-only at the same path into api, web, mcp and ingest: the targets of symlinks in your stores or library (see "A mirror with a hand-picked subset") |
| `state_dir` | secrets, users, caches and the rendered config |
| `scratch_dir` | disk for temporary DuckDB files and conversions (default: a docker volume) |
| `auth.internal_requires_auth` | whether datasets that are not `access: public` need a signed-in caller (default `true`; see "Internal datasets") |
| `auth.*`, `tls.*`, `ports.*` | see above |
| `run_as` | the user id the containers run as (default: whoever runs ant-stack) |
| `images.*` | registry, image names, tag (the release number; change it at every upgrade) |
| `services.mcp` | personal MCP links for AI clients (on by default; the supported way to use AI with anthive) |
| `ui.facets`, `ui.caveats`, `ui.api`, `ui.mcp` | which parts of the web UI the site shows (all `true` by default; `ui.mcp: false` also stops the mcp service). Caveats carry data-quality warnings: switching them off hides those warnings from visitors. See "A public mirror: switch off facets, caveats, API pages and MCP" |
| `mcp.token_ttl_days`, `mcp.max_links_per_user` | how long a link lives (0 = forever) and how many links one user may hold (default 5) |
| `services.paper_upload` | let `library.upload_groups` add papers through the API and MCP (mounts the library read-write) |
| `services.dataset_deposit` | since 2.46.0: let `datasets.upload_groups` deposit datasets through presigned S3 uploads (needs `s3.enabled`; see "Let depositors add datasets") |
| `datasets.*` | since 2.46.0: who may deposit (`upload_groups`, default admins and curators), `max_gb`, `url_ttl_hours`, `max_open_days` (an open deposit not completed within this many days expires, default 7), `max_open_per_user`, `max_pending_gb_per_user`, `update_policy`, `site_id` (default `site.name`) |
| `ports.tools` | since 2.46.1: the `127.0.0.1` port of Caddy's listener for the host-side deposit watcher (default 8079; published only with `services.dataset_deposit` or `artefacts.enabled`; requires the internal secret) |
| `artefacts.*` | since 2.47.0: artefact and collection deposits (`enabled`, needs `s3.enabled`), `upload_groups` and `collections_create_groups` (default admins and analysts), `max_file_gb`, `max_deposit_gb`, `max_files`, `max_versions`, `max_stored_gb_per_user`, `inline_max_mb`, `hold_public`, `serve_local` (`fallback` or `prefer`), `url_ttl_seconds` (see "Let analysts deposit artefacts") |
| `library.*` | upload groups, PDF size limit, git committer and push, S3 autopush, index publisher, `index_pull` (false: this site never downloads the shared literature index) |
| `library.deploy_key` | SSH key `ant-stack publish-library` pushes the library with (host path) |
| `s3.api_access` | what the api may do on S3: `none` (default), `read` (own read-only key pair) or `write` |
| `tls.trusted_proxies` | addresses whose `X-Forwarded-*` Caddy believes (default `private_ranges` with `tls.mode: off`) |
| `services.chat` | the in-app chat assistant; off by default and being phased out in favour of MCP, leave it off |
| `resources.*` | threads, memory, open-dataset limit, plot cache, catalogue scan |

`ant-stack render` (run by `up`) writes everything derived from `site.yaml`
to `<state_dir>/stack/`. Do not edit those files; they are rewritten on
every `up`. Since 2.46.1 every render goes to `<state_dir>/stack/staging/`
first; `init` and `render` put it in place, `up` does so only when `check`
finds nothing blocking (otherwise the running stack keeps its config), and
`passwd` applies only the login file. `ingest` and `publish-library` apply
only the ingest config, and `watch` only the watcher config: no running
service reads those, and before 2.46.4 S3 keys written after `init` stayed
staged, so the first `ant-stack ingest -- ant sync pull` of a new site failed.
`pull`, `check`, `logs`, `ps` and `restart` only stage, so containers of the
old release never read a config written for the new one.

Who sees which secret:

| Container | Config | From `state_dir` |
| --- | --- | --- |
| api | `stack/anthive-api.yaml` (S3 keys only as `s3.api_access` allows, the presigning pair with `services.dataset_deposit` or `artefacts.enabled`, the artefact read pair, chat key) | read-write, but `secrets/`, `authelia/` and `stack/` are hidden; only `internal_secret` and `mcp_token_secret` come back, read-only |
| mcp | `stack/anthive.yaml` (no keys) | only `secrets/internal_secret`, read-only |
| web | `stack/anthive.yaml` (no keys) | nothing |
| authelia | its own `authelia/` folder | nothing else |
| ingest | `stack/anthive-ingest.yaml` (full S3 keys) | all of it (it is the operator's tool) |

`bin/ops/ant-stack config-path --role ingest` prints the config with the
full S3 keys, for host-side scripts that need it.

For command-line ingest on a cluster, see [HPC.md](HPC.md). For building and
publishing releases, see [RELEASING.md](RELEASING.md). Image internals are in
[DOCKER.md](DOCKER.md).
