-- 0025: Purpose Classifier (Phase 1).
--
-- Numbered 0025, not 0007, deliberately. This branch carries six migration FILES but
-- the database it runs against already has twenty-four applied (schema_migrations);
-- the branch's code is behind its own schema. Taking the next free file number would
-- collide with a 0007 that already exists elsewhere and is already recorded as applied
-- here, so migrate.py would skip this file entirely and the tables would never appear.
-- 0025 is the first number this database has not seen.
--
-- WHAT THIS AGENT IS FOR, AND WHAT IT IS NOT
--
-- It does not build a record of processing. It answers a narrower question: for data
-- that already exists, is the purpose it is being used for still the purpose it was
-- collected for, and should it still be kept? So it stores ASSESSMENTS and FINDINGS,
-- never purposes themselves -- the purpose of a thing belongs to whichever agent
-- discovered the thing.
--
-- Deliberately NOT created here:
--   * a second purpose vocabulary -- purpose_taxonomy already exists and is used
--   * a purposes table -- declared purpose lives with its owning record
--   * usage_evidence -- server-side usage ingestion is a later phase, and an empty
--     table invites code that pretends to read from it

-- ── Assessment: one declared-vs-observed comparison ─────────────────────────────
create table if not exists purpose_assessments (
    id                  uuid primary key default gen_random_uuid(),
    org_id              uuid not null references organizations(id) on delete cascade,

    -- What was assessed. `subject_type` says which evidence family the row came from,
    -- and `subject_ref` is that family's own identifier -- a tracker id, a cookie id,
    -- a table name. Kept as text with no foreign key ON PURPOSE: the referenced row
    -- may be re-scanned and replaced, and an assessment must survive as the historical
    -- record of what was true when it was made.
    subject_type        text not null
                          check (subject_type in ('tracker','cookie','third_party_service','table')),
    subject_ref         text not null,
    subject_label       text,

    -- The comparison itself. Both sides are nullable because either can be genuinely
    -- unknown, and "unknown" is a real answer this agent is allowed to give.
    declared_purpose    text,
    declared_source     text
                          check (declared_source in ('ropa_record','policy','manual','none')),
    observed_purpose    text,
    observed_source     text
                          check (observed_source in ('consent_scan','table_name','manual','none')),

    -- Aligned / mismatched / undetermined. `undetermined` is not a failure state: it
    -- is what an honest comparison returns when one side is unknown, and it must never
    -- be collapsed into `aligned` just to produce a cleaner dashboard.
    alignment           text not null
                          check (alignment in ('aligned','mismatch','undetermined')),
    confidence          numeric(3,2) not null default 0.0
                          check (confidence >= 0 and confidence <= 1),

    -- Retention signal, Phase 2's anchor. Recorded now because the evidence (a
    -- cookie's expiry) is already available, but nothing acts on it yet.
    retention_status    text not null default 'not_evaluated'
                          check (retention_status in
                                 ('not_evaluated','within_expectation','review_required','unknown')),
    retention_note      text,

    -- Every identifier that supports this row, so a reviewer can go and look.
    evidence_refs       jsonb not null default '[]'::jsonb,

    scan_id             uuid references consent_scans(id) on delete set null,
    run_id              uuid,
    created_at          timestamptz not null default now()
);

create index if not exists purpose_assessments_org_idx on purpose_assessments (org_id);
create index if not exists purpose_assessments_run_idx on purpose_assessments (run_id);
create index if not exists purpose_assessments_alignment_idx on purpose_assessments (org_id, alignment);

-- ── Finding: an assessment a person should look at ──────────────────────────────
--
-- Separate from the assessment because most assessments are unremarkable. A finding is
-- the subset worth someone's attention, and it carries its own review lifecycle.
create table if not exists purpose_findings (
    id                  uuid primary key default gen_random_uuid(),
    org_id              uuid not null references organizations(id) on delete cascade,
    assessment_id       uuid not null references purpose_assessments(id) on delete cascade,

    finding_type        text not null
                          check (finding_type in
                                 ('purpose_mismatch','processing_without_consent',
                                  'purpose_undeclared','retention_review')),
    severity            text not null check (severity in ('high','medium','low')),
    title               text not null,
    description         text not null,

    -- Forced true for high severity by the service layer, not merely defaulted here.
    -- Whether a high-risk compliance finding reaches a customer unread must not depend
    -- on a caller remembering to set a flag.
    review_required     boolean not null default true,
    status              text not null default 'pending'
                          check (status in ('pending','approved','rejected','dismissed')),

    decided_by_user_id  uuid references users(id),
    decided_at          timestamptz,
    decision_reason     text,
    created_at          timestamptz not null default now()
);

create index if not exists purpose_findings_org_idx on purpose_findings (org_id);
create index if not exists purpose_findings_assessment_idx on purpose_findings (assessment_id);
create index if not exists purpose_findings_status_idx on purpose_findings (org_id, status);

-- ── Run: one execution, for status and observability ────────────────────────────
create table if not exists purpose_runs (
    id                  uuid primary key default gen_random_uuid(),
    org_id              uuid not null references organizations(id) on delete cascade,
    scan_id             uuid references consent_scans(id) on delete set null,
    status              text not null default 'queued'
                          check (status in ('queued','running','completed','failed','cancelled')),
    assessments_count   integer not null default 0,
    findings_count      integer not null default 0,
    error               text,
    started_at          timestamptz,
    completed_at        timestamptz,
    created_at          timestamptz not null default now()
);

create index if not exists purpose_runs_org_idx on purpose_runs (org_id);

-- ── Queue ───────────────────────────────────────────────────────────────────────
--
-- agent_jobs.job_type is an enumerated CHECK, and it is invisible to the Python that
-- writes into it: a new value type-checks, reads fine in review, passes every test
-- that never opens a database, and fails only at runtime. That has already happened
-- more than once in this project's history, which is why this widening is part of the
-- same migration as the tables rather than something to remember later.
--
-- Rebuilt from the full list rather than appended to, because this branch's 0001
-- defines only ('scan','analyze') while the database has since been widened past it --
-- an ALTER that assumed the file's version would silently narrow the constraint and
-- break every other agent's queue writes.
alter table agent_jobs drop constraint if exists agent_jobs_job_type_check;

alter table agent_jobs
    add constraint agent_jobs_job_type_check
    check (job_type in (
        'scan', 'analyze', 'ropa_discovery', 'dsr_search', 'dsr_execute',
        'incident_analysis', 'regwatch_collect', 'regwatch_assess',
        'consent_api_chain', 'consent_webhook',
        'purpose_assessment'
    ));
