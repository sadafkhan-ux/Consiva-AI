from app.agents.consent_agent.nodes.create_findings import _resolve_citations


def test_resolves_known_chunk_to_structured_citation():
    rag_chunks = [{
        "chunk_id": "c1", "document_title": "DPDP Rules 2025", "document_version": "2025-12-10",
        "section": "Section 4", "content": "...",
    }]
    [resolved] = _resolve_citations(["c1"], rag_chunks)
    assert resolved == {
        "chunk_id": "c1", "section": "Section 4", "source_doc": "DPDP Rules 2025", "version": "2025-12-10",
    }


def test_unknown_chunk_id_is_dropped_not_fabricated():
    rag_chunks = [{"chunk_id": "c1", "document_title": "DPDP Rules 2025", "document_version": None, "section": None}]
    resolved = _resolve_citations(["c1", "does-not-exist"], rag_chunks)
    assert len(resolved) == 1
    assert resolved[0]["chunk_id"] == "c1"


def test_missing_section_and_version_become_none_not_guessed():
    rag_chunks = [{"chunk_id": "c1", "document_title": "IT Act 2000", "document_version": None, "section": None}]
    [resolved] = _resolve_citations(["c1"], rag_chunks)
    assert resolved["section"] is None
    assert resolved["version"] is None
