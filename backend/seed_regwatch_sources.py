"""Register the regulatory sources this organisation will actually be watched against.

Every URL here was probed first and returns readable text. Nothing is registered on
faith: a source that cannot be collected would fail on every sweep, and a source
omitted because the fetch was awkward would be an invisible gap. Both were measured.

WHAT IS NOT HERE, AND WHY
-------------------------
MeitY's own content pages. The site is a client-rendered Next.js app -- the home page
is a 3.3KB shell with twenty script tags and four characters of text, and its data is
fetched in the browser from an API the HTML does not name. There is nothing for an
HTTP connector to read. Its sitemap IS readable and IS a real signal (it indexes the
documents and act-and-policies pages), so that is what is registered, under a name
that says exactly what it watches. PIB carries MeitY's press releases in plain HTML
and covers the announcement side.

No baseline is accepted here. That is a decision a person makes, with their name on
it, and the first collection raises a `first_capture` finding for exactly that
purpose.
"""
import asyncio
import uuid

from app.agents.regwatch.errors import InvalidSourceError
from app.agents.regwatch.schemas import watch
from app.agents.regwatch.services import collection_service, source_service
from app.db.session import async_session_factory, set_org_scope

ORG = uuid.UUID("8b2c939c-4993-4053-a7b5-a15fdb0b5310")

SOURCES = [
    {
        "name": "MeitY — document and policy index",
        "url": "https://www.meity.gov.in/sitemap.xml",
        "jurisdiction": "India",
        "topic": "rules policy notification",
        "authority": "Ministry of Electronics and Information Technology",
        "check_interval_minutes": 1440,
    },
    {
        "name": "PIB — government press releases",
        "url": "https://www.pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3",
        "jurisdiction": "India",
        "topic": "notification press release",
        "authority": "Press Information Bureau",
        "check_interval_minutes": 1440,
        # A feed, read as a feed. Flattened by the HTML stripper its twenty entries
        # collapse into one 4,000-character line, so a single new press release
        # produces a whole reflowed diff instead of one added line.
        "connector": watch.CONNECTOR_RSS,
    },
    {
        "name": "CERT-In — security advisories",
        "url": "https://www.cert-in.org.in/s2cMainServlet?pageid=PUBADVLIST",
        "jurisdiction": "India",
        "topic": "security breach incident reasonable security",
        "authority": "Indian Computer Emergency Response Team",
        "check_interval_minutes": 1440,
    },
    {
        "name": "EDPB — news and guidance",
        "url": "https://edpb.europa.eu/news/news_en",
        "jurisdiction": "EU",
        "topic": "consent rights transfer guidance",
        "authority": "European Data Protection Board",
        "check_interval_minutes": 1440,
    },
    {
        "name": "Irish DPC — latest news",
        "url": "https://www.dataprotection.ie/en/news-media/latest-news",
        "jurisdiction": "EU",
        "topic": "rights enforcement access request",
        "authority": "Data Protection Commission (Ireland)",
        "check_interval_minutes": 1440,
    },
]


async def main() -> None:
    set_org_scope(ORG)
    registered = []

    async with async_session_factory() as db:
        for spec in SOURCES:
            try:
                row = await source_service.register_source(db, org_id=ORG, **spec)
                await db.commit()
                registered.append(row)
                print(f"registered  {row.name}")
            except InvalidSourceError as exc:
                await db.rollback()
                print(f"SKIPPED     {spec['name']}: {exc}")

    print("\ncollecting each for the first time")
    for row in registered:
        async with async_session_factory() as db:
            source = await source_service.get_source_or_raise(db, row.id, ORG)
            collection = await collection_service.collect(db, source)
            change, finding = await collection_service.compare_and_record(db, source, collection)
            await db.commit()
            health = source_service.health(source)
            print(
                f"  {source.name:<38} {collection.status:<10} "
                f"{(str(collection.content_bytes) + 'B') if collection.content_bytes else '-':>9}  "
                f"{change.change_kind:<14} {finding.reference if finding else '-':<18} "
                f"health={health['state']}"
            )


asyncio.run(main())
