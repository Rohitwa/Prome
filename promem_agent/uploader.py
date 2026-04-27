"""Phase 4b.3 — uploader.

Glue between watcher (segments) and oauth (token). Chunks segments into
batches of <=MAX_BATCH, POSTs each batch to /api/upload-segments with
Bearer auth, handles 401 (re-login once), 413 (defensive — should never
fire), 429/5xx (exponential backoff). All-or-nothing per upload session:
mark_uploaded is the caller's responsibility, called only after every
batch succeeds.

CLI:
    python3 -m promem_agent.uploader test                # POST one fake segment
    python3 -m promem_agent.uploader push [LIMIT]        # fetch+upload up to LIMIT (default 10)
    python3 -m promem_agent.uploader push-all            # full sync until queue empty

Env:
    PROMEM_BASE_URL   override the upload host (default: https://promem.fly.dev)
"""

from __future__ import annotations

import os
import sys
import time
from typing import NamedTuple

import httpx

from promem_agent import oauth


DEFAULT_BASE_URL    = "https://promem.fly.dev"
UPLOAD_PATH         = "/api/upload-segments"
UPLOAD_FRAMES_PATH  = "/api/upload-frames"
MAX_BATCH           = 1000     # mirrors server's UPLOAD_SEGMENTS_MAX
MAX_FRAMES_BATCH    = 5000     # mirrors server's UPLOAD_FRAMES_MAX (smaller payloads)
RETRY_MAX           = 3        # for 5xx / 429 / network errors
RETRY_BACKOFF       = 2.0      # seconds, multiplied by 2**attempt
HTTP_TIMEOUT        = 30.0     # per-request


class UploadError(Exception):
    """Unrecoverable upload failure (auth, persistent 5xx, bad request)."""


class UploadResult(NamedTuple):
    n_received: int
    n_inserted: int
    n_batches: int
    duration_secs: float


def _noninteractive_mode() -> bool:
    return os.environ.get("PROMEM_AGENT_NONINTERACTIVE", "").strip().lower() in ("1", "true", "yes")


def _base_url() -> str:
    return os.environ.get("PROMEM_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _post_one_batch(url: str, batch: list[dict], payload_key: str = "segments") -> dict:
    """POST a single batch with retry/auth handling. Returns the parsed
    response body on success. Raises UploadError on unrecoverable failure.

    payload_key controls the JSON wrapper key — "segments" for /api/upload-segments,
    "frames" for /api/upload-frames."""
    token = oauth.get_access_token()
    relogin_used = False
    attempt = 0
    while attempt < RETRY_MAX:
        try:
            r = httpx.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={payload_key: batch},
                timeout=HTTP_TIMEOUT,
            )
        except httpx.RequestError as e:
            if attempt + 1 >= RETRY_MAX:
                raise UploadError(
                    f"Network error after {RETRY_MAX} attempts: {e}"
                ) from e
            time.sleep(RETRY_BACKOFF * (2 ** attempt))
            attempt += 1
            continue

        if r.status_code == 200:
            return r.json()

        if r.status_code == 401 and not relogin_used:
            # Refresh path silently failed (token rotated/revoked). Force OAuth.
            # In non-interactive scheduled mode we never open browser OAuth
            # here; surface a clear recoverable error instead.
            if _noninteractive_mode():
                raise UploadError(
                    "401 from server and non-interactive mode is enabled; "
                    "run `promem_agent init` interactively to refresh auth."
                )
            print("warning: 401 from server, re-running OAuth login flow...", file=sys.stderr)
            token = oauth.first_run_login()
            relogin_used = True
            continue  # do NOT increment attempt — fresh token, give it one shot

        if r.status_code == 401:
            raise UploadError(
                f"Auth still failing after re-login attempt: {r.text[:300]}"
            )

        if r.status_code == 413:
            raise UploadError(
                f"Server rejected batch as too large (413) with batch_size={len(batch)}, "
                f"client MAX_BATCH={MAX_BATCH}. This is a client bug — server cap "
                "may have been lowered."
            )

        if r.status_code == 429 or r.status_code >= 500:
            if attempt + 1 >= RETRY_MAX:
                raise UploadError(
                    f"HTTP {r.status_code} after {RETRY_MAX} attempts: {r.text[:300]}"
                )
            time.sleep(RETRY_BACKOFF * (2 ** attempt))
            attempt += 1
            continue

        # 4xx other than 401/413/429 — retrying won't help.
        raise UploadError(f"HTTP {r.status_code}: {r.text[:300]}")

    raise UploadError(f"Exhausted {RETRY_MAX} retries on batch of {len(batch)} segments")


def upload_segments(segments: list[dict], base_url: str | None = None) -> UploadResult:
    """Chunk segments into <=MAX_BATCH batches and POST each. Returns
    aggregate counts. Raises UploadError if ANY batch fails (the caller
    decides what to do with already-uploaded batches; server idempotency
    via ON CONFLICT means re-uploads are cheap)."""
    if not segments:
        return UploadResult(0, 0, 0, 0.0)
    url = f"{(base_url or _base_url()).rstrip('/')}{UPLOAD_PATH}"
    started = time.monotonic()
    total_received = 0
    total_inserted = 0
    n_batches = 0
    for batch in _chunks(segments, MAX_BATCH):
        body = _post_one_batch(url, batch, payload_key="segments")
        total_received += int(body.get("n_received", 0))
        total_inserted += int(body.get("n_inserted", 0))
        n_batches += 1
    duration = time.monotonic() - started
    return UploadResult(total_received, total_inserted, n_batches, duration)


