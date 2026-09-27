# -*- coding: utf-8 -*-
"""
IndexTTS-2.5 FastAPI 服务器（聚合到 LcTTSHub）

功能：
  * 零样本语音克隆（参考音频 3~10 秒）
  * 情感控制三种方式：
      - reference : 参考音频即情感（默认）
      - vector    : 8 维情感向量 [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]
      - instruct  : 文本指令控制情感（依赖 QwenEmotion 模型，需 use_qwen_emo=True 加载）
  * 多语言：zh / en / ja / es / zhen（中文夹杂英文）
  * speed 语速调节（>1 加快，<1 放慢）、interval_silence 句间停顿
  * 提供 REST / WebSocket / SSE 三种接口

启动示例：
  py312env\\Scripts\\python.exe index_tts25\\index_api_server.py
  # 说明：host / port / model / max-workers / bf16 / use-qwen-emo / device
  # 等启动参数统一由 config/index25_server.yaml 提供，命令行可临时覆盖。
"""

import argparse
import asyncio
import base64
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
import warnings
import wave
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# 专有依赖补丁包（packages/index25）
# ---------------------------------------------------------------------------
# IndexTTS-2.5 需要的 transformers==4.52.1 / tokenizers==0.21.4 / huggingface_hub==0.36.2
# 与主环境 py312env（transformers 5.x）冲突。补丁包路径的优先插入逻辑已放在
# indextts/__init__.py：只要 import indextts 即自动将 packages/index25 置于 sys.path 最前，
# 覆盖同名包并复用 py312env 的 torch。无需在本文件重复处理。

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
warnings.filterwarnings("ignore")

import numpy as np
import uvicorn
import yaml
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(ROOT_DIR)  # LcTTSHub 根目录
sys_path = os.path.join(ROOT_DIR, "lib")
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# 服务器配置（统一由 config/index25_server.yaml 提供，文件缺失回退内置默认）
# ---------------------------------------------------------------------------
SERVER_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "index25_server.yaml")

DEFAULT_SERVER_CONFIG = {
    "server": {
        "host": "0.0.0.0",
        "port": 8858,
        "device": None,
        "max_workers": 1,
        "max_concurrency": 1,
    },
    "model": {
        "model_dir": "models/index25",
        "use_bf16": True,
        "use_qwen_emo": True,
        "device": None,
    },
    "inference": {
        "temperature": 1.0,
        "top_p": 0.9,
        "top_k": 50,
        "num_beams": 1,
        "repetition_penalty": 1.2,
        "length_penalty": 1.0,
        "max_mel_tokens": 3000,
        "speed": 1.0,
        "interval_silence": 200,
        "max_text_tokens_per_segment": 120,
        "do_sample": True,
        "text_normalization": True,
        "use_random": False,
        "emotion_alpha": 1.0,
    },
}


def load_server_config():
    """加载 config/index25_server.yaml，缺失/异常时回退到默认配置。"""
    cfg = {k: dict(v) for k, v in DEFAULT_SERVER_CONFIG.items()}
    if os.path.isfile(SERVER_CONFIG_PATH):
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


# ---------------------------------------------------------------------------
# 推理并发控制
# 限制同时执行的 model_instance.infer 数量，避免批量请求并行抢占同一张 GPU
# 与同一个共享模型实例（IndexTTS-2.5 的 infer 并非线程安全）导致变慢 / 显存叠加
# OOM / 结果错乱。默认 1 = 严格串行，批量请求自动排队（FIFO）。
# 数值来自 config/index25_server.yaml 的 server.max_concurrency。
# ---------------------------------------------------------------------------
_INFER_MAX_CONCURRENCY = max(1, int(CONFIG.get("server", {}).get("max_concurrency", 1) or 1))
infer_semaphore = threading.Semaphore(_INFER_MAX_CONCURRENCY)


from tools.models_manager import ModelsManager

_models_manager = ModelsManager()

SAMPLING_RATE = 22050
model_instance = None
model_meta = {"loaded": False, "model_dir": "", "use_bf16": True, "use_qwen_emo": True}
task_state = {
    "task_id": None,
    "current": 0,
    "total": 0,
    "status": "idle",  # idle / running / done / error
    "error": "",
    "output_wav": None,
    "progress_ready": asyncio.Event(),
    "result_ready": asyncio.Event(),
    "elapsed": 0.0,
}

