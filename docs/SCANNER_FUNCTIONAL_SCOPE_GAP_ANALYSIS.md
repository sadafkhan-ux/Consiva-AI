# Scanner Functional Scope v1.0: gap analysis, feasibility and plan

Prepared 5 Nov 2026 for review. Sources: Functional Scope v1.0, Scoring Mechanism v1.0 (Annexure A), and the code in this repository.
The Benchmark Field Report was used for market context only and is not a specification.

This is a review document. **No application code was changed to produce it.** Estimates are engineering judgement, not calibrated against telemetry, and assume one backend and one frontend engineer.

## 1. The one decision that blocks the build

The Scoring Mechanism (7 domains, 100 weighted points, redistribution, bands, 70-point Quick ceiling) is a **weighted scoring engine**. On 4 Nov 2026 the instruction was to keep only the ten-check engine
(`backend/app/scoring/ten_checks.py`) and remove every weighted engine, API and PDF figure. The two instructions cannot both stand. The Functional Scope itself says to raise conflicts, not resolve them in code.

| Option | What it means | Effect |
|---|---|---|
| A. Scoring Mechanism replaces the ten checks | One new engine (domains A-G) becomes the only score. Ten-check code, its goldens and its UI are retired. | Matches the new documents exactly. Large rewrite of scoring, API contract, dashboard and both PDFs. |
| B. Ten checks stay, new documents applied around them | Add outcome taxonomy, bands, severity/confidence, guardrails, evidence rules, Quick/Deep. Do not build the 100-point matrix. | Smaller, but violates "follow the Scoring Mechanism strictly". |
| C. Both engines | Not recommended. It recreates the duplicate-score problem removed on 4 Nov. | Rejected. |

