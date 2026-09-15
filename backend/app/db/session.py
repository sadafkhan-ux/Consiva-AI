import logging
import uuid
from collections.abc import AsyncIterator
from contextvars import ContextVar

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.config import get_settings

logger = logging.getLogger(__name__)

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


# ── Tenant scope for row-level security (migration 0018) ────────────────────────
#
# Postgres policies call `current_org_id()`, which reads the `app.org_id` setting.
# Something has to put the organisation there, once per request, from the VERIFIED
# token -- never from anything the caller can choose.
#
# WHY A ContextVar AND AN EVENT, RATHER THAN A DEPENDENCY
# -------------------------------------------------------
# The obvious approach -- a dependency that runs `set_config` -- sets the value inside
# whatever transaction is open at the time. Routes commit partway through and then keep
# querying; a transaction-local setting does not survive that commit, so the next
# statement would run unscoped. Re-applying it on every `after_begin` covers the first
# transaction and every one after it, without asking eighty-seven call sites to
# remember.
#
# The ContextVar is per-asyncio-context, so one request cannot read another's scope
# even though both borrow connections from the same pool.
_org_scope: ContextVar[str | None] = ContextVar("consiva_org_scope", default=None)


def set_org_scope(org_id: str | uuid.UUID | None) -> None:
    """Bind this request to an organisation. Called from the auth dependency with the
    org_id off the verified token, and from nowhere else."""
    if org_id is None:
        _org_scope.set(None)
        return
    # Parsed, not trusted. The value is interpolated into SQL below -- a UUID that
    # round-trips through uuid.UUID cannot carry anything else.
    _org_scope.set(str(uuid.UUID(str(org_id))))


async def apply_org_scope(db: AsyncSession, org_id: str | uuid.UUID) -> None:
    """Bind the scope AND stamp the transaction that is already open.

    `after_begin` only fires when a transaction starts. The login and integration-key
    paths have to read the database BEFORE they know which organisation they are
    acting for, so by the time they find out, a transaction is already underway and
    the hook has been and gone. This covers that one, and the ContextVar covers every
    transaction after it.
    """
    set_org_scope(org_id)
    scope = _org_scope.get()
    if scope:
        await db.execute(text(f"set local app.org_id = '{scope}'"))


def get_org_scope() -> str | None:
    return _org_scope.get()


def clear_org_scope() -> None:
    _org_scope.set(None)


@event.listens_for(Session, "after_begin")
def _apply_org_scope(session, transaction, connection) -> None:
    """Stamp the organisation onto each new transaction.

    No scope set means no `app.org_id`, which means `current_org_id()` returns NULL,
    which means every tenant policy evaluates false and the connection sees nothing.
    Failing closed is the point: the worker has no request and therefore no scope, and
    it is expected to run as the privileged role instead (see migration 0018).
    """
    org = _org_scope.get()
    if org is None:
        return
    try:
        # Safe to interpolate: `set_org_scope` has already parsed this as a UUID.
        connection.exec_driver_sql(f"set local app.org_id = '{org}'")
    except Exception:
        # A database that has not had migration 0018 applied has no use for this, and
        # a failure to stamp scope must not take down a request that RLS is not yet
        # enforcing. Logged rather than swallowed silently.
        logger.warning("Could not apply org scope to this transaction", exc_info=True)
