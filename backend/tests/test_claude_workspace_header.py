"""A Claude API key that is not scoped to one workspace needs the `anthropic-workspace-id` header, or every request is a 400.

Found by running a real scan end to end: the analysis stage recorded primary_status=failed with "This API key is not
scoped to a workspace ..." and fell back to NVIDIA on every run, although ANTHROPIC_WORKSPACE_ID was set in the
environment. Nothing read it.
"""

from app.config import Settings
from app.llm.client import ClaudeLLMClient


_BASE = dict(
    nvidia_api_key="k", nvidia_llm_model="m", nvidia_embed_model="e", anthropic_api_key="sk-test",
    supabase_url="http://localhost", supabase_service_role_key="k", supabase_jwt_secret="s" * 32,
    database_url="postgresql+asyncpg://u:p@localhost/db",
)


def _settings(**kw):
    # An explicit value (even None) overrides backend/.env, which carries the real workspace id on this machine.
    return Settings(**{**_BASE, "anthropic_workspace_id": None, **kw})


def test_the_workspace_id_is_sent_as_a_header_when_configured():
    client = ClaudeLLMClient(_settings(anthropic_workspace_id="wrkspc_123"))
    assert client._client._custom_headers["anthropic-workspace-id"] == "wrkspc_123"


def test_no_header_is_sent_when_none_is_configured(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)  # newer SDKs also read this variable themselves
    client = ClaudeLLMClient(_settings())
    assert "anthropic-workspace-id" not in client._client._custom_headers


def test_the_setting_reads_the_environment_variable_the_deployment_already_uses(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_env")
    assert Settings(**_BASE).anthropic_workspace_id == "wrkspc_env"


def test_claude_has_room_for_its_reasoning_plus_the_answer():
    """Measured: thinking used 3,041 of a 4,000-token ceiling and the answer was truncated; the retry's answer was 2,631."""
    client = ClaudeLLMClient(_settings())
    assert client._max_output_tokens >= 3_041 + 2_631
