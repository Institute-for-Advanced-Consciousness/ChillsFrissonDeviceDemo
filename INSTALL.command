#!/usr/bin/env bash
# ChillsDemo — fully unattended setup for macOS.
# Double-click to install everything (Xcode CLT, Homebrew, Python, tkinter,
# Python deps) and then launch the app. Re-running is safe and fast.
#
# The only manual interactions you may see — and only if those tools
# aren't already installed on this Mac — are:
#   - a system dialog asking to install Xcode Command Line Tools
#   - one Mac password prompt the first time Homebrew installs
# Otherwise it runs to completion and opens the app for you.

set -e
cd "$(dirname "$0")"

# Strip Gatekeeper quarantine so subsequent launches don't re-prompt.
xattr -dr com.apple.quarantine . 2>/dev/null || true

clear 2>/dev/null || true
cat <<'BANNER'
============================================================
   ChillsDemo Setup

   This will install everything the app needs and then
   launch it. Leave this window open until it says
   "Setup complete" — it'll close itself when ready.
============================================================

BANNER

log()  { printf '\n→ %s\n' "$*"; }
warn() { printf '\n⚠ %s\n' "$*"; }
fail() {
  printf '\n❌ %s\n\n' "$*"
  printf 'Press any key to close…'
  read -n 1 -s -r
  printf '\n'
  exit 1
}

# ── Step 1: ensure Xcode Command Line Tools ──────────────────────────
# Required for Homebrew, git, compilers used by some pip wheels.
if ! xcode-select -p >/dev/null 2>&1; then
  log "Installing Xcode Command Line Tools (a system dialog will appear — click Install)"
  xcode-select --install >/dev/null 2>&1 || true
  printf '\nWaiting for Command Line Tools to finish installing…\n'
  printf '(this can take 5–15 minutes the first time, only happens once)\n'
  while ! xcode-select -p >/dev/null 2>&1; do
    sleep 10
    printf '.'
  done
  printf '\n✓ Command Line Tools installed.\n'
fi

# ── Step 2: locate a working Python 3.10+ with tkinter ───────────────
PY=""
find_python() {
  for cand in \
      python3.13 python3.14 python3.12 python3.11 python3.10 python3 \
      /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.14 \
      /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
      /opt/homebrew/bin/python3 \
      /usr/local/bin/python3.13 /usr/local/bin/python3 \
      /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
      /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
      /Library/Frameworks/Python.framework/Versions/3.11/bin/python3; do
    if command -v "$cand" >/dev/null 2>&1 || [ -x "$cand" ]; then
      if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null \
         && "$cand" -c 'import tkinter' 2>/dev/null; then
        PY="$cand"
        return 0
      fi
    fi
  done
  return 1
}

# ── Step 3: install Homebrew if needed ───────────────────────────────
ensure_brew() {
  if command -v brew >/dev/null 2>&1; then
    return 0
  fi
  log "Installing Homebrew (you'll be asked for your Mac password once)"
  NONINTERACTIVE=1 /bin/bash -c \
    "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
    || fail "Homebrew install failed. Try again with a network connection."
  if [ -x /opt/homebrew/bin/brew ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [ -x /usr/local/bin/brew ]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
}

if find_python; then
  log "Found compatible Python: $PY"
else
  log "Compatible Python with tkinter not found — installing via Homebrew"
  ensure_brew
  log "Installing python@3.13 and python-tk@3.13 (takes 1–3 minutes)"
  brew install python@3.13 python-tk@3.13 || \
    fail "Homebrew couldn't install Python. Check the messages above."
  if ! find_python; then
    fail "Couldn't find a working Python after install. Try re-running this script in a fresh Terminal."
  fi
  log "Installed Python at $PY"
fi

PYV=$("$PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
log "Using Python $PYV ($PY)"

# ── Step 4: virtualenv + Python deps ─────────────────────────────────
if [ -d "venv" ] && [ ! -x "./venv/bin/python" ]; then
  warn "Existing venv looks broken — recreating it."
  rm -rf venv
fi
if [ ! -d "venv" ]; then
  log "Creating virtual environment in ./venv"
  "$PY" -m venv venv || fail "venv creation failed."
fi

VPY="./venv/bin/python"
[ -x "$VPY" ] || fail "venv/bin/python missing after venv creation."

log "Upgrading pip"
"$VPY" -m pip install --upgrade pip --quiet || warn "pip upgrade failed (continuing)"

log "Installing Python dependencies (pygame-ce, customtkinter, bleak, pyserial, websockets)…"
"$VPY" -m pip install -r requirements.txt || \
  fail "pip install failed. Check the messages above."

# ── Step 5: directories + launcher executable bit ────────────────────
mkdir -p Stimuli Stimuli/Suuvi Data
chmod +x "ChillsDemo.command" 2>/dev/null || true

cat <<'DONE'

============================================================
✓ Setup complete.

Launching ChillsDemo now. From now on, you can just
double-click   ChillsDemo.command   to run the app.
============================================================

DONE

# Replace this shell with the running app — keeps things tidy and
# means closing the app window terminates this Terminal too.
exec "$VPY" app.py
