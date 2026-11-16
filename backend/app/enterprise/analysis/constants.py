"""Tunables of the text analysis, as module-level constants (the Enterprise settings module is not edited by this package)."""

from __future__ import annotations

#: The most characters of one source sent to the model in one call. A longer source is split into several chunks; none is dropped.
CHUNK_MAX_CHARS = 12_000
#: Characters of the previous chunk repeated at the start of the next, so a sentence cut by a boundary is whole in one of the two.
CHUNK_OVERLAP_CHARS = 400
#: Chunks analysed at the same time.
CONCURRENCY = 4
#: Attempts per model call by this package's wrapper (the existing client has its own bounded retries beneath this).
LLM_ATTEMPTS = 2
#: Attempts the existing client makes inside one wrapper attempt.
LLM_CLIENT_ATTEMPTS = 2
LLM_TIMEOUT_S = 150.0
LLM_BACKOFF_S = (2.0, 6.0)
#: A fact whose quote is shorter than this (after whitespace is normalised) is not accepted as evidence.
MIN_QUOTE_CHARS = 6
MAX_QUOTE_CHARS = 1200
#: Forms sent to the model for the necessity reading (C9) in one call.
FORMS_PER_CALL = 12
#: Passages sampled for the plain-language reading (N4), and the characters of each.
N4_SAMPLES = 8
N4_SAMPLE_CHARS = 700
#: A policy older than this many days before the assessment date is reported as possibly outdated.
OUTDATED_AFTER_DAYS = 730
#: At most this many evidence references are attached to one assessment.
MAX_REFS = 25
