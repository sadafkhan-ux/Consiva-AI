import os

import pytest


@pytest.fixture(autouse=True)
def _no_alert_variables_from_the_machine(monkeypatch):
    """backend/.env may hold real ALERT_* values (recipients, the mail server). Tests are about the rules, not this machine, so none of them may leak in."""
    for name in [n for n in os.environ if n.startswith("ALERT_")]:
        monkeypatch.delenv(name)
