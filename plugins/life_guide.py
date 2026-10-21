"""「人生决策指南」skill 插件：// 触发，读 GitHub 上的《高性价比人生指南》正文后回答。

来源：eternity4719/HowToLiveBetter（`book/` 下 34 个 md，合计约 1.3MB）。
机制：
- 正文太大塞不进一次对话，所以按需下载到 `data/HowToLiveBetter/` 缓存（不在 `memory/` 里）。
- 收到问题后，用字符 bigram 打分从 34 节里挑相关条目，把条目拼进 prompt 再调模型。
- 只调 `call_ai_raw`（单条 user 消息），不注入长期/情感记忆、不进短期上下文 —— 与命令隔离。
"""

import base64
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

REPO = "eternity4719/HowToLiveBetter"
BRANCH = "main"
TREE_API = f"https://api.github.com/repos/{REPO}/git/trees/{BRANCH}?recursive=1"

DATA_DIR = ROOT / "data" / "HowToLiveBetter"
BOOK_DIR = DATA_DIR / "book"
MARKER = DATA_DIR / ".cached"
CACHE_TTL = 7 * 24 * 3600      # 缓存 7 天后自动刷新一次
MAX_ENTRY_CHARS = 8000         # 拼进 prompt 的条目总字数上限（控 token）
MAX_ENTRIES = 12               # 最多带几条条目
MAX_BLOB_BYTES = 3 * 1024 * 1024

USAGE = (
    "用法：// 你的问题\n"
    "比如：// 该不该买保险、// 熬夜怎么补、// 这样犯法吗、// 租房子要注意什么。\n"
    "我会翻《高性价比人生指南》正文，查了再答，没写到的会直说。"
)

# 回答规则：转述自 skill 的「第 0/3/4/5 步 + 边界」。取书、定位由本插件代劳，不写给模型。
SKILL_RULES = """你是鲸鱼娘，正在用《高性价比人生指南》回答主人一个人生决策问题。

【唯一依据】下面【正文条目】就是这本书的相关原文，是你回答的唯一依据。
- 只据这些条目答，不要凭记忆补数字、补 DOI、补法条条款号。
- 条目里没提到的，直说「书里没写」，可以给常识但要标明那是常识。
- 数字照抄不改，HR/RR/OR 这类写法旁边就地翻成「低约 xx%」，原值保留。

【排序】先把相关条目排好序再答：
- 收益高、成本低、证据等级 A>B>C 优先；再按贴合主人处境的排前面。
- 换「寿命」和换「金钱/时间」的条目口径不同，分开列，别混着比。

【回答结构】按下面结构写，简体中文、口语化、语气克制、不说教、不用感叹号：
1. 一句话结论：这件事划不划算 / 该不该做 / 第一步是什么。
2. 先做这几条（3~7 条）：每条动词开头，写清「花掉什么、换回什么、证据等级、出处（第 X 节第 N 条 标题）」。
3. 别做 / 不用做的：书里明确说不值或有反证的，单列出来。
4. 书里没写的：如实说。
5. 需要的话补一句复查点。

【急症/危险优先】问题涉及正在发生的急症（倒地没呼吸、大出血、卒中/心梗等），先让打 120/119 和现场第一动作；提到自杀先给全国心理援助热线 12356。这些优先于上面的算账。

【边界】只给通用口径，不替代医生/律师/会计；具体病情/案件/税务只说该找谁。不辩论，不追着劝。
"""


def is_cached() -> bool:
    """缓存是否已就绪且未过期（给调用方决定要不要提示「第一次下载」）。"""
    if not (BOOK_DIR.is_dir() and any(BOOK_DIR.glob("*.md"))):
        return False
    return _fresh()


def _fresh() -> bool:
    if not MARKER.exists():
        return False
    try:
        return (time.time() - float(MARKER.read_text(encoding="utf-8").strip())) < CACHE_TTL
    except Exception:  # noqa: BLE001
        return False


async def _fetch_blob(client: httpx.AsyncClient, sha: str) -> bytes | None:
    """走 GitHub API（api.github.com 可达，raw 域名在墙内常不通）取 blob，base64 解码。"""
    try:
        r = await client.get(f"https://api.github.com/repos/{REPO}/git/blobs/{sha}")
        if r.status_code != 200:
            return None
        content = r.json().get("content") or ""
        data = base64.b64decode(content)
        return data if len(data) <= MAX_BLOB_BYTES else None
    except Exception:  # noqa: BLE001
        return None


