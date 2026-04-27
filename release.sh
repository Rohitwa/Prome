#!/usr/bin/env bash
# Release a new ProMem agent version.
#
# Usage:    ./release.sh <version>     # e.g. ./release.sh 0.2.0
#
# What it does:
#   1. Bumps __version__ in promem_agent/__init__.py
#   2. Stages an agent zip with INSTALLED_VERSION baked in
#   3. Computes sha256
#   4. Builds the release manifest JSON
#   5. Uploads zip + manifest to Fly's /data volume via flyctl ssh sftp
#   6. Builds the Windows installer (setup.exe) via installer/build.sh
#   7. Prints the gh-release-create command (manual final step on purpose)
#
# Prereqs (one-time):
#   brew install makensis flyctl
#   flyctl auth login
#
# After this script: existing installed agents will pick up the new version
# within ~1 hour (auto-update throttle). New installs use the .exe published
# via `gh release create`.

set -euo pipefail

# ── Resolve paths ─────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Args ──────────────────────────────────────────────────────────────────
if [ "$#" -lt 1 ]; then
  echo "usage: $0 <version>     (e.g. $0 0.2.0)" >&2
  exit 1
fi
VERSION="$1"

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+ ]]; then
  echo "warning: version '$VERSION' doesn't look like X.Y.Z" >&2
  read -r -p "continue anyway? [y/N] " ans
  [ "$ans" = "y" ] || exit 1
fi

# ── Tool checks ───────────────────────────────────────────────────────────
for cmd in flyctl shasum python3 zip makensis git; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "error: '$cmd' not found on PATH." >&2
    case "$cmd" in
      makensis) echo "       Install with:  brew install makensis" >&2 ;;
      flyctl)   echo "       Install with:  brew install flyctl"   >&2 ;;
    esac
    exit 1
  fi
done

# ── Warn on dirty git tree ────────────────────────────────────────────────
if [ -n "$(git status --porcelain promem_agent installer requirements-agent.txt 2>/dev/null)" ]; then
  echo "warning: working tree has uncommitted changes in agent/installer files:"
  git status --short promem_agent installer requirements-agent.txt
  echo
  read -r -p "continue anyway? [y/N] " ans
  [ "$ans" = "y" ] || exit 1
fi

# ── 1. Bump version ───────────────────────────────────────────────────────
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

# ── 2. Stage agent.zip via clean temp dir ─────────────────────────────────
# We assemble all release files in a tempdir, then zip the whole thing.
# This keeps the source repo untouched (no INSTALLED_VERSION pollution)
# and produces a flat zip layout with setup.bat at the root.
echo "→ Staging agent zip ..."
mkdir -p dist
DIST_ZIP="dist/promem_agent-${VERSION}.zip"
rm -f "$DIST_ZIP"

STAGE_DIR=$(mktemp -d)
trap 'rm -rf "$STAGE_DIR"' EXIT

cp -r promem_agent          "$STAGE_DIR/"
cp     requirements-agent.txt "$STAGE_DIR/"
cp     installer/setup.bat   "$STAGE_DIR/"
cp     installer/uninstall.bat "$STAGE_DIR/"
cp     installer/README.txt  "$STAGE_DIR/"

# Bundle productivity-tracker source. Path is overridable via env var so we
# can release from a worktree branch during development; default points at
# the live mainline tracker on the user's Mac.
TRACKER_SRC="${PROMEM_TRACKER_SRC:-/Users/rohitsingh/Desktop/memory/.claude/worktrees/tracker-act-monitor/productivity-tracker}"
if [ ! -d "$TRACKER_SRC" ]; then
  echo "error: productivity-tracker source not found at $TRACKER_SRC" >&2
  echo "       Override with PROMEM_TRACKER_SRC=<path> if it lives elsewhere." >&2
  exit 1
