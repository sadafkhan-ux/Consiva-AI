"""Detection tables for the consent-gap analyser.

Every table here is data, deliberately separated from the logic that applies it, so a
missed vendor is a one-line data change rather than a code change. All of it comes from
the working implementation's own field results against real Indian SME sites -- entries
exist because something was missed without them, so prefer adding over pruning.

Nothing in this module is LLM-driven. Compliance findings must be reproducible and
auditable: the same page has to produce the same finding every time, and it must be
possible to point at the exact string that caused it.
"""

import re

# --------------------------------------------------------------------------------
# Consent management platforms. Matched case-insensitively against the page HTML AND
# every <script src>. First hit wins and names the CMP.
# --------------------------------------------------------------------------------
CMP_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("OneTrust", ("onetrust", "optanon", "cookielaw.org", "otsdkstub")),
    ("Cookiebot", ("cookiebot", "consent.cookiebot.com", "cybotcookiebot")),
    ("CookieYes", ("cookieyes", "cdn-cookieyes.com", "cky-consent")),
    ("Osano", ("osano.com", "osano-cm")),
    ("Termly", ("termly.io", "termly-code")),
    ("Quantcast", ("quantcast", "quantcast.mgr", "__cmpcallbacks")),
    ("Didomi", ("didomi.io", "didomi-host", "window.didomi")),
    ("Usercentrics", ("usercentrics", "uc-block", "app.usercentrics.eu")),
    ("iubenda", ("iubenda.com", "iubenda_cs")),
    ("Complianz", ("complianz", "cmplz-")),
    ("CookieScript", ("cookie-script.com", "cookiescript")),
    ("Borlabs", ("borlabs-cookie",)),
    ("TrustArc", ("trustarc", "truste.com", "consent.trustarc.com")),
    ("Seers", ("seersco.com", "seers-cmp")),
    ("Secure Privacy", ("secureprivacy.ai",)),
    ("CookiePro", ("cookiepro.com",)),
    ("Ketch", ("ketchcdn.com", "ketch.com")),
    ("Klaro", ("klaro-config", "klaro.js")),
    ("tarteaucitron", ("tarteaucitron",)),
    ("Civic Cookie Control", ("civiccomputing", "cookiecontrol")),
    # The two WordPress plugins below are the most common CMPs on Indian SME sites by
    # a wide margin. Leaving them out silently misclassifies a large share of real
    # sites as having no consent mechanism at all.
    ("GDPR Cookie Consent (WP)", ("cookie-law-info", "cli-modal", "cli_settings")),
    ("Moove GDPR (WP)", ("moove_gdpr", "gdpr-cookie-compliance")),
)

# --------------------------------------------------------------------------------
# Cookie banner with no recognised vendor behind it. Still counts as a banner --
# typically it only informs and sets trackers regardless.
# --------------------------------------------------------------------------------
BANNER_SELECTOR_HINTS: tuple[str, ...] = (
    "cookie-banner", "cookie-consent", "cookie-notice", "cookie-bar", "cookiebanner",
    "cookieconsent", "gdpr-banner", "consent-banner", "cc-banner", "cookie-popup",
)

BANNER_TEXT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE) for p in (
        r"we use cookies",
        r"this (?:website|site) uses cookies",
        r"cookie (?:policy|preferences|settings|consent)",
        r"accept (?:all )?cookies",
        r"by continuing to (?:use|browse)",
        r"manage (?:your )?(?:cookie )?preferences",
    )
)

PRIVACY_LINK_PATTERN = re.compile(
    r"privacy[-_ ]?policy|cookie[-_ ]?policy|privacy[-_ ]?notice|data[-_ ]?protection", re.IGNORECASE
)

# The pattern above requires "privacy" to be FOLLOWED by "policy"/"notice", so a bare
# "Privacy" link is missed -- confirmed live on wordpress.org, whose only privacy link
# is text "Privacy" at href /about/privacy/. That false negative silently adds 15
# points to a site's score, so these two cover the bare form.
#
# Both are deliberately narrow rather than a loose "privacy" substring: anchored to a
# whole URL path segment, or to link text that IS the word rather than merely contains
# it. A page discussing "privacy-first marketing" must not read as having a policy.
PRIVACY_HREF_PATH_PATTERN = re.compile(
    r"/(privacy|privacy[-_]statement|cookies?|datenschutz)(?:/|\?|#|$)", re.IGNORECASE
)
PRIVACY_EXACT_TEXT_PATTERN = re.compile(
    r"^\s*(privacy|cookies?|privacy\s*&\s*cookies|privacy\s*policy)\s*$", re.IGNORECASE
)

# --------------------------------------------------------------------------------
# Tracking cookies, matched as a PREFIX on the cookie name.
#
# These lists are a floor, not a ceiling: any third-party cookie counts as a tracker
# whether or not it appears here (see classify_cookie). Name lists always miss things
# -- CLID, SRM_B and ANONCHK were invisible until that rule existed, and they were a
# third of one real site's trackers.
# --------------------------------------------------------------------------------
ADVERTISING_PREFIXES: tuple[str, ...] = (
    "_fbp", "_fbc", "fr", "IDE", "test_cookie", "NID", "DSID", "_gcl", "li_sugr",
    "bcookie", "lidc", "UserMatchHistory", "_uetsid", "_uetvid", "YSC",
    "VISITOR_INFO1_LIVE",
)

