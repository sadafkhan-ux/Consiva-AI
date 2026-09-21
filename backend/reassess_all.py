"""Re-run assessment on every finding now that each field is a function of one run."""
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
        rows = (await db.execute(select(RegWatchFinding).where(
            RegWatchFinding.org_id == ORG,
            RegWatchFinding.status == watch.REVIEW_REQUIRED))).scalars().all()
        print(f"{len(rows)} finding(s) to re-assess\n")
        for f in rows:
            try:
                await assessment_service.assess(db, f)
                await db.commit()
            except Exception as exc:
                await db.rollback()
                print(f"  {f.reference}  FAILED: {type(exc).__name__}: {exc}")
                continue
            consistent = (len(f.citations) > 0) == (f.drafted_by_model is not None)
            print(f"  {f.reference}  {f.relevance:<13}/{f.relevance_confidence:<9} "
                  f"priority={str(f.priority):<7} citations={len(f.citations)} "
                  f"questions={len(f.open_questions)} consistent={consistent}")
asyncio.run(main())
