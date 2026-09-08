from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import KnowledgeChunk, KnowledgeDocument
from app.llm.client import NvidiaLLMClient


@dataclass
class RetrievedChunk:
    chunk_id: str
    document_id: str
    document_title: str
    document_version: str | None
    content: str
    distance: float
    metadata: dict

    @property
    def section(self) -> str | None:
        return self.metadata.get("section")

    def as_prompt_dict(self) -> dict:
        """Lean view for the LLM prompt — just enough to cite by chunk_id. Section/
        version live on the full dataclass for create_findings.py to build the
        structured citation objects; the LLM never needs to transcribe them itself."""
        return {"chunk_id": self.chunk_id, "document_title": self.document_title, "content": self.content}

    def as_state_dict(self) -> dict:
        """Full view persisted into AgentState.rag_chunks — everything downstream
        consumers (prompting, citation-building) need, in one place."""
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "document_title": self.document_title,
            "document_version": self.document_version,
            "content": self.content,
            "section": self.section,
        }


async def retrieve(
    query: str, *, db: AsyncSession, llm_client: NvidiaLLMClient, top_k: int = 5,
    max_distance: float | None = None,
) -> list[RetrievedChunk]:
    """Cosine-similarity search over approved knowledge_chunks only (docs/architecture
    §I) — unapproved/draft documents never reach the LLM.

    `max_distance`, when set, drops chunks whose cosine distance exceeds it instead of
    padding the result out to top_k with barely-related material — a query about a
    topic genuinely absent from the knowledge base should return LESS context, not
    fake-relevant context the LLM is then invited to cite. Filtered in Python after
    the top_k fetch (k is small) so the SQL stays one simple ordered query."""
    [query_embedding] = await llm_client.embed([query], input_type="query")

    distance = KnowledgeChunk.embedding.cosine_distance(query_embedding).label("distance")
    stmt = (
        select(KnowledgeChunk, KnowledgeDocument.title, KnowledgeDocument.version, distance)
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .where(KnowledgeDocument.is_approved.is_(True))
        .order_by(distance)
        .limit(top_k)
    )
    result = await db.execute(stmt)

    chunks = [
        RetrievedChunk(
            chunk_id=str(chunk.id),
            document_id=str(chunk.document_id),
            document_title=title,
            document_version=version,
            content=chunk.content,
            distance=float(dist),
            metadata=chunk.chunk_metadata,
        )
        for chunk, title, version, dist in result.all()
    ]
    if max_distance is not None:
        chunks = [c for c in chunks if c.distance <= max_distance]
    return chunks
