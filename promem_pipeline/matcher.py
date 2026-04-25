"""Stage 4 — match classified work_pages to deliverables.

5-layer scoring (from project_tracker_sim.py, ported as-is):
  L1 keyword (word-boundary)        +0.40 + 0.05 per extra hit (cap 3)
  L2 ctx_label is in deliv.ctx_hints +0.35
  L3 title-token overlap            +0.05 per overlap (cap 4)
  L4 date band                      gate (off in v1)
  L5 manual pin/unpin               hard set, written by UI

Threshold to write a match: composite >= 0.35.

Idempotent: UNIQUE(deliverable_id, page_id) → INSERT OR REPLACE refreshes the
score on each run. Manual pins (source='pin') are skipped to preserve overrides.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

THRESHOLD = 0.35
STOP_TOKENS = {
    "rohit", "claude", "google", "chrome", "your", "with", "from", "this",
    "build", "version", "system", "their", "have", "that", "what", "when",
    "where", "they", "them", "into", "over",
}


# ──────────────────────────────────────────────────────────────────────────────
# Scoring (mirrors project_tracker_sim.score_page_for_deliverable)
# ──────────────────────────────────────────────────────────────────────────────
def _score(page: dict, deliv: dict) -> tuple[float, list[str]]:
    text = f"{page.get('title') or ''}  {page.get('summary') or ''}".lower()
    ctx = (page.get("ctx_label") or "").strip()
    reasons: list[str] = []
    score = 0.0

    # L1 keyword (word-boundary for single tokens; substring for multi-word)
    kw_hits: list[str] = []
    for kw in deliv["keywords"]:
        kw_lc = kw.lower()
        if " " in kw_lc:
            if kw_lc in text:
                kw_hits.append(kw)
        else:
            if re.search(r"\b" + re.escape(kw_lc) + r"\b", text):
                kw_hits.append(kw)
    if kw_hits:
        score += 0.40 + 0.05 * min(len(kw_hits), 3)
        reasons.append(f"keywords: {', '.join(kw_hits[:3])}")

    # L2 ctx inheritance
    if ctx and ctx in deliv["ctx_hints"]:
        score += 0.35
        reasons.append(f"ctx: {ctx}")

    # L3 title-token overlap
    deliv_tokens = set(re.findall(r"[a-z0-9]{4,}", (deliv["title"] or "").lower()))
    page_tokens = set(re.findall(r"[a-z0-9]{4,}", text))
    overlap = (deliv_tokens & page_tokens) - STOP_TOKENS
    if overlap:
        score += 0.05 * min(len(overlap), 4)
        reasons.append(f"title: {', '.join(sorted(overlap)[:3])}")

    return score, reasons


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
def _load_deliverables(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, project_id, title, description, keywords, ctx_hints "
        "FROM deliverables WHERE status != 'archived'"
    ).fetchall()
    out = []
    for r in rows:
        try:
            kws = json.loads(r[4] or "[]")
        except Exception:
            kws = []
        try:
            ctxs = json.loads(r[5] or "[]")
        except Exception:
            ctxs = []
        out.append({
            "id": r[0], "project_id": r[1], "title": r[2] or "",
            "description": r[3] or "", "keywords": kws, "ctx_hints": ctxs,
        })
    return out


def _load_pages(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, title, summary, sc_label, ctx_label, date_local "
        "FROM work_pages "
        "WHERE sc_label != '' AND COALESCE(is_unfiled, 0) = 0"
    ).fetchall()
    return [
        {"id": r[0], "title": r[1], "summary": r[2], "sc_label": r[3],
         "ctx_label": r[4], "date_local": r[5]}
        for r in rows
    ]


def match_all(prome_db: str | Path) -> dict:
    prome_db = Path(prome_db)
    conn = sqlite3.connect(prome_db, timeout=60.0)
    now = datetime.now().isoformat(timespec="seconds")
    deliverables = _load_deliverables(conn)
    pages = _load_pages(conn)

    if not deliverables or not pages:
        conn.close()
        return {
            "ok": True, "phase": "match", "skipped": True,
            "reason": "no deliverables" if not deliverables else "no classified pages",
            "n_pairs_scored": 0, "n_matched": 0, "n_upserted": 0,
        }

    # Pull existing manual pins so we don't overwrite them
    pinned_keys = set()
    for r in conn.execute(
        "SELECT deliverable_id, page_id FROM deliverable_match WHERE source='pin'"
    ).fetchall():
        pinned_keys.add((r[0], r[1]))

    n_pairs = 0
    n_matched = 0
    n_upserted = 0

    for p in pages:
        for d in deliverables:
            n_pairs += 1
            score, reasons = _score(p, d)
            if score < THRESHOLD:
                continue
            n_matched += 1
            key = (d["id"], p["id"])
            if key in pinned_keys:
                continue  # respect manual pin
            # INSERT OR REPLACE refreshes score on each run for source='auto'
            conn.execute("""
                INSERT INTO deliverable_match
                  (deliverable_id, page_id, score, reasons, source, matched_at)
                VALUES (?, ?, ?, ?, 'auto', ?)
                ON CONFLICT(deliverable_id, page_id) DO UPDATE SET
                  score=excluded.score,
                  reasons=excluded.reasons,
                  matched_at=excluded.matched_at
                WHERE deliverable_match.source='auto'
            """, (d["id"], p["id"], round(score, 4),
                  json.dumps(reasons), now))
            n_upserted += 1
    conn.commit()
    conn.close()

    return {
        "ok": True, "phase": "match", "skipped": False,
        "n_deliverables": len(deliverables),
        "n_pages": len(pages),
        "n_pairs_scored": n_pairs,
        "n_matched": n_matched,
        "n_upserted": n_upserted,
    }
