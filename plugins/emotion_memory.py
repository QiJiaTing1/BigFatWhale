"""情感记忆插件：把群里值得记住的「情绪」和「人际关系」沉淀成独立档案，每次对话带上。

设计要点
--------
1. **与事实档案分开**（`long_memory.py` 记的是"谁喜欢什么"这种稳定偏好，这里记的是
   "谁最近心情如何""谁和谁关系怎么样"）。
2. **情绪会过期**：情绪条目按指数衰减（越强烈忘得越慢），衰减到阈值以下自动遗忘；
   人际关系长期保留，只靠新信息 UPDATE/DELETE 修正。
3. **按 key 隔离**：key 是群号或私聊 user_id，主人的私聊天然独立一份，不跟群串。

触发时机
--------
- 复用 `MEM_CONSOLIDATE_EVERY`：攒够同样条数就自动合并一次（后台任务，不阻塞聊天）
- 另外由 `plugins/schedule.py` 每天中午兜底合并一次

存储
----
`qq-ai-bot/memory/情感记忆.json`，同时存档案和"待合并"消息（重启不丢）。
"""

import asyncio
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import nonebot  # noqa: E402

from config import get_config  # noqa: E402

cfg = get_config()

DATA_DIR = ROOT / "memory"
EMOTION_FILE = DATA_DIR / "情感记忆.json"

# 参数都在 config.py / .env 里（EMO_* 一组 + 复用事实记忆的攒够/缓冲阈值）
HALF_LIFE_DAYS = cfg.emotion.half_life_days
MIN_INTENSITY = cfg.emotion.min_intensity
MAX_DAYS = cfg.emotion.max_days
MAX_USE_CHARS = cfg.emotion.max_use_chars
CONSOLIDATE_EVERY = cfg.memory.consolidate_every
PENDING_KEEP = cfg.memory.pending_keep

# 内存态：{key: [ {id, subject, kind, fact, intensity, date}, ... ]}
_archive: dict[str, list[dict]] = {}
# 待合并的新消息：{key: [ [昵称, 内容, 时间], ... ]}
_pending: dict[str, list[list[str]]] = {}
# 正在合并的 key，防止并发重复合并
_busy: set[str] = set()


# ---------------------------------------------------------------- 存储

def _load() -> None:
    global _archive, _pending
    if not EMOTION_FILE.exists():
        return
    try:
        data = json.loads(EMOTION_FILE.read_text(encoding="utf-8"))
        _archive = {str(k): list(v) for k, v in (data.get("archive") or {}).items()}
        _pending = {str(k): [list(x) for x in v] for k, v in (data.get("pending") or {}).items()}
        nonebot.logger.info(
            f"[emotion_memory] 已加载情感记忆：{len(_archive)} 个 key，"
            f"{sum(len(v) for v in _archive.values())} 条印象"
        )
    except Exception as e:  # noqa: BLE001
        nonebot.logger.warning(f"[emotion_memory] 读取失败：{e}")


def _save() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"archive": _archive, "pending": _pending}
        EMOTION_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:  # noqa: BLE001
        nonebot.logger.warning(f"[emotion_memory] 保存失败：{e}")


_load()


# ---------------------------------------------------------------- 衰减

def _age_days(date_str: str, today: date | None = None) -> int:
    """从 'YYYY-MM-DD' 到今天过了多少天；解析失败返回一个很大的数（当过期处理）。"""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return 10**9
    return ((today or date.today()) - d).days


def _effective_intensity(intensity: float, age_days: int) -> float:
    """情绪指数衰减：强度 × 0.5^(age / half_life)，half_life 随强度变长（越强烈忘得越慢）。"""
    half_life = HALF_LIFE_DAYS * (1.0 + intensity)
    return intensity * (0.5 ** (age_days / half_life))


def _forgotten(it: dict, today: date | None = None) -> bool:
    """该条目是否已过期该忘。关系(kind=relation)永不衰减。"""
    if it.get("kind") == "relation":
        return False
    age = _age_days(it.get("date", ""), today)
    if age > MAX_DAYS:
        return True
    try:
        intensity = float(it.get("intensity", 0.5) or 0.5)
    except (TypeError, ValueError):
        intensity = 0.5
    return _effective_intensity(intensity, age) < MIN_INTENSITY


# ---------------------------------------------------------------- 读取

