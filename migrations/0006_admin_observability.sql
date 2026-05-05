-- Migration 0006 — admin observability: alerts + action audit log.
--
-- Backs the cloud-side watcher (5-min APScheduler job in promem_app.py)
-- and the per-user activity panels on /admin.
--
-- Apply via Supabase Dashboard → SQL Editor → paste + Run, or:
--   psql "$PROMEM_DB_URL" -f migrations/0006_admin_observability.sql
--
-- Idempotent: every CREATE uses IF NOT EXISTS; policies are DROP-then-CREATE.
-- Safe to re-run.

BEGIN;

-- ─── admin_alerts ────────────────────────────────────────────────────────
-- Watcher inserts a row when a user with recent activity goes silent, or
-- when their pipeline is stuck (segments uploaded but no work_pages).
-- Auto-resolved when the underlying condition clears.
CREATE TABLE IF NOT EXISTS admin_alerts (
    id              BIGSERIAL PRIMARY KEY,
    user_id         UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    alert_type      TEXT NOT NULL CHECK (alert_type IN ('silent', 'pipeline_stuck', 'agent_offline')),
    message         TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    acknowledged_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_admin_alerts_user
    ON admin_alerts(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_alerts_open
    ON admin_alerts(user_id) WHERE resolved_at IS NULL;


-- ─── admin_action_log ────────────────────────────────────────────────────
-- Audit trail for admin-triggered actions (force-resync, force-resync-all,
-- alert acknowledgements). Surfaces in the per-user activity panels.
CREATE TABLE IF NOT EXISTS admin_action_log (
    id            BIGSERIAL PRIMARY KEY,
    user_id       UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    action        TEXT NOT NULL,
    actor_user_id UUID REFERENCES auth.users(id) ON DELETE SET NULL,
    status        TEXT NOT NULL CHECK (status IN ('pending','running','success','failed')),
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    details_json  JSONB
);
CREATE INDEX IF NOT EXISTS idx_admin_action_log_user
    ON admin_action_log(user_id, started_at DESC);


-- ─── Row-Level Security ──────────────────────────────────────────────────
-- Users can read their own alerts and action history (so the agent or
-- self-service dashboards can surface them). The admin dashboard uses the
-- service_role connection which bypasses RLS for cross-user reads.
ALTER TABLE admin_alerts     ENABLE ROW LEVEL SECURITY;
ALTER TABLE admin_action_log ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS admin_alerts_self_read ON admin_alerts;
CREATE POLICY admin_alerts_self_read ON admin_alerts
    FOR SELECT USING (user_id = auth.uid());

DROP POLICY IF EXISTS admin_action_log_self_read ON admin_action_log;
CREATE POLICY admin_action_log_self_read ON admin_action_log
    FOR SELECT USING (user_id = auth.uid());

COMMIT;
