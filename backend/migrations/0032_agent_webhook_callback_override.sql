-- Per-run/per-scan override for the fixed external-partner webhook channel
-- (app/services/agent_webhook_service.py).
--
-- WHY THIS EXISTS
-- ----------------
-- AGENT_WEBHOOK_URL/AGENT_WEBHOOK_SECRET (added alongside agent_webhook_service)
-- assume ONE Consiva agent instance per environment. In practice this backend
-- is shared: this deployment's own CORS_ALLOWED_ORIGINS already answers for
-- both consiva.ai and uat.consiva.ai from the same process and the same
-- database, so a single fixed destination cannot be right for every run --
-- a UAT-started run must call uatapi.consiva.ai signed with UAT's own secret,
-- and a production-started run must call api.consiva.ai signed with
-- production's secret, never the other way round.
--
-- The caller that actually knows which environment started the run --
-- POST /sources/{id}/discover, POST /evidence, or POST /consent-agent/scans --
-- may now supply its own callback_url/callback_secret, which
-- agent_webhook_service.send_event prefers over the fixed env config when
-- present. The secret is Fernet-encrypted at rest under the SAME master key
-- ROPA source credentials already use (ROPA_CREDENTIAL_ENCRYPTION_KEY) --
-- see connectors/factory.py's encrypt_credential/decrypt_credential, now
-- public for this second use -- never stored in plaintext.
--
-- Additive only; idempotent (IF NOT EXISTS).

alter table ropa_discovery_runs add column if not exists agent_callback_url text;
alter table ropa_discovery_runs add column if not exists agent_callback_secret_ciphertext text;

alter table consent_scans add column if not exists agent_callback_url text;
alter table consent_scans add column if not exists agent_callback_secret_ciphertext text;
