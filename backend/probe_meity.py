"""Why can't we read MeitY? Look at what it actually serves."""
import asyncio, httpx, re

UA = {"User-Agent": "Consiva-RegulatoryWatch/1.0 (+compliance monitoring)"}

TARGETS = [
    "https://www.meity.gov.in/",
    "https://www.meity.gov.in/sitemap.xml",
    "https://www.meity.gov.in/robots.txt",
    "https://www.meity.gov.in/rss.xml",
    "https://www.meity.gov.in/feed",
    "https://www.meity.gov.in/static-content/data-protection-framework",
]

async def main():
    async with httpx.AsyncClient(follow_redirects=True, timeout=25, headers=UA) as c:
        for url in TARGETS:
            try:
                r = await c.get(url)
                body = r.text
                ctype = r.headers.get("content-type", "?")[:40]
                # How much of it is script vs prose?
                scripts = len(re.findall(r"<script", body, re.I))
                stripped = re.sub(r"(?s)<(script|style).*?</\1>", " ", body)
                text = re.sub(r"(?s)<[^>]+>", " ", stripped)
                text = re.sub(r"\s+", " ", text).strip()
                print(f"{r.status_code}  {len(body):>8}B raw  {len(text):>6} chars text  "
                      f"{scripts:>3} <script>  {ctype:<26} {url}")
                if text and len(text) < 400:
                    print(f"        text: {text[:200]!r}")
            except Exception as e:
                print(f"---  {type(e).__name__}: {str(e)[:70]}  {url}")
asyncio.run(main())
