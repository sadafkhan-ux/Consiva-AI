-- Give a blocked DSR action a second, requester-facing explanation.
--
-- `blocked_reason` is written for the reviewer: it names tables, columns, credentials
-- and configuration, because that is what makes it actionable. It was also being
-- copied verbatim into the response sent to the data subject, so a person asking for
-- their data deleted was reading sentences like "an administrator must enable
-- execution and configure a write credential" -- our internal state, telling them
-- nothing they can act on.
--
-- `requester_explanation` is the sentence the response uses instead. Where the
-- blocker is a POLICY (a retention requirement) it discloses the substance in full,
-- because why an erasure was refused on policy grounds is precisely what a data
-- principal is entitled to know. Where the blocker is our own configuration, it says
-- the record was not changed and that a human is handling it, and nothing more.
--
-- Additive and nullable; no existing row changes meaning.

alter table dsr_actions add column if not exists requester_explanation text;
