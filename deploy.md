# 蓝色大肥鱼 · Linux 服务器部署指南 🐋

把 QQ 群机器人「蓝色大肥鱼」部署到一台 Linux 云服务器（**2 核 4G · Ubuntu 22.04**）的完整步骤。

> 本文面向「把机器人放到服务器上 7×24 挂机」的场景。如果你只是在自己 Windows 电脑上跑着玩，看
> [README.md](README.md) 和 [docs/安装部署.md](docs/安装部署.md) 即可，不用上服务器。

---

## 1. 架构总览

机器人由三块拼成：**协议端（NapCat）** 负责接管一个 QQ 小号收发消息，**应用端（NoneBot2 后端）**
负责逻辑，**大模型 API** 负责「脑子」。

```
┌────────┐     ┌─────────┐   反向 WebSocket(客户端主动连)   ┌──────────────────┐     ┌────────────┐
│  QQ群  │ ──▶ │ NapCat  │ ─────────────────────────────▶ │  NoneBot2 后端     │ ──▶ │ DeepSeek   │
│ 群友   │ ◀── │ (协议端) │  ws://127.0.0.1:8080/onebot/v11/ws │  bot.py :8080     │ ◀── │  API       │
└────────┘     └─────────┘    (NapCat 主动连后端，不是后端连它)  └──────────────────┘     └────────────┘
```

**为什么是「反向 WS」？** NapCat 是 OneBot 的**反向 WebSocket 客户端**——它自己主动去连后端的
`ws://127.0.0.1:8080/onebot/v11/ws`。这样后端只需要「坐在那里监听 8080」，不用知道 NapCat 在哪、
连了几个，也天然支持断线自动重连。方向别搞反：是 **NapCat → 后端**，不是后端去连 NapCat。

**语音链路（可选，见第 6 节）**：

```
NoneBot2 后端 ──(HTTP POST 参考音频+文本)──▶ GPT-SoVITS API (:9880，跑在带 N 卡的机器上) ──▶ 音频 ──▶ 后端 ──▶ QQ 语音
```

> 2 核 4G 云服务器**没有 GPU**，跑不动 GPT-SoVITS 本地合成，语音必须放到另一台带 N 卡的机器上（详见第 6 节）。

---

## 2. 服务器准备

用一台 Ubuntu 22.04（自带 Python 3.10，满足 ≥3.10 的要求）。先更新系统并装齐依赖：

```bash
sudo apt update && sudo apt upgrade -y

# Python + pip + 虚拟环境
sudo apt install -y python3 python3-venv python3-pip

# ffmpeg：语音(TTS)要处理/转码音频；不打算用语音也可以不装，装了没坏处
sudo apt install -y ffmpeg

# 确认版本
python3 --version      # 应为 3.10 或更高
ffmpeg -version | head -1
```

> **为什么用 venv？** 系统 Python 是给系统工具用的，直接往里 `pip install` 会污染环境、还可能和系统
> 依赖冲突。venv 给这个项目一个独立的 Python 环境，删掉重来也干净。

---

## 3. 部署后端

### 3.1 拿到代码

二选一：有 git 就 clone（更省事），没有就本地上传（`scp` / `sftp` / 宝塔都行）。

```bash
# 方式 A：git clone
git clone https://github.com/chennianlaobai/qq-ai-bot.git
cd qq-ai-bot

# 方式 B：本地上传（Windows 本地把整个项目文件夹打包后 scp 上去）
#   scp -r "D:/Big fat whale/qq-ai-bot"  user@你的服务器IP:/opt/
# 然后：cd /opt/qq-ai-bot
```

> 本文后续统一假设项目放在 `/opt/qq-ai-bot`。放别处请自行替换路径。

### 3.2 建虚拟环境 + 装依赖

```bash
cd /opt/qq-ai-bot
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

> `-i https://pypi.tuna.tsinghua.edu.cn/simple` 是国内清华镜像，服务器在国内能明显提速。

### 3.3 配置 .env

```bash
cp .env.example .env
nano .env     # 或 vi / vim
```

必填项（模型服务三件套，换厂商只改这三行）：

```ini
AI_API_KEY=sk-xxxxxxxxxxxxxxxx          # 你的 API Key
AI_BASE_URL=https://api.deepseek.com    # DeepSeek 官方 OpenAI 兼容接口
AI_MODEL=deepseek-v4-flash              # 模型名（以官方最新为准）
```

