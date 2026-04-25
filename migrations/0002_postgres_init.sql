-- Migration 0002 — Postgres-flavor schema for ProMem multi-user cloud deploy.
--
-- Apply via Supabase Dashboard → SQL Editor → paste + Run, or:
--   psql "$PROMEM_DB_URL" -f migrations/0002_postgres_init.sql
--
-- Idempotent: every CREATE uses IF NOT EXISTS; every policy is DROP-then-CREATE.
-- Safe to re-run.

-- ─── orchestrator_state ──────────────────────────────────────────────────
-- Was a singleton (id=1) in SQLite; now one row per user.
CREATE TABLE IF NOT EXISTS orchestrator_state (
  user_id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  last_sync_at TEXT,
  last_classify_at TEXT,
  last_match_at TEXT,
  last_synthesis_at TEXT,
  next_due TEXT,
  last_error TEXT
);

-- ─── work_pages ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS work_pages (
  id TEXT PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  title TEXT,
  summary TEXT,
  date_local TEXT NOT NULL,
  sc_label TEXT DEFAULT '',
  ctx_label TEXT DEFAULT '',
  is_archived INTEGER DEFAULT 0,
  is_unfiled INTEGER DEFAULT 0,
  classified_at TEXT,
  source_segment_count INTEGER DEFAULT 1,
  total_minutes DOUBLE PRECISION DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_wp_user_date ON work_pages(user_id, date_local);
CREATE INDEX IF NOT EXISTS idx_wp_user_sc   ON work_pages(user_id, sc_label);

-- ─── sc_registry ─────────────────────────────────────────────────────────
-- Composite PK so each user gets their own SC list and is_keep flags.
CREATE TABLE IF NOT EXISTS sc_registry (
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  label TEXT NOT NULL,
  is_keep INTEGER NOT NULL,
  created_at TEXT DEFAULT (now()::text),
  PRIMARY KEY (user_id, label)
);

-- Helper: seed the 8 default SCs for a freshly-signed-up user.
-- App calls this once on first activity per user (idempotent via ON CONFLICT).
CREATE OR REPLACE FUNCTION seed_sc_registry_for_user(p_user_id UUID)
RETURNS void AS $$
BEGIN
  INSERT INTO sc_registry (user_id, label, is_keep) VALUES
    (p_user_id, 'Building Product',       1),
    (p_user_id, 'Sales & GTM',            1),
    (p_user_id, 'Core Product Tech',      1),
    (p_user_id, 'Research Reading',       1),
    (p_user_id, 'Personal & distraction', 0),
    (p_user_id, 'Side projects',          0),
    (p_user_id, 'Career & job hunt',      0),
    (p_user_id, 'Residual',               0)
  ON CONFLICT (user_id, label) DO NOTHING;
END;
$$ LANGUAGE plpgsql;

-- ─── pending_sc ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pending_sc (
  id BIGSERIAL PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  proposed_label TEXT NOT NULL,
  why TEXT,
  example_page_ids TEXT DEFAULT '[]',
  proposal_count INTEGER DEFAULT 1,
  first_seen TEXT DEFAULT (now()::text),
  last_seen  TEXT DEFAULT (now()::text),
  status TEXT CHECK(status IN ('pending','approved','rejected')) DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_pending_sc_user ON pending_sc(user_id, status);

-- ─── sc_wiki_cache ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sc_wiki_cache (
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  sc_label TEXT NOT NULL,
  prose_json TEXT,
  source_page_count INTEGER,
  generated_at TEXT,
  PRIMARY KEY (user_id, sc_label)
);

-- ─── projects ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  owner TEXT DEFAULT '',
  description TEXT DEFAULT '',
  status TEXT DEFAULT 'active',
  created_at TEXT DEFAULT (now()::text)
);
CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id);

-- ─── deliverables ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS deliverables (
  id TEXT PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  description TEXT DEFAULT '',
  keywords TEXT DEFAULT '[]',
  ctx_hints TEXT DEFAULT '[]',
  status TEXT DEFAULT 'in progress',
  created_at TEXT DEFAULT (now()::text)
);
CREATE INDEX IF NOT EXISTS idx_deliv_user_project ON deliverables(user_id, project_id);

-- ─── deliverable_match ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS deliverable_match (
  id BIGSERIAL PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  deliverable_id TEXT NOT NULL REFERENCES deliverables(id) ON DELETE CASCADE,
  page_id TEXT NOT NULL REFERENCES work_pages(id) ON DELETE CASCADE,
  score DOUBLE PRECISION,
  reasons TEXT,
  source TEXT CHECK(source IN ('auto','pin','unpin')) DEFAULT 'auto',
  matched_at TEXT DEFAULT (now()::text),
  UNIQUE(user_id, deliverable_id, page_id)
);
CREATE INDEX IF NOT EXISTS idx_dm_user_deliv ON deliverable_match(user_id, deliverable_id);

-- ─── deliverable_daily_feedback ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS deliverable_daily_feedback (
  id BIGSERIAL PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  deliverable_id TEXT NOT NULL REFERENCES deliverables(id) ON DELETE CASCADE,
  date TEXT NOT NULL,
  verdict TEXT CHECK(verdict IN ('correct','wrong')),
  note TEXT DEFAULT '',
  created_at TEXT DEFAULT (now()::text)
);
CREATE INDEX IF NOT EXISTS idx_ddf_user_deliv ON deliverable_daily_feedback(user_id, deliverable_id);

-- ─── deliverable_wiki_cache ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS deliverable_wiki_cache (
  deliverable_id TEXT PRIMARY KEY REFERENCES deliverables(id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  prose_json TEXT,
  source_page_count INTEGER,
  generated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_dwc_user ON deliverable_wiki_cache(user_id);


-- ─── Row-Level Security ──────────────────────────────────────────────────
-- Defense-in-depth: even if a query forgets WHERE user_id=...,
-- Postgres enforces tenant isolation at the row level.
-- Bypassed when the connection authenticates with the service_role key
-- (server-side admin ops); enforced when authenticated with a user JWT.

ALTER TABLE orchestrator_state           ENABLE ROW LEVEL SECURITY;
ALTER TABLE work_pages                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE sc_registry                  ENABLE ROW LEVEL SECURITY;
ALTER TABLE pending_sc                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE sc_wiki_cache                ENABLE ROW LEVEL SECURITY;
ALTER TABLE projects                     ENABLE ROW LEVEL SECURITY;
ALTER TABLE deliverables                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE deliverable_match            ENABLE ROW LEVEL SECURITY;
ALTER TABLE deliverable_daily_feedback   ENABLE ROW LEVEL SECURITY;
ALTER TABLE deliverable_wiki_cache       ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE
  tbl TEXT;
BEGIN
  FOREACH tbl IN ARRAY ARRAY[
    'orchestrator_state', 'work_pages', 'sc_registry', 'pending_sc',
    'sc_wiki_cache', 'projects', 'deliverables', 'deliverable_match',
    'deliverable_daily_feedback', 'deliverable_wiki_cache'
  ] LOOP
    EXECUTE format('DROP POLICY IF EXISTS %I_user_isolation ON %I', tbl, tbl);
    EXECUTE format(
      'CREATE POLICY %I_user_isolation ON %I '
      'USING (user_id = auth.uid()) '
      'WITH CHECK (user_id = auth.uid())',
      tbl, tbl
    );
  END LOOP;
END $$;

-- Authenticated users can call seed function (RLS already restricts what it touches).
GRANT EXECUTE ON FUNCTION seed_sc_registry_for_user(UUID) TO authenticated;
