"""ProMem — multi-user FastAPI app.

Cloud-deployable: prome.db is now Supabase Postgres; tracker.db stays a
local SQLite (read-only) since it lives on the user's PC. Auth is via
Supabase JWT (Authorization: Bearer or promem_session cookie).

Routes:
  /                              redirect → /wiki
  /wiki                          per-user index of SC cards + archive
  /wiki/sc/<slug>                single SC wiki page
  /wiki/archive                  archived pages, day-grouped
  /projects                      tracker dashboard (Tree + Daily tabs)
  /projects/new                  CRUD form for project + deliverables
  /productivity                  4 widgets reading tracker.db (local file)

APIs (JSON):
  POST /api/projects                                {name, owner, description}
  POST /api/deliverables                            {project_id, title, ...}
  POST /api/deliverables/<id>/feedback              {date, verdict, note}
  POST /api/deliverables/<id>/match/<page_id>/pin
  POST /api/deliverables/<id>/match/<page_id>/unpin
  POST /api/orchestrator/run                        — disabled in cloud (Phase 4+)
  GET  /api/orchestrator/status                     — disabled in cloud (Phase 4+)
  POST /api/upload-segments                         {segments: [...]} (Phase 4a)
  GET  /agent/manifest                              auto-update manifest (Phase 4b.7, public)
  GET  /agent/dist/{filename}                       agent zip download (Phase 4b.7, public)

Run:
  PROMEM_DB_URL="postgresql://..."  \
  SUPABASE_JWT_SECRET="..."         \
  python3 promem_app.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import traceback
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import db
from auth import get_current_user, _verify as _verify_jwt

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("PROMEM_DATA_DIR", str(ROOT / "data")))
TRACKER = Path(os.environ.get("PROMEM_TRACKER_DB", str(DATA_DIR / "tracker.db")))
TEMPLATES = Jinja2Templates(directory=str(ROOT / "templates"))

# .env loading happens in db.py (imported above) so anything pulling in
# auth/db gets the env vars without duplicating the loader.


# ──────────────────────────────────────────────────────────────────────────────
# Cloud orchestrator scheduler (Phase 4c.5c)
# ──────────────────────────────────────────────────────────────────────────────
# In-app APScheduler that drives the multi-user orchestrator:
#   - Fast loop  (every 30 min): sync only — raw activity → work_pages
#   - Slow loop  (14:00 + 18:00 UTC daily): classify + filter + match + synthesis
#
# Requires fly.toml min_machines_running=1 (set in 4c.5c) so the scheduler
# actually ticks. Without that, Fly auto-stops the machine when idle and
# the scheduler hibernates with it.
_scheduler = BackgroundScheduler(timezone="UTC")


def _fast_loop_job() -> None:
    try:
        from promem_orchestrator_cloud import run_fast_loop
        run_fast_loop()
    except Exception:
        print("scheduler: fast_loop crashed:\n" + traceback.format_exc())


def _slow_loop_job() -> None:
    try:
        from promem_orchestrator_cloud import run_slow_loop
        run_slow_loop()
    except Exception:
        print("scheduler: slow_loop crashed:\n" + traceback.format_exc())


@asynccontextmanager
async def _lifespan(app: FastAPI):
    from datetime import timezone as _tz
    _scheduler.add_job(
        _fast_loop_job,
        IntervalTrigger(minutes=30),
        id="fast_loop",
        # Fire once immediately on startup so fresh deploys sync new data
        # right away (without waiting up to 30min for the first interval tick).
        next_run_time=datetime.now(_tz.utc),
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    _scheduler.add_job(
        _slow_loop_job,
        CronTrigger(hour="14,18", minute=0, timezone="UTC"),
        id="slow_loop",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    _scheduler.start()
    print("scheduler: started — fast every 30min (first fire: now), "
          "slow at 14:00+18:00 UTC", flush=True)
    try:
        yield
    finally:
        _scheduler.shutdown(wait=False)
        print("scheduler: shutdown", flush=True)


app = FastAPI(title="ProMem", lifespan=_lifespan)


# ──────────────────────────────────────────────────────────────────────────────
# DB helpers — prome.db via psycopg pool (db.conn), tracker.db via sqlite3
# ──────────────────────────────────────────────────────────────────────────────
def _tracker_conn() -> sqlite3.Connection:
    """Open tracker.db read-only. Lives on the user's local machine; cloud
    deploy just shows a 'not connected' page until Phase 4 sync exists."""
    c = sqlite3.connect(f"file:{TRACKER}?mode=ro", uri=True, timeout=30.0)
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


TEMPLATES.env.filters["md"] = _md_to_html


# ──────────────────────────────────────────────────────────────────────────────
# Auth routes — login page + cookie-mint endpoint + logout
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/wiki") -> HTMLResponse:
    """Public route — serves the Supabase JS client + Google sign-in button."""
    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_anon_key = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    if not supabase_url or not supabase_anon_key:
        return HTMLResponse(
            "<h1>Misconfigured</h1>"
            "<p>Server is missing <code>SUPABASE_URL</code> and/or "
            "<code>SUPABASE_ANON_KEY</code>. Add both to your .env "
            "(or fly secrets) and restart.</p>",
            status_code=500,
        )
    return TEMPLATES.TemplateResponse(request, "promem_login.html", {
        "supabase_url": supabase_url,
        "supabase_anon_key": supabase_anon_key,
        "next": next or "/wiki",
    })


class SessionIn(BaseModel):
    access_token: str


@app.post("/auth/session")
def auth_set_session(req: SessionIn) -> JSONResponse:
    """Validate the Supabase access token, mint an HttpOnly cookie.
    Called by the login page's JS after a successful Google OAuth round-trip."""
    try:
        payload = _verify_jwt(req.access_token)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid access token: {e}")
    user_id = payload["sub"]
    response = JSONResponse({"ok": True, "user_id": user_id})
    response.set_cookie(
        key="promem_session",
        value=req.access_token,
        httponly=True,
        secure=os.environ.get("PROMEM_SECURE_COOKIES", "false").lower() == "true",
        samesite="lax",
        max_age=3600,  # 1h, matches the Supabase access-token expiry
        path="/",
    )
    return response


