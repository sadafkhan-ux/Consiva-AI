-- Agent 5 (Regulatory Watch) persistence: approved sources, what was collected from
-- them, what changed, and what an organisation decided about it.
--
-- Additive only; idempotent, same convention as every other migration here. Nothing
-- touches an Agent 1 (consent), Agent 2 (ropa), Agent 3 (dsr) or Agent 4 (breach)
-- table.
--
-- FIVE DECISIONS -- read before adding tables:
--
-- 1. A COLLECTION IS NOT A BASELINE. `regwatch_collections` records every attempt to
--    fetch a source, including the ones that FAILED. `regwatch_baselines` records the
--    content a human has accepted as the reference point. Keeping them apart is what
--    makes the spec's last guardrail enforceable -- "the system must not silently
--    report a source as current when collection failed". A failed collection is a row
--    with status='failed' and an error; it can never be mistaken for "nothing
--    changed", because nothing changed is a comparison against a baseline and a
--    failed collection has no content to compare.
--
-- 2. THE BASELINE ONLY MOVES ON A HUMAN DECISION. `approved_by_user_id` is NOT NULL.
--    If the agent advanced the baseline itself, a change would be reported once and
--    then silently absorbed; the next reviewer would see a source that looks current
--    and never learn what it absorbed. So a new baseline is created only when someone
--    accepts a change, and until then every re-check keeps reporting the same
--    outstanding diff. Re-reporting is the correct behaviour, not a bug to suppress.
--
-- 3. RELEVANCE AND IMPACT CARRY CONFIDENCE, NOT BOOLEANS. Whether a regulatory change
--    applies to this organisation is a judgement, and the platform does not let a
--    machine assert judgements. Both use the same four-level vocabulary Agent 4 uses
--    (confirmed / probable / possible / unknown), and `confirmed` is reachable only
--    through a person, enforced in the service layer exactly as it is for an incident
--    finding.
--
-- 4. IMPACT POINTS AT THE OTHER AGENTS BY REFERENCE, NOT BY COPY.
--    `regwatch_impacts.target_kind` + `target_id` name a ROPA record, a consent
--    finding, a DSR configuration or an incident. No regulatory table duplicates
--    another agent's data, so nothing can drift out of step with it.
--
-- 5. NO SEPARATE AUDIT TABLE. Who did what in Consiva is audit_logs filtered by
--    (entity_type='regwatch_finding', entity_id=finding_id) -- indexed by 0001 and
--    append-only by trigger since 0004, the same arrangement Agents 3 and 4 use.
--    `regwatch_changes` is a different thing: it is what changed IN THE WORLD, which
--    is a claim about a regulator's website rather than a record of our own actions.

-- ── Approved sources ────────────────────────────────────────────────────────
-- Only these are ever monitored. `credential_ref` names an environment variable, the
-- same secret-by-reference pattern Agents 2 and 3 use -- the value never lands here.
create table if not exists regwatch_sources (
    id                  uuid primary key default gen_random_uuid(),
    org_id              uuid not null,
    name                text not null,
    url                 text not null,
    connector           text not null default 'http',
    jurisdiction        text not null,
    topic               text,
    authority           text,
    -- How often the worker should re-check. Minutes, so a daily source and an hourly
    -- one use one unit and the sweep has one comparison to make.
    check_interval_minutes integer not null default 1440,
    credential_ref      text,
    config              jsonb not null default '{}'::jsonb,
    enabled             boolean not null default true,
    -- Set by the sweep so a source that has never been reached is visibly different
    -- from one that is up to date.
    last_checked_at     timestamptz,
    last_success_at     timestamptz,
    consecutive_failures integer not null default 0,
    created_by_user_id  uuid,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    constraint regwatch_sources_connector_check
        check (connector in ('http', 'rss', 'manual_upload')),
    constraint regwatch_sources_interval_check
        check (check_interval_minutes between 5 and 525600)
);
create unique index if not exists ux_regwatch_sources_org_name on regwatch_sources (org_id, name);
create index if not exists ix_regwatch_sources_org on regwatch_sources (org_id);
create index if not exists ix_regwatch_sources_due
    on regwatch_sources (last_checked_at) where enabled;

