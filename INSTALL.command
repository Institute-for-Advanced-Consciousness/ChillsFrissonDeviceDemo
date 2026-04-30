#!/usr/bin/env bash
# ChillsDemo — first-time install on macOS.
# Double-click this file to set up the app. Safe to re-run anytime.
set -e
cd "$(dirname "$0")"

# Strip the macOS quarantine attribute on every file in this folder so
# subsequent double-clicks (especially "ChillsDemo.command") don't trigger
# Gatekeeper warnings. Harmless if no quarantine attr is set.
xattr -dr com.apple.quarantine . 2>/dev/null || true

printf '\nChillsDemo — first-time install\n'
printf '================================\n\n'

# ── 1. Find a Python 3.10+ interpreter ───────────────────────────────
PY=""
for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      PY="$cand"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  printf '❌ Python 3.10 or newer is not installed on this Mac.\n\n'
  printf 'Install it from https://www.python.org/downloads/macos/\n'
  printf '(Pick the latest 3.x release, run the .pkg installer, then\n'
  printf 'double-click this script again.)\n\n'
  open "https://www.python.org/downloads/macos/" 2>/dev/null || true
  printf 'Press any key to close…'
  read -n 1 -s -r
  printf '\n'
  exit 1
fi

PYV=$("$PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
printf '✓ Using Python %s at %s\n\n' "$PYV" "$(command -v "$PY")"

# ── 2. Verify tkinter is available (the GUI toolkit) ─────────────────
if ! "$PY" -c "import tkinter" >/dev/null 2>&1; then
  printf '❌ Your Python is missing the tkinter GUI toolkit.\n\n'
  if command -v brew >/dev/null 2>&1; then
    printf 'You appear to have Homebrew. Run this and then re-run me:\n'
    printf '    brew install python-tk@%s\n\n' "$PYV"
  else
    printf 'Easiest fix: install Python from https://www.python.org/downloads/macos/\n'
    printf '(the python.org installer includes tkinter, unlike Homebrew Python).\n\n'
  fi
  printf 'Press any key to close…'
  read -n 1 -s -r
  printf '\n'
  exit 1
fi

# ── 3. Create venv if missing ────────────────────────────────────────
if [ ! -d "venv" ]; then
  printf 'Creating virtual environment in ./venv …\n'
  "$PY" -m venv venv
fi

# Always use venv's Python directly — never the venv's pip script,
# whose shebang can be stale if the project folder was moved.
VPY="./venv/bin/python"

if [ ! -x "$VPY" ]; then
  printf '❌ venv looks broken (no %s). Try deleting the venv folder and running again.\n' "$VPY"
  printf 'Press any key to close…'
  read -n 1 -s -r
  printf '\n'
  exit 1
fi

# ── 4. Install dependencies ──────────────────────────────────────────
printf '\nUpgrading pip and installing dependencies (takes ~30 s on first run)…\n\n'
"$VPY" -m pip install --upgrade pip
"$VPY" -m pip install -r requirements.txt

# ── 5. Make sure the data/stimuli folders exist ──────────────────────
mkdir -p Stimuli Stimuli/Suuvi Data

# ── 6. Mark the launcher as executable ───────────────────────────────
chmod +x "ChillsDemo.command" 2>/dev/null || true

printf '\n============================================================\n'
printf '✓ Installation complete.\n\n'
printf 'To launch the app, double-click   ChillsDemo.command\n\n'
printf 'If you don'\''t have audio files yet, drop them into Stimuli/:\n'
printf '    Arameic.mp3  Hallelujah.mp3  Misere.mp3\n'
printf '============================================================\n\n'
printf 'Press any key to close…'
read -n 1 -s -r
printf '\n'