**DeepSeek 官方 API 说明**：官方 OpenAI 兼容接口的 `base_url` 是 `https://api.deepseek.com`（部分工具
也兼容 `https://api.deepseek.com/v1`），模型填 `deepseek-v4-flash`（也见 `deepseek-chat` 等别名，**以
DeepSeek 官方文档最新为准**）。

> ⚠️ 注意：仓库 `.env.example` 里默认写的是**火山方舟**（`https://ark.cn-beijing.volces.com/api/v3` +
> `deepseek-v4-flash-ga-260731`）。你要用火山就保留默认；要用 **DeepSeek 官方**就按上面三行改。两者是
> OpenAI 兼容接口，随便换，只有 `AI_API_KEY` / `AI_BASE_URL` / `AI_MODEL` 不同。

其他常用项（可选，见 `.env.example` 里的注释）：

```ini
# 想用早安/晚安/上课提醒，就填推送目标群号（否则留空）
TARGET_GROUP=123456789

# 只允许特定群使用（逗号分隔；留空 = 不限制）
# ALLOW_GROUPS=123456789,987654321
```

> 改完 `.env` 必须**重启后端**才生效。

---

## 4. 部署 NapCat（协议端）

NapCat 负责登录一个 QQ 小号并转发消息。服务器上没有桌面，**推荐用 Docker 跑**；本机 Windows 则用
NapCat Desktop（图形界面，见 [docs/安装部署.md](docs/安装部署.md)）。

### 4.1 用 Docker 跑 NapCat（推荐）

先装 Docker：

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER     # 免 sudo，装完重新登录生效
```

写一个 `docker-compose.yml`：

```yaml
services:
  napcat:
    image: napneko/napcat-docker:latest   # ⚠️ 镜像名以 NapCat 官方仓库最新为准，见下
    container_name: napcat
    restart: unless-stopped
    environment:
      - NAPCAT_UID=1000                  # 和宿主机用户 uid 对齐（id -u 查看）
      - NAPCAT_GID=1000                  # id -g 查看
      - NAPCAT_MODE=webui                # 用网页端配置
    ports:
      - "6099:6099"                       # NapCat WebUI 管理页
    volumes:
      - ./napcat/config:/app/napcat/config   # 配置持久化
      - ./napcat/qq:/root/.config/QQ          # QQ 数据持久化
    mac_address: 02:42:ac:11:00:02       # 固定 MAC，保持设备指纹稳定（防登录态失效）
```

启动：

```bash
docker compose up -d
docker compose logs -f napcat
```

> **⚠️ 镜像名务必以 NapCat 官方最新为准**：到官方仓库确认当前镜像与参数——
> <https://github.com/NapNeko/NapCat-Docker>（或 NapCat 官方文档
> <https://napneko.github.io/>）。上面示例里的 `napneko/napcat-docker:latest` 只是占位，别照抄。

### 4.2 配置反向 WebSocket（关键一步）

打开 NapCat 管理页 `http://你的服务器IP:6099/webui`，进「网络配置」或「连接」页，**新增一个 WebSocket
客户端**（反向 WS），填：

| 字段 | 填什么 |
|---|---|
| 服务端 URL | `ws://127.0.0.1:8080/onebot/v11/ws` ⚠️ 末尾 `/ws` 不能少 |
| 鉴权 Token | **留空**（后端没开鉴权） |
| 连接角色 | Universal（全双工） |
| 消息上报格式 | Array |
| 心跳间隔 | 30000 |
| 重连间隔 | 5000 |

然后**扫码登录你的 QQ 小号**（用手机 QQ 扫管理页里出现的二维码）。

> **为什么 NapCat 和后端必须在同一台机器？** 上面的 URL 写的是 `127.0.0.1`（本机回环）。如果 NapCat 和
> 后端不在同一台机器，`127.0.0.1` 指向的是 NapCat 自己，会连不上后端——此时必须把 URL 改成后端的真实
> 内网/公网 IP，并在后端 `.env` 里把 `HOST` 从 `127.0.0.1` 改成 `0.0.0.0` 放开监听。**最简单的做法就是
> 两台服务放同一台机器，全程用 `127.0.0.1`，不暴露端口到公网。**

### 4.3 本机 Windows 跑 NapCat（不用 Docker 的备选）

