# 掉线守护（Offline Guard）设计

- 日期：2026-10-21
- 状态：已确认

## 背景

QQ 小号（178743458）登录态会过期，导致 NapCat 掉线、后端 WebSocket 静默断连，机器人「不回话」。
2026-10-21 实际发生一次：13:19 `bot_offline`，直到 18:11 人工扫码才恢复。
当前代码对掉线完全无感知，需要「自动恢复 + 主动告警」。

## 目标

1. 检测到「登录失效」掉线后，自动重启 NapCat 容器尝试快速重登。
2. 重登失败（快速登录 token 也过期）时，通过 Server 酱微信推送告警，提醒人工扫码。
3. 防抖，避免反复重启死循环。

## 架构

新增插件 `plugins/offline_guard.py`，复用 NoneBot2 driver 的事件钩子与 `httpx`。
后端以 root 运行，可直接执行 `docker restart napcat`（已实测确认）。

## 触发与处理流程

```
收到 notice.bot_offline（登录失效）
        │
        ▼
   等 5 秒 → docker restart napcat（尝试快速重登）
        │
        ▼
   等 60 秒看是否重新上线（bot_connect）
        ├─ 上线了 → token 有效，自动恢复，不打扰用户
        └─ 没上线 → token 也过期，发 Server 酱告警「需要扫码」
```

- 只监听 `bot_offline`（登录失效），不监听网络瞬断（那种 NapCat 自己会重连）。
- 防抖：5 分钟内自动重启超过 3 次，停止重启、只告警。

## 配置（.env 新增）

```
# ========== 掉线守护 ==========
SERVERCHAN_SENDKEY=        # Server 酱 SendKey（sct.ftqq.com），留空 = 只重启不告警
OFFLINE_AUTO_RESTART=true  # 掉线后是否自动 docker restart napcat
```

## 组件职责

| 组件 | 职责 | 依赖 |
|------|------|------|
| 检测 | 捕获 `bot_offline` 事件 | NoneBot2 driver |
| 重登 | 异步执行 `docker restart napcat` | subprocess / asyncio |
| 判定 | 等待 `bot_connect`，超时判定重登成败 | asyncio.Event |
| 告警 | POST `https://sctapi.ftqq.com/{SENDKEY}.send` | httpx |
| 防抖 | 记录重启时间戳，限频 | 内存 deque |

## 错误处理

- `docker restart` 失败（命令不存在/容器不存在）→ 直接告警，不崩溃。
- Server 酱请求失败 → 记日志，不中断守护流程。
- SendKey 为空 → 跳过告警，仅执行自动重启 + 日志。

## 测试

1. 单元：告警函数构造、防抖限频逻辑。
2. 集成：临时改配置触发 `bot_offline`，验证「重启 → 重连/告警」全链路。