SESSION_REPLAY_PREFIXES: tuple[str, ...] = ("_hj", "_hjs", "_clck", "_clsk", "MUID")

ALL_TRACKER_PREFIXES: tuple[str, ...] = (
    "_ga", "_gid", "_gat", "__utm", "_gcl", "_dc_gtm", "_fbp", "_fbc", "fr", "_hjs",
    "_hj", "_clck", "_clsk", "MUID", "__hstc", "hubspotutk", "__hssrc", "__hssc",
    "_lfa", "li_sugr", "bcookie", "lidc", "UserMatchHistory", "IDE", "test_cookie",
    "NID", "DSID", "mp_", "ajs_", "amplitude_", "_pk_", "_omappvp", "_uetsid",
    "_uetvid", "YSC", "VISITOR_INFO1_LIVE",
)

LONG_LIVED_DAYS = 180

# --------------------------------------------------------------------------------
# Form field classification. Applied to a single descriptor string per field, built
# from name + id + placeholder + type + aria-label + associated <label> text.
# --------------------------------------------------------------------------------
FIELD_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "email": tuple(re.compile(p, re.IGNORECASE) for p in (r"\bemail\b", r"\be-?mail\b", r"\bmail\b")),
    "phone": tuple(re.compile(p, re.IGNORECASE) for p in (
        r"\bphone\b", r"\bmobile\b", r"\btel\b", r"\bcontact.?number\b", r"\bwhatsapp\b")),
    "name": tuple(re.compile(p, re.IGNORECASE) for p in (
        r"\bname\b", r"\bfull.?name\b", r"\bfirst.?name\b", r"\blast.?name\b")),
    "address": tuple(re.compile(p, re.IGNORECASE) for p in (
        r"\baddress\b", r"\bcity\b", r"\bpincode\b", r"\bpin.?code\b", r"\bzip\b")),
    "company": tuple(re.compile(p, re.IGNORECASE) for p in (
        r"\bcompany\b", r"\borganisation\b", r"\borganization\b")),
    "message": tuple(re.compile(p, re.IGNORECASE) for p in (
        r"\bmessage\b", r"\benquiry\b", r"\binquiry\b", r"\bcomment\b", r"\brequirement\b")),
}

# A form is only a finding if it collects one of these. A bare message box is not
# personal data.
IDENTIFYING_FIELDS: frozenset[str] = frozenset({"email", "phone", "name", "address"})

CONSENT_CHECKBOX_PATTERN = re.compile(r"consent|agree|privacy|terms|permission|opt.?in", re.IGNORECASE)
FORM_PRIVACY_LINK_PATTERN = re.compile(r"privacy|policy|terms", re.IGNORECASE)

FORM_PROVIDERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Google Forms", ("docs.google.com/forms", "google.com/forms")),
    ("Typeform", ("typeform.com",)),
    ("HubSpot Forms", ("js.hsforms.net", "hsforms.com", "forms.hsforms")),
    ("Zoho Forms", ("forms.zohopublic", "zohoforms")),
    ("JotForm", ("jotform.com",)),
    ("Wufoo", ("wufoo.com",)),
    ("Formspree", ("formspree.io",)),
    ("Mailchimp Form", ("list-manage.com", "chimpstatic")),
    ("WPForms", ("wpforms",)),
    ("Contact Form 7", ("wpcf7", "contact-form-7")),
    ("Gravity Forms", ("gform_", "gravityforms")),
)

# --------------------------------------------------------------------------------
# Tech stack. Matched on script srcs + HTML.
# --------------------------------------------------------------------------------
CMS_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Shopify", ("cdn.shopify.com", "shopify.theme", "myshopify.com")),
    ("WordPress", ("wp-content", "wp-includes", "wp-json")),
    ("Wix", ("wix.com", "wixstatic.com", "parastorage.com")),
    ("Squarespace", ("squarespace.com", "sqsp.net", "static1.squarespace")),
    ("Webflow", ("webflow.com", "assets.website-files.com")),
    ("Drupal", ("/sites/default/files", "drupal.js", "drupal-settings-json")),
    ("Joomla", ("/media/jui/", "joomla", "com_content")),
    ("Ghost", ("ghost.io", "/ghost/api/")),
    ("Magento", ("mage/cookies", "static/version", "magento")),
    ("Blogger", ("blogger.com", "blogspot.com")),
    ("HubSpot CMS", ("hs-scripts.com", "hubspotusercontent")),
    ("Duda", ("dudamobile.com", "d1a19ys8w1wkc1.cloudfront.net", "duda.co")),
)

