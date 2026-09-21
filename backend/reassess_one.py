"""Re-assess one finding now the threshold comes from settings, and report what changed."""
import asyncio, uuid
from sqlalchemy import select
from app.db.models import RegWatchFinding
from app.db.session import async_session_factory, set_org_scope
from app.agents.regwatch.services import assessment_service
from app.agents.regwatch.schemas import watch

ORG = uuid.UUID("8b2c939c-4993-4053-a7b5-a15fdb0b5310")

async def main():
    set_org_scope(ORG)
    async with async_session_factory() as db:
        f = (await db.execute(
            select(RegWatchFinding).where(
                RegWatchFinding.org_id == ORG,
                RegWatchFinding.status == watch.REVIEW_REQUIRED,
                RegWatchFinding.reference == "REG-A52E831F43AE"))).scalar_one_or_none()
        if f is None:
            print("finding not in review_required; nothing to re-assess")
            return
        print(f"re-assessing {f.reference}  (citations before: {len(f.citations)})")
        await assessment_service.assess(db, f)
        await db.commit()
        print(f"  status      {f.status}")
        print(f"  relevance   {f.relevance} / {f.relevance_confidence}")
        print(f"  citations   {len(f.citations)}   model={f.drafted_by_model}")
        for c in f.citations:
            print(f"     - {c['document_title']}" + (f" §{c['section']}" if c.get('section') else ""))
        print(f"  grounded    {len(f.grounded_facts)} excerpt(s)")
        print(f"  summary     {(f.summary or '')[:400]}")
        for q in (f.open_questions or [])[:3]:
            print(f"  question    {q[:130]}")
asyncio.run(main())
