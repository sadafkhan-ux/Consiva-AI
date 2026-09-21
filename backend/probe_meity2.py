import asyncio, httpx, re
UA = {"User-Agent": "Consiva-RegulatoryWatch/1.0 (+compliance monitoring)"}

async def main():
    async with httpx.AsyncClient(follow_redirects=True, timeout=25, headers=UA) as c:
        r = await c.get("https://www.meity.gov.in/sitemap.xml")
        print("--- sitemap.xml (first 900 chars) ---")
        print(r.text[:900])
        print("\n--- scripts referenced by the SPA shell ---")
        shell = (await c.get("https://www.meity.gov.in/")).text
        for m in re.findall(r'src="([^"]+)"', shell)[:12]:
            print("  ", m)
        print("\n--- api guesses ---")
        for path in ["/api/whats-new", "/api/v1/whats-new", "/api/documents",
                     "/api/press-release", "/server/api/whats-new",
                     "/api/content/whats-new"]:
            try:
                rr = await c.get("https://www.meity.gov.in" + path)
                print(f"  {rr.status_code} {len(rr.text):>7}B  {rr.headers.get('content-type','?')[:28]}  {path}")
            except Exception as e:
                print(f"  --- {type(e).__name__} {path}")
asyncio.run(main())
