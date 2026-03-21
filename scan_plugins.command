#!/bin/bash
#
# Ableton-Claude-Bridge Plugin Scanner — double-click in Finder to scan installed plugins.
# Reads Ableton's plugin database and generates plugin_registry.json.
#

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT="$SCRIPT_DIR/plugin_registry.json"

echo "=========================================="
echo "  Ableton-Claude-Bridge Plugin Scanner"
echo "=========================================="
echo ""

# Common plugin directories
AU_DIR="/Library/Audio/Plug-Ins/Components"
VST_DIR="/Library/Audio/Plug-Ins/VST"
VST3_DIR="/Library/Audio/Plug-Ins/VST3"

echo "Scanning plugin directories..."
echo ""

python3 -c "
import json
import os
import subprocess
from pathlib import Path

registry = {
    'scan_date': '$(date -u +%Y-%m-%dT%H:%M:%SZ)',
    'au_plugins': [],
    'vst_plugins': [],
    'vst3_plugins': [],
}

# ── Scan AU plugins ──
au_dir = Path('$AU_DIR')
if au_dir.exists():
    for comp in sorted(au_dir.glob('*.component')):
        name = comp.stem
        registry['au_plugins'].append({
            'name': name,
            'format': 'AU',
            'path': str(comp),
        })
    print(f'  AU plugins found: {len(registry[\"au_plugins\"])}')
else:
    print('  AU directory not found, skipping.')

# ── Scan VST plugins ──
vst_dir = Path('$VST_DIR')
if vst_dir.exists():
    for vst in sorted(vst_dir.rglob('*.vst')):
        name = vst.stem
        registry['vst_plugins'].append({
            'name': name,
            'format': 'VST',
            'path': str(vst),
        })
    print(f'  VST plugins found: {len(registry[\"vst_plugins\"])}')
else:
    print('  VST directory not found, skipping.')

# ── Scan VST3 plugins ──
vst3_dir = Path('$VST3_DIR')
if vst3_dir.exists():
    for vst3 in sorted(vst3_dir.rglob('*.vst3')):
        name = vst3.stem
        registry['vst3_plugins'].append({
            'name': name,
            'format': 'VST3',
            'path': str(vst3),
        })
    print(f'  VST3 plugins found: {len(registry[\"vst3_plugins\"])}')
else:
    print('  VST3 directory not found, skipping.')

# ── Also scan user AU/VST directories ──
user_au = Path.home() / 'Library/Audio/Plug-Ins/Components'
if user_au.exists() and user_au != au_dir:
    for comp in sorted(user_au.glob('*.component')):
        name = comp.stem
        # Avoid duplicates
        existing = [p['name'] for p in registry['au_plugins']]
        if name not in existing:
            registry['au_plugins'].append({
                'name': name,
                'format': 'AU',
                'path': str(comp),
            })

user_vst3 = Path.home() / 'Library/Audio/Plug-Ins/VST3'
if user_vst3.exists() and user_vst3 != vst3_dir:
    for vst3 in sorted(user_vst3.rglob('*.vst3')):
        name = vst3.stem
        existing = [p['name'] for p in registry['vst3_plugins']]
        if name not in existing:
            registry['vst3_plugins'].append({
                'name': name,
                'format': 'VST3',
                'path': str(vst3),
            })

# ── Summary ──
total = len(registry['au_plugins']) + len(registry['vst_plugins']) + len(registry['vst3_plugins'])
registry['total_plugins'] = total

# ── Write output ──
output_path = '$OUTPUT'
with open(output_path, 'w') as f:
    json.dump(registry, f, indent=2)

print(f'')
print(f'  Total plugins found: {total}')
print(f'  Registry written to: {output_path}')
"

echo ""
echo "=========================================="
echo "  Plugin scan complete!"
echo "=========================================="
echo ""
echo "Claude can now use get_plugin_registry to see your installed plugins."
echo ""
