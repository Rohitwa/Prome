"""ProMem agent entrypoint — invoked by Windows Task Scheduler.

Composes oauth + watcher + uploader into the actual agent loop and adds
operational concerns: subcommand dispatch, structured logging, dry-run.

Subcommands:
    init      one-time OAuth login, save refresh_token in OS keyring
    run       main loop: fetch new tracker segments, upload, advance state
    dry-run   like 'run' but prints what would upload (no POST, no state change)
    status    read-only health check (auth, tracker.db, state, log)

Exit codes:
    0  success
    1  unhandled exception
    2  auth error (recoverable: re-run `init`)
    3  upload error (recoverable: will retry on next scheduled run)
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import sys
import time
import traceback
from datetime import datetime, timezone

import keyring

from promem_agent import __version__, oauth, updater
from promem_agent.uploader import MAX_BATCH, UploadError, upload_frames, upload_segments
from promem_agent.watcher import TrackerWatcher, default_state_path


LOG_FILE_NAME = "agent.log"
LOG_FORMAT    = "%(asctime)s %(levelname)-5s %(name)s %(message)s"
LOG_DATEFMT   = "%Y-%m-%dT%H:%M:%S"

log = logging.getLogger("promem_agent")


def _setup_logging(verbose: bool) -> None:
    """Rotating file handler is always on (1MB × 3 backups, lives next to
    the state file). Stdout handler only if --verbose."""
    log_path = default_state_path().parent / LOG_FILE_NAME
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    fh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8",
    )
    fh.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATEFMT))
    fh.setLevel(logging.DEBUG)
    root.addHandler(fh)

    if verbose:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATEFMT))
        sh.setLevel(logging.DEBUG)
        root.addHandler(sh)


# ── Subcommands ──────────────────────────────────────────────────────────
def cmd_init(args: argparse.Namespace) -> int:
    """One-time OAuth login. Invoked by the NSIS installer at install time."""
    print("Opening browser for one-time login...")
    try:
        access_token = oauth.first_run_login()
    except oauth.AuthError as e:
        print(f"AuthError: {e}", file=sys.stderr)
        return 2
    payload = oauth.whoami(access_token)
    print(f"Logged in as {payload.get('email', '?')} ({payload.get('sub', '?')}).")

    w = TrackerWatcher()
    if not w.tracker_db.exists():
        print(f"\nNote: tracker.db not found at {w.tracker_db}.")
        print("The agent will start uploading once the tracker is running.")
    else:
        peek = w.fetch_new_segments(limit=1)
        if peek:
            print(f"\nFound queued segments in tracker.db (first ts={peek[0]['timestamp_start']}).")
            print("To preview the queue:   python3 -m promem_agent dry-run")
        else:
            print("\nNo queued segments yet.")

    print("\nProMem agent is ready.")
    print("Task Scheduler will run the agent every 5 minutes (configured by installer).")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Main loop. Used by Task Scheduler. Logs to file; minimal stdout."""
    log.info("agent run start (version=%s)", __version__)
    started = time.monotonic()

    # Apply any pending update from a previous run (no-op in dev or if none staged).
    try:
        applied = updater.apply_pending_update()
        if applied:
            log.info("update applied at startup: now running %s", applied)
    except Exception:
        log.warning("apply_pending_update raised (continuing on current version):\n%s",
                    traceback.format_exc())

    try:
        w = TrackerWatcher()
        total_seg_received = 0
        total_seg_inserted = 0
        total_frame_received = 0
        total_frame_inserted = 0
        iterations = 0
        while True:
            segs = w.fetch_new_segments(limit=MAX_BATCH)
            if not segs:
                break
            iterations += 1
            log.debug("iter %d: uploading %d segment(s)", iterations, len(segs))
            seg_result = upload_segments(segs)
            total_seg_received += seg_result.n_received
            total_seg_inserted += seg_result.n_inserted

            # Phase 4d: ship the per-frame context_2 rows that belong to the
            # segments we just uploaded. Done BEFORE mark_uploaded so a frame
            # upload failure doesn't advance the segment cutoff.
            seg_ids = [s["target_segment_id"] for s in segs if s.get("target_segment_id")]
            frames = w.fetch_frames_for_segments(seg_ids) if seg_ids else []
            if frames:
                frame_result = upload_frames(frames)
                total_frame_received += frame_result.n_received
                total_frame_inserted += frame_result.n_inserted
                log.debug("iter %d: uploaded %d frame(s)", iterations, len(frames))

            w.mark_uploaded(segs)
        duration = time.monotonic() - started
        if iterations == 0:
            log.info("nothing to upload (queue empty); duration=%.2fs", duration)
        else:
            log.info(
                "uploaded %d segment(s) + %d frame(s) in %d iteration(s) "
                "(seg received=%d inserted=%d, frame received=%d inserted=%d); duration=%.2fs",
                total_seg_inserted, total_frame_inserted, iterations,
                total_seg_received, total_seg_inserted,
                total_frame_received, total_frame_inserted, duration,
            )

        # Throttled manifest check (no-op in dev or if checked < 1h ago).
        try:
            staged = updater.check_and_stage_update()
            if staged:
                log.info("staged update for next run: %s", staged)
        except Exception:
            log.warning("check_and_stage_update raised (will retry next run):\n%s",
                        traceback.format_exc())

        return 0
    except oauth.AuthError as e:
        log.error("AuthError: %s — re-run `python3 -m promem_agent init` to recover", e)
        return 2
    except UploadError as e:
        log.error("UploadError: %s", e)
        return 3
    except Exception:
        log.error("Unhandled exception:\n%s", traceback.format_exc())
        return 1


