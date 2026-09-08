from app.llm.client import NvidiaLLMClient
from app.rag.chunker import Chunk

_BATCH_SIZE = 32


async def embed_chunks(chunks: list[Chunk], llm_client: NvidiaLLMClient) -> list[list[float]]:
    """Embeds in batches with input_type="passage" (indexing side of NIM's asymmetric
    retrieval — see docs/architecture §H)."""
    embeddings: list[list[float]] = []
    for i in range(0, len(chunks), _BATCH_SIZE):
        batch = chunks[i : i + _BATCH_SIZE]
        batch_embeddings = await llm_client.embed([c.content for c in batch], input_type="passage")
        embeddings.extend(batch_embeddings)
    return embeddings
