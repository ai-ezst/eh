import os
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
IMAGE_BOT_TOKEN = os.getenv("IMAGE_BOT_TOKEN", "6975821458:AAE-zcgfkFh-h_LflZTPMZijHRmgqpfUvFM")
IMAGE_CHAT_ID = os.getenv("IMAGE_CHAT_ID", "-1002570901960")

EH_MEMBER_ID = os.getenv("EH_MEMBER_ID")
EH_PASS_HASH = os.getenv("EH_PASS_HASH")
TELEGRAPH_TOKEN = os.getenv("TELEGRAPH_TOKEN", "").strip()

STATE_FILE = "sent_galleries.json"
COSPLAY_URL = "https://e-hentai.org/?f_cats=959"
MAX_PAGES = 20
LIST_PAGES = 1

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


# ========= 通过 Telegram 中转上传 =========
async def upload_via_telegram(image_data: bytes) -> str | None:
    """发图到中转群，取 Telegram CDN 直链，供 Telegraph 使用"""
    bot = Bot(IMAGE_BOT_TOKEN)
    for attempt in range(3):
        try:
            msg = await bot.send_photo(chat_id=IMAGE_CHAT_ID, photo=image_data)
            file_id = msg.photo[-1].file_id
            file = await bot.get_file(file_id)
            url = f"https://api.telegram.org/file/bot{IMAGE_BOT_TOKEN}/{file.file_path}"
            # 验证 URL 是否可访问
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.head(url)
                if r.status_code == 200:
                    return url
            print(f"  ⚠️ Telegram CDN URL 不可访问 (重试 {attempt+1})")
        except Exception as e:
            print(f"  ❌ 中转上传异常 ({attempt+1}/3): {e}")
        if attempt < 2:
            await asyncio.sleep(5)
    return None


# ========= 状态 =========

def load_seen():
    if not os.path.exists(STATE_FILE):
        return set()
    try:
        data = json.load(open(STATE_FILE))
        if isinstance(data, list):
            return set(data)
        return set()
    except Exception:
        return set()

def save_seen(seen):
    json.dump(list(seen), open(STATE_FILE, "w"))


# ========= 标题清洗 =========

def clean_title(title):
    title = re.sub(r'\[.*?\]', '', title)
    title = re.sub(r'f:[^ ]+', '', title)
    title = re.sub(r'\s+', ' ', title)
    return title.strip()



# ========= 智能标签 =========

def generate_tags(title: str) -> str:
    stop_words = {
        "by","the","of","and","or","for","with","from","to","in","on","at",
        "is","are","a","an","photo","photos","set","collection","comic",
        "comiket","c","vol","volume","part","chapter","artist","pixiv",
        "twitter","fanbox","patreon","x","new","view","full","gallery"
    }
    words = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", title)
    tags=[]
    for w in words:
        if w.lower() in stop_words:
            continue
        if len(w)<=1 and not w.isdigit():
            continue
        if f"#{w}" not in tags:
            tags.append(f"#{w}")
    return " ".join(tags)


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


# ========= 抓列表多页图集 =========

async def get_galleries(client):
    galleries = []
    seen_urls = set()

    for page in range(LIST_PAGES):
        # e-hentai 列表翻页参数是 &page=N（从0开始）
        url = COSPLAY_URL if page == 0 else f"{COSPLAY_URL}&page={page}"
        print(f"  📄 列表第{page+1}页: {url}")

        try:
            r = await client.get(url)
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            print(f"  ⚠️ 列表第{page+1}页抓取失败: {e}")
            continue

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

        await asyncio.sleep(1)

    # 倒序：从最旧的（列表最底部）开始发
    galleries.reverse()
    print(f"  📋 共找到 {len(galleries)} 个图集（从最旧开始处理）")
    return galleries


# ========= 抓图集所有图片直链 =========

async def get_all_image_urls(client, base_url):
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

    async def fetch_img_url(url):
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

    results = await asyncio.gather(*[fetch_img_url(u) for u in all_pages])
    return [r for r in results if r]


# ========= 下载单张图片 =========

async def download_one(client, url) -> bytes | None:
    for attempt in range(3):
        try:
            r = await client.get(url, timeout=30)
            if r.status_code == 200 and 5000 < len(r.content) < 10 * 1024 * 1024:
                return r.content
            if r.status_code == 509:
                print(f"  ⚠️ E-Hentai 509 流量超限: {url}")
        except Exception as e:
            print(f"  ⚠️ 下载失败 (第{attempt+1}次): {e}")
            await asyncio.sleep(3)
    return None



# ========= 下载 → 中转上传 → 释放内存 =========
async def download_and_upload_all(client, urls) -> tuple[list[str], list[bytes]]:
    """
    逐张下载 → 通过 Telegram 中转上传 → 释放内存
    返回: tg_urls（Telegraph 可用直链）, 前20张原始数据用于选封面
    """
    tg_urls = []
    cover_candidates = []
    total = len(urls)

    for i, url in enumerate(urls):
        data = await download_one(client, url)
        if not data:
            print(f"  ⚠️ [{i+1}/{total}] 下载失败，跳过")
            continue

        if len(cover_candidates) < 20:
            cover_candidates.append(data)

        tg_url = await upload_via_telegram(data)
        if tg_url:
            tg_urls.append(tg_url)
            print(f"  ✅ [{i+1}/{total}] 中转上传成功")
        else:
            print(f"  ⚠️ [{i+1}/{total}] 中转上传失败，跳过")

        del data
        await asyncio.sleep(0.5)

    return tg_urls, cover_candidates


async def send_cover(bot, image: bytes, title: str, telegraph_url: str):
    tags = generate_tags(title)
    caption = (
        f"<b>{title}</b>\n"
        f"{tags}\n\n"
        f"<a href='{telegraph_url}'>👉 点击查看图集/View Photo Gallery</a>"
    )
    await bot.send_photo(
        chat_id=MAIN_CHANNEL,
        photo=image,
        caption=caption,
        parse_mode=ParseMode.HTML
    )


# ========= 主流程 =========

async def main():
    if not TELEGRAPH_TOKEN:
        print("❌ 未配置 TELEGRAPH_TOKEN，退出")
        return

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

            # 抓所有图片直链
            urls = await get_all_image_urls(client, g["url"])
            if not urls:
                print(f"  ⚠️ 未抓到图片 URL，跳过")
                seen.add(uid)
                save_seen(seen)
                continue

            print(f"  🔗 共获取 {len(urls)} 个图片 URL")

            # 逐张下载 → 中转上传 → 释放内存
            tg_urls, cover_candidates = await download_and_upload_all(client, urls)

            if not tg_urls:
                print(f"  ⚠️ 没有图片上传成功，跳过")
                seen.add(uid)
                save_seen(seen)
                continue

            print(f"  ✅ 成功上传 {len(tg_urls)}/{len(urls)} 张到中转")

            # 创建 Telegraph 页面
            telegraph_url = create_telegraph_page(g["title"], tg_urls)
            if not telegraph_url:
                print(f"  ⚠️ Telegraph 页面创建失败，跳过")
                seen.add(uid)
                save_seen(seen)
                continue

            # 从前20张里选封面
            if not cover_candidates:
                print(f"  ⚠️ 无封面候选，跳过")
                seen.add(uid)
                save_seen(seen)
                continue

            cover = pick_cover(cover_candidates)

            # 发封面到频道
            await send_cover(bot, cover, g["title"], telegraph_url)
            print(f"  ✅ 发送完成: {g['title']}")

            seen.add(uid)
            save_seen(seen)



asyncio.run(main())

