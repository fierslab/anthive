# Deploy an anthive site

This is the one supported way to run anthive: a docker compose stack,
described by one file (`site.yaml`) and driven by one tool
(`bin/ops/ant-stack`). Follow the steps in order. Every command below was run
end to end on a fresh checkout for release 2.41.0.

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
- [ ] Read access to the paper library repo
      (`gitlab.kuleuven.be/bdslab/anthive/library_store`, private; it holds
      the paper records, the PDFs come from S3). Optional: without it the
      site has no papers, literature search or paper context for AI clients.
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
git checkout v2.42.0
```

Always check out a release tag, and use the same number as the image tag in
step 4. The checkout only provides `ant-stack` and its templates; the
programs themselves run from the images.

---

## 3. Get the catalogue, gene sets and paper library

```sh
cd /srv/anthive
git clone git@gitlab.kuleuven.be:bdslab/anthive/h5adstore.git
git clone git@gitlab.kuleuven.be:bdslab/genesetdbs/anthive-genesetdb.git genesetstore
git clone git@gitlab.kuleuven.be:bdslab/anthive/library_store.git library          # optional, see step 1
mkdir -p library_index
```

`h5adstore/` now contains one folder per collection with small text files.
`library/papers/` holds one `.md` record per paper (`doc/LIBRARY.md`).
The actual data (`.h5ad`, `.duckdb`, parquet), the paper PDFs and the
precomputed literature index are downloaded in step 5.

The library repository (`bdslab/anthive/library_store`) is private: ask the
maintainers for read access. It holds only the paper records (`.md`); the
PDFs come from S3 in step 5. Without access, leave the two library lines out
of `site.yaml` in step 4: the site runs without papers.

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
  library: /srv/anthive/library               # the paper library checkout, or leave out
  library_index: /srv/anthive/library_index   # literature index (filled in step 5)

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
  tag: 2.42.0                                 # same number as the git tag

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
lists each `PROBLEM` with the fix. Repeat until it says OK. A `WARNING` about
datasets without a local `.duckdb` is expected at this point; step 5 fixes it.

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
and the site has nothing it can serve quickly. A dataset whose files are not
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
| literature index (`library_chunks.parquet` + `.json`) | S3, under `_library_index/` | `ant sync` (downloaded by every site; uploaded only by the site with `library_index_publisher: true`) |

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
| publish uploaded papers | `bin/ops/ant-stack publish-library` (better: from a timer, see "Let people add papers") |

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

Then `bin/ops/ant-stack init` (writes the Authelia user database) and
`bin/ops/ant-stack up`. Authelia picks up the new group within a few
minutes, at the next login at the latest. In `auth.mode: basic` there are no groups: only
`auth.admins` can upload.

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

### Upgrade to a new release

```sh
cd /srv/anthive/anthive4
git fetch --tags
git checkout vX.Y.Z             # the new release
nano site.yaml                  # images: tag: X.Y.Z
bin/ops/ant-stack pull
bin/ops/ant-stack up
```

Run `bin/ops/ant-stack check` before `up`: it flags new settings a release
needs. Users, passwords, MCP links and caches live in `state_dir` and carry
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
| `up` waits a long time, then "unhealthy" | the API is still scanning a large, mostly remote catalogue | pull the data you serve (step 5), run `up` again; `logs api` shows progress |
| 502 right after `up --no-wait` | the API is still starting | wait a minute |
| logged in but admin pages say 403 | the user is not an admin | add to `auth.admins` and `groups: [admins]`, `ant-stack init` |
| a dataset is listed but its page is very slow | its files are not on this disk | pull that collection with `--include-duckdb` |
| `check`: library_chunks.parquet is missing | the literature index was not downloaded | `bin/ops/ant-stack ingest -- ant sync pull --library-only` |
| `check`: index built with another model | the index and the image use different embedding models | literature search still works by keyword; rebuild the index with `ant lib-index --full` or use the matching release |
| literature search finds nothing in one paper | its PDF has no text layer (a scan) | the index build lists these; rebuild that paper with `ant lib-index --parser docling` (OCR) on a machine with the ingest image |
| upload returns 403 | the user is not in `library.upload_groups` (basic mode: not in `auth.admins`) | add the group or the user, `ant-stack init` |
| upload returns 503 with status `disabled` | `services.paper_upload` is off (library mounted read-only) or the checkout is not writable by the site uid | set `services.paper_upload: true` and fix ownership, `ant-stack check`, `ant-stack up` |
| `check` says "host port ... is already in use" on a running site | another program holds the port (`check` skips this test when the site's own containers are up) | find it with `ss -ltnp`, or run `ant-stack check --running` |
| `ant sync pull` says "s3.bucket is not set" | S3 is off for this site | set `s3.enabled: true` and put the keys in `<state_dir>/secrets` (see "S3"), or copy the data by hand |
| upload warns "not a git repository" | `data.library` is a plain folder | clone the library repo there; the uploaded files are kept |
| upload warns "git push failed" | no remote, or the container cannot authenticate | leave `library.git_push: false` and use `ant-stack publish-library` (see "Let people add papers") |
| Authelia log: "has an insecure scheme 'http'" | `tls.mode: off` behind a proxy, and Caddy did not believe the proxy's `X-Forwarded-Proto` | check `tls.trusted_proxies` (default `private_ranges`) and that the proxy sends `X-Forwarded-Proto https`; release 2.41.0 or newer |
| `check` says the app image is missing right after `pull` | before 2.41.0, `pull` used the image tag of the previous render | upgrade the checkout; `pull` now renders first and lists every image it pulled |
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

Tags are release numbers (`2.42.0`) plus `latest`. Always pin a release
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
| `site.issue_tracker_url` | where the "Report an issue" entry in the user menu points (empty hides it) |
| `site.docs_base_url` | folder the help pages link to for detailed docs such as MCP client setup (default: the anthive4 `doc/` folder on GitLab) |
| `data.h5adstore`, `data.genesetstore` | the data folders (absolute paths) |
| `data.library`, `data.library_index` | the paper library checkout and the literature index folder (absolute paths; leave out to run without papers) |
| `data.readonly` | mount the data read-only (a mirror that never annotates) |
| `state_dir` | secrets, users, caches and the rendered config |
| `scratch_dir` | disk for temporary DuckDB files and conversions (default: a docker volume) |
| `auth.*`, `tls.*`, `ports.*` | see above |
| `run_as` | the user id the containers run as (default: whoever runs ant-stack) |
| `images.*` | registry, image names, tag |
| `services.mcp` | personal MCP links for AI clients (on by default; the supported way to use AI with anthive) |
| `mcp.token_ttl_days`, `mcp.max_links_per_user` | how long a link lives (0 = forever) and how many links one user may hold (default 5) |
| `services.paper_upload` | let `library.upload_groups` add papers through the API and MCP (mounts the library read-write) |
| `library.*` | upload groups, PDF size limit, git committer and push, S3 autopush, index publisher |
| `library.deploy_key` | SSH key `ant-stack publish-library` pushes the library with (host path) |
| `s3.api_access` | what the api may do on S3: `none` (default), `read` (own read-only key pair) or `write` |
| `tls.trusted_proxies` | addresses whose `X-Forwarded-*` Caddy believes (default `private_ranges` with `tls.mode: off`) |
| `services.chat` | the in-app chat assistant; off by default and being phased out in favour of MCP, leave it off |
| `resources.*` | threads, memory, open-dataset limit, plot cache, catalogue scan |

`ant-stack render` (run by `up`) writes everything derived from `site.yaml`
to `<state_dir>/stack/`. Do not edit those files; they are rewritten on
every `up`.

Who sees which secret:

| Container | Config | From `state_dir` |
| --- | --- | --- |
| api | `stack/anthive-api.yaml` (S3 keys only as `s3.api_access` allows, chat key) | read-write, but `secrets/`, `authelia/` and `stack/` are hidden; only `internal_secret` and `mcp_token_secret` come back, read-only |
| mcp | `stack/anthive.yaml` (no keys) | only `secrets/internal_secret`, read-only |
| web | `stack/anthive.yaml` (no keys) | nothing |
| authelia | its own `authelia/` folder | nothing else |
| ingest | `stack/anthive-ingest.yaml` (full S3 keys) | all of it (it is the operator's tool) |

`bin/ops/ant-stack config-path --role ingest` prints the config with the
full S3 keys, for host-side scripts that need it.

For command-line ingest on a cluster, see [HPC.md](HPC.md). For building and
publishing releases, see [RELEASING.md](RELEASING.md). Image internals are in
[DOCKER.md](DOCKER.md).
