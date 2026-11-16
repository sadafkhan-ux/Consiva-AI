-- 0057: per-page evidence tracking for the Enterprise agent, and a count of URLs a scan could not record.
--
-- Until now the page's title, how much text was extracted, its content hash, how it was classified and how many passages were created and analysed were not kept anywhere a reader could
-- query: they had to be rebuilt from the frontier, the evidence rows and the chunk table. These columns keep them on the frontier row, so "where was content lost or skipped" is one query.
-- Additive: new nullable columns, and one counter with a default. Nothing is dropped or rewritten.
alter table ent_frontier
    add column if not exists page_title text,
    add column if not exists text_chars integer,
    add column if not exists content_hash text,
    add column if not exists extracted_at timestamptz,
    add column if not exists evidence_note text,
    add column if not exists page_category text,
    add column if not exists category_reason text,
    add column if not exists chunks_created integer,
    add column if not exists chunks_analysed integer,
    add column if not exists analysis_note text;

-- URLs found but not recorded because the scan reached its record cap (they were dropped, not queued).
alter table ent_scans
    add column if not exists urls_not_recorded integer not null default 0;

create index if not exists ent_frontier_content_hash_idx on ent_frontier (scan_id, content_hash) where content_hash is not null;
