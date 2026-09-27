#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Audio8_TTS API 服务

基于 Audio8_TTS (0.6B Preview, 多模态 TTS) 的 FastAPI 服务。
支持的能力（全部暴露）：
  - 纯文本合成（无参考音频）
  - 参考音频 + 参考文本（声音 / 音色克隆；文本缺省时自动用 ASR 转写参考音频）
  - 采样参数：temperature / top_p / top_k / seed / greedy
  - 解码长度：max_new_tokens / retry_max_new_tokens
  - 设备与精度：device / dtype
  - 保存声学 token：save_codes
  - 批量合成：JSONL manifest（每条可带独立参考音频与文本）
  - 模型信息、健康检查

启动：
    python audio8_api_server.py --host 0.0.0.0 --port 8007 --device auto --dtype auto

或双击 "启动_audio8_api.bat"
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
import uuid
import warnings
from pathlib import Path
from typing import Any, Optional

import soundfile as sf
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import asyncio
import functools
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import yaml

# 项目根目录（本文件位于 LcTTSHub 根目录）
PROJECT_ROOT = Path(__file__).resolve().parent
# Audio8_TTS 子项目
AUDIO8_DIR = PROJECT_ROOT / "Audio8_TTS"
DEFAULT_MODEL = PROJECT_ROOT / "models" / "Audio8"

# ----------------------------------------------------------------------------
# 服务器配置（统一由 config/audio8_server.yaml 提供，文件缺失回退内置默认）
# ----------------------------------------------------------------------------
SERVER_CONFIG_PATH = PROJECT_ROOT / "config" / "audio8_server.yaml"

DEFAULT_SERVER_CONFIG = {
    "server": {
        "host": "0.0.0.0",
        "port": 8007,
        "device": "auto",
        "dtype": "auto",
        "max_workers": 4,
        "gpu_concurrency": 1,
    }
}


