-- Migration: add_capability_dimensions_to_spend_logs (S2-10 + S6-01)
--
-- Spend log rows gain per-call capability-typed attribution:
--   - skill_ids       : array of skill IDs the call injected (S2-10)
--   - app_id          : the XCT app that initiated the call (S6-01)
--   - entity_type     : "model" | "agent" | "mcp" | "skill" (S6-01)
--   - entity_id       : the primary entity invoked
--   - entity_version  : version of the entity if applicable
--
-- All nullable / default-empty so existing rows are untouched.
-- Indexes targeted at the dashboard "by-app" + "by-entity" queries that
-- S6-02 / S6-03 will execute.

-- data-migration-ok: constant DEFAULT is catalog-only on Postgres 11+ (production runs 16), no row is rewritten
ALTER TABLE "LiteLLM_SpendLogs"
  ADD COLUMN IF NOT EXISTS "skill_ids"       TEXT[] NOT NULL DEFAULT '{}'::text[],
  ADD COLUMN IF NOT EXISTS "app_id"          TEXT,
  ADD COLUMN IF NOT EXISTS "entity_type"     TEXT,
  ADD COLUMN IF NOT EXISTS "entity_id"       TEXT,
  ADD COLUMN IF NOT EXISTS "entity_version"  TEXT;
