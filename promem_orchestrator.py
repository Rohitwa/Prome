"""ProMem orchestrator — the nightly pipeline driver.

Runs the end-to-end ingestion → classify → filter → match → synthesize
pipeline against Supabase Postgres, scoped to PROMEM_USER_ID.

CLI:
    python3 promem_orchestrator.py status     # show last_*_at + next_due
    python3 promem_orchestrator.py tick       # cheap check; run if due
    python3 promem_orchestrator.py run        # force full run
    python3 promem_orchestrator.py reset      # clear state (dev only)
    python3 promem_orchestrator.py set-key    # save OPENAI_API_KEY to .env
    python3 promem_orchestrator.py check-key  # verify env keys are set
    python3 promem_orchestrator.py whoami     # show resolved user_id
"""

from __future__ import annotations

import getpass
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"

# db.py loads .env on import, so subsequent imports see all env vars.
sys.path.insert(0, str(ROOT))
import db


def set_key() -> None:
    """Interactive CLI: prompt for OPENAI_API_KEY (masked) and write to .env."""
    if not ENV_FILE.exists():
        ENV_FILE.write_text("OPENAI_API_KEY=\nOPENAI_BASE_URL=https://api.openai.com/v1\n")
    key = getpass.getpass("Paste OPENAI_API_KEY (input is hidden): ").strip()
    if not key:
        print("No key entered. Aborted.")
        return
    if not key.startswith(("sk-", "sk_")):
        print(f"Warning: key doesn't start with 'sk-' (got: {key[:6]}…). Saving anyway.")
    prefix = "sk-proj-" if key.startswith("sk-proj-") else "sk-"
    if key.count(prefix) > 1:
        n = key.count(prefix)
        print(f"⚠ Detected '{prefix}' {n} times in the key — looks like a double-paste.")
        print(f"  Length: {len(key)} chars (an sk-proj key is usually ~160).")
        ans = input("Save anyway? (y/N) ").strip().lower()
        if ans != "y":
            print("Aborted. Re-run set-key and paste once.")
            return
    lines = ENV_FILE.read_text().splitlines()
    out, found = [], False
    for ln in lines:
        if ln.strip().startswith("OPENAI_API_KEY="):
            out.append(f"OPENAI_API_KEY={key}")
            found = True
        else:
            out.append(ln)
    if not found:
        out.append(f"OPENAI_API_KEY={key}")
    ENV_FILE.write_text("\n".join(out) + "\n")
    os.chmod(ENV_FILE, 0o600)
    print(f"OPENAI_API_KEY saved to {ENV_FILE} (file permissions 0600).")
    print("Verify with: python3 promem_orchestrator.py check-key")


def check_key() -> None:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        print("OPENAI_API_KEY: NOT SET")
        print("  → run: python3 promem_orchestrator.py set-key")
    else:
        print(f"OPENAI_API_KEY: set (length={len(key)}, starts={key[:6]}…)")
    print(f"OPENAI_BASE_URL: {os.environ.get('OPENAI_BASE_URL', '(default)')}")
    print(f"PROMEM_DB_URL:   {'set' if os.environ.get('PROMEM_DB_URL') else 'NOT SET'}")
    print(f"PROMEM_USER_ID:  {os.environ.get('PROMEM_USER_ID') or 'NOT SET'}")


def whoami() -> None:
    try:
        uid = db.user_id()
        print(f"PROMEM_USER_ID = {uid}")
    except RuntimeError as e:
        print(str(e))


# ──────────────────────────────────────────────────────────────────────────────
# Per-user orchestrator_state helpers
# ──────────────────────────────────────────────────────────────────────────────
def get_state() -> dict:
    user_id = db.user_id()
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM orchestrator_state WHERE user_id=%s",
            (user_id,),
        ).fetchone()
        return dict(row) if row else {}


def update_state(**fields) -> None:
    if not fields:
        return
    user_id = db.user_id()
    cols = ", ".join(f"{k} = %s" for k in fields)
    with db.conn() as c:
        # Ensure the row exists, then update.
        c.execute(
            "INSERT INTO orchestrator_state (user_id) VALUES (%s) "
            "ON CONFLICT (user_id) DO NOTHING",
            (user_id,),
        )
        c.execute(
            f"UPDATE orchestrator_state SET {cols} WHERE user_id = %s",
            (*fields.values(), user_id),
        )


# ──────────────────────────────────────────────────────────────────────────────
# Trigger logic — 14:00 / 18:00 + next-morning fallback
# ──────────────────────────────────────────────────────────────────────────────
def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def should_run(now: datetime | None = None) -> tuple[bool, str]:
    now = now or datetime.now()
    state = get_state()
    last = _parse(state.get("last_sync_at"))

    today_2pm = now.replace(hour=14, minute=0, second=0, microsecond=0)
    today_6pm = now.replace(hour=18, minute=0, second=0, microsecond=0)
    yest_6pm = today_6pm - timedelta(days=1)

    if now >= today_2pm and (last is None or last < today_2pm):
        return True, "14:00 due"
    if now >= today_6pm and (last is None or last < today_6pm):
        return True, "18:00 due"
    if now < today_2pm and (last is None or last < yest_6pm):
        return True, "morning catchup"
    return False, "nothing due"


