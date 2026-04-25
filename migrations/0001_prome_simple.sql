-- Migration 0001 — initial schema for the simplified ProMe engine.
--
-- Single new SQLite at: pmis_v2/data/prome.db
-- Tracker.db and memory.db are untouched.
--
-- Apply with:
--   sqlite3 pmis_v2/data/prome.db < pmis_v2/migrations/0001_prome_simple.sql

CREATE TABLE IF NOT EXISTS orchestrator_state (
  id INTEGER PRIMARY KEY CHECK(id = 1),
  last_sync_at TEXT,
  last_classify_at TEXT,
  last_match_at TEXT,
  last_synthesis_at TEXT,
  next_due TEXT,
  last_error TEXT
);
INSERT OR IGNORE INTO orchestrator_state (id) VALUES (1);

CREATE TABLE IF NOT EXISTS work_pages (
  id TEXT PRIMARY KEY,
  title TEXT,
  summary TEXT,
  date_local TEXT NOT NULL,
  sc_label TEXT DEFAULT '',
  ctx_label TEXT DEFAULT '',
  is_archived INTEGER DEFAULT 0,
  is_unfiled INTEGER DEFAULT 0,
  classified_at TEXT,
  source_segment_count INTEGER DEFAULT 1,
  total_minutes REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_wp_date ON work_pages(date_local);
CREATE INDEX IF NOT EXISTS idx_wp_sc   ON work_pages(sc_label);

CREATE TABLE IF NOT EXISTS sc_registry (
  label TEXT PRIMARY KEY,
  is_keep INTEGER NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO sc_registry (label, is_keep) VALUES
  ('Building Product',       1),
  ('Sales & GTM',            1),
  ('Core Product Tech',      1),
  ('Research Reading',       1),
  ('Personal & distraction', 0),
  ('Side projects',          0),
  ('Career & job hunt',      0),
  ('Residual',               0);

CREATE TABLE IF NOT EXISTS pending_sc (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  proposed_label TEXT NOT NULL,
  why TEXT,
  example_page_ids TEXT DEFAULT '[]',
  proposal_count INTEGER DEFAULT 1,
  first_seen TEXT DEFAULT (datetime('now')),
  last_seen  TEXT DEFAULT (datetime('now')),
  status TEXT CHECK(status IN ('pending','approved','rejected')) DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS sc_wiki_cache (
  sc_label TEXT PRIMARY KEY,
  prose_json TEXT,
  source_page_count INTEGER,
  generated_at TEXT
);

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  owner TEXT DEFAULT '',
  description TEXT DEFAULT '',
  status TEXT DEFAULT 'active',
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS deliverables (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT DEFAULT '',
  keywords TEXT DEFAULT '[]',
  ctx_hints TEXT DEFAULT '[]',
  status TEXT DEFAULT 'in progress',
  created_at TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS deliverable_match (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  deliverable_id TEXT NOT NULL,
  page_id TEXT NOT NULL,
  score REAL,
  reasons TEXT,
  source TEXT CHECK(source IN ('auto','pin','unpin')) DEFAULT 'auto',
  matched_at TEXT DEFAULT (datetime('now')),
  UNIQUE(deliverable_id, page_id)
);
CREATE INDEX IF NOT EXISTS idx_dm_deliv ON deliverable_match(deliverable_id);

CREATE TABLE IF NOT EXISTS deliverable_daily_feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  deliverable_id TEXT NOT NULL,
  date TEXT NOT NULL,
  verdict TEXT CHECK(verdict IN ('correct','wrong')),
  note TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS deliverable_wiki_cache (
  deliverable_id TEXT PRIMARY KEY,
  prose_json TEXT,
  source_page_count INTEGER,
  generated_at TEXT
);
