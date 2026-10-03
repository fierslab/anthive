# Connecting an AI client to anthive

Anthive is a remote MCP server (Model Context Protocol, Streamable HTTP
transport). Most AI tools that can use MCP can talk to it. This page lists
the setup for each client we know of. It is kept on GitLab so it can be
corrected without a new anthive release; the in-app help links here.

Every recipe was checked against the vendor's own documentation on
2026-10-02, with the source linked under each heading. Clients move fast.
If a menu name or config key has changed, the vendor link is the authority,
and a note in the [issue tracker](https://gitlab.kuleuven.be/bdslab/anthive/anthive4/-/issues)
helps us fix this page.

**What leaves the server.** Anything anthive returns to your AI client,
including text from papers and datasets, is sent to that client's AI
provider. Choose a client and provider you trust with that material.

## Before you start

1. **Get a personal link.** Sign in to your anthive site, open the account
   menu and choose **Connect an MCP client** (or **MCP links** once you have
   one). Make one link per client. Details are in [MCP_LINKS.md](MCP_LINKS.md).
2. **Treat the link as a password.** It identifies you. Do not commit a
   config file that contains it, and do not paste it into shared chats.
3. **Pick the form your client supports.** Every client below accepts the
   first; the second keeps the token out of the URL.

   | Form | URL | Extra header |
   | --- | --- | --- |
   | Personal link | `https://<your-anthive-site>/mcp/u/amcp_XXXX` | none |
   | Header | `https://<your-anthive-site>/mcp` | `Authorization: Bearer amcp_XXXX` |

   `amcp_XXXX` is the part of your link after `/mcp/u/`. In the examples
   below, replace `https://<your-anthive-site>/mcp/u/amcp_XXXX` with your
   own link.

4. **Name the server `anthive`.** Any name works, but the anthive skills and
   prompts assume this one.

Anthive does not use OAuth. When a client asks which sign-in method to use,
pick "none", "No sign in" or "No authentication". If a client starts an
OAuth flow anyway, cancel it or use the header form.

### Cloud connectors need a public site

claude.ai, the Claude Desktop and mobile apps (custom connectors) and ChatGPT
(developer mode) do not connect from your computer. Their servers call
anthive from the vendor's cloud. A site that is only reachable on a campus
network or over a VPN will not work with them. Use a client that runs on
your own machine instead (Claude Code, Codex, Gemini CLI, Cursor, VS Code,
OpenCode and most others below), or the [mcp-remote bridge](#any-other-client-the-mcp-remote-bridge)
for Claude Desktop.

## Quick reference

| Client | How | Key detail |
| --- | --- | --- |
| [Claude Code](#claude-code) | `claude mcp add --transport http anthive <link>` | `--scope user` for all projects |
| [claude.ai / Claude Desktop](#claudeai-and-claude-desktop) | Customize > Connectors > Add custom connector | public site only |
| [OpenAI Codex](#openai-codex) | `codex mcp add anthive --url <link>` | `~/.codex/config.toml` |
| [ChatGPT](#chatgpt) | Developer mode app | public site only, no headers |
| [Gemini CLI](#gemini-cli) | `gemini mcp add --scope user --transport http anthive <link>` | JSON key is `httpUrl` |
| [OpenCode](#opencode) | `opencode.json` | `"type": "remote"`, `"oauth": false` |
| [Cursor](#cursor) | `~/.cursor/mcp.json` | key `url` |
| [VS Code / Copilot](#vs-code-and-github-copilot) | `.vscode/mcp.json` | top key is `servers` |
| [Windsurf / Devin Desktop](#windsurf-devin-desktop) | `mcp_config.json` | key `serverUrl` |
| [Zed](#zed) | `settings.json` | top key is `context_servers` |
| [Cline](#cline) | MCP Servers panel | `"type": "streamableHttp"` |
| [Roo Code](#roo-code) | `.roo/mcp.json` | `"type": "streamable-http"` |
| [Continue](#continue) | `config.yaml` | `type: streamable-http` |
| [Goose](#goose) | `goose configure` or `config.yaml` | `uri`, `streamable_http` |
| [Amp](#amp) | `amp mcp add anthive <link>` | key `amp.mcpServers` |
| [Kiro](#kiro) | `~/.kiro/settings/mcp.json` | key `url` |
| [Warp](#warp) | Settings > Agents > MCP servers | key `url` |
| [JetBrains AI Assistant](#jetbrains-ai-assistant) | Settings > Tools > AI Assistant > MCP | key `url` |
| [JetBrains Junie](#jetbrains-junie) | `~/.junie/mcp/mcp.json` | key `url` |
| [LM Studio](#lm-studio) | Program tab > Edit mcp.json | key `url` |
| [Cherry Studio](#cherry-studio) | Settings > MCP Servers | `"type": "streamableHttp"` |
| [Pi](#pi) | `~/.pi/agent/mcp.json` | key `url` |
| [Anything else](#any-other-client-the-mcp-remote-bridge) | `npx mcp-remote <link>` | needs Node.js |

---

## Claude Code

Source: <https://code.claude.com/docs/en/mcp>

```bash
claude mcp add --transport http anthive https://<your-anthive-site>/mcp/u/amcp_XXXX
```

That adds anthive for the current project only. Add `--scope user` to have
it in every project. Avoid `--scope project`: it writes `.mcp.json`, which is
meant to be committed.

Header form:

```bash
claude mcp add --transport http anthive https://<your-anthive-site>/mcp \
  --header "Authorization: Bearer amcp_XXXX"
```

In a shared `.mcp.json`, keep the token in an environment variable:

```json
{
  "mcpServers": {
    "anthive": {
      "type": "http",
      "url": "https://<your-anthive-site>/mcp",
      "headers": { "Authorization": "Bearer ${ANTHIVE_MCP_TOKEN}" }
    }
  }
}
```

Check with `claude mcp list`, or `/mcp` inside a session. The anthive prompts
appear as `/mcp__anthive__investigate_dataset` and friends.

## claude.ai and Claude Desktop

Source: <https://support.claude.com/en/articles/11175166-getting-started-with-custom-connectors-using-remote-mcp>

A custom connector is set up once in claude.ai and then also shows up in
Claude Desktop and the mobile apps. **Your anthive site must be reachable
from the internet** (see [above](#cloud-connectors-need-a-public-site)).

- **Free, Pro, Max**: Customize > Connectors > **+** Add > Add custom
  connector. Name `anthive`, URL your personal link, authentication **No
  sign in**. The Free plan allows one custom connector.
- **Team, Enterprise**: an owner adds the connector under Organization
  settings > Connectors. Because a personal link belongs to one person, each
  member then needs their own connector entry or the header form with their
  own token.

Then enable the connector in a chat from the tools menu.

`claude_desktop_config.json` only takes local (stdio) servers. To use a
remote anthive from that file, or to reach a site that is not public, use
the [mcp-remote bridge](#any-other-client-the-mcp-remote-bridge): it runs on
your machine.

## OpenAI Codex

Source: <https://developers.openai.com/codex/mcp>

The CLI, the desktop app and the IDE extension share `~/.codex/config.toml`
(or `.codex/config.toml` in a project).

```bash
codex mcp add anthive --url https://<your-anthive-site>/mcp/u/amcp_XXXX
```

Or by hand:

```toml
[mcp_servers.anthive]
url = "https://<your-anthive-site>/mcp/u/amcp_XXXX"
```

Header form, with the token in your shell environment
(`export ANTHIVE_MCP_TOKEN=amcp_XXXX`):

```toml
[mcp_servers.anthive]
url = "https://<your-anthive-site>/mcp"
bearer_token_env_var = "ANTHIVE_MCP_TOKEN"
```

Check with `codex mcp list`, or `/mcp` in a session.

## ChatGPT

Source: <https://developers.openai.com/api/docs/guides/developer-mode>

Needs a Plus, Pro, Business, Enterprise or Education plan, the web app, and
a **public** anthive site.

1. Settings > Security and login > turn on **Developer mode**.
2. In the apps area, use **+** to create an app. URL your personal link,
   authentication **No authentication**.

ChatGPT does not document custom headers, so use the personal link.

## Gemini CLI

Source: <https://geminicli.com/docs/tools/mcp-server/>

```bash
gemini mcp add --scope user --transport http anthive https://<your-anthive-site>/mcp/u/amcp_XXXX
```

Without `--scope user` it goes into the project's `.gemini/settings.json`.
By hand, in `~/.gemini/settings.json`:

```json
{ "mcpServers": { "anthive": { "httpUrl": "https://<your-anthive-site>/mcp/u/amcp_XXXX" } } }
```

Use `httpUrl`, not `url`: in Gemini CLI `url` means the older SSE transport.
Header form: `"httpUrl": "https://<your-anthive-site>/mcp"` plus
`"headers": { "Authorization": "Bearer amcp_XXXX" }`. Check with `/mcp`.

## OpenCode

Source: <https://opencode.ai/docs/mcp-servers/>

In `opencode.json` (project) or your global OpenCode config:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "anthive": {
      "type": "remote",
      "url": "https://<your-anthive-site>/mcp/u/amcp_XXXX",
      "enabled": true,
      "oauth": false
    }
  }
}
```

`"oauth": false` stops OpenCode from trying to sign in. Header form: add
`"headers": { "Authorization": "Bearer amcp_XXXX" }`.

## Cursor

Source: <https://cursor.com/docs/context/mcp>

`~/.cursor/mcp.json` for all projects, `.cursor/mcp.json` for one:

```json
{ "mcpServers": { "anthive": { "url": "https://<your-anthive-site>/mcp/u/amcp_XXXX" } } }
```

Header form: add `"headers": { "Authorization": "Bearer amcp_XXXX" }`.
Cursor expands `${env:ANTHIVE_MCP_TOKEN}` inside the config. The server shows
up under Settings > MCP, where you can switch it on and off.

## VS Code and GitHub Copilot

Sources: <https://code.visualstudio.com/docs/copilot/customization/mcp-servers>,
<https://code.visualstudio.com/docs/copilot/reference/mcp-configuration>

Either run **MCP: Add Server** from the command palette and follow the
prompts (choose HTTP), or edit `.vscode/mcp.json` in a workspace. For all
workspaces use **MCP: Open User Configuration**. The top-level key is
`servers`, not `mcpServers`:

```json
{ "servers": { "anthive": { "type": "http", "url": "https://<your-anthive-site>/mcp/u/amcp_XXXX" } } }
```

To keep the token out of the file, let VS Code ask for it once:

```json
{
  "inputs": [
    { "type": "promptString", "id": "anthive-token", "description": "anthive MCP token (amcp_...)", "password": true }
  ],
  "servers": {
    "anthive": {
      "type": "http",
      "url": "https://<your-anthive-site>/mcp",
      "headers": { "Authorization": "Bearer ${input:anthive-token}" }
    }
  }
}
```

The tools appear in Copilot Chat in agent mode.

## Windsurf (Devin Desktop)

Source: <https://docs.devin.ai/desktop/cascade/mcp>

Edit `mcp_config.json` (on macOS and Linux `~/.config/devin/mcp_config.json`,
on Windows `%APPDATA%\devin\mcp_config.json`; older Windsurf builds used
`~/.codeium/windsurf/mcp_config.json`, which we could not confirm for
current releases):

```json
{ "mcpServers": { "anthive": { "serverUrl": "https://<your-anthive-site>/mcp/u/amcp_XXXX" } } }
```

Headers and `${env:VAR}` are supported. Refresh the MCP list in Cascade
after editing.

## Zed

Source: <https://zed.dev/docs/ai/mcp>

Settings > AI > MCP Servers > Add Remote Server, or in `settings.json`:

```json
{ "context_servers": { "anthive": { "url": "https://<your-anthive-site>/mcp/u/amcp_XXXX" } } }
```

Without an `Authorization` header Zed offers an OAuth sign-in, which anthive
does not have. Skip it, or use the header form to avoid the prompt:
`"url": "https://<your-anthive-site>/mcp"` plus
`"headers": { "Authorization": "Bearer amcp_XXXX" }`.

## Cline

Source: <https://docs.cline.bot/mcp/configuring-mcp-servers>

In the MCP Servers panel of the extension (or `~/.cline/mcp.json` for the
CLI). Set the type explicitly; Cline otherwise assumes the old SSE
transport:

```json
{ "mcpServers": { "anthive": { "type": "streamableHttp", "url": "https://<your-anthive-site>/mcp/u/amcp_XXXX", "disabled": false } } }
```

## Roo Code

Source: <https://roocodeinc.github.io/Roo-Code/features/mcp/using-mcp-in-roo>

"Edit Global MCP" (`mcp_settings.json`) or `.roo/mcp.json` in a project:

```json
{ "mcpServers": { "anthive": { "type": "streamable-http", "url": "https://<your-anthive-site>/mcp/u/amcp_XXXX" } } }
```

Headers are supported.

## Continue

Source: <https://docs.continue.dev/customize/deep-dives/mcp>

In `config.yaml`, or a file of its own under `.continue/mcpServers/`:

```yaml
mcpServers:
  - name: anthive
    type: streamable-http
    url: https://<your-anthive-site>/mcp/u/amcp_XXXX
```

Continue's docs do not show an MCP example with headers, so use the personal
link.

## Goose

Source: <https://goose-docs.ai/docs/getting-started/using-extensions/>

CLI: `goose configure`, then Add Extension > Remote Extension (Streamable
HTTP). Desktop: Extensions > Add custom extension. Or in
`~/.config/goose/config.yaml` (note `uri`, not `url`, and the underscore in
`streamable_http`):

```yaml
extensions:
  anthive:
    name: anthive
    type: streamable_http
    uri: https://<your-anthive-site>/mcp/u/amcp_XXXX
    timeout: 300
    enabled: true
```

A `headers:` map is supported for the header form.

## Amp

Source: <https://ampcode.com/docs/customize/mcp>

```bash
amp mcp add anthive https://<your-anthive-site>/mcp/u/amcp_XXXX
```

Or in `~/.config/amp/settings.json`:

```json
{ "amp.mcpServers": { "anthive": { "url": "https://<your-anthive-site>/mcp/u/amcp_XXXX" } } }
```

## Kiro

Source: <https://kiro.dev/docs/mcp/configuration/>

`~/.kiro/settings/mcp.json` (user) or `.kiro/settings/mcp.json` (workspace).
Changes apply without a restart.

```json
{ "mcpServers": { "anthive": { "url": "https://<your-anthive-site>/mcp/u/amcp_XXXX", "disabled": false } } }
```

## Warp

Source: <https://docs.warp.dev/knowledge-and-collaboration/mcp>

Settings > Agents > MCP servers > **+ Add** > "Streamable HTTP or SSE Server
(URL)":

```json
{ "anthive": { "url": "https://<your-anthive-site>/mcp/u/amcp_XXXX" } }
```

Warp's reference table calls `url` an SSE endpoint while the dialog says
"Streamable HTTP or SSE". It should work; we have not tested it.

## JetBrains AI Assistant

Source: <https://www.jetbrains.com/help/ai-assistant/mcp.html>

Settings > Tools > AI Assistant > Model Context Protocol (MCP) > Add, as JSON:

```json
{ "mcpServers": { "anthive": { "url": "https://<your-anthive-site>/mcp/u/amcp_XXXX" } } }
```

The docs do not show headers, so use the personal link.

## JetBrains Junie

Source: <https://junie.jetbrains.com/docs/junie-cli-mcp-configuration.html>
(not confirmed in the official IDE documentation as of 2026-10)

`~/.junie/mcp/mcp.json` (global) or `.junie/mcp/mcp.json` (project). Remote
servers need IDE 2025.3 or later.

```json
{ "mcpServers": { "anthive": { "url": "https://<your-anthive-site>/mcp/u/amcp_XXXX" } } }
```

## LM Studio

Source: <https://lmstudio.ai/docs/app/mcp>

Program tab > Install > Edit mcp.json:

```json
{ "mcpServers": { "anthive": { "url": "https://<your-anthive-site>/mcp/u/amcp_XXXX" } } }
```

A local model needs decent tool calling to make good use of anthive.

## Cherry Studio

Source: third-party guides only (not confirmed in official documentation as
of 2026-10).

Settings > MCP Servers > **+** Add > Import from JSON:

```json
{ "mcpServers": { "anthive": { "type": "streamableHttp", "url": "https://<your-anthive-site>/mcp/u/amcp_XXXX" } } }
```

## Pi

Source: <https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/mcp.md>

Native since Pi 1.0. `~/.pi/agent/mcp.json` (user) or `.pi/mcp.json`
(project, after you trust it):

```json
{ "mcpServers": { "anthive": { "url": "https://<your-anthive-site>/mcp/u/amcp_XXXX" } } }
```

Header form from the command line:

```bash
export ANTHIVE_MCP_TOKEN=amcp_XXXX
pi mcp add anthive --url https://<your-anthive-site>/mcp --bearer-token-env-var ANTHIVE_MCP_TOKEN
```

Older Pi versions can use the `pi-mcp-adapter` extension
(`pi install npm:pi-mcp-adapter`), which reads the same JSON.

## Any other client: the mcp-remote bridge

Source: <https://github.com/geelen/mcp-remote>

Some clients only start local (stdio) servers. `mcp-remote` is a small
local program that forwards to a remote server, so anything that can run a
command can use anthive. It needs Node.js. Because it runs on your machine,
it also reaches sites that are only on a campus network or VPN.

```json
{
  "mcpServers": {
    "anthive": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://<your-anthive-site>/mcp/u/amcp_XXXX", "--transport", "http-only"]
    }
  }
}
```

This is also the way to put a remote anthive in `claude_desktop_config.json`.

Header form. Write `Authorization:${AUTH_HEADER}` without a space after the
colon: Cursor, Codex and Claude Desktop on Windows mangle arguments with
spaces.

```json
{
  "mcpServers": {
    "anthive": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://<your-anthive-site>/mcp", "--transport", "http-only",
               "--header", "Authorization:${AUTH_HEADER}"],
      "env": { "AUTH_HEADER": "Bearer amcp_XXXX" }
    }
  }
}
```

---

## Did it work?

Ask the assistant "Which anthive datasets are about microglia?". It should
call `search_datasets` and answer with dataset names. Most clients also list
connected servers and their tools (`/mcp` in Claude Code, Codex, Gemini CLI
and Pi; a settings panel elsewhere).

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| 401 or "unauthorized" | The link was rotated, revoked or has expired. Check the MCP links page and copy the current link. |
| Client asks you to sign in or opens a browser | It tried OAuth. Choose "no sign-in", set the client's OAuth option off (OpenCode `"oauth": false`), or use the header form. |
| claude.ai or ChatGPT cannot connect, Claude Code can | Your site is not reachable from the internet. Use a local client or the mcp-remote bridge. |
| Connects, but no tools appear | The client fell back to SSE. Set the HTTP type explicitly (`httpUrl` in Gemini CLI, `streamableHttp` in Cline, `--transport http-only` for mcp-remote). |
| Upload tools missing | `check_paper` and `upload_paper` only appear for members of the library upload groups. |
| 429 "too many requests" | The per-link rate limit. Wait a minute. |

If none of this helps, open an issue with the client name and version and
the error text (never the link itself):
<https://gitlab.kuleuven.be/bdslab/anthive/anthive4/-/issues>.

## Skills

MCP gives a client the tools. The anthive skills teach it how to use them
well (start from the dataset context, cite papers, upload a paper). They are
public at <https://gitlab.kuleuven.be/bdslab/anthive/skills>; the README there
explains how to install them for each harness.
