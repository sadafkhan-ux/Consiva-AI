"""robots.txt as RFC 9309 defines it: which URLs of a site the scanner may visit.

The earlier reader understood only `Disallow:` prefixes of the `*` group. That mis-reads real files in both directions: an `Allow:` carved out of a disallowed
directory was ignored (pages the site permits were skipped), a wildcard rule (`/*?sid=`, `/*.pdf$`) was treated as a literal prefix (so it blocked nothing, and the
scanner visited what the site asked it not to), and a group written for this scanner by name was ignored in favour of `*`.

Rules applied here:
  * a group is one or more consecutive `User-agent:` lines and the rules that follow; the group whose agent token matches the scanner's product name wins, otherwise `*`;
  * within the group the longest matching pattern decides, and `Allow` wins a tie; no matching rule means allowed;
  * `*` matches any run of characters and a trailing `$` anchors the end; the path is compared with its query string;
  * an empty `Disallow:` blocks nothing.
Nothing here decides what to do when robots.txt cannot be fetched; that is the caller's call (crawler._fetch_robots).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PCT = re.compile(r"%([0-9a-fA-F]{2})")


def _normalise(text: str) -> str:
    """Percent-encodings compared as RFC 9309 asks: hex digits upper-cased, and an encoded unreserved character (%7E, %62) equal to the character itself."""
    def fix(m: re.Match[str]) -> str:
        ch = chr(int(m.group(1), 16))
        return ch if ch in _UNRESERVED else "%" + m.group(1).upper()
    return _PCT.sub(fix, text)


def agent_token(user_agent: str) -> str:
    """The product name a robots.txt group is matched against: 'ConsivaConsentAgent/0.1 (...)' -> 'consivaconsentagent'."""
    return re.split(r"[/\s]", user_agent.strip(), maxsplit=1)[0].lower()


def _compile(pattern: str) -> re.Pattern[str]:
    pattern = _normalise(pattern)
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile(regex + ("$" if anchored else ""))


@dataclass
class RobotsRules:
    """The rules that apply to one agent: (allow?, pattern) pairs. An instance with no rules allows everything."""
    rules: list[tuple[bool, str]] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    _compiled: list[tuple[bool, int, re.Pattern[str]]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self._compiled = [(allow, len(p), _compile(p)) for allow, p in self.rules if p]

    def allows(self, url_or_path: str) -> bool:
        parsed = urlparse(url_or_path)
        target = _normalise((parsed.path or "/") + (f"?{parsed.query}" if parsed.query else ""))
        best_len, best_allow = -1, True
        for allow, length, rx in self._compiled:
            if rx.match(target) and (length > best_len or (length == best_len and allow)):
                best_len, best_allow = length, allow
        return best_allow

    def blocks_everything(self) -> bool:
        return not self.allows("/") and any(p in ("/", "/*") and not a for a, p in self.rules)

    def __bool__(self) -> bool:
        return bool(self._compiled)


def parse_robots(text: str, user_agent: str) -> RobotsRules:
    """Rules for `user_agent` (and the sitemaps listed anywhere in the file)."""
    token = agent_token(user_agent)
    groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
    sitemaps: list[str] = []
    agents: list[str] | None = None
    rules: list[tuple[bool, str]] = []
    collecting_agents = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if not collecting_agents:                     # a rule came since the last agent line: this starts a new group
                agents, rules = [], []
                groups.append((agents, rules))
                collecting_agents = True
            assert agents is not None
            agents.append(value.lower())
        elif key in ("allow", "disallow"):
            collecting_agents = False
            if agents is not None:
                rules.append((key == "allow", value))
        elif key == "sitemap":
            if value and value not in sitemaps:
                sitemaps.append(value)

    chosen: list[tuple[bool, str]] = []
    best = -1
    for names, group_rules in groups:
        for name in names:
            if name == "*":
                score = 0
            elif name == token:
                score = len(name)
            else:
                continue
            if score > best:
                best, chosen = score, group_rules
            elif score == best:                           # two groups for the same agent are one group
                chosen = chosen + group_rules
    return RobotsRules(rules=[(a, p) for a, p in chosen if p], sitemaps=sitemaps)
