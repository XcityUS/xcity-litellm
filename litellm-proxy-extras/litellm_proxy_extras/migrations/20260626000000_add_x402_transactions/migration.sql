-- Migration: add_x402_transactions
--
-- Creates the LiteLLM_X402Transactions table to record every x402 payment
-- received by the proxy. Each row links to a LiteLLM_SpendLogs row via
-- request_id so that the LLM cost and the on-chain payment are co-traceable.
--
-- status values: pending | verified | settled | failed

CREATE TABLE IF NOT EXISTS "LiteLLM_X402Transactions" (
  "tx_id"               TEXT           PRIMARY KEY,
  "request_id"          TEXT,                           -- soft FK to LiteLLM_SpendLogs.request_id
  "caller_address"      TEXT           NOT NULL,        -- payer wallet address
  "model"               TEXT           NOT NULL,
  "chain"               TEXT           NOT NULL,        -- e.g. "base", "ethereum"
  "token"               TEXT           NOT NULL,        -- e.g. "USDC"
  "amount"              BIGINT         NOT NULL,        -- smallest token unit (USDC 6 decimals)
  "amount_usd"          NUMERIC(18,6)  NOT NULL,
  "tx_hash"             TEXT,                           -- on-chain hash, set after settlement
  "status"              TEXT           NOT NULL DEFAULT 'pending',
  "facilitator_url"     TEXT,
  "error_message"       TEXT,
  "retry_count"         INTEGER        NOT NULL DEFAULT 0,
  "settle_attempted_at" TIMESTAMP,
  "created_at"          TIMESTAMP      NOT NULL DEFAULT NOW(),
  "updated_at"          TIMESTAMP      NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS "x402_tx_created_at_idx"
  ON "LiteLLM_X402Transactions" ("created_at");

CREATE INDEX IF NOT EXISTS "x402_tx_status_created_at_idx"
  ON "LiteLLM_X402Transactions" ("status", "created_at");

CREATE INDEX IF NOT EXISTS "x402_tx_caller_created_at_idx"
  ON "LiteLLM_X402Transactions" ("caller_address", "created_at");

CREATE INDEX IF NOT EXISTS "x402_tx_model_created_at_idx"
  ON "LiteLLM_X402Transactions" ("model", "created_at");

CREATE INDEX IF NOT EXISTS "x402_tx_request_id_idx"
  ON "LiteLLM_X402Transactions" ("request_id")
  WHERE "request_id" IS NOT NULL;
