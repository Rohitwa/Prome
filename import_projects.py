"""Copy projects + deliverables from a local SQLite prome.db into the
multi-user Supabase Postgres, scoped by PROMEM_USER_ID.

Usage:
    python3 import_projects.py /path/to/old/prome.db
    python3 import_projects.py                          # defaults to ~/Desktop/memory/pmis_v2/data/prome.db

Safe to re-run — uses ON CONFLICT (id) DO NOTHING.

Side-effects:
  - Backfills `projects` rows for the current user.
  - Backfills `deliverables` rows for the current user (only those whose
    project_id resolves to a project we just inserted/own).
  - Re-running the orchestrator afterwards will pick them up in
    phase_match + phase_synthesis and populate deliverable wikis.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

# Make the sibling `db.py` importable when run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db


DEFAULT_SOURCE = Path("/Users/rohitsingh/Desktop/memory/pmis_v2/data/prome.db")


def import_from(source_db: Path) -> dict:
    if not source_db.exists():
        raise SystemExit(f"Source DB not found: {source_db}")
    user_id = db.user_id()

    src = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row

    projects = [dict(r) for r in src.execute(
        "SELECT id, name, owner, description, status, created_at FROM projects"
    ).fetchall()]
    deliverables = [dict(r) for r in src.execute(
        "SELECT id, project_id, title, description, keywords, ctx_hints, status, created_at "
        "FROM deliverables"
    ).fetchall()]
    src.close()

    n_p_inserted = 0
    n_d_inserted = 0
    project_ids_owned: set[str] = set()

    with db.conn() as c:
        for p in projects:
            cur = c.execute("""
                INSERT INTO projects (id, user_id, name, owner, description, status, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
            """, (p["id"], user_id, p["name"], p.get("owner") or "",
                  p.get("description") or "", p.get("status") or "active",
                  p.get("created_at") or ""))
            if cur.rowcount:
                n_p_inserted += 1
            project_ids_owned.add(p["id"])

        for d in deliverables:
            if d["project_id"] not in project_ids_owned:
                continue
            # Validate JSON columns; pass through if valid, else default to '[]'.
            kws = d.get("keywords") or "[]"
            ctxs = d.get("ctx_hints") or "[]"
            try: json.loads(kws)
            except Exception: kws = "[]"
            try: json.loads(ctxs)
            except Exception: ctxs = "[]"
            cur = c.execute("""
                INSERT INTO deliverables
                  (id, user_id, project_id, title, description,
                   keywords, ctx_hints, status, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
            """, (d["id"], user_id, d["project_id"], d["title"],
                  d.get("description") or "", kws, ctxs,
                  d.get("status") or "in progress",
                  d.get("created_at") or ""))
            if cur.rowcount:
                n_d_inserted += 1

    return {
        "ok": True,
        "source": str(source_db),
        "user_id": user_id,
        "projects_seen": len(projects),
        "projects_inserted": n_p_inserted,
        "deliverables_seen": len(deliverables),
        "deliverables_inserted": n_d_inserted,
    }


if __name__ == "__main__":
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SOURCE
    print(json.dumps(import_from(src), indent=2))
