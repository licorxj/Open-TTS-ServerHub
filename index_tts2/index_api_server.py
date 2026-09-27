#!/usr/bin/env python3
"""
IndexTTS FastAPI Server

提供完整的 TTS API 接口，支持：
- 声音克隆 (Voice Cloning)
- 情感参考克隆 (Emotion Reference Cloning)
- 情感向量控制 (Emotion Vector Control)
- 情感文本控制 (Emotion Text Control)
- 异步多线程处理
- 实时进度推送 (WebSocket/SSE)
- RTF 速率显示

Usage:
    python index_api_server.py [--host 0.0.0.0] [--port 8855] [--device cuda] [--model path]
"""

from __future__ import annotations

import sys as _sys
import os as _os
_index_lib = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'lib')
if _os.path.isdir(_index_lib) and _index_lib not in _sys.path:
    _sys.path.insert(0, _index_lib)

import argparse
import asyncio
import importlib
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
from typing import Any, Dict, List, Optional, Union

import numpy as np
import torch
import torchaudio
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _project_root)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
logging.getLogger("uvicorn.access").disabled = True

console = Console()


def _disable_tqdm_output():
    os.environ["TQDM_DISABLE"] = "1"
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    os.environ["HF_DATASETS_DISABLE_PROGRESS_BARS"] = "1"
    try:
        tqdm_module = importlib.import_module("tqdm")
        real_tqdm = tqdm_module.tqdm
        if not getattr(real_tqdm, "_index_quiet", False):
            class quiet_tqdm(real_tqdm):
                def __init__(self, *args, **kwargs):
                    kwargs["disable"] = True
                    super().__init__(*args, **kwargs)
            quiet_tqdm._index_quiet = True
            tqdm_module.tqdm = quiet_tqdm
            for module_name in ("tqdm.auto", "tqdm.std"):
                try:
                    module = importlib.import_module(module_name)
                    module.tqdm = quiet_tqdm
                except Exception:
                    pass
    except Exception:
        pass


_disable_tqdm_output()

TASK_TYPE_LABELS = {
    "clone": "🎙️ 声音克隆",
    "batch_clone": "🎙️ 批量声音克隆",
}

task_progress_map: Dict[str, Progress] = {}
task_progress_task_ids: Dict[str, int] = {}
task_panel_params: Dict[str, dict] = {}
task_live: Optional[Live] = None
task_live_lock = threading.RLock()


def _task_status_label(status: str) -> str:
    return {
        "pending": "[yellow]等待中[/]",
        "running": "[cyan]处理中[/]",
        "completed": "[bold green]完成[/]",
        "failed": "[bold red]失败[/]",
    }.get(status, status)


def _task_border_style(status: str) -> str:
    return {
        "pending": "yellow",
        "running": "cyan",
        "completed": "green",
        "failed": "red",
    }.get(status, "blue")


def _ensure_task_progress(task: "TaskInfo", description: str = ""):
    if task.task_id in task_progress_map:
        return
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=24),
        TextColumn("[progress.percentage]{task.percentage:>5.1f}%"),
        TextColumn("[dim]{task.fields[status]}"),
        TimeElapsedColumn(),
        expand=True,
    )
    tid = progress.add_task(
        description or TASK_TYPE_LABELS.get(task.task_type, task.task_type),
        total=100,
        completed=task.progress,
        status=task.message or "等待处理...",
    )
    task_progress_map[task.task_id] = progress
    task_progress_task_ids[task.task_id] = tid