如果你不上服务器、就在自己 Windows 电脑上跑，用 **NapCat Desktop**（图形界面），完整步骤（含「别点 QQ
更新」「WS 连不上」等已知坑）见 [docs/安装部署.md](docs/安装部署.md)。核心就两条：反向 WS 地址填
`ws://127.0.0.1:8080/onebot/v11/ws`，Token 留空。

---

## 5. 开机自启（systemd）

让 `python bot.py` 常驻，崩溃/重启自动拉起。

新建 `/etc/systemd/system/qqbot.service`：

```bash
sudo tee /etc/systemd/system/qqbot.service > /dev/null <<'EOF'
[Unit]
Description=QQ AI Bot (蓝色大肥鱼) - NoneBot2 backend
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/qq-ai-bot
ExecStart=/opt/qq-ai-bot/.venv/bin/python bot.py
Restart=always
RestartSec=3
# 以你自己的普通用户跑（别用 root）；和下面两行对应
User=ubuntu
Group=ubuntu
# 不打印日志到 stdout 的缓冲，便于 journalctl 即时查看
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
```

要点说明：

- **`ExecStart` 必须指向 venv 里的 python**（`/opt/qq-ai-bot/.venv/bin/python`），不是系统 `python3`，
  否则找不到已装好的依赖。
- **`WorkingDirectory`** 指向项目目录，因为 `bot.py` / `config.py` 是相对路径找 `.env`、`persona.md`、
  `timetable.json` 等数据文件的，不在正确目录启动就会「找不到文件」。
- **`Restart=always`**：进程崩了 3 秒后自动重启。

启用并启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now qqbot
sudo systemctl status qqbot          # 看是否 running
journalctl -u qqbot -f               # 实时看日志（看到「🤖 鲸鱼娘 起来了」就成）
```

常用命令：

```bash
sudo systemctl restart qqbot         # 改 .env 后重启
sudo systemctl stop qqbot
sudo systemctl disable qqbot         # 取消开机自启
```

---

## 6. 语音（TTS，可选）

> 语音功能已接入代码：`config.py` 提供 `TTS_*` 配置、`plugins/tts.py` 负责合成、`plugins/ai_group.py`
> 在文字回复后追加一条语音。核心结论——**2 核 4G 无 GPU 的服务器只能跑 Edge-TTS，跑不了
> GPT-SoVITS / RVC**——是确定的。开启语音需先设 `TTS_ENABLED=true`。

文字→语音方式（`TTS_ENGINE`），以及可选的 RVC 变声（`TTS_RVC_*`）：

### 方式 A：Edge-TTS（免费，无需 GPU，推荐起步）

```ini
TTS_ENABLED=true
TTS_ENGINE=edge
```

Edge-TTS 走微软在线语音合成，免费、免 GPU、延迟低，直接在 2 核 4G 服务器上就能跑，只是音色是「系统合成
音」而非角色音色。

### 方式 B：GPT-SoVITS（角色音色，需要 GPU 机器）

```ini
TTS_ENABLED=true
TTS_ENGINE=sovits
TTS_SOVITS_URL=http://你的GPU机器IP:9880          # GPT-SoVITS API 地址（默认端口 9880）
TTS_SOVITS_REF_AUDIO=/path/to/参考音频.wav          # 声优/角色的参考音频
TTS_SOVITS_PROMPT_TEXT=参考音频对应的文字内容        # 参考音频说的是什么
```

GPT-SoVITS 是本地语音合成，能复刻特定声优/角色音色，但**必须跑在一台带 NVIDIA 显卡（GPU）的机器上**。
2 核 4G 云服务器没有 GPU、显存更是没有，本地合成根本跑不动，所以要把合成服务部署到另一台带 N 卡的机器
（或租用带 GPU 的服务器），后端通过 HTTP 把「参考音频 + 文本」发给它的 9880 端口拿回音频，再转成 QQ 语音
发出去。

> **为什么 TTS 一定要放 GPU 机器？** 神经网络语音合成（尤其 GPT-SoVITS 这类）推理需要大量并行算力，
> 只有 GPU 才能在秒级出音频；CPU 上跑动辄几十秒、几分钟一句，还吃满内存。语音合成与「接消息、调大模型」
> 是两码事——后者 2 核 4G 足够，前者必须 GPU。

### 方式 C：RVC 变声（角色音色，需要 GPU 机器）

如果手里是 **RVC 变声模型**（单文件 `.pth`，比如你放的 `派蒙.pth`），它不是 TTS——它不认字，
只把「一段已有的声音」换成角色音色。所以链路是两段拼起来的：

```
文字 → Edge-TTS(跑在 2核4G 上，免费) → 底音 → RVC(派蒙.pth，跑在 GPU 机器上) → 派蒙音色 → QQ 语音
```

```ini
TTS_ENABLED=true
TTS_ENGINE=edge                                  # 底音用 edge 最省事
TTS_RVC_ENABLED=true                             # 关键：底音再送 RVC 变声
TTS_RVC_URL=http://你的GPU机器IP:6242             # RVC WebUI 的 api 地址（默认端口 6242）
TTS_RVC_PTH=/path/to/派蒙.pth                     # RVC 机器上的模型路径（注意是那台机器的路径）
# TTS_RVC_INDEX=/path/to/派蒙.index               # 索引文件，可选；没有就留空纯模型推理
# TTS_RVC_PITCH=0                                 # 音高偏移（-24~24），派蒙音色偏高可试 +3~+6
# TTS_RVC_F0=rmvpe                                # 音高提取：rmvpe（推荐）/ harvest / pm / crepe
```

RVC 服务端怎么搭：在带 N 卡的机器上装 **RVC WebUI**（Retrieval-based-Voice-Conversion-WebUI），把
`派蒙.pth` 丢进它的 `weights/` 目录，跑它的 `api_240604.py`（默认监听 6242），后端就会把 Edge-TTS 的底音
用 base64 发过去、拿回变声后的音频。RVC 推理相对轻，**6GB 显存就能跑**，比 GPT-SoVITS（要 12GB+）门槛低。

> ⚠️ RVC 的 API 字段名各版本不一（`pitch` 有的版本叫 `transposition`、音频字段有的叫 `input_audio_base64`
> 有的要 multipart 传文件）。代码里 `plugins/tts.py` 的 `_rvc_convert()` 用的是 api_240604.py 的约定，
> 对不上时按你服务端实际字段改那一处即可。

---

## 7. 验证

部署完成后逐层验证：

```bash
cd /opt/qq-ai-bot