def emotion_text(key: int) -> str:
    """把该 key 的情感档案格式化成一段文字，供拼进 prompt。没有就返回空串。

    读取时顺带过滤掉已衰减过期的情绪条目（关系不过滤）。
    """
    items = [it for it in (_archive.get(str(key)) or []) if not _forgotten(it)]
    lines = [
        f"- {it.get('subject') or '群友'}（{'情绪' if it.get('kind') == 'emotion' else '关系'}）：{it.get('fact', '')}"
        for it in items
        if it.get("fact")
    ]
    if not lines:
        return ""
    body = "\n".join(lines)
    if len(body) > MAX_USE_CHARS:
        body = body[:MAX_USE_CHARS] + "\n…"
    return (
        "【情感档案】以下是鲸鱼娘记住的、关于这个群近期情绪和人际关系的印象。"
        "聊天时自然地共情、关心（比如记得谁最近不开心、谁和谁关系好），但别刻意背出来、别逐条念。"
        "情绪是会过时的，太久没再提的就当没这回事：\n" + body
    )


def stats(key: int) -> dict:
    """给日志/测试看的统计。"""
    items = _archive.get(str(key)) or []
    text = emotion_text(key)
    return {
        "entries": len(items),
        "prompt_chars": len(text),
        "pending": len(_pending.get(str(key)) or []),
    }


# ---------------------------------------------------------------- 写入

def add_pending(key: int, name: str, text: str) -> None:
    """把一条消息记入"待合并"缓冲；攒够了就触发情感整理合并。"""
    text = (text or "").strip()
    if not text:
        return
    buf = _pending.setdefault(str(key), [])
    buf.append([name, text[:160], datetime.now().strftime("%m-%d %H:%M")])
    if len(buf) > PENDING_KEEP:
        del buf[: len(buf) - PENDING_KEEP]
    if len(buf) >= CONSOLIDATE_EVERY:
        _trigger(key)


def _trigger(key: int) -> None:
    """后台起一个合并任务（不阻塞聊天）。"""
    k = str(key)
    if k in _busy:
        return
    try:
        asyncio.get_running_loop().create_task(consolidate(key))
    except RuntimeError:
        pass  # 不在事件循环里（比如测试直接调用），忽略


async def consolidate_all() -> None:
    """把所有有待合并消息的 key 都合并一遍（每天兜底调用）。"""
    for k in list(_pending.keys()):
        if _pending.get(k):
            await consolidate(int(k))


# ---------------------------------------------------------------- 合并（核心）

EMOTION_MERGE_PROMPT = """你是「情感整理员」。任务：从一个 QQ 群的聊天记录里，提炼出值得长期记住的「情绪」和「人际关系」。

【情绪】谁最近是什么心情/状态（开心、难过、emo、压力大、生病、忙碌…），要能指导以后关心对方。
【关系】谁和谁是什么关系/亲疏（关系好、不对付、喜欢、嫌弃、谁对鲸鱼娘什么态度），要稳定、长期有效。

【不要记】日常寒暄（哈哈/晚安/在吗）、一次性话题（今晚吃啥）、玩笑话、没信息量的闲扯、群里陌生人随口一句。

【操作规则】
- 新增 → op="add"
- 同一人同一类的新状态覆盖旧状态 → op="update"（带旧条目的 id），不要两条并存
- 已过时/被新信息推翻 → op="delete"
- 情绪要带 intensity（0~1，越强烈越接近 1，默认 0.5）；关系不带 intensity
- fact 不超过 30 字，写清"谁 + 什么情绪/什么关系"
- 没有值得记的就输出 {"ops":[]}

只输出 JSON，不要任何解释、不要代码块以外的文字。

输出格式：
{"ops":[{"op":"add","subject":"小明","kind":"emotion","fact":"最近失恋心情低落","intensity":0.8},{"op":"add","subject":"小明↔小红","kind":"relation","fact":"关系很好常互怼"},{"op":"update","id":2,"fact":"已经缓过来了","intensity":0.3},{"op":"delete","id":5}]}

========== 已有情感档案 ==========
{archive}

========== 新增聊天记录 ==========
{new_msgs}
"""


def _extract_json(s: str) -> dict | None:
    if not s:
        return None
    s = re.sub(r"```(?:json)?", "", s).strip().strip("`")
    i, j = s.find("{"), s.rfind("}")
    if i == -1 or j == -1:
        return None
    try:
        return json.loads(s[i : j + 1])
    except Exception:  # noqa: BLE001
        return None


def _emotion_lines(key: int) -> str:
    items = _archive.get(str(key)) or []
    if not items:
        return "(空)"
    return "\n".join(
        f"[{it['id']}] {it.get('subject') or '群友'}（{it.get('kind', 'emotion')}）：{it.get('fact', '')}"
        for it in items
    )


