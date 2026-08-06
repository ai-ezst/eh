import httpx
import asyncio
import time
from PIL import Image
from io import BytesIO

img = Image.new("RGB", (500, 700), color=(200, 100, 50))
buf = BytesIO()
img.save(buf, format="JPEG", quality=90)
data = buf.getvalue()
print(f"Image size: {len(data)/1024:.0f}KB")

async def main():
    async with httpx.AsyncClient(timeout=60) as c:
        t0 = time.time()
        try:
            r = await c.post("https://api.pixhost.to/images",
                data={"content_type": "1"},
                files={"img": ("x.jpg", data, "image/jpeg")})
            print(f"Upload: {time.time()-t0:.1f}s status={r.status_code}")
            if r.status_code == 200:
                j = r.json()
                print(f"URL: {j.get('show_url')}")
                t1 = time.time()
                url = j["show_url"].replace("https://pixhost.to/show/", "https://img2.pixhost.to/images/")
                r2 = await c.get(url)
                print(f"Verify: {time.time()-t1:.1f}s status={r2.status_code} size={len(r2.content)}")
        except Exception as e:
            print(f"FAIL after {time.time()-t0:.1f}s: {e}")

asyncio.run(main())