# 1) 只体检、不起服务：逐项检查密钥/人设/课表/表情包/端口
.venv/bin/python check.py

# 2) 体检 + 真连一次模型接口，验证 API Key 可用（会 GET /models）
.venv/bin/python check.py --ping
```

看到 `AI_API_KEY ✅`、`监听端口 ✅ 127.0.0.1:8080 空闲`、`模型连通性 ✅` 就说明后端 OK。

再在 **QQ 群里 @ 机器人** 做端到端测试：

```
@蓝色大肥鱼 你好
```

有回复就说明整条链路（QQ 群 ↔ NapCat ↔ 后端 ↔ DeepSeek API）通了。没反应按
[docs/安装部署.md](docs/安装部署.md) 的排查表：①后端在跑吗 ②NapCat 反向 WS 地址带 `/ws` 吗 ③NapCat
实例在线吗。

---

## 8. 风控与合规

QQ 机器人走的是**非官方协议**，务必遵守，否则轻则封号、重则连累更多：

- **务必用 QQ 小号**，别用主号/工作号。账号一旦被封，主号损失不可逆。
- **控制发言频率**：别刷屏、别群发、别用来做营销/骚扰。可调 `.env` 的 `REPLY_SPLIT_LINES` / `REPLY_LINE_GAP`
  让它「像真人一样」分句、带间隔地发，天然降低风控。
- **API Key 严格保密**：`.env` 已写进 `.gitignore`，**不要**把它提交到 git、不要截图外发。一旦泄露立刻去
  对应平台重新生成。
- **音色/声优版权**：GPT-SoVITS 复刻的角色/声优音色**仅限个人自用**，不商用、不公开传播合成音频、不用于
  骚扰他人。
- **合规底线**：本项目属个人学习/自用性质，**不商用、不群发、不骚扰**。相关风险（封号、数据丢失）由使用者
  自负，详见 [README.md](README.md) 免责声明。

---

## 附：一键启动命令速查

```bash
# 后端相关
sudo systemctl restart qqbot          # 重启后端（改 .env 后必做）
journalctl -u qqbot -f                # 看后端日志

# NapCat 相关（Docker）
docker compose -f /opt/napcat/docker-compose.yml logs -f napcat   # 看 NapCat 日志
docker compose -f /opt/napcat/docker-compose.yml restart napcat   # 重启 NapCat

# 体检
cd /opt/qq-ai-bot && .venv/bin/python check.py --ping
```