fi
echo "  tracker source: $TRACKER_SRC"
mkdir -p "$STAGE_DIR/productivity-tracker"
# Use rsync if available for clean exclusion of dev artifacts; fall back to cp.
if command -v rsync >/dev/null 2>&1; then
  rsync -a \
    --exclude '.venv/' --exclude '__pycache__/' --exclude '*.pyc' \
    --exclude 'tests/' --exclude '.pytest_cache/' --exclude '.git/' \
    --exclude '.DS_Store' --exclude 'productivity_tracker.egg-info/' \
    --exclude 'data/' --exclude '*.db' --exclude '*.db-*' \
    --exclude 'src/ui/dashboard/node_modules/' \
    "$TRACKER_SRC/" "$STAGE_DIR/productivity-tracker/"
else
  cp -r "$TRACKER_SRC/." "$STAGE_DIR/productivity-tracker/"
fi

# Bake INSTALLED_VERSION into the staged copy (so updater.is_dev_install()
# returns False on installed machines). Source repo stays untouched.
echo "$VERSION" > "$STAGE_DIR/promem_agent/INSTALLED_VERSION"

find "$STAGE_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$STAGE_DIR" -name '*.pyc' -delete
find "$STAGE_DIR" -name '.DS_Store' -delete
find "$STAGE_DIR/productivity-tracker" -path '*/.venv*' -prune -exec rm -rf {} + 2>/dev/null || true

# Zip from inside the stage dir for flat paths (no leading temp/ prefix).
( cd "$STAGE_DIR" && zip -qr "$OLDPWD/$DIST_ZIP" . )

ZIP_BYTES=$(wc -c < "$DIST_ZIP" | tr -d ' ')
echo "  $DIST_ZIP (${ZIP_BYTES} bytes)"

# ── 3. Compute sha256 ─────────────────────────────────────────────────────
SHA256=$(shasum -a 256 "$DIST_ZIP" | awk '{print $1}')
echo "  sha256: $SHA256"

# ── 4. Build manifest JSON ────────────────────────────────────────────────
MANIFEST_PATH="dist/agent_manifest.json"
RELEASED_AT=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
python3 - "$VERSION" "$SHA256" "$RELEASED_AT" <<'PY' > "$MANIFEST_PATH"
import json, sys
version, sha, released_at = sys.argv[1:4]
print(json.dumps({
    "latest": version,
    "url": f"https://promem.fly.dev/agent/dist/promem_agent-{version}.zip",
    "sha256": sha,
    "min_compat_version": "0.0.0",
    "released_at": released_at,
}, indent=2))
PY

echo "  $MANIFEST_PATH:"
sed 's/^/    /' "$MANIFEST_PATH"

# ── 5. Upload to Fly /data via sftp ───────────────────────────────────────
echo
echo "→ Uploading to Fly /data via flyctl ssh sftp ..."
# Wake the Fly machine if auto-stopped — otherwise ssh sftp errors with
# 'app promem has no started VMs'. A simple HTTP GET against any public
# route triggers Fly's auto_start_machines (~2s wake on cold start).
echo "  (waking Fly machine if stopped...)"
curl -s -o /dev/null https://promem.fly.dev/agent/manifest || true
sleep 3

# flyctl sftp `put` won't overwrite (and sftp shell has no `rm`). Use
# `ssh console -C` to delete via the remote shell first. Failures are
# fine (file may not exist on first-time upload).
flyctl ssh console --app promem -C "rm -f /data/promem_agent-${VERSION}.zip /data/agent_manifest.json" >/dev/null 2>&1 || true

flyctl ssh sftp shell --app promem <<SFTP
put $DIST_ZIP /data/promem_agent-${VERSION}.zip
put $MANIFEST_PATH /data/agent_manifest.json
SFTP

echo "  uploaded."

# ── 6. Final instructions ─────────────────────────────────────────────────
echo
echo "==================================================================="
echo "Release v${VERSION} built and uploaded."
echo "==================================================================="
echo
echo "Existing installed agents pick up v${VERSION} within ~1 hour"
echo "(auto-update throttle). Verify the live manifest with:"
echo
echo "  curl https://promem.fly.dev/agent/manifest"
echo
echo "For new first-time installs, publish the agent zip on GitHub:"
echo
echo "  gh release create v${VERSION} \\"
echo "    \"$DIST_ZIP\" \\"
echo "    --title \"v${VERSION}\" \\"
echo "    --notes \"Download promem_agent-${VERSION}.zip, extract, double-click setup.bat. Auto-update users will pick up this release within 1 hour.\""
echo
