"""Stage 3 — split classified work_pages into keep vs archive.

Pure SQL — no LLM. A page is archived iff its sc_label is registered with
is_keep=0 in sc_registry.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db


def filter_pages() -> dict:
    user_id = db.user_id()
    with db.conn() as c:
        cur = c.execute("""
            UPDATE work_pages
               SET is_archived = CASE
                 WHEN (SELECT is_keep FROM sc_registry
                       WHERE user_id = work_pages.user_id
                         AND label = work_pages.sc_label) = 0
                   THEN 1 ELSE 0 END
             WHERE user_id = %s
               AND sc_label != ''
               AND COALESCE(is_unfiled, 0) = 0
        """, (user_id,))
        n_updated = cur.rowcount

        n_keep = c.execute(
            "SELECT COUNT(*) AS n FROM work_pages "
            "WHERE user_id=%s AND is_archived=0 AND COALESCE(is_unfiled,0)=0 AND sc_label!=''",
            (user_id,),
        ).fetchone()["n"]
        n_arch = c.execute(
            "SELECT COUNT(*) AS n FROM work_pages "
            "WHERE user_id=%s AND is_archived=1 AND COALESCE(is_unfiled,0)=0",
            (user_id,),
        ).fetchone()["n"]
        n_unfiled = c.execute(
            "SELECT COUNT(*) AS n FROM work_pages "
            "WHERE user_id=%s AND COALESCE(is_unfiled,0)=1",
            (user_id,),
        ).fetchone()["n"]
        n_unc = c.execute(
            "SELECT COUNT(*) AS n FROM work_pages "
            "WHERE user_id=%s AND sc_label='' AND COALESCE(is_unfiled,0)=0",
            (user_id,),
        ).fetchone()["n"]

    return {
        "ok": True, "phase": "filter", "skipped": False,
        "n_updated": n_updated,
        "n_keep": n_keep, "n_archive": n_arch,
        "n_unfiled": n_unfiled, "n_unclassified": n_unc,
    }
