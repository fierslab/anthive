# Anthive Configuration

Configuration is managed through `anthive.yaml` in the base anthive folder.
All parameters are optional - if missing, defaults from `anthive.default.yaml` are used.

## Configuration Parameters

### Data Processing

#### `max_obscat_values`
- **Type**: integer
- **Default**: 100
- **Description**: Maximum number of unique values allowed for an obs column to be treated as categorical. Columns exceeding this threshold are automatically added to the exclusion list to prevent database bloat.
- **Usage**: Controls the boundary between categorical and high-cardinality text fields

#### `melt_chunk_size`
- **Type**: integer
- **Default**: 10000
- **Description**: Number of cells to process per chunk when melting expression layers. This balances memory usage against processing speed. Larger values = faster but more memory; smaller values = slower but less memory.
- **Tuning**:
  - For machines with 16GB+ RAM: 20000-50000
  - For machines with 8GB RAM: 5000-10000
  - For very large datasets (>1M cells): adjust down as needed
- **Impact**: Affects conversion time and peak memory usage during `ant convert`

### REST API (future use)

#### `rest_api_host`
- **Type**: string
- **Default**: `localhost`
- **Description**: Host address for the REST API server
- **Note**: Authentication/authorization expected at reverse proxy level (nginx/caddy)

#### `rest_api_port`
- **Type**: integer
- **Default**: 8080
- **Description**: Port number for the REST API server

### Web UI

#### `issue_tracker_url`
- **Type**: string
- **Default**: `https://gitlab.kuleuven.be/bdslab/anthive/anthive4/-/issues`
- **Description**: Target of the "Report an issue" entry in the user menu (top right, signed-in users). Opens in a new tab. An empty string hides the entry. Env `ANTHIVE_ISSUE_TRACKER_URL` wins; container sites set `site.issue_tracker_url` in `site.yaml`.

### Personal MCP links (`mcp:` block)

Full list in `anthive.default.yaml`; user-facing guide in `doc/MCP_LINKS.md`.

#### `mcp.max_links_per_user`
- **Type**: integer
- **Default**: 5
- **Description**: How many named links one user may hold at the same time. Revoked links do not count.

#### `mcp.token_ttl_days`
- **Type**: integer
- **Default**: 90
- **Description**: Lifetime of a link; rotating or renewing starts a new period. 0 means links never expire.

### Paper library and literature index

See `doc/LIBRARY.md` and `doc/LIBRARY_INDEX.md`.

#### `library`
- **Type**: path
- **Default**: `data/library`
- **Description**: Checkout of the library repository (paper records under `papers/`, any depth). Env `ANTHIVE_LIBRARY`; container sites set `data.library` in `site.yaml`.

#### `library_index`
- **Type**: path
- **Default**: `data/library_index`
- **Description**: Folder holding `library_chunks.parquet` (and `library_chunks.json`). Env `ANTHIVE_LIBRARY_INDEX`; container sites set `data.library_index`.

#### `library_lab_authors`
- **Type**: list of strings
- **Default**: `[De Strooper]`
- **Description**: New papers with a matching author go to `papers/lab/` when added with `ant lib-add` / `ant lib-import` (case and accent insensitive). Folders are for people only.

#### `library_upload_groups`
- **Type**: list of strings
- **Default**: `[admins]`
- **Description**: Groups (from `Remote-Groups`) allowed to add papers through `POST /papers/upload` and the MCP tools `check_paper` / `upload_paper`. In basic auth mode the users in `auth.admins` count as `admins`. Container sites set `library.upload_groups`.

#### `library_upload_max_pdf_mb`
- **Type**: integer
- **Default**: `25`
- **Description**: Largest PDF the upload endpoint accepts (larger is a 413). Container sites set `library.max_pdf_mb`.

#### `library_git_committer`
- **Type**: string (`Name <email>`)
- **Default**: `anthive <noreply@anthive.local>`
- **Description**: Committer of upload commits in the library repository; the uploader is the author.

#### `library_git_push`
- **Type**: boolean
- **Default**: `false`
- **Description**: Push the library repository after each upload commit and on `ant sync library`. Needs a remote and credentials (for example a deploy key). Off: commits stay local until someone pushes.

#### `library_s3_autopush`
- **Type**: boolean
- **Default**: `false` (was `true` before 2.41.0)
- **Description**: Push an uploaded PDF to S3 right after the commit (when S3 is configured). Off: it goes with the next `ant sync library`. The serving process then needs no S3 write key; container sites publish with `ant-stack publish-library` from a timer (`doc/DEPLOY_CONTAINERS.md`).

#### `library_index_publisher`
- **Type**: boolean
- **Default**: `false`
- **Description**: This site publishes the literature index to S3 during `ant sync`. Set it on exactly one site; the others download the published index as a starting point and add their own papers (`doc/LIBRARY.md`, "Two-way sync").

#### `mcp.max_upload_mb`
- **Type**: integer
- **Default**: `25`
- **Description**: Largest PDF the MCP tool `upload_paper` accepts (sent base64 in the tool call). The API's own limit `library_upload_max_pdf_mb` still applies.

#### `model_cache`
- **Type**: path
- **Default**: empty
- **Description**: Cache for the query-embedding model. Empty means `FASTEMBED_CACHE_PATH` (the container image bakes the model there), else `<state_dir>/models`. Env `ANTHIVE_MODEL_CACHE`.

## Configuration File Format

Example `anthive.yaml`:

```yaml
max_obscat_values: 100
melt_chunk_size: 10000
rest_api_host: localhost
rest_api_port: 8080
```

## Validation

Invalid configuration values cause the program to exit with an error message.
It is better to crash early than to process data with incorrect settings.
