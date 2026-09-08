-- Delta migration (docs/architecture, master-prompt hardening pass):
--   1. DB-backed cookie/tracker lookup table (Build Plan Component 3, Layer 1) +
--      purpose taxonomy table — replaces the hardcoded Python catalog as the source
--      of truth, per "data, not hardcoded dicts."
--   2. Classification provenance on cookies/trackers: lookup | rule | llm_interpretation
--      | human_confirmed (master prompt §4) — findings' human involvement is already
--      fully captured via the `approvals` table, so provenance is scoped to
--      cookie/tracker classification only, not duplicated onto consent_findings.
--   3. `consent_states` on cookies/trackers: which of the three scan passes
--      (pre_consent, post_accept, post_reject) each item was observed in — an array,
--      not a single state, since the same cookie can appear in more than one pass.
-- Additive only, per this project's migration convention.
-- Idempotent (safe to re-run): every statement is guarded with IF NOT EXISTS /
-- ON CONFLICT / a pg_policies check — re-running an already-applied migration was
-- previously a hard error (empirically reproduced: `relation "cookie_lookup" already
-- exists`), which is exactly the wrong behavior for retried deploys and local resets.

-- ── Cookie/tracker lookup (seeded from the Open Cookie Database) ───────────────
create table if not exists cookie_lookup (
    id                  uuid primary key default gen_random_uuid(),
    name_pattern        text not null,        -- exact name, or a prefix when is_prefix_pattern
    is_prefix_pattern   boolean not null default false,  -- from the source's "Wildcard match" flag
    domain_pattern      text,                 -- optional domain substring to disambiguate
    vendor              text,
    category            text not null check (category in ('analytics','marketing','functional','other')),
    source              text not null default 'open_cookie_database',
    raw_metadata        jsonb not null default '{}',  -- whatever extra columns the source dataset has
    created_at          timestamptz not null default now()
);
create index if not exists cookie_lookup_name_pattern_idx on cookie_lookup (name_pattern);

-- ── Purpose taxonomy (fixed category list owned by compliance, not hardcoded) ──
create table if not exists purpose_taxonomy (
    code         text primary key,
    label        text not null,
    description  text,
    created_at   timestamptz not null default now()
);
insert into purpose_taxonomy (code, label, description) values
    ('analytics', 'Analytics', 'Usage measurement and analytics tracking'),
    ('marketing', 'Marketing', 'Advertising, retargeting and marketing attribution'),
    ('functional', 'Functional', 'Site functionality, security or user-requested features'),
    ('other', 'Other', 'Does not fit the categories above, or purpose could not be determined')
on conflict (code) do nothing;

-- ── Provenance + consent-state tracking ─────────────────────────────────────────
alter table cookies
    add column if not exists source text not null default 'rule'
        check (source in ('lookup','rule','llm_interpretation','human_confirmed')),
    add column if not exists consent_states text[] not null default '{}';

alter table trackers
    add column if not exists source text not null default 'rule'
        check (source in ('lookup','rule','llm_interpretation','human_confirmed')),
    add column if not exists consent_states text[] not null default '{}';

-- `priority` (master prompt §8) is distinct from `risk_level`: risk_level is how
-- serious the issue is, priority is how urgently it should be worked relative to the
-- other findings in the same report.
alter table consent_findings
    add column if not exists priority text not null default 'medium' check (priority in ('low','medium','high'));

alter table purpose_taxonomy enable row level security;
alter table cookie_lookup enable row level security;
-- Both tables are shared reference data (not org-scoped), same rationale as
-- knowledge_documents/knowledge_chunks in 0001_init.sql — no tenant_isolation policy
-- needed; readable by any authenticated role via a permissive policy.
-- (CREATE POLICY has no IF NOT EXISTS in Postgres — guarded via pg_policies instead.)
do $$
begin
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'purpose_taxonomy' and policyname = 'read_all') then
        create policy read_all on purpose_taxonomy for select using (true);
    end if;
    if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = 'cookie_lookup' and policyname = 'read_all') then
        create policy read_all on cookie_lookup for select using (true);
    end if;
end $$;
