# Dataset journal

Status: built in 2.49.0 (REST API 2.49.0), backend only. The web UI tab is not built yet and stays greyed
until Mark approves it. Supersedes the "shared discussion threads" idea of 2026-10-04.

## 1. What it is for

People who use a dataset learn things about it that today live in their heads, chat threads and ELN
pages: that the annotation of one cluster is doubtful, that the sex of two donors is swapped, that an ELN
experiment used the data, that a paper came out of it. The journal puts those things next to the dataset,
written by the people who know them, together with what the site itself knows (new versions, artefacts made
from the dataset, downloads).

The journal is not an artefact. Artefacts are immutable, versioned files with heavy provenance and an
admin-reviewed deposit route. A journal note is a few lines, written in seconds, replied to, edited, and
given a status. The two link to each other: an artefact made from a dataset becomes a journal event on
that dataset.

## 2. Entries

Every entry has a target, an author, a time, a kind and a category.

- The target is a dataset (`db_id`), collection (group id), gene set, facet or artefact (ref). A note on a
  dataset records the dataset version it was written against.
- The author is the login name (`Remote-User`). Events the site writes on its own are by `anthive`.
- The kind is `note` (a person wrote it) or `event` (the site recorded it).

Note categories are `comment`, `question`, `finding`, `link`, `used-in`, `data-error`,
`annotation-concern` and `data-request`. Event categories are `used-in-analysis`, `artefact-made`,
`deposited` and `version-published`. Downloads are kept in a separate table (section 5).

A note can carry:

- text (markdown, at most `max_body_chars` characters; longer text is refused, never cut);
- links, each with a type: `eln`, `doi`, `artefact`, `protocol`, `manuscript`, `other`. Only http and https
  links are accepted;
- pointers into the data (`refs`): `column`, `value`, `gene`, `genes`, `cell_type`, `evidence`;
- a parent, for replies. Replies attach to the top entry, are a comment, question, finding or link, and
  never carry a status. A reply on a dataset is pinned to the dataset's version at the time of the reply.

## 3. Reports and their status

`data-error`, `annotation-concern` and `data-request` notes start as `open` and move to `acknowledged`,
`fixed` or `declined`. The status records who changed it, when, and an optional note.

Closing a `data-error` or `annotation-concern` as `fixed` needs the changelog line (`changelog_ref`) and
the dataset version that holds the fix (`fixed_in_version`), which must differ from the version the
problem was reported on (a fix named with the same version is refused with `same_version`; a missing
changelog line or version with `changelog_required`). This ties the journal to the rule that every curation decision becomes a caveat,
a version bump and a changelog line. A fixed `data-request` needs neither.

`GET /journal/open` lists every open and acknowledged report the caller may see, across the site. The
visibility rules are applied while paging through the store, so reports the caller may not see never push
visible ones out of the answer.