app = FastAPI(
    title="IndexTTS-2.5 API",
    description="IndexTTS-2.5 语音合成服务器（LcTTSHub 聚合）\n\n"
    "支持多语言 zh/en/ja/es/zhen、三种情感控制方式（reference / vector / instruct）",
    version="2.5.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 模型加载
# ---------------------------------------------------------------------------
def load_index_model(model_dir: str, use_bf16: bool = True, use_qwen_emo: bool = True, device: str = None):
    """加载 IndexTTS-2.5 模型（依赖 models_manager 解析路径）"""
    global model_instance, model_meta
    model_dir = _models_manager.resolve_path(model_dir, model_type="indextts")
    if not os.path.isabs(model_dir):
        model_dir = os.path.abspath(model_dir)

    cfg_path = os.path.join(model_dir, "config.yaml")
    if not os.path.exists(cfg_path):
        raise FileNotFoundError(f"未找到配置文件: {cfg_path}")

    print(f">> 正在加载 IndexTTS-2.5 模型: {model_dir}")
    print(f"   use_bf16={use_bf16}, use_qwen_emo={use_qwen_emo}")
    t0 = time.time()

    from indextts.infer_v2_5 import IndexTTS2

    model_instance = IndexTTS2(
        cfg_path=cfg_path,
        model_dir=model_dir,
        use_bf16=use_bf16,
        use_qwen_emo=use_qwen_emo,
        device=device,
    )
    model_meta.update(
        loaded=True,
        model_dir=model_dir,
        use_bf16=use_bf16,
        use_qwen_emo=use_qwen_emo,
    )
    print(f">> 模型加载完成，耗时 {time.time() - t0:.1f}s")


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
INDEX25_SPEED_MIN = 0.3
INDEX25_SPEED_MAX = 3.0


def _clamp_index25_speed(v: Optional[float]) -> Optional[float]:
    """截断语速到合法范围，避免报错"""
    if v is None:
        return v
    return max(INDEX25_SPEED_MIN, min(INDEX25_SPEED_MAX, v))


class VoiceCloneRequest(BaseModel):
    input_text: str = Field(..., description="要合成的文本")
    speaker_audio: Optional[str] = Field(None, description="参考音频：自适应识别——优先按路径判断（存在该文件则视为路径），否则按 base64 解码（wav/mp3/flac）")
    speaker_audio_path: Optional[str] = Field(None, description="服务端参考音频路径（uploads/voice 目录下的文件名或绝对路径）；显式指定时优先于 speaker_audio")
    output_path: Optional[str] = Field(None, description="输出音频保存路径（绝对路径或相对 LcTTSHub 根目录）；不传则返回 JSON 内嵌 audio_base64")
    lang: str = Field("zh", description="语言：zh/en/ja/es/zhen")
    speed: Optional[float] = Field(None, description="语速因子，>1 加快，<1 放慢（0.3~3.0，超限自动截断）；留空则使用 config 默认值 1.0")

    @field_validator('speed', mode='before')
    @classmethod
    def clamp_speed(cls, v):
        return _clamp_index25_speed(v)
    text_normalization: Optional[bool] = Field(None, description="是否做文本归一化；留空则使用 config 默认")
    interval_silence: Optional[int] = Field(None, description="句间停顿毫秒数；留空则使用 config 默认")
    max_text_tokens_per_segment: Optional[int] = Field(None, description="每段最大 token 数；留空则使用 config 默认")
    use_random: Optional[bool] = Field(None, description="每次随机采样（同一文本每次声音略有不同）；留空则使用 config 默认")
    emo_control_method: str = Field("reference", description="情感控制方式：reference / vector / instruct")
    emo_vector: Optional[str] = Field(None, description="8 维情感向量，JSON 数组字符串，如 [0.8,0.1,0,0,0,0,0.1,0]")
    emo_audio: Optional[str] = Field(None, description="情感参考音频 base64（emo_control_method=reference 且需与克隆音色不同时使用）")
    instruct: Optional[str] = Field(None, description="文本指令情感（emo_control_method=instruct），如：用开心的语气说话")
    emotion_text: Optional[str] = Field(None, description="从该文本推断情感（与 instruct 二选一）")
    emotion_alpha: Optional[float] = Field(None, description="情感强度 0~1（instruct/vector 有效）；留空则使用 config 默认")
    do_sample: Optional[bool] = Field(None, description="是否采样；留空则使用 config 默认")
    top_p: Optional[float] = Field(None, description="top_p；留空则使用 config 默认")
    top_k: Optional[int] = Field(None, description="top_k；留空则使用 config 默认")
    temperature: Optional[float] = Field(None, description="temperature；留空则使用 config 默认")
    num_beams: Optional[int] = Field(None, description="beam 数量；留空则使用 config 默认")
    repetition_penalty: Optional[float] = Field(None, description="重复惩罚；留空则使用 config 默认")
    length_penalty: Optional[float] = Field(None, description="长度惩罚；留空则使用 config 默认")
    max_mel_tokens: Optional[int] = Field(None, description="最大 mel token 数；留空则使用 config 默认")
    verbose: bool = Field(False, description="打印详细日志")
    need_progress: bool = Field(False, description="是否通过 SSE 推送进度")


MODEL_ARGS = {"model_dir": "models/index25", "use_bf16": True, "use_qwen_emo": True, "device": None}


def _model_args_from_env():
    """模型参数优先级：环境变量 > config/index25_server.yaml > 内置默认。

    环境变量方式保留用于 uvicorn 多 worker 场景（子进程继承环境变量）。
    """
    model_cfg = CONFIG.get("model", {})
    use_bf16_env = os.environ.get("INDEX25_USE_BF16")
    use_qwen_env = os.environ.get("INDEX25_USE_QWEN_EMO")
    return {
        "model_dir": os.environ.get("INDEX25_MODEL_DIR") or model_cfg.get("model_dir", MODEL_ARGS["model_dir"]),
        "use_bf16": (use_bf16_env != "0") if use_bf16_env is not None else model_cfg.get("use_bf16", True),
        "use_qwen_emo": (use_qwen_env != "0") if use_qwen_env is not None else model_cfg.get("use_qwen_emo", True),
        "device": os.environ.get("INDEX25_DEVICE") or model_cfg.get("device") or None,
    }


@app.on_event("startup")
async def startup_event():
    # 每个 worker 进程都会在启动时加载模型
    global model_instance
    if model_instance is None:
        await asyncio.to_thread(load_index_model, **_model_args_from_env())


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def resolve_audio_source(audio_value: Optional[str], field_name: str = "speaker_audio") -> Optional[str]:
    """自适应识别音频输入，返回本地文件路径或 None。

    识别顺序：
      1) 路径优先：字符串本身是存在的文件，或相对 LcTTSHub 根目录 / uploads / voice 目录存在，则视为路径；
      2) 否则尝试 base64 解码为临时文件（wav/mp3/m4a）。
    """
    if not audio_value:
        return None
    audio_value = audio_value.strip()

    # ---- 1) 路径优先 ----
    if os.path.exists(audio_value):
        return os.path.abspath(audio_value)
    cand = os.path.join(PROJECT_ROOT, audio_value)
    if os.path.exists(cand):
        return os.path.abspath(cand)
    for base in ["uploads", "voice"]:
        cand = os.path.join(PROJECT_ROOT, base, audio_value)
        if os.path.exists(cand):
            return os.path.abspath(cand)

    # ---- 2) base64 解码 ----
    try:
        raw = base64.b64decode(audio_value)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"{field_name} 既不是有效路径也不是合法 base64: {e}")
    suffix = ".wav"
    if raw[:4] == b"RIFF":
        suffix = ".wav"
    elif raw[:3] == b"ID3" or raw[8:12] == b"ftyp":
        suffix = ".mp3"
    elif raw[4:8] == b"ftyp":
        suffix = ".m4a"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix, dir=tempfile.gettempdir())
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    return tmp_path


