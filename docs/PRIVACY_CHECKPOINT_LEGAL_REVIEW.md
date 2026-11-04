# The ten privacy checkpoints against the DPDP Act 2023 and the DPDP Rules 2025

Review of 2026-11-04. A review and specification document only: it changes no code, API, score or behaviour. Where it describes what the engine does today it says so, and where the
engine does less than this document asks, that is listed in section 4 as a gap, not changed.

**How to read the legal references.** The Act section and Rule numbers below are the ones in the texts recorded for this project (DPDP Rules 2025, G.S.R. 846(E) of 13 Nov 2025 and
the commencement notification G.S.R. 843(E)). A reference marked **(confirm)** comes from a summary and has not been checked against the gazette text; do not print it in a report
until it is. The Rules and most Act duties start about **13 May 2027** (the 18-month group: Act ss.3 to 17 and Rules 3, 5 to 16), so every finding is worded as readiness for that
date, never as a present breach. The SPDI Rules 2011 under the IT Act apply today and are treated separately (section 3).

## 1. Marking principles (apply to every check)

1. **Marks:** Passed 1, Partially Passed 0.5, Failed 0. Not Assessed and Not Applicable carry no marks and leave the maximum. Score = marks awarded / marks available x 100.
2. **No mark without verifiable evidence.** A positive result needs (a) a quotation or an observed record, (b) the address of the page it came from, and (c) for a quotation, a
   word-for-word match against the stored page text. A model's say-so, a paraphrase, or a quote with no source address is not evidence, and the check does not pass on it.
3. **"Not found" is not "not required" and not a breach.** A Failed result means the check ran to the end on the text that was read and the item was not there. It is limited to the
   pages scanned and is never worded as a violation. A page that could not be reached, blocked, was unreadable, or whose text was cut before the item could appear is **Not Assessed**.
4. **Required versus practice.** A check is tagged *DPDP-related* only where the Act or Rules text requires the item; *best practice* where they do not (checks 4 and 5); *technical
   observation* where the scan records behaviour and does not judge it (checks 8 and 9).
5. **Observed versus disclosed.** What the scan saw the site doing (forms, cookies, trackers, requests before consent) is compared with what the policy says. Collection the policy
   does not describe lowers the mark of the check it relates to, and the result names the item (section 2, checks 2 to 5).
6. **Evidence quality is stated** beside every result: Specific, General, Limited or None, with what it covers ("one kind of data only", "recipients not named", "2 of 3 rights").

### Where evidence comes from

| Source | What it supplies | Stored as |
|---|---|---|
| Privacy policy and cookie policy text | Quotations for checks 2 to 7, source URL per quotation | policy analysis (verified quotes) |
| Website pages (up to five by design, or more in application mode) | Existence of policy and contact pages, forms, footer links | scan pages and evidence |
| Forms | Fields collected (email, phone, name, message), page URL | scan evidence |
| Cookies and third-party requests | Vendor, category, lifetime, the consent state in which each fired | scan evidence |
| Consent banner and its buttons | Whether Accept and Reject exist and work; requests in each state | consent signals and states |
| Contact page, footer | Published DPO or grievance contact | **not stored today** (section 4, G3) |

## 2. The ten checkpoints

For each: the legal basis, what must be shown, the evidence required, the marking criteria, and what the engine does today.

### Check 1. Privacy Policy Availability
- **Legal basis:** Act s.5(1) (notice to the Data Principal); Rule 3 (the notice is standalone, in plain language). A published policy page is the usual way the notice is made available.
- **Evidence required:** the address of a privacy or cookie policy page found among the scanned pages, and its readable text. Source: website pages, the policy detector, the policy text.
- **Passed (1):** a policy page was found and its text was read (if only the first part of a very long policy was read, it passes and says so).
- **Partially Passed (0.5):** found, but the text could not be read or the policy analysis has not run, so what it says is not assessed.
- **Failed (0):** no policy page after searching the pages scanned (privacy and cookie pages are tried first). Wording: "may be on another domain or page the scan did not reach".
- **Not Assessed:** the site could not be reached, or the policy page was blocked (robots.txt, security check) or did not load.
- **Today:** implemented as above.

