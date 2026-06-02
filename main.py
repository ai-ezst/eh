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



async def upload_image_to_telegraph(client: httpx.AsyncClient, data: bytes) -> str | None:
    try:
        if len(data) > 5 * 1024 * 1024:
            print(f"  ⚠️ 图片超过 5MB ({len(data)//1024}KB)，跳过")
            return None

        # 准备文件
        files = {"file": ("image.jpg", BytesIO(data), "image/jpeg")}
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://telegra.ph/",
        }
        r = await client.post(
            "https://telegra.ph/upload",
            files=files,
            headers=headers,
            timeout=30,
            follow_redirects=True  # 允许重定向
        )
        print(f"  📡 状态码: {r.status_code}")
        print(f"  📡 响应头: {dict(r.headers)}")
        print(f"  📡 响应内容: {r.text[:300]}")

        if r.status_code == 200:
            try:
                result = r.json()
                if isinstance(result, list) and len(result) > 0:
                    src = result[0].get("src")
                    if src:
                        return "https://telegra.ph" + src
                print(f"  ⚠️ 返回结构异常: {result}")
            except Exception as e:
                print(f"  ⚠️ JSON 解析失败: {e}，响应原文: {r.text[:200]}")
        else:
            print(f"  ⚠️ 上传失败，状态码 {r.status_code}")
            if "cloudflare" in r.text.lower():
                print("  ⚠️ 可能被 Cloudflare 反爬拦截")
            elif "403" in str(r.status_code):
                print("  ⚠️ 403 Forbidden，需要模拟真实浏览器指纹")
        return None
    except Exception as e:
        print(f"  ⚠️ 上传异常: {e}")
        return None


def create_telegraph_page(title, telegraph_image_urls):
    """用已上传到 Telegraph 的图片 URL 创建页面"""
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
        except Exception as e:
            print(f"  ⚠️ 下载失败 (第{attempt+1}次): {e}")
            await asyncio.sleep(3)
    return None


# ========= 下载并上传所有图片到 Telegraph =========
async def download_and_upload_all(client, urls) -> tuple[list[str], list[bytes]]:
    """
    逐张下载 → 上传 Telegraph → 释放内存
    返回：(telegraph_urls, 前20张的原始数据用于选封面)
    """
    telegraph_urls = []
    cover_candidates = []  # 只保留前20张原始数据用于选封面

    total = len(urls)
    for i, url in enumerate(urls):
        data = await download_one(client, url)
        if not data:
            print(f"  ⚠️ 第{i+1}/{total}张下载失败，跳过")
            continue

        # 保留前20张原始数据用于选封面
        if len(cover_candidates) < 20:
            cover_candidates.append(data)

        # 上传到 Telegraph
        tg_url = upload_image_to_telegraph(data)
        if tg_url:
            telegraph_urls.append(tg_url)
            print(f"  ⬆️ {i+1}/{total} 上传成功")
        else:
            print(f"  ⚠️ {i+1}/{total} 上传失败，跳过")

        # 立即释放内存
        del data
        await asyncio.sleep(0.5)

    return telegraph_urls, cover_candidates


# ========= 发封面到频道 =========
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

            # 逐张下载并上传到 Telegraph
            telegraph_urls, cover_candidates = await download_and_upload_all(client, urls)

            if not telegraph_urls:
                print(f"  ⚠️ 没有图片上传成功，跳过")
                seen.add(uid)
                save_seen(seen)
                continue

            print(f"  ✅ 成功上传 {len(telegraph_urls)}/{len(urls)} 张到 Telegraph")

            # 创建 Telegraph 页面
            telegraph_url = create_telegraph_page(g["title"], telegraph_urls)
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

            # 每次只处理1个新图集，下次运行继续处理剩余的
            print(f"\n✅ 本次运行完成，下次运行继续处理剩余图集")
            break

asyncio.run(main())
