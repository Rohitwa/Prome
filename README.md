# ProMem

Your organisation memory that automates your work.

ProMem is the cloud-deployable memory + project-tracking engine extracted from
PMIS / ProMe — a focused FastAPI app over a single SQLite (Postgres in cloud),
no embeddings, no vector store, no GPU. Reads productivity-tracker context,
classifies it, links it to projects, and synthesizes per-project wikis.

## Quick start (Docker)

```bash
docker build -t promem:dev .
docker run --rm -p 8888:8888 -v "$(pwd)/data:/data" promem:dev
```

Then open:
- `http://localhost:8888/wiki` — per-project synthesized wikis
- `http://localhost:8888/projects` — project tracker (Tree + Daily)
- `http://localhost:8888/productivity` — 4 productivity widgets

The container creates `data/prome.db` on first launch (idempotent migration).
Tracker data is read from `${PROMEM_TRACKER_DB:-/data/tracker.db}` if present;
ProMem still serves wiki + projects routes when no tracker file exists.

## Local dev (Python directly)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 -m uvicorn promem_app:app --port 8888 --reload
```

## Layout

```
promem_app.py              FastAPI app (routes for /wiki, /projects, /productivity)
promem_orchestrator.py     CLI driver for the nightly pipeline (sync → classify → match → synthesize)
promem_pipeline/           Pipeline stages — each stage is a small stdlib module
templates/                 Jinja2 templates (promem_*.html, all extend promem_base.html)
migrations/                SQL migrations applied on first boot
```

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `PROMEM_DATA_DIR` | `./data` | Where `prome.db` (and `tracker.db` fallback) live |
| `PROMEM_TRACKER_DB` | `${PROMEM_DATA_DIR}/tracker.db` | Read-only path to productivity-tracker SQLite |
| `OPENAI_API_KEY` | unset | Required only for the nightly classify/synthesize phases |

## License

Apache 2.0 — see [LICENSE](LICENSE).
