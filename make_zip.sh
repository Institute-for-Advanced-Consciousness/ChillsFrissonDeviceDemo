#!/usr/bin/env bash
# Build a clean distributable zip of the project.
# Run from the project root:   ./make_zip.sh
set -e
cd "$(dirname "$0")"

NAME="ChillsDemo-$(date +%Y%m%d-%H%M).zip"

STAGE_PARENT=$(mktemp -d)
STAGE="$STAGE_PARENT/ChillsDemo"
mkdir -p "$STAGE"

# Copy the working tree minus build/runtime junk and per-machine state.
rsync -a \
  --exclude 'venv/' \
  --exclude '__pycache__/' \
  --exclude '.git/' \
  --exclude '.gitignore' \
  --exclude '.DS_Store' \
  --exclude 'Data/' \
  --exclude '*.zip' \
  --exclude 'track_overrides.json' \
  --exclude '.claude/' \
  --exclude 'make_zip.sh' \
  ./ "$STAGE/"

# Make sure the launcher scripts are executable in the staging copy.
chmod +x "$STAGE/INSTALL.command" "$STAGE/ChillsDemo.command" 2>/dev/null || true

# ditto produces a clean macOS-friendly zip (preserves +x bits).
ditto -c -k --sequesterRsrc --keepParent "$STAGE" "$NAME"
rm -rf "$STAGE_PARENT"

printf '\n✓ Created %s (%s)\n' "$NAME" "$(du -h "$NAME" | cut -f1)"
printf '\nSend this to your friend. They should:\n'
printf '  1. Unzip it.\n'
printf '  2. Double-click  INSTALL.command  (right-click → Open if Gatekeeper warns).\n'
printf '  3. Double-click  ChillsDemo.command  to launch.\n'