def load_server_config():
    """加载 config/audio8_server.yaml，缺失/异常时回退到默认配置。"""
    cfg = {k: dict(v) for k, v in DEFAULT_SERVER_CONFIG.items()}
    if SERVER_CONFIG_PATH.is_file():
        try:
            with open(SERVER_CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = yaml.safe_load(f) or {}
            for section, values in user_cfg.items():
                if isinstance(values, dict) and section in cfg:
                    cfg[section].update(values)
        except Exception as e:
            print(f"[WARN] 读取 {SERVER_CONFIG_PATH} 失败，使用内置默认配置: {e}")
    return cfg


CONFIG = load_server_config()


# 并发控制全局对象（在 __main__ 启动前初始化）
_executor = None            # 推理线程池
_gpu_semaphore = None       # GPU 并发信号量（串行化 GPU 推理 / ASR，避免争抢与显存叠加）
_model_load_lock = threading.Lock()  # 惰性加载模型的重入保护
_asr_load_lock = threading.Lock()    # 惰性加载 ASR 的单例保护


@contextmanager
def _gpu_lock():
    """GPU 临界区：若已初始化信号量则获取，否则直接放行。"""
    if _gpu_semaphore is None:
        yield
    else:
        _gpu_semaphore.acquire()
        try:
            yield
        finally:
            _gpu_semaphore.release()

# ============== 专有依赖补丁包优先加载 ==============
# Audio8 (ArktTS) 的生成代码针对 transformers 4.x 设计，在 py312env 默认的
# transformers 5.3.0 下 generate 行为异常，会生成极短静音（code_frames<10）。
# 优先加载 packages/index25 内的 transformers 4.52.1 / tokenizers 0.21.4 /
# huggingface_hub 0.36.2（与 Confucius4 / IndexTTS-2.5 共用同一套已验证补丁依赖）。
PATCH_PKG = PROJECT_ROOT / "packages" / "index25"
if PATCH_PKG.is_dir():
    sys.path.insert(0, str(PATCH_PKG))

sys.path.insert(0, str(AUDIO8_DIR))

# 复用项目的统一模型管理工具
from tools.models_manager import ModelsManager

# Audio8_TTS 推理依赖
from audio8_tts_infer import (  # noqa: E402
    InferenceItem,
    resolve_device,
    resolve_dtype,
    _patch_mistral_regex_safe,
)

# ----------------------------------------------------------------------------
# 目录约定（与项目其它 TTS 保持一致）
# ----------------------------------------------------------------------------
UPLOAD_DIR = PROJECT_ROOT / "uploads"
OUTPUT_DIR = PROJECT_ROOT / "outputs" / "audio8_tts"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ----------------------------------------------------------------------------
# 模型管理
# ----------------------------------------------------------------------------
models_manager = ModelsManager()


def find_model_path(requested: Optional[str]) -> Path:
    """解析模型目录：支持默认路径、ModelsManager 管理的目录、或用户传入的绝对/相对路径。

    默认（不传 requested）或传入已注册名称时，会经由 ModelsManager 解析——
    本地不存在则自动下载到对应 models/ 目录。
    """
    if not requested:
        # 默认走注册名 'audio8'，本地缺失时由 ModelsManager 自动下载到 models/Audio8
        try:
            return Path(models_manager.get_model_path("audio8"))
        except Exception as e:
            print(f"[audio8] 通过 ModelsManager 解析默认模型失败，回退到固定路径: {e}")
            return DEFAULT_MODEL
    p = Path(requested)
    if not p.is_absolute():
        # 仅当名称已在 ModelsManager 注册时才尝试查找，避免误触发下载
        try:
            if requested in models_manager.list_models():
                managed = models_manager.get_model_path(requested)
                if managed:
                    return Path(managed)
        except Exception:
            pass
        p = (PROJECT_ROOT / p).resolve()
    return p


# Audio8 必须存在的权重文件（缺失会导致加载失败）
_WEIGHT_FILES = ("model.safetensors", "codec.pth")


def ensure_model(model_name: str = "audio8") -> Path:
    """确保模型已存放在统一的 models/ 目录下（不存在则自动下载）。

    返回模型目录的绝对路径。失败（例如无网络）时回退到默认固定路径，
    由后续 AutoModel.from_pretrained 处理缺失情况。
    """
    try:
        model_dir = Path(models_manager.get_model_path(model_name))
    except Exception as e:
        print(f"[audio8] 确保模型 '{model_name}' 失败: {e}")
        model_dir = DEFAULT_MODEL

    # 检查关键权重文件是否齐全，缺失则明确提示（便于用户手动放置）
    missing = [w for w in _WEIGHT_FILES if not (model_dir / w).exists()]
    if missing:
        print(
            f"[audio8] 警告: 模型目录 {model_dir} 缺少权重文件 {missing}。\n"
            f"          请从 Hugging Face 仓库 'Audio8/Audio8-TTS-Preview-0.6b'\n"
            f"          下载这些文件放到该目录（或用 HF 镜像 https://hf-mirror.com）。"
        )
    return model_dir


# ----------------------------------------------------------------------------
# 全局推理状态（惰性加载）
# ----------------------------------------------------------------------------
_STATE: dict[str, Any] = {
    "model": None,
    "processor": None,
    "sample_rate": None,
    "model_path": None,
    "device": None,
    "dtype": None,
}


def _load_model(model_path: Path, device: torch.device, dtype: torch.dtype) -> None:
    if (
        _STATE["model"] is not None
        and _STATE["model_path"] == model_path
        and _STATE["device"] == device
        and _STATE["dtype"] == dtype
    ):
        return
    with _model_load_lock:
        # 二次检查（double-checked locking），避免并发首请求重复加载
        if (
            _STATE["model"] is not None
            and _STATE["model_path"] == model_path
            and _STATE["device"] == device
            and _STATE["dtype"] == dtype
        ):
            return
        from transformers import AutoModel, AutoProcessor

        # 修复 transformers 5.3 的 _patch_mistral_regex 重复参数 bug
        _patch_mistral_regex_safe()

        # 模型/处理器内部会调用已被弃用的 torch.nn.utils.weight_norm，
        # 其 FutureWarning 无实际影响，加载期间静默过滤避免刷屏。
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"`torch\.nn\.utils\.weight_norm` is deprecated",
                category=FutureWarning,
            )
            _attn_impl = os.environ.get("AUDIO8_ATTN", "sdpa")
            try:
                import flash_attn
                print(f"[FlashAttention] Audio8 注意力后端：{_attn_impl}（flash_attn {flash_attn.__version__} 可用）")
            except Exception:
                print(f"[FlashAttention] Audio8 注意力后端：{_attn_impl}（flash_attn 未安装；SDPA 仍可能走 memory-efficient 后端）")
            processor = AutoProcessor.from_pretrained(model_path, trust_remote_code=True)
            # 注意：transformers 4.52.1 下 from_pretrained 传 torch.dtype 会触发
            # config.to_json_string 序列化失败，故先以默认精度加载，再 .to() 转换。
            # attn_implementation 默认 sdpa（自动选 flash 后端）；设 AUDIO8_ATTN=flash_attention_2 强制 flash-attn 内核。
            model = AutoModel.from_pretrained(model_path, trust_remote_code=True, attn_implementation=_attn_impl)
            model = model.eval().to(device=device, dtype=dtype)
        _STATE.update(
            {
                "model": model,
                "processor": processor,
                "sample_rate": int(model.config.codec_sample_rate),
                "model_path": model_path,
                "device": device,
                "dtype": dtype,
            }
        )


