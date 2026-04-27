"""Stage 3 — classify each unclassified work_page into (sc_label, ctx_label).

LLM: OpenAI gpt-4o-mini via raw httpx.
Batch: 10 pages per LLM call. Concurrency: 8 parallel calls.
SC list grows over time — LLM may propose new SCs which land in pending_sc.

Idempotent: only touches rows where sc_label='' AND is_unfiled=0.
"""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db

MODEL = os.environ.get("PROME_CLASSIFY_MODEL", "gpt-4o-mini")
BATCH_SIZE = 10
CONCURRENCY = 8
LLM_TIMEOUT = 60.0
LLM_MAX_RETRIES = 2


def _llm_json(prompt: str) -> dict:
    """Call gpt-4o-mini with response_format=json_object. Returns parsed dict or {}."""
    from ._openai_client import credentials   # proxy/direct dispatch
    key, base = credentials()
    last_err = ""
    for attempt in range(LLM_MAX_RETRIES + 1):
        try:
            r = httpx.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": MODEL, "max_tokens": 1500, "temperature": 0.1,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=LLM_TIMEOUT,
            )
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return {}
            content = r.json()["choices"][0]["message"]["content"]
            return json.loads(content)
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            last_err = str(e)
            time.sleep(1.0 * (attempt + 1))
    print(f"  ⚠ llm call failed after retries: {last_err[:120]}")
    return {}


def _build_prompt(allowed_scs: list[str], pages: list[dict]) -> str:
    sc_list = "\n".join(f"- {s}" for s in allowed_scs)
    activities = json.dumps([
        {"id": p["id"], "title": (p["title"] or "")[:140],
         "summary": (p["summary"] or "")[:400]}
        for p in pages
    ], ensure_ascii=False)
    return f"""You classify captured work activities into a Super Context (SC) and a Context (CTX) within it.

Allowed SC labels (use these exactly when one fits):
{sc_list}

If NONE of the allowed labels fit, propose a new SC label that is broad and reusable (2-4 capitalised words, e.g. "Customer Support", "Hiring & Recruiting"). Set "propose_new" to true in that case.

For CTX: a 2-5 word phrase naming the specific phase/skill within the SC, e.g. "Code review", "Cold outreach drafting", "Account research".

Activities (classify each one, return one object per input id):
{activities}

Respond with a strict JSON object — no prose, no markdown:
{{"results": [{{"id": "<input id>", "sc": "<label>", "ctx": "<2-5 words>", "propose_new": <bool>, "why": "<one short sentence>"}}]}}"""


def _allowed_scs(c, user_id: str) -> list[str]:
    rows = c.execute(
        "SELECT label FROM sc_registry WHERE user_id=%s ORDER BY is_keep DESC, label",
        (user_id,),
    ).fetchall()
    return [r["label"] for r in rows]


def _fetch_unclassified(c, user_id: str, limit: int | None = None) -> list[dict]:
    sql = ("SELECT id, title, summary FROM work_pages "
           "WHERE user_id=%s AND sc_label='' AND COALESCE(is_unfiled,0)=0 "
           "ORDER BY date_local DESC")
    params: tuple = (user_id,)
    if limit:
        sql += " LIMIT %s"
        params = (user_id, limit)
    return [dict(r) for r in c.execute(sql, params).fetchall()]


def _apply_classification(c, user_id: str, page_id: str, sc: str,
                           ctx: str, propose_new: bool, why: str,
                           known_scs: set[str]) -> str:
    """Returns: 'classified' | 'pending'."""
    now = datetime.now().isoformat(timespec="seconds")
    if propose_new or sc not in known_scs:
        existing = c.execute(
            "SELECT id, proposal_count, example_page_ids FROM pending_sc "
            "WHERE user_id=%s AND proposed_label=%s AND status='pending'",
            (user_id, sc),
        ).fetchone()
        if existing:
            try:
                examples = json.loads(existing["example_page_ids"] or "[]")
            except Exception:
                examples = []
            if page_id not in examples:
                examples.append(page_id)
            c.execute(
                "UPDATE pending_sc SET proposal_count=%s, example_page_ids=%s, "
                "last_seen=%s WHERE id=%s AND user_id=%s",
                (existing["proposal_count"] + 1, json.dumps(examples[:10]),
                 now, existing["id"], user_id),
            )
        else:
            c.execute(
                "INSERT INTO pending_sc (user_id, proposed_label, why, "
                "example_page_ids, proposal_count, first_seen, last_seen, status) "
                "VALUES (%s, %s, %s, %s, 1, %s, %s, 'pending')",
                (user_id, sc, why or "", json.dumps([page_id]), now, now),
            )
        c.execute(
            "UPDATE work_pages SET is_unfiled=1, ctx_label=%s, classified_at=%s "
            "WHERE id=%s AND user_id=%s",
            (ctx or "", now, page_id, user_id),
        )
        return "pending"

    c.execute(
        "UPDATE work_pages SET sc_label=%s, ctx_label=%s, classified_at=%s "
        "WHERE id=%s AND user_id=%s",
        (sc, ctx or "", now, page_id, user_id),
    )
    return "classified"


def _classify_batch(allowed_scs: list[str], batch: list[dict]) -> list[dict]:
    prompt = _build_prompt(allowed_scs, batch)
    resp = _llm_json(prompt)
    return resp.get("results", []) if isinstance(resp, dict) else []


def classify_all(limit: int | None = None,
                 batch_size: int = BATCH_SIZE,
                 concurrency: int = CONCURRENCY) -> dict:
    user_id = db.user_id()
    with db.conn() as c:
        allowed = _allowed_scs(c, user_id)
        known = set(allowed)
        pages = _fetch_unclassified(c, user_id, limit=limit)

    if not pages:
        return {"ok": True, "phase": "classify", "skipped": True,
                "reason": "no unclassified pages", "n_total": 0}

    batches = [pages[i:i + batch_size] for i in range(0, len(pages), batch_size)]
    n_classified = 0
    n_pending = 0
    n_failed = 0
    started = time.time()

    # Concurrent LLM calls; DB writes serialised on a single pooled connection.
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(_classify_batch, allowed, b): b for b in batches}
        with db.conn() as wc:
            for i, fut in enumerate(as_completed(futures), 1):
                batch = futures[fut]
                results = fut.result()
                by_id = {r.get("id"): r for r in results if r.get("id")}
                for p in batch:
                    r = by_id.get(p["id"])
                    if not r:
                        n_failed += 1
                        continue
                    sc = (r.get("sc") or "").strip()
                    ctx = (r.get("ctx") or "").strip()
                    propose = bool(r.get("propose_new"))
                    why = (r.get("why") or "").strip()
                    if not sc:
                        n_failed += 1
                        continue
                    outcome = _apply_classification(
                        wc, user_id, p["id"], sc, ctx, propose, why, known,
                    )
                    if outcome == "classified":
                        n_classified += 1
                    elif outcome == "pending":
                        n_pending += 1
                if i % 10 == 0 or i == len(batches):
                    elapsed = time.time() - started
                    rate = (n_classified + n_pending) / max(1, elapsed)
                    print(f"    classify: {i}/{len(batches)} batches "
                          f"({n_classified} ok, {n_pending} pending, {n_failed} failed) "
                          f"{rate:.1f} pages/sec",
                          flush=True)

    return {
        "ok": True, "phase": "classify", "skipped": False,
        "n_total": len(pages), "n_classified": n_classified,
        "n_pending": n_pending, "n_failed": n_failed,
        "duration_sec": round(time.time() - started, 1),
    }


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    print(json.dumps(classify_all(limit=limit), indent=2))
