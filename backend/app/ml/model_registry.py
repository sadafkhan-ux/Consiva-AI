"""Phase 2 stub (docs/architecture §10/§J). No model exists yet — deliberately not
invented just to have one. Once enough reviewed outcomes accumulate in `approvals`,
register a trained model here; `classifier.py` will pick it up automatically."""

from typing import Protocol


class RegisteredModel(Protocol):
    name: str
    version: str

    def predict(self, features: dict) -> dict: ...


_active_model: RegisteredModel | None = None


def register(model: RegisteredModel) -> None:
    global _active_model
    _active_model = model


def get_active_model() -> RegisteredModel | None:
    return _active_model
