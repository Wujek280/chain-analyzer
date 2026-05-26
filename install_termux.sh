#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() {
  printf '%s\n' "$*"
}

warn() {
  printf 'WARN: %s\n' "$*" >&2
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

if ! have_cmd pkg; then
  die "This installer is meant for Termux. Run it inside Termux where 'pkg' exists."
fi

log "Refreshing Termux package lists..."
pkg update -y || warn "pkg update failed; continuing with the current package lists."

if ! have_cmd python; then
  log "Installing python..."
  pkg install -y python || die "Failed to install python with pkg."
else
  log "python already installed."
fi

if ! have_cmd ffmpeg; then
  log "Installing ffmpeg..."
  pkg install -y ffmpeg || die "Failed to install ffmpeg with pkg."
else
  log "ffmpeg already installed."
fi

log "Updating pip tooling..."
python -m pip install --upgrade pip setuptools wheel || warn "pip tooling upgrade failed; continuing anyway."

if [ -f "$PROJECT_DIR/requirements.txt" ]; then
  log "Installing Python dependencies from requirements.txt..."
  python -m pip install -r "$PROJECT_DIR/requirements.txt" || die "pip install failed."
else
  log "No requirements.txt found."
  log "This project currently uses only the Python standard library."
fi

if [ -d "$HOME/storage" ]; then
  log "Storage access already looks available."
else
  warn "Termux storage is not mounted yet."
  warn "Run this once if you want the default Camera folder to work:"
  warn "  termux-setup-storage"
fi

log "Checking project script syntax..."
python -m py_compile "$PROJECT_DIR/analyzer_gemini.py" || die "Python syntax check failed."

log ""
log "Setup complete."
log "Run this from the project directory:"
log "  python analyzer_gemini.py"
log "  python analyzer_gemini.py --limit 6"
log ""
log "If your videos are not in the default camera folders, pass the folder explicitly:"
log "  python analyzer_gemini.py --dir /path/to/videos"
log ""
log "You can also set an environment variable:"
log "  export CHAIN_SCAN_DIR=/path/to/videos"
log "  python analyzer_gemini.py"
