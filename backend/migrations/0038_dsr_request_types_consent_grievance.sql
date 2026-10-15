-- DSR (Agent 3): two new request types, `withdraw_consent` and `grievance`.
--
-- Withdrawing consent is not erasure, and a complaint is not a data lookup. Both used to
-- be forced into the existing vocabulary -- a consent withdrawal was classified as
-- `deletion` and could plan an erasure nobody asked for; a grievance fell into `other`
-- and was sent into a data search. They get their own types so neither can be mistaken
-- for the request it resembles.
--
-- The CHECK on dsr_requests.request_type is replaced, not edited in place (Postgres cannot
-- alter a CHECK). It is looked up by what it constrains rather than by name, because the
-- original was declared inline on the column and its generated name is not something this
-- file should assume. Additive in effect: every value that was legal stays legal, so no
-- existing row can violate the new constraint.

do $$
declare
    c record;
begin
    for c in
        select conname
        from pg_constraint
        where conrelid = 'dsr_requests'::regclass
          and contype = 'c'
          and pg_get_constraintdef(oid) ilike '%request_type%'
    loop
        execute format('alter table dsr_requests drop constraint %I', c.conname);
    end loop;
end
$$;

alter table dsr_requests add constraint dsr_requests_request_type_check
    check (request_type in (
        'unclassified','access','correction','deletion','export','information',
        'withdraw_consent','grievance','other'
    ));