def upload_frames(frames: list[dict], base_url: str | None = None) -> UploadResult:
    """Chunk frames into <=MAX_FRAMES_BATCH and POST each to /api/upload-frames.
    Same retry/auth/idempotency model as upload_segments."""
    if not frames:
        return UploadResult(0, 0, 0, 0.0)
    url = f"{(base_url or _base_url()).rstrip('/')}{UPLOAD_FRAMES_PATH}"
    started = time.monotonic()
    total_received = 0
    total_inserted = 0
    n_batches = 0
    for batch in _chunks(frames, MAX_FRAMES_BATCH):
        body = _post_one_batch(url, batch, payload_key="frames")
        total_received += int(body.get("n_received", 0))
        total_inserted += int(body.get("n_inserted", 0))
        n_batches += 1
    duration = time.monotonic() - started
    return UploadResult(total_received, total_inserted, n_batches, duration)


# ── CLI ──────────────────────────────────────────────────────────────────
SMOKE_SEGMENT = {
    "id": "agent-smoke-001",
    "target_segment_id": "AGENT-SMOKE",
    "timestamp_start": "2026-04-26T08:00:00",
    "timestamp_end": "2026-04-26T08:01:00",
    "target_segment_length_secs": 60,
    "short_title": "agent uploader smoke test",
    "window_name": "promem_agent.uploader",
    "detailed_summary": "synthetic segment to verify uploader CLI end-to-end",
    "supercontext": None,
    "context": None,
    # Phase 4d optional fields — sent as placeholders so the smoke also
    # exercises the extended INSERT path on the server.
    "worker": "human",
    "is_productive": 1,
    "human_frame_count": 1,
    "ai_frame_count": 0,
    "platform": "smoke-test",
    "medium": None,
    "full_text": None,
    "anchor": None,
}

SMOKE_FRAME = {
    "id": "agent-smoke-frame-001",
    "target_segment_id": "AGENT-SMOKE",
    "target_frame_number": 1,
    "frame_timestamp": "2026-04-26T08:00:30",
    "raw_text": "smoke-test frame raw text",
    "detailed_summary": "smoke-test frame summary",
    "worker_type": "human",
    "has_keyboard_activity": True,
    "has_mouse_activity": False,
}


def _print_result(label: str, result: UploadResult) -> None:
    print(
        f"{label}: received={result.n_received}, inserted={result.n_inserted}, "
        f"batches={result.n_batches}, duration={result.duration_secs:.2f}s"
    )


def _cli_test() -> int:
    try:
        seg_result = upload_segments([SMOKE_SEGMENT])
    except UploadError as e:
        print(f"UploadError (segments): {e}", file=sys.stderr)
        return 1
    _print_result("smoke segments", seg_result)
    try:
        frame_result = upload_frames([SMOKE_FRAME])
    except UploadError as e:
        print(f"UploadError (frames): {e}", file=sys.stderr)
        return 1
    _print_result("smoke frames  ", frame_result)
    print(
        "(stable ids 'agent-smoke-001' / 'agent-smoke-frame-001' — re-run to "
        "verify idempotency: n_inserted drops to 0. To clean up:\n"
        "  DELETE FROM tracker_segments WHERE id = 'agent-smoke-001';\n"
        "  DELETE FROM tracker_frames   WHERE id = 'agent-smoke-frame-001';\n"
        " in Supabase SQL Editor.)"
    )
    return 0


def _cli_push(limit: int) -> int:
    from promem_agent.watcher import TrackerWatcher
    w = TrackerWatcher()
    segs = w.fetch_new_segments(limit=limit)
    if not segs:
        print("Nothing to upload — watcher returned 0 segments.")
        return 0
    print(f"Uploading {len(segs)} segment(s)...")
    try:
        seg_result = upload_segments(segs)
    except UploadError as e:
        print(f"UploadError (state NOT advanced): {e}", file=sys.stderr)
        return 1
    _print_result("push segs  ", seg_result)

    # Phase 4d: ship frames belonging to those segments before advancing state.
    seg_ids = [s["target_segment_id"] for s in segs if s.get("target_segment_id")]
    frames = w.fetch_frames_for_segments(seg_ids) if seg_ids else []
    if frames:
        print(f"Uploading {len(frames)} frame(s) for those segments...")
        try:
            frame_result = upload_frames(frames)
        except UploadError as e:
            print(f"UploadError on frames (state NOT advanced): {e}", file=sys.stderr)
            return 1
        _print_result("push frames", frame_result)
    else:
        print("(no frames found in context_2 for those segments)")

    w.mark_uploaded(segs)
    state = w.get_state()
    print(f"State advanced → last_uploaded_timestamp_start={state.get('last_uploaded_timestamp_start')}")
    return 0


def _cli_push_all() -> int:
    from promem_agent.watcher import TrackerWatcher
    w = TrackerWatcher()
    total_received = 0
    total_inserted = 0
    iterations = 0
    while True:
        segs = w.fetch_new_segments(limit=MAX_BATCH)
        if not segs:
            break
        iterations += 1
        print(f"  iter {iterations}: uploading {len(segs)} segment(s)...")
        try:
            result = upload_segments(segs)
        except UploadError as e:
            print(f"UploadError on iter {iterations} (state NOT advanced): {e}", file=sys.stderr)
            return 1
        w.mark_uploaded(segs)
        total_received += result.n_received
        total_inserted += result.n_inserted
    if iterations == 0:
        print("Nothing to upload — watcher queue empty.")
    else:
        print(f"Done: {iterations} iteration(s), received={total_received}, inserted={total_inserted}")
    return 0


def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else None
    if cmd == "test":
        return _cli_test()
    if cmd == "push":
        limit = int(argv[2]) if len(argv) > 2 else 10
        return _cli_push(limit)
    if cmd == "push-all":
        return _cli_push_all()
    print(f"Unknown command: {cmd!r}. Try test | push [LIMIT] | push-all", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