# ----------------------------------------------------------------------------
# Pydantic 模型（API 契约）
# ----------------------------------------------------------------------------
class TTSRequest(BaseModel):
    text: str = Field(..., description="要合成的文本")
    reference_text: Optional[str] = Field(
        None, description="参考音频对应的文本；不提供时将自动用 ASR 识别参考音频内容"
    )
    model_name: str = Field("default", description="模型名称或路径；default 使用内置模型")
    device: str = Field("auto", description="auto / cpu / cuda / cuda:N")
    dtype: str = Field("auto", description="auto / bfloat16 / float16 / float32")
    max_new_tokens: int = Field(1024, description="最大生成 token 数（解码长度）")
    retry_max_new_tokens: int = Field(2000, description="未正常结束时的重试最大 token 数")
    temperature: float = Field(0.8, description="采样温度（>0）")
    top_p: float = Field(0.95, description="核采样概率 (0,1]")
    top_k: int = Field(50, description="Top-K 采样")
    seed: int = Field(42, description="随机种子")
    greedy: bool = Field(False, description="True 则贪心解码（关闭采样）")
    save_codes: bool = Field(False, description="是否额外保存声学 token (.npy)")
    reference_audio: Optional[str] = Field(
        None, description="参考音频文件路径（服务端已存在的文件）。与上传文件二选一"
    )


class BatchRow(BaseModel):
    text: str
    reference_audio: Optional[str] = None
    # 参考文本可省略：提供参考音频但缺文本时，自动用 ASR 识别参考音频内容
    reference_text: Optional[str] = None


class BatchRequest(BaseModel):
    items: list[BatchRow] = Field(..., description="批量合成条目")
    model_name: str = Field("default", description="模型名称或路径")
    device: str = Field("auto")
    dtype: str = Field("auto")
    max_new_tokens: int = Field(1024)
    retry_max_new_tokens: int = Field(2000)
    temperature: float = Field(0.8)
    top_p: float = Field(0.95)
    top_k: int = Field(50)
    seed: int = Field(42)
    greedy: bool = Field(False)
    save_codes: bool = Field(False)
    batch_size: int = Field(1, description="批处理大小")


# ----------------------------------------------------------------------------
# FastAPI 应用
# ----------------------------------------------------------------------------
app = FastAPI(
    title="Audio8_TTS API",
    description="Audio8_TTS 多模态 TTS 服务（文本 / 参考音频音色克隆 / 批量）",
    version="1.0.0",
)

