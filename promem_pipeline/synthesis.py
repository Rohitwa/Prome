"""Stage 5 — synthesize Karpathy-LLM-Wiki style prose for each kept SC and
each deliverable with matches. Caches result as JSON in *_wiki_cache tables.

Two prompts:
  - synth_sc(label, pages, ctxs)               → SC wiki page
  - synth_deliverable(deliv, matched_pages)    → deliverable wiki page

Cache invalidation: regenerate if source_page_count drifts ≥10% OR cache is
empty OR cache is older than 24h.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import httpx

MODEL = os.environ.get("PROME_SYNTHESIS_MODEL", "gpt-4o-mini")
TIMEOUT = 90.0
RETRIES = 2
CACHE_TTL_HOURS = 24
DRIFT_PCT = 0.10
PAGES_FOR_SC = 60        # cap pages fed into SC prompt to stay under context
PAGES_FOR_DELIV = 40
CONCURRENCY = 4


# ──────────────────────────────────────────────────────────────────────────────
# OpenAI helper
# ──────────────────────────────────────────────────────────────────────────────
def _llm_json(prompt: str, max_tokens: int = 1800) -> dict:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set")
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    last_err = ""
    for attempt in range(RETRIES + 1):
        try:
            r = httpx.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": MODEL, "max_tokens": max_tokens, "temperature": 0.4,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=TIMEOUT,
            )
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}: {r.text[:160]}"
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(2.0 * (attempt + 1))
                    continue
                return {}
            return json.loads(r.json()["choices"][0]["message"]["content"])
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            last_err = str(e)
            time.sleep(1.5 * (attempt + 1))
    print(f"  ⚠ synth LLM failed: {last_err[:120]}")
    return {}


# ──────────────────────────────────────────────────────────────────────────────
# Prompts
# ──────────────────────────────────────────────────────────────────────────────
def _build_sc_prompt(label: str, pages: list[dict], ctxs: list[tuple[str, int]]) -> str:
    page_lines = "\n".join(
        f"- [{p['date_local']}] ({p.get('ctx_label') or '—'}) {(p.get('title') or '')[:120]}"
        for p in pages[:PAGES_FOR_SC]
    )
    ctx_lines = "\n".join(f"- {label} ({n} pages)" for label, n in ctxs[:20])
    return f"""You are writing a Karpathy-LLM-Wiki style page synthesizing recent work in a Super Context.

Super Context: **{label}**

Distinct sub-contexts seen and their page counts:
{ctx_lines}

Recent activity (most-recent first, max {PAGES_FOR_SC} pages):
{page_lines}

Write a wiki page. Use the actual page titles + dates above — do not invent details. If a sub-context only has 1-2 pages, reflect that honestly. Cite specific dates (e.g., Apr 24) and entities (PR numbers, project names) when they appear in the activity.

Output strict JSON:
{{
  "tagline": "<one short italic-style sentence summarizing this SC's recent thrust>",
  "sections": [
    {{"title": "<5-8 word section title>", "body": "<markdown body, 3-7 sentences. Use **bold** for key facts and `code` for filenames or commands.>"}}
  ]
}}

Aim for 4-6 sections. Group related sub-contexts together where it reads better.
No prose outside the JSON."""


def _build_deliv_prompt(deliv: dict, pages: list[dict], stats: dict) -> str:
    page_lines = "\n".join(
        f"- [{p['date_local']}] (score {p['score']:.2f}) {(p.get('title') or '')[:120]}"
        for p in pages[:PAGES_FOR_DELIV]
    )
    return f"""You are writing a Karpathy-LLM-Wiki style page about a single project deliverable, grounded in matched activity.

Project: {deliv['project_name']}
Deliverable: **{deliv['title']}**
Description: {deliv.get('description','')}
Keywords seeded: {', '.join(deliv.get('keywords', [])) or '(none)'}

Matched activity in last 30d (sorted by relevance score, max {PAGES_FOR_DELIV}):
{page_lines}

Stats: {stats['n_pages']} matched pages, {stats['minutes']} min tracked, {stats['days_active']} days active, last activity {stats.get('last') or '—'}.

Be honest about gaps:
- If pages are mostly noise / weak match, say so.
- If the deliverable is "not started" (few or no pages), say that plainly instead of confabulating.
- If pages cluster around 1-2 themes, name them.

