import asyncio, httpx, re, json
UA = {"User-Agent": "Consiva-RegulatoryWatch/1.0 (+compliance monitoring)"}

async def main():
    async with httpx.AsyncClient(follow_redirects=True, timeout=25, headers=UA) as c:
        shell = (await c.get("https://www.meity.gov.in/")).text
        m = re.search(r'"buildId":"([^"]+)"', shell)
        print("buildId:", m.group(1) if m else "NOT FOUND")
        nd = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', shell, re.S)
        if nd:
            data = json.loads(nd.group(1))
            print("__NEXT_DATA__ keys:", list(data.keys()))
            print("page:", data.get("page"), "| props size:", len(json.dumps(data.get("props", {}))))
        if not m:
            return
        bid = m.group(1)
        for route in ["/whats-new", "/documents", "/press-release", "/index",
                      "/static-content/data-protection-framework"]:
            url = f"https://www.meity.gov.in/_next/data/{bid}{route}.json"
            try:
                r = await c.get(url)
                body = r.text
                print(f"  {r.status_code} {len(body):>8}B  {r.headers.get('content-type','?')[:24]}  {route}")
                if r.status_code == 200 and len(body) > 500:
                    print(f"       sample: {body[:180]}")
            except Exception as e:
                print(f"  --- {type(e).__name__} {route}")
asyncio.run(main())