# 允许跨域，便于本地 HTML 页面（file:// 或不同端口）调用本 API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _synthesize_one(
    text: str,
    reference_audio: Optional[Path],
    reference_text: Optional[str],
    device: torch.device,
    dtype: torch.dtype,
    model_path: Path,
    max_new_tokens: int,
    retry_max_new_tokens: int,
    temperature: float,
    top_p: float,
    top_k: int,
    seed: int,
    greedy: bool,
    save_codes: bool,
    output_path: Path,
) -> dict[str, Any]:
    """对单条文本（可带参考音频）执行合成，返回结果记录。"""
    _load_model(model_path, device, dtype)
    model = _STATE["model"]
    processor = _STATE["processor"]
    sample_rate = _STATE["sample_rate"]

    generator = torch.Generator(device=device).manual_seed(seed)
    processor_kwargs: dict[str, Any] = {
        "text": [text],
        "return_tensors": "pt",
    }
    if reference_audio is not None:
        processor_kwargs.update(
            reference_audio=[str(reference_audio)],
            reference_text=[reference_text or ""],
        )
    inputs = processor(**processor_kwargs)
    inputs = {name: value.to(device) for name, value in inputs.items()}
    with _gpu_lock():
        output = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            do_sample=not greedy,
            generator=generator,
            return_dict_in_generate=True,
        )
        finished = bool(output.finished[0])
        code_length = int(output.code_lengths[0])
        codes = output.codes[0, :, :code_length]
        waveforms, waveform_lengths = model.decode_audio(output.codes)
    waveform = waveforms[0, : int(waveform_lengths[0])].float().cpu().numpy()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(output_path, waveform, sample_rate)
    record: dict[str, Any] = {
        "status": "OK" if finished else "NO_EOS",
        "output_audio": str(output_path),
        "reference_audio": str(reference_audio) if reference_audio else None,
        "code_frames": int(codes.shape[1]),
        "waveform_samples": int(waveform.shape[0]),
        "sample_rate": sample_rate,
    }
    # 异常短输出诊断：参考文本与参考音频内容不符时模型常只生成几帧便提前结束
    if codes.shape[1] < 10:
        record["warning"] = (
            "输出极短(code_frames<10)：参考文本可能与参考音频内容不匹配，"
            "或参考音频质量过差/过短。Audio8 要求参考文本为参考音频的精确转写。"
        )
    if save_codes:
        codes_path = output_path.with_suffix(".npy")
        import numpy as np

        np.save(codes_path, codes.cpu().numpy())
        record["codes_file"] = str(codes_path)
    return record


def _resolve_reference(
    reference_audio_file: Optional[UploadFile],
    reference_text: Optional[str],
    existing_path: Optional[str],
) -> tuple[Optional[Path], Optional[str]]:
    """解析参考音频：优先使用上传文件，其次使用服务端已存在路径。"""
    if reference_audio_file is not None and reference_audio_file.filename:
        suffix = Path(reference_audio_file.filename).suffix or ".wav"
        ref_path = UPLOAD_DIR / f"ref_{uuid.uuid4().hex}{suffix}"
        with ref_path.open("wb") as f:
            f.write(reference_audio_file.file.read())
        return ref_path, reference_text
    if existing_path:
        p = Path(existing_path)
        if not p.is_absolute():
            p = (PROJECT_ROOT / p).resolve()
        if not p.is_file():
            raise HTTPException(status_code=400, detail=f"参考音频不存在: {existing_path}")
        return p, reference_text
    if reference_text:
        raise HTTPException(
            status_code=400, detail="提供了 reference_text 但未提供 reference_audio"
        )
    return None, None


# ----------------------------------------------------------------------------
# ASR：参考音频自动转写（懒加载单例）
# ----------------------------------------------------------------------------
_ASR_STATE: dict[str, Any] = {"instance": None, "device": None}


