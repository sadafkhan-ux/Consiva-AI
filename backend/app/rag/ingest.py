"""Ingestion pipeline: PDF -> text -> chunks -> embeddings -> knowledge_documents /
knowledge_chunks. Run standalone against the 6 existing DPDP/IT Act PDFs:

    python -m app.rag.ingest "../knowledge_sources/DPDP Rules.pdf" \\
        --title "DPDP Rules" --source-type dpdp_rules --approve

Ingested documents default to is_approved=False — retriever.py only reads approved
documents, so ingestion alone never makes new content live in the RAG pipeline. Pass
--approve only for content that's actually been reviewed as an authoritative source
(docs/architecture §4: "Approved Consiva policies / approved compliance documents").
"""

import argparse
import asyncio
import hashlib
from pathlib import Path

from app.db.models import KnowledgeChunk, KnowledgeDocument
from app.db.session import async_session_factory
from app.llm.client import NvidiaLLMClient
from app.rag.chunker import chunk_pages
from app.rag.embedder import embed_chunks
from app.rag.loaders.pdf_loader import load_pdf


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def ingest_document(
    path: Path, *, title: str, source_type: str, approve: bool = False, version: str | None = None
) -> str:
    pages = load_pdf(path)
    chunks = chunk_pages(pages)
    if not chunks:
        raise ValueError(f"No extractable text in {path}")

    llm_client = NvidiaLLMClient()
    embeddings = await embed_chunks(chunks, llm_client)

    async with async_session_factory() as db:
        document = KnowledgeDocument(
            title=title,
            source_type=source_type,
            source_ref=str(path),
            version=version,
            checksum=_checksum(path),
            is_approved=approve,
        )
        db.add(document)
        await db.flush()  # assigns document.id

        db.add_all([
            KnowledgeChunk(
                document_id=document.id,
                chunk_index=chunk.chunk_index,
                content=chunk.content,
                embedding=embedding,
                token_count=chunk.token_count,
                chunk_metadata=chunk.metadata,
            )
            for chunk, embedding in zip(chunks, embeddings, strict=True)
        ])
        await db.commit()
        return str(document.id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest a document into the RAG knowledge base")
    parser.add_argument("path", type=Path)
    parser.add_argument("--title", required=True)
    parser.add_argument(
        "--source-type", required=True,
        choices=["dpdp_act", "dpdp_rules", "govt_guidance", "govt_notification", "consiva_policy", "other"],
    )
    parser.add_argument("--version", default=None)
    parser.add_argument("--approve", action="store_true", help="Mark immediately retrievable")
    args = parser.parse_args()

    document_id = asyncio.run(ingest_document(
        args.path, title=args.title, source_type=args.source_type, approve=args.approve, version=args.version
    ))
    print(f"Ingested {args.path} -> knowledge_documents.id={document_id} (approved={args.approve})")


if __name__ == "__main__":
    main()
