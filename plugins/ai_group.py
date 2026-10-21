import asyncio
import base64
import random
import re
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from nonebot import on_message  # noqa: E402
from nonebot.adapters.onebot.v11 import (  # noqa: E402
    GroupMessageEvent,
    PrivateMessageEvent,
    Message,
    MessageSegment,
)

from config import get_config, redact  # noqa: E402
from plugins import emotion_memory as etm  # noqa: E402
from plugins import long_memory as ltm  # noqa: E402
from plugins import reply_log  # noqa: E402
from plugins import tts  # noqa: E402

cfg = get_config()

# —— 群聊上下文缓存（让机器人能"记住"刚才群里在聊什么）——
# 参数都在 config.py / .env 里（HISTORY_MAX / HISTORY_USE / MAX_LEN_PER_MSG / AT_WINDOW）
_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=cfg.context.history_max))

# 每个群"最近一次被 @"的时间戳：@ 之后 AT_WINDOW 秒内发的图，也当成是发给它的
_last_at: dict[int, float] = {}

# 看图时随机挑一个"吐槽角度"注入，逼它每次换个说法（不然模型爱复读同一个梗）
IMAGE_ANGLES = [
    "只挑图里最搞笑、最怪的一个细节说",
    "阴阳怪气一下发图的人",
    "把图里的东西跟群友（或者你自己）对比",
    "假装一时没看懂，随便糊弄一句",
    "夸张地夸上天，或者夸张地嫌弃",
    "联想到一个完全不相关的梗",
    "就发个怪叫或者两三个字（比如「？」「草」「好家伙」「逆天」）",
    "用一个很欠揍的语气评价这张图",
    "假装这图是你自己发的，然后开始瞎解释",
    "从图里挑个东西胡乱开个脑洞",
]

# 群聊不再只靠 @ 触发：消息里出现这些「鱼相关」关键词也主动搭话
FISH_KEYWORDS = (
    "大肥鱼", "鲸鱼娘", "鲸鱼", "蓝鲸", "肥鱼", "胖鱼", "鱼", "鲸",
    "白饭", "米饭", "女仆", "主人",
)


def _is_fish_related(text: str) -> bool:
    return any(kw in text for kw in FISH_KEYWORDS)

# 人设 prompt 在仓库根目录的 persona.md 里（改完要重启后端）
SYSTEM_PROMPT = cfg.persona.prompt
NICKNAME = cfg.persona.nickname

ai = on_message(priority=10, block=False)
collector = on_message(priority=1, block=False)

# 上游表示"服务繁忙/限流"的错误码，遇到这些值得自动重试
BUSY_CODES = {"1301", "1302", "1303", "1304", "1305"}

# 联网检索标记：模型需要实时/最新信息时输出 [搜索:关键词]，主人贴链接读正文时输出 [抓取:url]
SEARCH_TAG = re.compile(r"\[搜索[:：]\s*([^\]]+)\]")
FETCH_TAG = re.compile(r"\[抓取[:：]\s*([^\]]+)\]")

# 上一次实际调用的 token 用量（排查 / 统计用）
LAST_USAGE: dict = {}


# ------------------------------------------------------------------ 底层调用

