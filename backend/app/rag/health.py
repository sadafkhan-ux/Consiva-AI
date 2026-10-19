"""Start-up check: an empty regulatory knowledge base should be said out loud, not discovered later from a report
whose findings all read "DPDP reference: none supplied". Seen on the Docker stack (0 documents, 0 chunks)."""

import logging

from app.db.session import async_session_factory
from app.rag.retriever import _knowledge_base_has_chunks

logger = logging.getLogger(__name__)


async def warn_if_knowledge_base_empty() -> bool:
    """Log an error and return True when no approved knowledge chunk exists. Never raises: a database that
    cannot be read here is reported by the checks that own that, and must not stop the API from booting."""
    try:
        async with async_session_factory() as db:
            has_chunks = await _knowledge_base_has_chunks(db)
    except Exception:
        logger.warning("Could not check the RAG knowledge base at start-up", exc_info=True)
        return False
    if not has_chunks:
        logger.error(
            "RAG knowledge base has NO approved chunks: scans will run, but findings cannot cite any DPDP "
            "provision. Ingest the sources (python -m app.rag.ingest) before relying on citations."
        )
    return not has_chunks
