"""Start-up check: the cookie lookup table (the Open Cookie Database, ~2,266 entries) must be loaded.

Without it every cookie that the small built-in vendor list does not name is "unclassified": a scan of hindustantimes.com
on the Docker stack, where the table had 0 rows against 2,266 on the dev database, left 443 of 457 cookies unclassified.
Rule R-001 only counts analytics and marketing cookies, so an unclassified one is invisible to it.
"""

import logging

from sqlalchemy import select

from app.db.models import CookieLookup
from app.db.session import async_session_factory

logger = logging.getLogger(__name__)


async def warn_if_cookie_lookup_empty() -> bool:
    """Log an error and return True when the table has no rows. Never raises: an unreadable database is reported by the
    checks that own that, and must not stop the API from booting."""
    try:
        async with async_session_factory() as db:
            row = (await db.execute(select(CookieLookup.id).limit(1))).first()
    except Exception:
        logger.warning("Could not check the cookie lookup table at start-up", exc_info=True)
        return False
    if row is None:
        logger.error(
            "The cookie lookup table is EMPTY: cookies the built-in vendor list does not name will be unclassified, "
            "and the consent rules cannot see them. Load it with: python -m app.lookup.loader"
        )
    return row is None