async def _chat(
    messages: list,
    temperature: float | None = None,
    max_tokens: int | None = None,
    retries: int | None = None,
) -> tuple[str, dict]:
    """底层调用：返回 (回复文本, usage)。带限流退避重试。

    报错文本统一过 `redact()` —— 万一上游把请求回显了，密钥也不会写进日志。
    """
    temperature = cfg.ai.temperature if temperature is None else temperature
    retries = cfg.ai.max_retries if retries is None else retries
    last_err = ""
    for attempt in range(retries):
        try:
            payload = {
                "model": cfg.ai.model,
                "messages": messages,
                "thinking": {"type": "disabled"},
                "temperature": temperature,
            }
            if max_tokens:
                payload["max_tokens"] = max_tokens
            async with httpx.AsyncClient(timeout=cfg.ai.timeout) as c:
                r = await c.post(
                    cfg.ai.endpoint(),
                    headers={
                        "Authorization": f"Bearer {cfg.ai.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            data = r.json()

            if data.get("choices"):
                usage = data.get("usage") or {}
                LAST_USAGE.clear()
                LAST_USAGE.update(usage)
                return data["choices"][0]["message"]["content"].strip(), usage

            err = data.get("error", {}) or {}
            code = str(err.get("code", ""))
            last_err = redact(f"HTTP {r.status_code} code={code} {err.get('message') or str(data)[:300]}")

            if (code in BUSY_CODES or r.status_code >= 500) and attempt < retries - 1:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            return f"[AI 出错] {last_err}", {}

        except Exception as e:
            last_err = redact(f"{type(e).__name__}: {e}")
            if attempt < retries - 1:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue

    return f"[AI 调用失败] {last_err}", {}


async def call_ai_raw(prompt: str, temperature: float = 0.95, max_tokens: int | None = None):
    """给内部工具（如长期记忆整理）用的简化调用：单条 user 消息。返回 (文本, usage)。"""
    return await _chat([{"role": "user", "content": prompt}], temperature, max_tokens)


async def web_search(query: str) -> str:
    """调 DeepSeek 官方搜索端点，返回拼好的结果文本；失败/无结果返回空串。"""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{cfg.ai.base_url.rstrip('/')}/v1/tools/search",
                headers={
                    "Authorization": f"Bearer {cfg.ai.api_key}",
                    "Content-Type": "application/json",
                },
                json={"query": query, "count": 5},
            )
        if r.status_code != 200:
            return ""
        results = (r.json().get("results") or [])[:5]
        if not results:
            return ""
        lines = []
        for it in results:
            title = it.get("title") or ""
            url = it.get("url") or ""
            summary = it.get("summary") or it.get("snippet") or ""
            date = it.get("date") or ""
            line = f"- {title}\n  {summary}"
            if date:
                line += f"\n  时间：{date}"
            line += f"\n  来源：{url}"
            lines.append(line)
        return "\n".join(lines)
    except Exception:
        return ""


async def web_fetch(url: str) -> str:
    """调 DeepSeek 官方抓取端点，返回标题+正文；失败/空正文返回空串。"""
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"{cfg.ai.base_url.rstrip('/')}/v1/tools/fetch",
                headers={
                    "Authorization": f"Bearer {cfg.ai.api_key}",
                    "Content-Type": "application/json",
                },
                json={"url": url},
            )
        if r.status_code != 200:
            return ""
        data = r.json()
        content = (data.get("content") or "").strip()
        if not content:
            return ""
        title = data.get("title") or ""
        return f"标题：{title}\n正文：\n{content[:4000]}"
    except Exception:
        return ""


async def _maybe_search(
    reply: str,
    user_msg: str,
    context: str,
    images: list[str],
    long_term: str,
    emotion: str = "",
) -> str:
    """模型回复带 [搜索:xx]/[抓取:url] 时，执行检索并把结果回填、二次问模型。

    最多 2 轮，防止来回搜卡死；最终残留的标记会被剥掉，不会发到群里。
    """
    for _ in range(2):
        s = SEARCH_TAG.search(reply)
        f = FETCH_TAG.search(reply)
        if not s and not f:
            break
        gathered = []
        if s:
            gathered.append(await web_search(s.group(1).strip()))
        if f:
            gathered.append(await web_fetch(f.group(1).strip()))
        info = "\n\n".join(x for x in gathered if x)
        if not info:
            break
        followup = (
            f"【原始请求】{user_msg}\n\n"
            "【我刚帮你查到的资料，据此回答，这次别再用 [搜索:/[抓取:] 标记】：\n"
            f"{info}"
        )
        reply = await call_ai(followup, context, images, long_term, emotion)
    reply = SEARCH_TAG.sub("", reply)
    reply = FETCH_TAG.sub("", reply)
    return reply.strip()


