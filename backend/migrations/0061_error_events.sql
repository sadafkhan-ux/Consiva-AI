-- 0061: centralised error events for BOTH scanning agents and the platform around them.
--
-- One row per distinct ongoing problem (a "fingerprint": the agent, the module, the service, the category, the HTTP status, and for a website block the website). A failure that repeats is the same row with a
-- growing `occurrences`, not a stream of rows; when it stops (a call to the same service succeeds again) the row is resolved and one "recovered" email is sent. Operational data about the DEPLOYMENT, like
-- system_alerts (0053): no org_id, no row-level security, readable only by administrators through the API. Text is scrubbed of keys, tokens and passwords before it is stored.
-- Additive: one new table.

create table if not exists error_events (
    id               uuid primary key default gen_random_uuid(),
    error_id         text not null unique,                    -- the short id shown to a person and in the email, e.g. ERR-20261116-3F9A2C
    fingerprint      text not null,
    agent            text not null check (agent in ('enterprise', 'five_page', 'platform')),
    module           text not null,
    service          text,                                     -- the service that failed: claude_api, nvidia_api, embedding_api, postgres, vector_db, policy_kb, crawler, browser, worker, website, api ...
    grouping         text not null,                            -- api, rag, database, llm, embedding, crawler, worker, website_access, config
    category         text not null,                            -- the specific kind: bad_gateway, rate_limited, api_key_invalid, bot_blocked, connection_failed ...
    severity         text not null check (severity in ('info', 'warning', 'critical')),
    http_status      integer,
    message          text not null,
    detail           text,
    scan_id          text,
    website_url      text,
    request_id       text,
    temporary        boolean not null default true,            -- may clear by itself; false = an administrator has to act
    suggested_steps  text,
    status           text not null default 'open' check (status in ('open', 'acknowledged', 'resolved')),
    occurrences      integer not null default 1 check (occurrences >= 1),
    first_seen_at    timestamptz not null default now(),
    last_seen_at     timestamptz not null default now(),
    resolved_at      timestamptz,
    last_recovery_at timestamptz,
    acknowledged_by  text,
    acknowledged_at  timestamptz,
    notify_status    text,                                     -- sent, not_configured, failed: <short reason>, not_needed. Never "sent" unless the mail server accepted it.
    notify_count     integer not null default 0 check (notify_count >= 0),
    last_notified_at timestamptz,
    constraint error_events_resolved_has_time check (status <> 'resolved' or resolved_at is not null)
);
-- one live (open or acknowledged) event per fingerprint: the deduplication the code relies on
create unique index if not exists error_events_one_live_per_fingerprint on error_events (fingerprint) where status in ('open', 'acknowledged');
create index if not exists error_events_recent_idx on error_events (last_seen_at desc);
create index if not exists error_events_filter_idx on error_events (agent, grouping, status, severity);
create index if not exists error_events_scan_idx on error_events (scan_id) where scan_id is not null;

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'consiva_app') then
        grant select, insert, update on error_events to consiva_app;
    end if;
end $$;
