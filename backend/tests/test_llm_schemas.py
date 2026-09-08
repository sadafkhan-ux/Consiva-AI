import pytest
from pydantic import ValidationError

from app.llm.schemas import ConsentAnalysisResponse, DpdpReference


def test_valid_response_parses():
    response = ConsentAnalysisResponse.model_validate({
        "findings": [{
            "finding": "Analytics cookies set with no visible consent banner.",
            "category": "analytics",
            "risk_level": "high",
            "priority": "high",
            "evidence": ["cookie-uuid-1"],
            "dpdp_reference": ["chunk-uuid-1"],
            "recommendation": "Add a consent banner before setting analytics cookies.",
            "requires_human_review": True,
        }]
    })
    assert response.findings[0].category == "analytics"
    assert response.findings[0].priority == "high"


def test_invalid_category_rejected():
    with pytest.raises(ValidationError):
        ConsentAnalysisResponse.model_validate({
            "findings": [{
                "finding": "x", "category": "not_a_real_category", "risk_level": "high", "priority": "high",
                "evidence": [], "dpdp_reference": [], "recommendation": "x",
                "requires_human_review": True,
            }]
        })


def test_missing_required_field_rejected():
    with pytest.raises(ValidationError):
        ConsentAnalysisResponse.model_validate({"findings": [{"finding": "x"}]})


def test_dpdp_reference_requires_source_doc():
    with pytest.raises(ValidationError):
        DpdpReference.model_validate({"chunk_id": "c1", "section": "Section 4"})


def test_dpdp_reference_allows_null_section_and_version():
    ref = DpdpReference.model_validate({"chunk_id": "c1", "source_doc": "DPDP Rules 2025"})
    assert ref.section is None
    assert ref.version is None
