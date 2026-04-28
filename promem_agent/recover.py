"""Phase 4 v0.1.10 — recover idle data on disk that hasn't reached the cloud.

Two recovery cases this command handles:

  1. Rows in tracker.db that the agent's regular `run` would have skipped.
     This happens when:
       - The agent's state file cutoff is wrong/drifted (e.g. carried over
         from a prior install where uploads were succeeding under a
         different user_id, advancing the cutoff past rows we'd now want
         to upload under the current user).
       - Uploads silently failed before v0.1.7 because of the path bug,
         leaving rows captured but never POSTed.

  2. Tracker.db files at LEGACY paths from prior install layouts. The
     biggest one is ~/.productivity-tracker/tracker.db, which the tracker
     used as its default before v0.1.7 honored PROMEM_TRACKER_DB.

Also reports orphan PNGs — screenshots on disk not referenced by any
Context2 row (typically because the tracker crashed mid-segment, before
finalize). Those PNGs are unrecoverable noise; --delete-orphans removes
them.
"""

from __future__ import annotations

import logging
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from promem_agent.uploader import (
    MAX_BATCH,
    UploadError,
    upload_frames,
    upload_segments,
)
from promem_agent.watcher import TrackerWatcher, default_tracker_db


log = logging.getLogger("promem_agent.recover")


# Historical tracker.db default paths from earlier installer versions.
# Keep ordered most-likely-first; the tracker's default before v0.1.7 was
# the .productivity-tracker dir under HOME.
LEGACY_TRACKER_DB_PATHS = [
    Path.home() / ".productivity-tracker" / "tracker.db",
]


# ── Read-only scans ──────────────────────────────────────────────────────
def _scan_db_summary(db_path: Path, days: int) -> dict:
    """Return {exists, count, earliest, latest} for context_1 rows newer
    than `now - days`. On any error: {exists, error}."""
    if not db_path.exists():
        return {"exists": False}
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0) as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT COUNT(*) AS n, MIN(timestamp_start) AS mn, MAX(timestamp_start) AS mx "
                "FROM context_1 WHERE timestamp_start > ?",
                (cutoff,),
            ).fetchone()
        return {
            "exists":   True,
            "path":     str(db_path),
            "count":    row["n"] or 0,
            "earliest": row["mn"],
            "latest":   row["mx"],
        }
    except Exception as e:
        return {"exists": True, "path": str(db_path), "error": str(e)}


def _scan_orphan_pngs(db_path: Path) -> tuple[list[Path], int, int]:
    """Walk segment_dir paths from this tracker.db and find *.png files not
    referenced by any Context2 row. Returns (orphan_paths, ref_count, total_on_disk)."""
    if not db_path.exists():
        return [], 0, 0

    referenced: set[str] = set()
    seg_dirs: set[str] = set()
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0) as c:
            c.row_factory = sqlite3.Row
            try:
                for row in c.execute(
                    "SELECT screenshot_path FROM context_2 "
                    "WHERE screenshot_path IS NOT NULL AND screenshot_path != ''"
                ).fetchall():
                    referenced.add(str(Path(row["screenshot_path"]).resolve()))
            except sqlite3.OperationalError:
                pass  # context_2 / screenshot_path may not exist on older schemas
            try:
                for row in c.execute(
                    "SELECT segment_dir FROM context_1 "
                    "WHERE segment_dir IS NOT NULL AND segment_dir != ''"
                ).fetchall():
                    seg_dirs.add(row["segment_dir"])
            except sqlite3.OperationalError:
                pass
    except Exception as e:
        log.warning("orphan scan: failed to read %s: %s", db_path, e)
        return [], 0, 0

    on_disk: set[Path] = set()
    for sd in seg_dirs:
        sd_path = Path(sd)
        if sd_path.exists():
            for p in sd_path.rglob("*.png"):
                try:
                    on_disk.add(p.resolve())
                except OSError:
                    continue

    orphans = sorted(p for p in on_disk if str(p) not in referenced)
    return orphans, len(referenced), len(on_disk)


