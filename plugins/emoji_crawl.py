"""抓图表情包：主人发「抓图 [数量] <主题>」从网上抓图，AI 自动按情绪分类存入 表情包/。

图片来源 = DeepSeek 搜索找图；分类 = AI 按 7 种情绪自动归类。

流程：DeepSeek 搜索 → 从结果里挑直接的图片链接 + 抓结果页抽取 <img> →
下载 → 校验真实格式 → 按内容去重 → 用多模态模型判定情绪 → 存进对应文件夹。

注意：搜索接口只返回网页（不直接给图片），所以要多一步「抓页面抽 <img>」，
碰上反爬/懒加载的站点可能抽不到，属正常，换个主题再试即可。
"""
import asyncio
import hashlib
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from config import get_config, MOODS  # noqa: E402
from plugins.ai_group import _chat  # noqa: E402

cfg = get_config()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

MAX_IMG_BYTES = 5 * 1024 * 1024   # 单张图上限 5MB
MAX_DISCOVER = 60                 # 最多收集多少候选图
MAX_PAGES = 4                     # 最多抓几个结果页抽图

IMG_URL_RE = re.compile(r"https?://[^\s\"'<>]+?\.(?:jpe?g|png|gif|webp|bmp)(?:[?#][^\s\"'<>]*)?", re.I)
SRC_ATTR_RE = re.compile(r'<img[^>]+?(?:src|data-src|data-original)\s*=\s*["\']([^"\']+)["\']', re.I)


def _is_img_url(u: str) -> bool:
    return bool(IMG_URL_RE.search(u))


def _sniff(data: bytes) -> str | None:
    """按文件头判断真实图片格式，返回扩展名；不是认识的图返回 None。"""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"BM":
        return ".bmp"
    return None


async def _search_results(query: str, count: int = 10) -> list[dict]:
    """调 DeepSeek 搜索，返回原始结果列表；失败返回 []。"""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{cfg.ai.base_url.rstrip('/')}/v1/tools/search",
                headers={
                    "Authorization": f"Bearer {cfg.ai.api_key}",
                    "Content-Type": "application/json",
                },
                json={"query": query, "count": count},
            )
        if r.status_code != 200:
            return []
        return (r.json().get("results") or [])[:count]
    except Exception:
        return []


async def _page_img_urls(page_url: str) -> list[str]:
    """原样抓一个网页，抽取 <img> 的 src/data-src/data-original，转成绝对地址。"""
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True,
                                     headers={"User-Agent": USER_AGENT}) as c:
            r = await c.get(page_url)
        if r.status_code != 200:
            return []
        html = r.text
    except Exception:
        return []
    out: list[str] = []
    for raw in SRC_ATTR_RE.findall(html):
        u = urljoin(page_url, raw)
        if _is_img_url(u):
            out.append(u)
    return out


async def _discover(topic: str) -> list[str]:
    """收集候选图片 URL：结果正文里的直链 + 结果页里的 <img>。"""
    results = await _search_results(f"{topic} 表情包 图片", 10)
    urls: list[str] = []
    seen: set[str] = set()

    def add(u: str) -> None:
        u = u.strip()
        if u and u not in seen and _is_img_url(u):
            seen.add(u)
            urls.append(u)

    # 1) 搜索结果正文里可能直接带着图片链接
    for it in results:
        for field in ("content", "snippet", "summary"):
            for u in IMG_URL_RE.findall(str(it.get(field) or "")):
                add(u)
    # 2) 抓前几个结果页，把页面里的 <img> 抽出来
    pages = [it.get("url") for it in results if it.get("url")][:MAX_PAGES]
    for p in pages:
        for u in await _page_img_urls(p):
            add(u)
        if len(urls) >= MAX_DISCOVER:
            break
    return urls


async def _download(url: str) -> bytes | None:
    """下载一张图，超限/非图返回 None。"""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                     headers={"User-Agent": USER_AGENT}) as c:
            async with c.stream("GET", url) as r:
                if r.status_code != 200:
                    return None
                ct = (r.headers.get("content-type") or "").lower()
                if ct and "image" not in ct and not _is_img_url(url):
                    return None
                data = bytearray()
                async for chunk in r.aiter_bytes():
                    data += chunk
                    if len(data) > MAX_IMG_BYTES:
                        return None
                return bytes(data)
    except Exception:
        return None


async def _classify(url: str) -> str:
    """让多模态模型判断这张图当表情包最贴哪种情绪，只回一个词。"""
    content = [
        {"type": "text", "text": "判断这张图当表情包最贴哪种情绪，只回下面七个词之一，别多说：开心/无语/生气/卖萌/得意/委屈/震惊"},
        {"type": "image_url", "image_url": {"url": url}},
    ]
    text, _ = await _chat([{"role": "user", "content": content}],
                          temperature=0.1, max_tokens=10)
    for m in MOODS:
        if m in text:
            return m
    return MOODS[0]  # 判定失败兜底存「开心」


async def save_image(url: str, mood: str) -> Path | None:
    """下载一张图存进指定情绪文件夹，返回保存路径；失败返回 None。"""
    if mood not in MOODS:
        return None
    data = await _download(url)
    if not data:
        return None
    ext = _sniff(data)
    if not ext:
        return None
    h = hashlib.sha256(data).hexdigest()
    d = cfg.emoji.dir / mood
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"{h[:16]}{ext}"
    if not dest.exists():
        dest.write_bytes(data)
    return dest


async def crawl(topic: str, count: int = 10) -> str:
    """抓图主流程，返回给主人的一句话总结。"""
    d = cfg.emoji.dir
    d.mkdir(parents=True, exist_ok=True)
    for m in MOODS:
        (d / m).mkdir(exist_ok=True)

    urls = await _discover(topic)
    saved: dict[str, int] = {}
    seen_hash: set[str] = set()
    errors = 0

    for u in urls:
        if sum(saved.values()) >= count:
            break
        data = await _download(u)
        if not data:
            errors += 1
            continue
        ext = _sniff(data)
        if not ext:
            errors += 1
            continue
        h = hashlib.sha256(data).hexdigest()
        if h in seen_hash:
            continue
        seen_hash.add(h)
        mood = await _classify(u)
        (d / mood / f"{h[:16]}{ext}").write_bytes(data)
        saved[mood] = saved.get(mood, 0) + 1

    total = sum(saved.values())
    if not total:
        return "抓完了，但一张能用的图都没抓到（可能被网站挡住、或没搜到直链）。换个主题再试？"
    detail = "，".join(f"{m}{n}张" for m, n in saved.items())
    return f"抓好了，共 {total} 张：{detail}。已按情绪存进 表情包/ 对应文件夹。"
