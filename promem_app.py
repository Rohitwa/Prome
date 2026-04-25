"""ProMe simplified server — Stage 6.

FastAPI on port 8888. Reads prome.db (memory + projects) and tracker.db
(productivity widgets). Mirrors the page shape of port 8100 but on the new
simplified engine only.

Routes:
  /                              redirect → /wiki
  /wiki                          index of 4 SC cards + archive
  /wiki/sc/<slug>                single SC wiki page (Karpathy-style)
  /wiki/archive                  archived pages, day-grouped
  /projects                      tracker dashboard (Tree + Daily tabs)
  /projects/new                  CRUD form for project + deliverables
  /productivity                  4 widgets reading tracker.db

APIs (JSON):
  POST /api/projects                                {name, owner, description}
  POST /api/deliverables                            {project_id, title, ...}
  POST /api/deliverables/<id>/feedback              {date, verdict, note}
  POST /api/deliverables/<id>/match/<page_id>/pin
  POST /api/deliverables/<id>/match/<page_id>/unpin
  POST /api/orchestrator/run                        — manual tick
  GET  /api/orchestrator/status                     — last_*_at + should_run

Run:
  python3 promem_app.py
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("PROMEM_DATA_DIR", str(ROOT / "data")))
DB = DATA_DIR / "prome.db"
TRACKER = Path(os.environ.get("PROMEM_TRACKER_DB", str(DATA_DIR / "tracker.db")))
TEMPLATES = Jinja2Templates(directory=str(ROOT / "templates"))

# Load .env on startup so phase calls into orchestrator have OPENAI_API_KEY.
ENV_FILE = ROOT / ".env"
if ENV_FILE.exists():
    for _ln in ENV_FILE.read_text().splitlines():
        if "=" in _ln and not _ln.startswith("#"):
            _k, _, _v = _ln.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

def _ensure_schema() -> None:
    """Create data dir + apply the 0001 migration on first boot.
    The migration is `CREATE TABLE IF NOT EXISTS` so re-runs are safe."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    migration = ROOT / "migrations" / "0001_prome_simple.sql"
    if migration.exists():
        with sqlite3.connect(DB) as c:
            c.executescript(migration.read_text())


_ensure_schema()
app = FastAPI(title="ProMem")


# ──────────────────────────────────────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────────────────────────────────────
def _conn(path: Path = DB) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{path}?mode=ro" if path == TRACKER else path,
                        uri=(path == TRACKER), timeout=30.0)
    c.row_factory = sqlite3.Row
    return c


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _md_to_html(md: str) -> str:
    """Tiny markdown → HTML for synthesized prose (bold, code, paragraphs, bullets)."""
    md = (md or "").strip()
    md = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", md)
    md = re.sub(r"`([^`]+)`", r"<code>\1</code>", md)
    blocks = re.split(r"\n\n+", md)
    out = []
    for blk in blocks:
        lines = [l for l in blk.splitlines() if l.strip()]
        if all(l.strip().startswith(("- ", "* ")) for l in lines if l.strip()):
            items = "".join(f"<li>{l.strip()[2:]}</li>" for l in lines)
            out.append(f"<ul>{items}</ul>")
        else:
            out.append(f"<p>{blk.replace(chr(10), '<br>')}</p>")
    return "\n".join(out)


# Register the helper so templates can use {{ body|md }}
TEMPLATES.env.filters["md"] = _md_to_html


# ──────────────────────────────────────────────────────────────────────────────
# Routes — pages
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def root() -> RedirectResponse:
    return RedirectResponse(url="/wiki")


