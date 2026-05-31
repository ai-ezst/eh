import os
import sys
import json
import asyncio
import re
import httpx
import requests
from io import BytesIO
from PIL import Image
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.constants import ParseMode

BOT_TOKEN = os.getenv("BOT_TOKEN")
MAIN_CHANNEL = os.getenv("MAIN_CHANNEL_ID")

EH_MEMBER_ID = os.getenv("EH_MEMBER_ID")
EH_PASS_HASH = os.getenv("EH_PASS_HASH")

STATE_FILE = "sent_galleries.json"
COSPLAY_URL = "https://e-hentai.org/?f_cats=959"
MAX_PAGES = 20

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://e-hentai.org/",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

COOKIES = {
    "ipb_member_id": EH_MEMBER_ID,
    "ipb_pass_hash": EH_PASS_HASH
}

TELEGRAPH_TOKEN = None

# ========= Telegraph =========
def get_or_create_telegraph_token():
    global TELEGRAPH_TOKEN
    try:
        r = requests.post("https://api.telegra.ph/createAccount", json={
            "short_name": "EHBot",
            "author_name": "EH Cosplay Bot",
        }, timeout=15)
        if r.status_code == 200 and r.json().get("ok"):
            TELEGRAPH_TOKEN = r.json()["result"]["access_token"]
            print(f"✅ Telegraph token 创建成功")
        else:
            print(f"❌ Telegraph token 创建失败: {r.text}")
    except Exception as e:
        print(f"❌ Telegraph 初始化异常: {e}")

def create_telegraph_page(title, image_urls):
    """用图片 URL 直接创建 Telegraph 页面，无需下载上传"""
    if not TELEGRAPH_TOKEN:
        print("  ⚠️ 无 Telegraph token，跳过")
        return None

    children = [{"tag": "img", "attrs": {"src": url}} for url in image_urls]
    print(f"  📝 创建 Telegraph 页面，共 {len(children)} 张图片")

    try:
        payload = {
            "access_token": TELEGRAPH_TOKEN,
            "title": title[:256],
            "content": json.dumps(children, ensure_ascii=False),
            "return_content": "false",
        }
        r = requests.post(
            "https://api.telegra.ph/createPage",
            data=payload,
            timeout=15,
        )
        if r.status_code == 200 and r.json().get("ok"):
            url = r.json()["result"]["url"]
            print(f"  ✅ Telegraph 页面: {url}")
            return url
        else:
            print(f"  ❌ Telegraph 页面创建失败: {r.text[:120]}")
            return None
    except Exception as e:
        print(f"  ❌ Telegraph 异常: {e}")
        return None

# ========= 状态 =========
def load_seen():
    if not os.path.exists(STATE_FILE):
        return set()
    return set(json.load(open(STATE_FILE)))

def save_seen(seen):
    json.dump(list(seen), open(STATE_FILE, "w"))

# ========= 标题清洗 =========
def clean_title(title):
    title = re.sub(r'\[.*?\]', '', title)
    title = re.sub(r'f:[^ ]+', '', title)
    title = re.sub(r'\s+', ' ', title)
    return title.strip()


# ========= 选最佳封面 =========
def pick_cover(images: list[bytes]) -> bytes:
    portrait = []
    all_imgs = []

    for data in images:
        try:
            img = Image.open(BytesIO(data))
            w, h = img.size
            if w == 0 or h == 0:
                continue
            ratio = h / w
            size = len(data)
            all_imgs.append((size, data))
            if h > w and 1.2 <= ratio <= 3.0:
                portrait.append((size, data))
        except Exception as e:
            print(f"  ⚠️ 无法解析图片尺寸: {e}")
            continue

    if portrait:
        print(f"  📐 找到 {len(portrait)} 张合适竖图，选最大的作封面")
        return max(portrait, key=lambda x: x[0])[1]
    elif all_imgs:
        print(f"  ⚠️ 没有合适竖图，从所有图中选最大的作封面")
        return max(all_imgs, key=lambda x: x[0])[1]
    else:
        print(f"  ⚠️ 无法解析任何图片，使用第一张作封面")
        return images[0]

