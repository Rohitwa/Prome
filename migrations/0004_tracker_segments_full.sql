-- Migration 0004 — full context_1 + context_2 mirror (Phase 4d).
--
-- Phase 4a's tracker_segments only mirrored the 10 columns sync.py reads.
-- The /productivity dashboard needs more context_1 fields (worker,
-- is_productive, frame counts, platform) AND the per-frame context_2 data
-- (keyboard/mouse activity flags, frame timestamps) to populate fully on
-- Windows installs. This migration adds those.
--
-- Apply via Supabase Dashboard → SQL Editor → paste + Run, or:
--   psql "$PROMEM_DB_URL" -f migrations/0004_tracker_segments_full.sql
--
-- Idempotent: ALTER ADD COLUMN IF NOT EXISTS + CREATE TABLE IF NOT EXISTS
-- + DROP-then-CREATE policy. Safe to re-run.

-- ─── 1. Extend tracker_segments with dashboard-relevant columns ──────────
-- Default values match the Mac tracker.db schema's defaults so a NULL value
-- on existing rows (uploaded under v0.1.x agents) renders as a sensible
-- "no data captured for this field" rather than crashing the dashboard.
ALTER TABLE tracker_segments
  ADD COLUMN IF NOT EXISTS worker             TEXT,
  ADD COLUMN IF NOT EXISTS is_productive      INTEGER DEFAULT -1,
  ADD COLUMN IF NOT EXISTS human_frame_count  INTEGER DEFAULT 0,
  ADD COLUMN IF NOT EXISTS ai_frame_count     INTEGER DEFAULT 0,
  ADD COLUMN IF NOT EXISTS platform           TEXT,
  ADD COLUMN IF NOT EXISTS medium             TEXT,
  ADD COLUMN IF NOT EXISTS full_text          TEXT,
  ADD COLUMN IF NOT EXISTS anchor             TEXT;


-- ─── 2. tracker_frames — per-screenshot mirror of tracker.db.context_2 ───
-- Each row = one captured frame. Joins to tracker_segments via
-- (user_id, target_segment_id). The frame-level keyboard/mouse activity
-- flags drive the dashboard's "input activity" widget.
CREATE TABLE IF NOT EXISTS tracker_frames (
  user_id               UUID    NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  id                    TEXT    NOT NULL,
  target_segment_id     TEXT    NOT NULL,
  target_frame_number   INTEGER,
  frame_timestamp       TEXT,
  raw_text              TEXT,
  detailed_summary      TEXT,
  worker_type           TEXT,
  has_keyboard_activity BOOLEAN DEFAULT FALSE,
  has_mouse_activity    BOOLEAN DEFAULT FALSE,
  uploaded_at           TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (user_id, id)
);

-- Dashboard joins: WHERE user_id = ? AND target_segment_id IN (...).
CREATE INDEX IF NOT EXISTS idx_tf_user_seg
  ON tracker_frames(user_id, target_segment_id);


-- ─── 3. Row-Level Security on tracker_frames ────────────────────────────
ALTER TABLE tracker_frames ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tracker_frames_user_isolation ON tracker_frames;
CREATE POLICY tracker_frames_user_isolation ON tracker_frames
  USING      (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());