def wav_info(path: str):
    """返回 (sample_rate, duration_seconds)，duration 保留 3 位小数"""
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate()
        frames = wf.getnframes()
    duration = round(frames / float(sr), 3) if sr else 0.0
    return sr, duration


def build_tts_result(output_path: str, embed_base64: bool) -> dict:
    """构造合成结果：
      - embed_base64=True（未传 output_path）：包含 audio_base64 / format / sample_rate / duration
      - embed_base64=False（传入 output_path）：包含 path / format / sample_rate / duration
    """
    sr, duration = wav_info(output_path)
    result = {
        "path": output_path,
        "format": "wav",
        "sample_rate": sr,
        "duration": duration,
    }
    if embed_base64:
        result["audio_base64"] = base64.b64encode(wav_bytes_from_file(output_path)).decode("utf-8")
    return result


# ---------------------------------------------------------------------------
# 任务执行
# ---------------------------------------------------------------------------
# 推理参数优先级：请求显式传值 > config/index25_server.yaml > 代码默认值
INF_CFG = CONFIG.get("inference", {})


def _resolve(field_value, key: str, default):
    """解析单个推理参数：请求显式传值优先；未传则回退 config；再否则用代码默认值。"""
    if field_value is not None:
        return field_value
    if key in INF_CFG and INF_CFG[key] is not None:
        return INF_CFG[key]
    return default