@app.get("/wiki", response_class=HTMLResponse)
def wiki_index(request: Request) -> HTMLResponse:
    conn = _conn()
    cards = []
    for r in conn.execute(
        "SELECT label, is_keep FROM sc_registry ORDER BY is_keep DESC, label"
    ).fetchall():
        if r["is_keep"] == 0:
            continue
        n_pages = conn.execute(
            "SELECT COUNT(*) FROM work_pages WHERE sc_label=? AND COALESCE(is_archived,0)=0",
            (r["label"],),
        ).fetchone()[0]
        cache = conn.execute(
            "SELECT prose_json, generated_at FROM sc_wiki_cache WHERE sc_label=?",
            (r["label"],),
        ).fetchone()
        prose = json.loads(cache["prose_json"]) if cache and cache["prose_json"] else None
        cards.append({
            "label": r["label"], "slug": _slug(r["label"]),
            "n_pages": n_pages,
            "tagline": (prose or {}).get("tagline") or "",
            "generated_at": (cache or {})["generated_at"] if cache else None,
        })
    n_arch = conn.execute(
        "SELECT COUNT(*) FROM work_pages WHERE COALESCE(is_archived,0)=1"
    ).fetchone()[0]
    n_unfiled = conn.execute(
        "SELECT COUNT(*) FROM work_pages WHERE COALESCE(is_unfiled,0)=1"
    ).fetchone()[0]
    n_pending_sc = conn.execute(
        "SELECT COUNT(*) FROM pending_sc WHERE status='pending'"
    ).fetchone()[0]
    state = conn.execute("SELECT * FROM orchestrator_state WHERE id=1").fetchone()
    conn.close()
    return TEMPLATES.TemplateResponse(request, "promem_wiki_index.html", {
        "cards": cards, "n_archive": n_arch,
        "n_unfiled": n_unfiled, "n_pending_sc": n_pending_sc,
        "state": dict(state) if state else {},
    })


@app.get("/wiki/sc/{slug}", response_class=HTMLResponse)
def wiki_sc(slug: str, request: Request) -> HTMLResponse:
    conn = _conn()
    sc_row = None
    for r in conn.execute(
        "SELECT label FROM sc_registry WHERE is_keep=1"
    ).fetchall():
        if _slug(r["label"]) == slug:
            sc_row = r["label"]
            break
    if not sc_row:
        conn.close()
        raise HTTPException(404, f"No keep SC matches slug '{slug}'")
    cache = conn.execute(
        "SELECT prose_json, generated_at, source_page_count FROM sc_wiki_cache "
        "WHERE sc_label=?", (sc_row,),
    ).fetchone()
    prose = json.loads(cache["prose_json"]) if cache and cache["prose_json"] else {
        "tagline": "(no synthesis yet — run the synthesis phase)",
        "sections": [],
    }

    by_ctx = defaultdict(list)
    for r in conn.execute(
        "SELECT id, title, summary, date_local, ctx_label, total_minutes "
        "FROM work_pages WHERE sc_label=? AND COALESCE(is_archived,0)=0 "
        "ORDER BY date_local DESC LIMIT 200", (sc_row,),
    ).fetchall():
        by_ctx[r["ctx_label"] or "—"].append(dict(r))
    n_pages = conn.execute(
        "SELECT COUNT(*) FROM work_pages WHERE sc_label=? AND COALESCE(is_archived,0)=0",
        (sc_row,),
    ).fetchone()[0]
    conn.close()
    return TEMPLATES.TemplateResponse(request, "promem_sc.html", {
        "label": sc_row, "slug": slug, "prose": prose,
        "by_ctx": dict(by_ctx), "n_pages": n_pages,
        "generated_at": (cache or {})["generated_at"] if cache else None,
        "source_count": (cache or {})["source_page_count"] if cache else 0,
    })


@app.get("/wiki/archive", response_class=HTMLResponse)
def wiki_archive(request: Request) -> HTMLResponse:
    conn = _conn()
    by_sc: dict[str, list[dict]] = defaultdict(list)
    for r in conn.execute(
        "SELECT id, title, summary, date_local, sc_label, ctx_label "
        "FROM work_pages WHERE COALESCE(is_archived,0)=1 "
        "ORDER BY date_local DESC LIMIT 500"
    ).fetchall():
        by_sc[r["sc_label"]].append(dict(r))
    n_total = conn.execute(
        "SELECT COUNT(*) FROM work_pages WHERE COALESCE(is_archived,0)=1"
    ).fetchone()[0]
    conn.close()
    return TEMPLATES.TemplateResponse(request, "promem_archive.html", {
        "by_sc": dict(by_sc), "n_total": n_total,
    })


