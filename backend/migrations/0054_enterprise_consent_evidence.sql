-- 0054: allow the 'consent' evidence kind.
--
-- The Enterprise consent tests (accept, reject, withdraw, per-purpose choices) each produce one structured result. They are kept as evidence of kind 'consent', in the same table and
-- with the same hash, source and tenant rules as every other observation.
-- Additive: widening a CHECK cannot invalidate a row that already satisfies it. Same pattern as 0051.
alter table ent_evidence
    drop constraint if exists ent_evidence_kind_check;

alter table ent_evidence
    add constraint ent_evidence_kind_check
    check (kind in ('page_html', 'page_text', 'network', 'cookie', 'form', 'document_text', 'screenshot', 'header', 'consent'));
