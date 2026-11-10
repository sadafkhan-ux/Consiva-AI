"""Scorecard rows P11 (SSL certificate) and P12 (TLS version): the decision of each row, the 100-mark weighting, and the handshake probe itself (offline, against a local TLS server)."""

import datetime as dt
import ssl
import threading

import pytest

from app.scanner import tls_assessment as tls_mod
from app.scoring import scorecard as sc
from tests.test_scorecard import FORM, SIG_FORM, R, banner_ev, go

HOST = "x.test"


def sig(cert_status="valid", days=200, version="1.3", **kw):
    cert = {"status": cert_status, "reason": kw.get("reason"), "not_after": "2027-05-24", "issuer": "R11", "days_remaining": days}
    return {**SIG_FORM, "tls_signals": {"host": HOST, "certificate": cert, "tls": {"version": version, "reason": kw.get("tls_reason")}}}


def rows(**kw):
    r = go(ev=banner_ev(forms=[FORM]), signals=sig(**kw))
    return R(r, "P11"), R(r, "P12"), r


def test_weights_still_total_100_and_forms_and_security_keeps_its_16_marks():
    assert sum(r[3] for r in sc.ROWS) == 100 and {r[0]: r[3] for r in sc.ROWS}["P10"] == 8
    _, _, r = rows()
    assert next(c for c in r["categories"] if c["category"] == "Forms & Security")["marks"] == 16


def test_certificate_with_more_than_30_days_earns_full_marks_and_the_evidence_shows_expiry_and_days():
    p11, _, _ = rows(days=91)
    assert p11["outcome"] == "pass" and p11["awarded"] == 4
    text = p11["evidence"][0]["observed"]
    assert "2027-05-24" in text and "91 days remaining" in text and "R11" in text


def test_certificate_with_30_days_or_fewer_earns_half():
    for d in (30, 12, 0):
        p11, _, _ = rows(days=d)
        assert p11["outcome"] == "partial" and p11["awarded"] == 2, d


@pytest.mark.parametrize("status", ["expired", "invalid", "no_https"])
def test_missing_invalid_or_expired_certificate_earns_zero(status):
    p11, _, _ = rows(cert_status=status, days=None, reason="certificate has expired" if status == "expired" else "self-signed")
    assert p11["outcome"] == "fail" and p11["awarded"] == 0 and p11["evidence"]


def test_tls_versions_1_3_full_1_2_half_1_1_and_below_zero():
    got = {v: rows(version=v)[1] for v in ("1.3", "1.2", "1.1", "1.0")}
    assert [(v, x["outcome"], x["awarded"]) for v, x in got.items()] == [("1.3", "pass", 4), ("1.2", "partial", 2), ("1.1", "fail", 0), ("1.0", "fail", 0)]
    assert "TLS 1.3" in got["1.3"]["evidence"][0]["observed"] and "TLS 1.2" in got["1.2"]["evidence"][0]["observed"]


def test_an_expired_certificate_does_not_hide_the_tls_version():
    p11, p12, _ = rows(cert_status="expired", days=-5, version="1.3")
    assert p11["outcome"] == "fail" and p12["outcome"] == "pass"


def test_a_site_served_over_http_fails_both_and_p12_names_p11_as_the_blocker():
    p11, p12, _ = rows(cert_status="no_https", days=None, version=None)
    assert p11["outcome"] == "fail" and p12["outcome"] == "fail" and p12["blocked_by"] == "P11"


@pytest.mark.parametrize("signals", [None, {"tls_signals": {"host": HOST, "certificate": {"status": "unreachable", "reason": "timed out"}, "tls": {"version": None, "reason": "timed out"}}}])
def test_evidence_that_was_not_obtained_is_not_assessed_never_passed(signals):
    r = go(ev=banner_ev(forms=[FORM]), signals={**SIG_FORM, **(signals or {})})
    for rid in ("P11", "P12"):
        assert R(r, rid)["outcome"] == "not_assessed" and R(r, rid)["awarded"] is None
    assert r["points"]["not_assessable"] >= 8


def test_the_score_and_denominator_include_the_two_rows_consistently():
    full = go(ev=banner_ev(forms=[FORM]), signals=sig(days=200, version="1.3"))
    low = go(ev=banner_ev(forms=[FORM]), signals=sig(cert_status="expired", days=-1, version="1.1"))
    assert full["points"]["in_play"] == low["points"]["in_play"] and full["points"]["in_play"] - 0 <= 100
    assert full["points"]["awarded"] - low["points"]["awarded"] == 8
    assert low["findings"] and {"P11", "P12"} <= {f["ref"] for f in low["findings"]}


# ── the probe, against a local TLS server (no network) ──
def _server(tmp_path, maximum):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(1)
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False).sign(key, hashes.SHA256()))
    cf, kf = tmp_path / "c.pem", tmp_path / "k.pem"
    cf.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kf.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cf, kf)
    ctx.maximum_version = maximum
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    stop = threading.Event()

    def serve():
        srv.settimeout(0.5)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
            except OSError:
                continue
            try:
                with ctx.wrap_socket(c, server_side=True):
                    pass
            except (ssl.SSLError, OSError):
                pass
    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1], lambda: (stop.set(), srv.close())


def test_the_probe_reads_the_tls_version_and_flags_an_untrusted_certificate(tmp_path):
    port, close = _server(tmp_path, ssl.TLSVersion.TLSv1_2)
    try:
        r = tls_mod.probe("localhost", port, timeout=4)
    finally:
        close()
    assert r["certificate"]["status"] == "invalid" and r["tls"]["version"] == "1.2"        # a self-signed certificate is not trusted; the version is still read
    assert 28 <= r["certificate"]["days_remaining"] <= 30 and r["certificate"]["self_signed"] is True


def test_the_probe_marks_a_closed_port_unreachable_not_failed():
    r = tls_mod.probe("127.0.0.1", 1, timeout=1)
    assert r["certificate"]["status"] == "unreachable" and r["tls"]["version"] is None


async def test_a_site_reached_over_http_has_no_certificate():
    r = await tls_mod.assess_site_tls("http://x.test/")
    assert r["certificate"]["status"] == "no_https"


def test_a_certificate_that_renews_every_90_days_is_not_marked_down_while_it_has_more_than_30_days():
    for d in (31, 37, 83, 90):
        p11, _, _ = rows(days=d)
        assert p11["outcome"] == "pass" and p11["awarded"] == 4, d


def test_the_issuer_is_named_by_its_organisation_when_the_intermediate_name_alone_would_mean_nothing():
    r = go(ev=banner_ev(forms=[FORM]), signals=sig(days=91))
    sigs = sig(days=91)
    sigs["tls_signals"]["certificate"].update({"issuer": "YE2", "issuer_org": "Let's Encrypt"})
    p11 = R(go(ev=banner_ev(forms=[FORM]), signals=sigs), "P11")
    assert "issuer Let's Encrypt (YE2)" in p11["evidence"][0]["observed"]