def cmd_dry_run(args: argparse.Namespace) -> int:
    """Like 'run' but no POST and no state advance. Prints sample payload."""
    print("# Dry run — no POST, no state advance")
    w = TrackerWatcher()
    if not w.tracker_db.exists():
        print(f"tracker.db not found at {w.tracker_db}", file=sys.stderr)
        return 0
    state = w.get_state()
    print(f"# Cutoff:           {w._cutoff()}")
    print(f"# Last uploaded id: {state.get('last_uploaded_id', '<none>')}")
    print()

    segs = w.fetch_new_segments(limit=MAX_BATCH)
    print(f"# {len(segs)} segment(s) queued (showing up to 5):")
    if not segs:
        return 0
    print(json.dumps(segs[:5], indent=2, default=str))
    if len(segs) > 5:
        print(f"# ... {len(segs) - 5} more segment(s) not shown")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Read-only health check. Always exits 0 (info command, parse output)."""
    w = TrackerWatcher()
    log_path = default_state_path().parent / LOG_FILE_NAME

    print(f"ProMem agent v{__version__}")

    # Auth
    refresh = None
    try:
        refresh = keyring.get_password(oauth.KEYRING_SERVICE, oauth.KEYRING_USER)
    except Exception as e:
        print(f"  auth:       x keyring read failed: {e}")
    if refresh is None:
        print("  auth:       x no refresh_token in keyring (run `init`)")
    else:
        try:
            tok = oauth.get_access_token()
            payload = oauth.whoami(tok)
            print(f"  auth:       ok logged in as {payload.get('email', '?')}")
        except oauth.AuthError as e:
            print(f"  auth:       x refresh failed: {e}")

    # Tracker
    if w.tracker_db.exists():
        mtime = datetime.fromtimestamp(w.tracker_db.stat().st_mtime, tz=timezone.utc)
        age = (datetime.now(timezone.utc) - mtime).total_seconds()
        print(f"  tracker.db: ok {w.tracker_db} (last modified {int(age)}s ago)")
    else:
        print(f"  tracker.db: x not found at {w.tracker_db}")

    # State
    state = w.get_state()
    if state:
        print(f"  state:      last_uploaded_ts={state.get('last_uploaded_timestamp_start')}")
        print(f"              last_run_at=    {state.get('last_run_at')}")
    else:
        print(f"  state:      <empty> (cutoff={w._cutoff()})")

    # Log
    if log_path.exists():
        size = log_path.stat().st_size
        mtime = datetime.fromtimestamp(log_path.stat().st_mtime, tz=timezone.utc)
        age = (datetime.now(timezone.utc) - mtime).total_seconds()
        print(f"  log:        {log_path} ({size}B, last write {int(age)}s ago)")
    else:
        print(f"  log:        not yet created at {log_path}")

    return 0


# ── argparse ─────────────────────────────────────────────────────────────
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="promem_agent",
        description=f"ProMem agent v{__version__} — uploads local tracker segments to cloud.",
    )
    p.add_argument("--version", action="version", version=f"promem_agent v{__version__}")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="also log to stdout (default: log file only)")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("init",    help="one-time OAuth login, save refresh_token")
    sub.add_parser("run",     help="fetch new tracker segments and upload")
    sub.add_parser("dry-run", help="like 'run' but prints what would upload")
    sub.add_parser("status",  help="read-only health check")
    return p


def _main(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv[1:])
    _setup_logging(args.verbose)

    handlers = {
        "init":    cmd_init,
        "run":     cmd_run,
        "dry-run": cmd_dry_run,
        "status":  cmd_status,
    }
    handler = handlers.get(args.cmd)
    if handler is None:
        parser.print_help()
        return 2
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
