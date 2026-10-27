"""Skips the download of large, plain photographs on the pages after the first, to save the scanner's CPU and time.

What is never skipped, so that what the scan records about tracking is the same with or without this:
  * every request from a tracker or consent tool in the catalogue (a pixel from a known tracker domain is a tracker whatever its file type);
  * anything that looks like a measurement beacon (pixel, collect, track, 1x1 ...);
  * any third-party image whose address carries a query string (the form tracking pixels take; rules/consent_rules.py reads it the same way);
  * a gif or an svg (the formats of 1x1 pixels, logos and icons), and any image whose address has no image-file extension.
The request is still made by the page, so it is still recorded by the crawler's request listener and still reaches tracker detection; only the bytes of the file are not fetched.
The first page (the one whose screenshot, banner and consent behaviour the report shows) and both consent-interaction passes never use this.
"""

import logging
from urllib.parse import urlsplit

from playwright.async_api import Route

from app.rules.consent_rules import _looks_like_a_beacon
from app.rules.tracker_catalog import match_cmp, match_vendor
from app.scanner._domain import registered_domain

logger = logging.getLogger(__name__)

#: Photograph formats, the large files. gif (pixels) and svg (logos, icons) are deliberately not here.
_PHOTO_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".avif", ".bmp", ".tif", ".tiff", ".jfif"})


def should_skip_image(url: str, resource_type: str, site_registered: str) -> bool:
    """True only for an image request that is certainly a plain photograph from the site or its own content network, never for anything a tracker could be."""
    if resource_type != "image":
        return False
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False  # data: and blob: images cost nothing to fetch
    path = parts.path.lower()
    if not any(path.endswith(ext) for ext in _PHOTO_EXTENSIONS):
        return False
    if match_vendor(script_src=url) is not None or match_cmp(script_src=url) is not None or _looks_like_a_beacon(url):
        return False
    first_party = registered_domain(parts.hostname or "") == site_registered
    return first_party or not parts.query


class ImageSkipper:
    """A page-level route handler for one secondary page. Anything it does not skip goes on to the context's own handler (the navigation safety check)."""

    def __init__(self, site_registered: str) -> None:
        self._site = site_registered
        self.skipped = 0

    async def __call__(self, route: Route) -> None:
        request = route.request
        if should_skip_image(request.url, request.resource_type, self._site):
            self.skipped += 1
            await route.abort("blockedbyclient")
            return
        await route.fallback()
