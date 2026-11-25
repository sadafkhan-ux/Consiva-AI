-- 0063: an Enterprise report version records the method 3.0.0 result it was printed from.
--
-- Until now ent_reports.score_result_id was the only link, and it points at the 51-parameter (v2) score in ent_score_results, even for a report printed from method 3.0.0, the
-- official score. method3_result_id names the ent_method3_results row the PDF shows. score_result_id is kept as it is (the reports API returns it unchanged). A report printed
-- from the 51-parameter method, or printed before this migration, has no method 3.0.0 link (null). Additive only: one nullable column and its index.

alter table ent_reports add column if not exists method3_result_id uuid references ent_method3_results(id) on delete set null;
create index if not exists ent_reports_method3_result_idx on ent_reports (method3_result_id) where method3_result_id is not null;
