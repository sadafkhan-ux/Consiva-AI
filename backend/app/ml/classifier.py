"""The plug-in point for Phase 2 ML (docs/architecture §J). For now this only ever
defers to the deterministic rules already computed in rules/consent_rules.py; anything
the rules couldn't resolve with high confidence falls through to human review rather
than guessing. Callers should not need to change when a real model is registered —
only the branch below does.
"""

from dataclasses import dataclass

from app.ml.model_registry import get_active_model
from app.rules.consent_rules import RuleFinding


@dataclass
class Classification:
    category: str
    confidence: float
    source: str  # "rule" | "ml"
    model_version: str | None = None


def classify(item: dict, *, rule_finding: RuleFinding | None) -> Classification:
    if rule_finding is not None and rule_finding.confidence == "high":
        return Classification(category=rule_finding.category, confidence=1.0, source="rule")

    model = get_active_model()
    if model is None:
        # No ML model registered yet, and the rules weren't confident either — this
        # item stays unresolved and the caller must route it to human review.
        raise NotImplementedError(
            "No deterministic rule matched with high confidence and no ML model is "
            "registered — route this item to human review instead of guessing."
        )

    prediction = model.predict(item)
    return Classification(
        category=prediction["category"],
        confidence=prediction["confidence"],
        source="ml",
        model_version=model.version,
    )
