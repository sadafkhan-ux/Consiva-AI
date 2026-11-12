"""Recognising a "not found" page that was served with a success status (a soft 404).

moonsez.com/privacy and /terms both answered HTTP 200 with the title "Page Not Found.". The scanner took them for a found privacy policy and terms of use, read 1,640 characters of the
site's menu and footer as the notice, and gave full marks for a notice that was never there. A page is told to be missing by what it says about itself: its title, or, for a short page,
its text. Used by the crawler (the pages it reads) and by the policy reader (the pages it fetches).
"""
from __future__ import annotations

import re

_TITLE = re.compile(
    r"^\s*(?:error\s*[:\-]?\s*404\b|404(?:\s*[-:|.]?\s*(?:error|not\s+found|page\s+not\s+found))?\s*[.!]?\s*$|error\s*[:\-]?\s*(?:page\s+)?not\s+found|page\s+(?:was\s+)?not\s+found|not\s+found\b|file\s+not\s+found|"
    r"(?:the\s+)?page\s+(?:you\s+(?:are\s+|were\s+)?looking\s+for\s+)?(?:can(?:no|')t|cannot|could\s*n(?:o|')t|was\s+not|is\s+not)\s+(?:be\s+)?found|oops[,!.\s]+(?:page|that|we))",
    re.IGNORECASE,
)
_BODY = re.compile(
    r"(?:page|url|link|address)\s+(?:you\s+(?:are\s+|were\s+)?(?:looking\s+for|requested|tried)\s*)?(?:could\s*n(?:o|')t|cannot|can(?:no|')t|was\s+not|is\s+not|does\s+not|doesn(?:o|')t|not)\s+(?:be\s+)?(?:found|exist)|"
    r"looking\s+(?:for\s+)?not\s+be\s+found|^\s*(?:error\s*)?404\s*[-:|.]?\s*(?:error|not\s+found|page)\b|^\s*404\s*$",
    re.IGNORECASE,
)

#: Only a short page is judged by its text: a long page that mentions a missing page in passing (a privacy notice, say) is a real page.
BODY_MAX_CHARS = 1500


def looks_not_found(title: str | None, text: str | None) -> bool:
    """True when a page that loaded successfully says, in its title or (if it is short) in its text, that the page was not found. Never raises."""
    title = title if isinstance(title, str) else ""
    text = (text if isinstance(text, str) else "").strip()
    if _TITLE.search(title):
        return True
    return 0 < len(text) <= BODY_MAX_CHARS and bool(_BODY.search(text))
