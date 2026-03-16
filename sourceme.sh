#!/bin/bash
# Source this file to set up anthive environment in current shell
#
# Usage:
#   source sourceme.sh
#   OR
#   . sourceme.sh

# Get the directory where this script is located
ANTHIVE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Add bin directory to PATH if not already there
if [[ ":$PATH:" != *":$ANTHIVE_DIR/bin:"* ]]; then
    export PATH="$ANTHIVE_DIR/bin:$PATH"
    echo "✓ Added $ANTHIVE_DIR/bin to PATH"
else
    echo "✓ $ANTHIVE_DIR/bin already in PATH"
fi

# Load bash completion
if [[ -f "$ANTHIVE_DIR/bash_completion/ant" ]]; then
    source "$ANTHIVE_DIR/bash_completion/ant"
    echo "✓ Loaded bash completion for 'ant'"
else
    echo "⚠ Bash completion file not found"
fi

echo
echo "Anthive environment ready!"
echo
echo "Try:"
echo "  ant [TAB]                    # Shows available subcommands"
echo "  ant duckdb-check [TAB]       # Completes .h5ad files"
echo "  ant --help                   # Show help"
