"""Whether a provision is in force: a pure function of its commencement date and the date assessed on. Nothing here decides who the provision applies to.

The Act and Rules do not take effect on one date (G.S.R. 843(E)): some provisions are in force, some commence on 13 Nov 2026, most on 13 May 2027. So the assessment date is an input and the answer
is per provision. `in_force` and `not_yet_effective` are the only two answers a date can give. Whether a provision APPLIES to this organisation (a Significant Data Fiduciary, a processor of children's
data, an exempt class) is a fact about the organisation that a website does not show, so it is never guessed here: it stays `requires_declaration` until the organisation declares it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

IN_FORCE, NOT_YET_EFFECTIVE = "in_force", "not_yet_effective"


@dataclass(frozen=True)
class Effectiveness:
    status: str
    commences: date
    days_until: int            # 0 when in force

    @property
    def in_force(self) -> bool:
        return self.status == IN_FORCE


def effectiveness(commences: date, as_of: date) -> Effectiveness:
    if as_of >= commences:
        return Effectiveness(IN_FORCE, commences, 0)
    return Effectiveness(NOT_YET_EFFECTIVE, commences, (commences - as_of).days)


def split_by_effectiveness(items, as_of: date, *, key=lambda p: p.commences):
    """(in force, not yet effective) for anything with a commencement date. The two lists together are always everything: nothing is dropped."""
    now, later = [], []
    for item in items:
        (now if effectiveness(key(item), as_of).in_force else later).append(item)
    return now, later
