import asyncio, httpx, re
UA = {"User-Agent": "Consiva-RegulatoryWatch/1.0 (+compliance monitoring)"}

async def main():
    async with httpx.AsyncClient(follow_redirects=True, timeout=25, headers=UA) as c:
        sm = (await c.get("https://www.meity.gov.in/sitemap.xml")).text
        locs = re.findall(r"<loc>(.*?)</loc>", sm)
        print(f"MeitY sitemap: {len(locs)} URLs, {len(sm)} bytes")
        interesting = [u for u in locs if any(k in u for k in
                       ("document", "notification", "act", "rule", "data-protection", "whats-new", "press"))]
        print("  relevant entries:", interesting[:8] or "none")
        print()
        for label, url in [
            ("PIB RSS (all)",      "https://www.pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3"),
            ("PIB English rel",    "https://www.pib.gov.in/allRel.aspx"),
            ("PIB press releases", "https://www.pib.gov.in/PressReleasePage.aspx"),
            ("MeitY sitemap",      "https://www.meity.gov.in/sitemap.xml"),
            ("DPDP Act (indiacode)", "https://www.indiacode.nic.in/bitstream/123456789/20063/1/a2023-22.pdf"),
        ]:
            try:
                r = await c.get(url)
                txt = re.sub(r"(?s)<(script|style).*?</\1>", " ", r.text)
                txt = re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", txt)).strip()
                print(f"  {r.status_code} {len(r.text):>8}B raw {len(txt):>6} text  {label}")
            except Exception as e:
                print(f"  --- {type(e).__name__}: {str(e)[:60]}  {label}")
asyncio.run(main())