def _render_task_card(task: "TaskInfo"):
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("key", style="bold cyan", min_width=14)
    table.add_column("value")
    table.add_row("任务ID", f"[yellow]{task.task_id[:12]}...[/]")
    table.add_row("类型", TASK_TYPE_LABELS.get(task.task_type, task.task_type))
    table.add_row("状态", _task_status_label(task.status))
    if task.is_uploaded_ref:
        table.add_row("参考音频", "[dim]文件上传[/]")
    params = task_panel_params.get(task.task_id)
    if params:
        for key, value in params.items():
            if value is not None and value != "":
                table.add_row(key, f"[white]{value}[/]")
    if task.output_path:
        table.add_row("输出路径", f"[white]{task.output_path}[/]")
    if task.audio_duration > 0:
        table.add_row("音频时长", f"[white]{task.audio_duration:.2f}s[/]")
    if task.inference_time > 0:
        table.add_row("推理时间", f"[white]{task.inference_time:.2f}s[/]")
    if task.rtf > 0:
        rtf_color = "green" if task.rtf < 1.0 else "yellow" if task.rtf < 2.0 else "red"
        table.add_row("RTF", f"[bold {rtf_color}]{task.rtf:.4f}[/]")
    if task.error:
        table.add_row("错误", f"[red]{task.error}[/]")
    if task.completed_at or task.started_at:
        total_time = (task.completed_at or time.time()) - (task.started_at or task.created_at)
        table.add_row("总耗时", f"[white]{total_time:.2f}s[/]")
    _ensure_task_progress(task)
    progress = task_progress_map[task.task_id]
    title = f"[bold]任务 {task.task_id[:12]}[/]"
    return Panel(
        Group(table, progress),
        title=title,
        border_style=_task_border_style(task.status),
        width=80,
    )


def _render_task_dashboard():
    renderables = [
        _render_task_card(task)
        for task in sorted(tasks.values(), key=lambda item: item.created_at)
    ]
    return Group(*renderables)


def _refresh_task_dashboard():
    console.clear()
    console.print(_render_task_dashboard())


def show_task_card(task: "TaskInfo", params: dict = None, description: str = ""):
    with task_live_lock:
        if params is not None:
            task_panel_params[task.task_id] = params
        _ensure_task_progress(task, description)
        _refresh_task_dashboard()


def stop_task_dashboard():
    return


def print_task_created(task: "TaskInfo", params: dict = None):
    show_task_card(task, params=params)


def start_task_progress(task_id: str, description: str):
    if task_id in tasks:
        with task_live_lock:
            task = tasks[task_id]
            show_task_card(task, description=description)
            progress = task_progress_map[task_id]
            tid = task_progress_task_ids[task_id]
            progress.update(
                tid,
                description=description,
                completed=task.progress,
                status=task.message or "准备中...",
            )
            _refresh_task_dashboard()


def _update_task_progress(task_id: str, progress_val: float, message: str = ""):
    if task_id in tasks:
        with task_live_lock:
            task = tasks[task_id]
            _ensure_task_progress(task)
            progress = task_progress_map[task_id]
            tid = task_progress_task_ids[task_id]
            progress.update(
                tid,
                completed=min(100.0, max(0.0, progress_val)),
                status=message or "处理中...",
            )
            _refresh_task_dashboard()


def stop_task_progress(task_id: str):
    if task_id in tasks:
        with task_live_lock:
            task = tasks[task_id]
            _ensure_task_progress(task)
            progress = task_progress_map[task_id]
            tid = task_progress_task_ids[task_id]
            completed = 100.0 if task.status == "completed" else task.progress
            status = task.message or ("完成" if task.status == "completed" else "失败")
            progress.update(tid, completed=completed, status=status)
            progress.stop_task(tid)
            _refresh_task_dashboard()


def print_task_completed(task: "TaskInfo"):
    show_task_card(task)


def print_task_failed(task: "TaskInfo"):
    show_task_card(task)


# 全局变量
tts_model = None
device = None
sampling_rate = 22050

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


