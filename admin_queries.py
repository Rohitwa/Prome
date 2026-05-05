"""Org admin dashboard — read-only aggregations across all users.

These queries deliberately omit `WHERE user_id = %s` so they roll up the
entire org. Authorization is enforced at the route level by
`require_admin()` in auth.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import psycopg


ALIVE_MIN_7D = 5.0


def _age_health(ts: Any) -> str:
    """Map a datetime/None to a health bucket: green (<1h), amber (<24h),
    red (older or null). Used by /admin/user/<id> stat cards."""
    if not ts:
        return "red"
    if not isinstance(ts, datetime):
        return "red"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    if age < 3600:
        return "green"
    if age < 86400:
        return "amber"
    return "red"


def org_productivity_7d(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Per-user productivity, last 7 days.

    Returns: user_id, email, role, mins_today, mins_7d,
             human_mins_7d, ai_mins_7d, last_active.
    """
    sql = """
    WITH
    work_agg AS (
        SELECT user_id,
               SUM(total_minutes) AS mins_7d,
               SUM(CASE WHEN date_local = to_char(CURRENT_DATE, 'YYYY-MM-DD')
                        THEN total_minutes ELSE 0 END) AS mins_today,
               MAX(classified_at) AS last_active
        FROM work_pages
        WHERE date_local >= to_char(CURRENT_DATE - INTERVAL '7 days', 'YYYY-MM-DD')
        GROUP BY user_id
    ),
    seg_agg AS (
        SELECT user_id,
               COALESCE(SUM(target_segment_length_secs)
                        FILTER (WHERE worker = 'human'), 0) / 60.0 AS human_mins_7d,
               COALESCE(SUM(target_segment_length_secs)
                        FILTER (WHERE worker = 'ai'),    0) / 60.0 AS ai_mins_7d
        FROM tracker_segments
        WHERE timestamp_start >= to_char(CURRENT_DATE - INTERVAL '7 days', 'YYYY-MM-DD')
        GROUP BY user_id
    )
    SELECT om.user_id, om.email, om.role,
           COALESCE(wa.mins_today, 0)    AS mins_today,
           COALESCE(wa.mins_7d, 0)       AS mins_7d,
           COALESCE(sa.human_mins_7d, 0) AS human_mins_7d,
           COALESCE(sa.ai_mins_7d, 0)    AS ai_mins_7d,
           wa.last_active,
           (SELECT COUNT(*) FROM admin_alerts
              WHERE user_id = om.user_id AND resolved_at IS NULL) AS open_alerts,
           (SELECT MAX(uploaded_at) FROM tracker_segments
              WHERE user_id = om.user_id) AS last_upload
    FROM org_members om
    LEFT JOIN work_agg wa ON wa.user_id = om.user_id
    LEFT JOIN seg_agg  sa ON sa.user_id = om.user_id
    ORDER BY om.role DESC, mins_7d DESC NULLS LAST, om.email
    """
    return list(conn.execute(sql).fetchall())