async def _explicit_search(user_msg: str, who: str) -> str | None:
    """消息以 @@ 开头时，把后面的内容拿去搜；返回改写后的 user_msg，非 @@ 返回 None。"""
    if not user_msg.startswith("@@"):
        return None
    q = user_msg[2:].strip()
    if not q:
        return f"{who}说：{user_msg}"
    info = await web_search(q)
    if info:
        return f"{who}用 @@ 让你查「{q}」，下面是搜到的资料，请你据此回答：\n{info}"
    return f"{who}用 @@ 让你查「{q}」，但没搜到东西，实话实说搜不到。"


# base64 编解码命令：`编码/加密/b64 xxx` 编码，`解码/解密/b64d xxx` 解码
BASE64_ENC = re.compile(r"^(?:b64e|b64|编码|加密)\s+(.+)$", re.IGNORECASE)
BASE64_DEC = re.compile(r"^(?:b64d|unb64|解码|解密)\s+(.+)$", re.IGNORECASE)


def _try_base64(text: str) -> tuple[bool, str] | None:
    """base64 编解码命令。命中返回 (成功, 结果)，非命令返回 None。"""
    t = text.strip()
    m = BASE64_ENC.match(t)
    if m:
        raw = m.group(1).strip()
        return (True, base64.b64encode(raw.encode("utf-8")).decode("ascii"))
    m = BASE64_DEC.match(t)
    if m:
        raw = m.group(1).strip()
        try:
            return (True, base64.b64decode(raw, validate=True).decode("utf-8"))
        except Exception:
            return (False, "解码失败：这不是合法的 base64 字符串")
    return None


# 抓图表情包命令：`抓图 [数量] <主题>`，例如「抓图 10 鲸鱼娘」/「抓图 鲸鱼娘」
CRAWL_RE = re.compile(r"^(?:抓图|抓表情包)\s*(?:(\d{1,3})\s*张?\s+)?(.+)$")


async def _crawl_command(user_msg: str) -> str | None:
    """抓图命令。命中就执行并返回要发出去的结果；非命令返回 None。"""
    m = CRAWL_RE.match(user_msg.strip())
    if not m:
        return None
    count = min(max(int(m.group(1)) if m.group(1) else 10, 1), 30)
    topic = m.group(2).strip()
    await ai.send(Message(MessageSegment.text(f"收到，去给主人抓「{topic}」的表情包，稍等…")))
    import plugins.emoji_crawl as ec  # 惰性导入，避免和 emoji_crawl 相互引用
    try:
        return await ec.crawl(topic, count)
    except Exception as e:  # noqa: BLE001
        return f"抓图出错了：{type(e).__name__}: {e}"


# 存图命令：主人发图 + 文字里带情绪词，把图存进 表情包/<情绪>/（仅主人）
SAVE_WORDS = ("存", "放", "收", "归档", "保存", "归类")


async def _try_save_emoji(user_msg: str, images: list[str]) -> str | None:
    """存图命令。命中就存图并返回结果；非命令返回 None。"""
    if not images:
        return None
    mood = next((m for m in cfg.emoji.moods if m in user_msg), None)
    if mood is None or not any(w in user_msg for w in SAVE_WORDS):
        return None
    import plugins.emoji_crawl as ec  # 惰性导入，避免和 emoji_crawl 相互引用
    ok = 0
    for u in images[:5]:
        try:
            if await ec.save_image(u, mood):
                ok += 1
        except Exception:  # noqa: BLE001
            continue
    if ok:
        return f"收好了，{ok} 张图存进「{mood}」啦。"
    return "这张图没存进去（下载失败或格式不对），换张再试试。"


_WEEKDAYS = "一二三四五六日"


def _now_text() -> str:
    lt = time.localtime()
    return f"{lt.tm_year}-{lt.tm_mon:02d}-{lt.tm_mday:02d} {lt.tm_hour:02d}:{lt.tm_min:02d}（周{_WEEKDAYS[lt.tm_wday]}）"


