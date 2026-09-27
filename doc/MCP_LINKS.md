# Your personal MCP link

Anthive speaks the Model Context Protocol (MCP), so AI clients such as
Claude Code, Claude Desktop, claude.ai, Cursor and VS Code can search the
datasets, look up genes, run read-only SQL and make plots for you.

Every signed-in user gets **one personal link**. Open the account menu in
the web UI and pick **MCP link**. The link looks like

    https://anthive.example.org/mcp/u/amcp_Xy12...._Ab34....

## Treat it like a password

Anyone holding the link acts as you: they see what you can see and their
requests are logged under your name. So:

- do not paste it into shared documents, chats, screenshots or tickets;
- do not commit client config files that contain it;
- if you think it leaked, press **Rotate** on the MCP link page. The old
  link stops working within half a minute and you get a new one. **Revoke**
  switches your link off until you rotate again.

Links expire after 90 days by default (the page shows the date); rotating
starts a new period.

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

All tools are read-only. Admin-only tools (`analytics_viz`, which runs
Python on the server, and `rescan_databases`) only appear for members of
the `admins` group.

## For operators

- Config lives under `mcp:` in `anthive.yaml` (see `anthive.default.yaml`):
  `enabled`, `token_ttl_days`, `public_base_url`, `rate_limit_per_minute`,
  `verify_cache_seconds`, `token_secret`.
- Tokens are derived from a server secret (HMAC). The link table in
  `<state_dir>/mcp_tokens.db` never holds a working token. The secret is
  `mcp.token_secret`, env `ANTHIVE_MCP_TOKEN_SECRET`, or an auto-created
  `<state_dir>/secrets/mcp_token_secret` (mode 0600). Changing it
  invalidates every link at once (the emergency "rotate everything" switch).
- Admins can list links (`GET /api/admin/mcp-links`) and revoke anyone's
  (`DELETE /api/admin/mcp-links/<user>`).
- Reverse proxy requirements are in `doc/DEPLOY.md` section 7a.
