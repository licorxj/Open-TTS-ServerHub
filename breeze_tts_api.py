#!/usr/bin/env python3
"""
Breeze-TTS-2 FastAPI Server

Breeze TTS 2 (breezeblue-ai/breeze-tts) 的统一 API 封装，遵循本仓库其它引擎的风格：
- 健康检查：GET  /health
- 声音克隆（含 Voice Direction）：POST /api/v1/voice/clone
- 声音设计（无参考音频，纯指令）：POST /api/v1/voice/design
- 异步任务查询 / 下载 / 进度 / 删除（与 TTS 管家透传协议一致）

首次调用接口时，模型权重经 tools/models_manager 从 ModelScope 自动下载到 models/Breeze-TTS-2。

能力对照（Breeze-TTS-2 原生三种用法）：
- Voice Clone   : ref_audio + ref_text + text            -> /api/v1/voice/clone
- Voice Direction: ref_audio + ref_text + text + instruction -> /api/v1/voice/clone（带 instruction）
- Voice Design  : text + instruction（无参考音频）         -> /api/v1/voice/design

注意：
- Breeze-TTS-2 需要 NVIDIA CUDA GPU（推理运行时强制要求 cuda 设备）。
- 采样率固定 24000 Hz、单声道。
- 模型权重采用 BreezeBlue 研究与非商用许可，仅可用于研究与非商用场景。

Usage:
    python breeze_tts_api.py [--host 0.0.0.0] [--port 8860] [--device cuda] [--model path] [--max-workers 2]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import soundfile as sf
import torch
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

_project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _project_root)

# breeze-tts 上游代码（含 breeze_infer 与 models 两个顶层包），放在 breeze_tts/ 下。
_breeze_dir = os.path.join(_project_root, "breeze_tts")
if _breeze_dir not in sys.path:
    sys.path.insert(0, _breeze_dir)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
logging.getLogger("uvicorn.access").disabled = True

# ---------------------------------------------------------------------------
# 全局状态
# ---------------------------------------------------------------------------
runtime = None          # FastBreezeStreamingRuntime
model = None            # BreezeForConditionalGeneration
tokenizer = None
audio_tokenizer = None
device = None
sample_rate = 24000

DEFAULT_CFG_SCALE = 1.0
DESIGN_CFG_SCALE = 4.0
MAX_NEW_TOKENS = 1500
MAX_SEQ_LEN = 2048
REPETITION_PENALTY = 1.1

tasks: Dict[str, "TaskInfo"] = {}
executor = None


@dataclass
class TaskInfo:
    task_id: str
    task_type: str
    status: str
    progress: float = 0.0
    message: str = ""
    output_path: Optional[str] = None
    rtf: float = 0.0
    audio_duration: float = 0.0
    inference_time: float = 0.0
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    error: Optional[str] = None
    is_uploaded_ref: bool = False


# ---------------------------------------------------------------------------
# 请求分类（纯函数，便于测试与复用）
# ---------------------------------------------------------------------------
def classify_request(
    text: str,
    ref_audio_path: Optional[str],
    ref_text: Optional[str],
    instruction: Optional[str],
) -> str:
    """根据参数判定 Breeze-TTS-2 的模板模式。

    返回 "clone" / "direction" / "design" / "plain"。
    - clone    : 有参考音频 + 参考文本（无 instruction）
    - direction: 有参考音频 + 参考文本 + instruction（在克隆基础上引导语气/情绪）
    - design   : 仅 instruction（无参考音频）
    - plain    : 仅 text（使用默认说话人 S0，无需参考）
    """
    has_ref = bool(ref_audio_path) and bool(ref_text and ref_text.strip())
    has_instruction = bool(instruction and instruction.strip())
    if has_ref:
        return "direction" if has_instruction else "clone"
    if has_instruction:
        return "design"
    return "plain"


# ---------------------------------------------------------------------------
# Pydantic 响应模型
# ---------------------------------------------------------------------------
class TaskResponse(BaseModel):
    task_id: str
    status: str
    message: str


class TaskStatusResponse(BaseModel):
    task_id: str
    task_type: str
    status: str
    progress: float
    message: str
    output_path: Optional[str] = None
    rtf: Optional[float] = None
    audio_duration: Optional[float] = None
    inference_time: Optional[float] = None
    created_at: float
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    error: Optional[str] = None


class ServerInfo(BaseModel):
    model_loaded: bool
    device: str
    sample_rate: int
    max_workers: int


def get_best_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _breeze_weights_complete(model_dir: str) -> bool:
    """校验 Breeze-TTS-2 权重是否真正完整（不止看 4 个必需文件）。

    resolve_path 只要目录存在就直接返回，可能权重被中断下载而不全。
    这里读取 model.safetensors.index.json 声明的全部分片 + audio_tokenizer 权重逐一核对。
    """
    base = Path(model_dir)
    for f in (
        "config.json",
        "model.safetensors.index.json",
        "tokenizer.json",
        "audio_tokenizer/config.json",
    ):
        if not (base / f).is_file():
            return False
    index = base / "model.safetensors.index.json"
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
    except Exception:
        return False
    weight_map = data.get("weight_map", {})
    shards = set(weight_map.values())
    for shard in shards:
        if not (base / shard).is_file():
            return False
    if not (base / "audio_tokenizer" / "model.safetensors").is_file():
        return False
    return True


def load_breeze_model(model_path: str, device_str: Optional[str], fast_all: bool = False):
    """加载 Breeze-TTS-2 运行时。breeze 相关依赖在此处惰性导入，避免无 GPU / 缺依赖时
    连模块都无法 import。"""
    global runtime, model, tokenizer, audio_tokenizer, device, sample_rate

    from breeze_infer.runtime import (
        load_runtime,
        resolve_device,
        update_generation_config_for_breeze,
    )
    from breeze_infer.templates import get_template, prepare_inputs, select_template_name
    from models.fast_streaming import FastBreezeStreamingRuntime, FastStreamingConfig

    sys.path.insert(0, _project_root)
    from tools.models_manager import ModelsManager

    mgr = ModelsManager()
    resolved_path = mgr.resolve_path(model_path, model_type="breeze_tts")

    # resolve_path 在目录已存在时直接返回，可能权重被中断下载而不全。
    # 这里显式校验 checkpoint 分片完整性，缺失则（重新）触发自动下载。
    if not _breeze_weights_complete(resolved_path):
        logger.warning(
            f"Breeze-TTS-2 权重不完整（目录存在但分片缺失），触发自动下载：{resolved_path}"
        )
        resolved_path = mgr.download_model("breeze_tts")
        if not _breeze_weights_complete(resolved_path):
            raise RuntimeError(
                f"Breeze-TTS-2 自动下载后校验仍不完整：{resolved_path}。"
                "请检查网络后重试，或手动删除 models/Breeze-TTS-2 目录后重启。"
            )

    # Breeze-TTS-2 验收：权重目录必须包含 audio_tokenizer 子目录与根 config.json
    if not (Path(resolved_path) / "audio_tokenizer").is_dir():
        raise RuntimeError(
            f"Breeze-TTS-2 权重目录缺少 audio_tokenizer 子目录：{resolved_path}。"
            "请确认模型已正确下载（models/Breeze-TTS-2）。"
        )

    device = device_str or resolve_device()
    # torch.cuda.set_device 不接受裸 "cuda"（需带索引，如 "cuda:0"）
    if device and device.startswith("cuda") and ":" not in device:
        device = device + ":0"
    if not device.startswith("cuda"):
        raise RuntimeError(
            "Breeze-TTS-2 的推理运行时要求 CUDA GPU（cuda 设备）。"
            f"当前解析到设备：{device}。请使用 --device cuda 并确保机器具备 NVIDIA GPU。"
        )

    logger.info(f"正在加载 Breeze-TTS-2 模型: {resolved_path}")
    logger.info(f"设备: {device}, attn_implementation=eager")

    _tokenizer, _model, _audio_tokenizer = load_runtime(
        resolved_path,
        device=device,
        attn_implementation="eager",
    )
    update_generation_config_for_breeze(_model)

    config = FastStreamingConfig(
        max_new_tokens=MAX_NEW_TOKENS,
        max_seq_len=MAX_SEQ_LEN,
        fast_all=fast_all,
        repetition_penalty=REPETITION_PENALTY,
    )
    _runtime = FastBreezeStreamingRuntime(
        _model, _audio_tokenizer, config, tokenizer=_tokenizer
    )

    # 绑定到全局（供推理线程与 prepare_inputs 使用）
    tokenizer = _tokenizer
    model = _model
    audio_tokenizer = _audio_tokenizer
    runtime = _runtime
    sample_rate = _runtime.sample_rate

    logger.info(f"模型加载完成！采样率: {sample_rate}Hz")
    return runtime


def calculate_rtf(audio_duration: float, inference_time: float) -> float:
    if audio_duration <= 0:
        return 0.0
    return inference_time / audio_duration


def _save_upload(ref_audio: UploadFile) -> str:
    upload_dir = Path("uploads")
    upload_dir.mkdir(exist_ok=True)
    task_id_tmp = str(uuid.uuid4())
    suffix = Path(ref_audio.filename or "reference.wav").suffix or ".wav"
    saved_path = upload_dir / f"{task_id_tmp}{suffix}"
    with open(saved_path, "wb") as f:
        f.write(ref_audio.file.read())
    return str(saved_path)


def execute_synthesis(
    task_id: str,
    text: str,
    ref_audio_path: Optional[str],
    ref_text: Optional[str],
    instruction: Optional[str],
    cfg_scale: float,
    seed: int,
    output_path: Optional[str],
):
    """在线程池中执行一次完整合成，把结果写成 wav 并更新任务状态。"""
    task = tasks[task_id]
    task.status = "running"
    task.started_at = time.time()
    inference_start = time.time()

    try:
        if output_path:
            out_path = output_path
        else:
            out_dir = Path("outputs")
            out_dir.mkdir(exist_ok=True)
            out_path = str(out_dir / f"{task_id}.wav")
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)

        task.progress = 10.0
        task.message = "构建输入..."

        request_dict = {"id": task_id, "text": text, "speaker": "S0"}
        if ref_audio_path:
            request_dict["ref_audio_path"] = ref_audio_path
            request_dict["ref_text"] = (ref_text or "").strip()
        if instruction:
            request_dict["instruction"] = instruction.strip()

        template_name = select_template_name(request_dict)
        inputs = prepare_inputs(
            tokenizer,
            audio_tokenizer,
            model,
            [request_dict],
            get_template(template_name),
            guidance_scale=cfg_scale,
            guidance_scale_ref=None,
            guidance_scale_ins=None,
        )

        task.progress = 35.0
        task.message = f"生成中 (cfg_scale={cfg_scale})..."

        chunks = []
        for chunk in runtime.iter_audio_chunks(inputs, request_id=task_id, seed=seed):
            audio_np = chunk.audio
            if audio_np is not None and audio_np.size:
                chunks.append(audio_np)
            task.progress = min(95.0, max(task.progress, 35.0 + len(chunks) * 1.0))

        if not chunks:
            raise RuntimeError("模型未产生任何音频帧，合成失败。")

        audio = np.concatenate(chunks).astype(np.float32)
        sf.write(out_path, audio, sample_rate)

        inference_time = time.time() - inference_start
        audio_duration = float(audio.size) / float(sample_rate)
        rtf = calculate_rtf(audio_duration, inference_time)

        task.status = "completed"
        task.progress = 100.0
        task.message = "生成完成"
        task.output_path = out_path
        task.audio_duration = audio_duration
        task.inference_time = inference_time
        task.rtf = rtf
        task.completed_at = time.time()
        logger.info(
            f"任务 {task_id} 完成: RTF={rtf:.4f}, 时长={audio_duration:.2f}s, "
            f"推理时间={inference_time:.2f}s"
        )
    except Exception as e:
        logger.error(f"任务 {task_id} 失败: {e}")
        task.status = "failed"
        task.error = str(e)
        task.message = f"生成失败: {e}"
        task.completed_at = time.time()
        raise
    finally:
        if task.is_uploaded_ref and ref_audio_path and os.path.exists(ref_audio_path):
            try:
                os.remove(ref_audio_path)
            except Exception as e:
                logger.warning(f"清理上传参考音频失败: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global executor
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8860)
    parser.add_argument("--device", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-workers", type=int, default=2)
    parser.add_argument("--fast-all", action="store_true", default=False)
    args, _ = parser.parse_known_args()

    max_workers = args.max_workers
    executor = ThreadPoolExecutor(max_workers=max_workers)
    logger.info(f"线程池初始化完成，最大工作线程数: {max_workers}")

    # 测试 / 无 GPU 环境可跳过加载：设置 BREEZE_TTS_SKIP_LOAD=1
    if os.environ.get("BREEZE_TTS_SKIP_LOAD") != "1":
        try:
            model_path = args.model or os.path.join(_project_root, "models", "Breeze-TTS-2")
            load_breeze_model(model_path, args.device, fast_all=args.fast_all)
        except Exception as e:
            logger.error(f"Breeze-TTS-2 模型加载失败（服务仍会启动，调用接口将返回 503）：{e}")
    else:
        logger.warning("BREEZE_TTS_SKIP_LOAD=1：跳过模型加载（测试模式）。")

    logger.info("=" * 60)
    logger.info(f"API 文档地址: http://{args.host}:{args.port}/docs")
    logger.info("=" * 60)

    yield

    logger.info("正在关闭服务器...")
    executor.shutdown(wait=True)
    logger.info("服务器已关闭")


app = FastAPI(
    title="Breeze-TTS-2 API",
    description=(
        "Breeze-TTS-2 TTS API — 支持声音克隆 / 声音设计 / 声音引导\n\n"
        "**语音生成方式：**\n"
        "- 🎙️ **声音克隆 (Voice Clone)**: 参考音频 + 参考文本（精确转写）\n"
        "- 🎛️ **声音引导 (Voice Direction)**: 参考音频 + 参考文本 + 自然语言指令（引导语气/情绪/节奏）\n"
        "- 🎨 **声音设计 (Voice Design)**: 纯自然语言指令生成音色（无需参考音频）\n\n"
        "采样率固定 24000 Hz，需要 NVIDIA CUDA GPU。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", response_model=ServerInfo)
async def root():
    return ServerInfo(
        model_loaded=runtime is not None,
        device=str(device) if device else "unknown",
        sample_rate=sample_rate,
        max_workers=executor._max_workers if executor else 0,
    )


@app.get("/health")
async def health_check():
    gpu_available = torch.cuda.is_available()
    gpu_memory_used = None
    gpu_memory_total = None
    if gpu_available:
        try:
            gpu_memory_used = round(torch.cuda.memory_allocated() / 1024**3, 2)
            gpu_memory_total = round(torch.cuda.get_device_properties(0).total_mem / 1024**3, 2)
        except Exception:
            pass

    running_tasks = sum(1 for t in tasks.values() if t.status == "running")
    pending_tasks = sum(1 for t in tasks.values() if t.status == "pending")

    status = "healthy" if runtime is not None else "degraded"
    return JSONResponse(
        status_code=200,
        content={
            "status": status,
            "model_loaded": runtime is not None,
            "device": str(device) if device else "unknown",
            "gpu_available": gpu_available,
            "gpu_memory_used_gb": gpu_memory_used,
            "gpu_memory_total_gb": gpu_memory_total,
            "running_tasks": running_tasks,
            "pending_tasks": pending_tasks,
            "max_workers": executor._max_workers if executor else 0,
            "sample_rate": sample_rate,
        },
    )


def _resolve_ref(
    ref_audio: Optional[UploadFile],
    ref_audio_path: Optional[str],
) -> tuple[bool, Optional[str]]:
    """解析参考音频来源，返回 (is_uploaded, final_path)。"""
    if ref_audio_path and ref_audio_path.strip():
        final = ref_audio_path.strip()
        if not os.path.exists(final):
            raise HTTPException(status_code=400, detail=f"参考音频路径不存在: {final}")
        return False, final
    if ref_audio is not None and ref_audio.filename:
        return True, _save_upload(ref_audio)
    return False, None


def _submit_task(
    task_type: str,
    text: str,
    ref_audio: Optional[UploadFile],
    ref_audio_path: Optional[str],
    ref_text: Optional[str],
    instruction: Optional[str],
    cfg_scale: float,
    seed: int,
    output_path: Optional[str],
) -> TaskResponse:
    is_uploaded, final_ref = _resolve_ref(ref_audio, ref_audio_path)
    has_ref = final_ref is not None

    # 先做参数校验（与模型是否加载无关），让调用方能拿到明确的 400
    if task_type == "clone":
        # 克隆 / 引导：必须提供参考音频 + 参考文本
        if not has_ref:
            raise HTTPException(
                status_code=400,
                detail="声音克隆需提供 ref_audio（文件上传）或 ref_audio_path（本地路径）",
            )
        if not (ref_text and ref_text.strip()):
            raise HTTPException(
                status_code=400,
                detail="Breeze-TTS-2 克隆必须提供 ref_text（参考音频的精确文字稿）",
            )
    elif task_type == "design":
        if has_ref:
            raise HTTPException(
                status_code=400,
                detail="声音设计（Voice Design）无需参考音频，请改用 /api/v1/voice/clone 做声音引导",
            )
        if not (instruction and instruction.strip()):
            raise HTTPException(status_code=400, detail="声音设计需提供 instruction 指令")

    if runtime is None:
        raise HTTPException(status_code=503, detail="模型未加载，请稍候或检查日志")

    task_id = str(uuid.uuid4())
    task = TaskInfo(
        task_id=task_id,
        task_type=task_type,
        status="pending",
        message="任务已创建，等待处理",
        is_uploaded_ref=is_uploaded,
    )
    tasks[task_id] = task

    def run():
        execute_synthesis(
            task_id,
            text,
            final_ref,
            ref_text,
            instruction,
            cfg_scale,
            seed,
            output_path,
        )

    executor.submit(run)
    return TaskResponse(task_id=task_id, status="pending", message="任务已创建")


@app.post("/api/v1/voice/clone", response_model=TaskResponse)
async def voice_clone(
    background_tasks: BackgroundTasks,
    text: str = Form(..., description="要合成的文本（支持 (laugh)/[笑] 等表情事件）"),
    ref_audio: Optional[UploadFile] = File(None, description="参考音频文件（与 ref_audio_path 二选一）"),
    ref_audio_path: Optional[str] = Form(None, description="参考音频本地路径（推荐，免上传）"),
    ref_text: Optional[str] = Form(None, description="参考音频的精确文字稿（克隆/引导必需）"),
    instruction: Optional[str] = Form(None, description="自然语言指令；提供后变为声音引导(Voice Direction)"),
    cfg_scale: float = Form(DEFAULT_CFG_SCALE, description="CFG 引导强度；引导/设计建议设 4"),
    seed: int = Form(42, description="随机种子"),
    output_path: Optional[str] = Form(None, description="指定输出 wav 路径（可选）"),
):
    """🎙️ 声音克隆 / 🎛️ 声音引导。

    - 仅提供 ref_audio + ref_text + text → Voice Clone（保留音色/节奏/情绪）。
    - 额外提供 instruction → Voice Direction（在克隆基础上引导语气/情绪/节奏）。
    """
    return _submit_task(
        "clone", text, ref_audio, ref_audio_path, ref_text, instruction, cfg_scale, seed, output_path
    )


@app.post("/api/v1/voice/design", response_model=TaskResponse)
async def voice_design(
    background_tasks: BackgroundTasks,
    text: str = Form(..., description="要合成的文本"),
    instruction: Optional[str] = Form(None, description="自然语言音色描述，如 '温柔自信的年轻女性，语气亲切'"),
    ref_audio: Optional[UploadFile] = File(None, description="声音设计无需参考音频（提供将被拒绝）"),
    ref_audio_path: Optional[str] = Form(None, description="声音设计无需参考音频（提供将被拒绝）"),
    cfg_scale: float = Form(DESIGN_CFG_SCALE, description="CFG 引导强度；设计建议设 4"),
    seed: int = Form(42, description="随机种子"),
    output_path: Optional[str] = Form(None, description="指定输出 wav 路径（可选）"),
):
    """🎨 声音设计（Voice Design）：仅用自然语言指令生成音色，无需参考音频。"""
    return _submit_task(
        "design", text, ref_audio, ref_audio_path, None, instruction, cfg_scale, seed, output_path
    )


@app.get("/api/v1/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(task_id: str):
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    task = tasks[task_id]
    return TaskStatusResponse(
        task_id=task.task_id,
        task_type=task.task_type,
        status=task.status,
        progress=task.progress,
        message=task.message,
        output_path=task.output_path,
        rtf=task.rtf if task.status == "completed" else None,
        audio_duration=task.audio_duration if task.status == "completed" else None,
        inference_time=task.inference_time if task.status == "completed" else None,
        created_at=task.created_at,
        started_at=task.started_at,
        completed_at=task.completed_at,
        error=task.error,
    )


@app.get("/api/v1/tasks")
async def list_tasks(
    status: Optional[str] = Query(None, description="按状态筛选"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    task_list = list(tasks.values())
    if status:
        task_list = [t for t in task_list if t.status == status]
    task_list.sort(key=lambda x: x.created_at, reverse=True)
    total = len(task_list)
    task_list = task_list[offset:offset + limit]
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "tasks": [
            {
                "task_id": t.task_id,
                "task_type": t.task_type,
                "status": t.status,
                "progress": t.progress,
                "message": t.message,
                "created_at": t.created_at,
            }
            for t in task_list
        ],
    }


@app.delete("/api/v1/tasks/{task_id}")
async def delete_task(task_id: str):
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    task = tasks[task_id]
    if task.status == "running":
        raise HTTPException(status_code=400, detail="任务正在运行中，无法删除")
    if task.output_path and os.path.exists(task.output_path):
        try:
            os.remove(task.output_path)
        except Exception as e:
            logger.warning(f"删除文件失败: {e}")
    del tasks[task_id]
    return {"message": "任务已删除", "task_id": task_id}


@app.get("/api/v1/voice/download/{task_id}")
async def download_audio(task_id: str):
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    task = tasks[task_id]
    if task.status != "completed":
        raise HTTPException(status_code=400, detail="任务尚未完成")
    if not task.output_path or not os.path.exists(task.output_path):
        raise HTTPException(status_code=404, detail="音频文件不存在")
    return FileResponse(task.output_path, media_type="audio/wav", filename=f"{task_id}.wav")


@app.get("/api/v1/tasks/{task_id}/progress")
async def task_progress_sse(task_id: str):
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    task = tasks[task_id]

    async def event_generator():
        while True:
            data = {
                "task_id": task.task_id,
                "status": task.status,
                "progress": task.progress,
                "message": task.message,
                "rtf": task.rtf if task.status == "completed" else None,
            }
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            if task.status in ("completed", "failed"):
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8860)
    ap.add_argument("--device", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--max-workers", type=int, default=2)
    ap.add_argument("--fast-all", action="store_true", default=False)
    cli_args = ap.parse_args()
    uvicorn.run(app, host=cli_args.host, port=cli_args.port)
