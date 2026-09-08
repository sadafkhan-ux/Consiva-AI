"""Unit tests for crawler._host_resolver_pin_args -- the DNS-rebinding TOCTOU fix.

assert_safe_url resolves a hostname and validates the IP; Chromium's own separate
resolution moments later could be poisoned by an attacker who controls the scanned
domain's DNS (a short TTL serving a safe IP for the check, then an internal address
for the real connection). Pinning the hostname to the already-validated IP via a
Chromium --host-resolver-rules launch flag closes that window for the root/seed
hostname (the only one this crawler treats as fully trusted going into the browser
launch; see the function's own docstring for the documented residual gap on
same-registered-domain subdomains discovered mid-crawl).
"""

from app.scanner.crawler import _host_resolver_pin_args


def test_pins_ipv4_address():
    args = _host_resolver_pin_args("example.com", ["93.184.216.34"])
    assert args == ["--host-resolver-rules=MAP example.com 93.184.216.34"]


def test_prefers_ipv4_when_both_families_present():
    args = _host_resolver_pin_args("example.com", ["2606:2800:220:1:248:1893:25c8:1946", "93.184.216.34"])
    assert args == ["--host-resolver-rules=MAP example.com 93.184.216.34"]


def test_falls_back_to_ipv6_with_brackets_when_no_ipv4():
    args = _host_resolver_pin_args("example.com", ["2606:2800:220:1:248:1893:25c8:1946"])
    assert args == ["--host-resolver-rules=MAP example.com [2606:2800:220:1:248:1893:25c8:1946]"]


def test_returns_empty_list_for_no_resolved_ips():
    """Defensive: assert_safe_url always raises before returning an empty list in
    practice (an unresolvable hostname is itself rejected), but this must never
    produce a malformed launch arg if that assumption is ever violated."""
    assert _host_resolver_pin_args("example.com", []) == []
