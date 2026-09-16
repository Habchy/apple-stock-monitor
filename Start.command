#!/bin/bash
# Run from this script's directory even when Finder launches it from elsewhere.
# This script installs nothing, needs no administrator rights, and never handles
# the webhook. The Python app prompts privately and saves it outside the repo.
set -u
ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
PYTHON=""

# Finder can have a minimal PATH. Try both standard Homebrew locations before
# falling back to PATH. Older Apple developer-tool Python builds are skipped.
for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys, curses; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    PYTHON="$candidate"
    break
  fi
done

if [ -z "$PYTHON" ]; then
  printf '\nPython 3.10 or newer with curses is required.\n'
  printf 'Install Python from python.org, or run: brew install python\n'
  printf 'Then open this launcher again. There are no app pip dependencies.\n\n'
  if [ -t 0 ]; then read -r -p 'Press Return to close.' _; fi
  exit 1
fi

# Pass optional flags through verbatim. Replacing the shell keeps exit behavior
# simple; the app owns its keep-awake assertion and releases it on exit.
cd -- "$ROOT"
exec "$PYTHON" "$ROOT/tui.py" "$@"
