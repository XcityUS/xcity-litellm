ALTER TABLE "LiteLLM_AssetVerification"
    ADD COLUMN IF NOT EXISTS "voice_upload_id" TEXT,
    ADD COLUMN IF NOT EXISTS "voice_upload_expires_at" TIMESTAMPTZ(6);