def _norm(s: str) -> str:
    """去空白和标点，用来判断两条 fact 是不是「换汤不换药」的近义重复。"""
    return re.sub(r"[\W_]+", "", s or "")


def _is_dup(subject: str, kind: str, fact: str, items: list) -> bool:
    """同一人、同一类、同一条属性已存在（含近义重复）就返回 True。"""
    subj = subject or "群友"
    nf = _norm(fact)
    if len(nf) < 2:
        return False
    for it in items:
        if (it.get("subject") or "群友") != subj:
            continue
        if it.get("kind") != kind:
            continue
        other = _norm(it.get("fact", ""))
        if len(other) < 2:
            continue
        if nf in other or other in nf:
            return True
    return False


def _clamp_intensity(v) -> float:
    try:
        return min(max(float(v), 0.0), 1.0)
    except (TypeError, ValueError):
        return 0.5


def _apply_ops(key: int, ops: list) -> tuple[int, int, int]:
    """执行 ADD/UPDATE/DELETE，返回 (新增, 修改, 删除) 数量。"""
    items = _archive.setdefault(str(key), [])
    by_id = {it["id"]: it for it in items if isinstance(it.get("id"), int)}
    next_id = max(by_id, default=0) + 1
    a = u = d = 0
    today = date.today().strftime("%Y-%m-%d")

    for op in ops:
        if not isinstance(op, dict):
            continue
        op_kind = str(op.get("op", "")).lower().strip()
        subject = str(op.get("subject", "") or "").strip()[:20]
        kind = str(op.get("kind", "") or "").strip().lower()
        if kind not in ("emotion", "relation"):
            kind = "emotion"
        fact = str(op.get("fact", "") or "").strip()[:80]

        if op_kind == "add" and fact:
            if _is_dup(subject, kind, fact, items):
                continue
            intensity = _clamp_intensity(op.get("intensity", 0.5)) if kind == "emotion" else None
            items.append(
                {
                    "id": next_id,
                    "subject": subject or "群友",
                    "kind": kind,
                    "fact": fact,
                    "intensity": intensity,
                    "date": today,
                }
            )
            by_id[next_id] = items[-1]
            next_id += 1
            a += 1

        elif op_kind == "update":
            try:
                target = by_id.get(int(op.get("id", -1)))
            except (TypeError, ValueError):
                target = None
            if target and fact:
                target["fact"] = fact
                if target["kind"] == "emotion" and op.get("intensity") is not None:
                    target["intensity"] = _clamp_intensity(op["intensity"])
                target["date"] = today
                u += 1

        elif op_kind == "delete":
            try:
                tid = int(op.get("id", -1))
            except (TypeError, ValueError):
                continue
            before = len(items)
            items[:] = [it for it in items if it.get("id") != tid]
            if len(items) < before:
                by_id.pop(tid, None)
                d += 1

    return a, u, d


async def consolidate(key: int) -> dict:
    """把待合并消息合并进情感档案（真实的 LLM 调用）。返回本次统计。"""
    k = str(key)
    if k in _busy:
        return {}
    buf = _pending.get(k) or []
    if not buf:
        return {}
    _busy.add(k)
    info: dict = {"key": key, "input_msgs": len(buf)}
    try:
        from plugins.ai_group import call_ai_raw  # 延迟导入，避免循环依赖

        new_msgs = "\n".join(f"{n}: {t}" for n, t, _ in buf[-120:])
        prompt = EMOTION_MERGE_PROMPT.replace("{archive}", _emotion_lines(key)).replace(
            "{new_msgs}", new_msgs
        )
        raw, usage = await call_ai_raw(prompt, temperature=0.2, max_tokens=1200)
        data = _extract_json(raw)
        ops = (data or {}).get("ops") or []
        a, u, d = _apply_ops(key, ops)

        if data is not None:
            _pending[k] = []
        _save()

        info.update(
            {
                "added": a, "updated": u, "deleted": d,
                "entries": len(_archive.get(k) or []),
                "usage": usage,
            }
        )
        nonebot.logger.info(
            f"[emotion_memory] key{key} 合并完成：+{a} 改{u} 删{d}，"
            f"现有 {len(_archive.get(k) or [])} 条"
        )
    except Exception as e:  # noqa: BLE001
        info["error"] = f"{type(e).__name__}: {e}"
        nonebot.logger.warning(f"[emotion_memory] key{key} 合并失败：{type(e).__name__}: {e}")
    finally:
        _busy.discard(k)
    return info
