"""How big is an Agent 5 interpretation prompt, against the model's context?

The self-hosted model previously rejected a 6,462-token Agent 1 prompt against a
4,096-token context. If Agent 5's prompts are comparable, every assessment burns
retries before falling back, which is slow rather than wrong -- but worth knowing.
"""
import asyncio, uuid
from sqlalchemy import select
from app.db.models import RegWatchChange, RegWatchFinding, RegWatchSource
from app.db.session import async_session_factory, set_org_scope
from app.agents.regwatch.services import assessment_service
from app.config import get_settings

ORG = uuid.UUID("8b2c939c-4993-4053-a7b5-a15fdb0b5310")

async def main():
    set_org_scope(ORG)
    s = get_settings()
    print("primary base url :", s.llm_primary_base_url)
    print("primary model    :", (s.llm_primary_model or "")[:60])
    print("MAX_CHANGE_CHARS :", assessment_service.MAX_CHANGE_CHARS)
    print("RAG_TOP_K        :", assessment_service.RAG_TOP_K)
    async with async_session_factory() as db:
        rows = (await db.execute(
            select(RegWatchFinding, RegWatchChange, RegWatchSource)
            .join(RegWatchChange, RegWatchChange.id == RegWatchFinding.change_id)
            .join(RegWatchSource, RegWatchSource.id == RegWatchFinding.source_id)
            .where(RegWatchFinding.org_id == ORG))).all()
        print(f"\n{'source':<38} change_text  est. prompt tokens")
        for _f, ch, src in rows:
            text = assessment_service._change_text(ch) or ""
            # System + change text + ~5 corpus chunks of ~1-2k chars each.
            approx_chars = len(assessment_service._SYSTEM_PROMPT) + len(text) + 5 * 1500
            print(f"  {src.name[:36]:<36} {len(text):>6}      ~{approx_chars // 4:>6} tokens")
asyncio.run(main())
