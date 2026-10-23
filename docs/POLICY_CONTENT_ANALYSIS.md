# Privacy-policy content analysis and sector assessment (Consent Agent) — 2026-10-22

The scan shows what a site **does** (cookies, trackers, requests, forms, consent behaviour). This adds what it **says**: the privacy and cookie
policies, read in full, with the words to prove it. It sits beside the existing DPDP Compliance Score and **never changes it**.

## What was already there, and what was missing

| Capability | Before | Now |
|---|---|---|
| Live scan, three consent states, cookies, trackers, requests, forms, scan coverage | Implemented | Unchanged |
| DPDP score with per-check legal basis | Implemented | Unchanged |
| Policy text | Partial: only the first 2,000 characters, and none at all when the apex and `www.` spellings differed; the same policy listed twice | Full text (up to 60,000 characters), found across spellings, listed once |
| Policy content analysis with verified quotations | Missing | `app/policy` |
| Actionable fixes tied to legal provisions | Partial (score checks only) | Per policy point, with the provisions |
| Sector classification | Partial (8 sectors, merged categories, host-name-only matches possible) | 14 sectors, evidence-based, host name never enough on its own |
| Transparent limitations | Partial | In the dashboard section, the PDF appendix and the API result |

## How the policy analysis works

1. **Text.** The crawler keeps the rendered text of policy pages it visited. A policy the crawl did not reach is fetched as a plain page (every
   hop through the same SSRF guard). Terms pages are not read. Two URLs with identical text are read once.
2. **Model.** The existing Claude-first client (NVIDIA/Groq fallback) is asked, for each applicable requirement, for the exact passage that states
   it. It is told never to paraphrase.
3. **Verification.** A passage counts only if it is found in the policy text, character for character after whitespace, case, quote and dash styles
   are normalised. A paraphrase, an invention or a stitched quote is rejected, counted, and the requirement is reported as *not found in the policy
   text*. Absence is never reported as a violation.
4. **Checks in code, not by the model.** A grievance or DPO contact needs an email, phone number or form beside it; the grievance response period
   is read in days and judged against Rule 14(3)'s ninety days; a rights request needs a means.
5. **Legal references** come from the catalogue (`app/policy/requirements.py`), never from the model.
6. **Fail-soft.** No policy page, unreadable text, or an unreachable model gives an `unavailable` result with the reason. The stage always
   completes (the console reads a *failed* stage outside the analysis stages as the whole run failing), and the scan, score and findings are untouched.

Result: `GET /api/v1/consent/scans/{id}/policy-analysis` (`analysed`, `unavailable` or `not_run`). Stored in the `policy_analysis` stage metadata;
migration `0039` widens the stage CHECK, as 0021 and 0028 did. Completeness = (quoted + ½ × partly stated) ÷ applicable points.

## Legal mappings, checked against the Act and Rules in `knowledge_sources/`

Every provision in the catalogue was read in the Act or Rules text. Two kinds are kept apart:

* **Required** (the Act or Rules say this must be given or published): notice contents s.5(1) and r.3; withdrawal s.6(4); rights s.11, s.12, s.14
  and r.14(1), r.14(4); grievance s.13, s.8(10), r.14(3); complaint to the Board s.5(1)(iii), r.3(c)(iii); DPO or answering person s.8(9);
  notice in English or an Eighth Schedule language s.5(3); children's data s.9, r.10 (where plausible).
* **Good practice** (a duty stands behind it, but the policy is not stated to have to carry it): processors s.8(2), cross-border s.16 and r.15,
  retention s.8(7), security s.8(5) and r.6, breach s.8(6) and r.7, cookies s.5(1)(i), grounds s.6(1) and s.7.

Differences from the Digital Anumati report on the same site, found while checking it against the text:

* *Automated decision disclosure* is not in the Act (it is a GDPR idea; s.8(3)(a) only asks for accurate data); not included.
* *Section 6(9)* is Consent Manager registration, not a cookie consent manager; not used.
* *All 22 languages when requested* overstates s.5(3), which gives the person the **option** to read the notice in English or any Eighth Schedule language.
* The grievance response period is judged against **Rule 14(3)** (not more than ninety days).
* A DPO is required only of a notified Significant Data Fiduciary (s.10); for everyone else s.8(9) asks for the business contact of a DPO **or** a
  person able to answer, which is what is checked.

## Sectors

Fourteen: healthcare, education, BFSI, insurance, real estate, retail and e-commerce, manufacturing, hospitality, IT services, SaaS, technology
and AI, government vendor, legal services, and general. Evidence is page titles and paths, form field names, shop and booking platforms, shop
cookies, with the host name only as a hint something else must agree with (a payment gateway is not evidence: any business takes payments).
The organisation may declare its own sector (`business_sector`); the scan's reading is shown beside it. See `app/scoring/sector.py`, which also
lists the provisions that genuinely depend on a sector (Third and Fourth Schedules, s.10, s.8(7), s.17(1)(f), s.7, s.17(2)/(4)). Everywhere else the
Act applies alike, and the notes say so.

## Known limits

* The crawler stores page titles, not descriptions or headings, so a site whose scanned pages are mostly policy pages cannot be classified.
* A policy that is a PDF, behind a login, an image, or loaded after a click is not read.
* A bot-protected site (403 or "Access Denied") yields no policy text; the result says so.
* The PDF appendix is complete, so it is long for a large site (a scan with 54 forms and hundreds of requests runs to dozens of pages).

## Report layout (PDF)

