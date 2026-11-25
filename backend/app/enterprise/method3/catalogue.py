"""The Enterprise DPDP parameter catalogue, method 3.0.0: one parameter per independently assessable requirement of the DPDP Act, 2023 and the DPDP Rules, 2025.

Approved by the owner on 20 November 2026 after reviewing the demonstration reports for swaransoft.com and consiva.ai (docs/ENTERPRISE_SCORING_METHOD_V3.md).
96 parameters: 95 scored and E01 shown for information. Weights total 400; one weight point = 0.25 marks, so the maximum marks total exactly 100 and no parameter
loses its relative weight. Statuses: Pass (100%), Partial (50%, only where a Partial criterion is written), Fail (0); a conditional duty that does not apply to the
organisation is Not applicable and is removed from the maximum, with the reason. The legal readings are a DRAFT until a named person signs them off.

Independent of the 5-page agent: nothing here imports from app.scoring, app.rules, app.policy or app.llm.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

CATALOGUE_VERSION = "3.0.1"
METHOD_NAME = "Enterprise DPDP compliance score (method 3.0.1)"
LEGAL_REVIEW_STATUS = "draft: awaiting a named legal sign-off"
#: Act sections 3 to 17 (except 6(9)) and Rules 3 and 5 to 16 come into force eighteen months after 13 Nov 2025; s.6(9) and Rule 4 one year after.
IN_FORCE_MAIN = date(2027, 5, 13)
IN_FORCE_CONSENT_MANAGER = date(2026, 11, 13)

# kinds
SITE = "Observed on the website"
URL = "Observed URL by URL"
POLICY = "Read from the published policy"
RECORDS = "Published policy; internal records not inspected"
CONDITIONAL = "Conditional (applies only to some organisations)"
SUPPORT = "Supporting technical signal (not a legal duty)"
INFO = "Information only (no marks)"

CATS = {
    "N": "1. Privacy notice (s.5, Rule 3)",
    "C": "2. Consent (s.4, s.6)",
    "L": "3. Lawful ground (s.4, s.7)",
    "F": "4. Fiduciary duties and security safeguards (s.8, Rules 6, 7)",
    "RT": "5. Retention and erasure (s.8(7), s.8(8), Rule 8)",
    "G": "6. Contact and grievance redressal (s.8(9), s.8(10), s.13, Rules 9, 14)",
    "R": "7. Data Principal rights (s.11, 12, 14, Rule 14)",
    "T": "8. Transfers outside India (s.16, Rule 15)",
    "X": "9. What the site says against what it does",
    "K": "10. Children and persons with disability (s.9, Rules 10-12) - conditional",
    "S": "11. Significant Data Fiduciary (s.10, Rule 13) - conditional",
    "M": "12. Consent Managers (s.6(7)-(9), Rule 4, First Schedule) - conditional",
    "E": "13. Exemptions (s.17) - information only",
    "TS": "14. Technical security signals (supporting s.8(5))",
}


@dataclass(frozen=True)
class P:
    id: str
    cat: str
    name: str
    requirement: str
    act: str
    rule: str
    weight: int
    kind: str
    evidence: str
    method: str
    pass_: str
    partial: str
    fail: str

    @property
    def max_marks(self) -> float:
        return self.weight / 4

    @property
    def partial_allowed(self) -> bool:
        """Partial is a valid result only where a Partial criterion is written (a "-" means the requirement is met or not)."""
        return self.partial.strip() not in ("", "-")

    @property
    def scored(self) -> bool:
        return self.weight > 0

    @property
    def in_force(self) -> date | None:
        if self.kind == SUPPORT:
            return None
        return IN_FORCE_CONSENT_MANAGER if self.cat == "M" else IN_FORCE_MAIN


UP, UPA, UF = "Every applicable URL meets it.", "At least half of the applicable URLs meet it, but not all.", "Fewer than half of the applicable URLs meet it."

PARAMS = (
    # ---------------- 1. Notice
    P("N01", "N", "Privacy notice published", "A notice describing the processing of personal data is published by the Data Fiduciary.", "s.5(1)", "R3", 10, SITE,
      "A page whose text is a privacy notice, found by the crawl.", "Crawl every link and sitemap entry; classify pages by URL slug, title and text; read the notice text in full.",
      "A privacy notice page loads and its text was read.", "A notice exists only as a file, an image or behind a click that the scan could open only in part.", "No privacy notice found on any crawled URL."),
    P("N02", "N", "Notice linked from every page", "The notice can be reached from every page where personal data may be collected or tracking runs.", "s.5(1)", "R3(c)", 6, URL,
      "The HTML of every crawled page, searched for a link to the notice.", "For each crawled page, look for an <a href> pointing at the notice URL.", UP, UPA, UF),
    P("N03", "N", "Notice understandable on its own", "The notice is presented and understandable independently of any other information.", "s.5(1)", "R3(a)", 4, POLICY,
      "The notice page text.", "Check the notice is its own page or section and does not depend on terms or other documents to be understood.",
      "The notice is a standalone document with its own sections.", "The notice is complete but relies on another document for a key part.", "The notice is only a fragment of another document."),
    P("N04", "N", "Clear and plain language", "The notice is in clear and plain language.", "s.5(1)", "R3(a)", 4, POLICY,
      "The notice text; readability figures; a model reading of sampled passages.", "Readability figures over the whole notice plus a model reading; flagged for human review.",
      "Short, specific sentences a general reader can follow.", "Mostly clear, with legal or technical passages a general reader would struggle with.", "Mostly legal or technical language."),
    P("N05", "N", "Itemised description of personal data", "The notice gives an itemised description of the personal data to be processed.", "s.5(1)(i)", "R3(b)(i)", 10, POLICY,
      "Notice passages that name each item of personal data.", "Extract each named item; compare with the data actually collected (forms, cookies).",
      "Every kind of personal data collected is named specifically.", "Some kinds are named specifically, others only generically (e.g. 'usage data').", "No itemised description."),
    P("N06", "N", "Specified purpose of processing", "The notice states the specified purpose of processing.", "s.5(1)(i)", "R3(b)(ii)", 10, POLICY,
      "Notice passages stating purposes.", "Extract each purpose and test whether it is specific.", "Each purpose is specific.", "Some purposes are specific, some vague.", "No purpose stated."),
    P("N07", "N", "Goods, services or uses enabled", "The notice describes the goods or services to be provided, or the uses to be enabled, by the processing.", "s.5(1)(i)", "R3(b)(ii)", 4, POLICY,
      "Notice passages that tie purposes to services.", "Find the services or uses named against the purposes.", "Named for every purpose.", "Named for some purposes.", "Not stated."),
    P("N08", "N", "How to withdraw consent", "The notice states the manner in which consent may be withdrawn.", "s.5(1)(ii)", "R3(c)(i)", 6, POLICY,
      "A notice passage on withdrawal.", "Find the statement and a means (link, setting, email).", "Withdrawal statement with a means.", "Statement without a means.", "Not stated."),
    P("N09", "N", "How to exercise rights", "The notice states the manner in which the Data Principal may exercise her rights.", "s.5(1)(ii)", "R3(c)(ii)", 6, POLICY,
      "A notice passage on exercising rights.", "Find the statement and a means.", "Statement with a means.", "Statement without a means.", "Not stated."),
    P("N10", "N", "How to complain to the Board", "The notice states the manner in which a complaint may be made to the Data Protection Board.", "s.5(1)(iii)", "R3(c)(iii)", 6, POLICY,
      "A notice passage naming the Board.", "Find the reference to the Board and how to complain.", "The Board and the route are stated.", "The Board is named without any route.", "Not stated."),
    P("N11", "N", "Communication link for withdrawal and rights", "The notice gives the communication link of the website or app for withdrawing consent and exercising rights.", "s.5(1)", "R3(c)", 4, POLICY,
      "A link in the notice to a rights or withdrawal route.", "Read the notice HTML for a link to a rights or consent route.", "A working link is given.", "Only an email or postal address.", "No route given."),
    P("N12", "N", "Notice in English or an Eighth Schedule language", "The Data Principal can access the notice in English or any Eighth Schedule language.", "s.5(3)", "-", 4, POLICY,
      "The notice language; any language option.", "Detect the notice language and any language chooser.", "English or a scheduled language, and a choice where offered.", "-", "Neither available."),
    P("N13", "N", "Notice accompanies each consent request (forms)", "Every request for consent is accompanied or preceded by the notice.", "s.5(1)", "R3", 8, URL,
      "Every data-collecting form on every crawled URL; a link to the notice in or beside it.", "For each form collecting personal data, check for a notice link inside or beside the form.", UP, UPA, UF),
    P("N14", "N", "Notice to people who consented before commencement", "Where consent was given before the Act commenced, the Data Fiduciary gives a notice as soon as reasonably practicable.", "s.5(2)", "-", 4, RECORDS,
      "A policy statement that existing users are or will be notified. Sending the notice is a record the site cannot show.", "Read the policy for a statement on existing users.",
      "The policy says existing Data Principals are notified.", "The policy refers to existing users without saying they are notified.", "No statement."),
    # ---------------- 2. Consent
    P("C01", "C", "Consent mechanism wherever tracking runs", "Processing that relies on consent offers a consent mechanism on every page where it runs.", "s.4(1), s.6(1)", "-", 8, URL,
      "The consent tool's script and banner on each crawled URL.", "For each URL, check that the consent tool loaded.", UP, UPA, UF),
    P("C02", "C", "Consent by clear affirmative action (forms)", "Consent is signified by a clear affirmative action, not implied by submitting or using the site.", "s.6(1)", "-", 8, URL,
      "Every data-collecting form: its consent controls and its wording.", "Flag forms that rely on wording such as 'by submitting you agree' with no control.", UP, UPA, UF),
    P("C03", "C", "No pre-ticked consent boxes", "Consent is unambiguous: no consent control is ticked in advance.", "s.6(1)", "-", 4, URL,
      "The checked state of every consent control on every form.", "Read each checkbox's checked state on load.", UP, UPA, UF),
    P("C04", "C", "Consent specific to each purpose", "Consent can be given or refused for each specified purpose separately.", "s.6(1)", "R3(b)(ii)", 8, SITE,
      "The banner and its preferences layer.", "Open the preferences layer and read per-purpose toggles.", "A toggle per purpose.", "Purposes are listed or a preferences control exists, but per-purpose toggles could not be confirmed.", "All-or-nothing only."),
    P("C05", "C", "Consent unconditional (no cookie wall)", "Access to the service is not made conditional on consenting to unnecessary processing.", "s.6(1)", "-", 4, SITE,
      "Page loads after Reject on further pages.", "Reject, then load further pages and confirm they work.", "Pages work after Reject.", "Some pages are limited after Reject.", "Access is blocked until Accept."),
    P("C06", "C", "Data limited to what is necessary (forms)", "Consent is limited to the personal data necessary for the specified purpose.", "s.6(1)", "-", 4, URL,
      "Every distinct form's fields against its evident purpose.", "Compare fields with purpose; flag fields beyond it; human review.", UP, UPA, UF),
    P("C07", "C", "Consent request links to the notice", "The consent request is informed: it links to the notice.", "s.6(1), s.5(1)", "R3", 4, SITE,
      "The banner's text and links.", "Read the banner for a notice link.", "The banner links to the notice.", "It mentions a policy without a link.", "No reference."),
    P("C08", "C", "Consent request in clear, plain language", "The request for consent is presented in clear and plain language.", "s.6(3)", "-", 4, SITE,
      "Banner and form consent wording.", "Model reading of the wording; human review.", "Short and specific.", "Partly unclear.", "Unclear or misleading."),
    P("C09", "C", "Consent request in English or an Eighth Schedule language", "The consent request gives the option to access it in English or any Eighth Schedule language.", "s.6(3)", "-", 4, SITE,
      "The banner's language and language selector.", "Read the banner language and its selector options.", "English plus a selector, or a scheduled language.", "-", "Not available."),
    P("C10", "C", "Consent request gives the DPO or authorised person's contact", "Every consent request gives the contact details of a DPO or person authorised to answer.", "s.6(3)", "-", 4, SITE,
      "Banner and form consent text.", "Look for a contact in or linked from each request.", "Every request gives or links a contact.", "Some do.", "None do."),
    P("C11", "C", "No consent term that infringes the Act", "No part of the consent waives a right or infringes the Act.", "s.6(2)", "-", 3, POLICY,
      "Consent wording, policy and terms.", "Read for terms waiving rights (e.g. the right to complain).", "No such term.", "-", "A term infringes the Act."),
    P("C12", "C", "No non-essential tracking before a choice", "No analytics or advertising cookie or request before the person consents.", "s.6(1)", "-", 14, URL,
      "Cookies and requests captured on every crawled URL before any choice; tracker classification.", "For each URL, list analytics or advertising cookies and requests seen before consent.", UP, UPA, UF),
    P("C13", "C", "Reject is honoured", "After Reject, no analytics or advertising runs.", "s.6(1)", "-", 10, SITE,
      "Cookies and requests after Reject in a fresh browser.", "Click Reject; record cookies and requests.", "None after Reject.", "Only a non-identifying request.", "Analytics or advertising after Reject."),
    P("C14", "C", "Reject persists across pages", "The refusal holds on further pages.", "s.6(1)", "-", 4, SITE,
      "Further pages loaded after Reject.", "Load further pages after Reject.", "Holds on every further page.", "Holds on some.", "Does not hold."),
    P("C15", "C", "Reject as easy as Accept", "Refusing is offered with the same ease as accepting.", "supports s.6(1), s.6(4)", "-", 4, SUPPORT,
      "Banner first layer.", "Compare the Accept and Reject controls.", "Both on the first layer.", "Reject one layer deeper.", "No reject."),
    P("C16", "C", "Withdrawal as easy as giving consent", "Consent can be withdrawn with comparable ease to giving it.", "s.6(4)", "R3(c)(i)", 8, SITE,
      "A persistent control that reopens the choice; withdrawal test.", "After Accept, reopen the choice and withdraw.", "Reopened and withdrawn.", "Reopened, but withdrawal not available in one step.", "Cannot be reopened."),
    P("C17", "C", "Processing stops after withdrawal", "On withdrawal, the Data Fiduciary and its processors cease processing.", "s.6(6)", "-", 8, SITE,
      "Requests after withdrawal.", "Withdraw, then record analytics and advertising requests.", "None afterwards.", "Fewer services, but some continue.", "All continue."),
    P("C18", "C", "Consent can be proved (records kept)", "The Data Fiduciary can prove the notice was given and consent obtained.", "s.6(10)", "-", 4, RECORDS,
      "Policy statement on consent records; the records themselves are internal.", "Read the policy for consent record-keeping.", "States consent records are kept.", "-", "No statement."),
    # ---------------- 3. Lawful ground
    P("L01", "L", "Lawful purpose and ground stated", "Processing is for a lawful purpose, on consent or a legitimate use.", "s.4(1)", "-", 4, POLICY,
      "Policy statement of the ground for each purpose.", "Read the policy for the ground relied on.", "Ground stated per purpose.", "Ground stated for some purposes.", "No ground stated."),
    P("L02", "L", "Legitimate uses identified", "Where processing is without consent, the s.7 legitimate use relied on is identified.", "s.7", "-", 2, POLICY,
      "Policy passages on processing without consent (e.g. legal obligations).", "Find purposes not based on consent and the s.7 clause named.", "Each is tied to a s.7 use.", "Such purposes appear without naming the ground.", "No statement where such processing occurs."),
    # ---------------- 4. Fiduciary duties / security
    P("F01", "F", "Processors engaged under a valid contract", "A processor is engaged only under a valid contract.", "s.8(2)", "-", 4, RECORDS,
      "Policy statement; the contracts are internal.", "Read the policy for contracts with processors.", "States contracts are in place.", "Refers to processors without contracts.", "No statement."),
    P("F02", "F", "Accuracy and completeness", "Personal data used for decisions or disclosed is complete, accurate and consistent.", "s.8(3)", "-", 2, RECORDS,
      "Policy statement.", "Read the policy for accuracy measures.", "Stated.", "-", "Not stated."),
    P("F03", "F", "Technical and organisational measures", "Appropriate technical and organisational measures ensure observance of the Act.", "s.8(4)", "R6(1)(g)", 4, RECORDS,
      "Policy statement; certifications cited.", "Read the policy.", "Measures described.", "Only a general statement.", "Not stated."),
    P("F04", "F", "Encryption, masking or tokenisation", "Personal data is secured by encryption, obfuscation, masking or virtual tokens.", "s.8(5)", "R6(1)(a)", 4, RECORDS,
      "Policy statement; certifications cited.", "Read the policy for the named measure.", "The measure is named.", "Only a general safeguard statement or a certification.", "Nothing stated."),
    P("F05", "F", "Access control", "Access to the computer resources holding personal data is controlled.", "s.8(5)", "R6(1)(b)", 4, RECORDS,
      "Policy statement.", "As F04.", "The measure is named.", "Only a general statement or a certification.", "Nothing stated."),
    P("F06", "F", "Logs, monitoring and review", "Access is visible through logs, monitoring and review to detect unauthorised access.", "s.8(5)", "R6(1)(c)", 4, RECORDS,
      "Policy statement.", "As F04.", "The measure is named.", "Only a general statement or a certification.", "Nothing stated."),
    P("F07", "F", "Backups and continuity", "Reasonable measures keep processing going after compromise, such as backups.", "s.8(5)", "R6(1)(d)", 4, RECORDS,
      "Policy statement.", "As F04.", "The measure is named.", "Only a general statement or a certification.", "Nothing stated."),
    P("F08", "F", "Logs kept for one year", "Logs and personal data are kept for at least one year to detect and investigate misuse.", "s.8(5)", "R6(1)(e)", 3, RECORDS,
      "Policy statement.", "Read the policy for log retention.", "One year or more stated.", "Logs mentioned without a period.", "Not stated."),
    P("F09", "F", "Processor contracts require safeguards", "Contracts with processors require reasonable security safeguards.", "s.8(5)", "R6(1)(f)", 4, RECORDS,
      "Policy statement.", "Read the policy.", "Stated that contracts require safeguards.", "Contracts or confidentiality mentioned without safeguards.", "Not stated."),
    P("F10", "F", "Breach intimation to each affected person", "A personal data breach is intimated to each affected Data Principal, with the Rule 7(1) particulars.", "s.8(6)", "R7(1)", 8, RECORDS,
      "Policy statement of a breach process.", "Read the policy (only the policy body, not page furniture).", "Process described.", "Breach mentioned without notifying people.", "Not stated."),
    P("F11", "F", "Breach intimation to the Board", "A breach is intimated to the Board without delay, with the detailed report within 72 hours.", "s.8(6)", "R7(2)", 8, RECORDS,
      "Policy statement.", "As F10.", "Process described.", "Board mentioned without the process.", "Not stated."),
    # ---------------- 5. Retention
    P("RT1", "RT", "Erasure when the purpose is served or consent withdrawn", "Personal data is erased when consent is withdrawn or the purpose is no longer served.", "s.8(7)(a)", "R8", 8, POLICY,
      "Policy retention passage.", "Read the policy.", "Erasure on both triggers stated.", "One trigger stated.", "Not stated."),
    P("RT2", "RT", "Processors made to erase", "The Data Fiduciary causes its processors to erase the data too.", "s.8(7)(b)", "-", 4, RECORDS,
      "Policy statement.", "Read the policy.", "Stated.", "-", "Not stated."),
    P("RT3", "RT", "Retention period for each kind of data collected", "A retention period or criterion is given for every kind of personal data the site collects.", "s.8(7)", "R8", 4, POLICY,
      "Policy retention passage against the data observed (forms, analytics).", "Match each kind collected to a period.", "Every kind has a period.", "Some kinds have a period.", "None."),
    P("RT4", "RT", "Third Schedule platforms: erasure after inactivity, 48 hours' notice", "E-commerce, gaming and social media platforms of the stated size erase after the Third Schedule period, with 48 hours' notice.", "s.8(8)", "R8(1), R8(2), Third Schedule", 4, CONDITIONAL,
      "Whether the site is such a platform; policy statement.", "Classify the site; if it applies, read the policy.", "Applies and stated.", "Applies, partly stated.", "Applies, not stated."),
    P("RT5", "RT", "Data, traffic data and logs kept one year", "Personal data, traffic data and logs are retained for one year for the Seventh Schedule purposes.", "s.8(7)", "R8(3)", 3, RECORDS,
      "Policy statement.", "Read the policy.", "Stated.", "-", "Not stated."),
    # ---------------- 6. Contact and grievance
    P("G01", "G", "Contact of DPO or answering person published", "The business contact of the DPO or of a person able to answer questions on processing is published.", "s.8(9)", "R9", 8, POLICY,
      "Notice passage with a contact detail.", "Find a contact for privacy questions.", "Name or role with an email or phone.", "Only a generic contact.", "None."),
    P("G02", "G", "Contact published prominently", "The contact is published prominently on the website.", "s.8(9)", "R9", 3, URL,
      "The text of every crawled page.", "For each URL, check a contact email is visible.", UP, UPA, UF),
    P("G03", "G", "Grievance redressal mechanism", "An effective grievance redressal mechanism is established and published.", "s.8(10), s.13(1)", "R14(3)", 8, POLICY,
      "Notice passage naming a grievance officer or route.", "Find the route and contact.", "Route and contact published.", "Route without a contact.", "None."),
    P("G04", "G", "Grievance response within 90 days", "The published period for responding to grievances is at most 90 days.", "s.13(2)", "R14(3)", 4, POLICY,
      "Notice passage with a period.", "Extract the period.", "Stated, 90 days or less.", "Stated without a number.", "Not stated, or over 90 days."),
    # ---------------- 7. Rights
    P("R01", "R", "Right to a summary of the data", "The right to obtain a summary of the personal data and the processing.", "s.11(1)(a)", "R14(1)", 4, POLICY,
      "Notice passage on access.", "Find the right and how to use it.", "Stated.", "-", "Not stated."),
    P("R02", "R", "Identities of other fiduciaries and processors", "The right to the identities of fiduciaries and processors the data was shared with, or their disclosure.", "s.11(1)(b)", "-", 4, POLICY,
      "Notice passage naming recipients.", "Find recipients named or the right stated.", "Recipients named or the right stated.", "Recipients only described generically, with few names.", "Nothing."),
    P("R03", "R", "Right to correction, completion and updating", "The right to correct, complete and update personal data.", "s.12(1), s.12(2)", "R14(1)", 4, POLICY,
      "Notice passage.", "Find the right.", "Stated.", "Only part stated.", "Not stated."),
    P("R04", "R", "Right to erasure", "The right to erasure.", "s.12(3)", "R14(1)", 4, POLICY,
      "Notice passage.", "Find the right.", "Stated.", "-", "Not stated."),
    P("R05", "R", "Right to nominate", "The right to nominate another person to exercise rights on death or incapacity.", "s.14", "R14(4)", 4, POLICY,
      "Notice passage.", "Find the right.", "Stated.", "-", "Not stated."),
    P("R06", "R", "Means of making a rights request", "The means by which a request may be made are published.", "-", "R14(1)(a)", 6, POLICY,
      "Notice passage or link.", "Find the means.", "A form, portal or address given.", "Only 'contact us'.", "Not given."),
    P("R07", "R", "Identifiers needed for a request", "The particulars or identifiers needed to identify the requester are published.", "-", "R14(1)(b)", 3, POLICY,
      "Notice passage.", "Find what the requester must give.", "Stated.", "-", "Not stated."),
    P("R08", "R", "Rights-request route reachable", "The published request route can be reached from the notice.", "-", "R14(1)", 2, SITE,
      "The link target in the notice HTML.", "Check the link is present; open it when it is on the scanned host.", "Link present (and opens, when on the scanned host).", "-", "No link."),
    # ---------------- 8. Transfers
    P("T01", "T", "Transfers outside India disclosed", "Transfers of personal data outside India are disclosed.", "s.16(1)", "R15", 4, POLICY,
      "Notice passage.", "Find the statement.", "Stated with destinations.", "Stated without destinations.", "Not stated though third parties abroad are contacted."),
    P("T02", "T", "Safeguards for transfers stated", "The terms or safeguards for transfers are stated, consistent with any restriction notified.", "s.16", "R15", 2, POLICY,
      "Notice passage.", "Find the safeguard.", "Safeguard stated.", "-", "Not stated."),
    # ---------------- 9. Cross-checks
    P("X01", "X", "Cookies observed are declared", "Every cookie the site sets is declared in its cookie or privacy notice.", "s.5(1)(i)", "R3(b)(i)", 5, URL,
      "Every cookie observed (before consent, after Accept) against the cookie policy text.", "Match each cookie name or its service against the declared list.",
      "All declared.", "At least half declared, but not all.", "Fewer than half declared."),
    P("X02", "X", "Third parties contacted are named", "The analytics, advertising and embedded third parties the pages contact are named in the notice.", "s.5(1), s.11(1)(b)", "-", 5, URL,
      "Third-party hosts contacted on every URL, before consent and after Accept, against the notice.", "Match each non-functional service to the notice text.",
      "All named.", "At least half named, but not all.", "Fewer than half named."),
    P("X03", "X", "Form data matches the notice", "The data the forms collect is itemised in the notice.", "s.5(1)(i)", "R3(b)(i)", 4, URL,
      "Fields of every distinct form against the notice items.", "Match each field kind to the notice.", "All itemised.", "At least half itemised, but not all.", "Fewer than half itemised."),
    # ---------------- 10. Children and disability (conditional)
    P("K01", "K", "Verifiable parental consent", "Verifiable consent of a parent is obtained before processing a child's data.", "s.9(1)", "R10", 6, CONDITIONAL,
      "Signals that the site serves children (audience, forms, age fields).", "Decide applicability; if it applies, look for an age gate and parental consent.", "Applies and present.", "Applies, partly.", "Applies, absent."),
    P("K02", "K", "No detrimental processing of children's data", "No processing likely to cause a detrimental effect on a child's well-being.", "s.9(2)", "-", 2, CONDITIONAL,
      "As K01.", "As K01.", "Applies and met.", "Applies and partly met.", "Applies and not met."),
    P("K03", "K", "No tracking or behavioural monitoring of children", "No tracking or behavioural monitoring of children.", "s.9(3)", "R12 and Fourth Schedule (exemptions only)", 4, CONDITIONAL,
      "Tracking on child-directed pages.", "As K01.", "Applies and met.", "Applies and partly met.", "Applies and not met."),
    P("K04", "K", "No targeted advertising at children", "No targeted advertising directed at children.", "s.9(3)", "R12 and Fourth Schedule (exemptions only)", 4, CONDITIONAL,
      "Advertising on child-directed pages.", "As K01.", "Applies and met.", "Applies and partly met.", "Applies and not met."),
    P("K05", "K", "Guardian consent for persons with disability", "Consent of a verified lawful guardian for a person with disability.", "s.9(1)", "R11", 4, CONDITIONAL,
      "Signals the site serves such persons.", "As K01.", "Applies and met.", "Applies and partly met.", "Applies and not met."),
    # ---------------- 11. SDF (conditional)
    P("S01", "S", "DPO based in India", "A Significant Data Fiduciary appoints a DPO based in India.", "s.10(2)(a)", "-", 2, CONDITIONAL, "Notification as SDF; policy.", "Applicability first.", "Applies and met.", "Applies and partly met.", "Applies and not met."),
    P("S02", "S", "Independent data auditor", "An independent data auditor is appointed.", "s.10(2)(b)", "-", 2, CONDITIONAL, "As S01.", "As S01.", "Applies and met.", "Applies and partly met.", "Applies and not met."),
    P("S03", "S", "Periodic Data Protection Impact Assessment", "A DPIA is carried out periodically.", "s.10(2)(c)(i)", "R13(1)", 2, CONDITIONAL, "As S01.", "As S01.", "Applies and met.", "Applies and partly met.", "Applies and not met."),
    P("S04", "S", "Periodic audit", "A periodic audit is carried out.", "s.10(2)(c)(ii)", "R13(1)", 2, CONDITIONAL, "As S01.", "As S01.", "Applies and met.", "Applies and partly met.", "Applies and not met."),
    P("S05", "S", "Due diligence on algorithmic software", "Algorithmic software is verified not to risk Data Principals' rights.", "-", "R13(3)", 2, CONDITIONAL, "As S01.", "As S01.", "Applies and met.", "Applies and partly met.", "Applies and not met."),
    P("S06", "S", "Specified data processed in India", "Personal data specified by the Government is processed in India.", "-", "R13(4)", 2, CONDITIONAL, "As S01.", "As S01.", "Applies and met.", "Applies and partly met.", "Applies and not met."),
    # ---------------- 12. Consent Managers (conditional)
    P("M01", "M", "Board-registered Consent Manager integration", "Where a Consent Manager is used, it is registered with the Board and integrated as the Act requires.", "s.6(7)-(9)", "R4", 2, CONDITIONAL,
      "A Consent Manager named on the site.", "Distinguish a registered Consent Manager from a cookie tool.", "Applies and met.", "Applies and partly met.", "Applies and not met."),
    P("M02", "M", "Organisation as Consent Manager: First Schedule conditions", "If the organisation is a Consent Manager, the First Schedule disclosures are published.", "s.6(9)", "R4, First Schedule", 2, CONDITIONAL,
      "Statements that the organisation is a Consent Manager.", "As M01.", "Applies and met.", "Applies and partly met.", "Applies and not met."),
    # ---------------- 13. Exemption
    P("E01", "E", "Exemption claimed", "An exemption under s.17 is claimed (shown for information).", "s.17", "R16", 0, INFO,
      "Policy and terms.", "Read for a claimed exemption.", "-", "-", "-"),
    # ---------------- 14. Technical
    P("TS01", "TS", "HTTPS on every page", "Every page is served over HTTPS.", "supports s.8(5)", "R6(1)", 2, SUPPORT, "Scheme of every crawled URL.", "Per URL.", UP, UPA, UF),
    P("TS02", "TS", "No mixed content", "No page loads resources over plain HTTP.", "supports s.8(5)", "R6(1)", 2, SUPPORT, "Requests of every URL.", "Per URL.", UP, UPA, UF),
    P("TS03", "TS", "Valid certificate, more than 30 days left", "The certificate is valid with more than 30 days left.", "supports s.8(5)", "-", 2, SUPPORT, "TLS handshake.", "Read the certificate.", "Valid, >30 days.", "Valid, <=30 days.", "Invalid."),
    P("TS04", "TS", "TLS 1.2 or 1.3", "Only TLS 1.2 or 1.3 is negotiated.", "supports s.8(5)", "-", 2, SUPPORT, "TLS handshake.", "Read the version.", "1.2 or 1.3.", "-", "Older."),
    P("TS05", "TS", "HSTS header", "Strict-Transport-Security is sent.", "supports s.8(5)", "-", 1, SUPPORT, "Response headers of every URL.", "Per URL.", UP, UPA, UF),
    P("TS06", "TS", "Content-Security-Policy header", "A Content-Security-Policy is sent.", "supports s.8(5)", "-", 1, SUPPORT, "Response headers of every URL.", "Per URL.", UP, UPA, UF),
    P("TS07", "TS", "X-Content-Type-Options header", "X-Content-Type-Options: nosniff is sent.", "supports s.8(5)", "-", 1, SUPPORT, "Response headers of every URL.", "Per URL.", UP, UPA, UF),
    P("TS08", "TS", "Referrer-Policy header", "A Referrer-Policy is sent.", "supports s.8(5)", "-", 1, SUPPORT, "Response headers of every URL.", "Per URL.", UP, UPA, UF),
    P("TS09", "TS", "Permissions-Policy header", "A Permissions-Policy is sent.", "supports s.8(5)", "-", 1, SUPPORT, "Response headers of every URL.", "Per URL.", UP, UPA, UF),
    P("TS10", "TS", "Clickjacking protection", "X-Frame-Options or CSP frame-ancestors is sent.", "supports s.8(5)", "-", 1, SUPPORT, "Response headers of every URL.", "Per URL.", UP, UPA, UF),
    P("TS11", "TS", "Cookies carry Secure", "Every cookie set over HTTPS carries the Secure flag.", "supports s.8(5)", "-", 1, SUPPORT, "Every cookie observed in every state.", "Per cookie.", "All.", "At least half.", "Fewer than half."),
    P("TS12", "TS", "Cookies carry SameSite", "Every cookie sets SameSite.", "supports s.8(5)", "-", 1, SUPPORT, "Every cookie observed.", "Per cookie.", "All.", "At least half.", "Fewer than half."),
    P("TS13", "TS", "Redirects stay on HTTPS", "No redirect passes through plain HTTP.", "supports s.8(5)", "-", 1, SUPPORT, "Every redirect chain.", "Per chain.", "All.", "-", "Any leaves HTTPS."),
    P("TS14", "TS", "Forms submit over HTTPS", "Every form posts to an HTTPS address.", "supports s.8(5)", "-", 1, SUPPORT, "Form actions.", "Per form.", UP, UPA, UF),
    P("TS15", "TS", "Personal data kept out of URLs", "Forms collecting personal data do not use GET (which puts the data in the address).", "supports s.8(5)", "-", 1, SUPPORT,
      "Form method attribute of every data-collecting form.", "Per form; a script may send the data differently, so a GET form is flagged for review.", "Every form uses POST.", "GET with the page itself as the action (probably script-handled; to confirm).", "GET to another address."),
)

BY_ID: dict[str, P] = {p.id: p for p in PARAMS}

# ---- 3.0.1 (owner's approval of 20 Nov 2026): applicability, duty category and severity of the conditional and contact duties --------------------------------------------
#: Applicability of a result. Human review is a conditional duty the website evidence cannot settle (for example the Third Schedule user threshold): like Not applicable it is
#: kept out of the maximum, but it is shown apart, as marks held back until the organisation confirms, never as a confirmed Not applicable.
APPLICABLE, NOT_APPLICABLE, HUMAN_REVIEW = "applicable", "not_applicable", "human_review"
#: Which kind of organisation the duty is about (shown in the report's Category column).
DUTY_CATEGORY = {"K01": "Children", "K02": "Children", "K03": "Children", "K04": "Children", "K05": "Persons with disability",
                 "RT4": "E-commerce / online gaming / social media (Third Schedule)", "G02": "General (every data fiduciary)"}
#: The importance of the duty itself, never of the website category. A Fail shows it; a Partial one level lower; a Pass or an informational entry shows none.
SEVERITY = {"K01": "high", "K03": "high", "K04": "high", "K05": "high", "K02": "medium", "RT4": "medium", "G02": "medium"}
_LOWER = {"high": "medium", "medium": "low", "low": "low"}


def severity_of(pid: str, status: str) -> str | None:
    base = SEVERITY.get(pid)
    if base is None or status not in ("fail", "partial"):
        return None
    return base if status == "fail" else _LOWER[base]
TOTAL_WEIGHT = sum(p.weight for p in PARAMS)
MARK_PER_WEIGHT = 0.25

# The invariants the method rests on, checked when the module is loaded so an edit that breaks them cannot be imported.
assert len(PARAMS) == 96 and len(BY_ID) == 96, "96 distinct parameters"
assert TOTAL_WEIGHT == 400, f"the weights must total exactly 400, not {TOTAL_WEIGHT}"
assert sum(p.max_marks for p in PARAMS) == 100, "the maximum marks must total exactly 100"
assert [p.id for p in PARAMS if not p.scored] == ["E01"], "E01 is the one parameter shown for information only"
assert all(p.cat in CATS for p in PARAMS), "every parameter belongs to a category"
assert sum(p.weight for p in PARAMS if p.kind == SUPPORT) * 10 <= TOTAL_WEIGHT, "supporting signals carry at most 10% of the weight"
