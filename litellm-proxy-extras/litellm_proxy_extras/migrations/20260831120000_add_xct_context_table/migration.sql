-- Migration: add_xct_context_table
--
-- New table LiteLLM_XctContextTable — user-scoped context documents
-- (preferences, org knowledge, notes) shared across Xcity OS, xct-chat,
-- and xct-home through tokenhub. Served by /v1/xct-context.
-- The 256 KB content limit is enforced at the API layer, not the DB.

-- CreateTable
CREATE TABLE IF NOT EXISTS "LiteLLM_XctContextTable" (
    "context_id"   TEXT NOT NULL,
    "title"        TEXT NOT NULL,
    "content"      TEXT NOT NULL,
    "tags"         JSONB,
    "xct_metadata" JSONB DEFAULT '{}',
    "user_id"      TEXT,
    "team_id"      TEXT,
    "is_public"    BOOLEAN NOT NULL DEFAULT FALSE,
    "created_at"   TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "created_by"   TEXT,
    "updated_at"   TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_by"   TEXT,

    CONSTRAINT "LiteLLM_XctContextTable_pkey" PRIMARY KEY ("context_id")
);

-- CreateIndex
CREATE INDEX IF NOT EXISTS "LiteLLM_XctContextTable_user_id_idx"
    ON "LiteLLM_XctContextTable"("user_id");

-- CreateIndex
CREATE INDEX IF NOT EXISTS "LiteLLM_XctContextTable_team_id_idx"
    ON "LiteLLM_XctContextTable"("team_id");
