import asyncio, uuid, sys
from sqlalchemy import select
from app.db.models import RegWatchSource
from app.db.session import async_session_factory, set_org_scope
ORG = uuid.UUID("8b2c939c-4993-4053-a7b5-a15fdb0b5310")
async def main():
    set_org_scope(ORG)
    async with async_session_factory() as db:
        rows = (await db.execute(select(RegWatchSource).where(
            RegWatchSource.org_id == ORG, RegWatchSource.enabled.is_(True)))).scalars().all()
        for s in rows:
            sys.stdout.buffer.write(f"  {s.name!r}\n".encode("utf-8"))
asyncio.run(main())
