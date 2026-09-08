from app.scanner.form_detector import detect_forms
from app.scanner.page_parser import ParsedForm, ParsedFormField


def _form(fields):
    return ParsedForm(selector="#f", action_url="/submit", method="post", fields=fields)


def test_password_field_guesses_login_or_signup():
    form = _form([ParsedFormField(name="email", field_type="email", required=True),
                  ParsedFormField(name="password", field_type="password", required=True)])
    [record] = detect_forms("page-0", [form])
    assert record.purpose_guess == "login_or_signup"


def test_message_field_guesses_contact():
    form = _form([ParsedFormField(name="email", field_type="email", required=True),
                  ParsedFormField(name="message", field_type="textarea", required=True)])
    [record] = detect_forms("page-0", [form])
    assert record.purpose_guess == "contact_or_support"


def test_email_only_guesses_newsletter():
    form = _form([ParsedFormField(name="email", field_type="email", required=True)])
    [record] = detect_forms("page-0", [form])
    assert record.purpose_guess == "newsletter_signup"


def test_unrecognized_fields_guess_unknown():
    form = _form([ParsedFormField(name="quantity", field_type="number", required=True)])
    [record] = detect_forms("page-0", [form])
    assert record.purpose_guess == "unknown"
