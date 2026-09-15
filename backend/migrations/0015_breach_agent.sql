-- Agent 4 (Breach Response) persistence: the incident case and every artefact that
-- must trace back to it.
--
-- Additive only; idempotent, same convention as every other migration here. Nothing
-- touches an Agent 1 (consent), Agent 2 (ropa) or Agent 3 (dsr) table.
--
-- FOUR REUSE DECISIONS -- read before adding tables:
--
-- 1. NO incident_case_events TABLE. The case timeline of WHO DID WHAT IN CONSIVA is
--    audit_logs filtered by (entity_type='incident_case', entity_id=incident_id) --
--    indexed by 0001 and append-only by trigger since 0004. `incident_timeline` is a
--    different thing entirely: it is what happened IN THE WORLD, reconstructed from
--    evidence, and its entries are claims about an attacker rather than records of
--    our own actions. Conflating the two would make it impossible to tell "we believe
--    the database was read at 10:05" from "a reviewer approved something at 10:05".
--
-- 2. NO SEPARATE SLA TABLE. Deadlines live on the incident row, as they do for a DSR
--    case, and are swept by the same worker pass.
--
-- 3. NO SECOND DATA-CATEGORY TAXONOMY. Affected data references Agent 2's categories
--    by name and is derived using Agent 2's own classifier.
--
-- 4. RESPONSE ACTIONS ARE TRACKED WORK BY DEFAULT. Consiva has no connector to Active
--    Directory, a cloud console or a firewall, so it cannot disable an account or
--    isolate a service. `execution_mode` records which kind an action is: 'tracked'
--    means a person does it and attests what they did; 'connector' is the narrow case
--    where the action genuinely is a database operation on a source already
--    authorized for DSR, and Agent 3's connector performs it for real. Pretending
--    otherwise would be the fake implementation §50 forbids.

-- ── The incident case -- the central business object (§6) ────────────────────────
create table if not exists incident_cases (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    reference              text not null,              -- human-quotable, e.g. INC-9F3A2B01
    title                  text not null,
    description            text not null,
    -- Where it came from. A SIEM alert and an employee's phone call are both valid
    -- origins and are weighted differently when assessing confidence.
    source                 text not null
        check (source in (
            'manual','security_alert','siem','application_monitoring','database_monitoring',
            'access_anomaly','employee_report','vendor_notification','customer_complaint',
            'security_team','authorized_integration'
        )),
    reported_by            text,                       -- free text: may be a person outside Consiva
    incident_type          text not null default 'unclassified'
        check (incident_type in (
            'unclassified','unauthorized_access','data_exposure','data_leakage',
            'credential_compromise','malware_ransomware','accidental_disclosure',
            'lost_stolen_device','third_party_incident','misconfiguration',
            'insider_incident','other'
        )),
    classification_method  text,                       -- 'deterministic' | 'llm' | 'manual'
    classification_confidence  double precision,
    -- Severity is assessed from evidence, never taken on trust from the reporter.
    -- `initial_severity` is what the reporter claimed; `severity` is what the engine
    -- concluded. Keeping both means an under-reported critical incident is visible.
    initial_severity       text check (initial_severity in ('low','medium','high','critical')),
    severity               text check (severity in ('low','medium','high','critical')),
    severity_score         double precision,
    severity_confidence    text
        check (severity_confidence in ('confirmed','probable','possible','unknown')),
    -- THE question this agent exists to answer carefully. Never promoted to
    -- 'confirmed' by any rule or model -- only a named human may assert that.
    personal_data_involved text not null default 'unknown'
        check (personal_data_involved in ('confirmed','probable','possible','unknown')),
    breach_confirmed       text not null default 'unknown'
        check (breach_confirmed in ('confirmed','probable','possible','unknown')),
    status                 text not null default 'reported'
        check (status in (
            'reported','validating','investigating','impact_assessment','risk_assessment',
            'review_required','response_pending','approval_required','approved',
            'responding','verifying','communication_pending','closure_review','closed',
            'rejected','failed','escalated','partially_completed','cancelled'
        )),
    error_code             text,
    error_detail           text,
    -- Timestamps. `detected_at` is when the organisation became aware, which is what
    -- most notification clocks actually run from -- not when the incident began, and
    -- not when someone got round to filing it.
    occurred_at            timestamptz,                -- best estimate, may stay null
    detected_at            timestamptz not null,
    reported_at            timestamptz not null default now(),
    -- SLA (§41). Configurable per org; never a hard-coded legal deadline.
    due_at                 timestamptz,
    sla_breached           boolean not null default false,
    escalated_at           timestamptz,
    closed_at              timestamptz,
    closure_summary        text,
    correlation_id         text,
    idempotency_key        text,
    created_by_user_id     uuid,
    created_at             timestamptz not null default now(),
    updated_at             timestamptz not null default now(),
    unique (org_id, reference)
);
create index if not exists incident_cases_org_idx on incident_cases (org_id);
create index if not exists incident_cases_org_status_idx on incident_cases (org_id, status);
create index if not exists incident_cases_due_idx on incident_cases (due_at) where sla_breached = false;
create unique index if not exists incident_cases_idempotency_idx
    on incident_cases (org_id, idempotency_key) where idempotency_key is not null;

