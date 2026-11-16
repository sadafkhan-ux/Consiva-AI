import pytest
from pydantic import ValidationError

from app.enterprise.api.schemas import CreateEnterpriseScan


@pytest.mark.parametrize("typed,stored", [
    ("consiva.ai", "https://consiva.ai"),                       # a bare domain is promoted to https
    ("  swaransoft.com  ", "https://swaransoft.com"),
    ("www.example.com/about", "https://www.example.com/about"),
    ("https://example.com", "https://example.com"),
    ("http://example.com", "http://example.com"),                # an explicit http:// is kept
])
def test_a_domain_typed_the_way_people_say_it_is_accepted(typed, stored):
    assert CreateEnterpriseScan(website_url=typed, authorized=True).website_url == stored


@pytest.mark.parametrize("bad", ["", "   ", "localhost", "ftp://example.com", "https://", "javascript:alert(1)", "http://"])
def test_a_non_address_is_still_refused_with_a_plain_message(bad):
    with pytest.raises(ValidationError):
        CreateEnterpriseScan(website_url=bad, authorized=True)


def test_domain_verification_follows_the_deployment_setting_like_the_5_page_scan():
    """Not hard-wired: the Enterprise start endpoint asks for DNS verification only when SCANNER_REQUIRE_DOMAIN_VERIFICATION is on."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "app" / "enterprise" / "api" / "scans.py").read_text(encoding="utf-8")
    assert "if get_settings().scanner_require_domain_verification:" in src
    before, after = src.split("if get_settings().scanner_require_domain_verification:")
    assert "DomainNotVerified" not in before.split("async def create_scan")[1]       # the check sits inside the setting's branch
    assert "DomainNotVerified" in after.split("try:")[0]


def test_a_whole_site_scan_needs_a_verified_domain_by_default_and_only_the_setting_changes_that(monkeypatch):
    from app.config import Settings
    from app.scanner import scan_profiles

    base = dict(nvidia_api_key="k", nvidia_llm_model="m", nvidia_embed_model="e", supabase_url="http://x", supabase_service_role_key="k",
                database_url="postgresql+asyncpg://u:p@localhost/t", supabase_jwt_secret="s")
    assert scan_profiles.resolve_profile("application", Settings(_env_file=None, **base)).requires_verified_domain is True
    relaxed = scan_profiles.resolve_profile("application", Settings(_env_file=None, consent_app_require_verified_domain=False, **base))
    assert relaxed.requires_verified_domain is False and relaxed.max_pages is None       # only verification changes, never the page cap
    assert scan_profiles.resolve_profile("web", Settings(_env_file=None, **base)).requires_verified_domain is False