-- ── Collections: every attempt, including the failures ──────────────────────
create table if not exists regwatch_collections (
    id                uuid primary key default gen_random_uuid(),
    org_id            uuid not null,
    source_id         uuid not null references regwatch_sources(id) on delete cascade,
    status            text not null default 'pending',
    -- The evidence. `content_hash` is what change detection compares; `content_text`
    -- is kept so a reviewer can read what was actually retrieved rather than trusting
    -- a diff summary about it.
    content_hash      text,
    content_text      text,
    content_bytes     integer,
    http_status       integer,
    retrieved_at      timestamptz,
    -- Populated ONLY on failure, and the reason a failed collection is visible rather
    -- than silently absorbed.
    error_code        text,
    error_detail      text,
    job_id            uuid,
    created_at        timestamptz not null default now(),
    constraint regwatch_collections_status_check
        check (status in ('pending', 'collecting', 'collected', 'failed', 'skipped')),
    -- A collected row must carry content; a failed row must carry a reason. Enforced
    -- here so neither can be half-written by a future caller.
    constraint regwatch_collections_collected_has_content
        check (status <> 'collected' or (content_hash is not null and retrieved_at is not null)),
    constraint regwatch_collections_failed_has_reason
        check (status <> 'failed' or error_code is not null)
);
create index if not exists ix_regwatch_collections_source on regwatch_collections (source_id, created_at desc);
create index if not exists ix_regwatch_collections_org on regwatch_collections (org_id);

-- ── Baselines: the accepted reference point, moved only by a person ─────────
create table if not exists regwatch_baselines (
    id                 uuid primary key default gen_random_uuid(),
    org_id             uuid not null,
    source_id          uuid not null references regwatch_sources(id) on delete cascade,
    collection_id      uuid not null references regwatch_collections(id),
    version            integer not null,
    content_hash       text not null,
    -- NOT NULL on purpose: see decision 2 in the header. There is no code path that
    -- advances a baseline without a named person behind it.
    approved_by_user_id uuid not null,
    approved_at        timestamptz not null default now(),
    superseded_at      timestamptz,
    note               text,
    created_at         timestamptz not null default now()
);
create unique index if not exists ux_regwatch_baselines_version on regwatch_baselines (source_id, version);
create index if not exists ix_regwatch_baselines_current
    on regwatch_baselines (source_id) where superseded_at is null;
create index if not exists ix_regwatch_baselines_org on regwatch_baselines (org_id);

-- ── Detected changes ────────────────────────────────────────────────────────
create table if not exists regwatch_changes (
    id               uuid primary key default gen_random_uuid(),
    org_id           uuid not null,
    source_id        uuid not null references regwatch_sources(id) on delete cascade,
    -- Null when the source has no baseline yet: the first successful collection is a
    -- change from nothing, which is a real event a reviewer should see once.
    from_baseline_id uuid references regwatch_baselines(id),
    to_collection_id uuid not null references regwatch_collections(id),
    change_kind      text not null,
    -- Cheap, deterministic shape of the diff. The narrative explanation lives on the
    -- finding, where it can be attributed to a model and reviewed.
    added_lines      integer not null default 0,
    removed_lines    integer not null default 0,
    diff_excerpt     text,
    detected_at      timestamptz not null default now(),
    created_at       timestamptz not null default now(),
    constraint regwatch_changes_kind_check
        check (change_kind in ('first_capture', 'content_changed', 'unreachable', 'no_change'))
);
create index if not exists ix_regwatch_changes_source on regwatch_changes (source_id, detected_at desc);
create index if not exists ix_regwatch_changes_org on regwatch_changes (org_id);

