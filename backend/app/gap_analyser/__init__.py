"""Pre-consent consent-gap analyser.

`analyse(url) -> dict` opens a page in a fresh browser context, clicks nothing, and
reports what the site does with personal data before the visitor has consented.
"""

from app.gap_analyser.analyser import analyse, analyse_async

__all__ = ["analyse", "analyse_async"]