@app.get("/projects", response_class=HTMLResponse)
def projects_view(request: Request) -> HTMLResponse:
    conn = _conn()
    projects = [dict(r) for r in conn.execute(
        "SELECT * FROM projects ORDER BY created_at DESC"
    ).fetchall()]
    deliv_by_project: dict[str, list[dict]] = defaultdict(list)
    for r in conn.execute("SELECT * FROM deliverables").fetchall():
        d = dict(r)
        try:
            d["keywords_list"] = json.loads(d.get("keywords") or "[]")
        except Exception:
            d["keywords_list"] = []
        try:
            d["ctx_hints_list"] = json.loads(d.get("ctx_hints") or "[]")
        except Exception:
            d["ctx_hints_list"] = []
        # Match pages
        d["matches"] = [dict(m) for m in conn.execute("""
            SELECT dm.*, wp.title AS page_title, wp.summary AS page_summary,
                   wp.date_local, wp.ctx_label, wp.total_minutes,
                   wp.id AS page_id
            FROM deliverable_match dm
            JOIN work_pages wp ON wp.id = dm.page_id
            WHERE dm.deliverable_id = ?
            ORDER BY wp.date_local DESC, dm.score DESC
        """, (d["id"],)).fetchall()]
        # Stats
        dates = sorted({m["date_local"] for m in d["matches"] if m["date_local"]})
        d["stats"] = {
            "n_pages": len(d["matches"]),
            "minutes": round(sum(m["total_minutes"] or 0 for m in d["matches"]), 1),
            "days_active": len(dates),
            "last": dates[-1] if dates else None,
        }
        # Wiki cache
        cache = conn.execute(
            "SELECT prose_json FROM deliverable_wiki_cache WHERE deliverable_id=?",
            (d["id"],),
        ).fetchone()
        d["wiki"] = json.loads(cache["prose_json"]) if cache and cache["prose_json"] else None
        # Group matches by date for daily tab
        by_date = defaultdict(list)
        for m in d["matches"]:
            by_date[m["date_local"]].append(m)
        d["by_date"] = dict(sorted(by_date.items(), reverse=True))
        deliv_by_project[d["project_id"]].append(d)
    # Existing feedback for restoring tick/cross state
    fb_rows = [dict(r) for r in conn.execute(
        "SELECT deliverable_id, date, verdict, note FROM deliverable_daily_feedback"
    ).fetchall()]
    fb_map: dict[str, dict] = {}
    for fb in fb_rows:
        fb_map[f"{fb['deliverable_id']}|{fb['date']}"] = fb
    n_pages_total = conn.execute("SELECT COUNT(*) FROM work_pages").fetchone()[0]
    conn.close()
    return TEMPLATES.TemplateResponse(request, "promem_projects.html", {
        "projects": projects,
        "deliv_by_project": dict(deliv_by_project),
        "n_pages_total": n_pages_total,
        "fb_map": fb_map,
    })


@app.get("/projects/new", response_class=HTMLResponse)
def projects_new(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "promem_project_new.html", {})


