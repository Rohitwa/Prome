"""Stage 2 — sync tracker.db.context_1 rows into prome.db.work_pages.

context_1 already has `short_title` and `detailed_summary` per segment, so
sync is a pure mapping: read tracker (read-only) → INSERT OR IGNORE into prome.

Idempotent: same context_1 row keeps the same prome work_page id, so re-runs
no-op on rows already pulled.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

_DATA_DIR = Path(os.environ.get("PROMEM_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
DEFAULT_TRACKER = Path(os.environ.get("PROMEM_TRACKER_DB", str(_DATA_DIR / "tracker.db")))

INITIAL_BACKFILL_DAYS = 30


def _last_sync(prome_conn: sqlite3.Connection) -> datetime | None:
    row = prome_conn.execute(
        "SELECT last_sync_at FROM orchestrator_state WHERE id=1"
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except ValueError:
        return None


def _cutoff(prome_conn: sqlite3.Connection) -> str:
    """Pull from max(last_sync_at, today - INITIAL_BACKFILL_DAYS).
    Returns an ISO string suitable for `WHERE timestamp_start > ?`."""
    last = _last_sync(prome_conn)
    backfill = datetime.now() - timedelta(days=INITIAL_BACKFILL_DAYS)
    if last is None or last < backfill:
        return backfill.isoformat()
    return last.isoformat()


def sync_work_pages(
    prome_db: str | Path,
    tracker_db: str | Path = DEFAULT_TRACKER,
) -> dict:
    """Read new context_1 rows and insert them into prome.work_pages.
    Returns counts. Does not write to tracker.db."""
    prome_db, tracker_db = Path(prome_db), Path(tracker_db)
    if not tracker_db.exists():
        return {
            "ok": False, "phase": "sync", "skipped": True,
            "reason": f"tracker.db not found at {tracker_db}",
            "n_seen": 0, "n_inserted": 0,
        }

    pconn = sqlite3.connect(prome_db)
    pconn.row_factory = sqlite3.Row
    cutoff = _cutoff(pconn)

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
    for r in rows:
        date_local = (r["timestamp_start"] or "")[:10]
        if not date_local:
            continue
        title = (r["short_title"] or r["window_name"] or "Untitled").strip()
        summary = (r["detailed_summary"] or "").strip()
        minutes = (r["target_segment_length_secs"] or 0) / 60.0
        cur = pconn.execute("""
            INSERT OR IGNORE INTO work_pages
              (id, title, summary, date_local, total_minutes,
               source_segment_count, sc_label, ctx_label, classified_at)
            VALUES (?, ?, ?, ?, ?, 1, '', '', NULL)
        """, (r["id"], title[:200], summary[:2000], date_local, round(minutes, 2)))
        if cur.rowcount:
            n_inserted += 1
    pconn.commit()
    pconn.close()

    return {
        "ok": True, "phase": "sync", "skipped": False,
        "cutoff": cutoff, "n_seen": n_seen, "n_inserted": n_inserted,
    }