async def ensure_cached(client: httpx.AsyncClient) -> None:
    """缓存没有或过期时，从 GitHub 拉取 book/ 下全部正文到本地。"""
    global _entries_cache
    if is_cached():
        return
    r = await client.get(TREE_API)
    r.raise_for_status()
    blobs = [
        t for t in (r.json().get("tree") or [])
        if t["type"] == "blob" and t["path"].startswith("book/") and t["path"].endswith(".md")
    ]
    if not blobs:
        raise RuntimeError("指南仓库 book/ 下没找到正文")
    BOOK_DIR.mkdir(parents=True, exist_ok=True)
    for b in blobs:
        data = await _fetch_blob(client, b["sha"])
        if data:
            (BOOK_DIR / b["path"].split("/")[-1]).write_bytes(data)
    MARKER.write_text(str(time.time()), encoding="utf-8")
    _entries_cache = None


# ---------------------------------------------------------------- 索引

_entries_cache: list[dict] | None = None

_STOP = re.compile(r"[的了呢吗啊呀哦嗯么吧在是有没这那我你他她它们就都要不别被把和与或及]")


def _norm(s: str) -> str:
    s = re.sub(r"[\W_]+", "", s or "")
    return _STOP.sub("", s)


def _bigrams(s: str) -> set[str]:
    s = _norm(s)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _score(q: str, text: str) -> float:
    qb = _bigrams(q)
    if not qb:
        qn = _norm(q)
        return 1.0 if qn and qn in _norm(text) else 0.0
    tb = _bigrams(text)
    return len(qb & tb) / len(qb)


def _section_label(filename: str) -> tuple[str, str]:
    m = re.match(r"^(\d+)-(.+)\.md$", filename)
    if not m:
        return filename, filename
    num, name = int(m.group(1)), m.group(2)
    return f"第 {num} 节《{name}》", name


def _load_entries() -> list[dict]:
    global _entries_cache
    if _entries_cache is not None:
        return _entries_cache
    entries: list[dict] = []
    for f in sorted(BOOK_DIR.glob("*.md")):
        label, name = _section_label(f.name)
        text = f.read_text(encoding="utf-8", errors="replace")
        for part in re.split(r"(?m)^###\s+", text)[1:]:
            head, _, body = part.partition("\n")
            title = head.strip()
            if not title:
                continue
            body = re.sub(r"<!--.*?-->", "", body, flags=re.S).strip()
            entries.append({"section": label, "section_name": name, "title": title, "body": body})
    _entries_cache = entries
    return entries


def _retrieve(q: str, entries: list[dict]) -> list[str]:
    scored = []
    for e in entries:
        s = 2.0 * _score(q, e["title"]) + _score(q, e["section_name"]) + 0.3 * _score(q, e["body"][:300])
        if s > 0:
            scored.append((s, e))
    scored.sort(key=lambda x: -x[0])
    out, chars = [], 0
    for _, e in scored:
        block = f"【{e['section']}】{e['title']}\n{e['body']}"
        if chars + len(block) > MAX_ENTRY_CHARS or len(out) >= MAX_ENTRIES:
            continue
        out.append(block)
        chars += len(block)
    return out


# ---------------------------------------------------------------- 查询

async def query(q: str) -> str:
    """按问题查指南并生成回答。q 为去掉 // 后的纯问题文本。"""
    q = (q or "").strip()
    if not q:
        return USAGE
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        await ensure_cached(client)
    entries = _load_entries()
    hits = _retrieve(q, entries)
    if not hits:
        return (
            f"我把《高性价比人生指南》翻了一遍，没找到跟「{q}」直接相关的条目，不好瞎说。"
            "你可以换个更具体的问法再试试。"
        )
    prompt = (
        f"{SKILL_RULES}\n\n"
        "【正文条目】（已按相关度排序，是你回答的全部依据）\n"
        + "\n\n".join(hits)
        + f"\n\n【主人问】{q}\n\n请按上面的规则回答。"
    )
    try:
        from plugins.ai_group import call_ai_raw  # 延迟导入，避免循环依赖
        text, _ = await call_ai_raw(prompt, temperature=0.3)
    except Exception as e:  # noqa: BLE001
        return f"查询人生指南出错了：{type(e).__name__}: {e}"
    return (text or "").strip() or "（指南没给出可用答复）"