### Check 2. Personal Data Collection Disclosure
- **Legal basis:** Act s.5(1)(i); Rule 3(b)(i): an itemised description of the personal data processed.
- **Evidence required:** a verified quotation from the policy that lists the data collected; the page URL. Observed: the form fields on the site, and the cookie and device data of the trackers the scan saw.
- **Passed:** a verified quotation itemises the data, and nothing the scan observed collecting is left undescribed.
- **Partially Passed:** the analysis found part of it; or the quotation is verified but the scan observed collection it does not describe (for example a form asking for phone number where the policy lists only email, or analytics and advertising cookies where the policy mentions no cookie or device data). The result names the undescribed item.
- **Failed:** no itemised description in the text that was read. Observed forms and trackers are named so the reader sees what should have been described.
- **Not Assessed:** policy text unavailable or cut before the section could appear.
- **Today:** the policy side is implemented. The observed-versus-disclosed comparison is **written and tested but not deployed** (engine 1.1.0, backend/app/scoring/disclosure.py); the running engine is 1.0.0 and does not make it.

### Check 3. Purpose of Data Processing
- **Legal basis:** Act s.5(1)(i); Rule 3(b)(ii): the specified purpose, and the goods or services the processing enables, for the data described.
- **Evidence required:** a verified quotation stating why each kind of data is used; the page URL. Observed: every form, analytics tool (Google Analytics, Tag Manager) and advertising tag the scan saw.
- **Passed:** a verified purpose statement that covers each kind of collection observed (form submissions, analytics, advertising or marketing).
- **Partially Passed:** a purpose is stated, but not for everything observed (for example only "to reply to your enquiry" while Google Analytics and a marketing pixel run). The result states which observed item has no purpose in the policy.
- **Failed:** no purpose statement found. The observed items are named: "the scan observed X, so a purpose should be stated for each".
- **Not Assessed:** as check 2.
- **Today:** policy side implemented. The cross-check is in 1.1.0, not deployed. This is the gap the review of the digitalanumati.com report raised.

### Check 4. Data Sharing and Third Parties
- **Legal basis:** Act s.8(2) (a Data Fiduciary may engage a processor only under a valid contract). The Act and Rules do not list the recipients as a notice item, so this check is **best practice**, not a mandatory item.
- **Evidence required:** a verified quotation that names the kinds of recipient (analytics, advertising, payment, hosting, authorities). Observed: the third-party services that received requests from the site.
- **Passed:** at least one kind of recipient is named and the passage covers the analytics and advertising services observed.
- **Partially Passed:** "service providers" or "third parties" only, with no kind named; or recipients named but not the analytics or advertising services the scan observed.
- **Failed:** no sharing statement in the text that was read.
- **Not Assessed:** as check 2.
- **Today:** the "recipients not named" rule is implemented; the observed-service comparison is in 1.1.0.

### Check 5. Data Retention Disclosure
- **Legal basis:** Act s.8(7) (erase when consent is withdrawn or the purpose is no longer served, unless law requires retention) and Rule 8 (erasure timelines, 48 hours' prior notice, a minimum one year's retention of certain logs). Rule 3 does not list a retention period as a notice item, so this is **best practice** that supports compliance with s.8(7).
- **Evidence required:** a verified quotation giving a period or a criterion for how long data is kept; the page URL. Observed: persistent cookies and their lifetimes; data collected by forms.
- **Passed:** a period or a criterion that covers personal data in general, and states cookie lifetimes where persistent cookies are observed.
- **Partially Passed:** a period for one kind of data only (for example form submissions); or no retention stated for the persistent cookies observed. "As long as necessary" alone is a criterion in words only and is Partially Passed.
- **Failed:** nothing about retention in the text that was read. Observed items that need a stated retention are named.
- **Not Assessed:** as check 2. A privacy or cookie policy cut at the storage limit before the section could appear is Not Assessed, not Failed.
- **Today:** the "one kind of data only" rule is implemented; the cookie-retention comparison is in 1.1.0.

### Check 6. User Rights Disclosure
- **Legal basis:** Act ss.11 (information), 12 (correction and erasure), 13 (grievance redressal), 14 (nominate another person); Rule 14(1): publish the means of making a rights request.
- **Evidence required:** verified quotations for the means of making a request and for each right (access, correction and erasure, nomination); page URL.
- **Passed:** the means plus at least two of the three rights.
- **Partially Passed:** one right, or the means only, or rights without the means.
- **Failed:** none found. **Not Assessed:** as check 2.
- **Today:** implemented. Nothing observable on the site contradicts or confirms it.

