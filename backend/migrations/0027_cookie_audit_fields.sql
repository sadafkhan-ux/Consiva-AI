-- 0027: Cookie audit fields for the Consent Agent's cookie inventory.
--
-- The scanner already reads these from the browser (Playwright cookies()) and from
-- each page's Set-Cookie responses; until now it discarded them. All additive and
-- nullable/defaulted, so rows written before this migration stay valid -- they read as
-- "not recorded", which the report shows as "Not available" rather than a default.
--
--   secure / http_only / same_site  the browser's own attribute values
--   observations                     one entry per consent-state pass that saw the
--                                    cookie: {consent_state, page_url, observed_at,
--                                    method, source_request_url, candidate_page_urls}
--                                    (scanner/schemas.py CookieObservation)

alter table cookies add column if not exists secure       boolean;
alter table cookies add column if not exists http_only    boolean;
alter table cookies add column if not exists same_site    text;
alter table cookies add column if not exists observations jsonb not null default '[]'::jsonb;