class VoiceCloneRequest(BaseModel):
    text: str = Field(..., description="要合成的文本", min_length=1)
    instruct: Optional[str] = Field(None, description="声音风格控制指令（如 '害怕、紧张的语气'），传入后自动使用 text 情感控制模式")
    emo_control_method: str = Field("speaker", description="情感控制方式: speaker/reference/vector/text")
    emo_alpha: float = Field(1.0, ge=0.0, le=1.0, description="情感权重")
    emo_vector: Optional[List[float]] = Field(None, description="8维情感向量")
    emo_text: Optional[str] = Field(None, description="情感描述文本")
    use_random: bool = Field(False, description="情感随机采样")
    output_path: Optional[str] = Field(None, description="输出文件路径")
    max_text_tokens_per_segment: int = Field(120, ge=20, le=500, description="分句最大Token数")
    do_sample: bool = Field(True, description="是否采样")
    top_p: float = Field(0.8, ge=0.0, le=1.0, description="top_p 采样")
    top_k: int = Field(30, ge=0, le=100, description="top_k 采样")
    temperature: float = Field(0.8, ge=0.1, le=2.0, description="温度")
    num_beams: int = Field(3, ge=1, le=10, description="beam search 数量")
    repetition_penalty: float = Field(10.0, ge=0.1, le=20.0, description="重复惩罚")
    length_penalty: float = Field(0.0, ge=-2.0, le=2.0, description="长度惩罚")
    max_mel_tokens: int = Field(1500, ge=50, le=3000, description="最大生成Token数")


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
    sampling_rate: int
    max_workers: int
    model_version: Optional[str] = None


def get_best_device():
    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def load_index_model(model_path: str, device_str: str):
    global tts_model, device, sampling_rate

    from indextts.infer_v2 import IndexTTS2

    sys.path.insert(0, _project_root)
    from tools.models_manager import ModelsManager

    mgr = ModelsManager()
    resolved_path = mgr.resolve_path(model_path, model_type="indextts")

    device = device_str if device_str else get_best_device()

    cfg_path = os.path.join(resolved_path, "config.yaml")
    if not os.path.isfile(cfg_path):
        from indextts.utils.model_download import ensure_config_available
        ensure_config_available(resolved_path)

    logger.info(f"正在加载 IndexTTS2 模型: {resolved_path}")
    logger.info(f"设备: {device}")

    torch.cuda.empty_cache()
    use_fp16 = device.startswith("cuda")
    tts_model = IndexTTS2(
        cfg_path=cfg_path,
        model_dir=resolved_path,
        use_fp16=use_fp16,
        device=None,
        use_cuda_kernel=False,
        use_deepspeed=False,
    )
    sampling_rate = 22050

    logger.info(f"模型加载完成！采样率: {sampling_rate}Hz")
    return tts_model


def update_task_progress(task_id: str, progress: float, message: str = ""):
    if task_id in tasks:
        tasks[task_id].progress = min(100.0, max(0.0, progress))
        if message:
            tasks[task_id].message = message
        logger.debug(f"任务 {task_id}: {progress:.1f}% - {message}")
        _update_task_progress(task_id, progress, message)


def calculate_rtf(audio_duration: float, inference_time: float) -> float:
    if audio_duration <= 0:
        return 0.0
    return inference_time / audio_duration


