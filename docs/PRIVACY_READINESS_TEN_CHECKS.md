# Privacy Readiness Score: the ten checks

One engine, one score. `frontend/src/report/tenChecks.ts` (`buildTenChecks`, `scoreChecks`) is the only place the 10-check result is worked out. The dashboard hero and breakdown, the two-page Summary PDF, and the Free and Full PDF score cards all print its number. Nothing in a PDF component recomputes it. (The weighted "DPDP Compliance Score (proposed methodology)" section on the dashboard and in the long Evidence report is a separate reading and is labelled as one.)

## Formula (unchanged)

Passed = 1, Partially Passed = 0.5, Failed = 0. Not Applicable and Not Assessed carry no marks and are left out of the maximum.
Score = marks awarded ÷ applicable marks × 100, rounded half up to one decimal. No applicable check gives "No meaningful score", never 0 or 100.

## The five results

| Result | Meaning | Marks |
|---|---|---|
| Passed | The check ran and the evidence meets the pass condition. | 1 / 1 |
| Partially Passed | Evidence exists but covers only part of the requirement, or its quality is limited. | 0.5 / 1 |
| Failed | The check ran to the end and the disclosure or behaviour was not found or was seen to fail. A limit of the scan, not a finding that the law was broken. | 0 / 1 |
| Not Assessed | The check could not be run: no page loaded, the policy page was blocked or did not load, its text could not be read, or the analysis did not run. | none, left out of the maximum |
| Not Applicable | The check does not apply (only check 9, with no banner and nothing that tracks). | none, left out of the maximum |

Change recorded 2026-11-03: before this, a check that could not be run scored 0 and stayed in the maximum. A page that could not be reached is not a failed check, so it is now Not Assessed. If fewer than five checks can be assessed the score is marked provisional (`limitedBasis`).

## Per check

| # | Check | Evidence and where it comes from | Passed | Partially Passed | Failed | Not Assessed |
|---|---|---|---|---|---|---|
| 1 | Privacy Policy Availability | Pages the crawler read; policy detector result | Policy page found and its text read | Found, text unreadable or analysis not run | No policy page after searching the scanned pages | Site not reachable, or the policy page blocked or did not load |
| 2 | Personal Data Collection | Policy analysis, quote verified word for word against the policy text | Verified quote naming what is collected | Analysis says partial | Not found in the text that was read | Policy text unavailable |
| 3 | Purpose of Processing | as 2 | as 2 | as 2 | as 2 | as 2 |
| 4 | Data Sharing and Third Parties | as 2 | Verified quote that names at least one kind of recipient | Verified, but names only "service providers" or "third parties"; or analysis says partial | as 2 | as 2 |
| 5 | Data Retention | as 2 | Verified quote about personal data in general | Verified, but a period for one kind of data only (for example form submissions); or analysis says partial | as 2 | as 2 |
| 6 | User Rights | as 2, three rights and the means | The means of making a request and at least two of three rights named | One right, or only the means | None found | as 2 |
| 7 | Contact and Grievance | as 2 | Grievance contact plus a response period or a business or DPO contact | Only part | None found | as 2 |
| 8 | Cookie and Tracker Detection | The scan's own cookies and requests | Detection ran on every scanned page | A scanned page did not load | n/a | No page loaded, or evidence not loaded |
| 9 | Consent Behaviour | Before, after Accept and after Reject, observed on the homepage | All three states tested, nothing analytics or marketing before consent or after Reject | Only one of Accept and Reject tested, or requests sent in consent-denied mode | Tracking before consent and after Reject, or tracking with no banner | Neither Accept nor Reject could be tested |
| 10 | Evidence and Transparency | Checks 1 to 9 | 80% or more of the positive findings carry an excerpt and a source page the scan read | 50% or more | Fewer | None of the others assessed |

Evidence quality is stated beside every result (Specific, General, Limited, None) together with what it covers ("One kind of data only", "Recipients not named", "Homepage only", "2 of 3 rights named"). The quality rules for checks 4, 5 and 6 read only the verified passage and apply to any website; none is specific to a site.

## Decisions to know about

- **Coverage is stated, not scored.** A Snapshot reads at most five pages by design. The score card, the "how to read this score" band and the limitations say "5 of 112 pages", and check 8 carries "Detection covers 5 of 112 pages". Coverage alone does not lower a mark. If a stricter rule is wanted, it is one line in `buildTenChecks`.
- **Consent behaviour is homepage only.** Check 9 can be Passed on one page of evidence. Its row says "Homepage only" and that it does not show every consent requirement is met across the site.
- **Check 10 is not circular.** It judges only checks 1 to 9, says so in its evidence, and counts a finding as traceable only when it has an excerpt and a source address that is one of the pages the scan read. A "not found" result records what was searched and is not counted as evidence.
- **Technical observations are not legal findings.** Checks 8 and 9 are tagged "Technical check", checks 4 and 5 "Best practice", and the report states that detection records what a site does and does not assess whether the policy discloses it.
- **Recommendations** come from the checks that did not pass; one line per check, never worded as a violation.

## Not done here

- The 10-check engine runs in the browser at render time. There is no API or database copy of the score.
- Evidence per page now stores the final URL after redirects and the content type (migration 0046). Page title and HTTP status were already stored.
- The weighted DPDP Compliance Score section and the long Evidence report still use the 17-requirement engine.