def _now_hm() -> str:
    return time.strftime("%H:%M")


# 余额查询：带 5 分钟缓存，别每条消息都去查一次
_balance_cache: dict = {"ts": 0.0, "value": None}


async def get_balance(force: bool = False) -> float | None:
    """查 DeepSeek 账户余额（元）。失败或非 200 返回上次缓存/None。force=True 跳过缓存。"""
    now = time.time()
    if not force and _balance_cache["ts"] and now - _balance_cache["ts"] < 300:
        return _balance_cache["value"]
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(
                f"{cfg.ai.base_url.rstrip('/')}/user/balance",
                headers={"Authorization": f"Bearer {cfg.ai.api_key}"},
            )
        if r.status_code != 200:
            return _balance_cache["value"]
        total = 0.0
        for info in (r.json().get("balance_infos") or []):
            try:
                total += float(info.get("total_balance", 0))
            except (TypeError, ValueError):
                continue
        _balance_cache["ts"] = now
        _balance_cache["value"] = total
        return total
    except Exception:
        return _balance_cache["value"]


async def call_ai(
    user_msg: str,
    context: str = "",
    images: list[str] | None = None,
    long_term: str = "",
    emotion: str = "",
    retries: int | None = None,
) -> str:
    """调用大模型。

    - context：最近群聊记录
    - images：本次消息里的图片 URL（多模态看图）
    - long_term：长期记忆档案（**紧跟 system prompt**，位置固定便于命中上下文缓存）
    - emotion：情感档案（紧跟事实档案之后、上下文之前，位置固定）
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if long_term:
        # 固定位置：system prompt 之后立刻放，且内容变动不频繁 → 有利于服务端前缀缓存
        messages.append({"role": "user", "content": long_term})
    if emotion:
        messages.append({"role": "user", "content": emotion})
    if context:
        messages.append(
            {
                "role": "user",
                "content": "【以下是群里最近的聊天记录，仅供你理解上下文，不用逐条回应】：\n" + context,
            }
        )
    # 告诉它当前时间，免得它瞎编「大半夜」「晚上好」这种没依据的时点
    messages.append({"role": "user", "content": f"【当前时间：{_now_text()}】"})
    if images:
        content: list = [{"type": "text", "text": user_msg or "（我发了张图，你瞅瞅）"}]
        for u in images:
            content.append({"type": "image_url", "image_url": {"url": u}})
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": user_msg})

    text, _ = await _chat(messages, retries=retries)
    return text


def split_reply(text: str, split: bool = True) -> list[str]:
    """把回复正文切成「要分开发的那几段」。

    人设里「祝福语正经一句 + 皮一句要分成两句」这类要求靠换行表达，
    所以带换行的回复按行拆成多条发（像真人打字）；单行回复 = 一段，行为不变。
    `split=False`（`.env` 的 `REPLY_SPLIT_LINES=false`）时拼回一段，退回旧行为。
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if split:
        return lines
    return [" ".join(lines)] if lines else []


def build_chunks(lines: list[str], img: Path | None, mention: list) -> list[list]:
    """把「分好的行 + 可选表情包 + 可选 @」拼成要逐条发出去的消息段列表。

    - `mention` 传空列表（默认）= 回复不带 @；传 `[MessageSegment.at(...)]` 则首行加 @。
    - 带 @ 时正文前补一个空格跟 @ 隔开；不带 @ 就直接发正文。
    - 图片（表情包）永远贴在**最后一条**上；只有图没文字时单独发一条。
    """
    chunks: list[list] = []
    if lines:
        head = MessageSegment.text((" " + lines[0]) if mention else lines[0])
        chunks.append(mention + [head])
        chunks += [[MessageSegment.text(ln)] for ln in lines[1:]]
    if img:
        if chunks:
            chunks[-1].append(MessageSegment.image(img))
        else:
            chunks.append(mention + [MessageSegment.image(img)])   # 只有图，没文字
    return chunks