def execute_voice_clone(task_id: str, request: VoiceCloneRequest, spk_audio_path: str, emo_audio_path: Optional[str] = None):
    task = tasks[task_id]
    task.status = "running"
    task.started_at = time.time()
    start_task_progress(task_id, f"声音克隆 {task_id[:12]}")

    try:
        update_task_progress(task_id, 5, "正在加载参考音频...")
        if not os.path.exists(spk_audio_path):
            raise FileNotFoundError(f"参考音频不存在: {spk_audio_path}")

        update_task_progress(task_id, 15, "正在构建推理参数...")

        if request.output_path:
            output_path = request.output_path
        else:
            output_dir = Path("outputs")
            output_dir.mkdir(exist_ok=True)
            output_path = str(output_dir / f"{task_id}.wav")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        kwargs = {
            "spk_audio_prompt": spk_audio_path,
            "text": request.text,
            "output_path": output_path,
            "emo_alpha": request.emo_alpha,
            "use_random": request.use_random,
            "max_text_tokens_per_segment": request.max_text_tokens_per_segment,
            "do_sample": request.do_sample,
            "top_p": request.top_p,
            "top_k": request.top_k,
            "temperature": request.temperature,
            "num_beams": request.num_beams,
            "repetition_penalty": request.repetition_penalty,
            "length_penalty": request.length_penalty,
            "max_mel_tokens": request.max_mel_tokens,
        }

        if request.instruct:
            kwargs["use_emo_text"] = True
            kwargs["emo_text"] = request.instruct
        elif request.emo_control_method == "reference" and emo_audio_path:
            kwargs["emo_audio_prompt"] = emo_audio_path
        elif request.emo_control_method == "vector" and request.emo_vector:
            if len(request.emo_vector) != 8:
                raise ValueError("emo_vector 必须是 8 个浮点数: [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]")
            kwargs["emo_vector"] = request.emo_vector
        elif request.emo_control_method == "text":
            kwargs["use_emo_text"] = True
            if request.emo_text:
                kwargs["emo_text"] = request.emo_text

        update_task_progress(task_id, 30, f"开始生成...")
        inference_start = time.time()

        tts_model.infer(**kwargs)

        inference_time = time.time() - inference_start

        update_task_progress(task_id, 85, "正在计算音频信息...")

        wav, sr = torchaudio.load(output_path)
        audio_duration = wav.shape[-1] / sr
        rtf = calculate_rtf(audio_duration, inference_time)

        task.status = "completed"
        task.progress = 100.0
        task.message = "生成完成"
        task.output_path = output_path
        task.audio_duration = audio_duration
        task.inference_time = inference_time
        task.rtf = rtf
        task.completed_at = time.time()

        logger.info(f"任务 {task_id} 完成: RTF={rtf:.4f}, 时长={audio_duration:.2f}s, 推理时间={inference_time:.2f}s")
        stop_task_progress(task_id)
        print_task_completed(task)

        if task.is_uploaded_ref:
            try:
                for p in [spk_audio_path, emo_audio_path]:
                    if p and os.path.exists(p):
                        os.remove(p)
            except Exception as e:
                logger.warning(f"清理上传文件失败: {e}")

    except Exception as e:
        logger.error(f"任务 {task_id} 失败: {e}")
        task.status = "failed"
        task.error = str(e)
        task.message = f"生成失败: {e}"
        task.completed_at = time.time()
        stop_task_progress(task_id)
        print_task_failed(task)
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    global executor

    logger.info("=" * 60)
    logger.info("IndexTTS API Server 启动中...")
    logger.info("=" * 60)

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8855)
    parser.add_argument("--device", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-workers", type=int, default=4)
    args, _ = parser.parse_known_args()

    max_workers = args.max_workers
    executor = ThreadPoolExecutor(max_workers=max_workers)
    logger.info(f"线程池初始化完成，最大工作线程数: {max_workers}")

    model_path = args.model or os.path.join(_project_root, "models", "IndexTTS-2")
    load_index_model(model_path, args.device)

    logger.info("=" * 60)
    logger.info(f"API 文档地址: http://{args.host}:{args.port}/docs")
    logger.info("=" * 60)

    yield

    logger.info("正在关闭服务器...")
    stop_task_dashboard()
    executor.shutdown(wait=True)
    logger.info("服务器已关闭")


app = FastAPI(
    title="IndexTTS API",
    description=(
        "IndexTTS2 TTS API — 支持声音克隆和情感控制\n\n"
        "**情感控制方式：**\n"
        "- 🎙️ **默认 (speaker)**: 使用音色参考音频的情感\n"
        "- 🎭 **情感参考 (reference)**: 使用独立的情感参考音频\n"
        "- 📊 **情感向量 (vector)**: 8维情感向量控制\n"
        "- 📝 **情感文本 (text)**: 通过自然语言描述情感\n"
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
    model_version = None
    if tts_model is not None and hasattr(tts_model, 'model_version'):
        model_version = tts_model.model_version
    return ServerInfo(
        model_loaded=tts_model is not None,
        device=str(device) if device else "unknown",
        sampling_rate=sampling_rate,
        max_workers=executor._max_workers if executor else 0,
        model_version=model_version,
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

    status = "healthy" if tts_model is not None else "degraded"

    return JSONResponse(
        status_code=200,
        content={
            "status": status,
            "model_loaded": tts_model is not None,
            "device": str(device) if device else "unknown",
            "gpu_available": gpu_available,
            "gpu_memory_used_gb": gpu_memory_used,
            "gpu_memory_total_gb": gpu_memory_total,
            "running_tasks": running_tasks,
            "pending_tasks": pending_tasks,
            "max_workers": executor._max_workers if executor else 0,
        },
    )


@app.post("/api/v1/voice/clone", response_model=TaskResponse)
async def voice_clone(
    background_tasks: BackgroundTasks,
    text: str = Form(..., description="要合成的文本"),
    spk_audio: Optional[UploadFile] = File(None, description="音色参考音频文件"),
    spk_audio_path: Optional[str] = Form(None, description="音色参考音频本地路径"),
    instruct: Optional[str] = Form(None, description="声音风格控制指令（如 '害怕、紧张的语气'），传入后自动使用 text 情感控制模式"),
    emo_control_method: str = Form("speaker", description="情感控制方式: speaker/reference/vector/text"),
    emo_audio: Optional[UploadFile] = File(None, description="情感参考音频文件（emo_control_method=reference时）"),
    emo_audio_path: Optional[str] = Form(None, description="情感参考音频本地路径"),
    emo_vector: Optional[str] = Form(None, description="8维情感向量，逗号分隔，如 '0.8,0,0,0,0,0,0,0'"),
    emo_text: Optional[str] = Form(None, description="情感描述文本"),
    emo_alpha: float = Form(1.0, description="情感权重 (0.0-1.0)"),
    use_random: bool = Form(False, description="情感随机采样"),
    output_path: Optional[str] = Form(None, description="输出文件路径"),
    max_text_tokens_per_segment: int = Form(120, description="分句最大Token数"),
    do_sample: bool = Form(True, description="是否采样"),
    top_p: float = Form(0.8, description="top_p 采样"),
    top_k: int = Form(30, description="top_k 采样"),
    temperature: float = Form(0.8, description="温度"),
    num_beams: int = Form(3, description="beam search 数量"),
    repetition_penalty: float = Form(10.0, description="重复惩罚"),
    length_penalty: float = Form(0.0, description="长度惩罚"),
    max_mel_tokens: int = Form(1500, description="最大生成Token数"),
):
    """
    🎙️ 声音克隆接口

    克隆参考音频的声音特征来合成目标文本。支持多种情感控制方式。

    **情感控制方式：**
    - **speaker**: 默认，使用音色参考音频的情感
    - **reference**: 使用独立的情感参考音频
    - **vector**: 8维情感向量 [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]
    - **text**: 通过自然语言描述情感

    **指令克隆**：提供 `instruct` 参数时，自动使用 text 情感控制模式，将指令送入 Qwen 情感模型提取情感向量。
    """
    if tts_model is None:
        raise HTTPException(status_code=503, detail="模型未加载")

    is_uploaded = False
    final_spk_path = None

    if spk_audio_path and spk_audio_path.strip():
        final_spk_path = spk_audio_path.strip()
        if not os.path.exists(final_spk_path):
            raise HTTPException(status_code=400, detail=f"音色参考音频路径不存在: {final_spk_path}")
    elif spk_audio is not None:
        is_uploaded = True
        upload_dir = Path("uploads")
        upload_dir.mkdir(exist_ok=True)
        task_id_tmp = str(uuid.uuid4())
        saved_path = upload_dir / f"{task_id_tmp}_{spk_audio.filename}"
        with open(saved_path, "wb") as f:
            content = await spk_audio.read()
            f.write(content)
        final_spk_path = str(saved_path)
    else:
        raise HTTPException(status_code=400, detail="请提供 spk_audio（文件上传）或 spk_audio_path（本地路径）")

    final_emo_path = None
    if emo_control_method == "reference":
        if emo_audio_path and emo_audio_path.strip():
            final_emo_path = emo_audio_path.strip()
            if not os.path.exists(final_emo_path):
                raise HTTPException(status_code=400, detail=f"情感参考音频路径不存在: {final_emo_path}")
        elif emo_audio is not None:
            is_uploaded = True
            upload_dir = Path("uploads")
            upload_dir.mkdir(exist_ok=True)
            task_id_tmp = str(uuid.uuid4())
            saved_path = upload_dir / f"{task_id_tmp}_emo_{emo_audio.filename}"
            with open(saved_path, "wb") as f:
                content = await emo_audio.read()
                f.write(content)
            final_emo_path = str(saved_path)
        else:
            raise HTTPException(status_code=400, detail="emo_control_method=reference 时需要提供情感参考音频")

    parsed_emo_vector = None
    if emo_control_method == "vector" and emo_vector:
        try:
            parsed_emo_vector = [float(x.strip()) for x in emo_vector.split(",")]
            if len(parsed_emo_vector) != 8:
                raise ValueError
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="emo_vector 必须是 8 个逗号分隔的浮点数")

    task_id = str(uuid.uuid4())
    task = TaskInfo(
        task_id=task_id,
        task_type="clone",
        status="pending",
        message="任务已创建，等待处理",
        is_uploaded_ref=is_uploaded,
    )
    tasks[task_id] = task
    print_task_created(task, {
        "文本": text[:50] + ("..." if len(text) > 50 else ""),
        "音色参考": spk_audio_path or (spk_audio.filename if spk_audio else ""),
        "指令": instruct or emo_control_method,
        "情感权重": emo_alpha,
    })

    request = VoiceCloneRequest(
        text=text,
        instruct=instruct,
        emo_control_method=emo_control_method,
        emo_alpha=emo_alpha,
        emo_vector=parsed_emo_vector,
        emo_text=emo_text,
        use_random=use_random,
        output_path=output_path,
        max_text_tokens_per_segment=max_text_tokens_per_segment,
        do_sample=do_sample,
        top_p=top_p,
        top_k=top_k,
        temperature=temperature,
        num_beams=num_beams,
        repetition_penalty=repetition_penalty,
        length_penalty=length_penalty,
        max_mel_tokens=max_mel_tokens,
    )

    def run_clone():
        execute_voice_clone(task_id, request, final_spk_path, final_emo_path)

    executor.submit(run_clone)

    return TaskResponse(
        task_id=task_id,
        status="pending",
        message="声音克隆任务已创建",
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
    return FileResponse(
        task.output_path,
        media_type="audio/wav",
        filename=f"{task_id}.wav",
    )


@app.websocket("/ws/tasks/{task_id}")
async def websocket_task_progress(websocket: WebSocket, task_id: str):
    await websocket.accept()
    if task_id not in tasks:
        await websocket.send_json({"error": "任务不存在"})
        await websocket.close()
        return
    task = tasks[task_id]
    try:
        while True:
            await websocket.send_json({
                "task_id": task.task_id,
                "task_type": task.task_type,
                "status": task.status,
                "progress": task.progress,
                "message": task.message,
                "rtf": task.rtf if task.status == "completed" else None,
                "audio_duration": task.audio_duration if task.status == "completed" else None,
                "inference_time": task.inference_time if task.status == "completed" else None,
            })
            if task.status in ("completed", "failed"):
                await websocket.close()
                break
            await asyncio.sleep(0.5)
    except Exception as e:
        logger.error(f"WebSocket 错误: {e}")
        await websocket.close()


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
            yield f"data: {json.dumps(data)}\n\n"
            if task.status in ("completed", "failed"):
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8855)
