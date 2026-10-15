from app.rag.chunker import chunk_pages


def test_short_page_becomes_single_chunk():
    chunks = chunk_pages([{"page_number": 1, "text": "Short text."}])
    assert len(chunks) == 1
    assert chunks[0].metadata["page_number"] == 1


def test_long_page_splits_with_overlap():
    text = " ".join(f"word{i}" for i in range(1000))
    chunks = chunk_pages([{"page_number": 1, "text": text}], max_chars=200, overlap_chars=50)
    assert len(chunks) > 1
    assert all(c.metadata["page_number"] == 1 for c in chunks)
    # overlap: the last word of one chunk should reappear in the next chunk
    last_word_of_first_chunk = chunks[0].content.split()[-1]
    assert last_word_of_first_chunk in chunks[1].content


def test_empty_page_produces_no_chunks():
    assert chunk_pages([{"page_number": 1, "text": "   "}]) == []


def test_chunk_indices_are_sequential_across_pages():
    chunks = chunk_pages([
        {"page_number": 1, "text": "First page text."},
        {"page_number": 2, "text": "Second page text."},
    ])
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_section_heading_detected_when_present():
    text = "Preamble text.\nSection 4. Notice for consent\nA data fiduciary shall give notice..."
    [chunk] = chunk_pages([{"page_number": 1, "text": text}])
    assert chunk.metadata["section"] is not None
    assert chunk.metadata["section"].startswith("Section 4")


def test_section_is_none_when_not_detected():
    [chunk] = chunk_pages([{"page_number": 1, "text": "Just some ordinary paragraph text with no headings."}])
    assert chunk.metadata["section"] is None


def test_section_carries_into_later_chunk_of_same_page():
    body = "Rule 12. Consent manager obligations\n" + " ".join(f"clause{i}" for i in range(500))
    chunks = chunk_pages([{"page_number": 1, "text": body}], max_chars=300, overlap_chars=50)
    assert len(chunks) > 1
    assert all(c.metadata["section"] is not None and c.metadata["section"].startswith("Rule 12") for c in chunks)


# ── A mid-sentence cross-reference is not a section heading ─────────────────────
# These reach the customer-facing report as a finding's cited DPDP reference, so a
# real-looking but wrong one is worse than none. Measured across the whole ingested
# corpus: all 15 capitalised matches are genuine headings, all 21 lower-case ones are
# body text a PDF line-wrap pushed to the start of a line.

def test_a_lowercase_cross_reference_is_not_captured_as_a_heading():
    from app.rag.chunker import chunk_pages

    page_text = (
        "a Consent Manager shall be registered with the Board in the manner specified in\n"
        "section 35;] and shall comply with such obligations as may be prescribed.\n"
    )
    [chunk] = chunk_pages([{"page_number": 1, "text": page_text}])

    assert chunk.metadata["section"] is None, (
        "a mid-sentence reference was cited as the section supporting a finding"
    )


def test_a_real_capitalised_heading_is_still_captured():
    from app.rag.chunker import chunk_pages

    page_text = "CHAPTER II\nOBLIGATIONS OF DATA FIDUCIARY\nEvery Data Fiduciary shall.\n"
    [chunk] = chunk_pages([{"page_number": 1, "text": page_text}])

    assert chunk.metadata["section"] == "CHAPTER II"
