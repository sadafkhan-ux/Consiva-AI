"""Whether a knowledge chunk is readable English, so unreadable text is neither embedded nor handed to the model.

The DPDP Rules and Act PDFs are bilingual. Their Hindi pages extract as legacy-encoded text that decodes to garbage
("ती”से ऐसा कोई मध्यवती अजभप्रेत है िो अपिे क ं प्यूटर ..."). On the stored knowledge base that is 54 of 279 chunks (51 of the 105
DPDP Rules chunks): each one is embedded, can be retrieved, and then costs ~320 prompt tokens to tell the model nothing, and
cannot be cited meaningfully either. The English text of the same provisions is in the knowledge base, so excluding these
loses no provision."""

# Letters beyond Latin Extended-B (U+024F) are Devanagari and other non-Latin scripts. Accented Latin letters stay below it.
_LATIN_LIMIT = 0x024F
# Above this share of non-Latin letters a chunk is treated as not English. Measured on the stored corpus: English chunks sit
# at 0.0 and the garbled Hindi ones well above 0.3, so the threshold is not delicate.
MAX_NON_LATIN_SHARE = 0.3


def is_readable_text(text: str, *, max_non_latin_share: float = MAX_NON_LATIN_SHARE) -> bool:
    """False for text that is mostly non-Latin letters. Text with no letters at all is not readable either."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    non_latin = sum(1 for c in letters if ord(c) > _LATIN_LIMIT)
    return non_latin / len(letters) <= max_non_latin_share
