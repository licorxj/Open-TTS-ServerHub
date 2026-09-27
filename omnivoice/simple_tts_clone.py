#!/usr/bin/env python3
"""
简易声音克隆 TTS 端点（流式返回）

POST /tts
Body (JSON):
  text:       要合成的文本
  voice_name: 音色名称（可选）。克隆时到 voice/ 目录下查找同名的 .wav 作为参考音频
  ref_audio:  参考音频文件路径（可选）。优先级低于 voice_name
  ref_text:   参考音频文本（可选）

- 提供 voice_name 时，到 voice/<voice_name>.wav 读取参考音频；
  未提供 voice_name / ref_audio 或两者均无效时，回退到 voice/voice_path.txt
- 接口以流式（StreamingResponse）方式逐段返回 WAV 音频：长文本按 chunk 分段生成，
  生成一段即推送一段，客户端可边接收边播放
"""

import argparse
import io
import logging
import os
import sys
import uuid
import wave
import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from contextlib import asynccontextmanager

# 添加项目根目录到 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
logging.getLogger("uvicorn.access").disabled = True

# ── 全局变量 ──
model = None
device = None
dtype = None
sampling_rate = 24000
voice_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "voice")
voice_path_file = os.path.join(voice_dir, "voice_path.txt")


# ── Pydantic ──
class TTSRequest(BaseModel):
    text: str = Field(..., description="要合成的文本", min_length=1)
    voice_name: str | None = Field(None, description="音色名称，克隆时到 voice/<voice_name>.wav 找同名参考音频（可选）")
    ref_audio: str | None = Field(None, description="参考音频文件路径（可选，优先级低于 voice_name）")
    ref_text: str | None = Field(None, description="参考音频的文本（可选）")


# ── 辅助函数 ──
def get_best_device():
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def resolve_ref_by_voice_name(voice_name: str | None) -> str | None:
    """根据音色名称到 voice/ 目录下查找同名的 .wav 参考音频"""
    if not voice_name or not voice_name.strip():
        return None
    name = voice_name.strip()
    # 兼容用户直接带 .wav 后缀的情况
    if name.lower().endswith(".wav"):
        candidate = os.path.join(voice_dir, name)
    else:
        candidate = os.path.join(voice_dir, name + ".wav")
    if os.path.exists(candidate):
        logger.info(f"使用音色 [{name}] 对应的参考音频: {candidate}")
        return candidate
    logger.warning(f"音色 [{name}] 未找到对应参考音频: {candidate}")
    return None


def resolve_ref_audio(ref_audio_path: str | None) -> str:
    """解析参考音频路径：如果未提供或不存在，回退到 voice_path.txt"""
    if ref_audio_path and ref_audio_path.strip():
        resolved = ref_audio_path.strip()
        if not os.path.exists(resolved):
            logger.warning(f"参考音频不存在: {resolved}，回退到 voice_path.txt")
        else:
            return resolved

    # 回退到 voice_path.txt
    if os.path.exists(voice_path_file):
        with open(voice_path_file, "r", encoding="utf-8") as f:
            fallback = f.read().strip()
        # voice_path.txt 中的路径可能是相对路径，需要拼接项目根目录
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if not os.path.isabs(fallback):
            fallback = os.path.join(project_root, fallback)
        if os.path.exists(fallback):
            logger.info(f"使用 voice_path.txt 中的参考音频: {fallback}")
            return fallback
        else:
            raise FileNotFoundError(f"voice_path.txt 中路径也不存在: {fallback}")
    else:
        raise FileNotFoundError(f"voice_path.txt 文件不存在: {voice_path_file}")


