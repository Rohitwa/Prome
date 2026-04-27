#!/usr/bin/env bash
# Release a new ProMem Windows installer (single EXE, no Python preinstall).
#
# Usage:
#   ./release.sh <version>     # e.g. ./release.sh 0.2.0
#
# What it does:
#   1. Bumps __version__ in promem_agent/__init__.py
#   2. Verifies installer payload binaries are present
#      (built separately on Windows via installer/build_pyinstaller_windows.ps1)
#   3. Builds installer/promem_setup_<version>_x64.exe
#   4. Copies final EXE to dist/ and prints gh release command

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ "$#" -lt 1 ]; then
  echo "usage: $0 <version>     (e.g. $0 0.2.0)" >&2
  exit 1
fi
VERSION="$1"

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "error: version '$VERSION' must be X.Y.Z" >&2
  exit 1
fi

DEFAULT_TRACKER_SRC="/Users/rohitsingh/Desktop/memory/.claude/worktrees/tracker-act-monitor/productivity-tracker"
TRACKER_SRC="${PROMEM_TRACKER_SRC:-$DEFAULT_TRACKER_SRC}"

for cmd in python3 makensis git; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "error: '$cmd' not found on PATH." >&2
    case "$cmd" in
      makensis) echo "       Install with: brew install makensis" >&2 ;;
    esac
    exit 1
  fi
done

if [ -n "$(git status --porcelain promem_agent installer release.sh .gitignore README.md 2>/dev/null)" ]; then
  echo "warning: working tree has uncommitted changes in release-relevant files:"
  git status --short promem_agent installer release.sh .gitignore README.md
  echo
  read -r -p "continue anyway? [y/N] " ans
  [ "$ans" = "y" ] || exit 1
fi

AGENT_EXE="installer/payload/bin/promem_agent/promem_agent.exe"
TRACKER_EXE="installer/payload/bin/promem_tracker/promem_tracker.exe"
if [ ! -f "$AGENT_EXE" ] || [ ! -f "$TRACKER_EXE" ]; then
  echo "error: payload binaries are missing." >&2
  echo "  expected: $AGENT_EXE" >&2
  echo "  expected: $TRACKER_EXE" >&2
  echo >&2
  echo "Build payload on a Windows x64 machine first:" >&2
  echo "  PROMEM_TRACKER_SRC=\"$TRACKER_SRC\" powershell -ExecutionPolicy Bypass -File installer\\build_pyinstaller_windows.ps1" >&2
  exit 1
fi

echo "→ Bumping promem_agent/__init__.py to $VERSION ..."
python3 - "$VERSION" <<'PY'
import re, sys
version = sys.argv[1]
p = "promem_agent/__init__.py"
src = open(p).read()
new, n = re.subn(r'__version__ = "[^"]+"', f'__version__ = "{version}"', src)
if n == 0:
    sys.exit("could not find __version__ line in " + p)
if new != src:
    open(p, "w").write(new)
PY

ACTUAL=$(python3 -c "from promem_agent import __version__; print(__version__)")
if [ "$ACTUAL" != "$VERSION" ]; then
  echo "error: version bump failed (got '$ACTUAL', expected '$VERSION')." >&2
  exit 1
fi

echo "→ Building NSIS installer ..."
./installer/build.sh

OUT="installer/promem_setup_${VERSION}_x64.exe"
if [ ! -f "$OUT" ]; then
  echo "error: expected installer not found at $OUT" >&2
  exit 1
fi

mkdir -p dist
cp -f "$OUT" "dist/"
BYTES=$(wc -c < "dist/promem_setup_${VERSION}_x64.exe" | tr -d ' ')

echo
echo "==================================================================="
echo "Release v${VERSION} built"
echo "==================================================================="
echo
echo "Artifact: dist/promem_setup_${VERSION}_x64.exe (${BYTES} bytes)"
echo
echo "Publish on GitHub:"
echo
echo "  gh release create v${VERSION} \\
    dist/promem_setup_${VERSION}_x64.exe \\
    --title \"v${VERSION}\" \\
    --notes \"Windows installer (x64). No Python preinstall required.\""
echo