def pick_emoji(mood: str) -> Path | None:
    """按情绪挑一张表情包；对应文件夹没有就退而从「表情包」根目录随机挑。"""
    emoji = cfg.emoji
    if not emoji.dir.is_dir():
        return None
    for folder in (emoji.dir / mood, emoji.dir):
        if folder.is_dir():
            files = [f for f in folder.iterdir() if f.suffix.lower() in emoji.exts and f.is_file()]
            if files:
                return random.choice(files)
    return None


# ------------------------------------------------------------------ 消息处理

async def _reply_and_send(
    *,
    user_msg: str,
    raw_msg: str,
    images: list[str],
    context: str,
    mem_key: int,
    speaker: str,
    mention: list,
    started: float,
    is_master: bool = False,
) -> str | None:
    """调模型 → 拆 [表情:xxx] 标记 → 分句发送 → 语音 → 落日志。

    群聊和私聊共用这一段：差别只在入参（context / mention / mem_key 不同）。
    返回回复正文；什么都没发（既没文字也没图）时返回 None。
    """
    # 余额：主人可查（注入真实余额，让模型自己判断主人是不是在问、再自然报数），
    # 余额见底时对所有人按各自口吻顺带讨充值
    balance = await get_balance()
    if balance is not None:
        if is_master:
            if balance <= cfg.balance_alert_yuan:
                user_msg = (
                    f"【你的 DeepSeek 账户余额只剩 {balance:.2f} 元，快见底了。"
                    "主人问起余额/token/额度还剩多少，就如实报这个数，顺便撒娇求投喂（白米饭快没了）。】\n"
                    + user_msg
                )
            else:
                user_msg = (
                    f"【你的 DeepSeek 账户余额现在是 {balance:.2f} 元。"
                    "主人要是问起余额/token/额度还剩多少，就如实报这个数；没问就别主动提。】\n"
                    + user_msg
                )
        elif balance <= cfg.balance_alert_yuan:
            user_msg = (
                f"【系统提示：你的 API 余额只剩 {balance:.2f} 元，快见底了。"
                "回复时嘴硬一句「没饭吃了」讨充值，别硬塞、别每条消息都重复。】\n"
                + user_msg
            )

    long_term = ltm.archive_text(mem_key)
    emotion = etm.emotion_text(mem_key)
    reply = await call_ai(user_msg, context, images, long_term, emotion)
    reply = await _maybe_search(reply, user_msg, context, images, long_term, emotion)

    # 提取并移除回复里的 [表情:xxx] 标记
    mood = ""
    m = re.search(r"\[表情[:：]\s*([^\]]*)\]", reply)
    if m:
        mood = m.group(1).strip()
        reply = re.sub(r"\[表情[:：]\s*[^\]]*\]", "", reply).strip()

    img = pick_emoji(mood) if mood else None
    if not reply and not img:  # 既没文字也没图 → 别发空消息
        reply_log.record(
            group_id=mem_key, speaker=speaker, user_msg=raw_msg, reply="", mood=mood,
            images=len(images), latency_ms=int((time.perf_counter() - started) * 1000),
            error="模型没给出可用内容",
        )
        return None

    chunks = build_chunks(split_reply(reply, cfg.reply.split_lines), img, mention)
    for i, seg in enumerate(chunks):
        await ai.send(Message(seg))
        if i < len(chunks) - 1:
            await asyncio.sleep(cfg.reply.line_gap)   # 别刷屏式连发

    # 文字发完之后，再补发一条语音把回复读出来（出错静默跳过，不影响文字）
    if cfg.tts.enabled and reply and not reply.startswith("[AI "):
        audio = await tts.synthesize(reply)
        if audio:
            await ai.send(Message(MessageSegment.record(file=str(audio))))

    bad: list[str] = []
    if reply.startswith("[AI "):
        bad.append("API_ERROR")
    reply_log.record(
        group_id=mem_key, speaker=speaker, user_msg=raw_msg, reply=reply, mood=mood,
        images=len(images), latency_ms=int((time.perf_counter() - started) * 1000),
        error=",".join(bad),
    )
    return reply


