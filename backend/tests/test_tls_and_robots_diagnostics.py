"""Certificate diagnostics, and robots.txt as the two known failures show it. Certificates are generated here; nothing touches the network."""

from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.scanner.crawler import _is_robots_disallowed
from app.scanner.robots import parse_robots
from app.scanner.tls_diagnostics import (
    describe_certificate,
    inspect_certificate,
    parse_certificate,
    sentence,
)
from app.scanner.url_normalize import normalize_url

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def der(*, cn="*.example.com", names=("*.example.com", "example.com"), issuer_cn="Test CA", not_before, not_after, self_signed=False):
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    issuer = subject if self_signed else x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_cn)])
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(KEY.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(not_before).not_valid_after(not_after))
    if names:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
    return builder.sign(KEY, hashes.SHA256()).public_bytes(serialization.Encoding.DER)


class TestReadingACertificate:
    def test_an_expired_wildcard_certificate_that_names_the_host(self):
        # the shape of the real *.consiva.ai failure: right names, expired a day ago
        info = parse_certificate(der(not_before=NOW - timedelta(days=90), not_after=NOW - timedelta(hours=20)), "uat.example.com", NOW)
        assert info["expired"] is True and info["hostname_matches"] is True and info["days_since_expiry"] == 0 and info["issuer"] == "Test CA"
        assert "expired on 2026-10-26 (less than a day ago)" in sentence("uat.example.com", info)

    def test_days_since_expiry_is_counted(self):
        info = parse_certificate(der(not_before=NOW - timedelta(days=400), not_after=NOW - timedelta(days=27)), "example.com", NOW)
        assert info["days_since_expiry"] == 27 and "27 days ago" in sentence("example.com", info)

    def test_a_valid_certificate_for_another_host(self):
        info = parse_certificate(der(cn="other.org", names=("other.org",), not_before=NOW - timedelta(days=1), not_after=NOW + timedelta(days=60)), "example.com", NOW)
        assert info["expired"] is False and info["hostname_matches"] is False and info["days_until_expiry"] == 60
        assert "does not name example.com (it names other.org)" in sentence("example.com", info)

    def test_a_wildcard_covers_one_label_only(self):
        info = parse_certificate(der(not_before=NOW - timedelta(days=1), not_after=NOW + timedelta(days=60)), "a.b.example.com", NOW)
        assert info["hostname_matches"] is False
        assert parse_certificate(der(not_before=NOW - timedelta(days=1), not_after=NOW + timedelta(days=60)), "www.example.com", NOW)["hostname_matches"] is True

    def test_a_self_signed_certificate_is_said_to_be(self):
        info = parse_certificate(der(self_signed=True, not_before=NOW - timedelta(days=1), not_after=NOW + timedelta(days=60)), "example.com", NOW)
        assert info["self_signed"] is True and "is self-signed" in sentence("example.com", info)

    def test_a_certificate_that_is_not_yet_valid(self):
        info = parse_certificate(der(not_before=NOW + timedelta(days=2), not_after=NOW + timedelta(days=90)), "example.com", NOW)
        assert info["not_yet_valid"] is True and "is not valid until" in sentence("example.com", info)

    def test_nothing_readable_gives_no_sentence_and_never_an_exception(self):
        assert sentence("x.test", None) == "" and sentence("x.test", {"error": "refused"}) == ""
        assert "error" in inspect_certificate("127.0.0.1", port=1, timeout=0.5)


async def test_a_diagnostic_that_cannot_run_is_silent():
    assert await describe_certificate(None) is None
    result = await describe_certificate("127.0.0.1", port=1)
    assert result is None or "error" in result


class TestTheKekaRobotsFile:
    """https://swaransoft.keka.com/robots.txt, as served on 27 Oct 2026. Verified by a plain GET; no page of the site was requested."""

    KEKA = "User-agent: *\nDisallow: /\nAllow: /careers\nAllow: /careers/\n"

    @pytest.mark.parametrize("url,allowed", [
        ("https://swaransoft.keka.com/", False),
        ("https://swaransoft.keka.com/login", False),
        ("https://swaransoft.keka.com/#/home/dashboard", False),
        ("https://swaransoft.keka.com/careers", True),
        ("https://swaransoft.keka.com/careers/", True),
        ("https://swaransoft.keka.com/careers/jobdetails?jobid=1", True),
    ])
    def test_everything_is_disallowed_except_the_careers_pages(self, url, allowed):
        rules = parse_robots(self.KEKA, "ConsivaConsentAgent/0.1")
        assert rules.allows(url) is allowed
        assert (not _is_robots_disallowed(url, rules)) is allowed

    def test_the_whole_site_is_not_blocked_so_the_entered_root_is_what_is_refused(self):
        rules = parse_robots(self.KEKA, "ConsivaConsentAgent/0.1")
        assert not rules.allows("/") and rules.allows("/careers")


class TestFragmentsAreNotServerPaths:
    """A browser fragment (#/inbox/...) is never sent to the server, so robots.txt is judged on the path alone; the browser still receives the full address, so the client-side route is loaded."""

    def test_robots_is_judged_on_the_server_path_not_the_fragment(self):
        rules = parse_robots("User-agent: *\nDisallow: /inbox\n", "ConsivaConsentAgent/0.1")
        assert rules.allows("https://app.example.com/#/inbox/messages"), "the server is asked for '/', not '/inbox'"
        assert not rules.allows("https://app.example.com/inbox#top")

    def test_a_fragment_is_dropped_from_the_crawl_identity_so_one_page_is_not_scanned_twice(self):
        assert normalize_url("https://app.example.com/#/a") == normalize_url("https://app.example.com/#/b") == normalize_url("https://app.example.com/")