def org_project_rollup_7d(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Per-project rollup with owner + deliverable count + last-7d minutes.

    Returns: id, name, status, owner_user_id, owner_email,
             deliverable_count, mins_7d, is_alive.
    """
    sql = """
    SELECT p.id, p.name, p.status,
           p.user_id AS owner_user_id,
           om.email  AS owner_email,
           COUNT(DISTINCT d.id) AS deliverable_count,
           COALESCE(SUM(wp.total_minutes), 0) AS mins_7d,
           (COALESCE(SUM(wp.total_minutes), 0) >= %s) AS is_alive
    FROM projects p
    LEFT JOIN org_members om ON om.user_id = p.user_id
    LEFT JOIN deliverables d ON d.project_id = p.id
    LEFT JOIN deliverable_match dm ON dm.deliverable_id = d.id
    LEFT JOIN work_pages wp ON wp.id = dm.page_id
        AND wp.date_local >= to_char(CURRENT_DATE - INTERVAL '7 days', 'YYYY-MM-DD')
    GROUP BY p.id, p.name, p.status, p.user_id, om.email
    ORDER BY mins_7d DESC, p.name
    """
    return list(conn.execute(sql, (ALIVE_MIN_7D,)).fetchall())


def org_deliverables_7d(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Per-deliverable rollup. assignee = deliverables.user_id (top-down model).

    Returns: id, title, status, project_id, project_name,
             assignee_user_id, assignee_email, mins_7d.
    """
    sql = """
    SELECT d.id, d.title, d.status,
           p.id   AS project_id,
           p.name AS project_name,
           d.user_id AS assignee_user_id,
           om.email  AS assignee_email,
           COALESCE(SUM(wp.total_minutes), 0) AS mins_7d
    FROM deliverables d
    JOIN projects p ON p.id = d.project_id
    LEFT JOIN org_members om ON om.user_id = d.user_id
    LEFT JOIN deliverable_match dm ON dm.deliverable_id = d.id
    LEFT JOIN work_pages wp ON wp.id = dm.page_id
        AND wp.date_local >= to_char(CURRENT_DATE - INTERVAL '7 days', 'YYYY-MM-DD')
    GROUP BY d.id, d.title, d.status, p.id, p.name, d.user_id, om.email
    ORDER BY p.name, d.title
    """
    return list(conn.execute(sql).fetchall())


def org_pulse(conn: psycopg.Connection) -> dict[str, Any]:
    """Org-wide live data pulse — counts of tracker_segments and tracker_frames
    written across all users in the last 5 min / 1 hour / 24 hours, plus the
    latest upload timestamp. Used by the /admin pulse banner."""
    sql_template = """
    SELECT
        COUNT(*) FILTER (WHERE uploaded_at > NOW() - INTERVAL '5 minutes')  AS last_5m,
        COUNT(*) FILTER (WHERE uploaded_at > NOW() - INTERVAL '1 hour')     AS last_1h,
        COUNT(*) FILTER (WHERE uploaded_at > NOW() - INTERVAL '24 hours')   AS last_24h,
        COUNT(DISTINCT user_id) FILTER (WHERE uploaded_at > NOW() - INTERVAL '5 minutes') AS users_5m,
        COUNT(DISTINCT user_id) FILTER (WHERE uploaded_at > NOW() - INTERVAL '1 hour')    AS users_1h,
        MAX(uploaded_at) AS latest_upload
    FROM {table}
    """
    seg = dict(conn.execute(sql_template.format(table="tracker_segments")).fetchone())
    frm = dict(conn.execute(sql_template.format(table="tracker_frames")).fetchone())
    return {
        "segments": seg,
        "frames": frm,
        "segments_health": _age_health(seg.get("latest_upload")),
        "frames_health": _age_health(frm.get("latest_upload")),
    }


def org_user_activity_feed(
    conn: psycopg.Connection, user_id: str, limit: int = 20
) -> list[dict[str, Any]]:
    """Merged chronological feed for /admin user panels — alerts, force-sync
    actions, and recent segment uploads interleaved by timestamp.

    Each row: {ts, kind, subtype, message, status}.
    kind: alert_created | alert_resolved | action_started | action_finished | segment_uploaded
    """
    sql = """
    WITH events AS (
        SELECT created_at AS ts, 'alert_created' AS kind, alert_type AS subtype,
               message, NULL::text AS status
        FROM admin_alerts WHERE user_id = %(uid)s

        UNION ALL

        SELECT resolved_at AS ts, 'alert_resolved' AS kind, alert_type AS subtype,
               'resolved: ' || COALESCE(message, alert_type) AS message,
               NULL::text AS status
        FROM admin_alerts
        WHERE user_id = %(uid)s AND resolved_at IS NOT NULL

        UNION ALL

        SELECT started_at AS ts, 'action_started' AS kind, action AS subtype,
               action AS message, status
        FROM admin_action_log WHERE user_id = %(uid)s

        UNION ALL

        SELECT finished_at AS ts, 'action_finished' AS kind, action AS subtype,
               action || ' → ' || status AS message, status
        FROM admin_action_log
        WHERE user_id = %(uid)s AND finished_at IS NOT NULL

        UNION ALL

        SELECT uploaded_at AS ts, 'segment_uploaded' AS kind,
               COALESCE(worker, '?') AS subtype,
               COALESCE(NULLIF(window_name, ''), NULLIF(short_title, ''), '(no title)') AS message,
               CASE WHEN is_productive = 1 THEN 'productive' ELSE NULL END AS status
        FROM tracker_segments WHERE user_id = %(uid)s
    )
    SELECT * FROM events
    WHERE ts IS NOT NULL
    ORDER BY ts DESC
    LIMIT %(lim)s
    """
    return list(conn.execute(sql, {"uid": user_id, "lim": limit}).fetchall())


def org_user_detail(
    conn: psycopg.Connection, user_id: str
) -> dict[str, Any]:
    """Per-user pipeline + activity snapshot for /admin/user/<id>.

    Sections:
      profile          — email, role, joined_at, auth_created_at
      uploads          — segment + frame counts, first/last/upload timestamps
      classification   — work_pages totals, kept/archived/unfiled, total mins
      orchestrator     — last_sync_at, last_classify_at, last_match_at,
                          last_synthesis_at, last_error
      recent_segments  — last 20 tracker_segments
      recent_pages     — last 20 work_pages
    """
    profile = conn.execute("""
        SELECT om.email, om.role, om.joined_at,
               u.created_at AS auth_created_at
        FROM org_members om
        LEFT JOIN auth.users u ON u.id = om.user_id
        WHERE om.user_id = %s
    """, (user_id,)).fetchone()

    if not profile:
        return {"profile": None}

    uploads = conn.execute("""
        SELECT
          (SELECT COUNT(*) FROM tracker_segments WHERE user_id = %(uid)s) AS segments,
          (SELECT COUNT(*) FROM tracker_frames   WHERE user_id = %(uid)s) AS frames,
          (SELECT MIN(timestamp_start) FROM tracker_segments WHERE user_id = %(uid)s) AS first_segment_ts,
          (SELECT MAX(timestamp_start) FROM tracker_segments WHERE user_id = %(uid)s) AS latest_segment_ts,
          (SELECT MAX(uploaded_at)    FROM tracker_segments WHERE user_id = %(uid)s) AS last_upload_at,
          (SELECT COUNT(*) FROM tracker_segments WHERE user_id = %(uid)s
             AND uploaded_at > NOW() - INTERVAL '5 minutes') AS segs_5m,
          (SELECT COUNT(*) FROM tracker_segments WHERE user_id = %(uid)s
             AND uploaded_at > NOW() - INTERVAL '1 hour')    AS segs_1h,
          (SELECT COUNT(*) FROM tracker_segments WHERE user_id = %(uid)s
             AND uploaded_at > NOW() - INTERVAL '24 hours')  AS segs_24h,
          (SELECT COUNT(*) FROM tracker_frames   WHERE user_id = %(uid)s
             AND uploaded_at > NOW() - INTERVAL '5 minutes') AS frames_5m,
          (SELECT COUNT(*) FROM tracker_frames   WHERE user_id = %(uid)s
             AND uploaded_at > NOW() - INTERVAL '1 hour')    AS frames_1h,
          (SELECT COUNT(*) FROM tracker_frames   WHERE user_id = %(uid)s
             AND uploaded_at > NOW() - INTERVAL '24 hours')  AS frames_24h
    """, {"uid": user_id}).fetchone()

    classification = conn.execute("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN classified_at IS NOT NULL THEN 1 ELSE 0 END) AS classified,
               SUM(CASE WHEN classified_at IS NULL     THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN is_archived = 1 THEN 1 ELSE 0 END) AS archived,
               SUM(CASE WHEN is_unfiled  = 1 THEN 1 ELSE 0 END) AS unfiled,
               SUM(CASE WHEN is_archived = 0 AND is_unfiled = 0 AND classified_at IS NOT NULL
                        THEN 1 ELSE 0 END) AS kept,
               COALESCE(SUM(total_minutes), 0) AS total_minutes,
               MAX(classified_at) AS last_classified_at
        FROM work_pages WHERE user_id = %s
    """, (user_id,)).fetchone()

    orchestrator = conn.execute("""
        SELECT last_sync_at, last_classify_at, last_match_at,
               last_synthesis_at, next_due, last_error
        FROM orchestrator_state WHERE user_id = %s
    """, (user_id,)).fetchone()

    recent_segments = list(conn.execute("""
        SELECT timestamp_start, target_segment_length_secs AS secs,
               worker, is_productive, window_name, short_title
        FROM tracker_segments
        WHERE user_id = %s
        ORDER BY timestamp_start DESC
        LIMIT 20
    """, (user_id,)).fetchall())

    recent_pages = list(conn.execute("""
        SELECT id, title, date_local, sc_label, ctx_label,
               total_minutes, is_archived, is_unfiled, classified_at
        FROM work_pages
        WHERE user_id = %s
        ORDER BY classified_at DESC NULLS LAST, date_local DESC
        LIMIT 20
    """, (user_id,)).fetchall())

    uploads_d = dict(uploads)
    cls_d = dict(classification)
    orch_d = dict(orchestrator) if orchestrator else None

    upload_health = _age_health(uploads_d.get("last_upload_at"))
    if (cls_d.get("total") or 0) == 0 and (uploads_d.get("segments") or 0) > 0:
        cls_health = "red"
    elif (cls_d.get("pending") or 0) > 0:
        cls_health = "amber"
    elif (cls_d.get("total") or 0) == 0:
        cls_health = "amber"
    else:
        cls_health = "green"

    if orch_d and orch_d.get("last_error"):
        orch_health = "red"
    else:
        orch_health = _age_health(orch_d.get("last_sync_at") if orch_d else None)

    return {
        "user_id": user_id,
        "profile": dict(profile),
        "uploads": uploads_d,
        "classification": cls_d,
        "orchestrator": orch_d,
        "recent_segments": recent_segments,
        "recent_pages": recent_pages,
        "health": {
            "uploads": upload_health,
            "classification": cls_health,
            "orchestrator": orch_health,
        },
    }
