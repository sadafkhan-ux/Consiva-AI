"""Form detection with a lightweight, purely heuristic purpose guess. This guess is
descriptive scaffolding for the LLM/rules layers, not a compliance judgment."""

from app.scanner.page_parser import ParsedForm
from app.scanner.schemas import FormField, FormRecord

_PASSWORD_HINTS = ("password", "pwd")
_EMAIL_ONLY_HINTS = ("email",)
_MESSAGE_HINTS = ("message", "comment", "enquiry", "inquiry")


def _guess_purpose(field_names: list[str]) -> str:
    lowered = [f.lower() for f in field_names]
    if any(h in n for n in lowered for h in _PASSWORD_HINTS):
        return "login_or_signup"
    if any(h in n for n in lowered for h in _MESSAGE_HINTS):
        return "contact_or_support"
    if lowered and all(any(h in n for h in _EMAIL_ONLY_HINTS) for n in lowered):
        return "newsletter_signup"
    return "unknown"


def detect_forms(page_local_id: str, forms: list[ParsedForm]) -> list[FormRecord]:
    records = []
    for i, form in enumerate(forms):
        field_names = [f.name for f in form.fields]
        records.append(FormRecord(
            local_id=f"form-{page_local_id}-{i}",
            page_local_id=page_local_id,
            selector=form.selector,
            action_url=form.action_url,
            method=form.method,
            fields=[FormField(name=f.name, field_type=f.field_type, required=f.required) for f in form.fields],
            purpose_guess=_guess_purpose(field_names),
        ))
    return records
