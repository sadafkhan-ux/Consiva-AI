# swaransoft.com: changes needed for a Privacy Readiness Score of 100/100

Based on the Consiva report of 4 Nov 2026, 10:37 UTC (Report ID FR-20261104-58CE): **85/100, 8.5 of 10 marks.** Seven checks passed in full. Three scored half marks, and each is fixed
by adding specific sentences to the privacy policy at `swaransoft.com/privacy`. No change to the website's behaviour is needed for these three.

This is a readiness reading against the DPDP Act 2023 and Rules 2025, not legal advice. Checks 4 and 5 are best practice, not mandatory items. The DPDP duties start about May 2027.
Have your legal adviser approve the final wording and fill in the bracketed values with your real practice. Do not publish a period or a recipient that is not true.

## What the scan found

| # | Check | Now | What is missing |
|---|---|---|---|
| 3 | Purpose of Data Processing | 0.5 / 1 | The purposes passage covers enquiries and resources, but gives no purpose for **Google Analytics, Google Tag Manager and Google Ads**, which the scan saw run after Accept. |
| 4 | Data Sharing and Third Parties | 0.5 / 1 | "Trusted service providers" names no kind of recipient, and **Google Tag Manager, Google Analytics and Google Ads** are not named. |
| 5 | Data Retention Disclosure | 0.5 / 1 | Only "contact form submissions are retained for 24 months". No period for the cookies the scan saw (`_ga` 400 days, `_ga_PQEBJ3CS74` 400 days, `_gcl_au` 90 days) or for other personal data. |

All other checks passed (1, 2, 6, 7, 8, 9, 10). Keep them as they are. One thing to protect: check 2 passes because the policy describes the data collected, including
the cookie and device data of the analytics and advertising tools. Do not remove the cookie or analytics description from the policy while editing; if it goes, check 2 drops to half marks
because the scan observes Google Analytics and Google Ads.

## Change 1: purposes (check 3)

Put the purposes in **one passage** (one paragraph or one list), not scattered. The scan quotes a single passage, and it has to cover everything the site does.

Suggested wording:

> **Why we use your information.** We use the details you give us through our contact and enquiry forms to respond to your enquiries, send you the resources and guides you ask for,
> and provide information about our services. With your consent, we use Google Analytics and Google Tag Manager to measure how visitors use our website and to improve its performance
> and content. With your consent, we use Google Ads and its conversion measurement tags for advertising and marketing, to measure the effectiveness of our campaigns and to
> show our advertising on other websites. Analytics and advertising tags run only after you select "Accept All" and stop when you select "Reject All".

Why this passes: it states a purpose for the form data, for analytics (measure, performance) and for advertising and marketing, which are the three things the scan observed.
Name the tools only if they are really used; if you add another tracker later, add its purpose here.

## Change 2: recipients (check 4)

Name the **kinds** of recipient and, for the analytics and advertising services, the services themselves, in the sharing passage.

> **Who we share information with.** We share personal data only with: (a) analytics providers, namely Google (Google Analytics and Google Tag Manager), to measure website use;
> (b) advertising and marketing providers, namely Google (Google Ads), to measure and deliver our advertising; (c) cloud and hosting providers that run our website and systems
> ([name or kind]); (d) email delivery and CRM providers that help us reply to you ([name or kind]); and (e) professional advisers, courts, regulators and other authorities where the
> law requires. These recipients act under written agreements that limit their use of your data.

Why this passes: at least one kind of recipient is named, and the analytics and advertising services the scan saw are named. Remove any item in the list that does not apply to you.

## Change 3: retention (check 5)

Give a period or a clear criterion for **each kind** of data, including the cookies, and say "personal data" in general terms so the passage is not read as covering one category only.

> **How long we keep your information.** We keep contact form and enquiry details for 24 months after our last contact with you. We keep all other personal data only for as long as
> it is needed for the purpose it was collected for, and for [period, for example 24 months] after that purpose ends unless the law requires us to keep it longer. We then
> erase it or make it anonymous. Cookies set with your consent last for the following periods: Google Analytics cookies `_ga` and `_ga_PQEBJ3CS74` expire after 400 days (about 13
> months); the Google Ads cookie `_gcl_au` expires after 90 days. Analytics data held in Google Analytics is kept for [period set in your Google Analytics data retention
> setting, for example 14 months].

Why this passes: it covers personal data in general (not only form submissions), gives a period for each category, and states the lifetime of every persistent cookie the scan saw.
Use the lifetimes your own cookie settings produce; the figures above are what the scan observed on 4 Nov 2026. Check that the Google Analytics retention setting you quote is the one set in your account.

## Placement tips

1. **Keep each statement in one block of the policy page**, under its own heading ("Why we use your information", "Who we share information with", "How long we keep your information").
   The scan reads the policy text and quotes one passage per requirement.
2. **Put it in the page text, not in a pop-up or collapsed widget.** The scan reads the rendered page. The privacy page was read as 9,177 characters in this run and 24,449 in another run of the same site, so keep the policy as plain page content that loads the same way every time.
3. **If you keep a separate cookie policy** (`/cookie-policy`), put the cookie lifetimes there as well. The scan also reads it.
4. **Re-run the Consiva scan after publishing.** A new scan is the only way to confirm the score. The result depends on what the scan observes at that moment.

## Keep these working (so all 10 checks are assessed)

- **Consent banner buttons must stay clickable by an automated browser.** In an earlier run (4 Nov 2026, 10:23 UTC) the scanner could not click Accept or Reject. Check 9 then could not be assessed and the report showed 9 of 10 checks. In this run both worked ("Accept option: Found, Reject option: Found") and check 9 passed. Keep Accept All and Reject All as normal buttons with visible text, in the main banner layer.
- **Keep analytics and advertising tags blocked until Accept.** Before consent and after Reject the scan saw 0 analytics or marketing requests and 0 cookies. That is why check 9 passes.

## Optional, does not change the score

- **Grievance Officer details:** the report shows `saurabhk@swaransoft.com`. Naming the officer and the designation (for example "Saurabh Kumar, Grievance Officer") and giving a phone number and postal address is recommended. The DPDP Act and Rules ask for business contact information, not a name or designation, but other rules (SPDI Rules 2011, e-commerce rules) may require them depending on your business. This does not change any mark.
- **Data Protection Officer or contact person:** the report shows `info@swaransoft.com · +91 9818600650`. Keep it published in the policy and the website footer.

## Expected result

If all three passages are published and the scan reads them, checks 3, 4 and 5 each move from 0.5 to 1. The report would then show **10 / 10 marks, 100/100**. Nothing here is guaranteed: the
scan picks the passage it quotes and compares it with what it observes at that time, so confirm with a new scan.
