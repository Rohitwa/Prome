#!/usr/bin/env bash
# Build promem_setup_<version>.exe on macOS via brew makensis.
#
# Usage:   ./installer/build.sh
# Output:  installer/promem_setup_<version>.exe
#
# Prerequisites:  brew install makensis  (one-time)

set -euo pipefail

# Resolve paths relative to this script (so it works from any cwd).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

# ── Sanity checks ─────────────────────────────────────────────────────────
if ! command -v makensis >/dev/null 2>&1; then
  echo "error: makensis not found on PATH." >&2
  echo "       Install with:  brew install makensis" >&2
  exit 1
fi

if [ ! -f "promem_agent/__init__.py" ]; then
  echo "error: must run from a Prome repo (promem_agent/__init__.py missing)." >&2
  exit 1
fi

VERSION=$(python3 -c "from promem_agent import __version__; print(__version__)")
if [ -z "$VERSION" ]; then
  echo "error: could not read __version__ from promem_agent/__init__.py" >&2
  exit 1
fi

echo "Building installer for promem_agent v$VERSION ..."

# ── Stage agent files into a clean zip ────────────────────────────────────
# INSTALLED_VERSION marker tells updater.py this is a real install (not dev).
# Created here, baked into zip, removed after build so dev mode keeps working.
trap 'rm -f promem_agent/INSTALLED_VERSION' EXIT
echo "$VERSION" > promem_agent/INSTALLED_VERSION

# Clean any prior zip + .pyc cruft.
rm -f installer/agent.zip
find promem_agent -name '__pycache__' -type d -prune -exec rm -rf {} +
find promem_agent -name '*.pyc' -delete

# Build the zip with two top-level entries: promem_agent/ and requirements-agent.txt.
zip -qr installer/agent.zip \
    promem_agent \
    requirements-agent.txt \
    -x '*__pycache__*' \
    -x '*.pyc'

ZIP_BYTES=$(wc -c < installer/agent.zip | tr -d ' ')
echo "  agent.zip: ${ZIP_BYTES} bytes"

# ── Compile the installer ─────────────────────────────────────────────────
cd installer
makensis -DAGENT_VERSION="$VERSION" setup.nsi

OUT="promem_setup_${VERSION}.exe"
if [ -f "$OUT" ]; then
  EXE_BYTES=$(wc -c < "$OUT" | tr -d ' ')
  echo
  echo "✓ Built: installer/$OUT (${EXE_BYTES} bytes)"
  echo "  Test on Windows: copy to your Windows machine and double-click."
else
  echo "error: makensis succeeded but $OUT was not produced." >&2
  exit 1
fi

# Clean staging artifact (leave the .exe).
rm -f agent.zip