@collector.handle()
async def _(event: GroupMessageEvent):
    # 群白名单：ALLOW_GROUPS 留空 = 所有群都理
    if cfg.allow_groups and event.group_id not in cfg.allow_groups:
        return

    # 缓存群里每一条消息（不回复），供 AI 参考上下文
    text = event.get_plaintext().strip()
    has_img = any(seg.type == "image" for seg in event.get_message())
    if not text and not has_img:
        return
    name = event.sender.card or event.sender.nickname or str(event.user_id)
    _history[event.group_id].append((name, text or "[发了一张图片]", False, _now_hm()))

    # 同时丢进长期记忆 + 情感记忆的"待合并"缓冲（攒够了后台自动合并成档案）
    ltm.add_pending(event.group_id, name, text or "[发了一张图片]")
    etm.add_pending(event.group_id, name, text or "[发了一张图片]")


@collector.handle()
async def _(event: PrivateMessageEvent):
    # 私聊也记进长期记忆 + 短期上下文：攒够 MEM_CONSOLIDATE_EVERY 条自动合并，每天 12 点兜底
    text = event.get_plaintext().strip()
    has_img = any(seg.type == "image" for seg in event.get_message())
    if not text and not has_img:
        return
    name = event.sender.nickname or str(event.user_id)
    _history[event.user_id].append((name, text or "[发了一张图片]", False, _now_hm()))
    ltm.add_pending(event.user_id, name, text or "[发了一张图片]")
    etm.add_pending(event.user_id, name, text or "[发了一张图片]")


@ai.handle()
async def _(event: GroupMessageEvent):
    if cfg.allow_groups and event.group_id not in cfg.allow_groups:
        return

    gid = event.group_id
    now = time.time()
    started = time.perf_counter()

    user_msg = event.get_plaintext().strip()
    # 收集本条消息里的图片
    images = [
        seg.data.get("url") or seg.data.get("file")
        for seg in event.get_message()
        if seg.type == "image"
    ]
    images = [u for u in images if u]

    # base64 编解码：仅主人可用，确定性命令，不进 AI
    res = _try_base64(user_msg)
    if res is not None:
        if bool(cfg.master_qq) and event.user_id == cfg.master_qq:
            ok, result = res
            await ai.send(Message(MessageSegment.text(result)))
        return

    # 抓图表情包：仅主人可用
    if bool(cfg.master_qq) and event.user_id == cfg.master_qq:
        crawl_out = await _crawl_command(user_msg)
        if crawl_out is not None:
            await ai.send(Message(MessageSegment.text(crawl_out)))
            return

    # 存图表情包：仅主人，发图 + 指定情绪
    if bool(cfg.master_qq) and event.user_id == cfg.master_qq:
        save_out = await _try_save_emoji(user_msg, images)
        if save_out is not None:
            await ai.send(Message(MessageSegment.text(save_out)))
            return

    if event.is_tome():
        _last_at[gid] = now

    # 回应条件：①被 @；②刚被 @ 过（AT_WINDOW 秒内）又发了图；③消息里提到「鱼」相关关键词；④@@ 显式搜索
    recent_at = (now - _last_at.get(gid, 0)) < cfg.context.at_window
    if not (event.is_tome() or (images and recent_at) or _is_fish_related(user_msg) or user_msg.startswith("@@")):
        return
    if not user_msg and not images:
        return

    speaker = event.sender.card or event.sender.nickname or str(event.user_id)
    raw_msg = user_msg
    is_master = bool(cfg.master_qq) and event.user_id == cfg.master_qq
    who = "主人" if is_master else speaker

    # 取该群最近记录，排除最后一条（就是当前这条消息），再截取最近 HISTORY_USE 条
    hist = list(_history[gid])[:-1][-cfg.context.history_use:]
    context = "\n".join(
        f"[{ts}] {f'{NICKNAME}(我自己)' if is_bot else name}: {text[:cfg.context.max_len_per_msg]}"
        for name, text, is_bot, ts in hist
    )

    # 把"当前是谁在说话"标清楚，免得它认错人、前言不搭后语
    explicit = await _explicit_search(user_msg, who)
    if explicit is not None:
        user_msg = explicit
    elif images:
        angle = random.choice(IMAGE_ANGLES)
        cur = f"（{who}发了张图。本次吐槽角度：{angle}）"
        if user_msg:
            cur += f"\n{who}还说：{user_msg}"
        user_msg = cur
    else:
        user_msg = f"{who}说：{user_msg}"

    # 群聊默认不带 @（`REPLY_MENTION=false`）——被 @ 才会回，但回的时候不艾特对方，更像群友搭话。
    mention: list = [MessageSegment.at(event.user_id)] if cfg.reply.mention else []
    reply = await _reply_and_send(
        user_msg=user_msg, raw_msg=raw_msg, images=images, context=context,
        mem_key=gid, speaker=speaker, mention=mention, started=started,
        is_master=is_master,
    )

    # 把自己的回复也存进上下文
    if reply is not None:
        _history[gid].append(("", reply or "[只发了一张表情包]", True, _now_hm()))


