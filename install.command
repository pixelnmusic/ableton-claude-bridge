#!/bin/bash
#
# Ableton-Claude-Bridge Installer — double-click in Finder to install.
# Copies Control Surface to Ableton, creates MCP server venv, updates Claude Desktop config.
#

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "=========================================="
echo "  Ableton-Claude-Bridge Installer"
echo "=========================================="
echo ""
echo "Project directory: $SCRIPT_DIR"
echo ""

# ── 1. Copy Control Surface to Ableton Remote Scripts ──

REMOTE_SCRIPTS="$HOME/Music/Ableton/User Library/Remote Scripts"
echo "[1/7] Installing Control Surface..."

mkdir -p "$REMOTE_SCRIPTS"

if [ -d "$REMOTE_SCRIPTS/ClaudeBridge" ]; then
    echo "  Removing previous ClaudeBridge installation..."
    rm -rf "$REMOTE_SCRIPTS/ClaudeBridge"
fi

cp -R "$SCRIPT_DIR/ClaudeBridge" "$REMOTE_SCRIPTS/ClaudeBridge"
echo "  Copied to: $REMOTE_SCRIPTS/ClaudeBridge"

# ── 2. Create Python venv for MCP server ──

echo ""
echo "[2/7] Creating Python virtual environment..."

MCP_DIR="$SCRIPT_DIR/mcp_server"

if [ -d "$MCP_DIR/venv" ]; then
    echo "  Removing existing venv..."
    rm -rf "$MCP_DIR/venv"
fi

python3 -m venv "$MCP_DIR/venv"
echo "  venv created at: $MCP_DIR/venv"

# ── 3. Install Python dependencies ──

echo ""
echo "[3/7] Installing Python dependencies..."

"$MCP_DIR/venv/bin/pip" install --upgrade pip --quiet
"$MCP_DIR/venv/bin/pip" install -r "$MCP_DIR/requirements.txt" --quiet
echo "  Installed dependencies from requirements.txt"

# ── 4. Update Claude Desktop config ──

echo ""
echo "[4/7] Updating Claude Desktop configuration..."

CLAUDE_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
CLAUDE_CONFIG_DIR="$(dirname "$CLAUDE_CONFIG")"
VENV_PYTHON="$MCP_DIR/venv/bin/python3"
MCP_SCRIPT="$MCP_DIR/claude_mcp_server.py"

mkdir -p "$CLAUDE_CONFIG_DIR"

if [ -f "$CLAUDE_CONFIG" ]; then
    # Read existing config and merge
    python3 -c "
import json, sys

config_path = sys.argv[1]
venv_python = sys.argv[2]
mcp_script = sys.argv[3]

with open(config_path, 'r') as f:
    config = json.load(f)

if 'mcpServers' not in config:
    config['mcpServers'] = {}

# Remove old M4L-based bridge entries if present
for key in list(config['mcpServers'].keys()):
    if 'ableton' in key.lower() or 'claude-bridge' in key.lower() or 'claudebridge' in key.lower():
        print(f'  Removing old entry: {key}')
        del config['mcpServers'][key]

config['mcpServers']['ableton-claude-bridge'] = {
    'command': venv_python,
    'args': [mcp_script]
}

with open(config_path, 'w') as f:
    json.dump(config, f, indent=2)

print('  Config updated successfully.')
print(f'  Preserved keys: {[k for k in config.keys() if k != \"mcpServers\"]}')
" "$CLAUDE_CONFIG" "$VENV_PYTHON" "$MCP_SCRIPT"
else
    # Create new config
    python3 -c "
import json, sys

config_path = sys.argv[1]
venv_python = sys.argv[2]
mcp_script = sys.argv[3]

config = {
    'mcpServers': {
        'ableton-claude-bridge': {
            'command': venv_python,
            'args': [mcp_script]
        }
    }
}

with open(config_path, 'w') as f:
    json.dump(config, f, indent=2)

print('  Config created successfully.')
" "$CLAUDE_CONFIG" "$VENV_PYTHON" "$MCP_SCRIPT"
fi

# ── 5. Create exports directory ──

echo ""
echo "[5/7] Creating exports directory..."

EXPORTS_DIR="$SCRIPT_DIR/exports"
mkdir -p "$EXPORTS_DIR"
echo "  Exports folder: $EXPORTS_DIR"

# ── 6. Detect Ableton Live app name ──

echo ""
echo "[6/7] Detecting Ableton Live installation..."

ABLETON_APP=""
for app in /Applications/Ableton\ Live*.app; do
    if [ -d "$app" ]; then
        ABLETON_APP="$(basename "$app" .app)"
        break
    fi
done

if [ -n "$ABLETON_APP" ]; then
    echo "  Detected: $ABLETON_APP"
    echo "$ABLETON_APP" > "$MCP_DIR/.ableton_app_name"
    echo "  Saved to: $MCP_DIR/.ableton_app_name"
else
    echo "  No Ableton Live found in /Applications."
    echo "  Using default: Ableton Live 12 Suite"
    ABLETON_APP="Ableton Live 12 Suite"
fi

# ── 7. Open Accessibility preferences ──

echo ""
echo "[7/7] Opening Accessibility preferences..."
echo "  Claude Desktop needs Accessibility access for audio export."
echo "  Grant access to Claude Desktop (and Terminal if using CLI)."

open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" 2>/dev/null || true

# ── Done ──

echo ""
echo "=========================================="
echo "  Installation complete!"
echo "=========================================="
echo ""
echo "Next steps:"
echo ""
echo "  1. Open $ABLETON_APP"
echo "  2. Go to Preferences > MIDI"
echo "  3. Under Control Surface, select 'ClaudeBridge' from the dropdown"
echo "     (No Input/Output MIDI port needed)"
echo "  4. Verify in Help > Show Live Log File:"
echo "     [ClaudeBridge] ClaudeBridge ready on port 8765"
echo "  5. Restart Claude Desktop"
echo "  6. Claude will now have access to your Ableton session!"
echo ""
echo "  To scan your plugins, run: scan_plugins.command"
echo ""
