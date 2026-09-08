"""Shared eTLD+1 helper -- previously duplicated verbatim in crawler.py,
cookie_detector.py, and tracker_detector.py. Single source of truth so a future fix
to this logic (e.g. IDN normalization, empty-suffix handling) only needs to happen
once."""

import tldextract


def registered_domain(url_or_host: str) -> str:
    ext = tldextract.extract(url_or_host)
    return f"{ext.domain}.{ext.suffix}" if ext.suffix else ext.domain