# ── Backfill ─────────────────────────────────────────────────────────────
def _backfill_db(db_path: Path, days: int, use_real_state: bool) -> tuple[int, int]:
    """Upload all rows from db_path with timestamp_start newer than `now - days`.

    use_real_state=True (primary tracker.db): cutoff respects the agent's
        existing state file; upload advances that state. Mirrors normal `run`
        but with the backfill window as the floor.
    use_real_state=False (legacy paths): uses a temp state file so cutoff is
        purely `now - days`. Real state is left untouched.

    Returns (n_segments_inserted, n_frames_inserted). Raises UploadError on
    unrecoverable upload failure."""
    temp_state: Path | None = None
    try:
        if use_real_state:
            w = TrackerWatcher(tracker_db=db_path, backfill_days=days)
        else:
            tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
            tmp.write("{}")
            tmp.close()
            temp_state = Path(tmp.name)
            w = TrackerWatcher(
                tracker_db=db_path, state_path=temp_state, backfill_days=days,
            )

        segs_inserted = 0
        frames_inserted = 0
        while True:
            segs = w.fetch_new_segments(limit=MAX_BATCH)
            if not segs:
                break
            seg_result = upload_segments(segs)
            segs_inserted += seg_result.n_inserted

            seg_ids = [s["target_segment_id"] for s in segs if s.get("target_segment_id")]
            frames = w.fetch_frames_for_segments(seg_ids) if seg_ids else []
            if frames:
                fr_result = upload_frames(frames)
                frames_inserted += fr_result.n_inserted

            w.mark_uploaded(segs)

        return segs_inserted, frames_inserted
    finally:
        if temp_state is not None:
            try:
                temp_state.unlink()
            except OSError:
                pass


# ── CLI entrypoint ───────────────────────────────────────────────────────
def recover(apply: bool, delete_orphans: bool, days: int = 30) -> int:
    """Diagnose idle data; with --apply backfill rows; with --delete-orphans
    remove unreferenced PNGs. Returns process exit code."""
    print(f"# ProMem recover  (lookback: {days} days, apply={apply}, delete_orphans={delete_orphans})")
    print()

    primary = default_tracker_db()
    legacies = [p for p in LEGACY_TRACKER_DB_PATHS if p != primary]

    # 1. Scan all known tracker.db locations
    print("# Tracker.db locations:")
    summaries: list[tuple[Path, dict, bool]] = []  # (path, summary, is_primary)
    for p, is_primary in [(primary, True)] + [(lp, False) for lp in legacies]:
        s = _scan_db_summary(p, days)
        summaries.append((p, s, is_primary))
        role = "primary" if is_primary else "legacy"
        if not s.get("exists"):
            print(f"  [{role}] {p}: not present")
        elif "error" in s:
            print(f"  [{role}] {p}: ERROR {s['error']}")
        else:
            print(f"  [{role}] {p}: {s['count']} rows in last {days}d  [{s.get('earliest','-')} -> {s.get('latest','-')}]")
    print()

    # 2. Orphan PNG scan across all locations
    print("# Orphan PNGs (on disk, no Context2 row referencing them):")
    all_orphans: list[Path] = []
    for p, s, _ in summaries:
        if not s.get("exists"):
            continue
        orphs, ref_count, on_disk_count = _scan_orphan_pngs(p)
        if on_disk_count == 0:
            print(f"  {p}: no PNGs on disk under any segment_dir")
        else:
            try:
                sz_mb = sum(o.stat().st_size for o in orphs if o.exists()) / 1_000_000
            except OSError:
                sz_mb = 0
            print(f"  {p}: {len(orphs)} orphan / {on_disk_count} total ({sz_mb:.1f} MB orphan, {ref_count} referenced)")
            all_orphans.extend(orphs)
    print()

    # 3. Apply: backfill unsynced rows
    if apply:
        print("# Applying — backfilling unsynced rows...")
        for p, s, is_primary in summaries:
            if not s.get("exists") or s.get("count", 0) == 0:
                continue
            try:
                segs, frames = _backfill_db(p, days, use_real_state=is_primary)
                role = "primary" if is_primary else "legacy"
                print(f"  [{role}] {p}: inserted {segs} segments, {frames} frames "
                      f"(re-uploads dedup'd server-side via ON CONFLICT)")
            except UploadError as e:
                print(f"  [{role if is_primary else 'legacy'}] {p}: UPLOAD FAILED — {e}")
                return 3
        print()

    # 4. Delete orphan PNGs
    if delete_orphans:
        if not all_orphans:
            print("# --delete-orphans: no orphan PNGs to delete.")
        else:
            print(f"# Deleting {len(all_orphans)} orphan PNG(s)...")
            deleted = 0
            for o in all_orphans:
                try:
                    o.unlink()
                    deleted += 1
                except OSError as e:
                    log.warning("could not delete %s: %s", o, e)
            print(f"  deleted: {deleted}")
        print()

    if not apply and not delete_orphans:
        print("# Read-only summary printed. To act, re-run with:")
        print("#   --apply             upload unsynced rows from above tracker.db files")
        print("#   --delete-orphans    delete PNGs without a Context2 row")

    return 0
