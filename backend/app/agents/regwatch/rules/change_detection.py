"""Deciding whether a source actually changed, and how much (spec §4 step 4, §10).

Deterministic and pure. No model, no database. What a diff MEANS is a later question
answered with evidence and confidence; what a diff IS is arithmetic, and keeping the
two apart is what stops an interpretation from quietly becoming a fact.

THE THREE OUTCOMES THAT ARE NOT "CHANGED"
-----------------------------------------
  * `no_change`     -- hashes match. The only outcome that should stay silent.
  * `first_capture` -- there is no baseline yet. Not a change, but a reviewer should
                       see it once, because accepting it is what creates the baseline
                       every later comparison depends on.
  * `unreachable`   -- collection failed. Raised as a finding, deliberately. A source
                       nobody could read is a thing a compliance team needs to know
                       about; silence about it is precisely what §15 forbids.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from app.agents.regwatch.schemas import watch

# How much of the diff to keep on the change row. Enough for a reviewer to see what
# moved without storing a second copy of the document -- the full text is already on
# the collection, which is append-only, so the excerpt is a convenience and never the
# only record.
MAX_EXCERPT_CHARS = 4000

# A change smaller than this is still a change and still reported. The number is only
# used to describe it as minor in the summary, never to suppress it: deciding that a
# two-word amendment does not matter is exactly the judgement this layer must not make.
MINOR_LINE_THRESHOLD = 3


@dataclass(frozen=True)
class ChangeResult:
    kind: str
    added_lines: int = 0
    removed_lines: int = 0
    excerpt: str | None = None
    # Plain-language description of the shape of the diff. Not an interpretation of
    # what it means -- that needs the regulatory corpus and a human.
    shape: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def raises_finding(self) -> bool:
        return self.kind in watch.CHANGE_KINDS_RAISING_A_FINDING

    @property
    def is_minor(self) -> bool:
        return (self.added_lines + self.removed_lines) <= MINOR_LINE_THRESHOLD


def detect(
    *,
    baseline_text: str | None,
    baseline_hash: str | None,
    new_text: str | None,
    new_hash: str | None,
    collection_failed: bool = False,
    failure_reason: str | None = None,
) -> ChangeResult:
    """Compare an accepted baseline against a fresh collection.

    `collection_failed` is checked FIRST and unconditionally. A failed collection has
    no content, so every comparison below it would be comparing against nothing -- and
    "compared against nothing, found nothing" is indistinguishable from "unchanged"
    unless this returns before reaching it.
    """
    if collection_failed:
        return ChangeResult(
            kind=watch.CHANGE_UNREACHABLE,
            shape="the source could not be collected, so nothing was compared",
            notes=(
                failure_reason or "collection failed without a recorded reason",
                ("This is NOT a report that the source is unchanged. Its current "
                 "content is unknown."),
            ),
        )

    if new_hash is None or new_text is None:
        # Belt and braces: a caller that forgot to set collection_failed still cannot
        # get a "no change" out of this.
        return ChangeResult(
            kind=watch.CHANGE_UNREACHABLE,
            shape="no collected content was supplied for comparison",
            notes=("the caller passed no content and did not flag a failure",),
        )

    if baseline_hash is None or baseline_text is None:
        lines = len(new_text.splitlines())
        return ChangeResult(
            kind=watch.CHANGE_FIRST_CAPTURE,
            added_lines=lines,
            excerpt=new_text[:MAX_EXCERPT_CHARS],
            shape=f"first capture of this source, {lines} lines",
            notes=(
                ("No baseline existed. Accepting this finding is what creates one; "
                 "until then every re-check will report this again."),
            ),
        )

    if baseline_hash == new_hash:
        return ChangeResult(kind=watch.CHANGE_NONE, shape="identical to the accepted baseline")

    added, removed, excerpt = _diff(baseline_text, new_text)
    total = added + removed
    shape = f"{added} line(s) added, {removed} removed"
    notes: list[str] = []
    if total <= MINOR_LINE_THRESHOLD:
        notes.append(
            "A small diff. Reported in full regardless -- size is not a proxy for "
            "significance, and a one-line amendment can be the whole change."
        )
    if added and not removed:
        notes.append("Content was added; nothing was taken away.")
    elif removed and not added:
        notes.append("Content was removed; nothing was added.")

    return ChangeResult(
        kind=watch.CHANGE_CONTENT,
        added_lines=added,
        removed_lines=removed,
        excerpt=excerpt,
        shape=shape,
        notes=tuple(notes),
    )


def summarise_stored(
    *, change_kind: str, added_lines: int, removed_lines: int, source_name: str
) -> str:
    """`summarise`, rebuilt from a STORED change row rather than a ChangeResult.

    Same reason as `notes_for_change`: assessment can run more than once, and the
    deterministic half of a finding's summary has to be reproducible on each run so it
    can be REPLACED rather than appended to. `shape` is not a stored column, so it is
    reconstructed here from the counts that are.
    """
    if change_kind == watch.CHANGE_NONE:
        shape = ""
    elif change_kind == watch.CHANGE_UNREACHABLE:
        shape = "the source could not be collected, so nothing was compared"
    elif change_kind == watch.CHANGE_FIRST_CAPTURE:
        shape = f"first capture of this source, {added_lines} lines"
    else:
        shape = f"{added_lines} line(s) added, {removed_lines} removed"

    return summarise(
        ChangeResult(
            kind=change_kind, added_lines=added_lines,
            removed_lines=removed_lines, shape=shape,
        ),
        source_name=source_name,
    )


def notes_for_change(
    *, change_kind: str, added_lines: int, removed_lines: int,
    failure_reason: str | None = None,
) -> tuple[str, ...]:
    """The notes `detect` would produce, rebuilt from a STORED change row.

    Exists because assessment can run more than once on the same finding -- a retry, or
    a reviewer sending it back -- and the notes fall into two kinds that must not be
    treated alike:

      * notes about the CHANGE ("no baseline existed", "content was only added").
        Permanently true of that change, however many times it is assessed.
      * notes about an ASSESSMENT RUN ("no passage was close enough to ground an
        interpretation"). True of one run and possibly false of the next.

    Appending the second kind to the first left a finding carrying five citations AND
    a note saying nothing could be cited -- self-contradictory, in the one agent whose
    whole purpose is to be exact about what is and is not known. So assessment rebuilds
    the change notes from here each time and adds only its own run's notes to them.

    Deliberately derived from the stored columns rather than from the original
    ChangeResult: the row is what survives, so the row has to be enough.
    """
    if change_kind == watch.CHANGE_UNREACHABLE:
        return (
            failure_reason or "collection failed without a recorded reason",
            ("This is NOT a report that the source is unchanged. Its current "
             "content is unknown."),
        )
    if change_kind == watch.CHANGE_FIRST_CAPTURE:
        return (
            ("No baseline existed. Accepting this finding is what creates one; "
             "until then every re-check will report this again."),
        )
    if change_kind != watch.CHANGE_CONTENT:
        return ()

    notes: list[str] = []
    if added_lines + removed_lines <= MINOR_LINE_THRESHOLD:
        notes.append(
            "A small diff. Reported in full regardless -- size is not a proxy for "
            "significance, and a one-line amendment can be the whole change."
        )
    if added_lines and not removed_lines:
        notes.append("Content was added; nothing was taken away.")
    elif removed_lines and not added_lines:
        notes.append("Content was removed; nothing was added.")
    return tuple(notes)


def _diff(before: str, after: str) -> tuple[int, int, str]:
    """Unified diff, counting only real content lines.

    The +++/---/@@ headers are excluded from the counts: including them would inflate
    every change by three and make "5 lines added" mean something different depending
    on how many hunks the diff happened to produce.
    """
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    diff = list(difflib.unified_diff(
        before_lines, after_lines, fromfile="baseline", tofile="collected",
        lineterm="", n=2,
    ))

    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    excerpt = "\n".join(diff)[:MAX_EXCERPT_CHARS]
    return added, removed, excerpt


def summarise(result: ChangeResult, *, source_name: str) -> str:
    """One deterministic sentence for the change row.

    Assembled by string formatting, never by a model. The narrative interpretation --
    what the change means for this organisation -- is a separate, cited, reviewable
    thing that lives on the finding.
    """
    if result.kind == watch.CHANGE_NONE:
        return f"{source_name}: no change against the accepted baseline."
    if result.kind == watch.CHANGE_UNREACHABLE:
        return (
            f"{source_name}: COULD NOT BE COLLECTED. {result.shape}. "
            "Its current content is unknown; this is not a report that it is unchanged."
        )
    if result.kind == watch.CHANGE_FIRST_CAPTURE:
        return (
            f"{source_name}: {result.shape}. No baseline existed to compare against."
        )
    return f"{source_name}: content changed, {result.shape}."
