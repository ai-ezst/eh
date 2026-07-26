"""
代理池：自动爬取免费代理 → 验证可用性 → 轮换使用 → 定时刷新
"""
import asyncio
import httpx
import random
import time
from urllib.parse import urlparse

PROXY_SOURCES = [
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=all&anonymity=all",
    "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
]

TEST_URL = "https://imgbb.com/"
TEST_TIMEOUT = 8
REFRESH_INTERVAL = 1800  # 30 分钟刷新
MAX_PROXIES = 30


class ProxyPool:
    def __init__(self):
        self._proxies: list[str] = []
        self._index = 0
        self._last_refresh = 0
        self._lock = asyncio.Lock()

    async def _fetch_proxies(self) -> set[str]:
        """从多个源爬取代理列表"""
        proxies = set()
        async with httpx.AsyncClient(timeout=15) as client:
            for src in PROXY_SOURCES:
                try:
                    r = await client.get(src)
                    for line in r.text.strip().split("\n"):
                        line = line.strip()
                        if line and ":" in line and not line.startswith("#"):
                            # 确保是 ip:port 格式
                            parts = line.split(":")
                            if len(parts) >= 2 and parts[0].count(".") == 3:
                                proxy = f"http://{parts[0]}:{parts[1]}"
                                proxies.add(proxy)
                except Exception:
                    continue
        print(f"  🌐 爬取到 {len(proxies)} 个候选代理")
        return proxies

    async def _test_proxy(self, proxy: str) -> bool:
        """测试单个代理是否可用"""
        try:
            async with httpx.AsyncClient(
                proxy=proxy,
                timeout=TEST_TIMEOUT,
                follow_redirects=False,
            ) as client:
                r = await client.get(TEST_URL)
                return r.status_code in (200, 301, 302, 403)
        except Exception:
            return False

    async def _validate_proxies(self, candidates: set[str]) -> list[str]:
        """并发验证代理，取前 MAX_PROXIES 个可用的"""
        valid = []
        sem = asyncio.Semaphore(50)  # 并发 50 个测试

        async def test_one(proxy):
            async with sem:
                if await self._test_proxy(proxy):
                    valid.append(proxy)

        tasks = [test_one(p) for p in list(candidates)[:200]]
        await asyncio.gather(*tasks)
        return valid[:MAX_PROXIES]

    async def refresh(self, force: bool = False):
        """刷新代理池"""
        now = time.time()
        if not force and now - self._last_refresh < REFRESH_INTERVAL:
            return

        async with self._lock:
            if not force and now - self._last_refresh < REFRESH_INTERVAL:
                return

            print("🔄 刷新代理池...")
            candidates = await self._fetch_proxies()
            self._proxies = await self._validate_proxies(candidates)
            self._index = 0
            self._last_refresh = now
            print(f"  ✅ 代理池就绪: {len(self._proxies)} 个可用代理")

    def get(self) -> str | None:
        """轮换获取一个代理"""
        if not self._proxies:
            return None
        proxy = self._proxies[self._index % len(self._proxies)]
        self._index += 1
        return proxy

    def remove(self, proxy: str):
        """移除失效代理"""
        if proxy in self._proxies:
            self._proxies.remove(proxy)

    @property
    def count(self) -> int:
        return len(self._proxies)


# 全局单例
pool = ProxyPool()