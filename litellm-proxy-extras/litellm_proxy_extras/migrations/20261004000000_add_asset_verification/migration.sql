CREATE TABLE IF NOT EXISTS "LiteLLM_AssetVerification" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "owner_id" TEXT NOT NULL,
    "token_hash" TEXT UNIQUE,
    "inviter_name" TEXT NOT NULL DEFAULT '',
    "person_name" TEXT NOT NULL DEFAULT '',
    "callback_url" TEXT NOT NULL,
    "locale" TEXT NOT NULL DEFAULT 'en',
    "expires_at" TIMESTAMPTZ(6) NOT NULL,
    "created_at" TIMESTAMPTZ(6) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "consented_at" TIMESTAMPTZ(6),
    "consent_version" TEXT,
    "byted_token" TEXT UNIQUE,
    "h5_link" TEXT,
    "group_id" TEXT,
    "cancelled_at" TIMESTAMPTZ(6),
    "attempts" INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS "LiteLLM_AssetVerification_owner_id_created_at_idx"
    ON "LiteLLM_AssetVerification"("owner_id", "created_at");
