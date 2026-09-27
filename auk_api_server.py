#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AuK 语音生成 / 编辑 API 服务

基于腾讯混元 AuK（Tencent-Hunyuan/AuK）的 FastAPI 服务，暴露其全部能力：
  - 语音合成：Zero-shot TTS（音色克隆）、Instruct TTS（文字描述风格合成）
  - 内容编辑：说话内容改词、歌词编辑
  - 声学编辑：音调、语速、音量
  - 副语言编辑：情感转换、音色编辑、去口音、非语言声增删、耳语转换
  - 修复增强：语音增强（去噪/去混响）、说话人分离、人声/歌声提取、音质提升

模型统一由 tools/models_manager 管理（缺失时经魔搭自动下载，存于 models/）。
ASR 复用项目既有 tools/asr.py（Whisper），不依赖腾讯云 / SenseVoice 额外模型。

启动：
    python auk_api_server.py --host 0.0.0.0 --port 8021 --device auto --dtype bf16
或双击 "启动_auk_api.bat"
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent
AUK_DIR = PROJECT_ROOT / "AuK"
for _p in (str(PROJECT_ROOT), str(AUK_DIR), str(AUK_DIR / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi import FastAPI, File, Form, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

SERVER_CONFIG_PATH = PROJECT_ROOT / "config" / "auk_server.yaml"
DEFAULT_SERVER_CONFIG = {
    "server": {
        "host": "0.0.0.0",
        "port": 8021,
        "device": "auto",
        "dtype": "bf16",
        "max_workers": 4,
        "gpu_concurrency": 1,
    },
    "llm": {"api_key": "", "base_url": "", "model": ""},
    "asr": {"model": "openai/whisper-large-v3-turbo", "device": "auto"},
}


def load_server_config() -> dict:
    cfg = {k: dict(v) for k, v in DEFAULT_SERVER_CONFIG.items()}
    if SERVER_CONFIG_PATH.is_file():
        try:
            user_cfg = yaml.safe_load(SERVER_CONFIG_PATH.read_text(encoding="utf-8")) or {}
            for section, values in user_cfg.items():
                if isinstance(values, dict) and section in cfg:
                    cfg[section].update(values)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 读取 {SERVER_CONFIG_PATH} 失败，使用默认配置: {exc}")
    return cfg


CONFIG = load_server_config()
OUTPUT_DIR = AUK_DIR / "outputs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# 上传音频统一转码：任意可解码格式 -> 24kHz 单声道 PCM16 WAV
# libsndfile 不支持 m4a/mp3 等，故在入站后先经 ffmpeg 归一化，避免
# "Format not recognised" 报错。ffmpeg 缺失时退回原文件（WAV 仍可工作）。
# ---------------------------------------------------------------------------
_FFMPEG_BIN = None

def _resolve_ffmpeg() -> str | None:
    cand = PROJECT_ROOT / "py312env" / "ffmpeg" / "ffmpeg.exe"
    if cand.is_file():
        return str(cand)
    return shutil.which("ffmpeg")

def _to_wav(src_path: str) -> str:
    global _FFMPEG_BIN
    if _FFMPEG_BIN is None:
        _FFMPEG_BIN = _resolve_ffmpeg()
    if not _FFMPEG_BIN:
        return src_path
    out_path = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    try:
        subprocess.run(
            [_FFMPEG_BIN, "-y", "-i", src_path, "-ar", "24000", "-ac", "1", "-f", "wav", out_path],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return out_path
    except Exception:
        try:
            os.remove(out_path)
        except OSError:
            pass
        return src_path

# 引擎构建状态
_BUILDING = False
_READY = threading.Event()
_BUILD_ERROR: Optional[str] = None
_GEN_LOCK = threading.Lock()  # AuK 串行 GPU 推理


app = FastAPI(title="AuK 语音生成/编辑 API", version="1.0")

# 允许跨域（本地测试 HTML 从 file:// 或任意前端调用本 API 时必需）
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _resolve_device(device: str) -> str:
    if device and device != "auto":
        return device
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def _build_worker(device: str, dtype: str):
    global _BUILDING, _BUILD_ERROR
    try:
        import auk_service
        # 受限显存环境下可只构建部分变体，例如 AUK_VARIANTS="AuK (Base)"
        # 或 config/auk_server.yaml 的 server.variants: ["AuK (Base)"]
        variants = CONFIG["server"].get("variants")
        env_variants = os.environ.get("AUK_VARIANTS")
        if env_variants:
            variants = [v.strip() for v in env_variants.split(",") if v.strip()]
        # 可选：将 Qwen 文本编码器卸载到 CPU 以省显存（16GB 显卡可借此跑通 Base 引擎）
        text_encoder_device = CONFIG["server"].get("text_encoder_device")
        text_encoder_device = os.environ.get("AUK_TEXT_ENCODER_DEVICE") or text_encoder_device
        auk_service.build_engines(
            device=device, dtype=dtype,
            variants=variants, text_encoder_device=text_encoder_device,
        )
        _READY.set()
    except Exception as exc:  # noqa: BLE001
        _BUILD_ERROR = f"{type(exc).__name__}: {exc}"
        print(f"[ERROR] AuK 引擎构建失败: {_BUILD_ERROR}")
    finally:
        _BUILDING = False


def start_build(device: str, dtype: str):
    global _BUILDING
    if _READY.is_set() or _BUILDING:
        return
    _BUILDING = True
    t = threading.Thread(target=_build_worker, args=(device, dtype), daemon=True)
    t.start()


def _wait_ready(timeout: float = 0.0) -> None:
    if _READY.is_set():
        return
    if _BUILD_ERROR:
        raise HTTPException(status_code=503, detail=f"引擎构建失败: {_BUILD_ERROR}")
    if timeout > 0:
        _READY.wait(timeout)
    if not _READY.is_set():
        raise HTTPException(status_code=503, detail="引擎仍在加载中，请稍后重试（首次会下载模型）。")


def _save_wav(sr_audio) -> str:
    """sr_audio = (sample_rate, pcm16 ndarray)。保存为临时 WAV，返回相对 URL。"""
    import soundfile as sf
    import numpy as np
    sr, pcm16 = sr_audio
    pcm16 = np.asarray(pcm16)
    fname = f"{uuid.uuid4().hex}.wav"
    out_path = OUTPUT_DIR / fname
    sf.write(str(out_path), pcm16, int(sr), format="WAV", subtype="PCM_16")
    return f"/v1/files/{fname}"


@app.on_event("startup")
def _on_startup():
    srv = CONFIG["server"]
    start_build(_resolve_device(srv.get("device", "auto")), srv.get("dtype", "bf16"))


@app.get("/health")
def health():
    return {
        "status": "ok" if _READY.is_set() else "loading",
        "ready": _READY.is_set(),
        "building": _BUILDING,
        "error": _BUILD_ERROR,
        "variants": ["AuK (Base)", "AuK-Flash ⚡"],
    }


@app.get("/v1/tasks")
def tasks():
    import auk_service
    return JSONResponse({"tasks": auk_service.list_supported_tasks()})


@app.post("/v1/generate")
def generate(
    instruction: str = Form(..., description="标准指令（自然语言句子），如 '将语速调整为1.25倍。'"),
    variant: str = Form("AuK (Base)", description="AuK (Base) 或 AuK-Flash ⚡"),
    audio: Optional[UploadFile] = File(None, description="输入/参考音频（部分任务必需）"),
    gen_seconds: Optional[float] = Form(None, description="目标时长(秒)；Instruct TTS 无参考音时必需"),
    ref_text: Optional[str] = Form(None, description="参考音频的转写文本（可选）"),
    gen_text: Optional[str] = Form(None, description="目标合成文本（用于时长估算，可选）"),
    nfe: int = Form(32, description="Base 采样步数（Flash 忽略）"),
    cfg: float = Form(2.0, description="Base CFG 强度（Flash 忽略）"),
    seed: Optional[int] = Form(None),
    task_type: Optional[str] = Form(None, description="可选任务类型提示"),
):
    _wait_ready(30)
    audio_path = None
    tmp = None
    try:
        import auk_service
        if audio is not None:
            suffix = Path(audio.filename or "input.wav").suffix or ".wav"
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            tmp.write(audio.file.read())
            tmp.close()
            audio_path = _to_wav(tmp.name)
        with _GEN_LOCK:
            sr_audio = auk_service.generate(
                variant, audio_path, instruction,
                gen_seconds=gen_seconds, ref_text=ref_text, gen_text=gen_text,
                nfe=nfe, cfg=cfg, seed=seed, task_type=task_type,
            )
        url = _save_wav(sr_audio)
        return JSONResponse({"audio_url": url, "sample_rate": int(sr_audio[0])})
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 捕获 gradio/运行时错误
        detail = str(exc)
        if "gr.Error" in detail or "Error" in type(exc).__name__:
            detail = getattr(exc, "message", detail)
        raise HTTPException(status_code=400, detail=detail)
    finally:
        for _p in {tmp.name if tmp else None, audio_path}:
            if _p and os.path.exists(_p):
                try:
                    os.remove(_p)
                except OSError:
                    pass


@app.post("/v1/generate_with_pe")
def generate_with_pe(
    instruction: str = Form(..., description="口语化指令，如 '把这段话语速调快一点'"),
    variant: str = Form("AuK (Base)"),
    audio: Optional[UploadFile] = File(None, description="输入/参考音频（部分任务必需）"),
    use_pe: bool = Form(True, description="是否启用 Prompt Enhancer（LLM 解析指令）"),
    gen_seconds: Optional[float] = Form(None, description="目标时长(秒)；关闭 PE 时必需且>0"),
    ref_text: Optional[str] = Form(None),
    gen_text: Optional[str] = Form(None),
    nfe: int = Form(32),
    cfg: float = Form(2.0),
    seed: Optional[int] = Form(None),
    llm_api_key: Optional[str] = Form(None),
    llm_base_url: Optional[str] = Form(None),
    llm_model: Optional[str] = Form(None),
):
    _wait_ready(30)
    audio_path = None
    tmp = None
    try:
        import auk_service
        if audio is not None:
            suffix = Path(audio.filename or "input.wav").suffix or ".wav"
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            tmp.write(audio.file.read())
            tmp.close()
            audio_path = _to_wav(tmp.name)

        llm = CONFIG["llm"]
        asr = CONFIG["asr"]
        from auk_service import WhisperASRProvider
        asr_provider = WhisperASRProvider(model_name=asr.get("model"), device=asr.get("device"))

        with _GEN_LOCK:
            (sr_audio, metadata) = auk_service.generate_with_pe(
                variant, audio_path, instruction,
                use_pe=use_pe,
                gen_seconds=gen_seconds, ref_text=ref_text, gen_text=gen_text,
                nfe=nfe, cfg=cfg, seed=seed,
                llm_api_key=llm_api_key or llm.get("api_key"),
                llm_base_url=llm_base_url or llm.get("base_url"),
                llm_model=llm_model or llm.get("model"),
                asr_provider=asr_provider,
            )
        url = _save_wav(sr_audio)
        return JSONResponse({
            "audio_url": url,
            "sample_rate": int(sr_audio[0]),
            "metadata": metadata,
        })
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        for _p in {tmp.name if tmp else None, audio_path}:
            if _p and os.path.exists(_p):
                try:
                    os.remove(_p)
                except OSError:
                    pass


app.mount("/v1/files", StaticFiles(directory=str(OUTPUT_DIR)), name="auk_files")


def main():
    parser = argparse.ArgumentParser(description="AuK 语音生成/编辑 API 服务")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--device", default=None)
    parser.add_argument("--dtype", default=None)
    parser.add_argument("--max-workers", type=int, default=None)
    args = parser.parse_args()

    srv = CONFIG["server"]
    host = args.host or srv.get("host", "0.0.0.0")
    port = args.port or srv.get("port", 8021)
    device = args.device or srv.get("device", "auto")
    dtype = args.dtype or srv.get("dtype", "bf16")

    import uvicorn
    # 预先在后台开始构建引擎（启动事件也会触发，确保不遗漏）
    start_build(_resolve_device(device), dtype)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
