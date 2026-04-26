"""Phase 4b.5 — auto-update.

Two-phase swap pattern (Windows-safe):
    Run N: check_and_stage_update()  → download new zip to staged/, write marker
    Run N+1 start: apply_pending_update()  → file-by-file copy from staged/
                                             into install/ (atomic rename per file)

Why two-phase: Windows can't reliably replace files that are currently loaded
by a running Python process. So we download in this run, apply at the start
of the next run (before importing anything beyond stdlib + this module).

File-by-file copy is safe because Python opens .py files briefly to compile
to bytecode then releases the handle. The currently-running process stays
on the bytecode it already has in memory; the next process invocation reads
the new .py files.

Auto-update is a no-op in dev mode (running from the source repo with no
INSTALLED_VERSION marker file). All functions log to the agent logger and
swallow errors — auto-update must NEVER block the upload work.

CLI (for manual testing):
    python3 -m promem_agent.updater status     # print pending/throttle state
    python3 -m promem_agent.updater check      # manifest check, print, no stage
    python3 -m promem_agent.updater apply      # apply pending update now
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from promem_agent import __version__
from promem_agent.watcher import _platform_default_dir as _data_dir


DEFAULT_MANIFEST_URL = "https://promem.fly.dev/agent/manifest"
UPDATE_CHECK_INTERVAL_SEC = 3600   # 1 hour
HTTP_TIMEOUT = 30.0

log = logging.getLogger("promem_agent.updater")


class UpdateError(Exception):
    """Update flow failed (manifest fetch, sha256 mismatch, IO error)."""


# ── Path helpers ─────────────────────────────────────────────────────────
def _install_dir() -> Path:
    """The directory that CONTAINS the promem_agent package — i.e. the
    extract root of the agent zip on production installs."""
    return Path(__file__).resolve().parent.parent


def _staged_dir() -> Path:
    return _data_dir() / "staged"


def _pending_marker() -> Path:
    return _data_dir() / ".pending_update.json"


def _throttle_marker() -> Path:
    return _data_dir() / ".last_update_check"


def _manifest_url() -> str:
    return os.environ.get("PROMEM_MANIFEST_URL", DEFAULT_MANIFEST_URL).strip()


# ── Dev-mode detection ───────────────────────────────────────────────────
def is_dev_install() -> bool:
    """True if running from the source repo (no INSTALLED_VERSION marker).
    All auto-update functions become no-ops in dev mode."""
    return not (_install_dir() / "INSTALLED_VERSION").exists()


# ── Version comparison ───────────────────────────────────────────────────
def _parse_version(v: str) -> tuple[int, ...]:
    """Parse 'X.Y.Z' into (X, Y, Z) for comparison. Tolerates non-numeric
    suffixes by stripping them ('0.2.0-beta' → (0, 2, 0))."""
    parts = []
    for p in v.split("."):
        digits = ""
        for ch in p:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _is_newer(latest: str, current: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


def _meets_min_compat(min_compat: str | None, current: str) -> bool:
    if not min_compat:
        return True
    return _parse_version(current) >= _parse_version(min_compat)


# ── Throttle ─────────────────────────────────────────────────────────────
def _check_throttled() -> bool:
    """Return True if we checked the manifest less than UPDATE_CHECK_INTERVAL_SEC ago."""
    marker = _throttle_marker()
    if not marker.exists():
        return False
    try:
        last = datetime.fromisoformat(marker.read_text().strip())
    except (ValueError, OSError):
        return False
    age = (datetime.now(timezone.utc) - last).total_seconds()
    return age < UPDATE_CHECK_INTERVAL_SEC


def _record_check() -> None:
    marker = _throttle_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(datetime.now(timezone.utc).isoformat())


# ── Pending marker I/O ───────────────────────────────────────────────────
def _read_pending() -> dict | None:
    p = _pending_marker()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning("pending marker unreadable (%s); ignoring", e)
        return None


def _write_pending(target_version: str) -> None:
    p = _pending_marker()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "target_version": target_version,
        "from_version": __version__,
        "staged_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2))


def _delete_pending() -> None:
    try:
        _pending_marker().unlink()
    except FileNotFoundError:
        pass


# ── Manifest + download ──────────────────────────────────────────────────
def _fetch_manifest() -> dict:
    url = _manifest_url()
    r = httpx.get(url, timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        raise UpdateError(f"manifest fetch failed: HTTP {r.status_code} from {url}")
    try:
        return r.json()
    except ValueError as e:
        raise UpdateError(f"manifest is not valid JSON: {e}") from e


def _download_and_verify(url: str, expected_sha256: str) -> Path:
    """Stream-download zip to a tempfile, compute sha256 as we go, raise on
    mismatch. Returns path to the verified tempfile (caller deletes after use)."""
    tmp = tempfile.NamedTemporaryFile(
        delete=False, suffix=".zip", prefix="promem_agent_update_",
    )
    sha = hashlib.sha256()
    try:
        with httpx.stream("GET", url, timeout=HTTP_TIMEOUT) as r:
            if r.status_code != 200:
                raise UpdateError(f"download failed: HTTP {r.status_code} from {url}")
            for chunk in r.iter_bytes(chunk_size=64 * 1024):
                sha.update(chunk)
                tmp.write(chunk)
        tmp.close()
        actual = sha.hexdigest()
        if actual.lower() != expected_sha256.lower():
            raise UpdateError(
                f"sha256 mismatch: expected={expected_sha256}, got={actual}"
            )
        return Path(tmp.name)
    except Exception:
        tmp.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def _extract_to_staged(zip_path: Path) -> None:
    """Extract zip into staged/, replacing any prior staged contents."""
    staged = _staged_dir()
    if staged.exists():
        shutil.rmtree(staged)
    staged.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(staged)


# ── Apply (the two-phase swap, run N+1 start) ────────────────────────────
def apply_pending_update() -> str | None:
    """If a pending update is staged, copy each staged file into the install
    dir via atomic-rename (per-file). Returns the version applied, or None
    if nothing was pending. Logs and returns None on failure (does NOT raise
    — agent must keep running on the existing version)."""
    if is_dev_install():
        return None
    pending = _read_pending()
    if not pending:
        return None

    target_version = pending.get("target_version", "?")
    staged = _staged_dir()
    install = _install_dir()

    if not staged.exists():
        log.warning("pending marker says %s but staged/ is missing; clearing marker",
                    target_version)
        _delete_pending()
        return None

    try:
        copied = 0
        for staged_file in staged.rglob("*"):
            if not staged_file.is_file():
                continue
            rel = staged_file.relative_to(staged)
            dest = install / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Atomic per-file: write to .tmp sibling, then os.replace.
            tmp_dest = dest.with_suffix(dest.suffix + ".update_tmp")
            shutil.copy2(staged_file, tmp_dest)
            os.replace(tmp_dest, dest)
            copied += 1

        # Update the version marker last (so partial updates don't claim success).
        (install / "INSTALLED_VERSION").write_text(target_version)
        shutil.rmtree(staged, ignore_errors=True)
        _delete_pending()
        log.info("applied update %s → %s (%d file(s) copied)",
                 pending.get("from_version", "?"), target_version, copied)
        return target_version
    except Exception as e:
        log.error("apply_pending_update failed (%s); leaving staged/ intact for next run", e)
        return None


# ── Check + stage (run N end) ────────────────────────────────────────────
def check_and_stage_update() -> str | None:
    """Throttled manifest check. If a newer version is available and we
    meet its min_compat, download + verify + stage it. Returns the version
    staged, or None. Logs and returns None on failure."""
    if is_dev_install():
        return None
    if _check_throttled():
        log.debug("update check throttled (last < %ds ago)", UPDATE_CHECK_INTERVAL_SEC)
        return None

    try:
        manifest = _fetch_manifest()
    except UpdateError as e:
        log.warning("update check failed: %s", e)
        # Still record the check — don't hammer the manifest if it's down.
        _record_check()
        return None

    _record_check()
    latest = str(manifest.get("latest", "")).strip()
    url    = str(manifest.get("url", "")).strip()
    sha    = str(manifest.get("sha256", "")).strip()
    min_compat = manifest.get("min_compat_version")

    if not latest or not url or not sha:
        log.warning("manifest missing required fields (latest/url/sha256): %s", manifest)
        return None

    if not _is_newer(latest, __version__):
        log.debug("manifest latest=%s, current=%s; no update needed", latest, __version__)
        return None

    if not _meets_min_compat(min_compat, __version__):
        log.warning(
            "update %s requires min_compat=%s but current=%s; skipping (manual reinstall needed)",
            latest, min_compat, __version__,
        )
        return None

    try:
        zip_path = _download_and_verify(url, sha)
    except UpdateError as e:
        log.warning("update download/verify failed: %s", e)
        return None

    try:
        _extract_to_staged(zip_path)
        _write_pending(latest)
        log.info("staged update for next run: %s → %s", __version__, latest)
        return latest
    except Exception as e:
        log.warning("update extract/stage failed: %s", e)
        return None
    finally:
        try:
            zip_path.unlink()
        except OSError:
            pass


# ── CLI ──────────────────────────────────────────────────────────────────
def _cli_status() -> int:
    print(f"current_version:   {__version__}")
    print(f"install_dir:       {_install_dir()}")
    print(f"is_dev_install:    {is_dev_install()}")
    print(f"manifest_url:      {_manifest_url()}")
    print(f"data_dir:          {_data_dir()}")
    print(f"staged_dir_exists: {_staged_dir().exists()}")
    pending = _read_pending()
    print(f"pending:           {json.dumps(pending) if pending else '<none>'}")
    if _throttle_marker().exists():
        try:
            last = datetime.fromisoformat(_throttle_marker().read_text().strip())
            age = (datetime.now(timezone.utc) - last).total_seconds()
            print(f"last_check:        {last.isoformat()} ({int(age)}s ago)")
        except Exception:
            print(f"last_check:        <unreadable>")
    else:
        print(f"last_check:        <never>")
    return 0


def _cli_check() -> int:
    if is_dev_install():
        print("dev install — auto-update is a no-op here. (Tip: touch "
              f"{_install_dir() / 'INSTALLED_VERSION'} to simulate prod.)")
        return 0
    try:
        manifest = _fetch_manifest()
    except UpdateError as e:
        print(f"UpdateError: {e}", file=sys.stderr)
        return 1
    latest = manifest.get("latest", "?")
    print(json.dumps(manifest, indent=2))
    if _is_newer(latest, __version__):
        print(f"\n→ newer version available: {__version__} → {latest}")
    else:
        print(f"\n→ already at latest: {__version__}")
    return 0


def _cli_apply() -> int:
    applied = apply_pending_update()
    if applied:
        print(f"applied update: now at version {applied}")
        return 0
    if is_dev_install():
        print("dev install — apply_pending_update() is a no-op.", file=sys.stderr)
        return 0
    print("no pending update to apply.")
    return 0


def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "status":
        return _cli_status()
    if cmd == "check":
        return _cli_check()
    if cmd == "apply":
        return _cli_apply()
    print(f"Unknown command: {cmd}. Try status | check | apply", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