-- ── Evidence (§11) ──────────────────────────────────────────────────────────────
-- Everything the incident's conclusions rest on. Evidence is APPEND-ONLY by trigger
-- below: an investigation whose evidence can be rewritten afterwards is not evidence,
-- it is a narrative. Superseding is done by adding a new row that references the old.
create table if not exists incident_evidence (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    incident_id            uuid not null references incident_cases(id) on delete cascade,
    kind                   text not null
        check (kind in (
            'security_log','access_log','authentication_event','database_event',
            'application_log','system_alert','incident_report','source_metadata',
            'ropa_context','external_evidence','human_observation'
        )),
    -- Where it came from, and when the thing it describes happened.
    source_system          text not null,
    observed_at            timestamptz,
    summary                text not null,
    -- The evidence itself, already redacted by the caller. `contains_secrets` marks a
    -- row whose detail must never be rendered in the UI or an export (§38).
    detail                 jsonb not null default '{}'::jsonb,
    contains_secrets       boolean not null default false,
    -- Derived evidence is something Consiva worked out about itself (a ROPA lookup),
    -- not something that happened. A finding supported ONLY by derived evidence is
    -- not corroborated by anything external, and the risk engine weights it lower.
    is_derived             boolean not null default false,
    -- Set when this row replaces an earlier one. The earlier row stays.
    supersedes_id          uuid references incident_evidence(id),
    added_by_user_id       uuid,
    correlation_id         text,
    created_at             timestamptz not null default now()
);
create index if not exists incident_evidence_org_idx on incident_evidence (org_id);
create index if not exists incident_evidence_incident_idx on incident_evidence (incident_id);

-- ── Timeline (§13) -- what happened in the WORLD ────────────────────────────────
-- Distinct from audit_logs, which is what happened in Consiva. Every entry must cite
-- the evidence it rests on, and carries its own confidence: "the database was read at
-- 10:05" is a claim, and how strongly it is believed is part of the claim.
create table if not exists incident_timeline (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    incident_id            uuid not null references incident_cases(id) on delete cascade,
    occurred_at            timestamptz not null,
    event                  text not null,
    actor                  text,                       -- account, service or person, where known
    source_system          text,
    confidence             text not null default 'possible'
        check (confidence in ('confirmed','probable','possible','unknown')),
    evidence_id            uuid references incident_evidence(id),
    created_by_user_id     uuid,
    created_at             timestamptz not null default now()
);
create index if not exists incident_timeline_org_idx on incident_timeline (org_id);
create index if not exists incident_timeline_incident_idx
    on incident_timeline (incident_id, occurred_at);

-- ── Affected systems (§14) ──────────────────────────────────────────────────────
create table if not exists incident_affected_systems (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    incident_id            uuid not null references incident_cases(id) on delete cascade,
    system_name            text not null,
    system_kind            text not null
        check (system_kind in (
            'application','database','api','cloud_service','storage','server','vendor','other'
        )),
    component              text,                       -- schema, bucket, endpoint, host
    -- Links to the authorized-source registry where the affected system happens to be
    -- one Consiva already knows about. Null for a system Consiva has never connected
    -- to, which is the common case for a server or a vendor.
    data_source_id         uuid references ropa_data_sources(id),
    confidence             text not null default 'possible'
        check (confidence in ('confirmed','probable','possible','unknown')),
    evidence_id            uuid references incident_evidence(id),
    notes                  text,
    created_at             timestamptz not null default now(),
    unique (incident_id, system_name, component)
);
create index if not exists incident_systems_org_idx on incident_affected_systems (org_id);
create index if not exists incident_systems_incident_idx on incident_affected_systems (incident_id);

