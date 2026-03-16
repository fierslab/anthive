#!/bin/bash
# Install bash completion for ant command

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPLETION_FILE="$SCRIPT_DIR/ant"

echo "Installing bash completion for 'ant' command..."
echo

# Check if bash-completion is available
if ! command -v _init_completion &> /dev/null; then
    echo "Warning: bash-completion package may not be installed"
    echo "Install it with: sudo apt-get install bash-completion  (Debian/Ubuntu)"
    echo "              or: sudo yum install bash-completion      (RedHat/CentOS)"
    echo
fi

# Try system-wide installation first (requires sudo)
if [[ -w /etc/bash_completion.d/ ]]; then
    echo "Installing system-wide to /etc/bash_completion.d/ant"
    cp "$COMPLETION_FILE" /etc/bash_completion.d/ant
    echo "✓ Installed system-wide"
elif command -v sudo &> /dev/null; then
    echo "Installing system-wide to /etc/bash_completion.d/ant (requires sudo)"
    sudo cp "$COMPLETION_FILE" /etc/bash_completion.d/ant
    echo "✓ Installed system-wide"
else
    # Fall back to user installation
    echo "Installing for current user to ~/.bash_completion.d/ant"
    mkdir -p ~/.bash_completion.d
    cp "$COMPLETION_FILE" ~/.bash_completion.d/ant

    # Add sourcing to .bashrc if not already present
    if ! grep -q "\.bash_completion\.d/ant" ~/.bashrc; then
        echo "" >> ~/.bashrc
        echo "# Bash completion for ant command" >> ~/.bashrc
        echo "[[ -f ~/.bash_completion.d/ant ]] && source ~/.bash_completion.d/ant" >> ~/.bashrc
        echo "✓ Installed for current user"
        echo "✓ Added to ~/.bashrc"
    else
        echo "✓ Installed for current user"
        echo "  (already in ~/.bashrc)"
    fi
fi

echo
echo "Installation complete!"
echo
echo "To activate completion in your current shell:"
echo "  source ~/.bashrc"
echo
echo "Or simply open a new terminal."
echo
echo "Then try:"
echo "  ant [TAB]                    # Shows available subcommands"
echo "  ant duckdb-check [TAB]       # Completes .h5ad files"
echo "  ant db structure [TAB]       # Completes .duckdb files"
