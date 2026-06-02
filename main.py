import os
import json
import asyncio
import re
import logging
import httpx
from io import BytesIO
from PIL import Image
from bs4 import BeautifulSoup
from telegram import Bot, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, BadRequest

# ====================== 配置 ======================
BOT_TOKEN = os.getenv("BOT_TOKEN")
MAIN_CHANNEL = os.getenv("MAIN_CHANNEL_ID")      # 主频道（发封面 + 链接）
IMAGE_CHANNEL = os.getenv("IMAGE_CHANNEL_ID")    # 图组频道（发全部图片）

EH_MEMBER_ID = os.getenv("EH_MEMBER_ID")
EH_PASS_HASH = os.getenv("EH_PASS_HASH")

STATE_FILE = "sent_galleries.json"
COSPLAY_URL = "https://e-hentai.org/?f_cats=959&sort=1&order=d"  # 最新 cosplay 图集

MAX_PAGES_PER_GALLERY = 20      # 每个图集最多抓 20 页（约 800 张）
MAX_GALLERY_PAGES = 8           # 首页最多翻 8 页（遇到已发就立刻停止）
MAX_ASPECT_RATIO = 19.5         # Telegram 限制 20:1，留一点余量
DOWNLOAD_SEMAPHORE = 3
PAGE_SEMAPHORE = 3

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://e-hentai.org/",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

COOKIES = {
    "ipb_member_id": EH_MEMBER_ID,
    "ipb_pass_hash": EH_PASS_HASH,
}

# ====================== 日志 ======================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ====================== 状态管理 ======================
def load_seen() -> set:
    if not os.path.exists(STATE_FILE):
        return set()
    try:
        return set(json.load(open(STATE_FILE, encoding="utf-8")))
    except Exception as e:
        logger.error(f"加载 seen 状态失败: {e}")
        return set()


def save_seen(seen: set):
    try:
        json.dump(list(seen), open(STATE_FILE, "w", encoding="utf-8"))
    except Exception as e:
        logger.error(f"保存 seen 状态失败: {e}")


# ====================== 标题清洗 ======================
def clean_title(title: str) -> str:
    title = re.sub(r'\[.*?\]', '', title)
    title = re.sub(r'f:[^ ]+', '', title)
    title = re.sub(r'\s+', ' ', title)
    return title.strip()


# ====================== 图片尺寸过滤 ======================
def is_valid_image(data: bytes) -> bool:
    """检查图片宽高比是否 ≤ 19.5:1（Telegram 安全范围）"""
    try:
        img = Image.open(BytesIO(data))
        w, h = img.size
        if w == 0 or h == 0:
            return False
        ratio = max(w, h) / min(w, h)
        return ratio <= MAX_ASPECT_RATIO
    except Exception:
        return True  # 解析失败就放行，让 Telegram 自行处理


def filter_valid_images(images: list[bytes]) -> list[bytes]:
    """过滤极端比例图片"""
    valid = [img for img in images if is_valid_image(img)]
    skipped = len(images) - len(valid)
    if skipped:
        logger.warning(f"  ⚠️ 过滤掉 {skipped} 张极端比例图片")
    return valid


# ====================== 最佳封面选择 ======================
def pick_cover(images: list[bytes]) -> bytes:
    """优先选竖图（高>宽 且 比例 1.2~3.0），没有则选文件最大的"""
    portrait = []
    all_imgs = []

    for data in images:
        try:
            img = Image.open(BytesIO(data))
            w, h = img.size
            if w == 0 or h == 0:
                continue
            size = len(data)
            all_imgs.append((size, data))

            ratio = h / w
            if h > w and 1.2 <= ratio <= 3.0:
                portrait.append((size, data))
        except Exception:
            continue

    if portrait:
        logger.info(f"  📐 找到 {len(portrait)} 张合适竖图，选最大的作封面")
        return max(portrait, key=lambda x: x[0])[1]
    if all_imgs:
        logger.info(f"  ⚠️ 没有合适竖图，从所有图中选最大的作封面")
        return max(all_imgs, key=lambda x: x[0])[1]
    logger.warning(f"  ⚠️ 无法解析任何图片，使用第一张作封面")
    return images[0]