@app.post("/auth/logout")
def auth_logout(user_id: str = Depends(get_current_user)) -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie("promem_session", path="/")
    return response


# ──────────────────────────────────────────────────────────────────────────────
# Routes — pages
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def root() -> RedirectResponse:
    return RedirectResponse(url="/wiki")


@app.get("/wiki", response_class=HTMLResponse)
def wiki_index(request: Request, user_id: str = Depends(get_current_user)) -> HTMLResponse:
    cards = []
    with db.conn() as c:
        kept = c.execute(
            "SELECT label, is_keep FROM sc_registry "
            "WHERE user_id=%s ORDER BY is_keep DESC, label",
            (user_id,),
        ).fetchall()
        for r in kept:
            if r["is_keep"] == 0:
                continue
            n_pages = c.execute(
                "SELECT COUNT(*) AS n FROM work_pages "
                "WHERE user_id=%s AND sc_label=%s AND COALESCE(is_archived,0)=0",
                (user_id, r["label"]),
            ).fetchone()["n"]
            cache = c.execute(
                "SELECT prose_json, generated_at FROM sc_wiki_cache "
                "WHERE user_id=%s AND sc_label=%s",
                (user_id, r["label"]),
            ).fetchone()
            prose = json.loads(cache["prose_json"]) if cache and cache["prose_json"] else None
            cards.append({
                "label": r["label"], "slug": _slug(r["label"]),
                "n_pages": n_pages,
                "tagline": (prose or {}).get("tagline") or "",
                "generated_at": cache["generated_at"] if cache else None,
            })
        n_arch = c.execute(
            "SELECT COUNT(*) AS n FROM work_pages "
            "WHERE user_id=%s AND COALESCE(is_archived,0)=1",
            (user_id,),
        ).fetchone()["n"]
        n_unfiled = c.execute(
            "SELECT COUNT(*) AS n FROM work_pages "
            "WHERE user_id=%s AND COALESCE(is_unfiled,0)=1",
            (user_id,),
        ).fetchone()["n"]
        n_pending_sc = c.execute(
            "SELECT COUNT(*) AS n FROM pending_sc "
            "WHERE user_id=%s AND status='pending'",
            (user_id,),
        ).fetchone()["n"]
        state_row = c.execute(
            "SELECT * FROM orchestrator_state WHERE user_id=%s",
            (user_id,),
        ).fetchone()

    return TEMPLATES.TemplateResponse(request, "promem_wiki_index.html", {
        "cards": cards, "n_archive": n_arch,
        "n_unfiled": n_unfiled, "n_pending_sc": n_pending_sc,
        "state": dict(state_row) if state_row else {},
    })


