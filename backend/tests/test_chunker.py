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