-- ── Findings: the interpreted, reviewable item ──────────────────────────────
create table if not exists regwatch_findings (
    id                  uuid primary key default gen_random_uuid(),
    org_id              uuid not null,
    change_id           uuid not null references regwatch_changes(id) on delete cascade,
    source_id           uuid not null references regwatch_sources(id) on delete cascade,
    reference           text not null,
    status              text not null default 'detected',
    summary             text,
    jurisdiction        text,
    -- Is this change relevant to this organisation, and how sure are we.
    relevance           text not null default 'unknown',
    relevance_confidence text not null default 'unknown',
    relevance_reason    text,
    -- What it might mean, and how urgent.
    impact_summary      text,
    priority            text,
    priority_confidence text not null default 'unknown',
    -- Grounding. `citations` holds the approved source passages the interpretation
    -- rests on; `drafted_by_model` records which model wrote the narrative, or NULL
    -- where the text was assembled deterministically.
    citations           jsonb not null default '[]'::jsonb,
    grounded_facts      jsonb not null default '[]'::jsonb,
    drafted_by_model    text,
    -- What could NOT be established. Present for the same reason Agent 4 records
    -- gaps: a partial assessment must not read as a complete one.
    open_questions      jsonb not null default '[]'::jsonb,
    requires_human_review boolean not null default true,
    error_code          text,
    error_detail        text,
    reviewed_by_user_id uuid,
    reviewed_at         timestamptz,
    closed_at           timestamptz,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    constraint regwatch_findings_status_check
        check (status in (
            'detected', 'assessing', 'review_required', 'approved', 'dismissed',
            'action_open', 'closed', 'superseded', 'failed'
        )),
    constraint regwatch_findings_relevance_check
        check (relevance in ('relevant', 'not_relevant', 'undetermined')),
    constraint regwatch_findings_relevance_confidence_check
        check (relevance_confidence in ('confirmed', 'probable', 'possible', 'unknown')),
    constraint regwatch_findings_priority_confidence_check
        check (priority_confidence in ('confirmed', 'probable', 'possible', 'unknown')),
    constraint regwatch_findings_priority_check
        check (priority is null or priority in ('low', 'medium', 'high', 'critical'))
);
create unique index if not exists ux_regwatch_findings_reference on regwatch_findings (org_id, reference);
create index if not exists ix_regwatch_findings_org_status on regwatch_findings (org_id, status);
create index if not exists ix_regwatch_findings_change on regwatch_findings (change_id);
create index if not exists ix_regwatch_findings_source on regwatch_findings (source_id);

-- ── Impact: what in THIS organisation the change may touch ──────────────────
-- By reference. `target_kind` says which agent owns the thing named by `target_id`,
-- so nothing here duplicates another agent's data.
create table if not exists regwatch_impacts (
    id            uuid primary key default gen_random_uuid(),
    org_id        uuid not null,
    finding_id    uuid not null references regwatch_findings(id) on delete cascade,
    target_kind   text not null,
    target_id     uuid,
    target_label  text not null,
    confidence    text not null default 'possible',
    rationale     text,
    -- How this link was arrived at, so a reviewer can tell a rule from a model.
    derived_from  text not null default 'rule',
    created_at    timestamptz not null default now(),
    constraint regwatch_impacts_kind_check
        check (target_kind in (
            'ropa_record', 'ropa_data_source', 'consent_finding', 'consent_website',
            'dsr_configuration', 'incident_case', 'policy', 'control', 'other'
        )),
    constraint regwatch_impacts_confidence_check
        check (confidence in ('confirmed', 'probable', 'possible', 'unknown')),
    constraint regwatch_impacts_derived_check
        check (derived_from in ('rule', 'ropa_metadata', 'model', 'manual'))
);
create index if not exists ix_regwatch_impacts_finding on regwatch_impacts (finding_id);
create index if not exists ix_regwatch_impacts_org on regwatch_impacts (org_id);

-- ── Approvals: append-only, same shape as Agents 3 and 4 ────────────────────
create table if not exists regwatch_approvals (
    id                 uuid primary key default gen_random_uuid(),
    org_id             uuid not null,
    finding_id         uuid not null references regwatch_findings(id) on delete cascade,
    reviewer_user_id   uuid not null,
    subject            text not null,
    decision           text not null,
    reason             text,
    edited_payload     jsonb,
    expires_at         timestamptz,
    created_at         timestamptz not null default now(),
    constraint regwatch_approvals_subject_check
        check (subject in ('finding', 'baseline', 'action', 'impact')),
    constraint regwatch_approvals_decision_check
        check (decision in ('approved', 'rejected', 'edited', 'dismissed',
                            'request_more_information', 'escalated'))
);
create index if not exists ix_regwatch_approvals_finding on regwatch_approvals (finding_id, created_at desc);
create index if not exists ix_regwatch_approvals_org on regwatch_approvals (org_id);

