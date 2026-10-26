"""Which environment variables a source's `credential_ref` may name.

A ROPA, DSR or Regulatory Watch source stores `credential_ref`, the NAME of an environment variable that holds its secret. The server reads that variable
when it connects and sends the value to the source's host. Left unrestricted, a user who can create a source could name one of the platform's OWN secrets
(the JWT signing key, the database URL, a model API key), point the source at a host they control, and have the server send the secret there
(docs/CODE_AUDIT_2026-10-16.md, finding C1).

So a reference is refused when it names a variable the platform itself uses: every setting the application reads, a few well-known secret names, and the
platform's own prefixes. When `credential_ref_required_prefix` is set, a reference must also start with it, which is the stronger form of the same rule
(the allowlist the audit recommends); it is off by default so that sources already configured keep working.
"""

from __future__ import annotations

from app.config import Settings, get_settings

_PLATFORM_PREFIXES = ("SUPABASE_", "ROPA_CREDENTIAL", "AWS_", "AZURE_", "GOOGLE_APPLICATION")
_WELL_KNOWN = frozenset({
    "DATABASE_URL", "WORKER_DATABASE_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY", "NVIDIA_API_KEY", "LLM_PRIMARY_API_KEY",
    "WEBHOOK_SIGNING_SECRET", "AGENT_WEBHOOK_SECRET", "SECRET_KEY", "JWT_SECRET", "JWT_SECRET_KEY", "PATH", "HOME", "USER",
    "POSTGRES_PASSWORD", "POSTGRES_USER", "POSTGRES_DB", "PGPASSWORD", "PGPASSFILE", "PGSERVICE", "PGUSER", "PGHOST",
})


class CredentialRefNotAllowedError(ValueError):
    """The named variable is one the platform keeps for itself, or does not carry the required prefix."""


def _reserved_names() -> set[str]:
    return {name.upper() for name in Settings.model_fields} | set(_WELL_KNOWN)


def assert_credential_ref_allowed(name: str, *, settings: Settings | None = None) -> str:
    """Returns `name` when a source may read that variable; raises CredentialRefNotAllowedError otherwise. The message never repeats a value."""
    cleaned = (name or "").strip()
    upper = cleaned.upper()
    if upper in _reserved_names() or upper.startswith(_PLATFORM_PREFIXES):
        raise CredentialRefNotAllowedError(
            "credential_ref names an environment variable the platform uses itself and cannot be used for a source. Name a variable made for this source."
        )
    prefix = (settings or get_settings()).credential_ref_required_prefix
    if prefix and not cleaned.startswith(prefix):
        raise CredentialRefNotAllowedError(f"credential_ref must start with {prefix!r}.")
    return cleaned
