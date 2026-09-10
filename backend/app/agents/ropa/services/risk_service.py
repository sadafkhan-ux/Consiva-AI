"""Risk / gap detection and retention & access findings (ROPA prompt §12, §13, §16, §17).

Two hard constraints from the prompt shape this module:

- §16: "A gap is NOT automatically a legal violation." Every finding uses the
  neutral GapStatus vocabulary and no finding ever asserts a statutory breach.
- §17: risk scoring must be explainable, so every finding carries the concrete
  `severity_factors` that produced its severity rather than an opaque number.

Retention and access are reported from evidence.business_metadata / evidence.roles
only. Where that evidence is absent the value stays "Unknown" and becomes a gap --
the prompt explicitly forbids inventing "3 years" or an owner name.
"""

from __future__ import annotations

from app.agents.ropa.rules.personal_data_rules import SENSITIVE_CATEGORIES
from app.agents.ropa.schemas.evidence import DiscoveryEvidence
from app.agents.ropa.schemas.output import AccessFinding, RetentionFinding
from app.agents.ropa.schemas.ropa import PersonalDataElement, ProcessingActivity, RiskGapFinding

# Unknown-classification columns are reported as ONE aggregated finding rather
# than one per column: a 200-column database would otherwise bury every other
# gap under review noise.
_UNKNOWN_SAMPLE_SIZE = 10


def detect_gaps(
    evidence: DiscoveryEvidence,
    elements: list[PersonalDataElement],
    activities: list[ProcessingActivity],
    retention: list[RetentionFinding],
    access: list[AccessFinding],
) -> list[RiskGapFinding]:
    findings: list[RiskGapFinding] = []

    sensitive = [e for e in elements if e.classification in SENSITIVE_CATEGORIES]
    if sensitive:
        findings.append(
            RiskGapFinding(
                finding=(
                    f"{len(sensitive)} column(s) classified into sensitive categories "
                    f"({', '.join(sorted({e.classification for e in sensitive}))})."
                ),
                status="Potential Privacy Risk",
                related_evidence=sorted({ref for e in sensitive for ref in e.evidence})[:50],
                severity="high",
                severity_factors=[
                    "sensitive data category",
                    f"{len(sensitive)} affected column(s)",
                ],
                confidence=round(sum(e.confidence for e in sensitive) / len(sensitive), 4),
                recommendation="Confirm lawful basis, access restrictions and retention for these columns.",
                review_required=True,
            )
        )

    unknown = [e for e in elements if e.classification == "Unknown"]
    if unknown:
        sample = ", ".join(f"{e.table}.{e.column}" for e in unknown[:_UNKNOWN_SAMPLE_SIZE])
        findings.append(
            RiskGapFinding(
                finding=(
                    f"{len(unknown)} column(s) could not be classified from evidence. "
                    f"Examples: {sample}."
                ),
                status="Evidence Incomplete",
                related_evidence=sorted({ref for e in unknown for ref in e.evidence})[:50],
                severity="medium",
                severity_factors=["unknown classification", f"{len(unknown)} unclassified column(s)"],
                confidence=1.0,  # the absence of a classification is itself certain
                recommendation="Human reviewer should classify or explicitly mark these as non-personal.",
                review_required=True,
            )
        )

    unknown_purpose = [a for a in activities if a.purpose == "Unknown"]
    if unknown_purpose:
        findings.append(
            RiskGapFinding(
                finding=(
                    f"{len(unknown_purpose)} processing activity/activities hold personal data "
                    "with no processing purpose established from evidence."
                ),
                status="Potential Gap",
                related_evidence=sorted({ref for a in unknown_purpose for ref in a.evidence})[:50],
                severity="high",
                severity_factors=["unknown processing purpose", "personal data present"],
                confidence=1.0,
                recommendation="Confirm the business purpose for each activity before publishing the ROPA.",
                review_required=True,
            )
        )

    missing_retention = [r for r in retention if r.retention == "Unknown"]
    if missing_retention:
        findings.append(
            RiskGapFinding(
                finding=f"No retention period is evidenced for {len(missing_retention)} table(s).",
                status="Potential Gap",
                related_evidence=sorted({ref for r in missing_retention for ref in r.evidence})[:50],
                severity="medium",
                severity_factors=["missing retention evidence"],
                confidence=1.0,
                recommendation="Attach a documented retention policy to each processing activity.",
                review_required=True,
            )
        )

    missing_owner = [a for a in access if a.owner == "Unknown"]
    if missing_owner:
        findings.append(
            RiskGapFinding(
                finding=f"No business owner is evidenced for {len(missing_owner)} table(s).",
                status="Potential Gap",
                related_evidence=sorted({ref for a in missing_owner for ref in a.evidence})[:50],
                severity="low",
                severity_factors=["missing ownership metadata"],
                confidence=1.0,
                recommendation="Assign an accountable owner per processing activity.",
                review_required=True,
            )
        )

    if not evidence.vendors:
        findings.append(
            RiskGapFinding(
                finding="No vendor/processor evidence was supplied, so downstream data sharing is unmapped.",
                status="Evidence Incomplete",
                related_evidence=[s.local_id for s in evidence.sources],
                severity="medium",
                severity_factors=["no processor evidence", "data flow incomplete"],
                confidence=1.0,
                recommendation="Supply vendor/integration metadata so transfers and DPAs can be assessed.",
                review_required=True,
            )
        )

    return findings


def build_retention_findings(evidence: DiscoveryEvidence) -> list[RetentionFinding]:
    """Retention comes only from supplied business metadata (prompt §12)."""
    by_subject = {m.subject_local_id: m for m in evidence.business_metadata}
    findings: list[RetentionFinding] = []
    for table in evidence.tables:
        meta = by_subject.get(table.local_id)
        retention = meta.retention_policy if meta and meta.retention_policy else "Unknown"
        findings.append(
            RetentionFinding(
                target=table.table_name,
                retention=retention,
                evidence=[table.local_id] + ([meta.local_id] if meta else []),
                review_required=retention == "Unknown",
            )
        )
    return findings


def build_access_findings(evidence: DiscoveryEvidence) -> list[AccessFinding]:
    """Ownership and access come only from supplied metadata/roles (prompt §13)."""
    by_subject = {m.subject_local_id: m for m in evidence.business_metadata}
    role_names = [r.name for r in evidence.roles]
    findings: list[AccessFinding] = []
    for table in evidence.tables:
        meta = by_subject.get(table.local_id)
        owner = meta.business_owner if meta and meta.business_owner else "Unknown"
        findings.append(
            AccessFinding(
                target=table.table_name,
                owner=owner,
                access_roles=role_names,
                access_status="Mapped" if role_names else "Unknown",
                evidence=[table.local_id] + ([meta.local_id] if meta else []),
                review_required=owner == "Unknown" or not role_names,
            )
        )
    return findings
