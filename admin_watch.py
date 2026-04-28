#!/usr/bin/env python3
"""ProMem live admin watch — terminal dashboard polling Postgres via Fly SSH.

Usage:  python3 admin_watch.py  [--refresh SECONDS]

Refreshes every 10s by default. Shows: per-user segs/frames/work_pages/classified
counts + last upload, plus the cluster-wide last_fast_loop / last_slow_loop times.
Press Ctrl+C to stop.

Why Fly SSH and not direct psycopg: the Supabase direct-DB host is IPv6-only and
this Mac's resolver can't reach it; the Fly machine has no such issue, so we
shell out one query per refresh tick.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone


# Embedded Python script piped to `flyctl ssh ... python3 -`. Avoids nested
# triple-quotes by building SQL via single-line concatenation.
QUERY_SCRIPT = r"""
import json, os
import psycopg
from psycopg.rows import dict_row
from datetime import datetime, timezone

url = os.environ["PROMEM_DB_URL"]
out = {"now": datetime.now(timezone.utc).isoformat()}

SQL = (
    "SELECT u.id, u.email, "
    "(SELECT COUNT(*) FROM tracker_segments s WHERE s.user_id = u.id) AS segs, "
    "(SELECT COUNT(*) FROM tracker_frames   f WHERE f.user_id = u.id) AS frames, "
    "(SELECT COUNT(*) FROM work_pages       w WHERE w.user_id = u.id) AS pages, "
    "(SELECT COUNT(*) FROM work_pages       w WHERE w.user_id = u.id "
    "  AND w.sc_label IS NOT NULL AND w.sc_label <> '') AS classified, "
    "(SELECT MAX(uploaded_at) FROM tracker_segments s WHERE s.user_id = u.id) AS last_upload, "
    "(SELECT last_classify_at FROM orchestrator_state os WHERE os.user_id = u.id) AS last_classify "
    "FROM auth.users u "
    "ORDER BY last_upload DESC NULLS LAST "
    "LIMIT 30"
)

with psycopg.connect(url, row_factory=dict_row) as c:
    rows = list(c.execute(SQL))
    def _iso(v):
        if v is None:
            return None
        if hasattr(v, "isoformat"):
            return v.isoformat()
        return str(v)

    out["users"] = [{
        "email": r["email"] or "-",
        "segs": r["segs"], "frames": r["frames"], "pages": r["pages"],
        "classified": r["classified"],
        "last_upload":   _iso(r["last_upload"]),
        "last_classify": _iso(r["last_classify"]),
    } for r in rows]

    fl = c.execute("SELECT MAX(last_sync_at)     AS t FROM orchestrator_state").fetchone()
    sl = c.execute("SELECT MAX(last_classify_at) AS t FROM orchestrator_state").fetchone()
    tu = c.execute("SELECT COUNT(*) AS n FROM auth.users").fetchone()
    ts = c.execute("SELECT COUNT(*) AS n FROM tracker_segments").fetchone()
    out["last_fast_loop"] = _iso(fl["t"])
    out["last_slow_loop"] = _iso(sl["t"])
    out["total_users"] = tu["n"]
    out["total_segs"]  = ts["n"]

print(json.dumps(out, default=str))
"""


def _ago(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:19]
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)  # assume UTC for naive timestamps
    delta = datetime.now(timezone.utc) - t
    s = int(delta.total_seconds())
    if s < 0:
        return "future?"
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m ago"
    return f"{s // 86400}d ago"


def fetch() -> dict:
    """One-shot query via flyctl ssh + python3 stdin."""
    try:
        result = subprocess.run(
            ["flyctl", "ssh", "console", "--app", "promem", "-C", "python3 -"],
            input=QUERY_SCRIPT,
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {"error": "fly ssh timed out"}
    except FileNotFoundError:
        return {"error": "flyctl not found on PATH"}
    if result.returncode != 0:
        err = (result.stderr or "fly ssh failed").strip().splitlines()
        return {"error": err[-1] if err else "fly ssh failed"}
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"error": "no JSON found in flyctl output"}


def render(data: dict, refresh: int) -> None:
    """Clear screen and reprint the snapshot."""
    sys.stdout.write("\033[2J\033[H")
    print("ProMem live admin watch  ·  refresh", refresh, "s  ·  Ctrl+C to stop")
    print("-" * 100)

    if "error" in data:
        print(f"  ERROR: {data['error']}")
        sys.stdout.flush()
        return

    fl = data.get("last_fast_loop")
    sl = data.get("last_slow_loop")
    print(f"  cluster   : users={data.get('total_users','?'):<4}  total_segs={data.get('total_segs','?'):<6}"
          f"  fast_loop={_ago(fl):<14}  slow_loop={_ago(sl)}")
    print()
    header = f"  {'email':<40} {'segs':>5} {'frames':>7} {'pages':>6} {'class.':>7}  {'last_upload':<14}  {'last_classify':<14}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for u in data.get("users", []):
        line = (f"  {u['email'][:40]:<40} {u['segs']:>5} {u['frames']:>7} {u['pages']:>6} {u['classified']:>7}"
                f"  {_ago(u['last_upload']):<14}  {_ago(u['last_classify']):<14}")
        # Highlight rows with any activity in the last 10 min.
        if u["last_upload"]:
            try:
                t = datetime.fromisoformat(u["last_upload"].replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - t).total_seconds()
                if age < 600:
                    line = "\033[32m" + line + "\033[0m"
            except ValueError:
                pass
        print(line)
    print()
    print(f"  fetched at {data.get('now', '?')[:19]}Z")
    sys.stdout.flush()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--refresh", type=int, default=10, help="seconds between refreshes (default: 10)")
    args = p.parse_args()

    try:
        while True:
            data = fetch()
            render(data, args.refresh)
            time.sleep(args.refresh)
    except KeyboardInterrupt:
        print("\n  stopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
