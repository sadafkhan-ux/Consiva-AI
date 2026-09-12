-- Agent 3 (DSR Fulfillment) persistence: the DSR case and every artefact that must
-- trace back to it -- identity verification, search runs, evidence, action plans,
-- approvals, executions and the final response.
--
-- Additive only; idempotent (IF NOT EXISTS / guarded policy creation), same
-- convention as every other migration here. Nothing touches an Agent 1 (consent) or
-- Agent 2 (ropa) table.
--
-- THREE DELIBERATE REUSE DECISIONS -- read before adding tables:
--
-- 1. NO dsr_case_events TABLE. The case timeline is audit_logs filtered by
--    (entity_type='dsr_request', entity_id=case_id) -- a pair 0001 already indexes
--    and 0004 already made append-only by trigger. A second timeline table would be
--    a duplicate source of truth for the same facts.
--
-- 2. NO SECOND DATA-SOURCE REGISTRY. Authorized sources already live in
--    ropa_data_sources (0007) with the credential_ref secret pattern. DSR does not
--    copy them; dsr_source_authorizations POINTS at a ropa_data_sources row and adds
--    only what DSR needs on top: the search allowlist and a SEPARATE write credential.
--
-- 3. SEARCH RESULT *IS* EVIDENCE. dsr_evidence is one table, not a result table plus
--    an evidence table. A match that is not evidenced cannot support a DSR decision
--    (prompt §19), so there is no such thing as a result without evidence.
--
-- WRITE CREDENTIALS -- the security boundary that separates Agent 3 from Agent 2:
-- Agent 2's connector contract is read-only by design (connectors/base.py). Agent 3
-- must correct and delete. Rather than widen that contract -- which would hand write
-- capability to the discovery path -- a source must be SEPARATELY authorized for DSR
-- here, and write access requires its own credential_ref naming a different, more
-- privileged secret. A source authorized for discovery is NOT thereby authorized for
-- execution, and `allow_execution=false` means search-only no matter what credential
-- happens to exist.

-- ── Which authorized sources Agent 3 may touch, and how far ──────────────────────
create table if not exists dsr_source_authorizations (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    data_source_id         uuid not null references ropa_data_sources(id),
    -- Allowlists. Empty array means "nothing is searchable" -- fail closed, never
    -- "everything". The connector refuses any table/column not named here, so an LLM
    -- or a caller cannot widen the blast radius by asking nicely.
    searchable_tables      jsonb not null default '[]'::jsonb,
    -- Tables where ONE identifier value means ONE person (customers, users).
    -- Two rows here for one email is two candidate subjects, and the case stops
    -- for a human. Everywhere else many rows per person is normal (an orders
    -- table has many rows for one customer), so counting them as subjects would
    -- send every ordinary access request to review. Must be a subset of
    -- searchable_tables.
    identity_tables        jsonb not null default '[]'::jsonb,
    -- {table: {identifier_kind: column}} e.g. {"customers": {"email": "email"}}
    identifier_columns     jsonb not null default '{}'::jsonb,
    -- Columns returned for an ACCESS request, per table. Data minimization (§18):
    -- the connector selects these, never SELECT *.
    returnable_columns     jsonb not null default '{}'::jsonb,
    -- Execution is opt-in and separately credentialed.
    allow_execution        boolean not null default false,
    write_credential_ref   text,      -- env/secret NAME of the WRITE role -- never the secret
    -- Columns an erasure may null/anonymize, per table. A deletion plan that needs a
    -- column outside this list becomes a blocked action, not a silent partial.
    erasable_columns       jsonb not null default '{}'::jsonb,
    enabled                boolean not null default true,
    created_at             timestamptz not null default now(),
    updated_at             timestamptz not null default now(),
    unique (org_id, data_source_id)
);
create index if not exists dsr_source_auth_org_id_idx on dsr_source_authorizations (org_id);

