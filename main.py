import os
import json
import asyncio
import re
import httpx
from io import BytesIO
from PIL import Image
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.constants import ParseMode
from proxy_pool import pool, PROXY_SOURCES

BOT_TOKEN = os.getenv("BOT_TOKEN")
MAIN_CHANNEL = os.getenv("MAIN_CHANNEL_ID")

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


# ========= pixhost.to 上传 =========
UPLOAD_DELAY = 1       # 每张图片之间间隔 1 秒
RETRY_DELAY_BASE = 5   # 重试递增秒数
RETRY_DELAY_MAX = 15   # 重试最长秒数
MAX_RETRIES = 3        # 最多重试次数（每张图最多尝试4次）

class RateLimitError(Exception):
    """上传失败/限流"""

async def check_imgbb_url(url: str) -> bool:
    """下载 URL 并用 PIL 验证是否是真实图片"""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(url)
            if r.status_code == 200 and len(r.content) > 5000:
                return is_valid_image(r.content)
    except Exception:
        pass
    return False

async def upload_to_pixhost(image_data: bytes) -> str | None:
    """pixhost.to 上传（支持成人内容）+ 验证 + 递增退避重试"""
    ext = "jpg"
    if image_data[:4] == b"\x89PNG":
        ext = "png"
    elif image_data[:4] == b"RIFF":
        ext = "webp"

    for attempt in range(MAX_RETRIES + 1):  # 总共尝试 4 次
        url = None
        try:
            async with httpx.AsyncClient(timeout=30) as pix_client:
                r = await pix_client.post(
                    "https://api.pixhost.to/images",
                    data={"content_type": "1"},  # 1 = 成人内容
                    files={"img": (f"image.{ext}", image_data, f"image/{ext}")},
                )
            if r.status_code == 200:
                resp = r.json()
                if resp.get("show_url"):
                    # 从 show_url 构造直链
                    url = resp["show_url"].replace(
                        "https://pixhost.to/show/", "https://img2.pixhost.to/images/"
                    )
        except Exception as e:
            print(f"  ⚠️ pixhost 异常: {e}")

        # 验证上传结果
        if url and await check_imgbb_url(url):
            return url

        # 还有重试机会
        if attempt < MAX_RETRIES:
            delay = min(RETRY_DELAY_BASE * (attempt + 1), RETRY_DELAY_MAX)
            print(f"  🔄 重试 {attempt+1}/{MAX_RETRIES}，等待 {delay}s...")
            await asyncio.sleep(delay)

    return None



async def create_telegraph_page(client: httpx.AsyncClient, title: str, image_urls: list[str]) -> str | None:
    """用图片直链创建 Telegraph 页面"""
    if not TELEGRAPH_TOKEN:
        print("  ⚠️ 未配置 TELEGRAPH_TOKEN")
        return None
    if not image_urls:
        return None

    content = [{"tag": "img", "attrs": {"src": url}} for url in image_urls]
    print(f"  📝 创建 Telegraph 页面，共 {len(content)} 张图片")

    content.append({
        "tag": "img",
        "attrs": {"src": "https://i.ibb.co/bYwH4Y2/Chat-GPT-Image-2026-7-2-23-55-12.png"}
    })
    content.append({
        "tag": "p",
        "children": [
            {"tag": "a", "attrs": {"href": "http://t.me/fljtkwbot"}, "children": ["🔍 点击搜索更多图集、Cos、福利姬… 懂的都懂 👀"]}
        ]
    })

    try:
        r = await client.post(
            "https://api.telegra.ph/createPage",
            json={
                "access_token": TELEGRAPH_TOKEN,
                "title": title[:256],
                "author_name": "EH Cosplay Bot",
                "content": content,
                "return_content": False,
            },
            timeout=30,
        )
        if r.status_code == 200 and r.json().get("ok"):
            url = r.json()["result"]["url"]
            print(f"  ✅ Telegraph 页面: {url}")
            return url
        print(f"  ❌ Telegraph 页面创建失败: {r.text[:120]}")
        return None
    except Exception as e:
        print(f"  ❌ Telegraph 异常: {e}")
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

def is_valid_image(data: bytes) -> bool:
    """验证图片文件是否完整可解码"""
    try:
        img = Image.open(BytesIO(data))
        img.verify()
        return True
    except Exception:
        return False

async def download_one(client, url) -> bytes | None:
    for attempt in range(3):
        try:
            r = await client.get(url, timeout=30)
            if r.status_code == 200 and 5000 < len(r.content) < 10 * 1024 * 1024:
                if is_valid_image(r.content):
                    return r.content
                print(f"  ⚠️ 图片损坏，重试")
                continue
            if r.status_code == 509:
                print(f"  ⚠️ E-Hentai 509 流量超限，等待 60s...")
                await asyncio.sleep(60)
                continue
        except Exception as e:
            print(f"  ⚠️ 下载失败 (第{attempt+1}次): {e}")
            await asyncio.sleep(3)
    return None


# ========= 下载 → 上传 → 释放内存 =========
async def download_and_upload_all(client, urls) -> tuple[list[str], list[bytes], bool]:
    """
    逐张下载 → pixhost 上传 → 释放内存
    返回: urls, cover_candidates, rate_limited
    """
    img_urls = []
    cover_candidates = []
    total = len(urls)

    for i, url in enumerate(urls):

        data = await download_one(client, url)
        if not data:
            print(f"  ⚠️ [{i+1}/{total}] 下载失败，跳过")
            continue

        if len(cover_candidates) < 20:
            cover_candidates.append(data)

        try:
            img_url = await upload_to_pixhost(data)
            if img_url:
                img_urls.append(img_url)
                print(f"  ✅ [{i+1}/{total}] pixhost 上传成功")
            else:
                print(f"  ⚠️ [{i+1}/{total}] pixhost 上传失败，跳过")
        except RateLimitError:
            print(f"  ⛔ [{i+1}/{total}] 限流，已上传 {len(img_urls)} 张，剩余留着下次发")
            del data
            return img_urls, cover_candidates, True

        del data
        await asyncio.sleep(UPLOAD_DELAY)

    return img_urls, cover_candidates, False


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

    # 初始化代理池
    await pool.refresh(force=True)

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

            urls = await get_all_image_urls(client, g["url"])
            if not urls:
                print(f"  ⚠️ 未抓到图片 URL，跳过")
                seen.add(uid)
                save_seen(seen)
                continue

            print(f"  🔗 共获取 {len(urls)} 个图片 URL")

            img_urls, cover_candidates, rate_limited = await download_and_upload_all(client, urls)

            if not img_urls:
                print(f"  ⚠️ 没有图片上传成功，跳过")
                seen.add(uid)
                save_seen(seen)
                continue

            print(f"  ✅ 成功上传 {len(img_urls)}/{len(urls)} 张到 pixhost")

            telegraph_url = await create_telegraph_page(client, g["title"], img_urls)
            if not telegraph_url:
                print(f"  ⚠️ Telegraph 页面创建失败，跳过")
                seen.add(uid)
                save_seen(seen)
                continue

            if not cover_candidates:
                print(f"  ⚠️ 无封面候选，跳过")
                seen.add(uid)
                save_seen(seen)
                continue

            cover = pick_cover(cover_candidates)

            await send_cover(bot, cover, g["title"], telegraph_url)
            print(f"  ✅ 发送完成: {g['title']}")

            seen.add(uid)
            save_seen(seen)

            if rate_limited:
                print(f"\n⛔ 上传失败次数过多，剩余图集留着下次发")
                save_seen(seen)
                return


asyncio.run(main())