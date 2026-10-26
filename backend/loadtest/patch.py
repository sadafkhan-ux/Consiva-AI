"""Point name resolution at the fixture server, for the load-test processes only.

The scanner refuses private addresses (url_safety), and the fixture server is on the container network, so for a load test the two places that resolve a name are replaced:
`assert_safe_host` (the SSRF guard's resolver) answers with the fixture's address for EVERY name, and the browser is started with a rule that maps every name there as well.
The result is that the real API, queue, worker, browser, classifier and database run unchanged, while no request leaves the machine (not even to the analytics and advertising
hosts the fixture pages mention). It is applied by the launchers in this package and by nothing else; the production processes never import it.
"""

import os
import socket

from app.scanner import crawler, url_safety

FIXTURE_HOST = os.environ.get("LOADTEST_FIXTURE_HOST", "fixture")
_ip: list[str] = []


def fixture_ip() -> str:
    if not _ip:
        _ip.append(socket.gethostbyname(FIXTURE_HOST))
    return _ip[0]


async def _resolve_to_fixture(hostname: str) -> list[str]:
    return [fixture_ip()]


def _pin_everything_to_fixture(hostname: str, safe_ips: list[str]) -> list[str]:
    return [f"--host-resolver-rules=MAP * {fixture_ip()}"]


def apply() -> None:
    url_safety.assert_safe_host = _resolve_to_fixture
    crawler._host_resolver_pin_args = _pin_everything_to_fixture
