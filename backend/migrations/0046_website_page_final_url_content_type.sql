-- 0046: where each scanned page ended up, and what the server said it was.
--
-- website_pages already holds the address the scanner asked for, the page title and the HTTP status. These two columns add the address the
-- navigation ended on after redirects (final_url) and the Content-Type header of the answer (content_type), so the evidence can show a
-- redirect or a non-HTML answer instead of leaving it to be guessed.
--
-- Additive and nullable: nothing existing is altered, and scans made before this migration keep NULL here (the API returns null, never a guess).
alter table website_pages add column if not exists final_url text;
alter table website_pages add column if not exists content_type text;
