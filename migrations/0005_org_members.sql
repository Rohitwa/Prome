-- Migration 0005 — org hierarchy (admin / user) for the org admin dashboard.
--
-- Flat hierarchy: one fixed admin (seeded by email), all other auth.users are
-- 'user'. New OAuth signups auto-join via trigger on auth.users.
--
-- The admin dashboard at /admin reads across all user_ids using the
-- service_role / postgres connection (bypasses RLS); the org_members RLS
-- policy below only governs what individual users can see about themselves.
--
-- Apply via Supabase Dashboard → SQL Editor → paste + Run, or:
--   psql "$PROMEM_DB_URL" -f migrations/0005_org_members.sql
--
-- Idempotent: every CREATE uses IF NOT EXISTS; trigger + policy are
-- DROP-then-CREATE. Safe to re-run.

BEGIN;

-- ─── org_members ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS org_members (
    user_id    UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    email      TEXT,
    role       TEXT NOT NULL CHECK (role IN ('admin','user')),
    joined_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_org_members_role ON org_members(role);


-- ─── Auto-add new signups as 'user' ──────────────────────────────────────
-- Every future auth.users INSERT lands a matching 'user' row in org_members.
-- SECURITY DEFINER lets the trigger write into a different schema's table
-- (auth.users → public.org_members) regardless of the inserting role.
CREATE OR REPLACE FUNCTION add_user_to_org_members()
RETURNS TRIGGER
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    INSERT INTO org_members (user_id, email, role)
    VALUES (NEW.id, NEW.email, 'user')
    ON CONFLICT (user_id) DO NOTHING;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS on_auth_user_join_org ON auth.users;
CREATE TRIGGER on_auth_user_join_org
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION add_user_to_org_members();


-- ─── Backfill existing auth.users ────────────────────────────────────────
INSERT INTO org_members (user_id, email, role)
SELECT id, email, 'user' FROM auth.users
ON CONFLICT (user_id) DO NOTHING;


-- ─── Promote the fixed admin ─────────────────────────────────────────────
-- Replace this email if the admin identity ever changes.
UPDATE org_members
SET role = 'admin'
WHERE email = 'udit@mediamantra.net';


-- ─── Row-Level Security ──────────────────────────────────────────────────
-- Members can read their own row (used by the topbar / "am I admin?" check
-- on the client). Cross-user reads happen server-side with a connection
-- that bypasses RLS, so no admin-specific policy is needed here.
ALTER TABLE org_members ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS org_members_self_read ON org_members;
CREATE POLICY org_members_self_read ON org_members
    FOR SELECT USING (user_id = auth.uid());

COMMIT;
