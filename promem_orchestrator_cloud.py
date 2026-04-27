"""Multi-user cloud orchestrator — invoked by the Fly scheduler (4c.5c).

Iterates active users (anyone with rows in tracker_segments), runs the
requested phases per-user with error isolation. One failing user does
not stop the loop for the rest.

Loop entry points (called by APScheduler in promem_app.py):
  run_fast_loop()  → every 30 min, sync only (raw data → work_pages)
  run_slow_loop()  → 14:00 + 18:00 UTC, classify + filter + match + synthesis

CLI for manual testing (any machine with PROMEM_DB_URL set):
  python3 -m promem_orchestrator_cloud list   # show active users
  python3 -m promem_orchestrator_cloud fast   # sync for all
  python3 -m promem_orchestrator_cloud slow   # LLM phases for all
  python3 -m promem_orchestrator_cloud all    # everything for all
"""

from __future__ import annotations

import json
import os
import sys
import time

import db
from promem_orchestrator import PHASES, run_full_for_user

# Cloud orchestrator ALWAYS reads from tracker_segments (Postgres), never
# from a local tracker.db file. Set this once at module import so every
# downstream call to sync.py picks it up.
os.environ.setdefault("PROMEM_SYNC_SOURCE", "cloud")


def list_active_users() -> list[str]:
    """User_ids that have at least one row in tracker_segments. Naturally
    bounded — brand-new users with no data don't get processed (and have
    nothing to process anyway). They auto-join the loop on first upload.

    Avoids querying auth.users (which may have RLS issues when the
    orchestrator runs as a non-authenticated service-role connection)."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT DISTINCT user_id::text AS uid FROM tracker_segments"
        ).fetchall()
    return [r["uid"] for r in rows]


def run_for_all_users(reason: str = "scheduled",
                      phases: list | None = None) -> dict:
    """Run `phases` (default: ALL of PHASES) for every active user.
    Errors isolated per user; one failure does not stop the others."""
    started = time.monotonic()
    user_ids = list_active_users()
    print(f"orchestrator_cloud: {len(user_ids)} active user(s); reason={reason}")

    summary = {
        "reason": reason,
        "n_users": len(user_ids),
        "n_ok": 0,
        "n_failed": 0,
        "per_user": {},
    }
    for uid in user_ids:
        try:
            r = run_full_for_user(uid, reason=reason, phases=phases)
            summary["per_user"][uid] = {"ok": r.get("ok", False)}
            if r.get("ok"):
                summary["n_ok"] += 1
            else:
                summary["n_failed"] += 1
        except Exception as e:
            print(f"  x user {uid}: {e}")
            summary["per_user"][uid] = {"ok": False, "error": str(e)}
            summary["n_failed"] += 1
    summary["duration_secs"] = round(time.monotonic() - started, 2)
    print(f"orchestrator_cloud: done — ok={summary['n_ok']} "
          f"failed={summary['n_failed']} ({summary['duration_secs']}s)")
    return summary


# ── Loop entry points (Fly scheduler calls these) ──────────────────────
def run_fast_loop() -> dict:
    """Every 30 min — sync only (raw data → work_pages). No LLM calls.
    Raw activity flows to the wiki within ~30 min of upload."""
    return run_for_all_users(reason="fast_loop_sync", phases=PHASES[:1])


def run_slow_loop() -> dict:
    """Twice daily (14:00 + 18:00 UTC) — classify + filter + match +
    synthesis. All LLM-heavy phases batched together."""
    return run_for_all_users(reason="slow_loop_llm", phases=PHASES[1:])


# ── CLI for manual testing ─────────────────────────────────────────────
def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "fast"

    if cmd == "list":
        users = list_active_users()
        print(f"{len(users)} active user(s):")
        for u in users:
            print(f"  - {u}")
        return 0

    if cmd == "fast":
        result = run_fast_loop()
    elif cmd == "slow":
        result = run_slow_loop()
    elif cmd == "all":
        result = run_for_all_users(reason="manual")
    else:
        print(f"Unknown command: {cmd}. Try list | fast | slow | all", file=sys.stderr)
        return 2

    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
