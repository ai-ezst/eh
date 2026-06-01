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

# ========= 配置参数 =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
MAIN_CHANNEL = os.getenv("MAIN_CHANNEL_ID")

EH_MEMBER_ID = os.getenv("EH_MEMBER_ID")
EH_PASS_HASH = os.getenv("EH_PASS_HASH")

STATE_FILE = "sent_galleries.json"
COSPLAY_URL = "https://e-hentai.org/?f_cats=959"
MAX_PAGES = 20

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://e-hentai.org/",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

COOKIES = {
    "ipb_member_id": EH_MEMBER_ID,
    "ipb_pass_hash": EH_PASS_HASH
}

TELEGRAPH_TOKEN = None

# ========= Telegraph 账户初始化 =========
async def get_or_create_telegraph_token(client):
    global TELEGRAPH_TOKEN
    try:
        r = await client.post("https://api.telegra.ph/createAccount", json={
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

# ========= 智能图片压缩模块 =========
def compress_image(img_bytes, max_size=1600, quality=85):
    """保持长宽比压缩图片并转换为JPEG，确保严格小于5MB限制"""
    try:
        img = Image.open(BytesIO(img_bytes))
        
        # 统一转为 RGB 模式（防止 RGBA 转换为 JPEG 时报错）
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
            
        w, h = img.size
        if max(w, h) > max_size:
            if w > h:
                new_w = max_size
                new_h = int(h * (max_size / w))
            else:
                new_h = max_size
                new_w = int(w * (max_size / h))
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            
        out = BytesIO()
        img.save(out, format="JPEG", quality=quality)
        compressed_data = out.getvalue()
        
        # 二次极端兜底（如果依然大于4.5MB，降低质量再压一次）
        if len(compressed_data) > 4.5 * 1024 * 1024:
            out = BytesIO()
            img.save(out, format="JPEG", quality=60)
            compressed_data = out.getvalue()
            
        return compressed_data
    except Exception as e:
        print(f"  ⚠️ 图片处理/压缩失败，尝试返回原图: {e}")
        return img_bytes

# ========= 异步多图上传至 Telegraph =========
async def upload_to_telegraph(client, img_bytes):
    """异步上传单张图片到 Telegraph"""
    files = {'file': ('image.jpg', img_bytes, 'image/jpeg')}
    for attempt in range(3):
        try:
            r = await client.post("https://telegra.ph/upload", files=files, timeout=30)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and len(data) > 0:
                    return "https://telegra.ph" + data[0]["src"]
            await asyncio.sleep(1.5)
        except Exception as e:
            print(f"    ⚠️ 上传分片重试 (第{attempt+1}次): {e}")
            await asyncio.sleep(2)
    return None

async def upload_all_images(client, images_list):
    """并发上传图集所有图片"""
    semaphore = asyncio.Semaphore(3)  # 控制并发数，防止被 Telegraph 拒绝服务
    
    async def worker(img_bytes, idx):
        async with semaphore:
            url = await upload_to_telegraph(client, img_bytes)
            if url:
                print(f"    ✨ 上传进度: [{idx+1}/{len(images_list)}] 成功")
            return url

    tasks = [worker(img, i) for i, img in enumerate(images_list)]
    results = await asyncio.gather(*tasks)
    return [r for r in results if r]

# ========= 创建 Telegraph 页面（含多页自动切分逻辑） =========
async def create_telegraph_pages(client, title, t_urls):
    """创建 Telegraph 页面，若超过300张图自动进行逆向多页动态拼接"""
    if not TELEGRAPH_TOKEN:
        print("  ⚠️ 无 Telegraph token，跳过页面创建")
        return None

    # 按 300 张一张页面切分
    chunk_size = 300
    chunks = [t_urls[i:i + chunk_size] for i in range(0, len(t_urls), chunk_size)]
    
    next_page_url = None
    first_page_url = None
    
    # 逆向创建页面，以便前面的页面可以链接到后面的页面
    for idx in reversed(range(len(chunks))):
        chunk = chunks[idx]
        children = [{"tag": "img", "attrs": {"src": url}} for url in chunk]
        
        if next_page_url:
            children.append({
                "tag": "p", 
                "children": [{
                    "tag": "a", 
                    "attrs": {"href": next_page_url}, 
                    "children": [f"👉 查看下一页 / Next Page (Part {idx + 2})"]
                }]
            })
            
        page_title = title if len(chunks) == 1 else f"{title} (Part {idx + 1})"
        
        payload = {
            "access_token": TELEGRAPH_TOKEN,
            "title": page_title[:256],
            "content": json.dumps(children, ensure_ascii=False),
            "return_content": "false",
        }
        
        try:
            r = await client.post("https://api.telegra.ph/createPage", data=payload, timeout=15)
            if r.status_code == 200 and r.json().get("ok"):
                next_page_url = r.json()["result"]["url"]
                if idx == 0:
                    first_page_url = next_page_url
                print(f"  ✅ Telegraph 页面生成成功 (Part {idx + 1}): {next_page_url}")
            else:
                print(f"  ❌ Telegraph 页面生成失败: {r.text[:120]}")
                return first_page_url if first_page_url else next_page_url
        except Exception as e:
            print(f"  ❌ Telegraph 生成页面异常: {e}")
            return first_page_url if first_page_url else next_page_url

    return first_page_url

# ========= 状态持久化 =========
def load_seen():
    if not os.path.exists(STATE_FILE):
        return set()
    try:
        return set(json.load(open(STATE_FILE)))
    except:
        return set()

def save_seen(seen):
    json.dump(list(seen), open(STATE_FILE, "w"))

# ========= 文本清洗 =========
def clean_title(title):
    title = re.sub(r'\[.*?\]', '', title)
    title = re.sub(r'f:[^ ]+', '', title)
    title = re.sub(r'\s+', ' ', title)
    return title.strip()

# ========= 选最佳封面 =========
def pick_cover(images_data: list[bytes]) -> bytes:
    portrait = []
    all_imgs = []

    for data in images_data:
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
        except:
            continue

    if portrait:
        return max(portrait, key=lambda x: x[0])[1]
    elif all_imgs:
        return max(all_imgs, key=lambda x: x[0])[1]
    else:
        return images_data[0]

# ========= 爬取列表页 =========
async def get_galleries(client):
    r = await client.get(COSPLAY_URL)
    soup = BeautifulSoup(r.text, "html.parser")
    galleries = []
    seen_urls = set()

    for a in soup.select("a[href*='/g/']"):
        href = a.get("href", "")
        m = re.search(r"/g/(\d+)/([a-f0-9]+)/", href)
        if not m or href in seen_urls:
            continue
        seen_urls.add(href)

        title_node = a.select_one(".glink") or a.find(class_="glink")
        if not title_node:
            parent = a.parent
            for _ in range(5):
                if not parent: break
                title_node = parent.select_one(".glink")
                if title_node: break
                parent = parent.parent

        if not title_node: continue
        title = clean_title(title_node.text)
        if not title: continue

        galleries.append({
            "gid": m.group(1),
            "token": m.group(2),
            "url": href,
            "title": title
        })

    print(f"📋 频道扫描完毕，捕获到 {len(galleries)} 个可用图集")
    return galleries

# ========= 爬取单本图集的所有图片原始直链 =========
async def get_all_image_urls(client, base_url):
    r = await client.get(base_url)
    soup = BeautifulSoup(r.text, "html.parser")

    max_page = 0
    for a in soup.select(".ptt a"):
        try: max_page = max(max_page, int(a.text))
        except: pass

    actual_pages = min(max_page + 1, MAX_PAGES)
    print(f"📄 图集总页数: {max_page+1}，设定最大跨度抓取: {actual_pages} 页")

    all_pages = []
    for i in range(actual_pages):
        url = f"{base_url}?p={i}"
        try:
            r = await client.get(url)
            soup = BeautifulSoup(r.text, "html.parser")
            thumbs = [a["href"] for a in soup.select("#gdt a")]
            all_pages.extend(thumbs)
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"  ⚠️ 导流页 P.{i} 解析失败: {e}")
            continue

    semaphore = asyncio.Semaphore(4)
    async def fetch_img_url(url):
        for attempt in range(3):
            try:
                async with semaphore:
                    r = await client.get(url)
                    soup = BeautifulSoup(r.text, "html.parser")
                    img = soup.select_one("#img")
                    if img: return img["src"]
            except:
                await asyncio.sleep(2)
        return None

    results = await asyncio.gather(*[fetch_img_url(u) for u in all_pages])
    return [r for r in results if r]

# ========= 全量下载并自动压缩模块 =========
async def download_and_compress_all(client, urls):
    """并发下载全部图片，并在内存中完成无损/低损压缩"""
    semaphore = asyncio.Semaphore(5)

    async def worker(url):
        for attempt in range(3):
            try:
                async with semaphore:
                    r = await client.get(url, timeout=45)
                    if r.status_code == 200 and len(r.content) > 5000:
                        # 核心步骤：下载后直接在内存中执行压缩
                        return compress_image(r.content)
            except:
                await asyncio.sleep(2)
        return None

    tasks = [worker(u) for u in urls]
    results = await asyncio.gather(*tasks)
    return [r for r in results if r]

# ========= 发送消息至 Telegram Channel =========
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

# ========= 主驱动异步核心 =========
async def main():
    bot = Bot(BOT_TOKEN)
    seen = load_seen()

    async with httpx.AsyncClient(headers=HEADERS, cookies=COOKIES, timeout=60) as client:
        await get_or_create_telegraph_token(client)
        galleries = await get_galleries(client)

        for g in galleries:
            uid = f"{g['gid']}_{g['token']}"
            if uid in seen:
                print(f"⏭️ 跳过已处理图集: {g['title']}")
                continue

            print(f"\n📂 正在清洗并处理: {g['title']}")

            # 1. 抓取 EH 所有图片详情页中的直链 URL
            img_urls = await get_all_image_urls(client, g["url"])
            if not img_urls:
                print(f"  ⚠️ 未能解析到任何图片直链，跳过")
                continue
            print(f"  🔗 成功捕获 {len(img_urls)} 个原始图片直链")

            # 2. 并发下载并自动在内存中压缩图片
            print("  📥 开始高并发下载并实时执行抗上限压缩...")
            local_images = await download_and_compress_all(client, img_urls)
            if not local_images:
                print(f"  ⚠️ 核心图片数据下载失败，跳过")
                continue
            print(f"  💾 成功落盘并压缩 {len(local_images)} 张图片到动态内存")

            # 3. 并发上传至 Telegraph
            print("  📤 开始异步分片上传至 Telegraph 服务器...")
            telegraph_image_urls = await upload_all_images(client, local_images)
            if not telegraph_image_urls:
                print(f"  ⚠️ 所有图片均上传 Telegraph 失败，跳过")
                continue

            # 4. 创建 Telegraph 页面
            telegraph_url = await create_telegraph_pages(client, g["title"], telegraph_image_urls)
            if not telegraph_url:
                print(f"  ⚠️ Telegraph 联页节点创建失败，跳过")
                continue

            # 5. 从已下载的压缩图中挑选最佳竖图作为封面
            cover = pick_cover(local_images)

            # 6. 推送至 Telegram 频道
            try:
                await send_cover(bot, cover, g["title"], telegraph_url)
                print(f"  🚀 成功同步推送至 TG 频道: {g['title']}")
                
                seen.add(uid)
                save_seen(seen)
            except Exception as e:
                print(f"  ❌ 推送至 TG 频道失败: {e}")

            # 礼貌等待，减缓外部请求压力
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
