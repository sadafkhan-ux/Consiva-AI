# GR-24 sign-off pack: scoring changes awaiting named legal sign-off

Functional Scope GR-24 requires named legal sign-off for any change to what is scored or how (`scoring/legal_map.py`, `docs/SCORECARD.md`). The changes below altered scores and have **not** been signed off. Until they are, every report built on scorecard 3.1.0 to 3.3.0 should be treated as a working draft for internal review, not a customer release.

| # | Change | Version | Rule before | Rule now | Effect on a score |
|---|---|---|---|---|---|
| 1 | Weights of the Forms & Security rows | 3.1.0 | P10 forms 16 marks | P10 8, P11 SSL 4, P12 TLS 4 (category still 16) | none on the total; marks move between rows |
| 2 | P11 SSL certificate | 3.1.0, then 3.3.0 | none (not assessed) | 3.1.0: full above 90 days, half at 90 or fewer. **3.3.0: full above 30 days, half at 30 or fewer**, zero if expired, invalid or no HTTPS, Not Assessed if unreadable | +2 marks for a certificate with 31 to 90 days left (any 90-day auto-renewing certificate such as Let's Encrypt) |
| 3 | P12 TLS version | 3.1.0 | none | TLS 1.3 full, TLS 1.2 half, TLS 1.1 or lower zero, Not Assessed if unreadable | up to 4 marks |
| 4 | P5 withdrawal runs in the Quick Scan | 3.2.x | Deep Scan only | Every scan. Accept, reopen the control, withdraw, then watch what is still sent. Control reopens but activity continues: half marks | P5 changes from Not Assessed to 0, 4 or 8 marks. On swaransoft.com it scores 4 of 8: the reopen control was found, but an analytics request was still sent after "Revoke All" |
| 5 | Request classification (not a scoring rule, but it changes results) | 10 Nov 2026 | Public CDNs, Google Maps, reCAPTCHA, Stripe telemetry, Calendly and Sourcepoint, plus Google Ads beacons on google.com (conversion, remarketing, user-list), Bing Ads, Adobe, Comscore, Chartbeat, Optimizely and other named ad and analytics vendors, were left "unclassified" | Named in `rules/tracker_catalog.py` with a category | While any request is unclassified, "is a banner needed?" cannot be decided and checks 1, 2, 4, 5 are Not Assessed. A site with only harmless requests now gets Not Applicable (marks left out, reason stated); a site with advertising or analytics before consent gets Failed. 205 of 230 stored scans with unclassified requests are affected |
| 6 | Banner recognised by structure; a reject control found but not clickable is Not Assessed | 3.4.0 | A banner with only "Accept"/"Decline" buttons was "none detected"; a failed click on a found reject control scored 0 | Recognised as a banner; check 2 Not Assessed in the failed-click case | Sites with such banners are now scored on their banner (greenply.com style); a site no longer loses 8 marks when the scan could not click a reject control |
| 7 | Wording only | 3.2.3, 3.3.0 | "(depends on P6)", "No privacy notice was found" | "(depends on Check 6)", "...found on the N pages read"; issuer shown by organisation | none |

## Why change 2 is proposed
A certificate that renews itself every 90 days is healthy for its whole life but has at most 90 days left. Under the 90-day rule it was marked down to half marks on every scan (Poolscapes: 37 days left, Group SNP: 81 days left). The 30-day rule keeps the half mark for a certificate that is close to lapsing.

## Questions for the signatory
1. Is 30 days an acceptable threshold for full marks on P11?
2. Is half marks for TLS 1.2 acceptable, given that it is a currently supported version?
3. Is it acceptable for P5 to be scored from one Accept, reopen, withdraw sequence on the home page, with the reopen control found by label (for example "Cookie Preferences") and the withdraw click found by label (for example "Revoke All")?
4. The legal references of every row (`legal_map.py`) have not had named review either; sign off the mapping or list changes.

## Measured effect on the two customer reports
| | Before (older build) | Now (3.3.0) |
|---|---|---|
| Poolscapes | 53/100 (34 of 64) | 64/100 (41 of 64): policy read (+5 on P7), SSL full (+2) |
| Group SNP | 34/100 (22 of 64) | 38/100 (24 of 64): SSL full (+2) |

## Sign-off
| Item | Decision (approve / change / reject) | Name | Date |
|---|---|---|---|
| 1 Weights | | | |
| 2 SSL 30-day rule | | | |
| 3 TLS rule | | | |
| 4 P5 in the Quick Scan | | | |
| Legal mapping | | | |