@ai.handle()
async def _(event: PrivateMessageEvent):
    # 私聊没有「被 @」的概念，任何一条都算跟它说话，直接回。
    started = time.perf_counter()

    user_msg = event.get_plaintext().strip()
    images = [
        seg.data.get("url") or seg.data.get("file")
        for seg in event.get_message()
        if seg.type == "image"
    ]
    images = [u for u in images if u]
    if not user_msg and not images:
        return

    # base64 编解码：仅主人可用，确定性命令，不进 AI
    res = _try_base64(user_msg)
    if res is not None:
        if bool(cfg.master_qq) and event.user_id == cfg.master_qq:
            ok, result = res
            await ai.send(Message(MessageSegment.text(result)))
        return

    # 抓图表情包：仅主人可用
    if bool(cfg.master_qq) and event.user_id == cfg.master_qq:
        crawl_out = await _crawl_command(user_msg)
        if crawl_out is not None:
            await ai.send(Message(MessageSegment.text(crawl_out)))
            return

    # 存图表情包：仅主人，发图 + 指定情绪
    if bool(cfg.master_qq) and event.user_id == cfg.master_qq:
        save_out = await _try_save_emoji(user_msg, images)
        if save_out is not None:
            await ai.send(Message(MessageSegment.text(save_out)))
            return

    speaker = event.sender.nickname or str(event.user_id)
    raw_msg = user_msg
    is_master = bool(cfg.master_qq) and event.user_id == cfg.master_qq
    who = "主人" if is_master else speaker

    # 私聊短期上下文：最近几条（排除当前这条）；长期记忆/日志用 user_id 当 key
    hist = list(_history[event.user_id])[:-1][-cfg.context.history_use:]
    context = "\n".join(
        f"[{ts}] {f'{NICKNAME}(我自己)' if is_bot else name}: {text[:cfg.context.max_len_per_msg]}"
        for name, text, is_bot, ts in hist
    )
    explicit = await _explicit_search(user_msg, who)
    if explicit is not None:
        user_msg = explicit
    elif images:
        angle = random.choice(IMAGE_ANGLES)
        cur = f"（{who}发了张图。本次吐槽角度：{angle}）"
        if user_msg:
            cur += f"\n{who}还说：{user_msg}"
        user_msg = cur
    else:
        user_msg = f"{who}说：{user_msg}"

    reply = await _reply_and_send(
        user_msg=user_msg, raw_msg=raw_msg, images=images, context=context,
        mem_key=event.user_id, speaker=speaker, mention=[], started=started,
        is_master=is_master,
    )
    if reply is not None:
        _history[event.user_id].append(("", reply or "[只发了一张表情包]", True, _now_hm()))
