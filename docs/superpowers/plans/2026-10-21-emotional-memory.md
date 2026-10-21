# 情感记忆实现计划

**Goal:** 给鲸鱼娘加一层「情感记忆」：记住群友近期情绪 + 群里人际关系，情绪指数衰减、关系长期，独立于事实档案。

**Architecture:** 新建 `plugins/emotion_memory.py`（镜像 `long_memory.py`），独立存 `memory/情感记忆.json`；复用现有「攒够合并」触发，另跑一次情感整理 LLM；注入时拼成紧跟事实档案后的固定文本。

**Tech Stack:** Python 3.12 / NoneBot2 / 纯 JSON 存储，无新依赖。

## Global Constraints

- 事实档案（`long_memory.py`）一行不动。
- 记忆 key 用不透明 id（群号 or 私聊 user_id），隔离自带；主人私聊 = `master_qq` 独立一份。
- 情绪条目字段：`id / subject / kind(emotion|relation) / fact / intensity / date`。
- 衰减公式：`effective = intensity × 0.5^(age_days / half_life)`，`half_life = EMO_HALF_LIFE_DAYS × (1+intensity)`。
- 遗忘：`effective < EMO_MIN_INTENSITY` 或 `age_days > EMO_MAX_DAYS`；关系不衰减。
- 默认值：`EMO_HALF_LIFE_DAYS=3`、`EMO_MIN_INTENSITY=0.15`、`EMO_MAX_DAYS=30`、`EMO_MAX_CHARS=600`。

## 文件

- Create: `plugins/emotion_memory.py`
- Modify: `config.py`（新增 `EmotionalMemoryConfig` + `_build_emotional_memory()` + `Config.emotion` 字段）
- Modify: `.env.example`（加 4 个 `EMO_*`）
- Modify: `plugins/ai_group.py`（import + `call_ai` 加 `emotion` 参数 + `_maybe_search` 透传 + `_reply_and_send` 注入 + 两个 collector 里 `etm.add_pending`）
- Modify: `plugins/schedule.py`（`daily_memory` 里追加 `etm.consolidate_all()`）

## 接口

- `etm.add_pending(key, name, text)` — 消息进缓冲，攒够触发
- `etm.emotion_text(key) -> str` — 读情感档案产出注入文本（顺带衰减过滤）
- `etm.consolidate(key)` / `etm.consolidate_all()`
- `etm._effective_intensity(intensity, age_days) -> float` — 纯函数，单测点
- `call_ai(user_msg, context, images, long_term, emotion="")` — 新参数

## 任务

### Task 1: config.py + .env.example
新增 `EmotionalMemoryConfig` dataclass、`_build_emotional_memory()`、`Config.emotion` 字段、`load_config` 装配；`.env.example` 加注释块。

### Task 2: plugins/emotion_memory.py
按 `long_memory.py` 结构写：存储/衰减/读取/写入/合并（独立 MERGE_PROMPT）。

### Task 3: ai_group.py 接线
import `etm`；`call_ai` 加 `emotion` 参数并注入；`_maybe_search` 透传；`_reply_and_send` 计算并传 `emotion`；两个 collector 加 `etm.add_pending`。

### Task 4: schedule.py 兜底
`daily_memory` 追加 `await etm.consolidate_all()`。

## 验证

- `python -m py_compile` 全部改动文件
- `python -c` 单测 `_effective_intensity`（强度/天数 → 是否 < 阈值）、`_forgotten`（关系永不遗忘）、`_is_dup`
- `python check.py` 确认配置加载不报错