def _next_due(now: datetime) -> datetime:
    today_2pm = now.replace(hour=14, minute=0, second=0, microsecond=0)
    today_6pm = now.replace(hour=18, minute=0, second=0, microsecond=0)
    if now < today_2pm:
        return today_2pm
    if now < today_6pm:
        return today_6pm
    return today_2pm + timedelta(days=1)


# ──────────────────────────────────────────────────────────────────────────────
# Phases
# ──────────────────────────────────────────────────────────────────────────────
def phase_sync() -> dict:
    from promem_pipeline.sync import sync_work_pages
    r = sync_work_pages()
    if r.get("skipped"):
        print(f"  · phase_sync: skipped — {r.get('reason', '')}")
    else:
        print(f"  · phase_sync: seen={r['n_seen']} inserted={r['n_inserted']} since={r['cutoff']}")
    return r


def phase_classify() -> dict:
    from promem_pipeline.classify import classify_all
    r = classify_all()
    if r.get("skipped"):
        print(f"  · phase_classify: skipped — {r.get('reason', '')}")
    else:
        print(f"  · phase_classify: total={r['n_total']} classified={r['n_classified']} "
              f"pending={r['n_pending']} failed={r['n_failed']} ({r['duration_sec']}s)")
    return r


def phase_filter() -> dict:
    from promem_pipeline.filter import filter_pages
    r = filter_pages()
    print(f"  · phase_filter: keep={r['n_keep']} archive={r['n_archive']} "
          f"unfiled={r['n_unfiled']} unclassified={r['n_unclassified']}")
    return r


def phase_match() -> dict:
    from promem_pipeline.matcher import match_all
    r = match_all()
    if r.get("skipped"):
        print(f"  · phase_match: skipped — {r.get('reason', '')}")
    else:
        print(f"  · phase_match: pairs={r['n_pairs_scored']} matched={r['n_matched']} "
              f"upserted={r['n_upserted']}")
    return r


def phase_synthesis() -> dict:
    from promem_pipeline.synthesis import synthesize_all
    r = synthesize_all()
    if r.get("skipped"):
        print(f"  · phase_synthesis: skipped — {r.get('reason', '')}")
    else:
        print(f"  · phase_synthesis: SC={r['n_sc_ok']}/{r['n_sc_attempted']} "
              f"deliv={r['n_deliv_ok']}/{r['n_deliv_attempted']} "
              f"failed={r['n_sc_failed']+r['n_deliv_failed']} ({r['duration_sec']}s)")
    return r


PHASES = [
    ("sync",      phase_sync,      "last_sync_at"),
    ("classify",  phase_classify,  "last_classify_at"),
    ("filter",    phase_filter,    None),
    ("match",     phase_match,     "last_match_at"),
    ("synthesis", phase_synthesis, "last_synthesis_at"),
]


# ──────────────────────────────────────────────────────────────────────────────
# Entry points
# ──────────────────────────────────────────────────────────────────────────────
def run_full(reason: str = "manual") -> dict:
    now_iso = datetime.now().isoformat(timespec="seconds")
    print(f"orchestrator: run_full ({reason}) at {now_iso} for user {db.user_id()}")
    results = []
    try:
        for name, fn, state_col in PHASES:
            r = fn()
            results.append(r)
            if state_col and r.get("ok"):
                update_state(**{state_col: now_iso})
        update_state(next_due=_next_due(datetime.now()).isoformat(timespec="seconds"),
                     last_error="")
        return {"ok": True, "reason": reason, "phases": results}
    except Exception as e:
        update_state(last_error=str(e))
        raise


def tick() -> dict:
    ok, reason = should_run()
    if not ok:
        print(f"orchestrator: tick — {reason}; nothing to do")
        return {"ok": True, "ran": False, "reason": reason}
    return {**run_full(reason), "ran": True}


def reset() -> None:
    user_id = db.user_id()
    with db.conn() as c:
        c.execute(
            "UPDATE orchestrator_state SET last_sync_at=NULL, "
            "last_classify_at=NULL, last_match_at=NULL, "
            "last_synthesis_at=NULL, next_due=NULL, last_error=NULL "
            "WHERE user_id=%s",
            (user_id,),
        )
    print("orchestrator: state reset")


def status() -> dict:
    state = get_state()
    ok, reason = should_run()
    out = {**state, "should_run_now": ok, "reason": reason}
    print(json.dumps(out, indent=2, default=str))
    return out


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "status":   status()
    elif cmd == "tick":   tick()
    elif cmd == "run":    run_full(reason="manual")
    elif cmd == "reset":  reset()
    elif cmd == "set-key": set_key()
    elif cmd == "check-key": check_key()
    elif cmd == "whoami":  whoami()
    else:
        print(f"Unknown command: {cmd}\n"
              "Usage: status | tick | run | reset | set-key | check-key | whoami")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
