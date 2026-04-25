"""Stage 3 — split classified work_pages into keep vs archive.

Pure SQL — no LLM. A page is archived iff its sc_label is registered with
is_keep=0 in sc_registry.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def filter_pages(prome_db: str | Path) -> dict:
    prome_db = Path(prome_db)
    conn = sqlite3.connect(prome_db, timeout=30.0)
    try:
        # Set is_archived from sc_registry.is_keep (0 = archive, 1 = keep)
        cur = conn.execute("""
            UPDATE work_pages
               SET is_archived = CASE
                 WHEN (SELECT is_keep FROM sc_registry WHERE label = work_pages.sc_label) = 0
                   THEN 1 ELSE 0 END
             WHERE sc_label != ''
               AND COALESCE(is_unfiled, 0) = 0
        """)
        n_updated = cur.rowcount
        conn.commit()

        n_keep    = conn.execute("SELECT COUNT(*) FROM work_pages WHERE is_archived=0 AND COALESCE(is_unfiled,0)=0 AND sc_label!=''").fetchone()[0]
        n_arch    = conn.execute("SELECT COUNT(*) FROM work_pages WHERE is_archived=1 AND COALESCE(is_unfiled,0)=0").fetchone()[0]
        n_unfiled = conn.execute("SELECT COUNT(*) FROM work_pages WHERE COALESCE(is_unfiled,0)=1").fetchone()[0]
        n_unc     = conn.execute("SELECT COUNT(*) FROM work_pages WHERE sc_label='' AND COALESCE(is_unfiled,0)=0").fetchone()[0]
    finally:
        conn.close()

    return {
        "ok": True, "phase": "filter", "skipped": False,
        "n_updated": n_updated,
        "n_keep": n_keep, "n_archive": n_arch,
        "n_unfiled": n_unfiled, "n_unclassified": n_unc,
    }
