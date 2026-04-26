"""Stage 4 — match classified work_pages to deliverables.

5-layer scoring (from project_tracker_sim.py, ported as-is):
  L1 keyword (word-boundary)        +0.40 + 0.05 per extra hit (cap 3)
  L2 ctx_label is in deliv.ctx_hints +0.35
  L3 title-token overlap            +0.05 per overlap (cap 4)
  L4 date band                      gate (off in v1)
  L5 manual pin/unpin               hard set, written by UI

Threshold to write a match: composite >= 0.35.

Idempotent: UNIQUE(user_id, deliverable_id, page_id) → ON CONFLICT
DO UPDATE refreshes the score on each run. Manual pins (source='pin')
are skipped to preserve overrides.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db

THRESHOLD = 0.35
STOP_TOKENS = {
    "rohit", "claude", "google", "chrome", "your", "with", "from", "this",
    "build", "version", "system", "their", "have", "that", "what", "when",
    "where", "they", "them", "into", "over",
}


def _score(page: dict, deliv: dict) -> tuple[float, list[str]]:
    text = f"{page.get('title') or ''}  {page.get('summary') or ''}".lower()
    ctx = (page.get("ctx_label") or "").strip()
    reasons: list[str] = []
    score = 0.0

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

    if ctx and ctx in deliv["ctx_hints"]:
        score += 0.35
        reasons.append(f"ctx: {ctx}")

    deliv_tokens = set(re.findall(r"[a-z0-9]{4,}", (deliv["title"] or "").lower()))
    page_tokens = set(re.findall(r"[a-z0-9]{4,}", text))
    overlap = (deliv_tokens & page_tokens) - STOP_TOKENS
    if overlap:
        score += 0.05 * min(len(overlap), 4)
        reasons.append(f"title: {', '.join(sorted(overlap)[:3])}")

    return score, reasons


def _load_deliverables(c, user_id: str) -> list[dict]:
    rows = c.execute(
        "SELECT id, project_id, title, description, keywords, ctx_hints "
        "FROM deliverables WHERE user_id=%s AND status != 'archived'",
        (user_id,),
    ).fetchall()
    out = []
    for r in rows:
        try:
            kws = json.loads(r["keywords"] or "[]")
        except Exception:
            kws = []
        try:
            ctxs = json.loads(r["ctx_hints"] or "[]")
        except Exception:
            ctxs = []
        out.append({
            "id": r["id"], "project_id": r["project_id"],
            "title": r["title"] or "", "description": r["description"] or "",
            "keywords": kws, "ctx_hints": ctxs,
        })
    return out


def _load_pages(c, user_id: str) -> list[dict]:
    rows = c.execute(
        "SELECT id, title, summary, sc_label, ctx_label, date_local "
        "FROM work_pages "
        "WHERE user_id=%s AND sc_label != '' AND COALESCE(is_unfiled, 0) = 0",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def match_all() -> dict:
    user_id = db.user_id()
    now = datetime.now().isoformat(timespec="seconds")

    with db.conn() as c:
        deliverables = _load_deliverables(c, user_id)
        pages = _load_pages(c, user_id)

        if not deliverables or not pages:
            return {
                "ok": True, "phase": "match", "skipped": True,
                "reason": "no deliverables" if not deliverables else "no classified pages",
                "n_pairs_scored": 0, "n_matched": 0, "n_upserted": 0,
            }

        pinned_keys = set()
        for r in c.execute(
            "SELECT deliverable_id, page_id FROM deliverable_match "
            "WHERE user_id=%s AND source='pin'",
            (user_id,),
        ).fetchall():
            pinned_keys.add((r["deliverable_id"], r["page_id"]))

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
                    continue
                # Refresh score on each run for source='auto'; preserve pins.
                c.execute("""
                    INSERT INTO deliverable_match
                      (user_id, deliverable_id, page_id, score, reasons, source, matched_at)
                    VALUES (%s, %s, %s, %s, %s, 'auto', %s)
                    ON CONFLICT (user_id, deliverable_id, page_id) DO UPDATE SET
                      score=EXCLUDED.score,
                      reasons=EXCLUDED.reasons,
                      matched_at=EXCLUDED.matched_at
                    WHERE deliverable_match.source='auto'
                """, (user_id, d["id"], p["id"], round(score, 4),
                      json.dumps(reasons), now))
                n_upserted += 1

    return {
        "ok": True, "phase": "match", "skipped": False,
        "n_deliverables": len(deliverables),
        "n_pages": len(pages),
        "n_pairs_scored": n_pairs,
        "n_matched": n_matched,
        "n_upserted": n_upserted,
    }
