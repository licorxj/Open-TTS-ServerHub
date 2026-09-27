#!/usr/bin/env python3
"""
dots.tts FastAPI Server

提供完整的 TTS API 接口，支持：
- 声音克隆 (Voice Cloning) — 参考音频 + 可选文本
- X-vector 声音克隆 (X-vector Cloning) — 仅参考音频
- 多语言支持 (Multilingual)
- 流式生成 (Streaming)
- 异步多线程处理
- 实时进度推送 (WebSocket/SSE)
- RTF 速率显示

Usage:
    python dots_tts_api.py [--host 0.0.0.0] [--port 8856] [--device cuda] [--model path]
"""

from __future__ import annotations

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
from typing import Any, Dict, List, Optional

import numpy as np
import soundfile as sf
import torch
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

_project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _project_root)
sys.path.insert(0, os.path.join(_project_root, "dots", "src"))
sys.path.insert(0, os.path.join(_project_root, "dots"))

# Mock WeTextProcessing (tn) for Windows where pynini cannot be built
import types as _types
if "tn" not in sys.modules:
    _tn = _types.ModuleType("tn")
    _tn_chinese = _types.ModuleType("tn.chinese")
    _tn_english = _types.ModuleType("tn.english")
    _tn_chinese_normalizer = _types.ModuleType("tn.chinese.normalizer")
    _tn_english_normalizer = _types.ModuleType("tn.english.normalizer")
    class _MockNormalizer:
        def normalize(self, text): return text
    _tn_chinese_normalizer.Normalizer = _MockNormalizer
    _tn_english_normalizer.Normalizer = _MockNormalizer
    _tn.chinese = _tn_chinese
    _tn.english = _tn_english
    _tn_chinese.normalizer = _tn_chinese_normalizer
    _tn_english.normalizer = _tn_english_normalizer
    sys.modules["tn"] = _tn
    sys.modules["tn.chinese"] = _tn_chinese
    sys.modules["tn.english"] = _tn_english
    sys.modules["tn.chinese.normalizer"] = _tn_chinese_normalizer
    sys.modules["tn.english.normalizer"] = _tn_english_normalizer

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
        if not getattr(real_tqdm, "_dots_quiet", False):
            class quiet_tqdm(real_tqdm):
                def __init__(self, *args, **kwargs):
                    kwargs["disable"] = True
                    super().__init__(*args, **kwargs)
            quiet_tqdm._dots_quiet = True
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
    try:
        console.clear()
        console.print(_render_task_dashboard())
    except Exception:
        pass


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
runtime = None
device = None
sampling_rate = 48000

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
    prompt_text: Optional[str] = Field(None, description="参考音频的文本内容（可选，不提供则仅用 x-vector 克隆）")
    instruct: Optional[str] = Field(None, description="声音风格控制指令（如 '温柔的语气说'），使用 instruction_tts 模板")
    language: Optional[str] = Field(None, description="语言代码或名称（如 'en', 'zh', 'auto_detect'）")
    output_path: Optional[str] = Field(None, description="输出文件路径")
    num_steps: int = Field(10, ge=1, le=100, description="Flow-matching 采样步数")
    guidance_scale: float = Field(1.2, ge=0.0, le=5.0, description="CFG 引导强度")
    speaker_scale: float = Field(1.5, ge=0.0, le=5.0, description="说话人嵌入缩放系数")
    normalize_text: bool = Field(False, description="是否启用文本规范化")
    max_generate_length: int = Field(500, ge=50, le=2000, description="最大生成音频 patch 数")
    seed: int = Field(42, ge=0, description="随机种子")


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
    precision: Optional[str] = None


def get_best_device():
    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def load_dots_model(model_path: str, device_str: str):
    global runtime, device, sampling_rate

    from dots_tts.runtime import DotsTtsRuntime

    sys.path.insert(0, _project_root)
    from tools.models_manager import ModelsManager

    mgr = ModelsManager()
    resolved_path = mgr.resolve_path(model_path, model_type="dots")

    device = device_str if device_str else get_best_device()

    hf_cache = os.path.join(_project_root, "hf_download")
    os.makedirs(hf_cache, exist_ok=True)

    precision = "float16" if device.startswith("cuda") else "float32"

    logger.info(f"正在加载 dots.tts 模型: {resolved_path}")
    logger.info(f"设备: {device}, 精度: {precision}")

    runtime = DotsTtsRuntime.from_pretrained(
        resolved_path,
        precision=precision,
        optimize=False,
    )
    sampling_rate = runtime.sample_rate

    logger.info(f"模型加载完成！采样率: {sampling_rate}Hz")
    return runtime


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


