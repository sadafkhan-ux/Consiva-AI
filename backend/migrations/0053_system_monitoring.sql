-- 0053: system health and alerts. Operational data about the DEPLOYMENT (is the Claude key valid, is the database up, are jobs stuck), not about any tenant:
-- no org_id and no row-level security, the same position as policy_analysis_cache (0049) and the registry (0052). Only administrators can read it through the API.
--
-- system_health  the latest result of each automatic check: one row per source.
-- system_alerts  a problem that needs a person. At most one OPEN (or acknowledged) alert per source, so a failure that lasts a day is one alert with a count, not a thousand rows.
-- Additive: nothing existing is altered. Detail text is scrubbed of keys and tokens before it is stored (app/monitoring/scrub.py).

create table if not exists system_health (
    source               text primary key,
    label                text not null,
    status               text not null check (status in ('ok', 'failing', 'unknown')),
    detail               text,
    latency_ms           integer,
    checked_at           timestamptz not null default now(),
    last_ok_at           timestamptz,
    consecutive_failures integer not null default 0 check (consecutive_failures >= 0)
);

create table if not exists system_alerts (
    id              uuid primary key default gen_random_uuid(),
    source          text not null,
    severity        text not null check (severity in ('warning', 'critical')),
    status          text not null default 'open' check (status in ('open', 'acknowledged', 'resolved')),
    title           text not null,
    detail          text not null,
    action          text,
    first_seen_at   timestamptz not null default now(),
    last_seen_at    timestamptz not null default now(),
    occurrences     integer not null default 1 check (occurrences >= 1),
    resolved_at     timestamptz,
    acknowledged_by text,
    acknowledged_at timestamptz,
    last_notified_at timestamptz,
    notify_count    integer not null default 0 check (notify_count >= 0),
    -- what happened to the last email: sent, not_configured, or failed with a short reason. Never "sent" unless the mail server accepted it.
    notify_status   text,
    constraint system_alerts_resolved_has_time check (status <> 'resolved' or resolved_at is not null)
);
-- one live alert per source
create unique index if not exists system_alerts_one_live_per_source on system_alerts (source) where status in ('open', 'acknowledged');
create index if not exists system_alerts_recent_idx on system_alerts (last_seen_at desc);

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'consiva_app') then
        grant select, insert, update on system_health, system_alerts to consiva_app;
    end if;
end $$;