def _get_asr(device: torch.device):
    """惰性加载 ASR（Whisper）用于参考音频自动转写，跨请求复用实例。"""
    from tools.asr import ASR

    asr_device = "cuda" if str(device).startswith("cuda") else "cpu"
    with _asr_load_lock:
        if _ASR_STATE["instance"] is None or _ASR_STATE["device"] != asr_device:
            if _ASR_STATE["instance"] is not None:
                _ASR_STATE["instance"] = None  # 设备变化时释放旧实例，避免残留显存占用
            print(f"[audio8] 加载 ASR (Whisper) 以自动转写参考音频, 设备: {asr_device} ...")
            _ASR_STATE["instance"] = ASR(device=asr_device)
            _ASR_STATE["device"] = asr_device
    return _ASR_STATE["instance"]


def _ensure_reference_text(
    ref_path: Optional[Path],
    ref_text: Optional[str],
    device: torch.device,
) -> tuple[Optional[Path], Optional[str], bool]:
    """补全参考文本：仅提供参考音频而缺失参考文本时，用 ASR 自动转写。

    返回 (ref_path, ref_text, used_asr)。ASR 失败时回退为 400 提示，
    避免静默使用空文本导致极短输出。
    """
    if ref_path is None:
        if ref_text:
            raise HTTPException(
                status_code=400, detail="提供了 reference_text 但未提供 reference_audio"
            )
        return None, None, False
    if ref_text:
        return ref_path, ref_text, False
    try:
        asr = _get_asr(device)
        with _gpu_lock():
            ref_text = asr.transcribe(str(ref_path))
        if not ref_text:
            raise ValueError("ASR 识别结果为空")
        print(f"[audio8] 参考音频自动转写完成: {ref_text!r}")
        return ref_path, ref_text, True
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"[audio8] ASR 自动转写失败: {e}")
        raise HTTPException(
            status_code=400,
            detail=(
                "提供了 reference_audio 但未提供 reference_text，且 ASR 自动转写失败："
                f"{e}"
            ),
        ) from e


# ----------------------------------------------------------------------------
# 路由
# ----------------------------------------------------------------------------
@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse(
        "<html><head><meta charset='utf-8'><title>Audio8_TTS API</title></head>"
        "<body style='font-family:sans-serif;padding:2rem'>"
        "<h1>Audio8_TTS API</h1>"
        "<p>多模态 TTS 服务已运行。可用端点：</p>"
        "<ul>"
        "<li><b>POST /tts</b> — 文本 / 参考音频 合成（支持文件上传或路径）</li>"
        "<li><b>POST /tts/json</b> — 批量合成（JSON）</li>"
        "<li><b>GET /models</b> — 模型信息</li>"
        "<li><b>GET /docs</b> — 交互式 API 文档</li>"
        "</ul></body></html>"
    )


@app.get("/models")
async def models() -> JSONResponse:
    info = {
        "current_model": str(_STATE["model_path"]) if _STATE["model_path"] else None,
        "loaded": _STATE["model"] is not None,
        "device": str(_STATE["device"]) if _STATE["device"] else None,
        "dtype": str(_STATE["dtype"]) if _STATE["dtype"] else None,
        "sample_rate": _STATE["sample_rate"],
        "default_model_path": str(DEFAULT_MODEL),
        "managed_models": models_manager.list_models(),
    }
    return JSONResponse(info)


@app.get("/health")
async def health() -> JSONResponse:
    # 与项目其它接口（如 confucius4）范式对齐：status=healthy + model_loaded
    # Audio8 为惰性加载（首次合成时自动加载），服务存活即可用，故常置 True。
    device = _STATE.get("device")
    return JSONResponse({
        "status": "healthy",
        "model_loaded": True,
        "device": device if device else "auto",
        "model": str(_STATE.get("model_path")) if _STATE.get("model_path") else None,
    })


