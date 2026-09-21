"""Registering and maintaining approved regulatory sources (spec §4 step 1, §15).

"Only approved sources are monitored" is the first guardrail in the spec, and this
module is where it is enforced. Nothing else in Agent 5 fetches a URL: the collector
takes a source ROW, never a string, so there is no path from a caller-supplied URL to
an HTTP request that does not pass through a registration a person made.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.regwatch.errors import InvalidSourceError, SourceNotFoundError
from app.agents.regwatch.schemas import watch
from app.db.models import RegWatchSource
from app.db.repositories import regwatch_repository as repo
from app.scanner.url_safety import assert_safe_url
from app.services import audit_service

logger = logging.getLogger(__name__)

# A regulator's page does not change every five minutes, and asking it to is closer to
# a denial of service than to monitoring. The floor is enforced in the database too.
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 525_600  # one year

# After this many consecutive failures the source is still checked, but it is loudly
# flagged. NOT auto-disabled: a regulator's site being down for a week is exactly when
# a compliance team most needs to know it is not being watched, and silently switching
# the watch off would be the worst possible response.
FAILURES_BEFORE_ALARM = 3


async def register_source(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    url: str,
    jurisdiction: str,
    connector: str = watch.CONNECTOR_HTTP,
    topic: str | None = None,
    authority: str | None = None,
    check_interval_minutes: int = 1440,
    credential_ref: str | None = None,
    config: dict | None = None,
    created_by_user_id: uuid.UUID | None = None,
) -> RegWatchSource:
    """Approve a source for monitoring.

    Validation is strict because everything downstream trusts this row: the collector
    will fetch whatever URL is here, on a schedule, without asking again.
    """
    name = (name or "").strip()
    url = (url or "").strip()
    jurisdiction = (jurisdiction or "").strip()

    if not name:
        raise InvalidSourceError("a source needs a name")
    if not jurisdiction:
        raise InvalidSourceError(
            "a source needs a jurisdiction; relevance filtering has nothing to work "
            "with without one"
        )
    if connector not in watch.CONNECTORS:
        raise InvalidSourceError(
            f"{connector!r} is not a source connector; expected one of {sorted(watch.CONNECTORS)}"
        )

    # A manual-upload source has no URL to fetch, so the scheme check below would
    # reject a perfectly valid registration.
    if connector != watch.CONNECTOR_MANUAL:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise InvalidSourceError(
                f"a {connector} source needs an http(s) URL; got {url!r}"
            )
        if not parsed.netloc:
            raise InvalidSourceError(f"{url!r} has no host")

        # The same check the collector applies at fetch time, applied again HERE, at
        # the moment somebody adds the source. The collector's check is what actually
        # stops the request, and it stays; this one stops a link-local or private
        # address from entering the approved registry at all. Without it the list of
        # "approved sources" can contain an entry that will only ever fail, and a
        # reviewer reading that list has no way to tell why. Found by a live probe:
        # http://169.254.169.254/latest/meta-data/ registered cleanly.
        try:
            await assert_safe_url(url)
        except Exception as exc:
            raise InvalidSourceError(
                f"{url!r} does not resolve to a public address and cannot be "
                f"registered as a regulatory source: {exc}"
            ) from exc

    if not MIN_INTERVAL_MINUTES <= check_interval_minutes <= MAX_INTERVAL_MINUTES:
        raise InvalidSourceError(
            f"check_interval_minutes must be between {MIN_INTERVAL_MINUTES} and "
            f"{MAX_INTERVAL_MINUTES}; {check_interval_minutes} would either hammer the "
            "source or never check it"
        )

    # The secret itself must never arrive here. Same rule as Agents 2 and 3: the
    # database stores the NAME of an environment variable and nothing else.
    config = dict(config or {})
    leaked = [k for k in config if any(
        marker in k.lower() for marker in
        ("password", "secret", "token", "api_key", "apikey", "credential", "cookie")
    )]
    if leaked:
        raise InvalidSourceError(
            f"config carries credential-shaped keys {leaked}; put the secret in the "
            "environment and name it with credential_ref instead"
        )

    existing = await repo.find_source_by_name(db, org_id, name)
    if existing is not None:
        raise InvalidSourceError(
            f"a source named {name!r} is already registered for this organisation"
        )

    row = RegWatchSource(
        org_id=org_id, name=name, url=url, connector=connector,
        jurisdiction=jurisdiction, topic=(topic or "").strip() or None,
        authority=(authority or "").strip() or None,
        check_interval_minutes=check_interval_minutes,
        credential_ref=credential_ref, config=config,
        enabled=True, created_by_user_id=created_by_user_id,
    )
    await repo.create_source(db, row)

    await audit_service.record(
        db, org_id=org_id, actor_user_id=created_by_user_id,
        action=watch.AUDIT_SOURCE_REGISTERED,
        entity_type=watch.AUDIT_SOURCE_ENTITY, entity_id=row.id,
        after={
            "name": name, "url": url, "connector": connector,
            "jurisdiction": jurisdiction, "topic": topic,
            "check_interval_minutes": check_interval_minutes,
            # The REF, never a value.
            "credential_ref": credential_ref,
        },
    )
    return row


async def get_source_or_raise(
    db: AsyncSession, source_id: uuid.UUID, org_id: uuid.UUID
) -> RegWatchSource:
    """Fetch a source in this org, or 404.

    A source in ANOTHER org raises exactly the same error as one that does not exist.
    Distinguishing them would confirm the id.
    """
    source = await repo.get_source(db, source_id, org_id)
    if source is None:
        raise SourceNotFoundError(f"source {source_id} not found")
    return source


async def set_enabled(
    db: AsyncSession,
    source: RegWatchSource,
    *,
    enabled: bool,
    actor_user_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> RegWatchSource:
    """Turn monitoring of a source on or off.

    Disabling is recorded with a reason, because "we stopped watching this regulator"
    is a decision a compliance team may have to account for later.
    """
    if not enabled and not (reason and reason.strip()):
        raise InvalidSourceError(
            "disabling a source requires a reason; a regulator that stopped being "
            "watched, with no record of why, is a gap nobody can explain afterwards"
        )

    before = source.enabled
    source.enabled = enabled
    source.updated_at = datetime.now(UTC)
    await db.flush()

    await audit_service.record(
        db, org_id=source.org_id, actor_user_id=actor_user_id,
        action=watch.AUDIT_SOURCE_UPDATED if enabled else watch.AUDIT_SOURCE_DISABLED,
        entity_type=watch.AUDIT_SOURCE_ENTITY, entity_id=source.id,
        before={"enabled": before},
        after={"enabled": enabled, "reason": reason},
    )
    return source


async def record_check_outcome(
    db: AsyncSession,
    source: RegWatchSource,
    *,
    succeeded: bool,
    now: datetime | None = None,
) -> RegWatchSource:
    """Stamp the source after a collection attempt.

    `last_checked_at` moves on EVERY attempt; `last_success_at` only on success. Two
    fields rather than one because a source checked a minute ago that failed is not
    the same as one that succeeded a minute ago, and a single "last checked" column
    would present them identically -- which is the spec's closing guardrail lost at
    the last step.
    """
    moment = now or datetime.now(UTC)
    source.last_checked_at = moment
    if succeeded:
        source.last_success_at = moment
        source.consecutive_failures = 0
    else:
        source.consecutive_failures = (source.consecutive_failures or 0) + 1
        if source.consecutive_failures >= FAILURES_BEFORE_ALARM:
            # Logged, not silenced, and deliberately NOT auto-disabled: a regulator
            # unreachable for days is precisely when someone needs to be told the
            # watch is not working.
            logger.warning(
                "Regulatory source %r has failed %d consecutive collections; it is "
                "still enabled and still being attempted, but it is NOT being watched "
                "successfully",
                source.name, source.consecutive_failures,
            )
    await db.flush()
    return source


def health(source: RegWatchSource, *, now: datetime | None = None) -> dict:
    """What the UI shows about a source, with the honesty built in.

    `is_current` is the field that matters and it is deliberately narrow: it is true
    only when the last attempt SUCCEEDED and was within the check interval. A source
    that has never been collected, or whose last attempt failed, is not current, and
    the UI has no way to render it as though it were.
    """
    moment = now or datetime.now(UTC)
    never_checked = source.last_checked_at is None
    last_attempt_failed = bool(
        source.last_checked_at
        and (source.last_success_at is None or source.last_success_at < source.last_checked_at)
    )
    stale = bool(
        source.last_success_at
        and (moment - source.last_success_at).total_seconds()
        > source.check_interval_minutes * 60 * 2
    )

    if never_checked:
        state, note = "never_collected", "This source has never been successfully collected."
    elif last_attempt_failed:
        state, note = "failing", (
            f"The last {source.consecutive_failures} attempt(s) failed. Its current "
            "content is unknown; this is not a report that it has not changed."
        )
    elif stale:
        state, note = "stale", (
            "The last success is older than twice the check interval."
        )
    else:
        state, note = "current", "Collected successfully within the expected interval."

    return {
        "state": state,
        "is_current": state == "current",
        "note": note,
        "enabled": source.enabled,
        "last_checked_at": source.last_checked_at.isoformat() if source.last_checked_at else None,
        "last_success_at": source.last_success_at.isoformat() if source.last_success_at else None,
        "consecutive_failures": source.consecutive_failures or 0,
        "alarming": (source.consecutive_failures or 0) >= FAILURES_BEFORE_ALARM,
    }
