"""Which organisation is behind a vendor the scan recognises, and where it is established (Functional Scope DS-17, CI-01, CI-08; Scanner Score E2).

Every entry is from our own research of each company's public corporate details, never copied from another product's database (CI-08). The provenance is stated on every entry so a reader can
judge it (CI-01, CI-02): "research" means read from the company's public pages and not yet checked against a second source; no entry is marked "verified" until someone has checked it and
recorded when (CI-05). The country is where the organisation is established, not where its servers are: a scan cannot see that. A vendor with no entry is reported as "not identified", never as
being in India.

Add a vendor here only with the organisation and country from its own public pages, and keep `RESEARCHED_ON` honest.
"""

from __future__ import annotations

from dataclasses import dataclass

RESEARCHED_ON = "2026-11-05"


@dataclass(frozen=True)
class VendorOrg:
    organisation: str
    country: str
    provenance: str = "research"
    verified_on: str | None = None

    @property
    def outside_india(self) -> bool:
        return self.country.strip().lower() != "india"


def _o(org: str, country: str) -> VendorOrg:
    return VendorOrg(organisation=org, country=country)


VENDOR_ORGS: dict[str, VendorOrg] = {
    "Google Analytics": _o("Google LLC", "United States"), "Google Tag Manager": _o("Google LLC", "United States"), "Google Ads": _o("Google LLC", "United States"),
    "Google Fonts": _o("Google LLC", "United States"), "YouTube Embed": _o("Google LLC", "United States"),
    "Meta Pixel": _o("Meta Platforms, Inc.", "United States"),
    "Microsoft Clarity": _o("Microsoft Corporation", "United States"), "Bing UET": _o("Microsoft Corporation", "United States"), "LinkedIn Insight Tag": _o("LinkedIn Corporation", "United States"),
    "Xandr (Microsoft Advertising)": _o("Microsoft Corporation", "United States"),
    "PostHog": _o("PostHog, Inc.", "United States"), "Mixpanel": _o("Mixpanel, Inc.", "United States"), "Amplitude": _o("Amplitude, Inc.", "United States"),
    "Segment": _o("Twilio Inc.", "United States"), "Heap Analytics": _o("Heap Inc.", "United States"), "FullStory": _o("FullStory, Inc.", "United States"),
    "HubSpot": _o("HubSpot, Inc.", "United States"), "Intercom": _o("Intercom, Inc.", "United States"), "Stripe": _o("Stripe, Inc.", "United States"),
    "Cloudflare": _o("Cloudflare, Inc.", "United States"), "Vimeo Embed": _o("Vimeo, Inc.", "United States"), "Pinterest Tag": _o("Pinterest, Inc.", "United States"),
    "Snap Pixel": _o("Snap Inc.", "United States"), "Klaviyo": _o("Klaviyo, Inc.", "United States"), "Amazon Advertising": _o("Amazon.com, Inc.", "United States"),
    "Outbrain": _o("Outbrain Inc.", "United States"), "OpenX": _o("OpenX Technologies, Inc.", "United States"), "PubMatic": _o("PubMatic, Inc.", "United States"),
    "The Trade Desk": _o("The Trade Desk, Inc.", "United States"), "Magnite (Rubicon Project)": _o("Magnite, Inc.", "United States"), "AdRoll (NextRoll)": _o("NextRoll, Inc.", "United States"),
    "Matomo": _o("InnoCraft Ltd", "New Zealand"), "Plausible Analytics": _o("Plausible Insights OÜ", "Estonia"), "Hotjar": _o("Hotjar Ltd", "Malta"),
    "Clicky Analytics": _o("Roxr Software Ltd", "United Kingdom"), "Dianomi": _o("Dianomi plc", "United Kingdom"), "Permutive": _o("Permutive Technologies Ltd", "United Kingdom"),
    "Criteo": _o("Criteo SA", "France"), "Smart AdServer": _o("Equativ SAS", "France"), "Seedtag": _o("Seedtag Advertising S.L.", "Spain"), "Teads": _o("Teads S.A.", "Luxembourg"),
    "Index Exchange": _o("Index Exchange Inc.", "Canada"), "Taboola": _o("Taboola.com Ltd", "Israel"), "Envato CDN": _o("Envato Pty Ltd", "Australia"),
    "TikTok Pixel": _o("TikTok Pte. Ltd.", "Singapore"),
    "Razorpay": _o("Razorpay Software Private Limited", "India"), "Consiva Consent SDK": _o("Swaran Soft Support Solutions Pvt. Ltd.", "India"),
}


def org_of(vendor: str | None) -> VendorOrg | None:
    """The organisation behind a recognised vendor, or None when it is not in the table (not identified, never assumed)."""
    return VENDOR_ORGS.get(vendor or "")