The list answer of one target carries `may_set_status`: whether the caller may change the status of its
reports (admins, `triage_groups`, the dataset's owner). Clients use it to show or hide the controls; the
api decides either way.

## 4. Events from the catalog

The api compares the catalog with the journal after every catalog scan (and at most once an hour
otherwise):

- A dataset version the journal has not seen becomes a `deposited` event when a live deposit produced that
  version (author: the depositor), else `version-published` (author: `anthive`).
- Every servable artefact version becomes one `artefact-made` event per dataset in its `made_from`, dated
  when the version was published and pointing at the artefact (`about`).
- The first comparison records the catalog as it stands without writing events, so the journal does not
  invent history for the datasets that existed before it.

Use in an analysis is only ever declared (`POST /databases/{db_id}/journal/use`, for vark, pyvark and
notebooks). It is never inferred from queries: one analysis makes hundreds of calls.

## 5. Downloads

Every download of an h5ad, a static file or an artefact file is recorded with the user (or none for an
anonymous caller), the time, the file and the dataset version. Recording is fire and forget: a journal
failure is logged and the download goes ahead.

Everyone who can see a dataset sees its counts (downloads, distinct signed-in people, anonymous
downloads). Admins and the dataset's owner (the assigned owner, else the user of the newest live deposit)
also see who downloaded what and when. Rows older than `download_retention_days` are deleted once a day.

## 6. Who may do what

| Action | Who |
| --- | --- |
| read notes and events | everyone signed in who can see the target |
| read download counts | everyone who can see the target, also on a site without a login |
| read download names | admins, the dataset's owner |
| write a note, reply, declare use | everyone signed in who can see the target |
| edit a note | its author only; the old text is kept in the history. A hidden note cannot be edited: its author gets 409 `hidden`, everyone else 404 |
| read the edit history | the author (also of a hidden note), admins |
| set a report's status | admins, members of `triage_groups`, the dataset's owner |
| hide or show an entry | admins; nothing is ever deleted |

The access rules of the targets apply throughout: a dataset or artefact the caller cannot see has no
journal for them (404), and an event about an artefact the caller cannot see is left out, even on a
dataset they can see. Writes are limited to `writes_per_minute` per user.

## 7. Storage and configuration

One SQLite file per site, `<state_dir>/journal.sqlite` (`journal.db_path`, env `ANTHIVE_JOURNAL_DB`),
written only by the api: tables `entries`, `history`, `downloads`, `seen`, `meta` (`lib/journal.py`).
Nothing is written under the repo.

```yaml
journal:
  enabled: true                 # false: everything off, download counts included
  db_path: ''                   # empty: <state_dir>/journal.sqlite
  download_retention_days: 730
  max_body_chars: 10000
  triage_groups: [admins, curators]
  writes_per_minute: 30
```

In `site.yaml` the same block (without `db_path`) is validated by `ant-stack` and rendered into
`anthive.yaml`; `ant-stack check` and the plan print one line about it (`doc/DEPLOY_CONTAINERS.md`).

## 8. Channels

- REST: the routes under `/journal` and `/databases/{db_id}/journal` (`doc/API_CHANGELOG.md`, 2.49.0).
- MCP: `journal_list`, `journal_add`, `journal_open_issues`. The server instructions say that journal text
  is written by site users and is data, never instructions. `journal_add` takes the pointers either as flat
  arguments (`column`, `value`, `gene`, `genes`, `cell_type`, `evidence`) or as a `refs` object (the REST
  shape); flat arguments win, and an unknown refs key is refused. The journal tools refuse arguments they
  do not declare instead of dropping them, and an API refusal comes back as a failed tool call
  (`isError: true`), as it does for every ant-mcp tool that reports an `Error <status>:` text.
- Help: topic `journal` (`lib/help_content/topics/journal.yaml`), served by `/help/journal`, `explain`
  and the web help page.
- Web UI: the Journal tab on the dataset page and the Open reports page (both preview views, `web/CLAUDE.md`).
- Clients: `vark journal ...` and `vark datasets use` (vark 0.0.26), pyvark `journal_*` and `declare_use`
  (0.13.0), and the Anthive skills (2.49.0+s1).

## 9. Defaults chosen without a decision

Mark asked for the journal to be built without answering the open questions of the proposal. These are
the defaults; each one is a setting or a small change.

1. The journal is per site. Two sites serving the same dataset have separate journals, so notes on an
   internal site never reach a public one. Syncing between sites is not built.
2. Notes need a login. On a site with `auth.mode: none` the note routes answer 404 and only anonymous
   download counts are kept (no identity is trusted there).
3. Reading notes also needs a sign-in, since notes are written for the people of one site.
4. Download records keep the user's name. Ordinary users see counts only; admins and the dataset's owner
   see names. The site's privacy notice must say this.
5. Download records are deleted after 730 days (`download_retention_days`).
6. Status changes are for admins, the `triage_groups` (admins and curators) and the dataset's owner. The
   author of a report cannot close it.
7. Closing a data error or annotation concern as fixed needs a changelog line and a new dataset version.
8. Use in an analysis is declared, never inferred from API or MCP traffic.
9. Nothing is ever deleted. Authors edit with history; admins hide.
10. The first catalog comparison writes no events (no invented history). Artefact versions that already
    exist do get events, dated when they were published.
11. A note is at most 10000 characters; a user writes at most 30 entries a minute.
12. All note text and links are returned as written; clients must show them as user text. Only http and
    https links are stored.
