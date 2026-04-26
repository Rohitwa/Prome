# ProMem

Your organisation memory that automates your work.

ProMem is a multi-user FastAPI app over Supabase Postgres, with Google
OAuth via Supabase Auth. Reads your local productivity-tracker SQLite,
classifies activities into Super-Contexts, links them to project
deliverables, and synthesizes per-project wiki pages. No embeddings,
no vector store, no GPU.

## Quick start (local dev)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 -m uvicorn promem_app:app --port 8888 --reload
```

Then open `http://localhost:8888/wiki` — you'll be redirected to
`/login` for Google sign-in via Supabase.

## Setup checklist (one-time)

1. **Supabase project** — Dashboard → New project → Mumbai (or closest).
2. **Schema** — paste `migrations/0002_postgres_init.sql` into Supabase
   SQL Editor → Run. Confirms 10 tables under public schema.
3. **Google OAuth** — enable Google provider in Supabase Auth, add the
   Supabase callback URL (`https://<project>.supabase.co/auth/v1/callback`)
   to Google Cloud OAuth client → Authorized redirect URIs.
4. **Whitelist** — add `http://localhost:8888/login` (and your prod URL)
   to Supabase Auth → URL Configuration → Redirect URLs.
5. **`.env`** — copy these from Supabase Dashboard. **Do not put `#` comments
   on the same line as a value** — the env loader treats `# foo` after an
   unquoted value as part of the value:
   ```bash
   PROMEM_DB_URL="postgresql://postgres:PASSWORD@db.PROJECT.supabase.co:5432/postgres"
   SUPABASE_URL="https://PROJECT.supabase.co"
   SUPABASE_ANON_KEY="eyJhbGciOi..."
   SUPABASE_JWT_SECRET="your-jwt-secret"
   OPENAI_API_KEY="sk-..."
   PROMEM_USER_ID="00000000-0000-0000-0000-000000000000"
   PROMEM_TRACKER_DB="/Users/you/.productivity-tracker/tracker.db"
   ```

   `PROMEM_USER_ID`: Supabase → Authentication → Users → click your user
   → "User UID" (a UUID). `OPENAI_API_KEY`: needed only for the nightly
   classify/synthesize phases. `PROMEM_TRACKER_DB`: optional — defaults
   to `${PROMEM_DATA_DIR}/tracker.db`.

## Running the pipeline

The nightly pipeline is local-first: it reads your local `tracker.db`
and writes per-user data to Supabase Postgres.

```bash
python3 promem_orchestrator.py whoami        # confirm user resolution
python3 promem_orchestrator.py check-key     # confirm env wiring
python3 promem_orchestrator.py run           # full sync → classify → match → synth
python3 promem_orchestrator.py status        # last_*_at timestamps
```

After a run, refresh `/wiki` in the browser — your SC cards fill in,
deliverable wikis populate.

## Layout

```
promem_app.py              FastAPI app + routes (/wiki, /projects, /productivity, /login, /auth/*)
promem_orchestrator.py     CLI driver for the nightly pipeline
promem_pipeline/
  ├── sync.py              Stage 2: tracker.db → work_pages
  ├── classify.py          Stage 3: LLM classifies into (sc_label, ctx_label)
  ├── filter.py            Stage 3.5: archive non-keep SCs
  ├── matcher.py           Stage 4: match work_pages to deliverables (5-layer scoring)
  └── synthesis.py         Stage 5: LLM synthesizes per-SC and per-deliverable wiki prose
db.py                      Postgres connection pool + user_id helper
auth.py                    Supabase JWT verification (ES256 via JWKS, HS256 fallback)
templates/                 Jinja2 templates (promem_*.html, all extend promem_base.html)
migrations/0002_*.sql      Postgres schema (multi-user with RLS)
```

## Environment variables

| Var | Required for | Purpose |
|---|---|---|
| `PROMEM_DB_URL` | app + pipeline | Supabase Postgres connection string |
| `SUPABASE_URL` | app (auth) | Project URL — used for JWKS endpoint + login page |
| `SUPABASE_ANON_KEY` | app (login page) | Public anon key, embedded in `/login` HTML |
| `SUPABASE_JWT_SECRET` | app (HS256 only) | Legacy projects on HS256; ES256 uses JWKS automatically |
| `PROMEM_USER_ID` | pipeline | UUID of the user whose data the pipeline writes |
| `PROMEM_TRACKER_DB` | pipeline | Path to local productivity-tracker SQLite |
| `PROMEM_DATA_DIR` | optional | Used as fallback for tracker.db lookup |
| `OPENAI_API_KEY` | pipeline | gpt-4o-mini via httpx for classify + synthesize |
| `PROMEM_SECURE_COOKIES` | optional | `true` in HTTPS production to enable Secure flag |

## License

Apache 2.0 — see [LICENSE](LICENSE).
