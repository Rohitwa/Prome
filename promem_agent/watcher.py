"""Phase 4b.2 — tracker.db watcher.

Reads new rows from the local tracker.db.context_1 table and produces dicts
shaped to match the server's TrackerSegmentIn payload (so the uploader can
ship them without remapping).

State is held in a small JSON sidecar file (agent_state.json). The watcher
records the highest timestamp_start it has seen uploaded; on next poll, it
fetches WHERE timestamp_start > that. The server's ON CONFLICT (user_id, id)
DO NOTHING gives us idempotency-on-replay if the state file ever lags.

CLI:
    python3 -m promem_agent.watcher status        # print state file as JSON
    python3 -m promem_agent.watcher peek [N]      # print first N queued segments (default 5)
    python3 -m promem_agent.watcher reset         # delete state file (next run backfills)
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Reuse the .env loader from oauth.py — both modules share the same env file.
from promem_agent.oauth import _load_dotenv  # noqa: F401  (side-effect on import)


SCHEMA_VERSION = 1
DEFAULT_BACKFILL_DAYS = 30
SQL_FETCH = """
    SELECT id, target_segment_id, timestamp_start, timestamp_end,
           target_segment_length_secs, short_title, window_name,
           detailed_summary, supercontext, context
    FROM context_1
    WHERE timestamp_start > ?
    ORDER BY timestamp_start
    LIMIT ?
"""
COLUMNS = [
    "id", "target_segment_id", "timestamp_start", "timestamp_end",
    "target_segment_length_secs", "short_title", "window_name",
    "detailed_summary", "supercontext", "context",
]


# ── Platform-aware default paths ─────────────────────────────────────────
def _platform_default_dir() -> Path:
    """Where the agent stores tracker.db and state by default.

    - Windows: %LOCALAPPDATA%\\ProMem\\         (per-machine, per-user app data)
    - macOS:   <repo>/data/                     (dev convention — keeps tracker.db
                                                 and state next to the rest of Prome)
    - Linux:   ~/.local/share/ProMem/           (XDG-ish)
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "ProMem"
    if sys.platform == "darwin":
        return Path(__file__).resolve().parent.parent / "data"
    return Path.home() / ".local" / "share" / "ProMem"


def default_tracker_db() -> Path:
    env = os.environ.get("PROMEM_TRACKER_DB", "").strip()
    if env:
        return Path(env)
    return _platform_default_dir() / "tracker.db"


def default_state_path() -> Path:
    env_dir = os.environ.get("PROMEM_AGENT_STATE_DIR", "").strip()
    if env_dir:
        return Path(env_dir) / "agent_state.json"
    return _platform_default_dir() / "agent_state.json"


def _backfill_days() -> int:
    raw = os.environ.get("PROMEM_AGENT_BACKFILL_DAYS", "").strip()
    if not raw:
        return DEFAULT_BACKFILL_DAYS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_BACKFILL_DAYS


# ── Atomic JSON state I/O ────────────────────────────────────────────────
def _write_state_atomic(state_path: Path, state: dict) -> None:
    """Write state JSON via tempfile + os.replace — survives mid-write kills."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=state_path.parent, prefix=".agent_state.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_name, state_path)
    except Exception:
        # Best-effort cleanup of the tempfile if replace didn't happen.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_state(state_path: Path) -> dict:
    """Return state dict, or {} if missing / corrupt (with a stderr warning)."""
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(
            f"warning: state file at {state_path} is unreadable ({e}); "
            "treating as empty (will backfill).", file=sys.stderr,
        )
        return {}


# ── Watcher ──────────────────────────────────────────────────────────────
class TrackerWatcher:
    def __init__(
        self,
        tracker_db: Path | None = None,
        state_path: Path | None = None,
        backfill_days: int | None = None,
    ) -> None:
        self.tracker_db = Path(tracker_db) if tracker_db else default_tracker_db()
        self.state_path = Path(state_path) if state_path else default_state_path()
        self.backfill_days = backfill_days if backfill_days is not None else _backfill_days()

    # ── State ────────────────────────────────────────────────────────────
    def get_state(self) -> dict:
        return _read_state(self.state_path)

    def reset(self) -> None:
        try:
            self.state_path.unlink()
        except FileNotFoundError:
            pass

    def _cutoff(self) -> str:
        """Return the cutoff timestamp_start for the next fetch.
        Uses state.last_uploaded_timestamp_start if present, else now() - backfill_days.

        Format note: tracker.db writes timestamps with a space separator
        ('2026-04-02 01:34:44') so we use isoformat(sep=' ') for the initial
        cutoff. With 'T' separator, same-day segments would be lexicographically
        less than the cutoff (' ' < 'T') and silently skipped."""
        state = self.get_state()
        cutoff = state.get("last_uploaded_timestamp_start")
        if cutoff:
            return cutoff
        return (datetime.now() - timedelta(days=self.backfill_days)).isoformat(sep=" ")

    # ── Read ─────────────────────────────────────────────────────────────
    def fetch_new_segments(self, limit: int = 1000) -> list[dict]:
        """Read context_1 rows newer than the recorded cutoff. Returns dicts
        keyed to match TrackerSegmentIn (server-side Pydantic model)."""
        if not self.tracker_db.exists():
            print(
                f"warning: tracker.db not found at {self.tracker_db}; "
                "returning no segments.", file=sys.stderr,
            )
            return []
        cutoff = self._cutoff()
        with sqlite3.connect(
            f"file:{self.tracker_db}?mode=ro", uri=True, timeout=30.0,
        ) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(SQL_FETCH, (cutoff, int(limit))).fetchall()
        return [{col: row[col] for col in COLUMNS} for row in rows]

    # ── Write state ──────────────────────────────────────────────────────
    def mark_uploaded(self, segments: list[dict]) -> None:
        """Advance state to the max(timestamp_start) of the given segments and
        persist atomically. No-op for empty input."""
        if not segments:
            return
        # max() over ISO strings works because they sort lexicographically.
        latest = max(segments, key=lambda s: s.get("timestamp_start") or "")
        state = self.get_state()
        state.update({
            "schema_version": SCHEMA_VERSION,
            "last_uploaded_timestamp_start": latest.get("timestamp_start"),
            "last_uploaded_id": latest.get("id"),
            "last_run_at": datetime.now(timezone.utc).isoformat(),
        })
        _write_state_atomic(self.state_path, state)


# ── CLI ──────────────────────────────────────────────────────────────────
def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    w = TrackerWatcher()

    if cmd == "status":
        state = w.get_state()
        out = {
            "tracker_db":   str(w.tracker_db),
            "tracker_db_exists": w.tracker_db.exists(),
            "state_path":   str(w.state_path),
            "backfill_days": w.backfill_days,
            "cutoff_for_next_fetch": w._cutoff(),
            "state": state or "<empty>",
        }
        print(json.dumps(out, indent=2, default=str))
        return 0

    if cmd == "peek":
        n = int(argv[2]) if len(argv) > 2 else 5
        segs = w.fetch_new_segments(limit=n)
        print(f"# {len(segs)} segment(s) would be uploaded next (showing up to {n}):", file=sys.stderr)
        print(json.dumps(segs, indent=2, default=str))
        return 0

    if cmd == "reset":
        existed = w.state_path.exists()
        w.reset()
        print(f"State file {'deleted' if existed else 'was already absent'}: {w.state_path}")
        return 0

    print(f"Unknown command: {cmd}. Try status | peek [N] | reset", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
