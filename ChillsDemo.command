#!/usr/bin/env bash
# ChillsDemo — launcher. Double-click to start the app.
# If the venv hasn't been built yet, this hands off to INSTALL.command,
# which fully installs everything and then auto-launches the app.
set -e
cd "$(dirname "$0")"

xattr -dr com.apple.quarantine . 2>/dev/null || true

if [ ! -x "./venv/bin/python" ]; then
  exec bash "./INSTALL.command"
fi

exec ./venv/bin/python app.py
