-- 0052: the Enterprise DPDP regulatory registry: versioned, and never edited in place.
--
-- A registry is REFERENCE data shared by every organisation, not tenant data: no org_id and no row-level security (the same position as policy_analysis_cache, 0049).
-- Changing a provision means a NEW version row set; a version that has been approved is never altered, so any past assessment can be traced to the exact text it used.
--
-- ent_registry_versions     one row per version of the registry; draft until a named person approves it.
-- ent_registry_provisions   the provisions of one version: instrument (Act or Rules), exact reference, a phrase copied from the provision, when it commences.
-- Additive: nothing existing is altered.

create table if not exists ent_registry_versions (
    id          uuid primary key default gen_random_uuid(),
    version     text not null unique,
    source      text not null,
    note        text,
    status      text not null default 'draft' check (status in ('draft', 'approved')),
    approved_by text,
    approved_at timestamptz,
    created_at  timestamptz not null default now(),
    -- an approved version always names who approved it, and when
    constraint ent_registry_approved_has_approver check (status <> 'approved' or (approved_by is not null and approved_at is not null))
);

create table if not exists ent_registry_provisions (
    id            uuid primary key default gen_random_uuid(),
    version_id    uuid not null references ent_registry_versions(id) on delete cascade,
    instrument    text not null check (instrument in ('Act', 'Rules')),
    ref           text not null,
    summary       text not null,
    relation      text not null check (relation in ('source of the obligation', 'related context', 'good practice')),
    phrase        text not null,
    -- the full official text of the provision. Null until it is loaded from the gazetted PDFs and signed off; the assessment never claims more than `phrase`.
    official_text text,
    commences     date not null,
    constraint ent_registry_provision_unique unique (version_id, instrument, ref)
);
create index if not exists ent_registry_provisions_version_idx on ent_registry_provisions (version_id);

-- an approved version is frozen: its provisions cannot be inserted, changed or deleted
create or replace function ent_registry_freeze() returns trigger language plpgsql as $$
declare vid uuid;
begin
    vid := case when tg_op = 'DELETE' then old.version_id else new.version_id end;
    if exists (select 1 from ent_registry_versions v where v.id = vid and v.status = 'approved') then
        raise exception 'registry version % is approved and cannot be changed; create a new version', vid;
    end if;
    return case when tg_op = 'DELETE' then old else new end;
end $$;

drop trigger if exists ent_registry_provisions_freeze on ent_registry_provisions;
create trigger ent_registry_provisions_freeze before insert or update or delete on ent_registry_provisions
    for each row execute function ent_registry_freeze();

-- ... and an approved version itself cannot be edited or un-approved
create or replace function ent_registry_version_freeze() returns trigger language plpgsql as $$
begin
    if old.status = 'approved' then
        raise exception 'registry version % is approved and cannot be changed; create a new version', old.version;
    end if;
    return case when tg_op = 'DELETE' then old else new end;
end $$;

drop trigger if exists ent_registry_versions_freeze on ent_registry_versions;
create trigger ent_registry_versions_freeze before update or delete on ent_registry_versions
    for each row execute function ent_registry_version_freeze();

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'consiva_app') then
        grant select, insert, update on ent_registry_versions, ent_registry_provisions to consiva_app;
    end if;
end $$;
