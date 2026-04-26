"""Stage 2 — sync tracker.db.context_1 rows into prome.db.work_pages.

Tracker.db is local SQLite (read-only); prome.db is now Supabase Postgres.
We bridge them: read tracker locally, write work_pages to Postgres scoped
by PROMEM_USER_ID. Idempotent via ON CONFLICT (id) DO NOTHING.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Make sibling `db.py` importable when this file is executed directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db

_DATA_DIR = Path(os.environ.get("PROMEM_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
DEFAULT_TRACKER = Path(os.environ.get("PROMEM_TRACKER_DB", str(_DATA_DIR / "tracker.db")))

INITIAL_BACKFILL_DAYS = 30


def _last_sync(user_id: str) -> datetime | None:
    with db.conn() as c:
        row = c.execute(
            "SELECT last_sync_at FROM orchestrator_state WHERE user_id=%s",
            (user_id,),
        ).fetchone()
    if not row or not row["last_sync_at"]:
        return None
    try:
        return datetime.fromisoformat(row["last_sync_at"])
    except ValueError:
        return None


def _cutoff(user_id: str) -> str:
    """Pull from max(last_sync_at, today - INITIAL_BACKFILL_DAYS).
    Returns an ISO string suitable for `WHERE timestamp_start > ?`."""
    last = _last_sync(user_id)
    backfill = datetime.now() - timedelta(days=INITIAL_BACKFILL_DAYS)
    if last is None or last < backfill:
        return backfill.isoformat()
    return last.isoformat()


def sync_work_pages(tracker_db: str | Path = DEFAULT_TRACKER) -> dict:
    """Read new context_1 rows and insert them into prome.work_pages.
    Returns counts. Does not write to tracker.db."""
    user_id = db.user_id()
    tracker_db = Path(tracker_db)
    if not tracker_db.exists():
        return {
            "ok": False, "phase": "sync", "skipped": True,
            "reason": f"tracker.db not found at {tracker_db}",
            "n_seen": 0, "n_inserted": 0,
        }

    cutoff = _cutoff(user_id)

    tconn = sqlite3.connect(f"file:{tracker_db}?mode=ro", uri=True)
    tconn.row_factory = sqlite3.Row
    rows = list(tconn.execute("""
        SELECT id, target_segment_id, timestamp_start, timestamp_end,
               target_segment_length_secs, short_title, window_name,
               detailed_summary, supercontext, context AS ctx_label
        FROM context_1
        WHERE timestamp_start > ?
        ORDER BY timestamp_start
    """, (cutoff,)))
    tconn.close()

    n_seen = len(rows)
    n_inserted = 0
    with db.conn() as c:
        # Make sure orchestrator_state row exists for this user.
        c.execute(
            "INSERT INTO orchestrator_state (user_id) VALUES (%s) "
            "ON CONFLICT (user_id) DO NOTHING",
            (user_id,),
        )
        for r in rows:
            date_local = (r["timestamp_start"] or "")[:10]
            if not date_local:
                continue
            title = (r["short_title"] or r["window_name"] or "Untitled").strip()
            summary = (r["detailed_summary"] or "").strip()
            minutes = (r["target_segment_length_secs"] or 0) / 60.0
            cur = c.execute("""
                INSERT INTO work_pages
                  (id, user_id, title, summary, date_local, total_minutes,
                   source_segment_count, sc_label, ctx_label, classified_at)
                VALUES (%s, %s, %s, %s, %s, %s, 1, '', '', NULL)
                ON CONFLICT (id) DO NOTHING
            """, (r["id"], user_id, title[:200], summary[:2000],
                  date_local, round(minutes, 2)))
            if cur.rowcount:
                n_inserted += 1

    return {
        "ok": True, "phase": "sync", "skipped": False,
        "cutoff": cutoff, "n_seen": n_seen, "n_inserted": n_inserted,
    }