@app.get("/productivity", response_class=HTMLResponse)
def productivity(request: Request, date: str | None = None) -> HTMLResponse:
    from datetime import date as _d, timedelta as _td, datetime as _dt
    if not TRACKER.exists():
        # Cloud / fresh-deploy: tracker.db isn't wired up yet. Show a small
        # informational page rather than crashing the route.
        return HTMLResponse(
            f"""<!doctype html><html><head><title>productivity — ProMem</title>
<style>body{{font-family:-apple-system,Segoe UI,system-ui,sans-serif;
max-width:640px;margin:60px auto;padding:24px;color:#1a1a1a;background:#f7f7f8;}}
h1{{margin-top:0;}} code{{background:#eef0f3;padding:2px 6px;border-radius:4px;
font-family:ui-monospace,monospace;font-size:13px;}}
.note{{background:#fff;border:1px solid #e2e4e8;border-left:3px solid #ea580c;
padding:12px 16px;border-radius:8px;margin-top:16px;}}
a{{color:#2563eb;text-decoration:none;}}</style></head><body>
<h1>productivity</h1>
<p>Tracker database not connected.</p>
<div class="note">ProMem couldn't find <code>tracker.db</code> at <code>{TRACKER}</code>.
Set <code>PROMEM_TRACKER_DB</code> to point at a productivity-tracker SQLite file,
or mount one to that path.</div>
<p style="margin-top:24px"><a href="/wiki">← memory wiki</a> &nbsp;
<a href="/projects">projects →</a></p>
</body></html>""",
            status_code=200,
        )
    sel_date = (date or _d.today().strftime("%Y-%m-%d")).strip()
    try:
        sel_dt = _dt.strptime(sel_date, "%Y-%m-%d").date()
    except ValueError:
        sel_dt = _d.today()
        sel_date = sel_dt.strftime("%Y-%m-%d")

    tconn = _conn(TRACKER)

    # ── Stat cards: total, productive, human, ai (in minutes for selected date)
    stats_row = tconn.execute("""
        SELECT
          COALESCE(SUM(target_segment_length_secs), 0) AS total_secs,
          COALESCE(SUM(CASE WHEN is_productive = 1 THEN target_segment_length_secs ELSE 0 END), 0) AS prod_secs,
          COALESCE(SUM(CASE WHEN worker = 'human' THEN target_segment_length_secs ELSE 0 END), 0) AS human_secs,
          COALESCE(SUM(CASE WHEN worker IN ('ai','autonomous','agent') THEN target_segment_length_secs ELSE 0 END), 0) AS ai_secs,
          COALESCE(SUM(human_frame_count), 0) AS human_frames,
          COALESCE(SUM(ai_frame_count), 0) AS ai_frames,
          COUNT(*) AS n_segments
        FROM context_1 WHERE date(timestamp_start) = ?
    """, (sel_date,)).fetchone()
    stats = {
        "total_min": round((stats_row["total_secs"] or 0) / 60),
        "prod_min": round((stats_row["prod_secs"] or 0) / 60),
        "human_min": round((stats_row["human_secs"] or 0) / 60),
        "ai_min": round((stats_row["ai_secs"] or 0) / 60),
        "human_frames": stats_row["human_frames"] or 0,
        "ai_frames": stats_row["ai_frames"] or 0,
        "n_segments": stats_row["n_segments"] or 0,
    }

    # ── 30-day coverage heatmap (one cell per day)
    coverage = []
    for i in range(29, -1, -1):
        d = (sel_dt - _td(days=i)).strftime("%Y-%m-%d")
        secs = tconn.execute(
            "SELECT COALESCE(SUM(target_segment_length_secs),0) FROM context_1 "
            "WHERE date(timestamp_start) = ?", (d,),
        ).fetchone()[0] or 0
        # Buckets: 0=less, 1=light, 2=mid, 3=more, 4=heavy
        if secs == 0:
            level = 0
        elif secs < 1800:    # <30min
            level = 1
        elif secs < 5400:    # <90min
            level = 2
        elif secs < 14400:   # <240min
            level = 3
        else:
            level = 4
        coverage.append({"date": d, "level": level, "min": round(secs / 60)})

    # ── Weekly trend (Sun..Sat ending at sel_date's week)
    # Build 7 days ending at sel_date inclusive, labelled by weekday name
    weekly = []
    for i in range(6, -1, -1):
        d = (sel_dt - _td(days=i)).strftime("%Y-%m-%d")
        secs = tconn.execute(
            "SELECT COALESCE(SUM(target_segment_length_secs),0) FROM context_1 "
            "WHERE date(timestamp_start) = ?", (d,),
        ).fetchone()[0] or 0
        weekly.append({
            "date": d,
            "label": _dt.strptime(d, "%Y-%m-%d").strftime("%a"),
            "min": round(secs / 60),
            "is_sel": (d == sel_date),
        })

    # ── Time by application for selected date
    by_app = [
        {"app": (r["window_name"] or r["platform"] or "—")[:60],
         "secs": int(r["secs"] or 0)}
        for r in tconn.execute("""
            SELECT COALESCE(NULLIF(window_name,''), platform) AS window_name,
                   platform, SUM(target_segment_length_secs) AS secs
            FROM context_1 WHERE date(timestamp_start) = ?
            GROUP BY window_name, platform
            ORDER BY secs DESC LIMIT 12
        """, (sel_date,)).fetchall()
    ]

    # ── Hourly activity (selected date, 0-23 buckets in MINUTES)
    hour_counts = [0] * 24
    for r in tconn.execute("""
        SELECT CAST(strftime('%H', timestamp_start) AS INTEGER) AS hr,
               SUM(target_segment_length_secs) AS secs
        FROM context_1 WHERE date(timestamp_start) = ? GROUP BY hr
    """, (sel_date,)).fetchall():
        hr = r["hr"] if r["hr"] is not None else 0
        hour_counts[hr] = int((r["secs"] or 0) // 60)

    # ── Input activity (frames + keyboard + mouse from context_2 joined to context_1)
    inp_row = tconn.execute("""
        SELECT COUNT(*) AS frames,
               COALESCE(SUM(CASE WHEN has_keyboard_activity=1 THEN 1 ELSE 0 END), 0) AS kb,
               COALESCE(SUM(CASE WHEN has_mouse_activity=1 THEN 1 ELSE 0 END), 0) AS mouse
        FROM context_2 c2
        JOIN context_1 c1 ON c1.target_segment_id = c2.target_segment_id
        WHERE date(c1.timestamp_start) = ?
    """, (sel_date,)).fetchone()
    input_act = {
        "frames": inp_row["frames"] or 0,
        "kb": inp_row["kb"] or 0,
        "mouse": inp_row["mouse"] or 0,
    }

    # ── Recent activity list (last 12 segments of selected date)
    recent = [
        {"time": r["t"], "title": r["short_title"] or r["window_name"] or "—",
         "worker": r["worker"] or "—"}
        for r in tconn.execute("""
            SELECT strftime('%H:%M', timestamp_start) AS t,
                   short_title, window_name, worker
            FROM context_1 WHERE date(timestamp_start) = ?
            ORDER BY timestamp_start DESC LIMIT 12
        """, (sel_date,)).fetchall()
    ]
    tconn.close()

    # ── Time by project (joins through prome.db)
    pconn = _conn(DB)
    by_project = [dict(r) for r in pconn.execute("""
        SELECT p.id, p.name,
               COUNT(DISTINCT wp.id) AS pages,
               ROUND(SUM(wp.total_minutes), 1) AS mins
        FROM projects p
        JOIN deliverables d ON d.project_id = p.id
        JOIN deliverable_match dm ON dm.deliverable_id = d.id
        JOIN work_pages wp ON wp.id = dm.page_id
        WHERE wp.date_local = ?
        GROUP BY p.id, p.name
        ORDER BY mins DESC
    """, (sel_date,)).fetchall()]
    top_ctx = [dict(r) for r in pconn.execute("""
        SELECT ctx_label, COUNT(*) AS n
        FROM work_pages WHERE date_local = ? AND ctx_label != ''
        GROUP BY ctx_label ORDER BY n DESC LIMIT 8
    """, (sel_date,)).fetchall()]
    pconn.close()

    return TEMPLATES.TemplateResponse(request, "promem_productivity.html", {
        "sel_date": sel_date,
        "stats": stats,
        "coverage": coverage,
        "weekly": weekly,
        "max_weekly": max((w["min"] for w in weekly), default=0) or 1,
        "by_app": by_app,
        "by_project": by_project,
        "hour_counts": hour_counts,
        "max_hour": max(hour_counts) or 1,
        "input_act": input_act,
        "recent": recent,
        "top_ctx": top_ctx,
    })


# ──────────────────────────────────────────────────────────────────────────────
# Routes — APIs
# ──────────────────────────────────────────────────────────────────────────────
class ProjectIn(BaseModel):
    name: str
    owner: str = ""
    description: str = ""


class DeliverableIn(BaseModel):
    project_id: str
    title: str
    description: str = ""
    keywords: list[str] = []
    ctx_hints: list[str] = []


class FeedbackIn(BaseModel):
    date: str
    verdict: str
    note: str = ""


@app.post("/api/projects")
def api_create_project(p: ProjectIn) -> dict:
    pid = "P-" + secrets.token_hex(4)
    conn = sqlite3.connect(DB, timeout=30.0)
    conn.execute(
        "INSERT INTO projects (id, name, owner, description, status) "
        "VALUES (?, ?, ?, ?, 'active')",
        (pid, p.name, p.owner, p.description),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "id": pid}


@app.post("/api/deliverables")
def api_create_deliverable(d: DeliverableIn) -> dict:
    did = "D-" + secrets.token_hex(4)
    conn = sqlite3.connect(DB, timeout=30.0)
    conn.execute(
        "INSERT INTO deliverables (id, project_id, title, description, "
        "keywords, ctx_hints, status) VALUES (?, ?, ?, ?, ?, ?, 'in progress')",
        (did, d.project_id, d.title, d.description,
         json.dumps(d.keywords), json.dumps(d.ctx_hints)),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "id": did}


@app.post("/api/deliverables/{did}/feedback")
def api_feedback(did: str, fb: FeedbackIn) -> dict:
    if fb.verdict not in ("correct", "wrong"):
        raise HTTPException(400, "verdict must be 'correct' or 'wrong'")
    conn = sqlite3.connect(DB, timeout=30.0)
    # Upsert: same (deliv, date) replaces the previous feedback
    conn.execute(
        "DELETE FROM deliverable_daily_feedback WHERE deliverable_id=? AND date=?",
        (did, fb.date),
    )
    conn.execute(
        "INSERT INTO deliverable_daily_feedback (deliverable_id, date, verdict, note) "
        "VALUES (?, ?, ?, ?)",
        (did, fb.date, fb.verdict, fb.note),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/deliverables/{did}/match/{pid}/{action}")
def api_pin_unpin(did: str, pid: str, action: str) -> dict:
    if action not in ("pin", "unpin"):
        raise HTTPException(400, "action must be 'pin' or 'unpin'")
    conn = sqlite3.connect(DB, timeout=30.0)
    if action == "pin":
        conn.execute("""
            INSERT INTO deliverable_match (deliverable_id, page_id, score, source, matched_at)
            VALUES (?, ?, 1.0, 'pin', datetime('now'))
            ON CONFLICT(deliverable_id, page_id) DO UPDATE SET source='pin'
        """, (did, pid))
    else:
        conn.execute(
            "DELETE FROM deliverable_match WHERE deliverable_id=? AND page_id=?",
            (did, pid),
        )
    conn.commit()
    conn.close()
    return {"ok": True, "action": action}


@app.post("/api/orchestrator/run")
def api_orch_run() -> dict:
    sys.path.insert(0, str(ROOT))
    from promem_orchestrator import run_full
    return run_full(reason="manual")


@app.get("/api/orchestrator/status")
def api_orch_status() -> dict:
    sys.path.insert(0, str(ROOT))
    from promem_orchestrator import get_state, should_run
    ok, reason = should_run()
    return {**get_state(), "should_run_now": ok, "reason": reason}


# ──────────────────────────────────────────────────────────────────────────────
# Form handlers (HTML form posts → /projects/new)
# ──────────────────────────────────────────────────────────────────────────────
@app.post("/projects/new")
async def form_create_project_with_deliverables(request: Request) -> RedirectResponse:
    form = await request.form()
    name = (form.get("name") or "").strip()
    owner = (form.get("owner") or "").strip()
    description = (form.get("description") or "").strip()
    if not name:
        return RedirectResponse(url="/projects/new?err=name", status_code=303)
    pid = "P-" + secrets.token_hex(4)
    conn = sqlite3.connect(DB, timeout=30.0)
    conn.execute(
        "INSERT INTO projects (id, name, owner, description, status) "
        "VALUES (?, ?, ?, ?, 'active')",
        (pid, name, owner, description),
    )
    # deliverable rows: titles[]/keywords[]
    titles = form.getlist("d_title")
    descs = form.getlist("d_desc")
    keywords_lists = form.getlist("d_keywords")
    for i, t in enumerate(titles):
        t = (t or "").strip()
        if not t:
            continue
        d_desc = (descs[i] if i < len(descs) else "") or ""
        kws = [w.strip() for w in (keywords_lists[i] if i < len(keywords_lists) else "").split(",") if w.strip()]
        did = "D-" + secrets.token_hex(4)
        conn.execute(
            "INSERT INTO deliverables (id, project_id, title, description, "
            "keywords, ctx_hints, status) VALUES (?, ?, ?, ?, ?, '[]', 'in progress')",
            (did, pid, t, d_desc, json.dumps(kws)),
        )
    conn.commit()
    conn.close()
    return RedirectResponse(url="/projects", status_code=303)


# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8888"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
