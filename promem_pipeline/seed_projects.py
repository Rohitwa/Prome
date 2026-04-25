"""Seed prome.db.projects + deliverables with the 3 projects we used in
the project_tracker_sim simulation. Reuses PROJECTS literal — single source
of truth so the seed and the sim agree.

Idempotent — INSERT OR IGNORE so re-running is a no-op.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "prome.db"


def _import_projects() -> list[dict]:
    sys.path.insert(0, str(ROOT / "simulations"))
    from project_tracker_sim import PROJECTS  # type: ignore
    return PROJECTS


def seed(prome_db: str | Path = DB) -> dict:
    projects = _import_projects()
    conn = sqlite3.connect(prome_db, timeout=30.0)
    now = datetime.now().isoformat(timespec="seconds")
    n_projects, n_deliverables = 0, 0
    try:
        for p in projects:
            cur = conn.execute(
                "INSERT OR IGNORE INTO projects (id, name, owner, description, status, created_at) "
                "VALUES (?, ?, ?, ?, 'active', ?)",
                (p["id"], p["name"], p.get("owner", ""), p.get("description", ""), now),
            )
            if cur.rowcount:
                n_projects += 1
            for d in p.get("deliverables", []):
                cur = conn.execute(
                    "INSERT OR IGNORE INTO deliverables "
                    "(id, project_id, title, description, keywords, ctx_hints, "
                    " status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'in progress', ?)",
                    (d["id"], p["id"], d["title"], d.get("description", ""),
                     json.dumps(d.get("keywords", [])),
                     json.dumps(d.get("ctx_hints", [])),
                     now),
                )
                if cur.rowcount:
                    n_deliverables += 1
        conn.commit()
        n_p_total = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        n_d_total = conn.execute("SELECT COUNT(*) FROM deliverables").fetchone()[0]
    finally:
        conn.close()
    return {
        "ok": True, "phase": "seed",
        "n_projects_inserted": n_projects,
        "n_deliverables_inserted": n_deliverables,
        "n_projects_total": n_p_total,
        "n_deliverables_total": n_d_total,
    }


if __name__ == "__main__":
    print(json.dumps(seed(), indent=2))
