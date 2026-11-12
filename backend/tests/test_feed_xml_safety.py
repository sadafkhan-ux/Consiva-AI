"""A feed is untrusted XML: an entity bomb must be refused, and an ordinary feed must still be read."""
from app.agents.regwatch.connectors import feed_source

BOMB = '''<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;"><!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]>
<rss version="2.0"><channel><item><title>&c;</title><link>https://x.test/1</link></item></channel></rss>'''

OK = '<rss version="2.0"><channel><item><title>Rule changed</title><link>https://x.test/1</link><pubDate>Mon, 02 Nov 2026 10:00:00 GMT</pubDate></item></channel></rss>'


def test_an_entity_bomb_is_refused_not_expanded():
    assert feed_source.normalize(BOMB) is None


def test_an_ordinary_feed_is_still_read():
    out = feed_source.normalize(OK)
    assert out is not None and "Rule changed" in out
