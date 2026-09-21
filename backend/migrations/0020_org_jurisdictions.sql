-- 0020: where an organisation operates.
--
-- Agent 5 decides relevance by comparing a source's jurisdiction against the
-- organisation's. Until now there was nowhere to record the second half of that
-- comparison, and rules/relevance.py handles the absence honestly -- it returns
-- `undetermined` with `unknown` confidence and says "record them to make this
-- assessment meaningful". That is the correct behaviour for an unconfigured org, but
-- it should not be the ONLY behaviour available.
--
-- Deliberately NOT derived from the registered sources. An organisation that
-- monitors the EDPB is not thereby established as operating in the EU, and deriving
-- one from the other would make the jurisdiction test vacuous: every source's
-- jurisdiction would match by construction and `not_relevant` could never be
-- reached.
--
-- Additive, defaulted, nullable in effect. No existing agent reads this column, so
-- nothing in Agents 1-4 changes behaviour because of it.
alter table organizations
    add column if not exists jurisdictions jsonb not null default '[]'::jsonb;

comment on column organizations.jurisdictions is
    'Jurisdictions this organisation operates in, as free text (e.g. ["India","EU"]). '
    'Empty means not recorded -- which Agent 5 reports as undetermined relevance, '
    'never as irrelevance.';
