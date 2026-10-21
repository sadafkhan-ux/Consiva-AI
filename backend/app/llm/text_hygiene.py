"""Keeps the scan's internal references out of the prose a customer reads.

The model is given the evidence under short local ids (t4, c1, sig1) and internal status codes (cmp_not_found), and is
told to cite them in the `evidence` field only. Its prose sometimes repeats them anyway, and a real report said:
"No consent banner or CMP was detected (signal sig1: cmp_not_found; ...)" and "with cookies set (c1-c3)". Those mean
nothing to a reader, and the code names expose how the product works inside. They are removed from the written finding
and recommendation; the `evidence` field is untouched, so every claim still traces to its evidence.
"""

import re

_ID = r"(?:sig|pol|[tcsf])\d{1,3}"
_NOT_PART_OF_A_WORD = r"(?<![\w./:-])"

_CODES = {
    "cmp_not_found": "no consent banner was found",
    "cmp_not_automatable": "the banner could not be operated by the scanner",
    "click_failed": "the click did not work",
    "page_unreachable": "the page could not be loaded",
}
_CODE_RE = re.compile(r"\b(" + "|".join(_CODES) + r")\b")

# "(c1-c3)", "(t4)", "(sig1)", "(signal sig1)", "(c1, c2 and t3)": a parenthesis holding nothing but references.
_BARE_REFERENCES = re.compile(
    r"\s*\(\s*(?:signals?\s+)?" + _NOT_PART_OF_A_WORD + _ID
    + r"(?:\s*(?:[-–,]|and)\s*" + _ID + r")*\s*\)"
)
# "signal sig1: ", "sig1: " leading a clause.
_LABELLED = re.compile(_NOT_PART_OF_A_WORD + r"(?:signals?\s+)?" + _ID + r"\s*:\s*")
# A reference left in running text: "c1-c3", "t4".
_INLINE = re.compile(_NOT_PART_OF_A_WORD + _ID + r"(?:\s*[-–]\s*" + _ID + r")?(?![\w.])")


def scrub_internal_references(text: str | None) -> str | None:
    if not text:
        return text
    out = _CODE_RE.sub(lambda m: _CODES[m.group(1)], text)
    out = _BARE_REFERENCES.sub("", out)
    out = _LABELLED.sub("", out)
    out = _INLINE.sub("", out)
    out = re.sub(r"\(\s*\)", "", out)                 # a parenthesis emptied by the above
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\s+([,.;:])", r"\1", out)
    return out.strip()