-- ── Affected data (§15) ─────────────────────────────────────────────────────────
-- Categories are Agent 2's, referenced by name rather than redefined. `derived_from`
-- records HOW the category was established -- a ROPA lookup is a strong prior but it
-- is not the same as having observed the data in the incident itself.
create table if not exists incident_affected_data (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    incident_id            uuid not null references incident_cases(id) on delete cascade,
    affected_system_id     uuid references incident_affected_systems(id) on delete cascade,
    data_category          text not null,              -- Agent 2's category vocabulary
    table_name             text,
    column_name            text,
    confidence             text not null default 'possible'
        check (confidence in ('confirmed','probable','possible','unknown')),
    derived_from           text not null default 'ropa_metadata'
        check (derived_from in ('ropa_metadata','incident_evidence','manual')),
    evidence_id            uuid references incident_evidence(id),
    created_at             timestamptz not null default now()
);
create index if not exists incident_data_org_idx on incident_affected_data (org_id);
create index if not exists incident_data_incident_idx on incident_affected_data (incident_id);

-- ── Affected subjects (§16) ─────────────────────────────────────────────────────
-- Counts are NEVER a bare number. `count_basis` says whether a figure was counted,
-- estimated or is unknown, because "11,500 customers affected" and "we think roughly
-- 11,500" are different statements and only one of them belongs in a regulator's
-- inbox.
create table if not exists incident_affected_subjects (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    incident_id            uuid not null references incident_cases(id) on delete cascade,
    subject_group          text not null,              -- 'Customer', 'Employee', ...
    record_count           integer,
    count_basis            text not null default 'unknown'
        check (count_basis in ('counted','estimated','unknown')),
    -- How the figure was arrived at, in words a reviewer can check.
    basis_note             text,
    confidence             text not null default 'possible'
        check (confidence in ('confirmed','probable','possible','unknown')),
    evidence_id            uuid references incident_evidence(id),
    created_at             timestamptz not null default now(),
    unique (incident_id, subject_group)
);
create index if not exists incident_subjects_org_idx on incident_affected_subjects (org_id);
create index if not exists incident_subjects_incident_idx on incident_affected_subjects (incident_id);

-- ── Risk assessment (§19) ───────────────────────────────────────────────────────
-- Versioned: re-assessing as evidence arrives is normal, and the earlier assessment
-- is part of the incident's history rather than something to overwrite.
create table if not exists incident_risk_assessments (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    incident_id            uuid not null references incident_cases(id) on delete cascade,
    version                integer not null default 1,
    risk_level             text not null check (risk_level in ('low','medium','high','critical')),
    risk_score             double precision,
    confidence             text not null
        check (confidence in ('confirmed','probable','possible','unknown')),
    -- Every factor that moved the score, with its contribution, so a reviewer can
    -- disagree with one input rather than with an opaque number.
    factors                jsonb not null default '[]'::jsonb,
    reason                 text not null,
    -- Regulatory context retrieved from the approved knowledge base, kept SEPARATE
    -- from the system facts above (§20). This is never a determination that a law
    -- applies -- it is the passages a human should read before deciding.
    regulatory_context     jsonb not null default '[]'::jsonb,
    assessed_by            text not null default 'engine',   -- 'engine' | 'manual'
    review_status          text not null default 'pending'
        check (review_status in ('pending','approved','rejected','superseded')),
    reviewed_by_user_id    uuid,
    reviewed_at            timestamptz,
    review_reason          text,
    created_at             timestamptz not null default now(),
    unique (incident_id, version)
);
create index if not exists incident_risk_org_idx on incident_risk_assessments (org_id);
create index if not exists incident_risk_incident_idx on incident_risk_assessments (incident_id);