# ====================== 抓取新图集（分页 + 提前停止） ======================
async def get_new_galleries(client: httpx.AsyncClient, seen: set) -> list[dict]:
    galleries = []
    seen_urls = set()

    for page in range(MAX_GALLERY_PAGES):
        url = f"{COSPLAY_URL}&p={page}" if page > 0 else COSPLAY_URL
        try:
            r = await client.get(url, timeout=30)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")   # 可换成 "lxml" 更快

            new_in_page = False
            for a in soup.select("a[href*='/g/']"):
                href = a.get("href", "")
                m = re.search(r"/g/(\d+)/([a-f0-9]+)/", href)
                if not m:
                    continue
                uid = m.group(1) + "_" + m.group(2)

                if uid in seen:
                    logger.info(f"  ✅ 遇到已发送图集，停止翻页")
                    return galleries  # 直接停止（因为是最新排序）

                if href in seen_urls:
                    continue
                seen_urls.add(href)

                # 提取标题
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
                    "title": title,
                    "uid": uid,
                })
                new_in_page = True

            logger.info(f"  📋 第 {page + 1} 页找到 {len(galleries) - len(galleries) + (len([g for g in galleries if g['uid'] not in seen]))} 个新图集")  # 简化日志

            if not new_in_page:
                break

            await asyncio.sleep(1.5)
        except Exception as e:
            logger.error(f"  ⚠️ 抓取第 {page + 1} 页失败: {e}")
            continue

    logger.info(f"  📋 共发现 {len(galleries)} 个新图集")
    return galleries


# ====================== 抓取单图集全部图片 ======================
async def get_all_images(client: httpx.AsyncClient, base_url: str) -> list[str]:
    try:
        r = await client.get(base_url, timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")

        # 计算最大页数
        max_page = 0
        for a in soup.select(".ptt a"):
            try:
                max_page = max(max_page, int(a.text))
            except:
                pass

        actual_pages = min(max_page + 1, MAX_PAGES_PER_GALLERY)
        logger.info(f"📄 图集共 {max_page + 1} 页，本次抓取 {actual_pages} 页")

        thumb_links = []

        semaphore = asyncio.Semaphore(PAGE_SEMAPHORE)
        async def fetch_page(p: int):
            url = f"{base_url}?p={p}" if p > 0 else base_url
            async with semaphore:
                try:
                    r = await client.get(url, timeout=30)
                    soup = BeautifulSoup(r.text, "html.parser")
                    thumbs = [a["href"] for a in soup.select("#gdt a")]
                    return thumbs
                except Exception as e:
                    logger.warning(f"  第 {p} 页抓取失败: {e}")
                    return []

        results = await asyncio.gather(*[fetch_page(i) for i in range(actual_pages)])
        for thumbs in results:
            thumb_links.extend(thumbs)

        logger.info(f"👉 共找到 {len(thumb_links)} 张图片页")

        # 提取真实图片链接
        semaphore = asyncio.Semaphore(PAGE_SEMAPHORE)
        async def fetch_image_url(url: str):
            async with semaphore:
                for attempt in range(3):
                    try:
                        r = await client.get(url, timeout=30)
                        soup = BeautifulSoup(r.text, "html.parser")
                        img = soup.select_one("#img")
                        if img and img.get("src"):
                            return img["src"]
                    except Exception:
                        await asyncio.sleep(2)
            return None

        image_urls = await asyncio.gather(*[fetch_image_url(u) for u in thumb_links])
        return [u for u in image_urls if u]

    except Exception as e:
        logger.error(f"获取图集图片失败: {e}")
        return []


# ====================== 下载图片 ======================
async def download_images(client: httpx.AsyncClient, urls: list[str]) -> list[bytes]:
    semaphore = asyncio.Semaphore(DOWNLOAD_SEMAPHORE)

    async def dl(url: str):
        for attempt in range(3):
            try:
                async with semaphore:
                    r = await client.get(url, timeout=40)
                    if r.status_code == 200 and 5000 < len(r.content) < 10 * 1024 * 1024:
                        return r.content
            except Exception as e:
                logger.debug(f"  下载失败 (第{attempt+1}次): {e}")
                await asyncio.sleep(3)
        return None

    results = await asyncio.gather(*[dl(u) for u in urls])
    images = [r for r in results if r]
    logger.info(f"  ✅ 成功下载 {len(images)} 张图片")
    return images


# ====================== 安全发送（异步包装） ======================
async def send_media_group_safe(bot: Bot, chat_id: str, media: list[InputMediaPhoto]) -> list | None:
    """发送 media group，自动重试 + flood control"""
    for attempt in range(6):
        try:
            return await asyncio.to_thread(
                bot.send_media_group,
                chat_id=chat_id,
                media=media
            )
        except RetryAfter as e:
            wait = e.retry_after + 2
            logger.warning(f"  ⏳ Flood control，等待 {wait} 秒...")
            await asyncio.sleep(wait)
        except TimedOut:
            logger.warning(f"  ⏳ 超时，重试 (第{attempt+1}次)")
            await asyncio.sleep(10)
        except BadRequest as e:
            logger.warning(f"  ⚠️ BadRequest: {e}，尝试逐张发送跳过问题图...")
            for single in media:
                try:
                    await asyncio.to_thread(
                        bot.send_media_group,
                        chat_id=chat_id,
                        media=[single]
                    )
                    await asyncio.sleep(2)
                except Exception as se:
                    logger.warning(f"    跳过单张: {se}")
            return None
        except Exception as e:
            logger.error(f"  ⚠️ 发送失败 (第{attempt+1}次): {e}")
            await asyncio.sleep(5)
    return None


async def send_photo_safe(bot: Bot, chat_id: str, photo: bytes, caption: str = None):
    """发送单张带文字的图片"""
    for attempt in range(5):
        try:
            await asyncio.to_thread(
                bot.send_photo,
                chat_id=chat_id,
                photo=photo,
                caption=caption,
                parse_mode=ParseMode.HTML
            )
            return True
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 2)
        except Exception as e:
            logger.error(f"  ⚠️ 封面发送失败 (第{attempt+1}次): {e}")
            await asyncio.sleep(4)
    return False


