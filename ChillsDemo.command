#!/usr/bin/env bash
# ChillsDemo — launcher. Double-click to start the app.
set -e
cd "$(dirname "$0")"

# Self-dequarantine — keeps Gatekeeper from re-prompting after a download.
xattr -dr com.apple.quarantine . 2>/dev/null || true

# Auto-bootstrap: if the venv isn't set up yet, run the installer first.
if [ ! -x "./venv/bin/python" ]; then
  printf 'No virtual environment found — running first-time install…\n\n'
  bash "./INSTALL.command"
fi

if [ ! -x "./venv/bin/python" ]; then
  printf '\n❌ Setup incomplete. Run INSTALL.command and try again.\n'
  printf 'Press any key to close…'
  read -n 1 -s -r
  printf '\n'
  exit 1
fi

# Launch the app. exec replaces this shell so closing the window kills
# the python process cleanly.
exec ./venv/bin/python app.py
