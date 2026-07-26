-- Migration: deleted_token_app_tenancy
--
-- 20260519090000_add_xct_app_tenancy added app_id + token_type to
-- LiteLLM_VerificationToken but not to the deletion archive table. Deleting a
-- key copies the token row into LiteLLM_DeletedVerificationToken, so every
-- delete failed once the proxy started sending those fields:
--
--   prisma.errors.FieldNotFoundError: Could not find field at
--   `createManyLiteLLM_DeletedVerificationToken.data.app_id`
--
-- Purely additive: two nullable columns + a partial index mirroring the live
-- table's.

ALTER TABLE "LiteLLM_DeletedVerificationToken"
  ADD COLUMN IF NOT EXISTS "app_id"     TEXT,
  ADD COLUMN IF NOT EXISTS "token_type" TEXT;

CREATE INDEX IF NOT EXISTS "LiteLLM_DeletedVerificationToken_app_id_idx"
  ON "LiteLLM_DeletedVerificationToken" ("app_id")
  WHERE "app_id" IS NOT NULL;