The report reads in order: numbered sections 1 to 10 (score and summary, readiness, privacy areas, top findings; consent behaviour, technologies,
privacy elements, the table of every check; pages scanned and scan review), then the Evidence appendix, lettered A onward (sector, policy analysis,
consent banner, forms, cookies, third-party requests, policies, services, compliance findings). A contents on page 1 and an index at the start of the
appendix give the page of each section; both are built from the sections as drawn, so the page numbers are true.

* Nothing is cut with an ellipsis. Blocks grow to fit their text and flow onto the next page (the table of checks, the top-finding cards, the
  summary tiles, the technology rows). A heading is never left alone at the foot of a page.
* Legal references are spelt one way everywhere (`frontend/src/report/legalRefs.ts`): "DPDP Act, 2023, Section 5(1)(i)", "DPDP Rules, 2025, Rule 3(b)(i)",
  "Fourth Schedule, Part A, item 3"; the narrow check table uses "Act, Section 5(1)(i)". Only the spelling changes, never the provision.
* The dashboard's policy and sector sections use the same formatter, so they agree with the PDF.
* The PDF has no GDPR or CCPA framework scores: the assessment covers the DPDP Act and Rules only.

## Homepage preview (screenshot)

Every scan photographs the first screen of the homepage it loads, so the dashboard and every PDF show the real site beside the score and a reader can
see at a glance that the right site was scanned.

* **Capture** (`app/scanner/crawler.py`, `_capture_homepage_screenshot`): in the seed page's load, after the page has loaded and settled and before the scroll
  pass, as a 1280x720 JPEG. It only reads the page (no click, scroll, cookie or storage change, no extra request), so the consent tests that follow
  are unaffected; a test with a real Chromium proves cookies, storage, scroll position and the request log are identical before and after, and an A/B
  of repeated scans with the capture on and off showed the same behaviour.
* **Fail-soft**: a timeout, a crash or a blank page becomes a status on the record, never an error. A site that answers the scanner with an error or a
  challenge page (HTTP 4xx/5xx, or a title such as "Access Denied") is recorded as `blocked` and the picture is **not kept**: a block page is not the
  client's website.
* **Storage** (migration `0040`, `scan_screenshots`, one row per scan, image as `bytea`): the picture is kept with its scan, so a report made later, or
  a scan reopened from "Recent scans", shows the same picture. No row is ever shared between scans.
* **API**: `GET /api/v1/consent/scans/{id}/screenshot` returns the picture as base64 with the scan id and the domain it belongs to, or a plain-words
  `message` when there is none (never an internal error, reason code or path). A scan made before previews existed answers `not_captured`.
* **Dashboard and PDF** read one module (`frontend/src/report/hero.ts`): score ring at the top left, the domain above the picture at the top right, the
  band, headline and coverage between them. The same picture is used in both. A picture whose scan id differs from the scan being reported is refused. Without a
  picture the same space shows "Homepage preview not available" and why.
* The domain is spelt as the scan was entered, the same way the page header spells it. The address is printed under the picture only when the scanned
  page is not just the homepage.

## Report additions: cookie inventory, checklist, prioritised fixes, dedicated sections

Added to both the dashboard and the PDF, from the same view modules (`report/cookieInventory.ts`, `policyAnalysis.ts`, `insights.ts`, `dpdpScore.ts`):

- **Cookie inventory**: provider, first/third-party, lifetime, purpose, and Set / Not seen / Not tested before consent, after Accept and after Reject. Purposes come from `backend/app/rules/cookie_purposes.py` and only for recognised cookies; otherwise the report says the purpose could not be identified.
- **Policy checklist**: Quoted / Partially stated / Not found / Not applicable for every point.
- **Prioritised fixes**: score fixes with the real effect on the score, then policy-wording fixes with the effect on completeness. Shown once per medium.
- **Five sections**: grievance officer, DPO, Indian-language support, children's data and age gate, observable security signals. Each is labelled Confirmed, Observed or Not verified. Language and security signals are collected by the crawler (`scanner/page_signals.py`) into the website_scan stage metadata, so no migration is needed.

None of these change the DPDP score, and an absence is never called a violation. A scan made before this change shows "Not recorded for this scan" for the new signals.

## Five-page DPDP readiness review

`frontend/src/report/readiness.ts` builds a review of the five pages that matter most (sign-up, else checkout, else the enquiry form; homepage; privacy notice; account settings, else the contact form; grievance / rights page, else Contact Us), from the scan's own evidence. It is shown on the dashboard (`components/ReadinessReview.tsx`) and as a numbered section of the PDF, from the same object.

- Pages are chosen from the pages the scan loaded, then from links and sitemap pages it only knows of (named, marked "not loaded"). A page that does not exist is written NOT FOUND, recorded as a finding, and the fallback is used.
- Each check has one status: Compliant, Gap, Not Found, Cannot Verify, Requires document review. Only what the scan saw, or the policy's own quoted words, is Compliant. Wording beside a form, pre-ticked boxes, a footer's text, plain language, policy dates and anything behind a login are Cannot Verify. Breach reporting, impact assessments, retention schedules and Significant Data Fiduciary status are Requires document review.
- Readiness score: Compliant over (Compliant + Gap + Not Found), each check equal, out of 100. A missing page is a finding, not a failed check. It is separate from the DPDP Compliance Score and changes nothing.
- Top five gaps are ranked by a fixed risk (1 to 5) per check; the riskiest page is the one whose gaps carry the most risk in total.
- Backend: the detailed evidence view now gives each form's `page_url` (agent's own evidence unchanged).
- Worded as a readiness check, with the 13 May 2027 date, and ends "This is a compliance readiness review, not legal advice."
