-- 0036: Internal/technical notes for a Consent Agent finding, kept separate from the
-- customer-facing finding_text.
--
-- Before this, create_rule_findings.py's narrative-missing fallback (the path taken
-- when the LLM's write-up fails grounding validation or every provider is
-- unreachable) wrote the raw exception string -- including internal chunk_id/
-- dpdp_reference UUIDs and provider error text -- straight into finding_text, which
-- the customer-facing API/report surfaces verbatim. A real scan's finding_text read
-- "LLM output failed grounding after 2 retries: ungrounded dpdp_reference ids
-- ['24977b5b-...', ...]; unsupported vendor-timing claims {...}" -- an internal
-- diagnostic, not something a compliance reviewer should ever see as the explanation
-- of a DPDP finding.
--
-- internal_notes holds that diagnostic instead. It is deliberately not surfaced by
-- any customer-facing API schema (FindingOut / consent_scans.py's FindingResponse) --
-- additive, nullable, no change to any existing row.

alter table consent_findings add column if not exists internal_notes text;