# ====================== 发送图组（除封面外） ======================
async def send_groups(bot: Bot, images: list[bytes]):
    if not images:
        return
    images = filter_valid_images(images)   # 最终过滤
    if not images:
        return

    for i in range(0, len(images), 10):
        chunk = images[i:i + 10]
        media = [InputMediaPhoto(media=img) for img in chunk]
        await send_media_group_safe(bot, IMAGE_CHANNEL, media)
        await asyncio.sleep(5)   # 组间间隔防 flood


# ====================== 主流程 ======================
async def main():
    bot = Bot(BOT_TOKEN)
    seen = load_seen()

    async with httpx.AsyncClient(
        headers=HEADERS,
        cookies=COOKIES,
        timeout=60,
        follow_redirects=True,
    ) as client:

        galleries = await get_new_galleries(client, seen)

        for g in galleries:
            uid = g["uid"]

            if uid in seen:
                continue

            logger.info(f"\n🚀 开始处理: {g['title']}")

            try:
                # 1. 抓图片链接
                urls = await get_all_images(client, g["url"])
                if not urls:
                    logger.warning("  ⚠️ 未抓到任何图片，跳过")
                    continue

                # 2. 下载
                images = await download_images(client, urls)
                if not images:
                    logger.warning("  ⚠️ 下载全部失败，跳过")
                    continue

                # 3. 过滤
                images = filter_valid_images(images)
                if not images:
                    logger.warning("  ⚠️ 过滤后无有效图片，跳过")
                    continue

                # 4. 选封面
                cover = pick_cover(images)
                rest = [img for img in images if img is not cover]

                # 5. 先发封面到图组频道（单独一条）
                cover_msg = await send_media_group_safe(
                    bot, IMAGE_CHANNEL, [InputMediaPhoto(media=cover)]
                )
                if not cover_msg:
                    logger.warning("  ⚠️ 封面发送失败，跳过该图集")
                    continue

                logger.info("  📸 封面已发送到图组频道")

                # 6. 发剩余图片
                await send_groups(bot, rest)

                # 7. 构造私聊频道链接并发送封面到主频道
                msg_id = cover_msg[0].message_id
                if str(IMAGE_CHANNEL).startswith("-100"):
                    channel_id = str(IMAGE_CHANNEL)[4:]
                    link = f"https://t.me/c/{channel_id}/{msg_id}"
                else:
                    link = f"https://t.me/{IMAGE_CHANNEL.lstrip('@')}/{msg_id}"

                text = (
                    f"<b>{g['title']}</b>\n\n"
                    f"<a href='{link}'>👉 查看全部图片 / View Full Gallery</a>"
                )
                await send_photo_safe(bot, MAIN_CHANNEL, cover, text)

                # 8. 记录已发
                seen.add(uid)
                save_seen(seen)
                logger.info(f"  ✅ 图集处理完成: {g['title']}")

                await asyncio.sleep(10)   # 图集间间隔

            except Exception as e:
                logger.error(f"  ❌ 处理图集时发生异常: {e}", exc_info=True)
                continue

    logger.info("🎉 本次任务全部完成")


if __name__ == "__main__":
    asyncio.run(main())
