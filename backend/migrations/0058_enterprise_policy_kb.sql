-- 0058: the Enterprise persistent policy knowledge base, and cross-scan reuse of analysed passages.
--
-- What the organisation DECLARES (privacy notice, cookie policy, grievance page, terms, ...) is kept per website as versioned documents. A rescan compares the page's content hash with the
-- stored version: unchanged -> nothing is re-embedded; changed -> a new version, the old one kept; gone -> marked removed, history kept. What the website actually DOES (cookies, trackers,
-- forms, consent) stays in ent_evidence and is never mixed into these tables.
--
-- Reuses what exists: the same Postgres, the pgvector extension and the 1024-dimension embedding the 5-page product's knowledge base already uses, and the tenancy rule (org_id on every row,
-- row-level security enabled and forced, and an org filter in every query). Nothing here touches knowledge_documents or knowledge_chunks (the DPDP legal texts).
-- Additive only: new tables, and two nullable columns on ent_analysis_chunks.

create extension if not exists vector;

create table if not exists ent_policy_docs (
    id                 uuid primary key default gen_random_uuid(),
    org_id             uuid not null,
    domain             text not null,
    identity_key       text not null,
    doc_type           text not null,
    status             text not null default 'active' check (status in ('active', 'removed')),
    current_version_id uuid,
    stale              boolean not null default false,
    stale_reason       text,
    url_history        jsonb not null default '[]'::jsonb,
    first_seen_at      timestamptz not null default now(),
    last_seen_at       timestamptz not null default now(),
    last_verified_at   timestamptz,
    constraint ent_policy_docs_unique unique (org_id, domain, identity_key)
);
create index if not exists ent_policy_docs_site_idx on ent_policy_docs (org_id, domain, status);

create table if not exists ent_policy_versions (
    id                        uuid primary key default gen_random_uuid(),
    org_id                    uuid not null,
    doc_id                    uuid not null references ent_policy_docs(id) on delete cascade,
    version_no                integer not null check (version_no >= 1),
    scan_id                   uuid references ent_scans(id) on delete set null,
    original_url              text not null,
    final_url                 text not null,
    title                     text,
    doc_type                  text not null,
    classification_method     text not null,
    classification_confidence text,
    content_hash              text not null,
    text                      text not null,
    text_chars                integer not null,
    extracted_at              timestamptz not null default now(),
    extraction_status         text not null default 'complete' check (extraction_status in ('complete', 'partial', 'failed')),
    extraction_note           text,
    sync_status               text not null default 'pending' check (sync_status in ('pending', 'ready', 'failed')),
    sync_error                text,
    embedding_model           text,
    embedding_dim             integer,
    chunk_count               integer not null default 0,
    is_current                boolean not null default false,
    superseded_at             timestamptz,
    source_evidence_ids       uuid[] not null default '{}',
    constraint ent_policy_versions_unique unique (doc_id, version_no)
);
create index if not exists ent_policy_versions_doc_idx on ent_policy_versions (doc_id, version_no desc);
create unique index if not exists ent_policy_versions_one_current on ent_policy_versions (doc_id) where is_current;

create table if not exists ent_policy_chunks (
    id               uuid primary key,
    org_id           uuid not null,
    doc_id           uuid not null references ent_policy_docs(id) on delete cascade,
    version_id       uuid not null references ent_policy_versions(id) on delete cascade,
    domain           text not null,
    doc_type         text not null,
    source_url       text not null,
    chunk_index      integer not null check (chunk_index >= 0),
    section_heading  text,
    content          text not null,
    content_hash     text not null,
    token_estimate   integer not null,
    embedding        vector(1024),
    embedding_source text check (embedding_source in ('generated', 'reused')),
    is_active        boolean not null default false,
    tsv              tsvector generated always as (to_tsvector('english', content)) stored,
    created_at       timestamptz not null default now(),
    constraint ent_policy_chunks_unique unique (version_id, chunk_index)
);
create index if not exists ent_policy_chunks_site_idx on ent_policy_chunks (org_id, domain, is_active);
create index if not exists ent_policy_chunks_hash_idx on ent_policy_chunks (org_id, doc_id, content_hash) where embedding is not null;
create index if not exists ent_policy_chunks_tsv_idx on ent_policy_chunks using gin (tsv);
create index if not exists ent_policy_chunks_embedding_idx on ent_policy_chunks using hnsw (embedding vector_cosine_ops);

create table if not exists ent_policy_sync_log (
    id         uuid primary key default gen_random_uuid(),
    org_id     uuid not null,
    scan_id    uuid references ent_scans(id) on delete set null,
    doc_id     uuid references ent_policy_docs(id) on delete set null,
    version_id uuid,
    domain     text not null,
    action     text not null check (action in ('new', 'unchanged', 'changed', 'removed', 'url_changed', 'extraction_failed', 'sync_failed', 'marked_stale', 'inconsistency')),
    detail     jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);
create index if not exists ent_policy_sync_log_site_idx on ent_policy_sync_log (org_id, domain, created_at desc);

create table if not exists ent_policy_retrievals (
    id           uuid primary key default gen_random_uuid(),
    org_id       uuid not null,
    domain       text not null,
    parameter_id text,
    mode         text not null,
    results      integer not null,
    created_at   timestamptz not null default now()
);
create index if not exists ent_policy_retrievals_site_idx on ent_policy_retrievals (org_id, domain, created_at desc);

alter table ent_frontier add column if not exists page_category_strength text;

-- cross-scan reuse of an analysed passage: the same text, read with the same extraction prompt by the same model, gives the same facts
alter table ent_analysis_chunks
    add column if not exists text_hash text,
    add column if not exists extractor_key text;
create index if not exists ent_analysis_chunks_reuse_idx on ent_analysis_chunks (text_hash, extractor_key) where status = 'done' and level = 'page';

do $$
declare t text;
begin
    if exists (select 1 from pg_proc where proname = 'current_org_id') then
        foreach t in array array['ent_policy_docs', 'ent_policy_versions', 'ent_policy_chunks', 'ent_policy_sync_log', 'ent_policy_retrievals'] loop
            execute format('alter table %I enable row level security', t);
            execute format('alter table %I force row level security', t);
            if not exists (select 1 from pg_policies where schemaname = current_schema() and tablename = t and policyname = 'tenant_isolation') then
                execute format('create policy tenant_isolation on %I using (org_id = current_org_id()) with check (org_id = current_org_id())', t);
            end if;
            if exists (select 1 from pg_roles where rolname = 'consiva_app') then
                execute format('grant select, insert, update, delete on %I to consiva_app', t);
            end if;
        end loop;
    end if;
end $$;
