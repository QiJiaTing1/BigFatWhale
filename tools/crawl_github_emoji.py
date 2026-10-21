"""从 GitHub「蓝色大肥鱼档案馆」抓鲸鱼娘表情包，按情绪分类存入 表情包/。

来源：EDMOK/blue-fish-archive 仓库的 large/*.webp（共 271 张，文件名即拼音情绪/动作）。
分类：用多模态模型判定情绪（和「抓图」命令同一套 7 情绪），存进对应文件夹。

独立脚本：不 import plugins.*（那些会触发 nonebot 初始化），自己直连模型 + 下载。
用法：.venv/Scripts/python.exe tools/crawl_github_emoji.py [最大总数]
"""
import asyncio
import base64
import hashlib
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from config import get_config, MOODS  # noqa: E402

cfg = get_config()

REPO = "EDMOK/blue-fish-archive"
BRANCH = "main"
API = f"https://api.github.com/repos/{REPO}/git/trees/{BRANCH}?recursive=1"
RAW = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/"

MAX_PER_MOOD = 8          # 每种情绪最多存几张
MAX_TOTAL = 56            # 总上限（7 情绪 × 8）
MAX_ATTEMPTS = 150        # 分类尝试次数上限（防止全桶满后空转）
MAX_IMG_BYTES = 5 * 1024 * 1024
IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

CLASSIFY_PROMPT = (
    "判断这张图当表情包最贴哪种情绪，只回下面七个词之一，别多说："
    "开心/无语/生气/卖萌/得意/委屈/震惊"
)


def _sniff(data: bytes) -> str | None:
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


async def list_images(client: httpx.AsyncClient) -> list[str]:
    r = await client.get(API)
    r.raise_for_status()
    data = r.json()
    return [
        t["path"] for t in data.get("tree", [])
        if t["type"] == "blob" and t["path"].startswith("large/")
        and Path(t["path"]).suffix.lower() in IMG_EXTS
    ]


MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}


async def classify(client: httpx.AsyncClient, data: bytes, ext: str) -> str | None:
    """用 base64 内联图片（DeepSeek 服务器拉不到 raw.githubusercontent）判定情绪。失败返回 None。"""
    mime = MIME.get(ext, "image/webp")
    data_uri = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    payload = {
        "model": cfg.ai.model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": CLASSIFY_PROMPT},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }],
        "thinking": {"type": "disabled"},
        "temperature": 0.1,
        "max_tokens": 10,
    }
    try:
        r = await client.post(
            cfg.ai.endpoint(),
            headers={"Authorization": f"Bearer {cfg.ai.api_key}"},
            json=payload,
        )
    except Exception as e:  # noqa: BLE001
        print(f"    [classify err] {type(e).__name__}")
        return None
    if r.status_code != 200:
        print(f"    [classify {r.status_code}] {r.text[:200]}")
        return None
    data = r.json()
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    for m in MOODS:
        if m in text:
            return m
    return MOODS[0]


async def download(client: httpx.AsyncClient, url: str) -> bytes | None:
    try:
        async with client.stream("GET", url) as r:
            if r.status_code != 200:
                return None
            data = bytearray()
            async for chunk in r.aiter_bytes():
                data += chunk
                if len(data) > MAX_IMG_BYTES:
                    return None
            return bytes(data)
    except Exception:
        return None


async def main() -> None:
    total = int(sys.argv[1]) if len(sys.argv) > 1 else MAX_TOTAL
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        paths = await list_images(client)
        print(f"发现 {len(paths)} 张 large/ 图，目标最多存 {total} 张")
        random.shuffle(paths)

        saved = {m: 0 for m in MOODS}
        seen: set[str] = set()
        attempts = 0
        for path in paths:
            if sum(saved.values()) >= total or attempts >= MAX_ATTEMPTS:
                break
            url = RAW + path
            attempts += 1
            data = await download(client, url)
            if not data:
                continue
            ext = _sniff(data)
            if not ext:
                continue
            h = hashlib.sha256(data).hexdigest()
            if h in seen:
                continue
            mood = await classify(client, data, ext)
            if mood is None or saved[mood] >= MAX_PER_MOOD:
                continue
            seen.add(h)
            dest = cfg.emoji.dir / mood / f"{h[:16]}{ext}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                dest.write_bytes(data)
            saved[mood] += 1
            print(f"  {mood} ← {Path(path).name}")

    detail = "，".join(f"{m}{n}张" for m, n in saved.items() if n)
    print(f"\n完成：共 {sum(saved.values())} 张（{detail}），分类尝试 {attempts} 次")


if __name__ == "__main__":
    asyncio.run(main())
