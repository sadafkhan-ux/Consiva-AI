"""Hand the first-capture findings to the worker, the same way run_collection does."""
import asyncio, uuid
from sqlalchemy import select
from app.db.models import RegWatchFinding
from app.db.session import async_session_factory, set_org_scope
from app.agents.regwatch.schemas import watch
from app.jobs import queue

ORG = uuid.UUID("8b2c939c-4993-4053-a7b5-a15fdb0b5310")

async def main():
    set_org_scope(ORG)
    async with async_session_factory() as db:
        rows = (await db.execute(
            select(RegWatchFinding).where(
                RegWatchFinding.org_id == ORG,
                RegWatchFinding.status == watch.DETECTED))).scalars().all()
        for f in rows:
            await queue.enqueue(db, org_id=ORG, job_type="regwatch_assess",
                                payload={"finding_id": str(f.id), "org_id": str(ORG)})
            print("queued assessment for", f.reference)
        await db.commit()
        print(f"{len(rows)} queued")
asyncio.run(main())