### Check 7. Contact and Grievance Mechanism
- **Legal basis:** Act s.8(9) and Rule 9: publish the business contact information of the Data Protection Officer, where one applies, or of a person who can answer the Data Principal's questions, prominently on the website or app, and give it in every reply to a rights request. Act s.8(10) and s.13 and Rule 14: a grievance redressal system with a stated response time of at most 90 days. Act s.6(3) **(confirm)**: the consent request carries the same contact. Name and designation are **not** required by the Act or Rules; "business contact information" is. They are required elsewhere (section 3).
- **Evidence required:** a verified quotation showing (a) a grievance channel (email, phone or form link), (b) a business contact of the DPO or answering person, (c) the response period in days, (d) where it is published (policy, footer, consent notice, contact page), and its page URL.
- **Passed:** a grievance contact together with a business contact or a stated response period within 90 days.
- **Partially Passed:** only part, for example a business contact but no grievance contact (the digitalanumati.com result), or a grievance contact but neither period nor business contact.
- **Failed:** no privacy contact, DPO or grievance mechanism in the text read.
- **Not Assessed:** as check 2.
- **Today:** implemented as above. It does not separately report name, designation, phone, address, or where the contact is published; footer and banner text are not stored (gaps G2, G3).

### Check 8. Cookie and Tracker Detection (technical observation)
- **Legal basis:** relates to Act s.6(1) and Rule 3; it records what the site does and does not judge whether the policy discloses it.
- **Evidence required:** the cookies and third-party requests observed, by vendor and category, with the page and the consent state; the pages scanned out of those found.
- **Passed:** detection ran on every scanned page; the result states the coverage ("5 of 170 pages") and any items that could not be classified.
- **Partially Passed:** a scanned page did not load, so tracking there was not observed.
- **Failed / Not Assessed:** no page loaded or the evidence was not loaded: Not Assessed.
- **Today:** implemented. It deliberately does not compare with the policy; that comparison lives in checks 2 to 5.

### Check 9. Consent Behaviour (technical observation)
- **Legal basis:** relates to Act s.6(1) (consent must be free, specific, informed, unconditional and unambiguous) and s.6(4) (withdrawal as easy as giving consent); Rule 3(c)(i).
- **Evidence required:** the analytics and marketing requests and cookies observed in each state: before any consent action, after Accept, after Reject; whether Accept and Reject were found and worked.
- **Passed:** all three states tested and nothing analytics or marketing before consent or after Reject. Stated as homepage-only evidence.
- **Partially Passed:** only one of Accept and Reject could be tested, or requests were sent in a consent-denied mode.
- **Failed:** tracking before consent and again after Reject, or tracking with no banner.
- **Not Applicable:** no banner and nothing that tracks. **Not Assessed:** neither Accept nor Reject could be tested.
- **Today:** implemented. The banner's own text (does it carry the DPO contact, the purposes, a link to the notice) is not assessed (gap G3).

### Check 10. Privacy Evidence and Transparency (report integrity)
- **Legal basis:** none; this checks the report, not the website. It supports the rule that marks require verifiable evidence.
- **Evidence required:** for each positive finding of checks 1 to 9, an excerpt and a source address that is a page the scan actually read. A "not found" result is not counted as evidence. Check 10 is not counted in its own evidence.
- **Passed:** 80% or more of the positive findings are traceable. **Partially Passed:** 50% or more. **Failed:** fewer.
- **Not Assessed:** none of the other checks could be assessed.
- **Today:** implemented.

## 3. DPO, Grievance Officer and the other regimes

| Item | DPDP Act and Rules (from 13 May 2027) | SPDI Rules 2011 (in force now) **(confirm each)** | E-commerce Rules 2020 **(confirm)** |
|---|---|---|---|
| Who | DPO (significant data fiduciaries) or a person able to answer on the fiduciary's behalf | Grievance Officer | Grievance Officer |
| Published where | Prominently on the website or app; in every rights reply; in the consent request | On the website | On the platform |
| Required content | **Business contact information** (role plus email arguably suffices) | Name and contact details | Name, designation, contact details (phone and email) |
| Response time | Stated; at most 90 days (Rule 14) | One month | Acknowledge in 48 hours, resolve in one month |
| Applies to | All fiduciaries (DPO only for significant data fiduciaries, Rule 9 as recorded) | Bodies corporate handling sensitive personal data | E-commerce entities |

