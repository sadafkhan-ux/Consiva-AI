"""Shared eTLD+1 helper -- previously duplicated verbatim in crawler.py,
cookie_detector.py, and tracker_detector.py. Single source of truth so a future fix
to this logic (e.g. IDN normalization, empty-suffix handling) only needs to happen
once."""

from urllib.parse import urlsplit

import tldextract


def registered_domain(url_or_host: str) -> str:
    ext = tldextract.extract(url_or_host)
    return f"{ext.domain}.{ext.suffix}" if ext.suffix else ext.domain


def site_host(url_or_host: str) -> str:
    """The host a scan is filed under in `websites.domain`: lower-cased, no port, and
    with a leading "www." removed.

    Deliberately NOT registered_domain() above. eTLD+1 would fold every subdomain into
    one site, and these really are separate applications that happen to share a parent
    domain -- this deployment has 40 scans of hireflow.gignaati.com next to 5 of
    gignaati.com, and merging them would put one app's consent evidence, monitoring
    baseline and ownership verification under the other's name.

    What it DOES fold is the pair that is always one site: "www.example.com" and
    "example.com". Those were previously two rows, because scan_service took
    urlparse(url).netloc verbatim, so verifying ownership of one left the other
    unverified and their scan histories never met. The port goes for the same reason --
    https://example.com and https://example.com:443 are not two websites.
    """
    raw = url_or_host.strip()
    host = urlsplit(raw if "//" in raw else f"//{raw}").hostname or ""
    host = host.lower()
    return host.removeprefix("www.")
