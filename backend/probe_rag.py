"""Is the corpus empty, or is the distance threshold too tight?

If the knowledge base genuinely has nothing about these sources, skipping the
interpretation is correct. If it has DPDP content and the cutoff is just set too
low, then the grounded-interpretation path is dead code and nobody would know.
"""
import asyncio, uuid
from sqlalchemy import func, select
from app.db.models import KnowledgeChunk, KnowledgeDocument
from app.db.session import async_session_factory
from app.config import get_settings
from app.llm.client import NvidiaLLMClient
from app.rag import retriever
from app.agents.regwatch.services import assessment_service

async def main():
    async with async_session_factory() as db:
        docs = (await db.execute(select(KnowledgeDocument.title, KnowledgeDocument.is_approved))).all()
        n = (await db.execute(select(func.count()).select_from(KnowledgeChunk))).scalar_one()
        print(f"knowledge documents: {len(docs)}  ({sum(1 for _, a in docs if a)} approved)")
        for t, a in docs[:10]:
            print(f"   {'approved' if a else 'DRAFT   '}  {t[:70]}")
        print(f"knowledge chunks: {n}")
        if not n:
            print("\nThe corpus is empty. Skipping interpretation is the only honest outcome.")
            return

        print(f"\nretrieval threshold currently: {assessment_service.RAG_MAX_DISTANCE}")
        client = NvidiaLLMClient(get_settings())
        queries = [
            "India rules policy notification consent data fiduciary",
            "EU consent rights transfer guidance data protection board",
            "security breach incident reasonable security safeguards",
            "consent notice withdrawal data principal rights",
        ]
        for q in queries:
            chunks = await retriever.retrieve(q, db=db, llm_client=client, top_k=5)
            if not chunks:
                print(f"  no chunks at all for: {q[:50]}")
                continue
            ds = [round(c.distance, 3) for c in chunks]
            kept = [d for d in ds if d <= assessment_service.RAG_MAX_DISTANCE]
            print(f"  distances {ds}  kept {len(kept)}/5   <- {q[:48]}")
            print(f"       nearest: {chunks[0].document_title[:60]}")
asyncio.run(main())
