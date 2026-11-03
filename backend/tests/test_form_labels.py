"""The wording beside a checkbox is recorded, so a consent checkbox is recognised by what it says and not by a generated field name."""

from app.scanner.form_detector import detect_forms
from app.scanner.page_parser import parse_page

URL = "https://x.test/contact/"


def _fields(html: str):
    return parse_page(html, URL).forms[0].fields


def test_a_checkbox_inside_its_label_gets_the_label_text_including_links():
    f = _fields('<form><input type="email" name="email" required>'
                '<label><input type="checkbox" id="_r_1_" required> I agree to receive newsletters from CookieYes and accept the <a href="/privacy-policy/">Privacy Policy</a>.</label></form>')
    box = next(x for x in f if x.field_type == "checkbox")
    assert box.name == "_r_1_"                      # the generated id says nothing...
    assert box.label == "I agree to receive newsletters from CookieYes and accept the Privacy Policy."   # ...the label does
    assert box.required is True


def test_a_label_that_points_at_the_checkbox_with_for_is_found():
    f = _fields('<form><input type="email" name="email"><input type="checkbox" id="c9"><label for="c9">I consent to the processing of my data</label></form>')
    assert next(x for x in f if x.field_type == "checkbox").label == "I consent to the processing of my data"


def test_text_beside_the_checkbox_in_its_own_small_container_is_the_label():
    f = _fields('<form><input type="email" name="email"><div class="row"><input type="checkbox" name="x1"><span>Keep me updated by e-mail</span></div></form>')
    assert next(x for x in f if x.field_type == "checkbox").label == "Keep me updated by e-mail"


def test_an_aria_label_is_used_when_there_is_no_visible_label():
    f = _fields('<form><input type="email" name="email"><input type="checkbox" aria-label="Accept the terms"></form>')
    assert next(x for x in f if x.field_type == "checkbox").label == "Accept the terms"


def test_a_checkbox_with_no_text_has_no_label_and_the_form_text_is_never_taken_for_it():
    f = _fields('<form>Subscribe to our list and read our long notice here. <input type="email" name="email"><input type="checkbox" name="c"></form>')
    assert next(x for x in f if x.field_type == "checkbox").label is None


def test_only_checkboxes_and_radios_carry_a_label():
    f = _fields('<form><label>Your e-mail <input type="email" name="email"></label><label><input type="radio" name="r"> Yes please</label></form>')
    assert next(x for x in f if x.field_type == "email").label is None
    assert next(x for x in f if x.field_type == "radio").label == "Yes please"


def test_a_very_long_label_is_cut_not_stored_whole():
    f = _fields('<form><label><input type="checkbox" name="c"> ' + "word " * 200 + "</label></form>")
    assert len(f[0].label) <= 300


def test_the_label_survives_detection_and_persistence_shape():
    forms = parse_page('<form><input type="email" name="email"><label><input type="checkbox" name="_r_1_" required> I agree</label></form>', URL).forms
    rec = detect_forms("p1", forms)[0]
    dumped = [f.model_dump() for f in rec.fields]        # what scan_repository stores in the JSON column
    assert {"name": "_r_1_", "field_type": "checkbox", "required": True, "label": "I agree"} in dumped


def test_a_stored_form_from_before_labels_existed_still_loads():
    from app.scanner.schemas import FormField
    assert FormField(name="x", field_type="checkbox").label is None
