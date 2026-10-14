"""Form detection with a lightweight, purely heuristic purpose guess. This guess is
descriptive scaffolding for the LLM/rules layers, not a compliance judgment."""

from app.scanner.page_parser import ParsedForm, ParsedFormField
from app.scanner.schemas import FormField, FormRecord

_PASSWORD_HINTS = ("password", "pwd")
_EMAIL_HINTS = ("email", "mail")
_MESSAGE_HINTS = ("message", "comment", "enquiry", "inquiry", "query", "subject")

# A checkbox is almost always a consent/marketing opt-in or terms-acceptance toggle on
# this kind of form, never the thing the form is FOR -- counting it against "does this
# form ask for anything besides an email" was the actual bug: the single most common
# shape on a real scan (swaransoft.com) was exactly one email field plus one consent
# checkbox, six separate forms of it, every one classified "unknown" because the
# checkbox's own field name was never going to contain the word "email". `hidden`
# fields carry no purpose signal either (CSRF tokens, UTM passthroughs, etc).
_NON_CONTENT_FIELD_TYPES = frozenset({"checkbox", "hidden"})


def _guess_purpose(fields: list[ParsedFormField]) -> str:
    content_fields = [f for f in fields if f.field_type not in _NON_CONTENT_FIELD_TYPES]
    lowered = [f.name.lower() for f in content_fields]

    if any(h in n for n in lowered for h in _PASSWORD_HINTS):
        return "login_or_signup"
    if any(h in n for n in lowered for h in _MESSAGE_HINTS):
        return "contact_or_support"
    if content_fields and all(
        f.field_type == "email" or any(h in n for h in _EMAIL_HINTS)
        for f, n in zip(content_fields, lowered)
    ):
        return "newsletter_signup"
    return "unknown"


def detect_forms(page_local_id: str, forms: list[ParsedForm]) -> list[FormRecord]:
    records = []
    for i, form in enumerate(forms):
        records.append(FormRecord(
            local_id=f"form-{page_local_id}-{i}",
            page_local_id=page_local_id,
            selector=form.selector,
            action_url=form.action_url,
            method=form.method,
            fields=[FormField(name=f.name, field_type=f.field_type, required=f.required) for f in form.fields],
            purpose_guess=_guess_purpose(form.fields),
        ))
    return records
