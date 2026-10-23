"""Stored embeddings of the provisions the consent rules are about, used when the hosted embedding service does not answer.

Each retrieval query is the rule's own summary followed, in brackets, by the provision it concerns ("... (consent under section 6, processing
personal data before consent is given)", retrieve_rag._query_text and consent_rules.RULE_DPDP_TOPIC_HINTS). The bracketed part is constant for a
rule and is what decides WHICH provision is retrieved, so its embedding can be computed once and kept. When the service is down, a query whose
provision is stored here is searched with that stored vector: the same cosine search over the same chunks, not a different method.

The vectors are only valid for the model and width they were made with; the file records both, and a mismatch makes it unusable (it is then
ignored, never applied). Regenerate it, with the service up, after changing NVIDIA_EMBED_MODEL or its dimensions:

    python -m app.rag.topic_embeddings [output path]
"""

import asyncio
import json
import logging
import re
from functools import lru_cache
from pathlib import Path

from app.config import get_settings
from app.rules.consent_rules import RULE_DPDP_TOPIC_HINTS

logger = logging.getLogger(__name__)

PATH = Path(__file__).with_name("topic_embeddings.json")
# The query used when a scan matched no rule: it has no provision in brackets, and is stored whole.
GENERAL_QUERY = "General DPDP Act consent, cookie, and website tracking compliance requirements"
_BRACKETED = re.compile(r"\(([^()]*)\)\s*$")


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# The provisions the rules name. Matched whole, longest first, because one of them contains brackets of its own ("... sub-section (4) of
# section 6"), which a plain "last bracketed group" search would cut short.
_KNOWN = sorted({_normalise(h) for h in RULE_DPDP_TOPIC_HINTS.values()}, key=len, reverse=True)
_GENERAL = _normalise(GENERAL_QUERY)


def topic_of(query: str) -> str | None:
    """The provision named in brackets at the end of a retrieval query, or None."""
    q = _normalise(query)
    if q == _GENERAL:
        return _GENERAL
    for known in _KNOWN:
        if q.endswith(f"({known})"):
            return known
    m = _BRACKETED.search(q)
    return _normalise(m.group(1)) if m else None


@lru_cache(maxsize=1)
def _load() -> dict[str, list[float]]:
    settings = get_settings()
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if data.get("model") != settings.nvidia_embed_model or data.get("dimensions") != settings.nvidia_embed_dimensions:
        logger.warning("Stored topic embeddings were made with a different model or width and are ignored; regenerate them")
        return {}
    return {_normalise(k): v for k, v in (data.get("topics") or {}).items() if isinstance(v, list) and len(v) == data["dimensions"]}


def clear_cache() -> None:
    _load.cache_clear()


def stored_vector(query: str) -> list[float] | None:
    """The stored embedding of the provision this query is about, or None when it names none that is stored."""
    topic = topic_of(query)
    return _load().get(topic) if topic else None


async def _generate(target: Path = PATH) -> None:
    from app.llm.client import NvidiaLLMClient   # imported here: the module is also imported by the retriever

    settings = get_settings()
    topics = list(dict.fromkeys([*(_normalise(h) for h in RULE_DPDP_TOPIC_HINTS.values()), _GENERAL]))
    vectors = await NvidiaLLMClient().embed(topics, input_type="query")
    target.write_text(
        json.dumps({
            "model": settings.nvidia_embed_model, "dimensions": settings.nvidia_embed_dimensions,
            "topics": {t: [round(x, 6) for x in v] for t, v in zip(topics, vectors, strict=True)},
        }),
        encoding="utf-8",
    )
    print(f"Wrote {len(topics)} topic embeddings ({settings.nvidia_embed_model}, {settings.nvidia_embed_dimensions} dims) to {target}")


if __name__ == "__main__":
    import sys

    asyncio.run(_generate(Path(sys.argv[1]) if len(sys.argv) > 1 else PATH))