-- ── Actions: the tracked follow-up work ─────────────────────────────────────
create table if not exists regwatch_actions (
    id               uuid primary key default gen_random_uuid(),
    org_id           uuid not null,
    finding_id       uuid not null references regwatch_findings(id) on delete cascade,
    title            text not null,
    rationale        text not null,
    expected_result  text not null,
    owner_label      text,
    status           text not null default 'open',
    due_at           timestamptz,
    -- Consiva does not perform regulatory work. Like Agent 4's containment, an action
    -- is carried out by a person and attested to.
    completed_by     text,
    completion_note  text,
    completed_at     timestamptz,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now(),
    constraint regwatch_actions_status_check
        check (status in ('open', 'in_progress', 'completed', 'cancelled', 'blocked')),
    constraint regwatch_actions_completed_has_attestation
        check (status <> 'completed' or (completed_by is not null and completion_note is not null))
);
create index if not exists ix_regwatch_actions_finding on regwatch_actions (finding_id);
create index if not exists ix_regwatch_actions_org_status on regwatch_actions (org_id, status);

-- ── Row-level security, forced, on every table ──────────────────────────────
-- FORCE included from the start (migration 0018 had to retrofit it everywhere else).
do $$
declare
    t text;
begin
    foreach t in array array[
        'regwatch_sources', 'regwatch_collections', 'regwatch_baselines',
        'regwatch_changes', 'regwatch_findings', 'regwatch_impacts',
        'regwatch_approvals', 'regwatch_actions'
    ] loop
        execute format('alter table %I enable row level security', t);
        execute format('alter table %I force row level security', t);
        if not exists (select 1 from pg_policies where tablename = t and policyname = 'tenant_isolation') then
            execute format('create policy tenant_isolation on %I using (org_id = current_org_id())', t);
        end if;
        execute format('grant select, insert, update, delete on %I to consiva_app', t);
    end loop;
end $$;

-- ── Append-only: approvals, and the collected evidence ──────────────────────
-- Approvals for the reason Agents 3 and 4 have it: a decision that can be edited
-- afterwards is not a decision anyone can rely on.
--
-- Collections too, which the other agents do not do. The whole agent rests on "this
-- is what the regulator's page said when we fetched it"; a collection that can be
-- rewritten makes every downstream change record unfalsifiable. Sources, findings,
-- impacts and actions all mutate in place by design -- they carry review state.
do $$
declare
    t text;
begin
    foreach t in array array['regwatch_approvals', 'regwatch_collections'] loop
        if not exists (select 1 from pg_trigger
                       where tgname = t || '_append_only' and not tgisinternal) then
            execute format(
                'create trigger %I before update or delete on %I '
                'for each row execute function reject_append_only_mutation()', t || '_append_only', t);
        end if;
    end loop;
end $$;

-- ── The job type the worker will dispatch ───────────────────────────────────
-- Widened the same way 0012 and 0016 did. Note what is NOT here: there is no
-- 'regwatch_act' job. An action is performed by a person and attested to, exactly as
-- Agent 4's containment is, so there is nothing for a worker to carry out.
alter table agent_jobs drop constraint if exists agent_jobs_job_type_check;

alter table agent_jobs add constraint agent_jobs_job_type_check
    check (job_type in (
        -- Agent 1 (Consent)
        'scan',
        'analyze',
        -- Agent 2 (Data Discovery / ROPA)
        'ropa_discovery',
        -- Agent 3 (DSR Fulfillment)
        'dsr_search',
        'dsr_execute',
        -- Agent 4 (Breach Response)
        'incident_analysis',
        -- Agent 5 (Regulatory Watch) -- collection and assessment only; see above.
        'regwatch_collect',
        'regwatch_assess'
    ));
