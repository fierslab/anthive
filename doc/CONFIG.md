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
