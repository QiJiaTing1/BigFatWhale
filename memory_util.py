"""记忆注入的相关性粗判：档案偏大时滤掉明显不相关的条目，控制 prompt 膨胀、减少「背台词」。

为什么不精确语义：这边没有本地向量模型，只做字符集 Dice 粗判 + 保留最近几条兜底。
设计原则是「宁可多留、不可误删」——阈值压得很低，query 为空、档案小、或精简后为空时
都原样返回。真正的语义检索等以后上向量再说。
"""

from __future__ import annotations

REL_MIN_SCORE = 0.12
REL_KEEP_RECENT = 3


def rel_score(query: str, text: str) -> float:
    """字符集 Dice 系数（0~1），粗判两条中文文本的话题重合度。"""
    if not query or not text:
        return 0.0
    q, t = set(query), set(text)
    if not q or not t:
        return 0.0
    return 2 * len(q & t) / (len(q) + len(t))


def relevance_gate(items: list[dict], query: str, always_keep=None) -> list[dict]:
    """按相关性精简条目列表（保持原顺序）。

    - `query` 空、条目 <= REL_KEEP_RECENT*2 → 原样返回（太小不值得滤）
    - 每条算 `subject+fact` 与 query 的 Dice 分，>= 阈值、最近几条、或 `always_keep(item)`
      命中就保留
    - 精简后为空 → 回退全量（安全第一）
    """
    query = (query or "").strip()
    if not query or len(items) <= REL_KEEP_RECENT * 2:
        return items
    recent = items[-REL_KEEP_RECENT:]
    keep: list[dict] = []
    for it in items:
        text = (it.get("subject") or "") + it.get("fact", "")
        if (
            rel_score(query, text) >= REL_MIN_SCORE
            or it in recent
            or (always_keep and always_keep(it))
        ):
            keep.append(it)
    return keep or items