-- ── Response actions (§21, §24) ─────────────────────────────────────────────────
create table if not exists incident_actions (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    incident_id            uuid not null references incident_cases(id) on delete cascade,
    action_kind            text not null
        check (action_kind in (
            'disable_account','rotate_credential','isolate_service','preserve_logs',
            'revoke_session','patch_misconfiguration','investigate','notify_internal',
            'review_notification_duty','other_containment'
        )),
    -- 'tracked': a person performs it and attests what they did.
    -- 'connector': Agent 3's connector performs it for real, with read-back.
    execution_mode         text not null default 'tracked'
        check (execution_mode in ('tracked','connector')),
    title                  text not null,
    rationale              text not null,
    expected_result        text not null,
    target                 text,                       -- account, service, host, table
    affected_system_id     uuid references incident_affected_systems(id),
    risk                   text not null default 'medium' check (risk in ('low','medium','high')),
    requires_approval      boolean not null default true,
    status                 text not null default 'proposed'
        check (status in (
            'proposed','approved','rejected','blocked','in_progress','completed',
            'failed','skipped'
        )),
    blocked_reason         text,
    assignee_label         text,
    created_at             timestamptz not null default now(),
    updated_at             timestamptz not null default now()
);
create index if not exists incident_actions_org_idx on incident_actions (org_id);
create index if not exists incident_actions_incident_idx on incident_actions (incident_id);

-- ── Review decisions -- append-only, like every other decision record ────────────
-- Separate from Agent 1's `approvals` only because that table's finding_id is a NOT
-- NULL FK to consent_findings. Agents 2 and 3 met the same constraint and made the
-- same call; all of them write through the SHARED audit_service, which is the actual
-- common infrastructure.
create table if not exists incident_approvals (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    incident_id            uuid not null references incident_cases(id) on delete cascade,
    -- Exactly one of these is set: what the decision is ABOUT.
    action_id              uuid references incident_actions(id),
    risk_assessment_id     uuid references incident_risk_assessments(id),
    communication_id       uuid,                       -- FK added after the table below
    subject               text not null,               -- 'action' | 'risk' | 'communication' | 'closure'
    decision               text not null
        check (decision in ('approved','rejected','edited','request_more_information','escalated')),
    reason                 text,
    edited_payload         jsonb,
    reviewer_user_id       uuid not null,
    -- An approval authorizes specific work and does not last forever; execution
    -- re-reads this and refuses one that has lapsed.
    expires_at             timestamptz,
    created_at             timestamptz not null default now()
);
create index if not exists incident_approvals_org_idx on incident_approvals (org_id);
create index if not exists incident_approvals_incident_idx on incident_approvals (incident_id);
create index if not exists incident_approvals_action_idx on incident_approvals (action_id);

-- ── Execution ledger (§25) ──────────────────────────────────────────────────────
-- One row per attempt at one action. The unique index on (org_id, idempotency_key) is
-- what stops a double-click, a retry or a worker restart disabling an account twice.
--
-- For a TRACKED action the "execution" is a human attestation: `performed_by` and
-- `attestation` record who says they did it and what they say they did. That is a
-- weaker claim than a connector read-back, and `verification_status` says which kind
-- of confirmation this is rather than blurring them.
create table if not exists incident_executions (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    incident_id            uuid not null references incident_cases(id) on delete cascade,
    action_id              uuid not null references incident_actions(id) on delete cascade,
    idempotency_key        text not null,
    execution_mode         text not null check (execution_mode in ('tracked','connector')),
    status                 text not null default 'pending'
        check (status in ('pending','running','succeeded','failed','verified','verification_failed')),
    -- Connector executions only.
    rows_affected          integer,
    connector_response     jsonb,
    -- Tracked executions only: who performed it outside Consiva, and what they say.
    performed_by           text,
    attestation            text,
    -- 'read_back'  -- a connector independently confirmed the end state
    -- 'attested'   -- a human said they did it; nothing machine-checked it
    -- 'failed' / 'not_applicable'
    verification_status    text
        check (verification_status in ('pending','read_back','attested','failed','not_applicable')),
    verification_detail    jsonb,
    verified_at            timestamptz,
    error_code             text,
    error_detail           text,
    attempts               integer not null default 0,
    job_id                 uuid,
    correlation_id         text,
    executed_by_user_id    uuid,
    started_at             timestamptz,
    completed_at           timestamptz,
    created_at             timestamptz not null default now(),
    updated_at             timestamptz not null default now()
);
create index if not exists incident_executions_org_idx on incident_executions (org_id);
create index if not exists incident_executions_incident_idx on incident_executions (incident_id);
create unique index if not exists incident_executions_idempotency_idx
    on incident_executions (org_id, idempotency_key);