def execute_voice_clone(request: VoiceCloneRequest) -> dict:
    """执行语音克隆，返回合成结果 dict（见 build_tts_result）"""
    global model_instance, task_state
    if model_instance is None:
        raise HTTPException(status_code=500, detail="模型尚未加载，请先加载模型")

    # ---- 准备参考音频（自适应：路径优先，否则 base64）----
    if request.speaker_audio_path:
        ref_path = resolve_audio_source(request.speaker_audio_path, "speaker_audio_path")
        if ref_path is None:
            raise HTTPException(status_code=400, detail=f"参考音频路径不存在: {request.speaker_audio_path}")
    elif request.speaker_audio:
        ref_path = resolve_audio_source(request.speaker_audio, "speaker_audio")
        if ref_path is None:
            raise HTTPException(status_code=400, detail="必须提供 speaker_audio 或 speaker_audio_path")
    else:
        raise HTTPException(status_code=400, detail="必须提供 speaker_audio 或 speaker_audio_path")

    # ---- 准备情感参考音频（同样自适应）----
    emo_audio_path = resolve_audio_source(request.emo_audio, "emo_audio")

    # ---- 组装 infer 参数 ----
    kwargs = dict(
        spk_audio_prompt=ref_path,
        text=request.input_text,
        lang=request.lang,
        output_path=None,  # 占位，下方生成
        emo_alpha=_resolve(request.emotion_alpha, "emotion_alpha", 1.0),
        use_random=_resolve(request.use_random, "use_random", False),
        interval_silence=_resolve(request.interval_silence, "interval_silence", 200),
        max_text_tokens_per_segment=_resolve(request.max_text_tokens_per_segment, "max_text_tokens_per_segment", 120),
        # speed >1 加快，<1 放慢；模型内部 duration_factor 语义相反，需取倒数
        duration_factor=1.0 / max(0.1, _resolve(request.speed, "speed", 1.0)),
        text_normalization=_resolve(request.text_normalization, "text_normalization", True),
        verbose=request.verbose,
        do_sample=_resolve(request.do_sample, "do_sample", True),
        top_p=_resolve(request.top_p, "top_p", 0.9),
        top_k=_resolve(request.top_k, "top_k", 50),
        temperature=_resolve(request.temperature, "temperature", 1.0),
        num_beams=_resolve(request.num_beams, "num_beams", 1),
        repetition_penalty=_resolve(request.repetition_penalty, "repetition_penalty", 1.2),
        length_penalty=_resolve(request.length_penalty, "length_penalty", 1.0),
        max_mel_tokens=_resolve(request.max_mel_tokens, "max_mel_tokens", 3000),
    )

    method = request.emo_control_method.lower()
    if method == "reference":
        kwargs["emo_audio_prompt"] = emo_audio_path
    elif method == "vector":
        if request.emo_vector:
            try:
                vec = json.loads(request.emo_vector)
                vec = [float(x) for x in vec]
                if len(vec) != 8:
                    raise ValueError("emo_vector 必须为 8 个数值")
                kwargs["emo_vector"] = vec
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"emo_vector 解析失败: {e}")
    elif method == "instruct":
        emo_text = request.instruct or request.emotion_text
        if not emo_text:
            raise HTTPException(status_code=400, detail="emo_control_method=instruct 时需提供 instruct 或 emotion_text")
        if not model_meta.get("use_qwen_emo"):
            raise HTTPException(status_code=400, detail="当前模型未加载 QwenEmotion，无法使用 instruct 情感控制")
        kwargs["use_emo_text"] = True
        kwargs["emo_text"] = emo_text
    else:
        raise HTTPException(status_code=400, detail="emo_control_method 仅支持 reference / vector / instruct")

    # ---- 生成输出路径（未传 output_path 时写入 outputs/index25）----
    task_id = uuid.uuid4().hex[:8]
    if request.output_path:
        output_path = request.output_path
        if not os.path.isabs(output_path):
            output_path = os.path.join(PROJECT_ROOT, output_path)
        output_path = os.path.abspath(output_path)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
    else:
        output_dir = os.path.join(PROJECT_ROOT, "outputs", "index25")
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, f"tts_{task_id}.wav")
    kwargs["output_path"] = output_path

    # ---- 执行推理 ----
    task_state["task_id"] = task_id
    task_state["status"] = "running"
    task_state["current"] = 0
    task_state["total"] = 1
    task_state["error"] = ""
    task_state["output_wav"] = None
    t0 = time.time()
    try:
        with infer_semaphore:
            result = model_instance.infer(**kwargs)
        # v2.5 行为：传入 output_path 时 infer 返回保存路径字符串；
        # 个别路径可能返回 (sr, wav) 元组，需手动保存。
        if result is None:
            raise RuntimeError("推理未生成音频")
        if isinstance(result, tuple):
            sr, wav = result
            wav_data = np.asarray(wav)
            if wav_data.ndim == 1:
                wav_data = wav_data[None, :]
            else:
                wav_data = wav_data.T
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            import torch
            import torchaudio
            torchaudio.save(output_path, torch.from_numpy(wav_data.copy()), sr)
        task_state["status"] = "done"
        task_state["output_wav"] = output_path
        task_state["elapsed"] = time.time() - t0
        return build_tts_result(output_path, request.output_path is None)
    except Exception as e:
        task_state["status"] = "error"
        task_state["error"] = str(e)
        raise HTTPException(status_code=500, detail=f"推理失败: {e}")
    finally:
        task_state["result_ready"].set()