-- ── The DSR case -- the central business object (§11) ────────────────────────────
-- Every other table in this migration references it. The status column IS the case
-- lifecycle (§12); transitions are enforced in app/agents/dsr/services/lifecycle.py
-- and the check constraint here stops anything outside the vocabulary reaching disk.
create table if not exists dsr_requests (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    reference              text not null,             -- human-quotable case ref, e.g. DSR-7F3A2B
    -- What the requester asked for, in their own words. Kept verbatim for the audit
    -- trail; classification derives a controlled type from it but never replaces it.
    raw_request            text not null,
    request_type           text not null default 'unclassified'
        check (request_type in ('unclassified','access','correction','deletion','export','information','other')),
    classification_method  text,                      -- 'deterministic' | 'llm' | 'manual'
    classification_confidence  double precision,
    -- Requester identifiers. Deliberately narrow: an email/phone is what a search
    -- matches on. No name, no address, no free-form PII beyond raw_request.
    requester_email        text,
    requester_phone        text,
    requester_reference    text,                      -- customer/employee/account id
    status                 text not null default 'received'
        check (status in (
            'received','identity_pending','identity_verified','classified',
            'searching','search_completed','review_required','approval_required',
            'approved','executing','execution_verified','response_pending','completed',
            'failed','rejected','partially_completed','escalated','cancelled','expired'
        )),
    -- Set whenever the case stops for a reason a human must read (§38). Never NULL
    -- on a failed/blocked case -- "no silent failures" is enforced by the service
    -- layer, and this column is where the reason lands.
    error_code             text,
    error_detail           text,
    -- SLA (§36). due_at is stamped at creation from the org's configured window.
    received_at            timestamptz not null default now(),
    due_at                 timestamptz not null,
    sla_breached           boolean not null default false,
    escalated_at           timestamptz,
    -- Idempotent intake: a retried POST /requests with the same key returns the
    -- existing case rather than opening a second one for the same person.
    idempotency_key        text,
    created_by_user_id     uuid,
    closed_at              timestamptz,
    created_at             timestamptz not null default now(),
    updated_at             timestamptz not null default now(),
    unique (org_id, reference)
);
create index if not exists dsr_requests_org_id_idx on dsr_requests (org_id);
create index if not exists dsr_requests_org_status_idx on dsr_requests (org_id, status);
create index if not exists dsr_requests_due_at_idx on dsr_requests (due_at) where sla_breached = false;
-- Partial unique index, not a table constraint: multiple cases may legitimately have
-- no idempotency key, and NULLs would not collide under a plain unique(org_id, key)
-- on every Postgres version's semantics we want to depend on.
create unique index if not exists dsr_requests_idempotency_idx
    on dsr_requests (org_id, idempotency_key) where idempotency_key is not null;

-- ── Identity verification -- the gate before any sensitive search (§13) ──────────
-- Minimum necessary metadata only: the challenge is stored as a SHA-256 hash, never
-- in the clear, so a database dump cannot be replayed to pass verification.
create table if not exists dsr_identity_verifications (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    request_id             uuid not null references dsr_requests(id) on delete cascade,
    method                 text not null
        check (method in ('email_challenge','manual','external')),
    status                 text not null default 'pending'
        check (status in ('pending','in_progress','verified','failed','expired','manually_verified')),
    challenge_hash         text,                      -- sha256(challenge) -- never the challenge
    attempts               integer not null default 0,
    max_attempts           integer not null default 5,
    expires_at             timestamptz,
    verified_at            timestamptz,
    -- Who attested, for 'manual'/'manually_verified'. Verification without an actor
    -- or an evidence note is not accepted by the service layer (§13).
    verified_by_user_id    uuid,
    evidence_note          text,
    created_at             timestamptz not null default now(),
    updated_at             timestamptz not null default now()
);
create index if not exists dsr_identity_org_id_idx on dsr_identity_verifications (org_id);
create index if not exists dsr_identity_request_idx on dsr_identity_verifications (request_id);

-- ── One row per search execution across one source ───────────────────────────────
create table if not exists dsr_search_runs (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    request_id             uuid not null references dsr_requests(id) on delete cascade,
    data_source_id         uuid references ropa_data_sources(id),
    source_name            text not null,
    status                 text not null default 'pending'
        check (status in ('pending','running','completed','no_match','multiple_matches','failed')),
    -- Which identifiers were actually used, so a reviewer can see what was searched
    -- on without the values themselves being re-derivable from this row alone.
    identifier_kinds       jsonb not null default '[]'::jsonb,
    tables_searched        jsonb not null default '[]'::jsonb,
    match_count            integer not null default 0,
    distinct_subject_count integer not null default 0,
    error_code             text,
    error_detail           text,
    job_id                 uuid,                      -- agent_jobs.id, for tracing (§29)
    correlation_id         text,
    started_at             timestamptz,
    completed_at           timestamptz,
    created_at             timestamptz not null default now()
);
create index if not exists dsr_search_runs_org_id_idx on dsr_search_runs (org_id);
create index if not exists dsr_search_runs_request_idx on dsr_search_runs (request_id);

