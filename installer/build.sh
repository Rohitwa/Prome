#!/usr/bin/env bash
# Build promem_setup_<version>_x64.exe from prebuilt payload binaries.
#
# Usage:
#   ./installer/build.sh
#
# Prerequisites:
#   1) installer/payload/bin/promem_agent/promem_agent.exe exists
#   2) installer/payload/bin/promem_tracker/promem_tracker.exe exists
#   3) makensis installed (brew install makensis)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

if ! command -v makensis >/dev/null 2>&1; then
  echo "error: makensis not found on PATH." >&2
  echo "       Install with: brew install makensis" >&2
  exit 1
fi

if [ ! -f "promem_agent/__init__.py" ]; then
  echo "error: promem_agent/__init__.py missing; run from repo root." >&2
  exit 1
fi

VERSION=$(python3 -c "from promem_agent import __version__; print(__version__)")
if [ -z "$VERSION" ]; then
  echo "error: failed to read __version__ from promem_agent/__init__.py" >&2
  exit 1
fi

AGENT_EXE="installer/payload/bin/promem_agent/promem_agent.exe"
TRACKER_EXE="installer/payload/bin/promem_tracker/promem_tracker.exe"

if [ ! -f "$AGENT_EXE" ] || [ ! -f "$TRACKER_EXE" ]; then
  echo "error: installer payload is missing required binaries." >&2
  echo "  expected: $AGENT_EXE" >&2
  echo "  expected: $TRACKER_EXE" >&2
  echo >&2
  echo "Build payload on Windows first:" >&2
  echo "  powershell -ExecutionPolicy Bypass -File installer/build_pyinstaller_windows.ps1" >&2
  exit 1
fi

echo "Building NSIS installer for ProMem v$VERSION ..."

cd installer
makensis -DAGENT_VERSION="$VERSION" setup.nsi

OUT="promem_setup_${VERSION}_x64.exe"
if [ ! -f "$OUT" ]; then
  echo "error: makensis completed but $OUT was not produced." >&2
  exit 1
fi

EXE_BYTES=$(wc -c < "$OUT" | tr -d ' ')
echo
echo "✓ Built: installer/$OUT (${EXE_BYTES} bytes)"
