-- 0059: what the model suggested from a site's addresses, and the policies a page holds as sections.
--
-- slug_kinds       the policy kinds the model suggested for this address when it read the site's address list (a hint that raises the crawl priority and asks for a content check)
-- also_categories  the policies found as sections inside this page, in addition to its own category (for example a cookie policy inside the privacy page)
-- Additive: two nullable columns.
alter table ent_frontier
    add column if not exists slug_kinds text[],
    add column if not exists also_categories text[];