def wav_bytes_from_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


# ---------------------------------------------------------------------------
# REST 接口
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "model_loaded": model_meta["loaded"],
        "model_dir": model_meta["model_dir"],
        "use_bf16": model_meta["use_bf16"],
        "use_qwen_emo": model_meta["use_qwen_emo"],
        "sampling_rate": SAMPLING_RATE,
        "languages": ["zh", "en", "ja", "es", "zhen"],
        "emotion_methods": ["reference", "vector", "instruct"],
    }


@app.post("/api/tts")
async def tts_api(request: VoiceCloneRequest):
    """自适应语音克隆合成接口：
      - 不传 output_path：返回 JSON { audio_base64, format, sample_rate, duration }（audio_base64 不含 data: 前缀）
      - 传入 output_path：音频保存到指定路径，返回 { file, format, sample_rate, duration }
    """
    if model_instance is None:
        raise HTTPException(status_code=500, detail="模型尚未加载，请先加载模型")
    result = await asyncio.to_thread(execute_voice_clone, request)
    if request.output_path:
        return {
            "code": 0,
            "file": result["path"],
            "format": result["format"],
            "sample_rate": result["sample_rate"],
            "duration": result["duration"],
            "elapsed": round(task_state["elapsed"], 2),
        }
    return {
        "code": 0,
        "audio_base64": result["audio_base64"],
        "format": result["format"],
        "sample_rate": result["sample_rate"],
        "duration": result["duration"],
        "elapsed": round(task_state["elapsed"], 2),
    }


