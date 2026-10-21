"""主动发消息插件：主人私聊偶尔主动关心/闲聊一句。

两段式决策（先过确定性闸门，命中才调一次大模型，默认闭嘴）：
1. `PROACTIVE_ENABLED=true` 才跑（默认关，opt-in）
2. 填了 `MASTER_QQ` 才有对象（只发主人私聊，群聊不碰）
3. 静默时段（默认 23:00~9:00）不发
4. 当日已发 ≥ `PROACTIVE_MAX_PER_DAY` 条不发
5. 主人最近 `PROACTIVE_COOLDOWN_HOURS` 小时内回过话就跳过（有回应就少打扰）
6. 每次 tick 随机中签（`PROACTIVE_HIT_RATE` 概率），没中签不发

内容 = 主人的情感档案 + 事实档案 + 「多久没聊」作为上下文，让鲸鱼娘按人设
结合记忆关心一句或随便闲聊；1~2 句、≤160 字、不说「自动提醒/随机想起」这类元话术。

配置都在 config.py / .env（PROACTIVE_* 一组），这里不改。
"""
import asyncio
import datetime
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import nonebot  # noqa: E402
from nonebot import on_message, require  # noqa: E402

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler  # noqa: E402

from nonebot.adapters.onebot.v11 import Message, PrivateMessageEvent  # noqa: E402

from config import get_config  # noqa: E402
from plugins.ai_group import call_ai, split_reply  # noqa: E402
from plugins import emotion_memory as etm  # noqa: E402
from plugins import long_memory as ltm  # noqa: E402
from plugins import reply_log  # noqa: E402

cfg = get_config()

DATA_DIR = ROOT / "memory"
STATE_FILE = DATA_DIR / "主动发消息.json"

# 内存态：last_master 只在进程内记（重启归零，可接受）；sent/date 落盘防重启后超发
_track = {"last_master": 0.0, "sent": 0, "date": ""}


def _load_state() -> None:
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            _track["sent"] = int(data.get("sent") or 0)
            _track["date"] = str(data.get("date") or "")
    except Exception:  # noqa: BLE001
        pass


def _save_state() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps({"date": _track["date"], "sent": _track["sent"]},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:  # noqa: BLE001
        nonebot.logger.warning(f"[proactive] 保存状态失败：{e}")


_load_state()


# 主人发来的私聊都记一下时间，供「减速窗口」判断（有回应就少打扰）
_probe = on_message(priority=1, block=False)


@_probe.handle()
async def _(event: PrivateMessageEvent):
    if cfg.master_qq and event.user_id == cfg.master_qq:
        _track["last_master"] = time.time()


def _in_quiet(hour: int) -> bool:
    """当前小时是否落在静默时段（支持跨午夜，如 23:00~9:00）。"""
    qs, qe = cfg.proactive.quiet_start, cfg.proactive.quiet_end
    if qs < qe:
        return qs <= hour < qe
    return hour >= qs or hour < qe


async def _generate(now: datetime.datetime) -> str:
    """过了闸门才调：用主人的情感+事实档案 + 「多久没聊」生成一句主动问候。"""
    fact = ltm.archive_text(cfg.master_qq)
    emotion = etm.emotion_text(cfg.master_qq)
    if _track["last_master"]:
        silent = max(0.0, (now.timestamp() - _track["last_master"]) / 3600)
        note = f"主人已经 {silent:.0f} 小时没找你说话了"
    else:
        note = "主人有阵子没跟你聊天了"
    instruction = (
        f"（{note}。请你主动以鲸鱼娘的口吻给主人发一条消息，关心一下或者随便聊聊。"
        "1~2 句话，不超过 160 字，自然一点，别说「自动提醒」「随机想起」「系统」这类话，"
        "就像你突然想主人了。）"
    )
    return await call_ai(instruction, long_term=fact, emotion=emotion)


async def _send(text: str) -> None:
    """把文案发给主人私聊，带换行按行拆发，和群聊回复一个规则。"""
    lines = split_reply(text, cfg.reply.split_lines)
    chunks: list[Message] = [Message(ln) for ln in lines] or [Message("")]
    try:
        bot = nonebot.get_bot()
        for i, msg in enumerate(chunks):
            await bot.send_private_msg(user_id=cfg.master_qq, message=msg)
            if i < len(chunks) - 1:
                await asyncio.sleep(cfg.reply.line_gap)
        reply_log.record(group_id=cfg.master_qq, speaker="", user_msg="主动发消息",
                         reply=text, mood="", kind="proactive")
    except Exception as e:  # noqa: BLE001
        nonebot.logger.warning(f"[proactive] 发送失败：{e}")


@scheduler.scheduled_job("interval", minutes=cfg.proactive.tick_minutes, id="proactive_tick")
async def proactive_tick() -> None:
    if not cfg.proactive.enabled:
        return
    if not cfg.master_qq:
        nonebot.logger.warning("[proactive] 没填 MASTER_QQ，主动发消息跳过")
        return
    now = datetime.datetime.now()
    if _in_quiet(now.hour):
        return
    today = now.strftime("%Y-%m-%d")
    if _track["date"] != today:
        _track["date"] = today
        _track["sent"] = 0
    if _track["sent"] >= cfg.proactive.max_per_day:
        return
    if _track["last_master"] and \
            (now.timestamp() - _track["last_master"]) < cfg.proactive.cooldown_hours * 3600:
        return
    if random.random() > cfg.proactive.hit_rate:
        return

    text = await _generate(now)
    if not text or text.startswith("[AI "):
        return
    await _send(text)
    _track["sent"] += 1
    _save_state()
