import os
import json
import asyncio
import re
import httpx
from io import BytesIO
from PIL import Image
from bs4 import BeautifulSoup
from telegram import Bot, InputMediaPhoto
from telegram.constants import ParseMode

BOT_TOKEN = os.getenv("BOT_TOKEN")
MAIN_CHANNEL = os.getenv("MAIN_CHANNEL_ID")

EH_MEMBER_ID = os.getenv("EH_MEMBER_ID")
EH_PASS_HASH = os.getenv("EH_PASS_HASH")

STATE_FILE = "sent_galleries.json"
COSPLAY_URL = "https://e-hentai.org/?f_cats=959"
MAX_PAGES = 20

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Referer": "https://e-hentai.org/",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

COOKIES = {
    "ipb_member_id": EH_MEMBER_ID,
    "ipb_pass_hash": EH_PASS_HASH
}

# ========= 新增：获取 Torrent 下载链接 =========
async def get_torrent_url(client, base_url):
    """从画廊页面抓取 Torrent 下载链接"""
    try:
        r = await client.get(base_url)
        soup = BeautifulSoup(r.text, "html.parser")

        # 优先匹配 ehtracker.org 的种子链接（最准确）
        for a in soup.select('a[href*="ehtracker.org/get/"]'):
            href = a.get("href", "")
            if href.startswith("http"):
                print(f"  📥 找到 Torrent: {href}")
                return href

        # 备用：查找文字包含 Torrent Download 的链接
        for a in soup.find_all("a"):
            text = a.get_text(strip=True).lower()
            if "torrent" in text:
                href = a.get("href", "")
                if href and "download" in text:
                    if not href.startswith("http"):
                        href = "https://e-hentai.org" + href
                    print(f"  📥 找到 Torrent: {href}")
                    return href

        print("  ⚠️ 未找到 Torrent 链接（该画廊可能没有种子）")
        return None
    except Exception as e:
        print(f"  ⚠️ 获取 Torrent 失败: {e}")
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


# ========= 抓首页图集列表 =========
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
            print(f"  第{i}页: {len(thumbs)} 张图片页")
            await asyncio.sleep(1)
        except Exception as e:
            print(f"  ⚠️ 第{i}页抓取失败: {e}")
            continue

    print(f"👉 图片页总数: {len(all_pages)}")

    semaphore = asyncio.Semaphore(3)

    async def fetch_img_url(page_url):
        for attempt in range(3):
            try:
                async with semaphore:
                    r = await client.get(page_url)
                    soup = BeautifulSoup(r.text, "html.parser")
                    img = soup.select_one("#img")
                    if img and img.get("src"):
                        return img["src"], page_url
            except Exception as e:
                print(f"  ⚠️ 图片页抓取失败 (第{attempt+1}次): {e}")
                await asyncio.sleep(3)
        return None, page_url

    results = await asyncio.gather(*[fetch_img_url(u) for u in all_pages])
    valid_pairs = [r for r in results if r[0]]
    print(f"✅ 成功获取 {len(valid_pairs)} 个图片直链")
    return valid_pairs


# ========= 下载单张图片 =========
async def download_one(client, url: str, referer: str | None = None) -> bytes | None:
    for attempt in range(3):
        try:
            if referer:
                r = await client.get(url, headers={"Referer": referer}, timeout=30)
            else:
                r = await client.get(url, timeout=30)

            if r.status_code != 200:
                continue

            data = r.content
            if not (5000 < len(data) < 10 * 1024 * 1024):
                continue

            try:
                img = Image.open(BytesIO(data))
                img.verify()
                fmt = img.format or "Unknown"
                print(f"  ✅ 第{attempt+1}次下载成功，有效图片 ({len(data)//1024}KB, {fmt})")
                return data
            except Exception as e:
                print(f"  ❌ 第{attempt+1}次 下载的不是有效图片: {e}")
                continue

        except Exception as e:
            print(f"  ⚠️ 下载失败 (第{attempt+1}次): {e}")
            await asyncio.sleep(3)
    return None


# ========= 下载所有图片 =========
async def download_all_images(client, image_pairs: list[tuple[str, str]]) -> list[bytes]:
    images = []
    total = len(image_pairs)
    for i, (url, referer) in enumerate(image_pairs):
        data = await download_one(client, url, referer=referer)
        if data:
            images.append(data)
            print(f"  📥 {i+1}/{total} 下载完成")
        else:
            print(f"  ⚠️ {i+1}/{total} 下载失败，跳过")
        await asyncio.sleep(0.3)
    return images


# ========= 发送媒体组（每10张一组） =========
async def send_media_groups(bot, chat_id: int, images: list[bytes], title: str):
    if not images:
        return
    media_groups = [images[i:i+10] for i in range(0, len(images), 10)]
    for idx, group in enumerate(media_groups):
        media = [InputMediaPhoto(BytesIO(img)) for img in group]
        caption = f"{title}（{idx+1}/{len(media_groups)}）" if idx == 0 else None
        await bot.send_media_group(
            chat_id=chat_id,
            media=media,
            caption=caption,
            parse_mode=ParseMode.HTML
        )
        print(f"  ✅ 已发送第 {idx+1}/{len(media_groups)} 组（{len(group)} 张）")
        await asyncio.sleep(1)


# ========= 发封面 + 图集 + Torrent 链接 =========
async def send_gallery(bot, cover: bytes, title: str, all_images: list[bytes], torrent_url: str | None):
    caption = (
        f"<b>{title}</b>\n\n"
        f"📸 共 {len(all_images)} 张图片\n"
        f"👇 向下滑动查看完整图集"
    )
    if torrent_url:
        caption += f"\n\n📥 <a href='{torrent_url}'>Torrent Download（完整原图包）</a>"

    await bot.send_photo(
        chat_id=MAIN_CHANNEL,
        photo=cover,
        caption=caption,
        parse_mode=ParseMode.HTML
    )

    await send_media_groups(bot, MAIN_CHANNEL, all_images, title)
    print(f"  ✅ 完整图集 + Torrent 链接发送完成！")


# ========= 主流程 =========
async def main():
    bot = Bot(BOT_TOKEN)
    seen = load_seen()

    async with httpx.AsyncClient(
        headers=HEADERS,
        cookies=COOKIES,
        timeout=60,
        follow_redirects=True
    ) as client:

        galleries = await get_galleries(client)

        for g in galleries:
            uid = g["gid"] + "_" + g["token"]

            if uid in seen:
                print(f"⏭️ 跳过已发: {g['title']}")
                continue

            print(f"\n🚀 开始处理: {g['title']}")

            # 1. 抓图片直链
            image_pairs = await get_all_image_urls(client, g["url"])
            if not image_pairs:
                print(f"  ⚠️ 未抓到图片 URL，跳过")
                seen.add(uid)
                save_seen(seen)
                continue

            # 2. 抓 Torrent 链接
            torrent_url = await get_torrent_url(client, g["url"])

            # 3. 下载所有图片
            all_images = await download_all_images(client, image_pairs)
            if not all_images:
                print(f"  ⚠️ 没有图片下载成功，跳过")
                seen.add(uid)
                save_seen(seen)
                continue

            # 4. 选封面并发送
            cover = pick_cover(all_images[:20])
            await send_gallery(bot, cover, g["title"], all_images, torrent_url)

            seen.add(uid)
            save_seen(seen)

            print(f"\n✅ 本次运行完成（已处理 1 个图集），下次运行继续剩余图集")
            break  # 每次只处理 1 个，防止超时

asyncio.run(main())