@app.get("/wiki/sc/{slug}", response_class=HTMLResponse)
def wiki_sc(slug: str, request: Request, user_id: str = Depends(get_current_user)) -> HTMLResponse:
    with db.conn() as c:
        sc_row = None
        for r in c.execute(
            "SELECT label FROM sc_registry WHERE user_id=%s AND is_keep=1",
            (user_id,),
        ).fetchall():
            if _slug(r["label"]) == slug:
                sc_row = r["label"]
                break
        if not sc_row:
            raise HTTPException(404, f"No keep SC matches slug '{slug}'")
        cache = c.execute(
            "SELECT prose_json, generated_at, source_page_count FROM sc_wiki_cache "
            "WHERE user_id=%s AND sc_label=%s",
            (user_id, sc_row),
        ).fetchone()
        prose = json.loads(cache["prose_json"]) if cache and cache["prose_json"] else {
            "tagline": "(no synthesis yet — run the synthesis phase)",
            "sections": [],
        }
        by_ctx = defaultdict(list)
        rows = c.execute(
            "SELECT id, title, summary, date_local, ctx_label, total_minutes "
            "FROM work_pages "
            "WHERE user_id=%s AND sc_label=%s AND COALESCE(is_archived,0)=0 "
            "ORDER BY date_local DESC LIMIT 200",
            (user_id, sc_row),
        ).fetchall()
        for r in rows:
            by_ctx[r["ctx_label"] or "—"].append(dict(r))
        n_pages = c.execute(
            "SELECT COUNT(*) AS n FROM work_pages "
            "WHERE user_id=%s AND sc_label=%s AND COALESCE(is_archived,0)=0",
            (user_id, sc_row),
        ).fetchone()["n"]

    return TEMPLATES.TemplateResponse(request, "promem_sc.html", {
        "label": sc_row, "slug": slug, "prose": prose,
        "by_ctx": dict(by_ctx), "n_pages": n_pages,
        "generated_at": cache["generated_at"] if cache else None,
        "source_count": cache["source_page_count"] if cache else 0,
    })


@app.get("/wiki/archive", response_class=HTMLResponse)
def wiki_archive(request: Request, user_id: str = Depends(get_current_user)) -> HTMLResponse:
    by_sc: dict[str, list[dict]] = defaultdict(list)
    with db.conn() as c:
        for r in c.execute(
            "SELECT id, title, summary, date_local, sc_label, ctx_label "
            "FROM work_pages "
            "WHERE user_id=%s AND COALESCE(is_archived,0)=1 "
            "ORDER BY date_local DESC LIMIT 500",
            (user_id,),
        ).fetchall():
            by_sc[r["sc_label"]].append(dict(r))
        n_total = c.execute(
            "SELECT COUNT(*) AS n FROM work_pages "
            "WHERE user_id=%s AND COALESCE(is_archived,0)=1",
            (user_id,),
        ).fetchone()["n"]
    return TEMPLATES.TemplateResponse(request, "promem_archive.html", {
        "by_sc": dict(by_sc), "n_total": n_total,
    })