def audio_chunk_to_wav_bytes(audio: np.ndarray, sample_rate: int) -> bytes:
    """将一段单声道/多声道 numpy 音频（float32，范围[-1,1]）编码为标准 WAV 字节流"""
    # OmniVoice 返回 float32，写入 16-bit PCM
    if audio.dtype != np.float32 and audio.dtype != np.float64:
        if np.issubdtype(audio.dtype, np.floating):
            audio = audio.astype(np.float32)
        else:
            audio = audio.astype(np.float32) / 32768.0
    else:
        audio = audio.astype(np.float32)

    # 转为 (samples, channels) 便于 wave 写入
    if audio.ndim == 1:
        audio = audio[:, np.newaxis]
    audio_int16 = np.clip(audio, -1.0, 1.0)
    audio_int16 = (audio_int16 * 32767.0).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(audio_int16.shape[1])
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_int16.tobytes())
    return buf.getvalue()


def load_model(model_path: str, device_str: str):
    global model, device, dtype

    from omnivoice import OmniVoice

    device = device_str if device_str else get_best_device()
    dtype = torch.float16 if device == "cuda" else torch.float32

    logger.info(f"正在加载模型: {model_path}")
    model = OmniVoice.from_pretrained(
        model_path,
        device_map=device,
        dtype=dtype,
        load_asr=True,
    )
    global sampling_rate
    sampling_rate = model.sampling_rate
    logger.info(f"模型加载完成！采样率: {sampling_rate}Hz")


# ── FastAPI ──
@asynccontextmanager
async def lifespan(app: FastAPI):
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8853)
    parser.add_argument("--device", default=None)
    parser.add_argument("--model", default=None)
    args, _ = parser.parse_known_args()

    model_path = args.model or os.path.join(os.getcwd(), "models")
    load_model(model_path, args.device)

    logger.info(f"Server running on http://{args.host}:{args.port}")
    yield

    logger.info("Shutting down...")


app = FastAPI(title="Simple TTS Clone", lifespan=lifespan)


@app.post("/tts")
async def tts_clone(request: TTSRequest):
    if model is None:
        raise HTTPException(503, "模型未加载")

    # 解析参考音频路径：voice_name 优先，其次 ref_audio，最后 voice_path.txt
    try:
        ref_audio = resolve_ref_by_voice_name(request.voice_name)
        if ref_audio is None:
            ref_audio = resolve_ref_audio(request.ref_audio)
    except FileNotFoundError as e:
        raise HTTPException(400, str(e))

    task_id = str(uuid.uuid4())
    logger.info(f"[{task_id}] 开始生成: voice_name={request.voice_name}, text={request.text[:50]}")

    # 流式生成器：逐 chunk 编码为 WAV 并 yield
    def audio_stream():
        try:
            from omnivoice import OmniVoiceGenerationConfig

            gen_config = OmniVoiceGenerationConfig(
                num_step=32,
                guidance_scale=2.0,
                denoise=True,
                postprocess_output=True,
                # 开启分段生成，长文本会被拆成多个 chunk 便于流式输出
                audio_chunk_duration=30.0,
            )

            audio = model.generate(
                text=request.text,
                ref_audio=ref_audio,
                ref_text=request.ref_text if request.ref_text else None,
                instruct=None,
                language=None,
                speed=1.0,
                duration=None,
                generation_config=gen_config,
            )

            # 归一化成 chunk 列表：长文本返回 list（每个元素是一段），短文本返回单个 ndarray
            if isinstance(audio, list) and len(audio) > 0:
                if isinstance(audio[0], list):
                    chunks = [c for seg in audio for c in seg]
                else:
                    chunks = audio
            else:
                chunks = [audio]

            for i, chunk in enumerate(chunks):
                if chunk is None:
                    continue
                arr = chunk
                if isinstance(arr, torch.Tensor):
                    arr = arr.cpu().numpy()
                wav_bytes = audio_chunk_to_wav_bytes(arr, sampling_rate)
                logger.info(f"[{task_id}] 推送 chunk {i}: {len(wav_bytes)} bytes")
                yield wav_bytes

        except Exception as e:
            logger.error(f"[{task_id}] 流式生成失败: {e}")

    return StreamingResponse(
        audio_stream(),
        media_type="audio/wav",
        headers={"X-Task-Id": task_id, "Cache-Control": "no-cache"},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8853, access_log=False)