def _tts_blocking(
    text: str,
    model_name: str,
    device: str,
    dtype: str,
    max_new_tokens: int,
    retry_max_new_tokens: int,
    temperature: float,
    top_p: float,
    top_k: int,
    seed: int,
    greedy: bool,
    save_codes: bool,
    ref_path: Optional[Path],
    ref_text: Optional[str],
) -> dict[str, Any]:
    """阻塞式合成（在线程池中运行）：补全参考文本(ASR) + 推理。"""
    dev = resolve_device(device)
    dt = resolve_dtype(dtype, dev)
    ref_path, ref_text, used_asr = _ensure_reference_text(ref_path, ref_text, dev)
    mp = ensure_model("audio8") if model_name == "default" else find_model_path(model_name)

    out_name = f"{uuid.uuid4().hex}.wav"
    out_path = OUTPUT_DIR / out_name
    record = _synthesize_one(
        text=text,
        reference_audio=ref_path,
        reference_text=ref_text,
        device=dev,
        dtype=dt,
        model_path=mp,
        max_new_tokens=max_new_tokens,
        retry_max_new_tokens=retry_max_new_tokens,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        seed=seed,
        greedy=greedy,
        save_codes=save_codes,
        output_path=out_path,
    )
    record["audio_url"] = f"/audio/{out_name}"
    if used_asr:
        record["reference_text_source"] = "asr"
        record["asr_reference_text"] = ref_text
    return record


@app.post("/tts")
async def tts(
    text: str = Form(..., description="要合成的文本"),
    reference_text: Optional[str] = Form(None),
    reference_audio_file: Optional[UploadFile] = File(None),
    reference_audio: Optional[str] = Form(None),
    model_name: str = Form("default"),
    device: str = Form("auto"),
    dtype: str = Form("auto"),
    max_new_tokens: int = Form(1024),
    retry_max_new_tokens: int = Form(2000),
    temperature: float = Form(0.8),
    top_p: float = Form(0.95),
    top_k: int = Form(50),
    seed: int = Form(42),
    greedy: bool = Form(False),
    save_codes: bool = Form(False),
):
    """文本合成（纯文本或参考音频音色克隆）。

    参考音频来源二选一：
      - 通过 multipart 上传（reference_audio_file）
      - 通过 reference_audio 传入服务端已存在的文件路径

    参考文本 reference_text 可不提供：仅传参考音频时，自动用 ASR
    识别参考音频内容作为参考文本（响应中带 reference_text_source=asr）。

    注意：阻塞的 ASR + 模型推理在线程池中执行，避免卡死事件循环。
    """
    try:
        # 参考音频落盘需在事件循环中完成（请求流随处理器退出即失效）
        ref_path, ref_text = _resolve_reference(
            reference_audio_file, reference_text, reference_audio
        )
        loop = asyncio.get_running_loop()
        record = await loop.run_in_executor(
            _executor,
            functools.partial(
                _tts_blocking,
                text, model_name, device, dtype,
                max_new_tokens, retry_max_new_tokens, temperature, top_p, top_k,
                seed, greedy, save_codes, ref_path, ref_text,
            ),
        )
        return JSONResponse(record)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"合成失败: {exc}") from exc


def _batch_blocking(req: BatchRequest) -> dict[str, Any]:
    """阻塞式批量合成（在线程池中运行）。内部逐条 ASR + 推理，受 GPU 信号量串行化。"""
    dev = resolve_device(req.device)
    dt = resolve_dtype(req.dtype, dev)
    mp = ensure_model("audio8") if req.model_name == "default" else find_model_path(req.model_name)

    records: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    items = req.items
    bs = max(1, req.batch_size)
    for start in range(0, len(items), bs):
        group = items[start : start + bs]
        for row in group:
            try:
                ref_path = None
                ref_text = row.reference_text
                if row.reference_audio:
                    p = Path(row.reference_audio)
                    if not p.is_absolute():
                        p = (PROJECT_ROOT / p).resolve()
                    if not p.is_file():
                        raise FileNotFoundError(
                            f"参考音频不存在: {row.reference_audio}"
                        )
                    ref_path = p
                # 仅提供参考音频而缺参考文本时，自动用 ASR 转写补全
                ref_path, ref_text, used_asr = _ensure_reference_text(
                    ref_path, ref_text, dev
                )
                out_name = f"{uuid.uuid4().hex}.wav"
                out_path = OUTPUT_DIR / out_name
                rec = _synthesize_one(
                    text=row.text,
                    reference_audio=ref_path,
                    reference_text=ref_text,
                    device=dev,
                    dtype=dt,
                    model_path=mp,
                    max_new_tokens=req.max_new_tokens,
                    retry_max_new_tokens=req.retry_max_new_tokens,
                    temperature=req.temperature,
                    top_p=req.top_p,
                    top_k=req.top_k,
                    seed=req.seed,
                    greedy=req.greedy,
                    save_codes=req.save_codes,
                    output_path=out_path,
                )
                rec["audio_url"] = f"/audio/{out_name}"
                if used_asr:
                    rec["reference_text_source"] = "asr"
                    rec["asr_reference_text"] = ref_text
                records.append(rec)
            except Exception as exc:  # noqa: BLE001
                failures.append({"text": row.text, "error": str(exc)})
    return {"records": records, "failures": failures}


