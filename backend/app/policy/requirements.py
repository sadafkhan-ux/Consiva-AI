"""What a privacy policy should say, and where the DPDP Act and Rules say so.

Every mapping below was read against the Act and Rules in knowledge_sources/ (DPDP Act 2023, DPDP Rules 2025), not taken from a
third-party report. Two kinds are kept apart, because the law does not treat them alike:

  required  The Act or Rules require this to be given or published (the notice contents of s.5(1) and rule 3; the means of exercising
            rights and the grievance system of rule 14; the contact of the Data Protection Officer or answering person, s.8(9)).
  practice  The Act has a duty behind it, but does not say the privacy policy must describe it (retention s.8(7), security s.8(5),
            breach s.8(6), cross-border transfer s.16 and rule 15, the use of processors s.8(2)). A policy that says so is more
            transparent; one that does not has not necessarily broken a rule. The report says which is which.

Left out on purpose:
  * "automated decision disclosure": that is a GDPR (Art. 22) idea. The Act's only related duty, s.8(3)(a), is to keep data used for a
    decision about a person complete and accurate; it asks for no disclosure.
  * Significant Data Fiduciary duties (s.10, rule 13): they depend on being notified, which a website cannot show; see Applicability.
Absence from the policy text is reported as "not found in the policy text", never as a violation.
"""

from __future__ import annotations

from dataclasses import dataclass

REQUIRED, PRACTICE = "required", "practice"


@dataclass(frozen=True)
class PolicyRequirement:
    id: str
    title: str
    group: str
    basis: str                     # required | practice
    provisions: tuple[str, ...]    # every one checked against the Act or Rules text
    look_for: str                  # what the reader should find; given to the model
    fix: str                       # what to add when it is not found
    applies: str = "all"           # "all", or "children" (only where children's data is plausible)


