import httpx
import asyncio
from PIL import Image
from io import BytesIO

async def check_all(urls):
    broken = []
    async with httpx.AsyncClient(timeout=10) as c:
        for i, url in enumerate(urls):
            try:
                r = await c.get(url)
                if r.status_code != 200:
                    broken.append((i, url, f"HTTP {r.status_code}"))
                    continue
                img = Image.open(BytesIO(r.content))
                img.verify()
                if len(r.content) < 5000:
                    broken.append((i, url, f"too small {len(r.content)}"))
            except Exception as e:
                broken.append((i, url, str(e)))
            if i % 20 == 0:
                print(f"  checked {i}...")
    return broken

async def main():
    import re
    # Fetch page
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get("https://telegra.ph/Velvet-chann---Link-Ninja-08-06")
        urls = re.findall(r'src="(https://i\.ibb\.co/[^"]+)"', r.text)
    print(f"Total images: {len(urls)}")
    broken = await check_all(urls)
    print(f"\n=== Result: {len(broken)} broken ===")
    for i, url, reason in broken:
        print(f"[{i+1}] {reason} {url}")

asyncio.run(main())