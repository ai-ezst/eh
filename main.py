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


def upload_image_to_telegraph(data: bytes) -> str | None:
    """上传图片到 Telegraph（自动把 WebP 转 JPEG）"""
    try:
        if len(data) > 5 * 1024 * 1024:
            print(f"  ⚠️ 图片超过 5MB ({len(data)//1024}KB)，跳过")
            return None

        # 打开图片并判断格式
        img = Image.open(BytesIO(data))
        original_format = (img.format or "Unknown").upper()

        upload_data = data
        upload_mime = "image/jpeg"
        upload_filename = "image.jpg"

        # 如果是 WebP（E-Hentai 大量使用），强制转 JPEG
        if original_format == "WEBP":
            print(f"  🔄 WebP → JPEG 转换中... ", end="")
            buffer = BytesIO()
            img.convert("RGB").save(buffer, format="JPEG", quality=95, optimize=True)
            upload_data = buffer.getvalue()
            print(f"完成 ({len(upload_data)//1024}KB)")
        else:
            fmt = original_format.lower()
            ext = "jpg" if fmt in ("jpeg", "jpg") else fmt
            upload_filename = f"image.{ext}"
            upload_mime = f"image/{fmt}" if fmt != "jpeg" else "image/jpeg"

        print(f"  📤 正在上传... 格式: {upload_filename} ({len(upload_data)//1024}KB)")

        r = requests.post(
            "https://telegra.ph/upload",
            files={"file": (upload_filename, BytesIO(upload_data), upload_mime)},
            timeout=30,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://telegra.ph/",
                "Accept": "*/*",
            }
        )

        if r.status_code != 200:
            print(f"  ❌ HTTP {r.status_code}: {r.text[:200]}")
            return None

        try:
            result = r.json()
        except:
            result = None

        if isinstance(result, list) and result and isinstance(result[0], dict) and "src" in result[0]:
            url = "https://telegra.ph" + result[0]["src"]
            print(f"  ✅ 上传成功: {url}")
            return url
        else:
            error_msg = r.text[:300] if r.text else str(result)
            print(f"  ❌ Telegraph 拒绝: {error_msg}")
            return None

    except Exception as e:
        print(f"  ⚠️ 图片上传异常: {e}")
        return None


def create_telegraph_page(title, telegraph_image_urls):
    if not TELEGRAPH_TOKEN:
        print("  ⚠️ 无 Telegraph token，跳过")
        return None

    children = [{"tag": "img", "attrs": {"src": url}} for url in telegraph_image_urls]
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


# ========= 下载并上传 =========
async def download_and_upload_all(client, image_pairs: list[tuple[str, str]]) -> tuple[list[str], list[bytes]]:
    telegraph_urls = []
    cover_candidates = []

    total = len(image_pairs)
    for i, (url, referer) in enumerate(image_pairs):
        data = await download_one(client, url, referer=referer)
        if not data:
            print(f"  ⚠️ 第{i+1}/{total}张下载失败或无效，跳过")
            continue

        if len(cover_candidates) < 20:
            cover_candidates.append(data)

        tg_url = upload_image_to_telegraph(data)
        if tg_url:
            telegraph_urls.append(tg_url)
            print(f"  ⬆️ {i+1}/{total} 上传成功")
        else:
            print(f"  ⚠️ {i+1}/{total} 上传失败，跳过")

        del data
        await asyncio.sleep(0.5)

    return telegraph_urls, cover_candidates


# ========= 发封面 =========
async def send_cover(bot, image: bytes, title: str, telegraph_url: str):
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
        timeout=60,
        follow_redirects=True
    ) as client:

        galleries = await get_galleries(client)

        for g in galleries:
            uid = g["gid"] + "_" + g["token"]

            if uid in seen:
                print(f"⏭️ 跳过已发: {g['title']}")
                continue

            print(f"\n处理: {g['title']}")

            image_pairs = await get_all_image_urls(client, g["url"])
            if not image_pairs:
                print(f"  ⚠️ 未抓到任何图片 URL，跳过")
                seen.add(uid)
                save_seen(seen)
                continue

            print(f"  🔗 共获取 {len(image_pairs)} 个图片 URL")

            telegraph_urls, cover_candidates = await download_and_upload_all(client, image_pairs)

            if not telegraph_urls:
                print(f"  ⚠️ 没有图片上传成功，跳过")
                seen.add(uid)
                save_seen(seen)
                continue

            print(f"  ✅ 成功上传 {len(telegraph_urls)}/{len(image_pairs)} 张到 Telegraph")

            telegraph_url = create_telegraph_page(g["title"], telegraph_urls)
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

            print(f"\n✅ 本次运行完成，下次运行继续处理剩余图集")
            break

asyncio.run(main())