**Decision for the marking (recommended, not yet implemented).** Keep the pass rule on what the Act and Rules require (a grievance channel plus a business contact or a response period). Report name, designation,
phone and address as an advisory list under the result ("found: email; not found: name, designation") and say that other regimes may require the name and the designation. Make no mark
depend on name or designation, because the DPDP text does not require them.

## 4. The four gaps: reviewed and decided (2026-11-04, later the same day)

| # | Finding | Decision | Status |
|---|---|---|---|
| G1 | The observed-versus-disclosed comparison for checks 2 to 5 was missing. | **Yes, change.** Implemented in `backend/app/scoring/disclosure.py`, called by the ten-check engine (version 1.1.0). It moves a result from Passed to Partially Passed, or adds the observed item to the text of a result that was not a pass, and changes nothing else. `assess_ten_checks(..., cross_check=False)` reproduces 1.0.0, which the recorded-oracle tests hold to the browser engine. | Done and live. On five stored scans it lowered four (swaransoft 95 to 85, cookieyes 90 to 75, wipro 95 to 85, bbc 85 to 75); icicibank is unchanged at 62.5. Each change was read: the policy states no purpose for the advertising or analytics the scan observed (check 3), names none of those services as recipients (4), and states no period for the persistent cookies observed (5). |
| G2 | Check 7 did not say which parts of a DPO or grievance contact were found. | **Yes, change, advisory only.** Check 7's text now lists the parts found and "not detected" (email, phone, name, designation, postal address, response period) and says the DPDP Act and Rules ask for business contact information, not a name or designation. No mark depends on it. | Done. Name detection is by pattern and is reported as "not detected", never as absent. |
| G3 | Footer, contact-page and consent-banner text is not stored, so "where the contact is published" and the banner's content cannot be evidenced. | **No change.** It needs scanner and storage changes for items whose legal basis is still unconfirmed (s.6(3), section 5 below), and the DPDP duties start about May 2027. The contact page is already read when it is among the policy documents (`/contact-us` was read in the digitalanumati.com scans). | Left as a documented limit. |
| G4 | The captured policy text for one page varied between runs (7,808 characters on most, 60,000 and cut on one). | **Yes, change, one defect.** Cause found in the stored text: the browser capture collected each shadow root with `textContent`, which includes `<style>` and `<script>`, and a consent widget kept about 50,000 characters of CSS there. The plain-HTTP reader already skipped those. The browser capture now skips `style`, `script`, `noscript` and `template` too (`scanner/crawler._SHADOW_DOM_TEXT_JS`), tested in a real headless browser. A policy that is still cut was already Not Assessed (`completed=False`), not Failed, so that part needed no change; the 0/1 "Failed" in the earlier screenshot is from an older build. | Done. Not verified on the live site: no new scan of digitalanumati.com was run. |

### Consent evidence (added later the same day)
Check 9's evidence now names, for each consent state the scan tested, the analytics and marketing requests and cookies seen (for example "after Accept: requests to Google Tag Manager, Google Analytics and Google Ads; cookies _ga, _gcl_au and _ga_PQEBJ3CS74, first seen 2026-11-03 06:10:17 UTC"). A state that was not tested is printed as "not tested", never as "none". Marks and pass rules are unchanged. **Limit:** cookies carry the time the scanner first saw them; third-party requests are stored with the consent state they were seen in but without a time of their own (the crawler keeps them as address and type only). A per-request timestamp would need a crawler, storage and migration change and was not made.

## 5. Open items to confirm before printing in a report

1. The s.6(3) requirement that the consent request carries the DPO or answering-person contact.
2. The SPDI Rules 2011 rule number and wording for the Grievance Officer, and the one-month response time.
3. The E-commerce Rules 2020 rule number, the designation and phone requirements, and the 48-hour acknowledgement.
4. Whether Rule 9 limits the DPO duty to significant data fiduciaries, as the recorded text indicates, or applies to all fiduciaries; the pass rule for check 7 is written to accept either a DPO or an answering person.
5. Whether a policy that states retention only in the cookie policy (not the privacy policy) satisfies check 5; today it does, because either document's verified passage counts.