Recommendation: **A**, built as the single engine behind `/assessment`, so every consumer (dashboard, JSON, both PDFs, the other team's UAT) still reads one score from one endpoint.

Other conflicts to confirm (each is stated in the documents as an open decision):
- Scope L-13 / SC-08: no decimals anywhere. The ten-check engine reports 8.5 / 10 marks today, so that wording must go with it.
- Scope L-08 says Quick shows a band, not a number; Scoring SC-02 says every report is out of 100. Recommended in Scope 15.2: band with the calculation available on request.
- "Do not touch any other file": this is a feature spanning scanner, scoring, API, database and report code. New work will add files and edit existing ones where the feature lives. Please confirm that is intended; otherwise only new files can be added.

## 2. Where the product stands today

The product is an authenticated, organisation-scoped scan console with a ten-check readiness assessment. Findings below come from a code survey; items marked (verify) were only grep-checked.

### Already in place (preserve)
- Quick-like profile: 5-page web profile (`scanner/scan_profiles.py`); application profile uncapped.
- Pre-consent, post-accept, post-reject tests, each in a fresh browser context (`scanner/crawler.py`).
- robots.txt, nested sitemap and home-page link discovery; SSRF guard checked at every redirect hop (`scanner/url_safety.py`, `crawler.py`).
- Failure catalogue with 14 categories; per-URL inventory statuses (migration 0047).
- Forms inspected, never submitted (verify no submit path).
- Statutory mapping with 13 May 2027 commencement (`scoring/requirements.py`).
- Scheduled rescans, scan diff, change notifications, webhooks, actions, audit log, queue with fair scheduling and capacity governor, per-scan LLM usage cost.
- The new observed-vs-disclosed comparison (`scoring/disclosure.py`).

### Gaps against the Functional Scope

| Area | Requirement IDs | Status | Feasibility | Estimate |
|---|---|---|---|---|
| Anonymous Quick Scan start, ownership statement, 24 h reuse, per-IP and per-domain limits, daily cost ceiling | QS-01..05, PF-12, PF-23, PF-24, GR-25 | Missing. Every scan route requires login and org. | Straightforward; needs a new unauthenticated entry route and abuse controls. | 2-3 wks |
| Quick time budget (60 s hard) and first finding in 15 s | PF-01..04, PF-11 | Missing. Web ceiling is 1,800 s. | Medium. Needs a deadline passed through the crawler and "decline to start if it cannot finish". | 1-2 wks |
| One page per category with a true plain-English reason; fuller failure reasons (not found, unsupported) | QS-08..12 | Partial (path-keyword ranking only). | Easy to medium. | 1-1.5 wks |
| Progress from completed work, live evidence lines, cancel, partial results | QS-24..30, EX-01, EX-02 | Partial. | Medium. | 1.5-2 wks |
| Storage names (never values); PII-parameter scrubbing before storage | QS-18, GR-16, GR-17 | Missing or unverified. | Medium; must be proven by test. | 1-1.5 wks |
| Fourth consent state (withdraw) + 2 further pages; per-purpose tests; N/A vs not tested | DS-10..15, QS-21..23 | Missing. Hardest scanner work: banner variants differ per vendor. | Hard. Not credible without a reference-site set (PF-29). | 4-6 wks |
| Domain ownership by DNS, file or tag; agency authorisation record | DS-02..05 | Partial (DNS TXT only). | Easy. | 1-1.5 wks |
| Plans, entitlements enforced server-side | DS-01, DS-06, CV-05 | Missing. | Medium; needs a commercial model. | 2 wks |
| Outcome taxonomy (9 outcomes), blocked-by-prerequisite, severity vs confidence, one outcome per finding | 6.2, L-01..07 | Missing. | Medium; core of the new engine. | 2-3 wks |
| Validated result gate: EV-02 refusal, EV-07, GR-19/20, prohibited wording, fixed phrase library, no free LLM wording | EV-01..07, GR-01..08, GR-23 | Missing. | Medium; this is the highest-value part (Scope section 17). | 3-4 wks |
| Scoring Mechanism: 7 domains, redistribution, Quick ceiling 70, bands, integrity rules, no decimals | SC-01..52, L-08..14 | Missing. | Medium, if option A. | 3-4 wks |
| Immutable, versioned issued reports | L-15..18, SC-07, SC-31 | Missing. Reports are recomputed on read. | Medium. Needs a snapshot table (new migration 0048+). | 2 wks |
| Two-page report redesign, colour/status semantics, 5-action cap, share link, CTA from own data | RP-01..22, CV-01..10 | Partial. | Medium. Frontend heavy. | 3-4 wks |
| Cookie intelligence: provenance, security/fraud category, unclassified queue, verified date, org and non-India flag | CI-01..10, DS-16, DS-17 | Partial. Needs a named owner and licensed data. | Medium for schema and UI; ongoing for data. | 3-4 wks |
| Form analysis: pre-selected flag, purpose, destination | DS-20, G2, F2 | Partial. | Easy to medium. | 1 wk |
| Free/paid capacity separation, autoscaling, warm minimum, ops view, metrics (detection rate by platform) | PF-13..15, PF-22, PF-28, PF-29 | Missing. Needs infrastructure decisions. | Medium to hard. | 3-4 wks |
| Scanner identity page, honest user-agent with info link, CAPTCHA/WAF handling | GR-10, GR-12, EX-04, EX-15 | Partial. | Easy. | 0.5-1 wk |
| Integrations: CSV export, Slack, issue-tracker push, plugins, public API polish | IN-06..11 | Mostly missing. | Medium; plugins are separate projects. | 3-5 wks |
| Release gates: reference-site set, form-submission canary, tenant-isolation tests, worker-loss test | 15.1 | Missing. | Medium. | 2-3 wks |

Total if built in sequence: roughly **9-12 months of one engineer pair** at the estimates above; with a team of four, about **4-5 months**. These are proposals, not commitments. The Scope intentionally sets no dates.

### Statements that cannot be made until proven (Scope PF-06, PF-29)
No speed target and no consent-testing capability may be claimed publicly until measured against the reference site set. That set does not exist yet.

## 3. Proposed build order (once section 1 is decided)

1. **Foundation: validated result and outcome model.** Outcome taxonomy, severity/confidence, evidence gate, prohibited-wording check, phrase library, versioned immutable report snapshot. Everything else renders from this.
2. **Scoring Mechanism engine** behind `/assessment`, with parity tests and a reference set; retire ten-check output only after option A is confirmed.
3. **Quick Scan flow:** anonymous entry, reuse, limits, 60 s budget, page-category selection with reasons, honest progress.
4. **Report rebuild:** two pages, colours/shapes, centrepiece consent table, share link.
5. **Deep Scan:** withdrawal state, per-purpose, ownership methods, entitlements, cookie intelligence.
6. **Operations:** capacity split, cost view, metrics, then integrations and plugins.

Each step keeps the existing ten-check endpoint working until its replacement is verified, so nothing existing breaks mid-way.

## 4. Open items from the documents (named owners needed)
Tracker-intelligence owner; legal reviewer for statutory mapping (GR-24); daily free-tier cost ceiling; agency authorisation capture; live status of each Consiva capability named in remedies (SC-32); Domain D/G applicability tests; published position on crawl instructions for the entered address.