-- ── Evidence: every match, with everything needed to justify it (§17, §19) ───────
create table if not exists dsr_evidence (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    request_id             uuid not null references dsr_requests(id) on delete cascade,
    search_run_id          uuid not null references dsr_search_runs(id) on delete cascade,
    -- Which source this record came from. Execution addresses the record through
    -- its evidence rather than by re-running the search, so the evidence has to
    -- carry the source id, not just its display name.
    data_source_id         uuid references ropa_data_sources(id),
    source_name            text not null,
    schema_name            text,
    table_name             text not null,
    -- The column the match was made ON, and the value's identifier kind. The matched
    -- VALUE is not repeated here -- it is the requester's own identifier, already on
    -- dsr_requests, and copying it per row multiplies PII for no benefit (§18).
    matched_column         text not null,
    identifier_kind        text not null,             -- 'email' | 'phone' | 'reference'
    match_type             text not null
        check (match_type in ('exact','normalized_exact','partial','fuzzy')),
    confidence             double precision not null default 1.0,
    -- Primary-key reference of the matched row, as {column: value}. This is how
    -- execution later addresses the exact record -- never a re-run of the search.
    record_reference       jsonb not null,
    -- The minimized, returnable projection of the record for an ACCESS response.
    -- Only columns in returnable_columns reach this field.
    record_snapshot        jsonb,
    -- Populated from Agent 2's classification where the column is known to it, so a
    -- reviewer sees "Contact Data" not just "email" (§9). NULL when Agent 2 has not
    -- discovered this source -- absence of a ROPA label never blocks a DSR.
    ropa_category          text,
    observed_at            timestamptz not null default now(),
    created_at             timestamptz not null default now()
);
create index if not exists dsr_evidence_org_id_idx on dsr_evidence (org_id);
create index if not exists dsr_evidence_request_idx on dsr_evidence (request_id);
create index if not exists dsr_evidence_search_run_idx on dsr_evidence (search_run_id);

-- ── Action plan: what is PROPOSED, before anything is done (§21) ─────────────────
create table if not exists dsr_action_plans (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    request_id             uuid not null references dsr_requests(id) on delete cascade,
    version                integer not null default 1,
    status                 text not null default 'draft'
        check (status in ('draft','review_required','approved','rejected','superseded','executed','partially_executed')),
    summary                text not null,
    -- Constraint evaluation output (§20), kept separate from the actions themselves
    -- so SYSTEM FACT and POLICY INTERPRETATION never merge into one field.
    constraints_evaluated  jsonb not null default '[]'::jsonb,
    requires_approval      boolean not null default true,
    created_at             timestamptz not null default now(),
    updated_at             timestamptz not null default now(),
    unique (request_id, version)
);
create index if not exists dsr_action_plans_org_id_idx on dsr_action_plans (org_id);
create index if not exists dsr_action_plans_request_idx on dsr_action_plans (request_id);

-- ── One proposed operation against one record ────────────────────────────────────
create table if not exists dsr_actions (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    request_id             uuid not null references dsr_requests(id) on delete cascade,
    plan_id                uuid not null references dsr_action_plans(id) on delete cascade,
    evidence_id            uuid references dsr_evidence(id),
    data_source_id         uuid references ropa_data_sources(id),
    source_name            text not null,
    table_name             text not null,
    record_reference       jsonb not null,
    operation              text not null
        check (operation in ('disclose','update_field','anonymize_field','delete_record','retain','no_op')),
    -- For update_field: {column: new_value}. For anonymize_field: {column: null}.
    operation_payload      jsonb not null default '{}'::jsonb,
    reason                 text not null,
    expected_result        text not null,
    risk                   text not null default 'medium'
        check (risk in ('low','medium','high')),
    requires_approval      boolean not null default true,
    -- 'blocked' means a constraint forbids it; the action stays visible with its
    -- reason rather than being dropped from the plan (§47).
    status                 text not null default 'proposed'
        check (status in ('proposed','approved','rejected','blocked','executing','executed','failed','skipped')),
    blocked_reason         text,
    created_at             timestamptz not null default now(),
    updated_at             timestamptz not null default now()
);
create index if not exists dsr_actions_org_id_idx on dsr_actions (org_id);
create index if not exists dsr_actions_request_idx on dsr_actions (request_id);
create index if not exists dsr_actions_plan_idx on dsr_actions (plan_id);

-- ── Approval decisions -- append-only, like Agent 1's approvals table ────────────
-- Separate from Agent 1's `approvals`: that table's finding_id is a NOT NULL FK to
-- consent_findings, so a DSR action cannot be recorded there without altering an
-- Agent 1 table. Agent 2 faced the same constraint and made the same call. Both
-- write through the SHARED audit_service, which is the actual common infrastructure.
create table if not exists dsr_approvals (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    request_id             uuid not null references dsr_requests(id) on delete cascade,
    plan_id                uuid references dsr_action_plans(id),
    action_id              uuid references dsr_actions(id),
    decision               text not null
        check (decision in ('approved','rejected','request_more_information','edited','escalated')),
    reason                 text,
    edited_payload         jsonb,
    reviewer_user_id       uuid not null,
    -- Approval is not valid forever. Execution re-checks this (§24, §39): an approval
    -- older than its expiry cannot authorize a write.
    expires_at             timestamptz,
    created_at             timestamptz not null default now()
);
create index if not exists dsr_approvals_org_id_idx on dsr_approvals (org_id);
create index if not exists dsr_approvals_request_idx on dsr_approvals (request_id);
create index if not exists dsr_approvals_action_idx on dsr_approvals (action_id);

