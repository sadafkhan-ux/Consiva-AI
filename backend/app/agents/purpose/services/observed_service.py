"""What the data is OBSERVED to be used for, read from Agent 1's scan evidence.

This module reads and reshapes. It classifies nothing: `trackers.category` and
`cookies.category` were already decided by the Consent Agent's vendor-signature rules,
and re-deciding them here would create a second opinion that silently disagrees with
the one the customer already saw on their scan report.

WHAT MAKES THIS EVIDENCE VALUABLE

Not the category on its own -- it is the category together with `consent_states`.
A tracker labelled `marketing` is unremarkable. A tracker labelled `marketing` that
fired with `pre_consent` in its consent states is processing that happened before any
lawful basis existed, and that is a finding the platform could not previously produce,
because the two halves live in the same table and nothing ever compared them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ConsentSignal, Cookie, ThirdPartyService, Tracker

# Consent states in which processing is NOT covered by a granted consent.
#
# `post_reject` counts because the visitor actively declined; `pre_consent` counts
# because nothing had been granted yet. `post_accept` is the only state in which a
# non-essential purpose has a basis, and even then only if the click actually worked --
# which is why the caller checks the consent signal separately.
_UNCONSENTED_STATES = ("pre_consent", "post_reject")

# Purposes that require consent under the DPDP framing this platform applies.
# `functional` is excluded: strictly necessary processing is the standard carve-out,
# and flagging every session cookie would bury the real findings.
_CONSENT_REQUIRING = ("analytics", "marketing")


@dataclass
class ObservedPurpose:
    """One observed use of data, with everything needed to justify it."""

    subject_type: str
    subject_ref: str
    subject_label: str
    purpose: str | None
    consent_states: list[str] = field(default_factory=list)
    vendor: str | None = None
    expiry_days: int | None = None
    evidence_refs: list[str] = field(default_factory=list)
    # Other names this subject may be declared under. A declaration written against
    # `customers` should still be found for a table read as `demo_customer.customers`
    # -- the schema qualifier is how the reader addressed it, not part of its identity.
    # Only populated when the shorter name is unambiguous in the source; see
    # structured_service.observe.
    lookup_aliases: list[str] = field(default_factory=list)
    # How many underlying rows this entry represents. One host can appear as dozens of
    # tracker rows differing only by a build hash; they are one thing to act on.
    occurrences: int = 1

    @property
    def fired_without_consent(self) -> bool:
        """Observed in a state where no consent had been granted.

        Note this is about the STATES, not about whether the purpose needed consent --
        `requires_consent` is the separate question, and both must hold before this
        becomes a finding.
        """
        return any(state in _UNCONSENTED_STATES for state in self.consent_states)

    @property
    def requires_consent(self) -> bool:
        return self.purpose in _CONSENT_REQUIRING


async def collect_for_scan(db: AsyncSession, scan_id: uuid.UUID) -> list[ObservedPurpose]:
    """Every observed purpose in one consent scan.

    Scoped by scan rather than by organisation because a scan is the unit a customer
    recognises -- "the purposes we saw on your site last Tuesday" -- and because an
    org-wide read would mix results from sites scanned months apart.
    """
    observed: list[ObservedPurpose] = []

    trackers = (await db.execute(select(Tracker).where(Tracker.scan_id == scan_id))).scalars().all()
    for tracker in trackers:
        host = _host_of(tracker.script_src)
        observed.append(ObservedPurpose(
            subject_type="tracker",
            subject_ref=str(tracker.id),
            subject_label=host,
            purpose=tracker.category,
            consent_states=list(tracker.consent_states or []),
            vendor=tracker.vendor,
            evidence_refs=[str(tracker.id)],
        ))

    cookies = (await db.execute(select(Cookie).where(Cookie.scan_id == scan_id))).scalars().all()
    for cookie in cookies:
        observed.append(ObservedPurpose(
            subject_type="cookie",
            subject_ref=str(cookie.id),
            subject_label=f"{cookie.name} ({cookie.domain})",
            purpose=cookie.category,
            consent_states=list(cookie.consent_states or []),
            vendor=cookie.vendor,
            expiry_days=_days_until(cookie.expiry),
            evidence_refs=[str(cookie.id)],
        ))

    services = (
        await db.execute(select(ThirdPartyService).where(ThirdPartyService.scan_id == scan_id))
    ).scalars().all()
    for service in services:
        observed.append(ObservedPurpose(
            subject_type="third_party_service",
            subject_ref=str(service.id),
            subject_label=service.service_name or "(unnamed service)",
            purpose=service.category,
            # A service record is a roll-up across trackers and carries no consent
            # state of its own. Left empty rather than inherited from a member tracker,
            # which would attribute one script's timing to a whole vendor.
            consent_states=[],
            evidence_refs=[str(service.id)],
        ))

    return _group(observed)


def _group(observed: list[ObservedPurpose]) -> list[ObservedPurpose]:
    """Collapse rows that describe the same thing into one entry.

    A single host routinely appears as dozens of tracker rows differing only by a
    webpack build hash. Assessed individually they produce dozens of identical
    findings -- measured on a real scan: 48 findings covering 13 distinct subjects,
    with fonts.gstatic.com alone accounting for 18. A reviewer facing that stops
    reading, which costs more than the duplicates are worth.

    Grouped on (type, label, purpose, consent states). Consent states are IN the key
    deliberately: the same host seen only after Accept and seen before consent are
    different compliance facts, and merging them would erase the finding.
    """
    grouped: dict[tuple, ObservedPurpose] = {}
    for item in observed:
        key = (
            item.subject_type,
            item.subject_label,
            item.purpose,
            tuple(sorted(item.consent_states)),
        )
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = item
            continue
        existing.occurrences += 1
        existing.evidence_refs.extend(item.evidence_refs)
        # Keep the longest-lived expiry: if any copy of a cookie persists for two
        # years, the group persists for two years.
        if item.expiry_days is not None and (
            existing.expiry_days is None or item.expiry_days > existing.expiry_days
        ):
            existing.expiry_days = item.expiry_days
    return list(grouped.values())


async def consent_was_obtained(db: AsyncSession, scan_id: uuid.UUID) -> bool | None:
    """Whether the scan established that consent could actually be given.

    Returns None when there is no consent signal at all -- which is different from
    False. False means a mechanism was found and the control did not work; None means
    the question was never answered, and a finding must not claim otherwise.
    """
    signal = (
        await db.execute(select(ConsentSignal).where(ConsentSignal.scan_id == scan_id))
    ).scalars().first()
    if signal is None:
        return None
    evidence = signal.evidence or {}
    # Only "clicked" establishes the state the pass is named after. Any other value
    # (click_failed, cmp_not_automatable, cmp_not_found, page_unreachable) means the
    # pass never proved consent could be granted.
    return evidence.get("accept_interaction") == "clicked"


def _host_of(script_src: str | None) -> str:
    """The host a script came from -- the part a person recognises. The rest of the
    path is usually a build hash and carries no meaning for a reviewer."""
    from urllib.parse import urlparse

    if not script_src:
        return "(inline/unknown)"
    return urlparse(str(script_src)).netloc or "(inline/unknown)"


def _days_until(expiry) -> int | None:
    """A cookie's remaining lifetime in whole days, or None.

    Returns None rather than 0 on a malformed value: 0 would read as "expires today",
    which is a claim, and an unparseable timestamp supports no claim at all.
    """
    if expiry is None:
        return None
    from datetime import UTC, datetime

    try:
        when = expiry if isinstance(expiry, datetime) else datetime.fromisoformat(str(expiry))
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return (when - datetime.now(UTC)).days
    except (TypeError, ValueError):
        return None
