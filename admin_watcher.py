"""Cloud-side admin watcher — runs every 5 min via APScheduler.

Detects two conditions, writes to admin_alerts:

  silent          — user has activity in last 24h but no upload in last 30m
  pipeline_stuck  — segments uploaded but 0 work_pages (orchestrator skipping)

Auto-resolves alerts when the underlying condition clears. Cooldown of 60m
between repeat 'silent' alerts for the same user prevents notification spam.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import db


SILENT_THRESHOLD_MIN  = 30
RE_ALERT_COOLDOWN_MIN = 60


def run_watcher() -> dict[str, Any]:
    """Scan all role='user' org_members for silent / stuck conditions,
    insert new alerts, auto-resolve recovered ones. Returns a summary dict."""
    now = datetime.now(timezone.utc)
    n_silent = 0
    n_stuck = 0
    n_resolved = 0

    with db.conn() as c:
        silent_users = c.execute("""
            SELECT om.user_id, MAX(ts.uploaded_at) AS last_upload
            FROM org_members om
            JOIN tracker_segments ts ON ts.user_id = om.user_id
            WHERE om.role = 'user'
              AND ts.uploaded_at > NOW() - INTERVAL '24 hours'
            GROUP BY om.user_id
            HAVING MAX(ts.uploaded_at) < NOW() - INTERVAL '30 minutes'
        """).fetchall()

        for row in silent_users:
            uid = row["user_id"]
            recent = c.execute("""
                SELECT 1 FROM admin_alerts
                WHERE user_id = %s AND alert_type = 'silent'
                  AND created_at > NOW() - INTERVAL '60 minutes'
                LIMIT 1
            """, (uid,)).fetchone()
            if recent:
                continue
            mins = (now - row["last_upload"]).total_seconds() / 60
            c.execute("""
                INSERT INTO admin_alerts (user_id, alert_type, message)
                VALUES (%s, 'silent', %s)
            """, (uid, f"No upload in {mins:.0f} min (last: {row['last_upload']})"))
            n_silent += 1

        stuck_users = c.execute("""
            SELECT om.user_id
            FROM org_members om
            WHERE om.role = 'user'
              AND EXISTS (SELECT 1 FROM tracker_segments WHERE user_id = om.user_id LIMIT 1)
              AND NOT EXISTS (SELECT 1 FROM work_pages WHERE user_id = om.user_id LIMIT 1)
        """).fetchall()

        for row in stuck_users:
            uid = row["user_id"]
            open_alert = c.execute("""
                SELECT 1 FROM admin_alerts
                WHERE user_id = %s AND alert_type = 'pipeline_stuck'
                  AND resolved_at IS NULL
                LIMIT 1
            """, (uid,)).fetchone()
            if open_alert:
                continue
            c.execute("""
                INSERT INTO admin_alerts (user_id, alert_type, message)
                VALUES (%s, 'pipeline_stuck',
                        'Segments uploaded but no work_pages — orchestrator skipped this user')
            """, (uid,))
            n_stuck += 1

        resolved_silent = c.execute("""
            UPDATE admin_alerts
            SET resolved_at = now()
            WHERE alert_type = 'silent' AND resolved_at IS NULL
              AND EXISTS (
                  SELECT 1 FROM tracker_segments
                  WHERE user_id = admin_alerts.user_id
                    AND uploaded_at > NOW() - INTERVAL '5 minutes'
              )
            RETURNING id
        """).fetchall()
        n_resolved += len(resolved_silent)

        resolved_stuck = c.execute("""
            UPDATE admin_alerts
            SET resolved_at = now()
            WHERE alert_type = 'pipeline_stuck' AND resolved_at IS NULL
              AND EXISTS (
                  SELECT 1 FROM work_pages
                  WHERE user_id = admin_alerts.user_id LIMIT 1
              )
            RETURNING id
        """).fetchall()
        n_resolved += len(resolved_stuck)

    return {
        "silent_alerts_created": n_silent,
        "pipeline_stuck_alerts_created": n_stuck,
        "alerts_resolved": n_resolved,
    }


if __name__ == "__main__":
    print(run_watcher())