@app.post("/api/tts/base64")
async def tts_api_base64(request: VoiceCloneRequest):
    """语音克隆合成，返回 base64 JSON（兼容接口，等价于 /api/tts 不传 output_path）"""
    if model_instance is None:
        raise HTTPException(status_code=500, detail="模型尚未加载，请先加载模型")
    result = await asyncio.to_thread(execute_voice_clone, request)
    return {
        "code": 0,
        "audio_base64": result["audio_base64"],
        "format": result["format"],
        "sample_rate": result["sample_rate"],
        "duration": result["duration"],
        "file": os.path.basename(result["path"]),
        "elapsed": round(task_state["elapsed"], 2),
    }


@app.post("/api/tts/form")
async def tts_api_form(
    input_text: str = Form(...),
    speaker_audio: Optional[str] = Form(None),
    speaker_audio_path: Optional[str] = Form(None),
    lang: str = Form("zh"),
    speed: float = Form(1.0, description="语速因子，>1 加快，<1 放慢（0.5~2.0）"),
    text_normalization: bool = Form(True),
    interval_silence: int = Form(200),
    use_random: bool = Form(False),
    emo_control_method: str = Form("reference"),
    emo_vector: Optional[str] = Form(None),
    emo_audio: Optional[str] = Form(None),
    instruct: Optional[str] = Form(None),
    emotion_text: Optional[str] = Form(None),
    emotion_alpha: float = Form(1.0),
    temperature: float = Form(1.0),
    top_p: float = Form(0.9),
    top_k: int = Form(50),
    max_mel_tokens: int = Form(3000),
    need_progress: bool = Form(False),
    output_path: Optional[str] = Form(None, description="输出音频保存路径；不传则返回 wav 音频流"),
):
    """语音克隆合成（表单方式），返回 wav 音频流"""
    req = VoiceCloneRequest(
        input_text=input_text,
        speaker_audio=speaker_audio,
        speaker_audio_path=speaker_audio_path,
        lang=lang,
        speed=speed,
        text_normalization=text_normalization,
        interval_silence=interval_silence,
        use_random=use_random,
        emo_control_method=emo_control_method,
        emo_vector=emo_vector,
        emo_audio=emo_audio,
        instruct=instruct,
        emotion_text=emotion_text,
        emotion_alpha=emotion_alpha,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        max_mel_tokens=max_mel_tokens,
        need_progress=need_progress,
        output_path=output_path,
    )
    result = await asyncio.to_thread(execute_voice_clone, req)
    audio_bytes = wav_bytes_from_file(result["path"])
    return StreamingResponse(
        io.BytesIO(audio_bytes),
        media_type="audio/wav",
        headers={"Content-Disposition": f'attachment; filename="{os.path.basename(result["path"])}"'},
    )


