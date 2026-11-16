-- 0056: crawl priority for the Enterprise frontier.
--
-- A site can have far more pages than a crawl will reach (a limit, a deadline, a memory budget). The pages that matter most to a privacy assessment (the privacy notice, the cookie policy, the terms,
-- the grievance and contact pages) must be read first, so a scan that is cut short still has them. `priority` is 0 for those and 1 for every other page; the frontier is claimed by priority, then by
-- depth. Additive: a new column with a default, and an index.
alter table ent_frontier
    add column if not exists priority smallint not null default 1;

create index if not exists ent_frontier_priority_idx on ent_frontier (scan_id, status, priority, depth, discovered_at);
