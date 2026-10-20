"""文字转语音：把 AI 回复读出来。

支持两种引擎（由 `.env` 的 `TTS_ENGINE` 决定）：
- `edge`：微软 Edge 在线 TTS，免费、免部署，网络通就能用
- `sovits`：自建 GPT-SoVITS 服务，本地音色克隆
"""
import base64
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from nonebot import logger  # noqa: E402

from config import get_config  # noqa: E402

cfg = get_config()

CACHE_DIR = ROOT / ".tts_cache"


async def synthesize(text: str) -> Path | None:
    """生成 `text` 的语音文件，返回文件路径；失败返回 None（不抛异常）。

    链路：文字 → 底音（edge 或 sovits）→（可选）RVC 变声 → 最终音频。
    """
    if not text.strip():
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    engine = cfg.tts.engine

    base: Path | None = None
    if engine == "edge":
        base = await _edge(text)
    elif engine == "sovits":
        base = await _sovits(text)
    else:
        logger.warning(f"未知的 TTS_ENGINE：{engine!r}（可选 edge / sovits）")
        return None

    if base is None:
        return None

    if cfg.tts.rvc_enabled:
        converted = await _rvc_convert(base)
        if converted is None:
            # 变声失败不硬性打断——退回底音（至少还能出声），但记一条警告
            logger.warning("RVC 变声失败，退回未变声的底音")
            return base
        return converted

    return base


async def _edge(text: str) -> Path | None:
    try:
        import edge_tts

        out_path = CACHE_DIR / f"tts_{int(time.time() * 1000)}.mp3"
        comm = edge_tts.Communicate(text, cfg.tts.edge_voice)
        await comm.save(str(out_path))
        return out_path
    except Exception as e:
        logger.warning(f"edge-tts 合成失败：{type(e).__name__}: {e}")
        return None


async def _sovits(text: str) -> Path | None:
    try:
        out_path = CACHE_DIR / f"tts_{int(time.time() * 1000)}.wav"
        # GPT-SoVITS 的 API 字段名各版本不一（如 text_lang / prompt_lang 有的版本叫 language），
        # 对不上时按你的服务端实际字段调整。
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                f"{cfg.tts.sovits_url}/tts",
                json={
                    "text": text,
                    "text_lang": "zh",
                    "ref_audio_path": cfg.tts.sovits_ref_audio,
                    "prompt_text": cfg.tts.sovits_prompt_text,
                    "prompt_lang": cfg.tts.sovits_prompt_lang,
                    "media_type": "wav",
                    "streaming_mode": False,
                },
            )
        if r.status_code >= 400:
            logger.warning(f"sovits TTS 返回 HTTP {r.status_code}：{r.text[:300]}")
            return None
        out_path.write_bytes(r.content)
        return out_path
    except Exception as e:
        logger.warning(f"sovits TTS 合成失败：{type(e).__name__}: {e}")
        return None


async def _rvc_convert(audio: Path) -> Path | None:
    """把底音送到 RVC WebUI（跑在带 GPU 的机器上）转成角色音色。

    RVC WebUI 的 api_240604.py（端口默认 6242）接受 base64 音频 + 参数，
    各版本字段名可能不同（如 pitch / transposition、index_rate 等），对不上时
    按你服务端实际字段改。
    """
    try:
        data = audio.read_bytes()
        payload = {
            "pth_path": cfg.tts.rvc_pth,
            "index_path": cfg.tts.rvc_index or "",
            "input_audio_base64": base64.b64encode(data).decode("ascii"),
            "pitch": cfg.tts.rvc_pitch,
            "formant": cfg.tts.rvc_formant,
            "index_rate": cfg.tts.rvc_index_rate,
            "f0_method": cfg.tts.rvc_f0,
            "output_format": "wav",
        }
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(cfg.tts.rvc_url, json=payload)
        if r.status_code >= 400:
            logger.warning(f"RVC 返回 HTTP {r.status_code}：{r.text[:300]}")
            return None
        # 有些 RVC API 直接回音频字节，有些回 JSON 里带 base64 字段
        ctype = r.headers.get("content-type", "")
        if "json" in ctype:
            try:
                j = r.json()
                b64 = j.get("output_audio_base64") or j.get("audio") or j.get("data")
                if isinstance(b64, list):
                    b64 = b64[0] if b64 else None
                if not b64:
                    logger.warning(f"RVC 返回 JSON 但没找到音频字段：{list(j)[:10]}")
                    return None
                out = base64.b64decode(b64)
            except Exception:
                logger.warning(f"RVC 返回 JSON 解析失败：{r.text[:300]}")
                return None
        else:
            out = r.content
        out_path = CACHE_DIR / f"rvc_{int(time.time() * 1000)}.wav"
        out_path.write_bytes(out)
        return out_path
    except Exception as e:
        logger.warning(f"RVC 变声失败：{type(e).__name__}: {e}")
        return None