-- ── Execution records -- the idempotency ledger (§25) ────────────────────────────
-- One row per attempt to perform one action. The unique index on (org_id,
-- idempotency_key) is what makes a double-click, a retry, or a worker restart
-- return the previous verified result instead of performing the action twice.
create table if not exists dsr_executions (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    request_id             uuid not null references dsr_requests(id) on delete cascade,
    action_id              uuid not null references dsr_actions(id) on delete cascade,
    idempotency_key        text not null,
    status                 text not null default 'pending'
        check (status in ('pending','running','succeeded','failed','verified','verification_failed','partial')),
    -- What the connector reported, and what a fresh read-back afterwards found.
    -- A success claim needs both: "the command returned OK" is not verification (§24).
    rows_affected          integer,
    connector_response     jsonb,
    verification_status    text
        check (verification_status in ('pending','passed','failed','not_applicable')),
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
create index if not exists dsr_executions_org_id_idx on dsr_executions (org_id);
create index if not exists dsr_executions_request_idx on dsr_executions (request_id);
create index if not exists dsr_executions_action_idx on dsr_executions (action_id);
create unique index if not exists dsr_executions_idempotency_idx
    on dsr_executions (org_id, idempotency_key);

-- ── The response returned to the requester ───────────────────────────────────────
create table if not exists dsr_responses (
    id                     uuid primary key default gen_random_uuid(),
    org_id                 uuid not null,
    request_id             uuid not null references dsr_requests(id) on delete cascade,
    version                integer not null default 1,
    status                 text not null default 'draft'
        check (status in ('draft','review_required','approved','sent')),
    -- The body a human will read, and the machine-checkable facts it was built from.
    -- grounded_facts is populated from dsr_evidence and dsr_executions ONLY -- if a
    -- sentence in body_text is not traceable to a row here, it is not a DSR fact.
    body_text              text not null,
    grounded_facts         jsonb not null default '[]'::jsonb,
    -- Set when the LLM drafted the prose. NULL means fully deterministic. Either way
    -- the facts come from evidence, never from the model.
    drafted_by_model       text,
    approved_by_user_id    uuid,
    sent_at                timestamptz,
    created_at             timestamptz not null default now(),
    updated_at             timestamptz not null default now(),
    unique (request_id, version)
);
create index if not exists dsr_responses_org_id_idx on dsr_responses (org_id);
create index if not exists dsr_responses_request_idx on dsr_responses (request_id);

-- ── Append-only enforcement on the decision record ───────────────────────────────
-- Same guarantee 0004 gave audit_logs and approvals, via the same shared function: an
-- approval may be added but never rewritten or erased, by any role, from anywhere.
-- dsr_executions is NOT append-only -- an execution legitimately transitions
-- pending -> running -> succeeded -> verified in place, and that row IS the
-- idempotency ledger, so it must be updatable.
drop trigger if exists dsr_approvals_append_only on dsr_approvals;
create trigger dsr_approvals_append_only
    before update or delete on dsr_approvals
    for each row execute function reject_append_only_mutation();

-- ── Row-level security ───────────────────────────────────────────────────────────
alter table dsr_source_authorizations   enable row level security;
alter table dsr_requests                enable row level security;
alter table dsr_identity_verifications  enable row level security;
alter table dsr_search_runs             enable row level security;
alter table dsr_evidence                enable row level security;
alter table dsr_action_plans            enable row level security;
alter table dsr_actions                 enable row level security;
alter table dsr_approvals               enable row level security;
alter table dsr_executions              enable row level security;
alter table dsr_responses               enable row level security;

do $$
declare
    t text;
begin
    foreach t in array array[
        'dsr_source_authorizations','dsr_requests','dsr_identity_verifications',
        'dsr_search_runs','dsr_evidence','dsr_action_plans','dsr_actions',
        'dsr_approvals','dsr_executions','dsr_responses'
    ] loop
        if not exists (
            select 1 from pg_policies
            where schemaname = current_schema() and tablename = t and policyname = 'tenant_isolation'
        ) then
            execute format('create policy tenant_isolation on %I using (org_id = current_org_id())', t);
        end if;
    end loop;
end $$;