@app.get("/projects", response_class=HTMLResponse)
def projects_view(request: Request, user_id: str = Depends(get_current_user)) -> HTMLResponse:
    with db.conn() as c:
        projects = [dict(r) for r in c.execute(
            "SELECT * FROM projects WHERE user_id=%s ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()]
        deliv_by_project: dict[str, list[dict]] = defaultdict(list)
        for r in c.execute(
            "SELECT * FROM deliverables WHERE user_id=%s",
            (user_id,),
        ).fetchall():
            d = dict(r)
            try:
                d["keywords_list"] = json.loads(d.get("keywords") or "[]")
            except Exception:
                d["keywords_list"] = []
            try:
                d["ctx_hints_list"] = json.loads(d.get("ctx_hints") or "[]")
            except Exception:
                d["ctx_hints_list"] = []
            d["matches"] = [dict(m) for m in c.execute("""
                SELECT dm.*, wp.title AS page_title, wp.summary AS page_summary,
                       wp.date_local, wp.ctx_label, wp.total_minutes,
                       wp.id AS page_id
                FROM deliverable_match dm
                JOIN work_pages wp ON wp.id = dm.page_id
                WHERE dm.user_id=%s AND dm.deliverable_id = %s
                ORDER BY wp.date_local DESC, dm.score DESC
            """, (user_id, d["id"])).fetchall()]
            dates = sorted({m["date_local"] for m in d["matches"] if m["date_local"]})
            d["stats"] = {
                "n_pages": len(d["matches"]),
                "minutes": round(sum(m["total_minutes"] or 0 for m in d["matches"]), 1),
                "days_active": len(dates),
                "last": dates[-1] if dates else None,
            }
            cache = c.execute(
                "SELECT prose_json FROM deliverable_wiki_cache "
                "WHERE user_id=%s AND deliverable_id=%s",
                (user_id, d["id"]),
            ).fetchone()
            d["wiki"] = json.loads(cache["prose_json"]) if cache and cache["prose_json"] else None
            by_date = defaultdict(list)
            for m in d["matches"]:
                by_date[m["date_local"]].append(m)
            d["by_date"] = dict(sorted(by_date.items(), reverse=True))
            deliv_by_project[d["project_id"]].append(d)
        fb_rows = [dict(r) for r in c.execute(
            "SELECT deliverable_id, date, verdict, note FROM deliverable_daily_feedback "
            "WHERE user_id=%s",
            (user_id,),
        ).fetchall()]
        fb_map: dict[str, dict] = {}
        for fb in fb_rows:
            fb_map[f"{fb['deliverable_id']}|{fb['date']}"] = fb
        n_pages_total = c.execute(
            "SELECT COUNT(*) AS n FROM work_pages WHERE user_id=%s",
            (user_id,),
        ).fetchone()["n"]
    return TEMPLATES.TemplateResponse(request, "promem_projects.html", {
        "projects": projects,
        "deliv_by_project": dict(deliv_by_project),
        "n_pages_total": n_pages_total,
        "fb_map": fb_map,
    })


@app.get("/projects/new", response_class=HTMLResponse)
def projects_new(request: Request, user_id: str = Depends(get_current_user)) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "promem_project_new.html", {})


@app.get("/productivity", response_class=HTMLResponse)
def productivity(
    request: Request,
    date: str | None = None,
    user_id: str = Depends(get_current_user),
) -> HTMLResponse:
    from datetime import date as _d, timedelta as _td, datetime as _dt
    if not TRACKER.exists():
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
or mount one to that path. Cloud sync of tracker data is a Phase 4 deliverable.</div>
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

    tconn = _tracker_conn()
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

    coverage = []
    for i in range(29, -1, -1):
        d = (sel_dt - _td(days=i)).strftime("%Y-%m-%d")
        secs = tconn.execute(
            "SELECT COALESCE(SUM(target_segment_length_secs),0) FROM context_1 "
            "WHERE date(timestamp_start) = ?", (d,),
        ).fetchone()[0] or 0
        if secs == 0:
            level = 0
        elif secs < 1800:
            level = 1
        elif secs < 5400:
            level = 2
        elif secs < 14400:
            level = 3
        else:
            level = 4
        coverage.append({"date": d, "level": level, "min": round(secs / 60)})

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

    hour_counts = [0] * 24
    for r in tconn.execute("""
        SELECT CAST(strftime('%H', timestamp_start) AS INTEGER) AS hr,
               SUM(target_segment_length_secs) AS secs
        FROM context_1 WHERE date(timestamp_start) = ? GROUP BY hr
    """, (sel_date,)).fetchall():
        hr = r["hr"] if r["hr"] is not None else 0
        hour_counts[hr] = int((r["secs"] or 0) // 60)

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

    with db.conn() as c:
        by_project = [dict(r) for r in c.execute("""
            SELECT p.id, p.name,
                   COUNT(DISTINCT wp.id) AS pages,
                   ROUND(SUM(wp.total_minutes)::numeric, 1) AS mins
            FROM projects p
            JOIN deliverables d ON d.project_id = p.id AND d.user_id = p.user_id
            JOIN deliverable_match dm ON dm.deliverable_id = d.id AND dm.user_id = d.user_id
            JOIN work_pages wp ON wp.id = dm.page_id AND wp.user_id = dm.user_id
            WHERE p.user_id=%s AND wp.date_local = %s
            GROUP BY p.id, p.name
            ORDER BY mins DESC
        """, (user_id, sel_date)).fetchall()]
        top_ctx = [dict(r) for r in c.execute("""
            SELECT ctx_label, COUNT(*) AS n
            FROM work_pages
            WHERE user_id=%s AND date_local = %s AND ctx_label != ''
            GROUP BY ctx_label ORDER BY n DESC LIMIT 8
        """, (user_id, sel_date)).fetchall()]

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


class TrackerSegmentIn(BaseModel):
    id: str
    target_segment_id: Optional[str] = None
    timestamp_start: str
    timestamp_end: Optional[str] = None
    target_segment_length_secs: int = 0
    short_title: Optional[str] = None
    window_name: Optional[str] = None
    detailed_summary: Optional[str] = None
    supercontext: Optional[str] = None
    context: Optional[str] = None


class UploadSegmentsIn(BaseModel):
    segments: list[TrackerSegmentIn]


@app.post("/api/projects")
def api_create_project(p: ProjectIn, user_id: str = Depends(get_current_user)) -> dict:
    pid = "P-" + secrets.token_hex(4)
    with db.conn() as c:
        c.execute(
            "INSERT INTO projects (id, user_id, name, owner, description, status) "
            "VALUES (%s, %s, %s, %s, %s, 'active')",
            (pid, user_id, p.name, p.owner, p.description),
        )
    return {"ok": True, "id": pid}


@app.post("/api/deliverables")
def api_create_deliverable(d: DeliverableIn, user_id: str = Depends(get_current_user)) -> dict:
    did = "D-" + secrets.token_hex(4)
    with db.conn() as c:
        c.execute(
            "INSERT INTO deliverables (id, user_id, project_id, title, description, "
            "keywords, ctx_hints, status) VALUES (%s, %s, %s, %s, %s, %s, %s, 'in progress')",
            (did, user_id, d.project_id, d.title, d.description,
             json.dumps(d.keywords), json.dumps(d.ctx_hints)),
        )
    return {"ok": True, "id": did}


@app.post("/api/deliverables/{did}/feedback")
def api_feedback(did: str, fb: FeedbackIn, user_id: str = Depends(get_current_user)) -> dict:
    if fb.verdict not in ("correct", "wrong"):
        raise HTTPException(400, "verdict must be 'correct' or 'wrong'")
    with db.conn() as c:
        c.execute(
            "DELETE FROM deliverable_daily_feedback "
            "WHERE user_id=%s AND deliverable_id=%s AND date=%s",
            (user_id, did, fb.date),
        )
        c.execute(
            "INSERT INTO deliverable_daily_feedback (user_id, deliverable_id, date, verdict, note) "
            "VALUES (%s, %s, %s, %s, %s)",
            (user_id, did, fb.date, fb.verdict, fb.note),
        )
    return {"ok": True}


@app.post("/api/deliverables/{did}/match/{pid}/{action}")
def api_pin_unpin(
    did: str, pid: str, action: str,
    user_id: str = Depends(get_current_user),
) -> dict:
    if action not in ("pin", "unpin"):
        raise HTTPException(400, "action must be 'pin' or 'unpin'")
    with db.conn() as c:
        if action == "pin":
            c.execute("""
                INSERT INTO deliverable_match (user_id, deliverable_id, page_id, score, source, matched_at)
                VALUES (%s, %s, %s, 1.0, 'pin', now()::text)
                ON CONFLICT (user_id, deliverable_id, page_id) DO UPDATE SET source='pin'
            """, (user_id, did, pid))
        else:
            c.execute(
                "DELETE FROM deliverable_match "
                "WHERE user_id=%s AND deliverable_id=%s AND page_id=%s",
                (user_id, did, pid),
            )
    return {"ok": True, "action": action}


@app.post("/api/orchestrator/run")
def api_orch_run(user_id: str = Depends(get_current_user)) -> dict:
    # The nightly pipeline (sync/classify/match/synthesize) is still SQLite-bound.
    # Cloud-side run requires Phase 4 (tracker.db sync) + a Postgres rewrite of
    # promem_pipeline. Until then, the pipeline runs locally on the user's
    # machine and writes directly to Supabase Postgres.
    raise HTTPException(
        status_code=503,
        detail="Cloud-side pipeline runs are not yet supported. Run "
               "`python3 promem_orchestrator.py run` locally — it writes to "
               "Supabase via PROMEM_DB_URL.",
    )


@app.get("/api/orchestrator/status")
def api_orch_status(user_id: str = Depends(get_current_user)) -> dict:
    with db.conn() as c:
        state = c.execute(
            "SELECT * FROM orchestrator_state WHERE user_id=%s",
            (user_id,),
        ).fetchone()
    return {**(dict(state) if state else {}), "should_run_now": False,
            "reason": "cloud-side runs disabled (Phase 4+)"}


# Per-request cap. Tracker.db typically yields hundreds of new segments per
# day; 1000 leaves comfortable headroom and surfaces agent bugs as 413s
# instead of silently truncating.
UPLOAD_SEGMENTS_MAX = 1000


@app.post("/api/upload-segments")
def api_upload_segments(
    payload: UploadSegmentsIn,
    user_id: str = Depends(get_current_user),
) -> dict:
    n_received = len(payload.segments)
    if n_received == 0:
        return {"ok": True, "n_received": 0, "n_inserted": 0}
    if n_received > UPLOAD_SEGMENTS_MAX:
        raise HTTPException(
            status_code=413,
            detail=f"max {UPLOAD_SEGMENTS_MAX} segments per request "
                   f"(received {n_received}); chunk client-side",
        )
    rows = [
        (user_id, s.id, s.target_segment_id, s.timestamp_start, s.timestamp_end,
         s.target_segment_length_secs, s.short_title, s.window_name,
         s.detailed_summary, s.supercontext, s.context)
        for s in payload.segments
    ]
    with db.conn() as c:
        cur = c.cursor()
        cur.executemany(
            "INSERT INTO tracker_segments "
            "(user_id, id, target_segment_id, timestamp_start, timestamp_end, "
            " target_segment_length_secs, short_title, window_name, "
            " detailed_summary, supercontext, context) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (user_id, id) DO NOTHING",
            rows,
        )
        n_inserted = cur.rowcount
    return {"ok": True, "n_received": n_received, "n_inserted": n_inserted}


# ──────────────────────────────────────────────────────────────────────────────
# Agent auto-update endpoints (Phase 4b.7) — public, no auth.
# Manifest is read by every agent ~once per hour; zips are downloaded once per
# release. Both live on the Fly persistent volume at /data, populated by the
# Mac-side release.sh via `flyctl ssh sftp`.
# ──────────────────────────────────────────────────────────────────────────────
_AGENT_DATA_DIR = Path("/data")
_AGENT_MANIFEST = _AGENT_DATA_DIR / "agent_manifest.json"
_AGENT_ZIP_RE = re.compile(r"^promem_agent-[0-9.]+\.zip$")


@app.get("/agent/manifest")
def get_agent_manifest() -> dict:
    """Return the latest agent release manifest. Returns a placeholder shape
    if no release has been published yet (auto-updater treats this as 'no
    update needed' since the placeholder version is 0.0.0)."""
    if not _AGENT_MANIFEST.exists():
        return {
            "latest": "0.0.0",
            "url": "",
            "sha256": "",
            "min_compat_version": "0.0.0",
            "released_at": "",
            "note": "no release published yet",
        }
    try:
        return json.loads(_AGENT_MANIFEST.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise HTTPException(500, f"manifest unreadable: {e}")


@app.get("/agent/dist/{filename}")
def get_agent_dist(filename: str) -> FileResponse:
    """Serve a released agent zip from /data. Filename whitelisted against
    a strict pattern to prevent path traversal."""
    if not _AGENT_ZIP_RE.match(filename):
        raise HTTPException(404, f"not found: {filename}")
    p = _AGENT_DATA_DIR / filename
    if not p.exists() or not p.is_file():
        raise HTTPException(404, f"not found: {filename}")
    return FileResponse(str(p), media_type="application/zip", filename=filename)


# ──────────────────────────────────────────────────────────────────────────────
# Form handlers (HTML form posts → /projects/new)
# ──────────────────────────────────────────────────────────────────────────────
@app.post("/projects/new")
async def form_create_project_with_deliverables(
    request: Request,
    user_id: str = Depends(get_current_user),
) -> RedirectResponse:
    form = await request.form()
    name = (form.get("name") or "").strip()
    owner = (form.get("owner") or "").strip()
    description = (form.get("description") or "").strip()
    if not name:
        return RedirectResponse(url="/projects/new?err=name", status_code=303)
    pid = "P-" + secrets.token_hex(4)
    with db.conn() as c:
        c.execute(
            "INSERT INTO projects (id, user_id, name, owner, description, status) "
            "VALUES (%s, %s, %s, %s, %s, 'active')",
            (pid, user_id, name, owner, description),
        )
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
            c.execute(
                "INSERT INTO deliverables (id, user_id, project_id, title, description, "
                "keywords, ctx_hints, status) VALUES (%s, %s, %s, %s, %s, %s, '[]', 'in progress')",
                (did, user_id, pid, t, d_desc, json.dumps(kws)),
            )
    return RedirectResponse(url="/projects", status_code=303)


# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8888"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
