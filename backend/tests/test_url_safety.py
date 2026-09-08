import pytest

from app.core.exceptions import ScanAuthorizationError
from app.scanner.url_safety import assert_safe_url


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://localhost/",
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata endpoint
    "http://10.0.0.5/",
    "http://192.168.1.1/",
    "http://[::1]/",
])
async def test_blocks_private_and_internal_addresses(url):
    with pytest.raises(ScanAuthorizationError):
        await assert_safe_url(url)


@pytest.mark.parametrize("url", ["ftp://example.com/", "file:///etc/passwd", "javascript:alert(1)"])
async def test_blocks_non_http_schemes(url):
    with pytest.raises(ScanAuthorizationError):
        await assert_safe_url(url)


async def test_blocks_url_with_no_hostname():
    with pytest.raises(ScanAuthorizationError):
        await assert_safe_url("http:///path-only")


async def test_allows_public_domain():
    await assert_safe_url("https://example.com/")  # IANA-reserved, always resolvable, no side effects
