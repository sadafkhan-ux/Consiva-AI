"""A cookie that fires on several pages is ONE cookie: five cookies seen on five pages are five, never twenty-five."""
from app.scanner.cookie_detector import attribute_new_cookies, cookie_key, detect_cookies
from app.scanner.crawler import _merge_cookie_pass

SITE = "example.com"


def raw(name="_ga", domain=".example.com", path="/"):
    return {"name": name, "value": "x", "domain": domain, "path": path, "expires": -1, "secure": False, "httpOnly": False, "sameSite": "Lax"}


def test_the_same_cookie_read_after_five_pages_is_one_record():
    """The browser keeps one cookie per name, domain and path however many pages set it, so the jar read after five pages holds it once."""
    jar_after_five_pages = [raw()]
    assert len(detect_cookies(jar_after_five_pages, SITE)) == 1


def test_a_cookie_set_by_five_pages_is_attributed_once_to_the_first_page():
    seen: set = set()
    observations = {}
    for n in range(1, 6):
        page = f"https://example.com/p{n}"
        event = {"name": "_ga", "domain": "example.com", "path": "/", "page_url": page, "request_url": page, "observed_at": f"2026-11-13T10:0{n}:00Z"}
        new = attribute_new_cookies([raw()], seen, [page], [event], f"2026-11-13T10:0{n}:00Z", "pre_consent")
        observations.update(new)
        seen.update(new)                                  # what the crawler does after every batch
    assert list(observations) == [cookie_key("_ga", ".example.com", "/")]
    assert observations[cookie_key("_ga", "example.com", "/")].page_url == "https://example.com/p1"      # first sighting, not five


def test_five_different_cookies_are_five_and_the_same_one_five_times_is_one():
    five_different = [raw(name=f"c{i}") for i in range(5)]
    assert len(detect_cookies(five_different, SITE)) == 5
    same_five_times = _merge_cookie_pass([], detect_cookies([raw()] * 1, SITE))
    for _ in range(4):
        same_five_times = _merge_cookie_pass(same_five_times, detect_cookies([raw()], SITE))
    assert len(same_five_times) == 1


def test_the_consent_states_of_one_cookie_are_merged_not_counted_again():
    def in_state(state):
        return [c.model_copy(update={"consent_states": [state]}) for c in detect_cookies([raw()], SITE)]

    merged = _merge_cookie_pass(_merge_cookie_pass(in_state("pre_consent"), in_state("post_accept")), in_state("post_reject"))
    assert len(merged) == 1
    assert merged[0].consent_states == ["pre_consent", "post_accept", "post_reject"]


def test_a_leading_dot_and_letter_case_do_not_make_a_second_cookie():
    merged = _merge_cookie_pass(detect_cookies([raw(domain=".Example.com")], SITE), detect_cookies([raw(domain="example.com")], SITE))
    assert len(merged) == 1


def test_a_different_path_or_a_different_domain_is_a_different_cookie():
    base = detect_cookies([raw(path="/")], SITE)
    assert len(_merge_cookie_pass(list(base), detect_cookies([raw(path="/admin")], SITE))) == 2
    assert len(_merge_cookie_pass(list(base), detect_cookies([raw(domain=".shop.example.com")], SITE))) == 2
