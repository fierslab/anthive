# Using Anthive with Claude Code

Anthive ships a **skill** that teaches Claude Code how to query the
REST API via curl. Once installed, Claude can search datasets, plot
gene expression, run SQL analytics, and more — triggered automatically
or via `/anthive`.

## Prerequisites

- The Anthive API must be running (`ant serve` or `make serve-api`)
- If the server requires authentication, add credentials to `~/.netrc`:
  ```
  machine anthive.example.org
  login <your-username>
  password <your-password>
  ```

## Install the skill

### Option A: clone the repo (recommended)

If you work inside the anthive4 repo, the skill is already at
`.claude/skills/anthive/SKILL.md` — nothing to install.

### Option B: fetch from the API

For use outside the repo, install the skill into your user-level
Claude Code config:

```bash
mkdir -p ~/.claude/skills/anthive
curl -n -s https://anthive.example.org/api/skill -o ~/.claude/skills/anthive/SKILL.md
```

Re-run the curl command any time to update to the latest version.

## Usage

Inside Claude Code, either:

- Type `/anthive` to explicitly activate the skill
- Just ask a biology question — Claude auto-triggers the skill when the
  request matches (gene expression, cell types, datasets, etc.)

### Example prompts

```
Show me APOE expression across cell types in the Sierksma2025 dataset

Compare TREM2 expression between AD and control in all microglia datasets

What datasets do we have for human brain tissue?

Plot a UMAP of the Zielonka2025 L23 dataset colored by cell type
```

## How it works

The skill instructs Claude to call the REST API with `curl -n`. No MCP
server or additional daemon is required — only `ant-serve` running on
its usual port.

- **JSON endpoints** (database listing, gene search, analytics): Claude
  reads the JSON response directly.
- **Plot endpoints** (UMAP, violin, heatmap, etc.): Claude saves the
  image to `/tmp/`, then shows it to you.
- **Analytics viz** (custom Python figures): The API returns base64 JSON;
  Claude decodes it to a file and shows it.

## Updating

The skill is served from the API itself at `/api/skill`. When the API
is updated with new endpoints or parameters, re-fetch the skill:

```bash
curl -n -s https://anthive.example.org/api/skill -o ~/.claude/skills/anthive/SKILL.md
```

Repo users get updates automatically via `git pull`.