-- ── Communications (§26) ────────────────────────────────────────────────────────
create table if not exists incident_communications (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    incident_id            uuid not null references incident_cases(id) on delete cascade,
    audience               text not null
        check (audience in (
            'internal','management','privacy_team','affected_individual','customer',
            'vendor','regulator'
        )),
    subject                text not null,
    body                   text not null,
    status                 text not null default 'draft'
        check (status in ('draft','review_required','approved','rejected','sent')),
    -- Populated when a model drafted the prose. Null means fully deterministic.
    -- Either way the FACTS come from the incident's own rows, never from the model.
    drafted_by_model       text,
    grounded_facts         jsonb not null default '[]'::jsonb,
    approved_by_user_id    uuid,
    approved_at            timestamptz,
    -- Only ever set when something was actually sent. No outbound provider is
    -- configured in this project, so in practice this is stamped by a human
    -- recording that they sent it -- never by the system claiming it did.
    sent_at                timestamptz,
    sent_by_user_id        uuid,
    created_at             timestamptz not null default now(),
    updated_at             timestamptz not null default now()
);
create index if not exists incident_comms_org_idx on incident_communications (org_id);
create index if not exists incident_comms_incident_idx on incident_communications (incident_id);

alter table incident_approvals
    drop constraint if exists incident_approvals_communication_id_fkey;
alter table incident_approvals
    add constraint incident_approvals_communication_id_fkey
    foreign key (communication_id) references incident_communications(id);

-- ── Incident report (§17 of the phase list) ─────────────────────────────────────
create table if not exists incident_reports (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    incident_id            uuid not null references incident_cases(id) on delete cascade,
    version                integer not null default 1,
    body_text              text not null,
    -- The machine-checkable rows the prose was assembled from. A sentence not
    -- traceable to one of these is not an incident fact.
    grounded_facts         jsonb not null default '[]'::jsonb,
    drafted_by_model       text,
    status                 text not null default 'draft'
        check (status in ('draft','review_required','approved','final')),
    approved_by_user_id    uuid,
    created_at             timestamptz not null default now(),
    updated_at             timestamptz not null default now(),
    unique (incident_id, version)
);
create index if not exists incident_reports_org_idx on incident_reports (org_id);
create index if not exists incident_reports_incident_idx on incident_reports (incident_id);

-- ── Append-only enforcement ─────────────────────────────────────────────────────
-- Same shared function 0004 introduced. Evidence and approvals are the two records an
-- investigation's credibility rests on: if either can be rewritten after the fact,
-- the incident report is a story rather than a finding. Executions stay mutable --
-- a row legitimately moves pending -> running -> verified in place and IS the
-- idempotency ledger.
drop trigger if exists incident_evidence_append_only on incident_evidence;
create trigger incident_evidence_append_only
    before update or delete on incident_evidence
    for each row execute function reject_append_only_mutation();

drop trigger if exists incident_approvals_append_only on incident_approvals;
create trigger incident_approvals_append_only
    before update or delete on incident_approvals
    for each row execute function reject_append_only_mutation();

-- ── Row-level security ──────────────────────────────────────────────────────────
alter table incident_cases              enable row level security;
alter table incident_evidence           enable row level security;
alter table incident_timeline           enable row level security;
alter table incident_affected_systems   enable row level security;
alter table incident_affected_data      enable row level security;
alter table incident_affected_subjects  enable row level security;
alter table incident_risk_assessments   enable row level security;
alter table incident_actions            enable row level security;
alter table incident_approvals          enable row level security;
alter table incident_executions         enable row level security;
alter table incident_communications     enable row level security;
alter table incident_reports            enable row level security;

do $$
declare
    t text;
begin
    foreach t in array array[
        'incident_cases','incident_evidence','incident_timeline',
        'incident_affected_systems','incident_affected_data','incident_affected_subjects',
        'incident_risk_assessments','incident_actions','incident_approvals',
        'incident_executions','incident_communications','incident_reports'
    ] loop
        if not exists (
            select 1 from pg_policies
            where schemaname = current_schema() and tablename = t and policyname = 'tenant_isolation'
        ) then
            execute format('create policy tenant_isolation on %I using (org_id = current_org_id())', t);
        end if;
    end loop;
end $$;
