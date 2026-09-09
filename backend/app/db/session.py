from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

_settings = get_settings()

engine = create_async_engine(
    _settings.database_url,
    pool_pre_ping=True,
    # asyncpg's default connect timeout is 60s — too slow to fail fast on a real outage
    connect_args={"timeout": 5},
    # Explicit and small, because SQLAlchemy's defaults (pool_size=5 + max_overflow=10)
    # let ONE process hold 15 connections -- which is the entire budget of Supabase's
    # session-mode pooler (port 5432, "max clients are limited to pool_size: 15").
    #
    # This was a latent bug that only surfaced once the app ran in two places at once:
    # a laptop running uvicorn plus the deployed backend and worker containers is four
    # processes against a 15-client ceiling, and the pooler started refusing every new
    # connection with EMAXCONNSESSION. The visible symptom was scans timing out at the
    # website_scan stage on BOTH machines, because a worker that cannot reach Postgres
    # cannot record evidence or advance the scan's status.
    #
    # 3 + 2 = 5 per process, so three concurrent processes stay inside the ceiling with
    # room to spare. Raise DB_POOL_SIZE only alongside a bigger pooler (or move to the
    # transaction-mode pooler on 6543, which allows far more clients -- that needs
    # prepared statements disabled for asyncpg, so it is a deliberate change, not a
    # port swap).
    pool_size=_settings.db_pool_size,
    max_overflow=_settings.db_max_overflow,
    # Recycle before Supabase's own idle cutoff so a long-idle worker doesn't wake up
    # holding a connection the server has already dropped.
    pool_recycle=1800,
)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with async_session_factory() as session:
        yield session
