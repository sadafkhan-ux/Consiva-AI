"""Nothing that looks like a secret may reach the alerts table, the dashboard or an email. Applied to every piece of text that comes from a failed call."""

from __future__ import annotations

import re

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-…[hidden]"),                                         # Anthropic and OpenAI style keys
    (re.compile(r"nvapi-[A-Za-z0-9_\-]{8,}"), "nvapi-…[hidden]"),                                   # NVIDIA keys
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{8,}"), "Bearer […hidden]"),
    (re.compile(r"(?i)(x-api-key|api[_-]?key|apikey|authorization|token|secret|password|passwd)(\s*[=:]\s*)[^\s,;&\"']+"), r"\1\2[hidden]"),
    (re.compile(r"(?i)(://[^/\s:@]+:)[^@\s]+@"), r"\1[hidden]@"),                                  # user:password@host in a URL
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*"), "[jwt hidden]"),   # a JSON web token
)


def scrub(text: str | None, limit: int = 400) -> str:
    """The text with keys, tokens and passwords replaced, cut to `limit` characters on one line."""
    if not text:
        return ""
    out = " ".join(str(text).split())
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out if len(out) <= limit else out[: limit - 1] + "…"