Output strict JSON:
{{
  "tagline": "<one short italic sentence — current state of this deliverable>",
  "sections": [
    {{"title": "<5-8 word title>", "body": "<3-6 sentences with **bold** + dates>"}}
  ]
}}

3-5 sections. No prose outside the JSON."""


# ──────────────────────────────────────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────────────────────────────────────
def _is_stale(generated_at: str | None, source_count_old: int | None, current_count: int) -> bool:
    if not generated_at or source_count_old is None:
        return True
    try:
        ts = datetime.fromisoformat(generated_at)
    except ValueError:
        return True
    if datetime.now() - ts > timedelta(hours=CACHE_TTL_HOURS):
        return True
    if source_count_old <= 0:
        return True
    drift = abs(current_count - source_count_old) / source_count_old
    return drift >= DRIFT_PCT


def _sc_inputs(conn: sqlite3.Connection) -> list[dict]:
    """Return one entry per kept SC with its pages and ctx counts."""
    out = []
    sc_rows = conn.execute(
        "SELECT label FROM sc_registry WHERE is_keep=1"
    ).fetchall()
    for (label,) in sc_rows:
        pages = [dict(r) for r in conn.execute("""
            SELECT id, title, date_local, ctx_label
            FROM work_pages
            WHERE sc_label=? AND COALESCE(is_archived,0)=0 AND COALESCE(is_unfiled,0)=0
            ORDER BY date_local DESC LIMIT ?
        """, (label, PAGES_FOR_SC * 3)).fetchall()]
        ctx_counter: Counter = Counter()
        for r in conn.execute(
            "SELECT ctx_label, COUNT(*) FROM work_pages "
            "WHERE sc_label=? AND COALESCE(is_archived,0)=0 GROUP BY ctx_label",
            (label,),
        ).fetchall():
            ctx_counter[r[0] or "—"] = r[1]
        out.append({
            "label": label,
            "pages": pages,
            "ctxs": ctx_counter.most_common(),
            "current_count": sum(ctx_counter.values()),
        })
    return out


def _deliv_inputs(conn: sqlite3.Connection) -> list[dict]:
    """Return one entry per deliverable with its matched pages + stats."""
    out = []
    rows = conn.execute("""
        SELECT d.id, d.project_id, d.title, d.description, d.keywords,
               p.name AS project_name
        FROM deliverables d
        JOIN projects p ON p.id = d.project_id
        WHERE d.status != 'archived'
    """).fetchall()
    for r in rows:
        try:
            kws = json.loads(r[4] or "[]")
        except Exception:
            kws = []
        match_pages = [dict(rr) for rr in conn.execute("""
            SELECT wp.id, wp.title, wp.date_local, wp.total_minutes,
                   dm.score, dm.reasons
            FROM deliverable_match dm
            JOIN work_pages wp ON wp.id = dm.page_id
            WHERE dm.deliverable_id = ?
            ORDER BY dm.score DESC, wp.date_local DESC
            LIMIT ?
        """, (r[0], PAGES_FOR_DELIV * 2)).fetchall()]
        if not match_pages:
            stats = {"n_pages": 0, "minutes": 0, "days_active": 0, "last": None}
        else:
            dates = sorted({p["date_local"] for p in match_pages if p["date_local"]})
            stats = {
                "n_pages": len(match_pages),
                "minutes": round(sum(p["total_minutes"] or 0 for p in match_pages), 1),
                "days_active": len(dates),
                "last": dates[-1] if dates else None,
            }
        out.append({
            "id": r[0], "project_id": r[1], "project_name": r[5],
            "title": r[2], "description": r[3] or "", "keywords": kws,
            "matched_pages": match_pages, "stats": stats,
            "current_count": stats["n_pages"],
        })
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
def _synth_one_sc(item: dict) -> tuple[str, dict]:
    prompt = _build_sc_prompt(item["label"], item["pages"], item["ctxs"])
    return item["label"], _llm_json(prompt, max_tokens=1800)


def _synth_one_deliv(item: dict) -> tuple[str, dict]:
    prompt = _build_deliv_prompt(item, item["matched_pages"], item["stats"])
    return item["id"], _llm_json(prompt, max_tokens=1500)


def synthesize_all(prome_db: str | Path, force: bool = False) -> dict:
    prome_db = Path(prome_db)
    conn = sqlite3.connect(prome_db, timeout=60.0)
    conn.row_factory = sqlite3.Row
    now = datetime.now().isoformat(timespec="seconds")

    # Decide which SC + deliverable items need re-generation
    sc_items = _sc_inputs(conn)
    sc_to_synth = []
    for item in sc_items:
        cache = conn.execute(
            "SELECT prose_json, source_page_count, generated_at FROM sc_wiki_cache "
            "WHERE sc_label = ?", (item["label"],),
        ).fetchone()
        old_count = cache["source_page_count"] if cache else None
        if force or _is_stale(cache["generated_at"] if cache else None,
                              old_count, item["current_count"]):
            sc_to_synth.append(item)

    deliv_items = _deliv_inputs(conn)
    deliv_to_synth = []
    for item in deliv_items:
        cache = conn.execute(
            "SELECT prose_json, source_page_count, generated_at FROM deliverable_wiki_cache "
            "WHERE deliverable_id = ?", (item["id"],),
        ).fetchone()
        old_count = cache["source_page_count"] if cache else None
        if force or _is_stale(cache["generated_at"] if cache else None,
                              old_count, item["current_count"]):
            deliv_to_synth.append(item)

    if not sc_to_synth and not deliv_to_synth:
        conn.close()
        return {"ok": True, "phase": "synthesis", "skipped": True,
                "reason": "all caches fresh", "n_sc": 0, "n_deliv": 0}

    print(f"    synthesis: {len(sc_to_synth)} SCs + {len(deliv_to_synth)} deliverables to (re)generate",
          flush=True)

    started = time.time()
    sc_results: dict[str, dict] = {}
    deliv_results: dict[str, dict] = {}
    sc_failed = 0
    deliv_failed = 0

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = []
        for it in sc_to_synth:
            futures.append(("sc", ex.submit(_synth_one_sc, it), it))
        for it in deliv_to_synth:
            futures.append(("deliv", ex.submit(_synth_one_deliv, it), it))
        for kind, fut, it in futures:
            try:
                key, data = fut.result()
            except Exception as e:
                print(f"  ⚠ synth {kind} {it.get('id') or it.get('label')} failed: {e}")
                if kind == "sc":
                    sc_failed += 1
                else:
                    deliv_failed += 1
                continue
            if not data or "sections" not in data:
                if kind == "sc":
                    sc_failed += 1
                else:
                    deliv_failed += 1
                continue
            if kind == "sc":
                sc_results[key] = data
            else:
                deliv_results[key] = data

    # Persist caches
    for label, data in sc_results.items():
        item = next(i for i in sc_to_synth if i["label"] == label)
        conn.execute("""
            INSERT INTO sc_wiki_cache (sc_label, prose_json, source_page_count, generated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(sc_label) DO UPDATE SET
              prose_json=excluded.prose_json,
              source_page_count=excluded.source_page_count,
              generated_at=excluded.generated_at
        """, (label, json.dumps(data), item["current_count"], now))
    for did, data in deliv_results.items():
        item = next(i for i in deliv_to_synth if i["id"] == did)
        conn.execute("""
            INSERT INTO deliverable_wiki_cache (deliverable_id, prose_json,
                                                source_page_count, generated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(deliverable_id) DO UPDATE SET
              prose_json=excluded.prose_json,
              source_page_count=excluded.source_page_count,
              generated_at=excluded.generated_at
        """, (did, json.dumps(data), item["current_count"], now))
    conn.commit()
    conn.close()

    return {
        "ok": True, "phase": "synthesis", "skipped": False,
        "n_sc_attempted": len(sc_to_synth), "n_sc_ok": len(sc_results),
        "n_sc_failed": sc_failed,
        "n_deliv_attempted": len(deliv_to_synth), "n_deliv_ok": len(deliv_results),
        "n_deliv_failed": deliv_failed,
        "duration_sec": round(time.time() - started, 1),
    }


if __name__ == "__main__":
    DB = Path(__file__).resolve().parent.parent / "data" / "prome.db"
    envf = Path(__file__).resolve().parent.parent / ".env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
    force = "--force" in sys.argv
    print(json.dumps(synthesize_all(DB, force=force), indent=2))