def execute_voice_clone(task_id: str, request: VoiceCloneRequest, prompt_audio_path: Optional[str] = None):
    task = tasks[task_id]
    task.status = "running"
    task.started_at = time.time()
    start_task_progress(task_id, f"声音克隆 {task_id[:12]}")

    try:
        update_task_progress(task_id, 5, "正在准备参数...")

        if request.output_path:
            output_path = request.output_path
        else:
            output_dir = Path("outputs")
            output_dir.mkdir(exist_ok=True)
            output_path = str(output_dir / f"{task_id}.wav")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        update_task_progress(task_id, 15, "正在设置随机种子...")
        from dots_tts.utils.util import seed_everything
        seed_everything(request.seed)

        update_task_progress(task_id, 30, f"开始生成 (steps={request.num_steps})...")
        inference_start = time.time()

        gen_kwargs = {
            "text": request.text,
            "num_steps": request.num_steps,
            "guidance_scale": request.guidance_scale,
            "speaker_scale": request.speaker_scale,
            "normalize_text": request.normalize_text,
        }
        if request.instruct:
            gen_kwargs["template_name"] = "instruction_tts"
        if prompt_audio_path:
            gen_kwargs["prompt_audio_path"] = prompt_audio_path
        if request.prompt_text:
            gen_kwargs["prompt_text"] = request.prompt_text
        if request.language:
            gen_kwargs["language"] = request.language

        result = runtime.generate(**gen_kwargs)

        inference_time = time.time() - inference_start

        update_task_progress(task_id, 85, "正在保存音频...")

        audio_tensor = result["audio"].float().cpu().squeeze()
        sf.write(output_path, audio_tensor.numpy(), result["sample_rate"])

        audio_duration = audio_tensor.shape[-1] / result["sample_rate"]
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
        
        time.sleep(0.2)
        
        stop_task_progress(task_id)
        print_task_completed(task)

        if task.is_uploaded_ref and prompt_audio_path:
            try:
                os.remove(prompt_audio_path)
            except Exception as e:
                logger.warning(f"清理上传文件失败: {e}")

    except Exception as e:
        logger.error(f"任务 {task_id} 失败: {e}")
        task.status = "failed"
        task.error = str(e)
        task.message = f"生成失败: {e}"
        task.completed_at = time.time()
        time.sleep(0.2)
        stop_task_progress(task_id)
        print_task_failed(task)
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    global executor

    logger.info("=" * 60)
    logger.info("dots.tts API Server 启动中...")
    logger.info("=" * 60)

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8856)
    parser.add_argument("--device", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-workers", type=int, default=4)
    args, _ = parser.parse_known_args()

    max_workers = args.max_workers
    executor = ThreadPoolExecutor(max_workers=max_workers)
    logger.info(f"线程池初始化完成，最大工作线程数: {max_workers}")

    model_path = args.model or os.path.join(_project_root, "models", "dot")
    load_dots_model(model_path, args.device)

    logger.info("=" * 60)
    logger.info(f"API 文档地址: http://{args.host}:{args.port}/docs")
    logger.info("=" * 60)

    yield

    logger.info("正在关闭服务器...")
    stop_task_dashboard()
    executor.shutdown(wait=True)
    logger.info("服务器已关闭")


app = FastAPI(
    title="dots.tts API",
    description=(
        "dots.tts TTS API — 支持声音克隆\n\n"
        "**语音生成方式：**\n"
        "- 🎙️ **声音克隆**: 参考音频 + 可选文本\n"
        "- 🎤 **X-vector 克隆**: 仅参考音频\n"
        "- 🌐 **多语言**: 支持中英文等多种语言\n"
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
    precision = None
    if runtime is not None:
        precision = runtime.precision
    return ServerInfo(
        model_loaded=runtime is not None,
        device=str(device) if device else "unknown",
        sampling_rate=sampling_rate,
        max_workers=executor._max_workers if executor else 0,
        precision=precision,
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
            "sample_rate": sampling_rate,
        },
    )


@app.post("/api/v1/voice/clone", response_model=TaskResponse)
async def voice_clone(
    background_tasks: BackgroundTasks,
    text: str = Form(..., description="要合成的文本"),
    ref_audio: Optional[UploadFile] = File(None, description="参考音频文件"),
    ref_audio_path: Optional[str] = Form(None, description="参考音频本地路径"),
    prompt_text: Optional[str] = Form(None, description="参考音频的文本内容（不提供则仅用 x-vector 克隆）"),
    instruct: Optional[str] = Form(None, description="声音风格控制指令（如 '温柔的语气说'），使用 instruction_tts 模板"),
    language: Optional[str] = Form(None, description="语言代码（en/zh/auto_detect）"),
    output_path: Optional[str] = Form(None, description="输出文件路径"),
    num_steps: int = Form(10, description="Flow-matching 采样步数"),
    guidance_scale: float = Form(1.2, description="CFG 引导强度"),
    speaker_scale: float = Form(1.5, description="说话人嵌入缩放系数"),
    normalize_text: bool = Form(False, description="是否启用文本规范化"),
    max_generate_length: int = Form(500, description="最大生成音频 patch 数"),
    seed: int = Form(42, description="随机种子"),
):
    """
    🎙️ 声音克隆接口

    克隆参考音频的声音特征来合成目标文本。
    支持三种克隆模式：
    - **续写克隆**: 提供 ref_audio + prompt_text，最佳音色还原
    - **X-vector 克隆**: 仅提供 ref_audio，通过说话人嵌入克隆
    - **指令克隆**: 提供 instruct，使用 instruction_tts 模板控制语气/风格
    """
    if runtime is None:
        raise HTTPException(status_code=503, detail="模型未加载")

    is_uploaded = False
    final_ref_path = None

    if ref_audio_path and ref_audio_path.strip():
        final_ref_path = ref_audio_path.strip()
        if not os.path.exists(final_ref_path):
            raise HTTPException(status_code=400, detail=f"参考音频路径不存在: {final_ref_path}")
    elif ref_audio is not None:
        is_uploaded = True
        upload_dir = Path("uploads")
        upload_dir.mkdir(exist_ok=True)
        task_id_tmp = str(uuid.uuid4())
        saved_path = upload_dir / f"{task_id_tmp}_{ref_audio.filename}"
        with open(saved_path, "wb") as f:
            content = await ref_audio.read()
            f.write(content)
        final_ref_path = str(saved_path)
    else:
        raise HTTPException(status_code=400, detail="请提供 ref_audio（文件上传）或 ref_audio_path（本地路径）")

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
        "参考音频": ref_audio_path or (ref_audio.filename if ref_audio else ""),
        "步数": num_steps,
        "引导尺度": guidance_scale,
    })

    request = VoiceCloneRequest(
        text=text,
        prompt_text=prompt_text,
        instruct=instruct,
        language=language,
        output_path=output_path,
        num_steps=num_steps,
        guidance_scale=guidance_scale,
        speaker_scale=speaker_scale,
        normalize_text=normalize_text,
        max_generate_length=max_generate_length,
        seed=seed,
    )

    def run_clone():
        execute_voice_clone(task_id, request, final_ref_path)

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
            data = {
                "task_id": task.task_id,
                "task_type": task.task_type,
                "status": task.status,
                "progress": task.progress,
                "message": task.message,
                "rtf": task.rtf if task.status == "completed" else None,
                "audio_duration": task.audio_duration if task.status == "completed" else None,
                "inference_time": task.inference_time if task.status == "completed" else None,
                "output_path": task.output_path if task.status == "completed" else None,
            }
            await websocket.send_json(data)
            if task.status in ("completed", "failed"):
                await asyncio.sleep(0.1)
                await websocket.close()
                break
            await asyncio.sleep(0.5)
    except Exception as e:
        logger.error(f"WebSocket 错误: {e}")
        try:
            await websocket.close()
        except Exception:
            pass


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
    uvicorn.run(app, host="0.0.0.0", port=8856)
