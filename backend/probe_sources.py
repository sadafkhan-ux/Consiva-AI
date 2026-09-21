"""Which real regulator pages can the HTTP connector actually read?

Registering a source that can never be collected is not free: it will fail on
every sweep. Registering nothing because the fetch is awkward is worse -- that is
an invisible gap. So: measure first, then decide per source.
"""
import asyncio
from app.agents.regwatch.connectors import http_source

CANDIDATES = [
    # India -- the jurisdiction that matters most here
    ("MeitY home",              "https://www.meity.gov.in/"),
    ("MeitY documents",         "https://www.meity.gov.in/documents"),
    ("MeitY whatsnew",          "https://www.meity.gov.in/whats-new"),
    ("MeitY press releases",    "https://www.meity.gov.in/press-release"),
    ("PIB MeitY releases",      "https://www.pib.gov.in/indexd.aspx"),
    ("India Code DPDP",         "https://www.indiacode.nic.in/handle/123456789/20063"),
    ("eGazette",                "https://egazette.gov.in/"),
    ("CERT-In home",            "https://www.cert-in.org.in/"),
    ("CERT-In advisories",      "https://www.cert-in.org.in/s2cMainServlet?pageid=PUBADVLIST"),
    ("TRAI releases",           "https://www.trai.gov.in/release-publication/press-release"),
    ("RBI notifications",       "https://www.rbi.org.in/Scripts/NotificationUser.aspx"),
    # International, for orgs operating outside India
    ("EDPB news",               "https://edpb.europa.eu/news/news_en"),
    ("EDPB RSS",                "https://edpb.europa.eu/rss/news_en"),
    ("ICO news",                "https://ico.org.uk/about-the-ico/media-centre/news-and-blogs/"),
    ("Irish DPC news",          "https://www.dataprotection.ie/en/news-media/latest-news"),
    ("CNIL actualites",         "https://www.cnil.fr/fr/actualites"),
    ("EUR-Lex GDPR",            "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32016R0679"),
]

async def probe(label, url):
    try:
        r = await http_source.fetch(url)
        return ("OK", r.http_status, len(r.text), label, url, "")
    except Exception as exc:
        return ("--", getattr(exc, "status_code", "-"), 0, label, url,
                f"{type(exc).__name__}: {str(exc)[:70]}")

async def main():
    results = await asyncio.gather(*(probe(l, u) for l, u in CANDIDATES))
    print(f"{'':2}  {'chars':>7}  {'source':<22} url")
    for ok, _code, chars, label, url, err in results:
        print(f"{ok:2}  {chars:>7}  {label:<22} {url}")
        if err:
            print(f"{'':33}{err}")
asyncio.run(main())