ECOMMERCE_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Shopify", ("cdn.shopify.com", "myshopify.com")),
    ("WooCommerce", ("woocommerce", "wc-ajax", "wc_add_to_cart")),
    ("Magento", ("magento", "mage/cookies")),
    ("BigCommerce", ("bigcommerce.com", "bigcommerce")),
    ("PrestaShop", ("prestashop",)),
    ("OpenCart", ("opencart", "index.php?route=")),
    ("Zoho Commerce", ("zohocommerce", "zohocdn.com/commerce")),
)

PAYMENT_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Razorpay", ("razorpay.com", "checkout.razorpay")),
    ("PayU", ("payu.in", "payubiz", "secure.payu")),
    ("CCAvenue", ("ccavenue.com",)),
    ("Instamojo", ("instamojo.com",)),
    ("Cashfree", ("cashfree.com",)),
    ("Paytm", ("paytm.in", "securegw.paytm")),
    ("BillDesk", ("billdesk.com",)),
    ("Stripe", ("js.stripe.com", "stripe.com/v3")),
    ("PayPal", ("paypal.com/sdk", "paypalobjects.com")),
)

ANALYTICS_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Google Tag Manager", ("googletagmanager.com/gtm.js", "gtm.start")),
    ("Universal Analytics", ("google-analytics.com/analytics.js", "ga('create'")),
    ("Hotjar", ("static.hotjar.com", "hotjar.com/c/hotjar")),
    ("Microsoft Clarity", ("clarity.ms", "clarity.js")),
    ("Matomo", ("matomo.js", "piwik.js", "matomo.php")),
    ("Mixpanel", ("mixpanel.com", "cdn.mxpnl.com")),
    ("Segment", ("cdn.segment.com", "analytics.min.js")),
    ("Plausible", ("plausible.io",)),
    ("Yandex Metrica", ("mc.yandex.ru", "yandex_metrika")),
)

MARKETING_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Meta Pixel", ("connect.facebook.net", "fbq('init'", "facebook.com/tr")),
    ("LinkedIn Insight", ("snap.licdn.com", "_linkedin_partner_id")),
    ("HubSpot", ("hs-scripts.com", "hs-analytics.net")),
    ("Mailchimp", ("chimpstatic.com", "list-manage.com")),
    ("Zoho", ("zohopublic", "salesiq.zoho", "zohocdn.com")),
    ("Freshworks", ("freshchat.com", "freshworks.com", "wchat.freshchat")),
    ("Intercom", ("intercom.io", "intercomcdn.com")),
    ("Drift", ("drift.com", "driftt.com")),
    ("Tawk.to", ("tawk.to",)),
    ("Crisp", ("crisp.chat",)),
    ("Google Ads", ("googleadservices.com", "googlesyndication.com", "doubleclick.net")),
    ("WhatsApp Business", ("wa.me/", "api.whatsapp.com", "web.whatsapp.com")),
)

# Case-SENSITIVE measurement ids. A lowercase substring search for "g-" matches almost
# every page on the web; these must stay anchored and case-sensitive.
GA4_ID_PATTERN = re.compile(r"\bG-[A-Z0-9]{4,}")
GTM_ID_PATTERN = re.compile(r"\bGTM-[A-Z0-9]{4,}")

# --------------------------------------------------------------------------------
# Page status. Parked is checked BEFORE blocked -- otherwise dead stores (which often
# answer 402/503) get retried forever.
# --------------------------------------------------------------------------------
PARKED_MARKERS: tuple[str, ...] = (
    "このドメインは", "お名前.com", "sedoparking", "sedo.com/search", "afternic.com",
    "hugedomains.com", "dan.com/buy-domain", "parkingcrew", "bodis.com",
    "domain is for sale", "buy this domain", "this domain is parked", "domain for sale",
    "inquire about this domain", "apache2 ubuntu default page", "welcome to nginx",
    "iis windows server", "future home of something", "index of /",
    "site is under construction", "under construction", "coming soon",
    "default web site page", "account suspended", "bandwidth limit exceeded",
    "store unavailable", "this store is currently unavailable",
    "shop is currently unavailable", "this store is unavailable",
    "site is temporarily unavailable", "this account has been suspended",
    "website is temporarily unavailable", "service temporarily unavailable",
)

BLOCKED_MARKERS: tuple[str, ...] = (
    "checking your browser", "just a moment", "please wait while we verify",
    "verify you are human", "verifying you are human", "are you a robot",
    "enable javascript and cookies to continue", "ddos protection by",
    "attention required! | cloudflare", "cf-browser-verification", "access denied",
    "request unsuccessful", "unusual traffic", "captcha", "hcaptcha", "recaptcha",
    "incapsula incident", "this website is using a security service to protect itself",
)

# These statuses only mean "blocked" when the body is essentially empty. A 402 or 503
# that still renders real content is usable -- a suspended Shopify store still names
# the shop.
BLOCKED_STATUSES: frozenset[int] = frozenset({401, 402, 403, 407, 423, 429, 451, 500, 502, 503, 504})
BLOCKED_BODY_MAX_CHARS = 400