# ========= 抓首页 =========
async def get_galleries(client):
    r = await client.get(COSPLAY_URL)
    soup = BeautifulSoup(r.text, "html.parser")

    galleries = []
    seen_urls = set()

    for a in soup.select("a[href*='/g/']"):
        href = a.get("href", "")
        m = re.search(r"/g/(\d+)/([a-f0-9]+)/", href)
        if not m:
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)

        title_node = a.select_one(".glink") or a.find(class_="glink")
        if not title_node:
            parent = a.parent
            for _ in range(5):
                if not parent:
                    break
                title_node = parent.select_one(".glink")
                if title_node:
                    break
                parent = parent.parent

        if not title_node:
            continue

        title = clean_title(title_node.text)
        if not title:
            continue

        galleries.append({
            "gid": m.group(1),
            "token": m.group(2),
            "url": href,
            "title": title
        })

    print(f"  📋 共找到 {len(galleries)} 个图集")
    return galleries

# ========= 抓全部分页 =========
async def get_all_images(client, base_url):
    r = await client.get(base_url)
    soup = BeautifulSoup(r.text, "html.parser")

    max_page = 0
    for a in soup.select(".ptt a"):
        try:
            max_page = max(max_page, int(a.text))
        except:
            pass

    actual_pages = min(max_page + 1, MAX_PAGES)
    print(f"📄 页数: {max_page+1}，实际抓取: {actual_pages} 页")

    all_pages = []

    for i in range(actual_pages):
        url = f"{base_url}?p={i}"
        try:
            r = await client.get(url)
            soup = BeautifulSoup(r.text, "html.parser")
            thumbs = [a["href"] for a in soup.select("#gdt a")]
            all_pages.extend(thumbs)
            print(f"  第{i}页: {len(thumbs)}")
            await asyncio.sleep(1)
        except Exception as e:
            print(f"  ⚠️ 第{i}页抓取失败: {e}")
            continue

    print(f"👉 图片页总数: {len(all_pages)}")

    semaphore = asyncio.Semaphore(3)

    async def fetch(url):
        for attempt in range(3):
            try:
                async with semaphore:
                    r = await client.get(url)
                    soup = BeautifulSoup(r.text, "html.parser")
                    img = soup.select_one("#img")
                    if img:
                        return img["src"]
            except Exception as e:
                print(f"  ⚠️ 图片页抓取失败 (第{attempt+1}次): {e}")
                await asyncio.sleep(3)
        return None

    results = await asyncio.gather(*[fetch(u) for u in all_pages])
    return [r for r in results if r]

# ========= 下载 =========
async def download_images(client, urls):
    semaphore = asyncio.Semaphore(3)

    async def dl(url):
        for attempt in range(3):
            try:
                async with semaphore:
                    r = await client.get(url, timeout=30)
                    if r.status_code == 200 and 5000 < len(r.content) < 10*1024*1024:
                        return r.content
            except Exception as e:
                print(f"  ⚠️ 图片下载失败 (第{attempt+1}次): {e}")
                await asyncio.sleep(3)
        return None

    results = await asyncio.gather(*[dl(u) for u in urls])
    return [r for r in results if r]

# ========= 发封面到频道 =========
async def send_cover(bot, image, title, telegraph_url):
    caption = (
        f"<b>{title}</b>\n\n"
        f"<a href='{telegraph_url}'>👉 查看全部图片 / View Full Gallery</a>"
    )

    await bot.send_photo(
        chat_id=MAIN_CHANNEL,
        photo=image,
        caption=caption,
        parse_mode=ParseMode.HTML
    )

# ========= 主流程 =========
async def main():
    get_or_create_telegraph_token()

    bot = Bot(BOT_TOKEN)
    seen = load_seen()

    async with httpx.AsyncClient(
        headers=HEADERS,
        cookies=COOKIES,
        timeout=60
    ) as client:

        galleries = await get_galleries(client)

        for g in galleries:
            uid = g["gid"] + "_" + g["token"]

            if uid in seen:
                print(f"⏭️ 跳过已发: {g['title']}")
                continue

            print(f"\n处理: {g['title']}")

            urls = await get_all_images(client, g["url"])
            if not urls:
                print(f"  ⚠️ 未抓到图片，跳过")
                continue

            images = await download_images(client, urls)
            if not images:
                print(f"  ⚠️ 图片下载全部失败，跳过")
                continue

            print(f"  ✅ 成功下载 {len(images)} 张图片")

            # 创建 Telegraph 页面
            telegraph_url = create_telegraph_page(g["title"], urls)

            # 选封面
            cover = pick_cover(images)

            # 封面发到频道，附带 Telegraph 链接
            if telegraph_url:
                await send_cover(bot, cover, g["title"], telegraph_url)
                print(f"  ✅ 发送完成: {g['title']}")
            else:
                print(f"  ⚠️ Telegraph 页面失败，跳过发送")

            seen.add(uid)
            save_seen(seen)

            await asyncio.sleep(10)

asyncio.run(main())
