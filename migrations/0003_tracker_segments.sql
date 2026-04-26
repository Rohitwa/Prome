-- Migration 0003 — tracker_segments: server-side mirror of tracker.db.context_1.
--
-- Phase 4a — cloud push API. Lets the Windows agent (Phase 4b) POST raw
-- tracker segments up to ProMem, so the pipeline's sync.py can read from
-- Postgres instead of the user's local SQLite tracker.db.
--
-- Apply via Supabase Dashboard → SQL Editor → paste + Run, or:
--   psql "$PROMEM_DB_URL" -f migrations/0003_tracker_segments.sql
--
-- Idempotent: every CREATE uses IF NOT EXISTS; the policy is DROP-then-CREATE.
-- Safe to re-run.

-- ─── tracker_segments ────────────────────────────────────────────────────
-- Mirrors the exact 10 columns sync.py reads from tracker.db.context_1, plus
-- user_id (tenancy) and uploaded_at (server-side audit). Composite PK on
-- (user_id, id) gives true tenant isolation: two users' tracker.dbs can
-- legitimately produce colliding local ids without one losing rows on upload.
CREATE TABLE IF NOT EXISTS tracker_segments (
  user_id                    UUID    NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  id                         TEXT    NOT NULL,
  target_segment_id          TEXT,
  timestamp_start            TEXT    NOT NULL,
  timestamp_end              TEXT,
  target_segment_length_secs INTEGER DEFAULT 0,
  short_title                TEXT,
  window_name                TEXT,
  detailed_summary           TEXT,
  supercontext               TEXT,
  context                    TEXT,
  uploaded_at                TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (user_id, id)
);

-- Pipeline reads: WHERE user_id = ? AND timestamp_start > ? ORDER BY timestamp_start.
CREATE INDEX IF NOT EXISTS idx_ts_user_start
  ON tracker_segments(user_id, timestamp_start);


-- ─── Row-Level Security ──────────────────────────────────────────────────
-- Same pattern as 0002: enforce tenant isolation at the row level so any
-- query that forgets WHERE user_id=... still can't leak across users.
ALTER TABLE tracker_segments ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tracker_segments_user_isolation ON tracker_segments;
CREATE POLICY tracker_segments_user_isolation ON tracker_segments
  USING      (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());