@app.post("/tts/json")
async def tts_json(req: BatchRequest):
    """批量合成。每条可带独立的参考音频路径（服务端路径）与参考文本。

    注意：整个批量循环在线程池中执行，避免卡死事件循环；内部 GPU 工作
    仍由 GPU 信号量串行化，保证不与其它请求争抢。
    """
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            _executor, functools.partial(_batch_blocking, req)
        )
        return JSONResponse(result)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"批量合成失败: {exc}") from exc


@app.get("/audio/{filename}")
async def audio(filename: str):
    """返回生成的音频文件。"""
    path = OUTPUT_DIR / filename
    if not path.is_file() or ".." in filename:
        raise HTTPException(status_code=404, detail="音频不存在")
    return FileResponse(path, media_type="audio/wav", filename=filename)


# 静态挂载 uploads / outputs，便于调试
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")
app.mount("/outputs", StaticFiles(directory=str(OUTPUT_DIR)), name="outputs")


# ----------------------------------------------------------------------------
# 启动入口
# ----------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audio8_TTS API Server")
    parser.add_argument("--host", default=None, help="服务器地址（默认读 config）")
    parser.add_argument("--port", type=int, default=None, help="服务器端口（默认读 config）")
    parser.add_argument("--device", default=None, help="计算设备 (cuda/cpu/mps)（默认读 config，且可按请求覆盖）")
    parser.add_argument("--dtype", default=None, help="推理精度 (auto/bf16/f16/f32)（默认读 config）")
    parser.add_argument("--max-workers", type=int, default=None, help="推理线程池大小（默认读 config）")
    parser.add_argument("--gpu-concurrency", type=int, default=None, help="GPU 并发推理数量（默认读 config）")
    return parser.parse_args()


if __name__ == "__main__":
    import uvicorn

    # 启动前确保模型统一存放在 models/Audio8（不存在则尝试自动下载）
    ensured = ensure_model("audio8")
    print(f"[audio8] 模型目录: {ensured}")

    srv_cfg = CONFIG.get("server", {})
    args = parse_args()

    # 解析有效值：命令行 > config > 内置默认
    host = args.host or srv_cfg.get("host", "0.0.0.0")
    port = args.port if args.port is not None else srv_cfg.get("port", 8007)
    device = args.device or srv_cfg.get("device", "auto")
    dtype = args.dtype or srv_cfg.get("dtype", "auto")
    max_workers = args.max_workers if args.max_workers is not None else srv_cfg.get("max_workers", 4)
    gpu_concurrency = args.gpu_concurrency if args.gpu_concurrency is not None else srv_cfg.get("gpu_concurrency", 1)

    # 初始化并发控制：线程池 + GPU 信号量（串行化 GPU 推理/ASR，避免争抢与显存叠加）
    _executor = ThreadPoolExecutor(max_workers=max_workers)
    _gpu_semaphore = threading.Semaphore(gpu_concurrency)
    print(
        f"[audio8] 并发配置: max_workers={max_workers}, "
        f"gpu_concurrency={gpu_concurrency}（配置来源 config/audio8_server.yaml）"
    )

    uvicorn.run(app, host=host, port=port)
