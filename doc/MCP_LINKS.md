# Your personal MCP links

Anthive speaks the Model Context Protocol (MCP), so AI clients such as
Claude Code, Claude Desktop, claude.ai, Cursor and VS Code can search the
datasets, look up genes, run read-only SQL and make plots for you.

Every signed-in user can make **personal links**, one per client (a laptop,
Claude Desktop, a notebook server), up to five by default. Open the account
menu in the web UI: the entry reads **Connect an MCP client** until you have
a link and **MCP links** afterwards. Give each link a name so you know which
client uses it. A link looks like

    https://anthive.example.org/mcp/u/amcp_Xy12...._Ab34....

## Treat it like a password

Anyone holding the link acts as you: they see what you can see and their
requests are logged under your name. So:

- do not paste it into shared documents, chats, screenshots or tickets;
- do not commit client config files that contain it;
- if you think one leaked, press **Rotate** next to it on the MCP links
  page. That link's old URL stops working within half a minute and you get
  a new one; your other links keep working. **Revoke** removes a link for
  good (make a new one if you need it again).

Links expire after 90 days by default (the page shows the date); rotating
or renewing starts a new period.

## Connect a client

**Claude Code**

    claude mcp add --transport http anthive <your link>

**Claude Desktop / claude.ai**: Settings, Connectors, Add custom
connector. Paste the link as the server URL and leave the OAuth fields
empty.

**Cursor** (`~/.cursor/mcp.json`) and **VS Code** (`.vscode/mcp.json`, key
`servers` instead of `mcpServers`):

```json
{
  "mcpServers": {
    "anthive": {
      "type": "http",
      "url": "<your link>"
    }
  }
}
```

**Clients that support custom headers** can keep the token out of the
URL. Use `https://anthive.example.org/mcp` as the URL and send

    Authorization: Bearer amcp_Xy12...._Ab34....

(the part after `/mcp/u/`). Claude Code:

    claude mcp add --transport http anthive https://anthive.example.org/mcp \
        --header "Authorization: Bearer amcp_..."

## What you can do with it

Data tools: `search_datasets`, `get_dataset_info`, `search_genes`, `plot`,
`analytics_schema`, `analytics_query`, `module_score`, `list_module_scores`,
`list_genesets`, `get_geneset`.

Literature tools (2.39): `search_literature` (full-text passages from the
library papers, with citation, page and section), `get_paper` (a paper's
record, summary, topics and the datasets it is the source of),
`related_papers` (same cohort, cell type, method, shared authors,
citations) and `get_dataset_context` (background pack to load before
analysing a dataset).

Prompts, offered by clients that support them (Claude Code shows them as
`/mcp__anthive__...` commands): `investigate_dataset(db_id)` and
`literature_in_our_data(topic)`. Resources: `anthive://guide` (how to use
Anthive well) and `anthive://dataset/<group>/<file>/context`.

Papers marked `anthive_llm: never` in the library never reach an MCP client;
`summary_only` papers show their summary but no passages or page text.

Paper upload tools (2.40): `paper_upload_requirements()` (which fields a
paper record needs, with examples and the allowed values),
`check_paper(metadata)` (a dry run: errors, missing fields, what CrossRef
and OpenAlex would fill in, duplicates, the citekey and folder it would get)
and `upload_paper(metadata, pdf_base64=None, folder=None, update=False)`.
`check_paper` and `upload_paper` only appear for members of
`library_upload_groups` (default: admins). The PDF goes base64-encoded in the
call and is capped at `mcp.max_upload_mb` (default 25 MB). The paper is
committed to the library with you as the author (`doc/LIBRARY.md`).

Apart from `upload_paper`, all tools are read-only. Admin-only tools
(`analytics_viz`, which runs Python on the server, and `rescan_databases`)
only appear for members of the `admins` group.

## For operators

- Config lives under `mcp:` in `anthive.yaml` (see `anthive.default.yaml`):
  `enabled`, `token_ttl_days`, `max_links_per_user` (default 5),
  `public_base_url`, `rate_limit_per_minute`, `verify_cache_seconds`,
  `token_secret`. Container sites set `mcp.token_ttl_days` and
  `mcp.max_links_per_user` in `site.yaml`.
- Tokens are derived from a server secret (HMAC). The link table in
  `<state_dir>/mcp_tokens.db` never holds a working token. The secret is
  `mcp.token_secret`, env `ANTHIVE_MCP_TOKEN_SECRET`, or an auto-created
  `<state_dir>/secrets/mcp_token_secret` (mode 0600). Changing it
  invalidates every link at once (the emergency "rotate everything" switch).
- REST: `GET /api/me/mcp` (your links, plus the single-link fields of
  API 2.37 for old clients), `POST /api/me/mcp/links {"label": ...}`,
  `POST /api/me/mcp/links/<id>/rotate`, `DELETE /api/me/mcp/links/<id>`.
  The 2.37 calls `POST /api/me/mcp/rotate` (your first link) and
  `DELETE /api/me/mcp` (all your links) still work.
- Admins see every link in the "All users' links" section of the MCP links
  page (`GET /api/admin/mcp-links`, one row per link) and can revoke one
  (`DELETE /api/admin/mcp-links/<user>/<id>`) or all of a user's links
  (`DELETE /api/admin/mcp-links/<user>`).
- A database from release 2.37 or earlier (one link per user) is converted
  in place the first time 2.38 opens it; existing links keep working and
  are labelled "default".
- Reverse proxy requirements are in `doc/DEPLOY.md` section 7a.
