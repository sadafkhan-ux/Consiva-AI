# How the scanner chooses its five pages

Written 27 Oct 2026 from the code (`backend/app/scanner/crawler.py`, `url_normalize.py`, `sitemap.py`, `scan_profiles.py`). Everything below is what the code does; where something is a limitation it says so.

## The limit

A web scan loads **at most 5 distinct pages** (`MAX_WEB_PAGES = 5` in `scan_profiles.py`, enforced again inside `_crawl_full_site`, so no setting can raise it). A page that fails, times out, redirects away or is robots-disallowed does not use one of the five: the scan may make up to `5 + max(5, 5) = 10` attempts, and up to 30 dead pages (HTTP 404/410) are not counted at all. Up to 4 pages load at the same time, but the entered address is always loaded **alone and first**, so the choice of the other pages is made from everything it links to.

### Why five, and who can change it

* **Intentional.** Five is the public (web-profile) scan's design limit, set in code as `MAX_WEB_PAGES = 5` and described to the reader as the scan limit ("Free scan limit" in the dashboard and the report). The web profile is chosen by the **server** from the endpoint and the caller, never from a number in the request.
* **Configurable only downward.** `CONSENT_WEB_MAX_PAGES` (and the fallback `SCANNER_MAX_PAGES`) accept 1 to 5. A larger value left in an older `.env` is **clamped to 5**, not honoured and not rejected (`config._public_scans_are_capped_at_five_pages`), and the crawler clamps again where pages are launched. So no environment value raises it.
* **Not imposed by time.** The web profile is bounded by the page count, not by a time budget; each page has its own 30 s navigation timeout.
* **The other profile.** An authorised **application** scan (a domain-verified caller) runs under `CONSENT_APP_MAX_PAGES`, whose default 0 means no fixed page cap, bounded by an optional crawl deadline and a 24-hour ceiling.
* The limit was not changed in this work. Raising it would be a product decision (cost per scan, scan duration, and the 2-vCPU capacity measured in `docs/SCALING.md`), not a configuration change.

## Where addresses come from (and only from here)

1. **The entered address** (the "seed"). Always scanned first. It is the only page that gets the screenshot and the Accept and Reject passes.
2. **The site's sitemap.** The `Sitemap:` lines of `robots.txt`, then the conventional `/sitemap.xml`. A sitemap index is followed to at most 10 child sitemaps, 2 levels deep; at most 500 addresses are taken, in the order the file lists them; every address must be on the entered domain.
3. **Every link on a page that has already been scanned** (`<a href>`, from the rendered page, not only the navigation menu: the scanner does not tell the menu from the footer or the body).

No address is guessed. A site with no `/privacy` link and no sitemap entry for it never gets `/privacy` requested.

## What is excluded

* Another host: a subdomain or another website is neither followed nor recorded (`_host_key` must equal the entered host).
* A page the site's `robots.txt` disallows: counted and recorded as skipped (`robots_disallowed`), never loaded.
* A repeat of an address. Two spellings are one page: `/about`, `/about/`, `/about#team`, `/about?utm_source=nav`, `www.` or not. The tracking parameters on the list in `url_normalize.py` are ignored. **New:** a query-string variant of a page that has a path of its own (`/blog?page=2` after `/blog`, `/pricing?plan=a` after `/pricing`) is not scanned again (`query_variant_of_known_page`). A root address and a script address (`/?p=12`, `/index.php?page=privacy`) are never variants, because the query is what selects their content.
* A redirect that ends on an already scanned page, or on another host.

## The order

Each address gets a rank (`priority()` in `url_normalize.py`), judged from its **path and query text**, not from the page's content. Lower is earlier:

| Rank | Kind | Matches (anywhere in the path or query) |
|---|---|---|
| seed | the entered address | always first |
| 0 | privacy and cookie pages | privacy, cookie, gdpr, dpdp, data-protection, datenschutz |
| 1 | terms pages | terms |
| 2 | contact and legal notices | contact, legal, disclaimer, imprint |
| 3 | sign-up, login, checkout | signup, sign-up, register, login, sign-in, signin, checkout, cart, payment, subscribe |
| 4 | about, pricing, product | about, pricing, plans, product, service, solution |
| 5 | any other page | nothing above matched |
| 6 | blog and news | a path **segment** that is blog, news, article, post, insights, press, stories, magazine, tag, category, archive, author (this wins over every keyword above, so `/blog/dpdp-act-compliance-guide` is a blog post, not a privacy page) |

Why this order: with a limit of five, the entered address, the privacy page, the cookie policy and the terms page take four slots, and the fifth goes to the contact page rather than to a second legal notice (a disclaimer or imprint) or to a login or checkout page. Before 27 Oct 2026 the disclaimer, imprint and `/legal` shared rank 1 with the terms page, and the contact page shared rank 2 with login and checkout, so on swaransoft.com a disclaimer took the slot the contact page should have had.

One more rule, added 27 Oct 2026: a page in ranks 0 to 2 only counts as the site's own policy, terms or contact page when its address is **named like one** (`/privacy`, `/privacy-policy`, `/privacy-and-cookies`, `/terms-of-use`, `/contact-us`, or anything under `/legal/`, `/policies/`, `/policy/`). A page that merely contains the word (`/privacy-blueprint`, `/dpdp-and-gdpr-consent`, `/cookie-consent-management`, `/contact-center-software`) ranks 2.5: after every real policy, terms and contact page, before sign-up and login pages (`effective_rank` in `url_normalize.py`). On uat.consiva.ai four product pages that mention DPDP, GDPR or cookies had taken four of the five slots and the real policy, `/legal/privacy-rights-and-grievance`, was not scanned.

Two refinements: a page in a different section of the same host (`/ae-en/` when `/in-en/` was entered) is ranked half a step behind the same kind of page in the entered section; and **ties are now broken by the address itself** (`tie_break_key`): shallower path first, then an address without a query string before one with, then alphabetical. Before this change a tie was broken by the order in which addresses were *discovered*, which depended on the sitemap's order and on which page finished loading first. Now the same site gives the same pages on every scan.

## Why these pages

The scanner is a consent and privacy audit, so a page is worth what it settles: the privacy and cookie pages settle the notice and cookie-disclosure checks, the terms page the legal notice, a page with a form (contact, sign-up) the notice-at-collection checks. Blog posts add the least, so they go last. This is a ranking of evidence value, not of general importance.

## When a priority page is not available

A page that fails to load, times out or answers with an error is recorded as a page row (`failed` or `timeout`, with its HTTP status and the reason) and the next address in the order takes its place, up to the attempt ceiling. A slow site (3 timeouts in a row) stops the crawl early and says so.

## What the report shows

The two-page report lists each page on page 1, **why it was chosen** (its kind and where it was found: sitemap or a link on a scanned page; the reason is worked out when the evidence is read, from the address and how it was found, never stored as a claim) and how it went, then says how many pages were scanned of how many were found and what was skipped, and why. "Found" is every distinct same-domain address the crawler saw (sitemap and links), including robots-disallowed ones; the crawler does not track a separate "eligible" figure, so the report states the robots-blocked count rather than inventing one.

## Limitations

* The kind of a page is a keyword match on its address. A page named in another language (`/datenschutz` is matched, `/confidentialite` is not) or with an opaque address (`/p/123`) is rank 4.
* Login, cart and checkout pages share rank 2 with contact pages.
* Menus are not distinguished from other links.
* A sitemap contributes at most its first 500 addresses; a privacy page listed after that is found only if some scanned page links to it.
* Five pages are never the whole site. Accept and Reject are tested on the homepage only.
