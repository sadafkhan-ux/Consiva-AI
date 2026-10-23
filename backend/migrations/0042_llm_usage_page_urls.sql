-- 0042: which pages' content each recorded LLM call read.
--
-- The usage panel in the console shows token use by page where that can be said exactly. A call is attributed to one page only when it read exactly one;
-- a call that read several pages together (the findings analysis reads every scanned page, the policy analysis reads every policy document) is shown as
-- shared across them, never split by guesswork. So each event keeps the list of page URLs its prompt was built from. Empty means "not recorded".
--
-- Additive: one column with a default, nothing existing is altered.
alter table llm_usage_events add column if not exists page_urls text[] not null default '{}';
