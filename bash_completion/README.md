# Bash Completion for Ant Command

This directory contains bash completion scripts for the `ant` command, enabling intelligent tab completion based on subcommands.

## Quick Setup (No System-wide Installation)

The easiest way to use anthive without system-wide installation:

```bash
cd /data/1/users/mark/anthive4
source sourceme.sh
```

This will:
- Add `./bin/` to your PATH
- Load bash completion
- Work only in the current shell session

Add this to your `~/.bashrc` to make it permanent:
```bash
source /data/1/users/mark/anthive4/sourceme.sh
```

## Features

- **Subcommand completion**: `ant [TAB]` shows all available subcommands
- **File type completion**: Completes appropriate file types based on subcommand
  - `.h5ad` files for: `check`, `h5ad-check`, `convert`, `duckdb-check`
  - `.duckdb` files for: `db` subcommands
  - Directories for: `serve`, `browse`, `build-all`
- **Flag completion**: Suggests available flags for subcommands like `serve`
- **Nested completion**: Smart completion for multi-level commands like `ant db structure [TAB]`

## Quick Install

```bash
cd bash_completion
./install.sh
source ~/.bashrc
```

## Manual Installation

### System-wide (requires sudo)

```bash
sudo cp bash_completion/ant /etc/bash_completion.d/ant
```

Then reload your shell or run:
```bash
source /etc/bash_completion.d/ant
```

### User-specific

```bash
mkdir -p ~/.bash_completion.d
cp bash_completion/ant ~/.bash_completion.d/ant
```

Add to your `~/.bashrc`:
```bash
[[ -f ~/.bash_completion.d/ant ]] && source ~/.bash_completion.d/ant
```

Then reload:
```bash
source ~/.bashrc
```

## Usage Examples

```bash
# Complete subcommands
ant [TAB]
# Shows: browse build-all check convert db duckdb-check h5ad-check md serve

# Complete .h5ad files
ant check [TAB]
# Shows: dataset1.h5ad dataset2.h5ad ...

# Complete .h5ad or .duckdb files
ant duckdb-check [TAB]
# Shows: dataset1.h5ad dataset2.h5ad dataset1.duckdb ...

# Complete db subcommands
ant db [TAB]
# Shows: structure schema query

# Complete .duckdb files
ant db structure [TAB]
# Shows: dataset1.duckdb dataset2.duckdb ...

# Complete serve flags
ant serve --[TAB]
# Shows: --data-folder --host --port --reload
```

## Completion Rules

| Subcommand | File Type | Description |
|------------|-----------|-------------|
| `check` | `.h5ad` | Check h5ad and DuckDB files |
| `h5ad-check` | `.h5ad` | Check h5ad file only |
| `duckdb-check` | `.h5ad`, `.duckdb` | Check DuckDB file |
| `convert` | `.h5ad` | Convert h5ad to DuckDB |
| `db structure` | `.duckdb` | Show database structure |
| `db schema` | `.duckdb` | Show table schema |
| `db query` | `.duckdb` | Execute SQL query |
| `serve` | directory | Start REST API server |
| `browse` | directory | Start web browser |
| `build-all` | directory | Process entire datastore |
| `md get/set/delete` | `.h5ad`, `.md` | Manage metadata |

## Troubleshooting

### Completion not working

1. **Check bash-completion is installed**:
   ```bash
   # Debian/Ubuntu
   sudo apt-get install bash-completion

   # RedHat/CentOS
   sudo yum install bash-completion

   # macOS
   brew install bash-completion@2
   ```

2. **Verify completion is loaded**:
   ```bash
   type _init_completion
   # Should show: _init_completion is a function
   ```

3. **Check the completion file is sourced**:
   ```bash
   complete -p ant
   # Should show: complete -F _ant_completion ant
   ```

4. **Reload your shell**:
   ```bash
   source ~/.bashrc
   # Or open a new terminal
   ```

### Still not working?

Try sourcing the completion file directly:
```bash
source /data/1/users/mark/anthive4/bash_completion/ant
```

If this works, add it to your `~/.bashrc`.

## Development

To modify the completion rules, edit `bash_completion/ant` and reload:

```bash
source bash_completion/ant
```

Test completion by typing commands and pressing TAB.

## Uninstall

```bash
# System-wide
sudo rm /etc/bash_completion.d/ant

# User-specific
rm ~/.bash_completion.d/ant
# And remove the sourcing line from ~/.bashrc
```
