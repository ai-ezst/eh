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
IMAGE_CHANNEL = os.getenv("IMAGE_CHANNEL_ID")

EH_MEMBER_ID = os.getenv("EH_MEMBER_ID")
EH_PASS_HASH = os.getenv("EH_PASS_HASH")

STATE_FILE = "sent_galleries.json"
COSPLAY_URL = "https://e-hentai.org/?f_cats=959&sort=1&order=d"  # sort=1 按上传时间，order=d 降序（最新在前）
MAX_PAGES = 20  # 每个图集最多抓 20 页（约 800 张），避免触碰免费账号 960 张上限

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

# ========= 过滤图片尺寸 =========
def is_safe_for_telegram(data: bytes) -> bool:
    """检查图片宽高比是否在 Telegram 允许范围内（不超过 20:1）"""
    try:
        img = Image.open(BytesIO(data))
        w, h = img.size
        if w == 0 or h == 0:
            return False
        ratio = max(w, h) / min(w, h)
        return ratio <= 19  # 留一点余量，不要贴着 20:1 的上限
    except:
        return True  # 解析失败就放行，让 Telegram 自己决定

def filter_safe_images(images: list[bytes]) -> list[bytes]:
    """过滤掉比例太极端的图片，避免 photo_invalid_dimensions"""
    safe = [img for img in images if is_safe_for_telegram(img)]
    skipped = len(images) - len(safe)
    if skipped:
        print(f"  ⚠️ 过滤掉 {skipped} 张比例异常的图片")
    return safe

# ========= 选最佳封面 =========
def pick_cover(images: list[bytes]) -> bytes:
    """
    从图片列表中挑选最佳封面：
    1. 优先选竖图（高 > 宽）且宽高比在 1:1.2 ~ 1:3 之间（Telegram 安全范围）
    2. 若没有合适竖图，选所有图里文件最大的
    """
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

    # ?f_cats=959 是标准图库列表页，每个图集入口是带 /g/ 的链接
    # 兼容两种布局：.gl1t（大图模式）和 .gl2c（列表模式）
    seen_urls = set()
    for a in soup.select("a[href*='/g/']"):
        href = a.get("href", "")
        m = re.search(r"/g/(\d+)/([a-f0-9]+)/", href)
        if not m:
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)

        # 标题在 .glink 里，找最近的祖先容器
        title_node = a.select_one(".glink") or a.find(class_="glink")
        if not title_node:
            # 往上找
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
    return galleries  # 返回全部找到的图集，不限制数量

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

# ========= 过滤比例异常的图片 =========
def filter_valid_images(images: list[bytes]) -> list[bytes]:
    """过滤掉 Telegram 无法接受的极端比例图片（宽高比超过 20:1）"""
    valid = []
    for data in images:
        try:
            img = Image.open(BytesIO(data))
            w, h = img.size
            if w == 0 or h == 0:
                continue
            ratio = max(w, h) / min(w, h)
            if ratio > 20:
                print(f"  ⚠️ 跳过极端比例图片 {w}x{h}（比例 {ratio:.1f}:1）")
                continue
            valid.append(data)
        except Exception:
            valid.append(data)  # 无法解析的保留，让 Telegram 自己判断
    return valid

# ========= 发送图集 =========
async def send_groups(bot, images):
    from telegram.error import RetryAfter, TimedOut, BadRequest

    first_msg = None

    # ✅ 修复：发送前过滤比例异常图片，避免 photo_invalid_dimensions
    images = filter_valid_images(images)

    for i in range(0, len(images), 10):
        chunk = images[i:i+10]
        media = [InputMediaPhoto(media=img) for img in chunk]

        for attempt in range(5):
            try:
                msgs = await bot.send_media_group(chat_id=IMAGE_CHANNEL, media=media)
                if not first_msg:
                    first_msg = msgs[0]
                break
            except RetryAfter as e:
                wait = e.retry_after + 2
                print(f"  ⏳ Flood control，等待 {wait} 秒...")
                await asyncio.sleep(wait)
            except TimedOut:
                print(f"  ⏳ 超时，等待 10 秒后重试 (第{attempt+1}次)...")
                await asyncio.sleep(10)
            except BadRequest as e:
                # ✅ 修复：整组里有一张图尺寸不对就会失败，逐张发来找出问题图并跳过
                print(f"  ⚠️ BadRequest: {e}，尝试逐张发送跳过问题图...")
                for single in chunk:
                    try:
                        msgs = await bot.send_media_group(
                            chat_id=IMAGE_CHANNEL,
                            media=[InputMediaPhoto(media=single)]
                        )
                        if not first_msg:
                            first_msg = msgs[0]
                        await asyncio.sleep(2)
                    except Exception as se:
                        print(f"    跳过一张: {se}")
                break
            except Exception as e:
                print(f"  ⚠️ 发送失败 (第{attempt+1}次): {e}")
                await asyncio.sleep(5)

        await asyncio.sleep(5)  # ✅ 修复：每组之间等待加长到 5 秒，减少 flood

    return first_msg.message_id if first_msg else None

# ========= 发封面 =========
async def send_cover(bot, image, title, link):
    text = (
        f"<b>{title}</b>\n\n"
        f"<a href='{link}'>👉 查看全部图片 / View Full Gallery</a>"
    )

    await bot.send_photo(
        chat_id=MAIN_CHANNEL,
        photo=image,
        caption=text,
        parse_mode=ParseMode.HTML
    )

# ========= 主流程 =========
async def main():
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

            # ✅ 修复：过滤比例异常的图片，避免 photo_invalid_dimensions
            images = filter_safe_images(images)
            if not images:
                print(f"  ⚠️ 过滤后无图片，跳过")
                continue

            # ✅ 修复：智能选封面（竖图优先，文件最大优先）
            cover = pick_cover(images)
            rest = [img for img in images if img is not cover]

            msg_id = await send_groups(bot, rest)

            if msg_id:
                link = f"https://t.me/{IMAGE_CHANNEL.lstrip('@')}/{msg_id}"
                await send_cover(bot, cover, g["title"], link)
                print(f"  ✅ 发送完成: {g['title']}")

            seen.add(uid)
            save_seen(seen)

            await asyncio.sleep(10)

asyncio.run(main())