REQUIREMENTS: tuple[PolicyRequirement, ...] = (
    PolicyRequirement(
        "notice_data_itemised", "Itemised description of the personal data collected", "Notice", REQUIRED,
        ("DPDP Act s.5(1)(i)", "DPDP Rules r.3(b)(i)"),
        "A list of the specific kinds of personal data collected (for example name, email address, phone number, company, location), not just 'information you provide'.",
        "List each kind of personal data you collect, in plain language, as an itemised list.",
    ),
    PolicyRequirement(
        "notice_purposes", "Specified purposes, and the services or uses they enable", "Notice", REQUIRED,
        ("DPDP Act s.5(1)(i)", "DPDP Rules r.3(b)(ii)"),
        "The purposes the data is processed for, and what goods, services or uses each purpose enables.",
        "State each purpose separately and the specific service or use it enables; avoid a single catch-all purpose.",
    ),
    PolicyRequirement(
        "consent_withdrawal", "How to withdraw consent, as easily as it was given", "Consent", REQUIRED,
        ("DPDP Act s.6(4)", "DPDP Act s.5(1)(ii)", "DPDP Rules r.3(c)(i)"),
        "A statement that consent can be withdrawn at any time and the way to do it (a link, a setting, an address).",
        "Say that consent can be withdrawn at any time and give the means, which must be about as easy as giving consent was.",
    ),
    PolicyRequirement(
        "rights_access", "Right to a summary of the data and its processing", "Rights", REQUIRED,
        ("DPDP Act s.11",),
        "The right to obtain a summary of the personal data processed and the processing activities.",
        "State the right to a summary of the personal data processed and how to request it.",
    ),
    PolicyRequirement(
        "rights_correction_erasure", "Right to correction, completion, updating and erasure", "Rights", REQUIRED,
        ("DPDP Act s.12",),
        "The right to have data corrected, completed, updated or erased.",
        "State the rights to correct, complete, update and erase personal data and how to request each.",
    ),
    PolicyRequirement(
        "rights_nominate", "Right to nominate another person to exercise rights", "Rights", REQUIRED,
        ("DPDP Act s.14", "DPDP Rules r.14(4)"),
        "The right to nominate another individual to exercise the person's rights in the event of death or incapacity.",
        "State the right to nominate another individual to exercise rights on death or incapacity, and how to do so.",
    ),
    PolicyRequirement(
        "rights_means", "Published means of making a rights request", "Rights", REQUIRED,
        ("DPDP Rules r.14(1)(a)", "DPDP Act s.5(1)(ii)", "DPDP Rules r.3(c)(ii)"),
        "A concrete way to make a request to exercise rights: an email address, a form, a portal or a postal address.",
        "Publish the means (email, form or portal) to make a rights request, and what identifier you need to find the person's data.",
    ),
    PolicyRequirement(
        "grievance_contact", "Grievance redressal contact", "Grievance", REQUIRED,
        ("DPDP Act s.13(1)", "DPDP Act s.8(10)", "DPDP Rules r.14(3)"),
        "A named grievance officer or a grievance mechanism with a way to reach it (email, phone, form).",
        "Publish who handles grievances and how to reach them: a name or role with an email address, phone number or form.",
    ),
    PolicyRequirement(
        "grievance_timeline", "Grievance response period of at most 90 days", "Grievance", REQUIRED,
        ("DPDP Rules r.14(3)", "DPDP Act s.13(2)"),
        "The time within which grievances are acknowledged and answered.",
        "State the time within which grievances are answered; the Rules allow a reasonable period not exceeding ninety days.",
    ),
    PolicyRequirement(
        "complaint_to_board", "How to complain to the Data Protection Board", "Grievance", REQUIRED,
        ("DPDP Act s.5(1)(iii)", "DPDP Rules r.3(c)(iii)", "DPDP Act s.13(3)"),
        "A statement that the person may complain to the Data Protection Board of India, and how.",
        "Say that a complaint can be made to the Data Protection Board of India (after using your grievance process) and how to do so.",
    ),
    PolicyRequirement(
        "dpo_contact", "Business contact of a Data Protection Officer or answering person", "Grievance", REQUIRED,
        ("DPDP Act s.8(9)",),
        "The business contact details (name or title, email, phone or address) of a Data Protection Officer or a person who can answer questions about processing.",
        "Publish the business contact of a Data Protection Officer or of a named person who answers questions about how personal data is handled.",
    ),
    PolicyRequirement(
        "data_sharing", "Sharing with processors and other third parties", "Transparency", PRACTICE,
        ("DPDP Act s.8(2)", "DPDP Act s.8(3)(b)"),
        "Who personal data is shared with (service providers, processors, affiliates, other organisations) and why.",
        "Name the categories of recipients (service providers, processors, affiliates) and the reason for sharing with each.",
    ),
    PolicyRequirement(
        "cross_border", "Transfer of data outside India", "Transparency", PRACTICE,
        ("DPDP Act s.16", "DPDP Rules r.15"),
        "Whether personal data is transferred or processed outside India, and where.",
        "State whether personal data leaves India and to which countries; transfers may be restricted by notification.",
    ),
    PolicyRequirement(
        "retention", "Retention periods and erasure", "Transparency", PRACTICE,
        ("DPDP Act s.8(7)", "DPDP Rules r.8"),
        "How long each kind of data is kept, or the criteria for deciding, and what happens at the end.",
        "State how long each kind of data is kept (or the rule for deciding) and that it is erased afterwards unless a law requires it to be kept.",
    ),
    PolicyRequirement(
        "security", "Security safeguards", "Transparency", PRACTICE,
        ("DPDP Act s.8(5)", "DPDP Rules r.6"),
        "The kinds of security measures used to protect personal data (technical and organisational).",
        "Describe the safeguards you use (for example encryption, access control, logging, backups) without overstating them.",
    ),
    PolicyRequirement(
        "breach_response", "Personal data breach response", "Transparency", PRACTICE,
        ("DPDP Act s.8(6)", "DPDP Rules r.7"),
        "What happens if there is a personal data breach, including telling affected people and the Data Protection Board.",
        "Say that affected people and the Data Protection Board will be informed of a personal data breach, and how.",
    ),
    PolicyRequirement(
        "cookies_tracking", "Cookies and tracking technologies", "Transparency", PRACTICE,
        ("DPDP Act s.5(1)(i)", "DPDP Rules r.3(b)"),
        "Which cookies, pixels or similar technologies are used, what for, and how to control them.",
        "List the categories of cookies and trackers used, the purpose of each, and how a visitor can control them.",
    ),
    PolicyRequirement(
        "lawful_basis", "Grounds for processing: consent and the legitimate uses relied on", "Consent", PRACTICE,
        ("DPDP Act s.6(1)", "DPDP Act s.7"),
        "Whether processing rests on consent or on a legitimate use (for example data voluntarily provided, legal obligation), stated for the processing described.",
        "State, for each purpose, whether you rely on consent or on a legitimate use under the Act.",
    ),
    PolicyRequirement(
        "notice_language", "Notice available in English or a scheduled Indian language", "Notice", REQUIRED,
        ("DPDP Act s.5(3)",),
        "A statement that the notice can be read in English or in any language listed in the Eighth Schedule to the Constitution.",
        "Offer the notice in English or any Eighth Schedule language the person chooses, and say so.",
    ),
    PolicyRequirement(
        "children_data", "Children's data and verifiable parental consent", "Children", REQUIRED,
        ("DPDP Act s.9(1)", "DPDP Act s.9(3)", "DPDP Rules r.10"),
        "How data of children (under eighteen) is handled: verifiable consent of a parent or guardian, how the adult is checked, and that children are not tracked or targeted with advertising.",
        "Say whether the service is for children; if it handles their data, describe how verifiable parental consent is obtained and that tracking and targeted advertising to children are not done.",
        applies="children",
    ),
)

BY_ID = {r.id: r for r in REQUIREMENTS}
GROUPS = ("Notice", "Consent", "Rights", "Grievance", "Transparency", "Children")

# Sectors in which children's data is plausible enough that the children's requirement is assessed (see scoring/sector.py), in addition to
# any site whose forms ask for children's details.
CHILD_SECTORS = ("education", "healthcare", "insurance", "hospitality")
