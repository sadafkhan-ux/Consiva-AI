"""A public scan reads one page of each kind (Functional Scope QS-08 to QS-11), and says why each page was chosen, truthfully.

Five pages should be five different kinds of evidence: the entered address, the privacy page, the cookie policy, the terms or legal page and the contact or grievance page, and then (if room is left) a page that
collects visitor details and the most prominent remaining page. A second page of a kind already read is not read, and a kind whose page failed to load is read again from the next candidate.
"""

import pytest

from app.config import get_settings
from app.scanner.crawler import _crawl_full_site
from app.scanner.scan_profiles import APPLICATION, WEB, resolve_profile
from app.scanner.url_normalize import PAGE_CATEGORIES, category_of, selection_reason
from tests.test_scanner_coverage import ROOT, _FakeContext, _links


@pytest.mark.parametrize("path,kind", [
    ("/privacy", "privacy"), ("/privacy-policy", "privacy"), ("/privacy-and-cookies", "privacy"), ("/data-protection", "privacy"), ("/legal/privacy-rights-and-grievance", "privacy"),
    ("/cookie-policy", "cookie"), ("/cookies", "cookie"), ("/terms", "terms"), ("/terms-of-use", "terms"), ("/legal", "terms"), ("/disclaimer", "terms"),
    ("/contact", "contact"), ("/contact-us", "contact"), ("/sign-up", "form"), ("/login", "form"), ("/checkout", "form"),
    ("/about", "remaining"), ("/pricing", "remaining"), ("/blog/dpdp-act-compliance-guide", "remaining"), ("/privacy-blueprint", "remaining"), ("/something-else", "remaining"),
])
def test_an_address_is_one_kind_of_page(path, kind):
    assert category_of(ROOT + path) == kind and kind in PAGE_CATEGORIES


def test_the_reason_for_a_page_is_true_to_what_was_found_on_it():
    assert "privacy" in selection_reason(ROOT + "/privacy", "sitemap").lower() and "sitemap" in selection_reason(ROOT + "/privacy", "sitemap")
    assert selection_reason(ROOT + "/cookie-policy", "link").startswith("Cookie policy")
    assert selection_reason(ROOT + "/contact", "link").startswith("Contact or grievance page")
    assert selection_reason(ROOT + "/sign-up", "link", True).startswith("Page that collects visitor details: a form was found on it.")
    no_form = selection_reason(ROOT + "/sign-up", "link", False)
    assert "no form was found" in no_form and "collects visitor details" not in no_form          # never described as collecting details when it has no form (QS-10)
    assert "was not recorded" in selection_reason(ROOT + "/sign-up", "link", None)
    assert selection_reason(ROOT + "/", "seed") == "The address that was entered: always scanned first."


def _site(paths: list[str], extra_links: dict[str, list[str]] | None = None) -> dict:
    pages = {f"{ROOT}/": _links(*paths)}
    for p in paths:
        pages[f"{ROOT}{p}"] = _links(*(extra_links or {}).get(p, []))
    return pages


async def _scan(paths, failures=None, profile=WEB):
    settings = get_settings()
    context = _FakeContext(_site(paths), {f"{ROOT}{k}": v for k, v in (failures or {}).items()})
    records, *_rest, diagnostics = await _crawl_full_site(context, ROOT, settings, disallowed_prefixes=set(), profile=resolve_profile(profile, settings), deadline_monotonic=None)
    return [p.url.removeprefix(ROOT) or "/" for p in records if p.discovered_via in ("seed", "link", "sitemap")], diagnostics


async def test_two_privacy_pages_take_one_slot_and_another_kind_takes_the_other():
    got, diag = await _scan(["/privacy", "/privacy-policy", "/privacy-notice", "/cookie-policy", "/terms", "/contact", "/sign-up", "/about"])
    assert len(got) == 5 and got[0] == "/"
    kinds = [category_of(ROOT + u) for u in got[1:]]
    assert sorted(kinds) == ["contact", "cookie", "privacy", "terms"]                       # five pages, five different kinds (pages are listed in the order they finished loading)
    assert diag["skipped_by_reason"].get("category_already_covered") == 2                    # the other two privacy pages: not read, with that reason


async def test_a_kind_whose_page_did_not_load_is_read_from_the_next_candidate():
    got, _ = await _scan(["/privacy", "/privacy-policy", "/cookie-policy", "/terms", "/contact"], failures={"/privacy": RuntimeError("dead link")})
    assert "/privacy" not in got and "/privacy-policy" in got
    assert sorted(category_of(ROOT + u) for u in got[1:]) == sorted(["privacy", "cookie", "terms", "contact"])


async def test_no_duplicates_are_used_to_fill_the_five_when_fewer_kinds_exist():
    got, diag = await _scan(["/privacy", "/privacy-policy", "/terms"])
    assert len(got) == 3 and diag["skipped_by_reason"].get("category_already_covered") == 1


async def test_remaining_pages_only_fill_what_is_left_after_the_distinct_kinds():
    got, _ = await _scan(["/about", "/pricing", "/blog/x", "/sign-up", "/privacy", "/privacy-policy"])
    kinds = [category_of(ROOT + u) for u in got[1:]]
    assert len(got) == 5 and kinds.count("privacy") == 1 and "form" in kinds                # the kinds come first; the rest of the budget goes to the most prominent remaining pages
    assert kinds.count("remaining") == 2


async def test_a_site_with_only_ordinary_pages_still_uses_its_budget():
    got, _ = await _scan(["/a", "/b", "/c", "/d", "/e"])
    assert len(got) == 5


async def test_an_application_scan_is_unchanged_and_reads_every_page():
    got, diag = await _scan(["/privacy", "/privacy-policy", "/privacy-notice"], profile=APPLICATION)
    assert len(got) == 4 and "category_already_covered" not in diag["skipped_by_reason"]
