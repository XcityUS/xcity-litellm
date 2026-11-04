ALTER TABLE "LiteLLM_AssetVerification"
    ADD COLUMN IF NOT EXISTS "voice_url" TEXT,
    ADD COLUMN IF NOT EXISTS "voice_key" TEXT,
    ADD COLUMN IF NOT EXISTS "voice_language" TEXT,
    ADD COLUMN IF NOT EXISTS "voice_duration_ms" INTEGER,
    ADD COLUMN IF NOT EXISTS "voice_consent_version" TEXT,
    ADD COLUMN IF NOT EXISTS "voice_consent_at" TIMESTAMPTZ(6),
    ADD COLUMN IF NOT EXISTS "voice_submissions" INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS "voice_submitted_at" TIMESTAMPTZ(6),
    ADD COLUMN IF NOT EXISTS "voice_imported_at" TIMESTAMPTZ(6);
