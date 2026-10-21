"""掉线守护：QQ 登录失效导致 NapCat 掉线时，自动重启容器尝试快速重登，
失败则通过 Server 酱微信推送告警。

触发用 driver 的 on_bot_disconnect（覆盖所有断连），配合「重连成功即不打扰」
的逻辑：网络瞬断 NapCat 会自己重连，只有登录失效这种不会自动恢复的才重启/告警。
"""

import asyncio
import time
from collections import deque

import httpx
from nonebot import get_driver, logger
from nonebot.adapters.onebot.v11 import Bot

from config import get_config

driver = get_driver()

# —— 守护参数（暂不暴露成 .env，需要再改）——
AUTO_RECONNECT_WAIT = 30.0      # 网络瞬断时，等 NapCat 自己重连的秒数
RESTART_DELAY = 5.0             # docker restart 前额外缓冲
RESTART_RECONNECT_WAIT = 60.0   # restart 后等重连的秒数
DEBOUNCE_WINDOW = 300.0         # 防抖窗口（秒）
DEBOUNCE_MAX = 3                # 窗口内最多自动重启次数
NAPCHAT_CONTAINER = "napcat"

_last_connect = 0.0             # 最近一次 bot 上线时间戳（monotonic）
_restart_times: deque[float] = deque()
_guard_task: asyncio.Task | None = None


@driver.on_bot_connect
async def _on_connect(bot: Bot) -> None:
    global _last_connect
    if isinstance(bot, Bot):
        _last_connect = time.monotonic()


@driver.on_bot_disconnect
async def _on_disconnect(bot: Bot) -> None:
    global _guard_task
    if not isinstance(bot, Bot):
        return
    if _guard_task is not None and not _guard_task.done():
        return  # 已有守护在跑（可能是我们自己 restart 导致的断连）
    _guard_task = asyncio.create_task(_guard())


async def _wait_reconnect(timeout: float) -> bool:
    """timeout 秒内是否出现新的上线；有则返回 True。"""
    start = _last_connect
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _last_connect > start:
            return True
        await asyncio.sleep(1.0)
    return _last_connect > start


async def _restart_napcat() -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "restart", NAPCHAT_CONTAINER,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0:
            logger.info(f"已执行 docker restart {NAPCHAT_CONTAINER}")
            return True
        logger.warning(
            f"docker restart 失败（exit {proc.returncode}）：{stderr.decode(errors='replace')}"
        )
        return False
    except FileNotFoundError:
        logger.error("找不到 docker 命令，无法重启 NapCat")
        return False
    except Exception as e:  # noqa: BLE001
        logger.error(f"重启 NapCat 异常：{type(e).__name__}: {e}")
        return False


async def _alert(sendkey: str, reason: str) -> None:
    if not sendkey:
        logger.warning("Server 酱 SendKey 未配置，跳过告警")
        return
    guard = get_config().offline_guard
    desp = f"{reason}\n\n时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"
    if guard.webui_url:
        desp += f"\n扫码：{guard.webui_url}"
        if guard.webui_token:
            desp += f"\nToken：{guard.webui_token}"
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, data={"title": "蓝色大肥鱼掉线了", "desp": desp})
        if r.status_code == 200:
            logger.info("Server 酱告警已发送")
        else:
            logger.warning(f"Server 酱返回 {r.status_code}：{r.text[:200]}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"Server 酱告警失败：{type(e).__name__}: {e}")


async def _guard() -> None:
    cfg = get_config()
    guard = cfg.offline_guard
    now = time.monotonic()
    while _restart_times and now - _restart_times[0] > DEBOUNCE_WINDOW:
        _restart_times.popleft()

    # 第一阶段：可能是网络瞬断，先等 NapCat 自己重连
    if await _wait_reconnect(AUTO_RECONNECT_WAIT):
        logger.info("掉线后自动重连成功，无需干预")
        return

    # 第二阶段：自动重启尝试快速重登
    if guard.auto_restart and len(_restart_times) < DEBOUNCE_MAX:
        _restart_times.append(now)
        await asyncio.sleep(RESTART_DELAY)
        if await _restart_napcat() and await _wait_reconnect(RESTART_RECONNECT_WAIT):
            logger.info("掉线后自动重启 NapCat 成功，已恢复")
            return
        await _alert(guard.serverchan_sendkey, "自动重启后仍未恢复，请扫码重新登录")
        return

    if guard.auto_restart:
        await _alert(
            guard.serverchan_sendkey,
            f"{int(DEBOUNCE_WINDOW // 60)} 分钟内掉线超过 {DEBOUNCE_MAX} 次，"
            f"已停止自动重启，请检查",
        )
    else:
        await _alert(guard.serverchan_sendkey, "检测到掉线（未开启自动重启），请扫码重新登录")