@app.post("/api/tts/stream")
async def tts_stream(request: VoiceCloneRequest):
    """SSE 流式返回：先推送进度，再推送 wav base64"""
    async def gen():
        loop = asyncio.get_event_loop()
        future = loop.run_in_executor(None, execute_voice_clone, request)
        yield "data: " + json.dumps({"status": "start"}) + "\n\n"
        try:
            result = await future
            b64 = result.get("audio_base64")
            if b64 is None:
                b64 = base64.b64encode(wav_bytes_from_file(result["path"])).decode("utf-8")
            yield "data: " + json.dumps({
                "status": "done",
                "audio_base64": b64,
                "format": result["format"],
                "sample_rate": result["sample_rate"],
                "duration": result["duration"],
            }) + "\n\n"
        except Exception as e:
            detail = str(e)
            # 兼容 HTTPException
            if isinstance(e, HTTPException):
                detail = e.detail
            yield "data: " + json.dumps({"status": "error", "error": detail}) + "\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# WebSocket 接口
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        data = await websocket.receive_text()
        req = VoiceCloneRequest(**json.loads(data))
        loop = asyncio.get_event_loop()
        future = loop.run_in_executor(None, execute_voice_clone, req)
        await websocket.send_text(json.dumps({"type": "start"}))
        try:
            result = await future
            b64 = result.get("audio_base64")
            if b64 is None:
                b64 = base64.b64encode(wav_bytes_from_file(result["path"])).decode("utf-8")
            await websocket.send_text(json.dumps({
                "type": "done",
                "audio_base64": b64,
                "format": result["format"],
                "sample_rate": result["sample_rate"],
                "duration": result["duration"],
                "file": os.path.basename(result["path"]),
                "elapsed": round(task_state["elapsed"], 2),
            }))
        except Exception as e:
            await websocket.send_text(json.dumps({"type": "error", "error": str(e)}))
    except Exception as e:
        await websocket.send_text(json.dumps({"type": "error", "error": f"请求解析失败: {e}"}))
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 任务进度（v2.5 无流式音频，仅占位）
# ---------------------------------------------------------------------------
@app.get("/api/task/{task_id}")
async def get_task(task_id: str):
    if task_state["task_id"] != task_id:
        return {"task_id": task_id, "status": "not_found"}
    return {
        "task_id": task_id,
        "status": task_state["status"],
        "current": task_state["current"],
        "total": task_state["total"],
        "error": task_state["error"],
        "elapsed": round(task_state["elapsed"], 2),
    }


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def main():
    srv_cfg = CONFIG.get("server", {})
    model_cfg = CONFIG.get("model", {})

    parser = argparse.ArgumentParser(description="IndexTTS-2.5 API 服务器")
    # 以下参数均可通过命令行临时覆盖；不传则一律以 config/index25_server.yaml 为准。
    parser.add_argument("--host", type=str, default=None, help="监听地址（默认读 config）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认读 config）")
    parser.add_argument("--model", type=str, default=None, help="模型目录（默认读 config）")
    parser.add_argument("--max-workers", type=int, default=None, help="最大工作线程数（默认读 config）")
    parser.add_argument("--bf16", dest="bf16", action="store_true", default=None, help="使用 bf16（默认读 config）")
    parser.add_argument("--no-bf16", dest="bf16", action="store_false", help="关闭 bf16，使用 fp32")
    parser.add_argument("--use-qwen-emo", dest="use_qwen_emo", action="store_true", default=None, help="加载 QwenEmotion（默认读 config）")
    parser.add_argument("--no-qwen-emo", dest="use_qwen_emo", action="store_false", help="不加载 QwenEmotion，节省显存")
    parser.add_argument("--device", type=str, default=None, help="推理设备 cuda/cpu（默认读 config）")
    args = parser.parse_args()

    # ---- 解析有效值：命令行 > config > 内置默认 ----
    host = args.host or srv_cfg.get("host", "0.0.0.0")
    port = args.port if args.port is not None else srv_cfg.get("port", 8858)
    model_dir = args.model or model_cfg.get("model_dir", "models/index25")
    max_workers = args.max_workers if args.max_workers is not None else srv_cfg.get("max_workers", 1)
    use_bf16 = (args.bf16 if args.bf16 is not None else model_cfg.get("use_bf16", True))
    use_qwen_emo = (args.use_qwen_emo if args.use_qwen_emo is not None else model_cfg.get("use_qwen_emo", True))
    device = args.device or model_cfg.get("device") or None

    # 模型参数通过环境变量传给各 worker（在 startup 事件中加载）
    os.environ["INDEX25_MODEL_DIR"] = model_dir
    os.environ["INDEX25_USE_BF16"] = "1" if use_bf16 else "0"
    os.environ["INDEX25_USE_QWEN_EMO"] = "1" if use_qwen_emo else "0"
    if device:
        os.environ["INDEX25_DEVICE"] = device

    print(f">> IndexTTS-2.5 API 服务启动中: http://{host}:{port}")
    print(f">> 配置来源: config/index25_server.yaml")
    print(f">> 模型目录: {model_dir} | bf16={use_bf16} | use_qwen_emo={use_qwen_emo} | device={device or 'auto'} | max_concurrency={_INFER_MAX_CONCURRENCY}")
    print(f">> 接口列表:")
    print(f"   GET  /api/health        健康检查")
    print(f"   POST /api/tts           自适应合成（JSON；不传 output_path 返回 base64，传则保存文件）")
    print(f"   POST /api/tts/base64    合成（JSON，返回 base64，兼容）")
    print(f"   POST /api/tts/form      合成（表单，返回 wav 流）")
    print(f"   POST /api/tts/stream    SSE 流式")
    print(f"   WS   /ws                WebSocket")
    uvicorn.run(app, host=host, port=port, workers=max_workers)


if __name__ == "__main__":
    main()
