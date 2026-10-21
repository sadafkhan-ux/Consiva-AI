"""Internal ids and status codes are removed from the prose of a finding; the evidence list is not touched."""

import pytest

from app.llm.text_hygiene import scrub_internal_references as scrub


@pytest.mark.parametrize("raw,expected", [
    # Both phrases are from a real report on smartians.ai.
    ("No consent banner or CMP was detected (signal sig1: cmp_not_found; accept and reject could not be tested).",
     "No consent banner or CMP was detected (no consent banner was found; accept and reject could not be tested)."),
    ("hosts are unclassified and fired before any consent interaction, with cookies set (c1-c3). Their purpose is unknown.",
     "hosts are unclassified and fired before any consent interaction, with cookies set. Their purpose is unknown."),
    ("Trackers (t4, t5 and c2) fired before consent.", "Trackers fired before consent."),
    ("The Reject control returned cmp_not_automatable.", "The Reject control returned the banner could not be operated by the scanner."),
    ("Nothing internal here.", "Nothing internal here."),
    ("", ""),
])
def test_internal_references_are_removed_and_ordinary_text_is_kept(raw, expected):
    assert scrub(raw) == expected


@pytest.mark.parametrize("text", [
    "Requests to t1.example.com and https://c1.cdn.test/a.js fired before consent.",     # hostnames are not references
    "Section 6(4) and rule 17 apply; see the 2023 Act.",
    "The page loaded in 3s with 5 requests.",
])
def test_hostnames_sections_and_numbers_are_never_mistaken_for_references(text):
    assert scrub(text) == text


def test_none_passes_through():
    assert scrub(None) is None
