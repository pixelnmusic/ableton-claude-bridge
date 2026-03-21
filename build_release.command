#!/bin/bash
#
# Ableton-Claude-Bridge Release Builder — double-click in Finder to create a distributable ZIP.
#

set -e

VERSION="2.2.0"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STAGING="/tmp/Ableton-Claude-Bridge-v${VERSION}"
ZIP_NAME="Ableton-Claude-Bridge-v${VERSION}.zip"
ZIP_PATH="$HOME/Desktop/$ZIP_NAME"

echo "=========================================="
echo "  Ableton-Claude-Bridge Release Builder v${VERSION}"
echo "=========================================="
echo ""

# ── Clean previous staging ──

if [ -d "$STAGING" ]; then
    rm -rf "$STAGING"
fi

mkdir -p "$STAGING"

# ── Copy distributable files ──

echo "Copying files..."

# Control Surface
cp -R "$SCRIPT_DIR/ClaudeBridge" "$STAGING/ClaudeBridge"
# Remove __pycache__ from Control Surface
find "$STAGING/ClaudeBridge" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# MCP server (only the script and requirements, not venv)
mkdir -p "$STAGING/mcp_server"
cp "$SCRIPT_DIR/mcp_server/claude_mcp_server.py" "$STAGING/mcp_server/"
cp "$SCRIPT_DIR/mcp_server/requirements.txt" "$STAGING/mcp_server/"

# Scripts
cp "$SCRIPT_DIR/install.command" "$STAGING/"
cp "$SCRIPT_DIR/scan_plugins.command" "$STAGING/"
cp "$SCRIPT_DIR/build_release.command" "$STAGING/"

# Config
cp "$SCRIPT_DIR/claude_bridge_config.json" "$STAGING/"

# Docs
cp "$SCRIPT_DIR/README.md" "$STAGING/"
cp "$SCRIPT_DIR/LICENSE" "$STAGING/"
cp "$SCRIPT_DIR/NOTICE" "$STAGING/"
cp "$SCRIPT_DIR/CHANGELOG.md" "$STAGING/"
cp "$SCRIPT_DIR/.gitignore" "$STAGING/"

# Empty exports directory with .gitkeep
mkdir -p "$STAGING/exports"
touch "$STAGING/exports/.gitkeep"

echo "  Staged to: $STAGING"

# ── Make scripts executable ──

chmod +x "$STAGING/install.command"
chmod +x "$STAGING/scan_plugins.command"
chmod +x "$STAGING/build_release.command"

# ── Create ZIP ──

echo ""
echo "Creating ZIP archive..."

# Remove previous ZIP if it exists
if [ -f "$ZIP_PATH" ]; then
    rm "$ZIP_PATH"
fi

cd /tmp
zip -r "$ZIP_PATH" "Ableton-Claude-Bridge-v${VERSION}/" -x "*.DS_Store"
cd "$SCRIPT_DIR"

echo "  ZIP created: $ZIP_PATH"

# ── Clean up staging ──

rm -rf "$STAGING"

# ── Summary ──

echo ""
echo "=========================================="
echo "  Build complete!"
echo "=========================================="
echo ""
echo "  Output: $ZIP_PATH"
echo "  Size:   $(du -h "$ZIP_PATH" | cut -f1)"
echo ""
echo "  Contents:"
unzip -l "$ZIP_PATH" | grep "Ableton-Claude-Bridge" | awk '{print "    " $4}'
echo ""
