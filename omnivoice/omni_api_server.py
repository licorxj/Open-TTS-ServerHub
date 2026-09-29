#!/usr/bin/env python3
"""
OmniVoice FastAPI Server

提供完整的 TTS API 接口，支持：
- 声音克隆 (Voice Cloning)
- 声音设计 (Voice Design)
- 异步多线程处理
- 实时进度推送 (WebSocket/SSE)
- RTF 速率显示

Usage:
    python api_server.py [--host 0.0.0.0] [--port 8853] [--device cuda] [--model path]
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
import types
import uuid

_torchcodec_core_mock = types.ModuleType("torchcodec._core")
_torchcodec_core_mock.ops = types.SimpleNamespace()
_torchcodec_core_mock.__all__ = ["ops", "VideoDecoder"]
_torchcodec_core_mock.VideoDecoder = type("VideoDecoder", (), {})
sys.modules["torchcodec._core"] = _torchcodec_core_mock
sys.modules["torchcodec._core.ops"] = _torchcodec_core_mock.ops


def _disable_transformers_torchcodec():
    """Force Transformers audio loading to use librosa/soundfile on Windows.

    The bundled torchcodec package is present, so Transformers 5.x detects it
    and tries to use it for ASR audio decoding. In this portable Windows env the
    native libtorchcodec DLLs fail to load, so voice-clone auto transcription
    must fall back to the non-torchcodec audio loader.
    """
    try:
        import transformers.utils as transformers_utils
        import transformers.utils.import_utils as import_utils

        try:
            import_utils.is_torchcodec_available.cache_clear()
        except Exception:
            pass

        def _torchcodec_unavailable():
            return False

        import_utils.is_torchcodec_available = _torchcodec_unavailable
        transformers_utils.is_torchcodec_available = _torchcodec_unavailable

        audio_utils = sys.modules.get("transformers.audio_utils")
        if audio_utils is not None:
            audio_utils.is_torchcodec_available = _torchcodec_unavailable
            if hasattr(audio_utils, "TORCHCODEC_VERSION"):
                delattr(audio_utils, "TORCHCODEC_VERSION")
    except Exception:
        pass


_disable_transformers_torchcodec()

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import numpy as np
import torch
import soundfile as sf
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator, ValidationInfo
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
logging.getLogger("uvicorn.access").disabled = True

console = Console()


def _disable_tqdm_output():
    """Keep downstream tqdm progress bars from corrupting the rich task dashboard."""
    os.environ["TQDM_DISABLE"] = "1"
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    os.environ["HF_DATASETS_DISABLE_PROGRESS_BARS"] = "1"

    try:
        tqdm_module = importlib.import_module("tqdm")
        real_tqdm = tqdm_module.tqdm
        if not getattr(real_tqdm, "_omnivoice_quiet", False):
            class quiet_tqdm(real_tqdm):
                def __init__(self, *args, **kwargs):
                    kwargs["disable"] = True
                    super().__init__(*args, **kwargs)

            quiet_tqdm._omnivoice_quiet = True
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
    "clone": "🎛️ 可控克隆",
    "design": "🎨 声音设计",
    "batch_clone": "🎛️ 批量可控克隆",
    "batch_design": "🎨 批量声音设计",
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
model = None
device = None
dtype = None
sampling_rate = 24000

# 任务管理器
tasks: Dict[str, "TaskInfo"] = {}
executor = None


@dataclass
class TaskInfo:
    """任务信息"""
    task_id: str
    task_type: str  # "clone" or "design"
    status: str  # "pending", "running", "completed", "failed"
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


# ============== Pydantic 模型 ==============

OMNI_SPEED_MIN = 0.3
OMNI_SPEED_MAX = 3.0


def _clamp_omni_speed(v: float) -> float:
    """截断语速到合法范围，避免报错"""
    return max(OMNI_SPEED_MIN, min(OMNI_SPEED_MAX, v))


class VoiceCloneRequest(BaseModel):
    """声音克隆请求"""
    text: str = Field(..., description="要合成的文本", min_length=1)
    ref_text: Optional[str] = Field(None, description="参考音频的文本（可选，不提供则自动识别）")
    instruct: Optional[str] = Field(None, description="声音风格控制指令（可选，如 'female, low pitch, whisper'）")
    language: Optional[str] = Field(None, description="语言代码或名称（如 'en', 'zh', 'English'）")
    output_path: Optional[str] = Field(None, description="输出文件路径（可选，默认使用任务ID）")
    num_steps: int = Field(32, ge=1, le=100, description="扩散步数（1-100）")
    guidance_scale: float = Field(2.0, ge=0.0, le=10.0, description="分类器自由引导尺度")
    speed: float = Field(1.0, description="语速因子（超限自动截断）")
    duration: Optional[float] = Field(None, ge=1.0, le=300.0, description="固定输出时长（秒）")
    denoise: bool = Field(True, description="是否启用去噪")
    max_workers: int = Field(1, ge=1, le=8, description="处理线程数")

    @field_validator('speed', mode='before')
    @classmethod
    def clamp_speed(cls, v):
        return _clamp_omni_speed(v)


class VoiceDesignRequest(BaseModel):
    """声音设计请求"""
    text: str = Field(..., description="要合成的文本", min_length=1)
    instruct: str = Field(..., description="声音描述指令（如 'female, low pitch, british accent'）")
    language: Optional[str] = Field(None, description="语言代码或名称")
    output_path: Optional[str] = Field(None, description="输出文件路径（可选）")
    num_steps: int = Field(32, ge=1, le=100, description="扩散步数")
    guidance_scale: float = Field(2.0, ge=0.0, le=10.0, description="分类器自由引导尺度")
    speed: float = Field(1.0, description="语速因子（超限自动截断）")

    @field_validator('speed', mode='before')
    @classmethod
    def clamp_speed(cls, v):
        return _clamp_omni_speed(v)
    duration: Optional[float] = Field(None, ge=1.0, le=300.0, description="固定输出时长（秒）")
    denoise: bool = Field(True, description="是否启用去噪")
    max_workers: int = Field(1, ge=1, le=8, description="处理线程数")


class TaskResponse(BaseModel):
    """任务响应"""
    task_id: str
    status: str
    message: str


class TaskStatusResponse(BaseModel):
    """任务状态响应"""
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
    """服务器信息"""
    model_loaded: bool
    device: str
    dtype: str
    sampling_rate: int
    max_workers: int


# ============== 辅助函数 ==============

def get_best_device():
    """自动检测最佳可用设备"""
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def load_model(model_path: str, device_str: str):
    """加载 OmniVoice 模型"""
    global model, device, dtype, sampling_rate
    
    from omnivoice import OmniVoice, OmniVoiceGenerationConfig
    
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from tools.models_manager import ModelsManager
    
    mgr = ModelsManager()
    resolved_path = mgr.resolve_path(model_path, model_type="omnivoice")
    
    device = device_str if device_str else get_best_device()
    dtype = torch.float16 if device == "cuda" else torch.float32
    
    logger.info(f"正在加载模型: {resolved_path}")
    logger.info(f"设备: {device}, 数据类型: {dtype}")
    
    # 默认 sdpa：当前 flash-attn 构建下 flash_attention_2 会在 varlen 路径报
    # "cu_seqlens_q must have shape (batch_size + 1)"，故默认使用稳定后端。
    # 如需尝试 flash 加速，设置环境变量 OMNIVOICE_ATTN=flash_attention_2
    _attn_impl = os.environ.get("OMNIVOICE_ATTN", "sdpa")
    print(
        f"[Attention] OmniVoice 注意力后端：{_attn_impl}"
        + ("（flash-attn 加速；若报 cu_seqlens 错误请改回 sdpa）" if _attn_impl == "flash_attention_2"
           else "（默认 sdpa，稳定可用）")
    )
    model = OmniVoice.from_pretrained(
        resolved_path,
        device_map=device,
        dtype=dtype,
        load_asr=True,  # 加载 ASR 用于自动转录
        attn_implementation=_attn_impl,
    )
    sampling_rate = model.sampling_rate
    
    logger.info(f"模型加载完成！采样率: {sampling_rate}Hz")
    return model


def update_task_progress(task_id: str, progress: float, message: str = ""):
    """更新任务进度"""
    if task_id in tasks:
        tasks[task_id].progress = min(100.0, max(0.0, progress))
        if message:
            tasks[task_id].message = message
        logger.debug(f"任务 {task_id}: {progress:.1f}% - {message}")
        _update_task_progress(task_id, progress, message)


def calculate_rtf(audio_duration: float, inference_time: float) -> float:
    """计算 RTF (Real-Time Factor)
    
    RTF = 推理时间 / 音频时长
    RTF < 1 表示实时生成，RTF 越小速度越快
    """
    if audio_duration <= 0:
        return 0.0
    return inference_time / audio_duration


def generate_voice_clone(task_id: str, request: VoiceCloneRequest, ref_audio_path: str):
    """执行声音克隆（在线程池中运行）"""
    from omnivoice import OmniVoiceGenerationConfig
    
    task = tasks[task_id]
    task.status = "running"
    task.started_at = time.time()
    start_task_progress(task_id, f"声音克隆 {task_id[:12]}")
    
    try:
        update_task_progress(task_id, 5, "正在加载参考音频...")
        
        # 检查参考音频是否存在
        if not os.path.exists(ref_audio_path):
            raise FileNotFoundError(f"参考音频不存在: {ref_audio_path}")
        
        update_task_progress(task_id, 15, "正在创建语音克隆提示...")
        
        # 创建生成配置
        gen_config = OmniVoiceGenerationConfig(
            num_step=request.num_steps,
            guidance_scale=request.guidance_scale,
            denoise=request.denoise,
            postprocess_output=True,
        )
        
        update_task_progress(task_id, 25, "正在准备生成...")
        
        # 记录推理开始时间
        inference_start = time.time()
        
        update_task_progress(task_id, 35, f"开始生成 ({request.num_steps} 步)...")
        
        # 生成音频
        audio = model.generate(
            text=request.text,
            ref_audio=ref_audio_path,
            ref_text=request.ref_text if request.ref_text else None,
            instruct=request.instruct if request.instruct else None,
            language=request.language,
            speed=request.speed,
            duration=request.duration,
            generation_config=gen_config,
        )
        
        # 记录推理结束时间
        inference_time = time.time() - inference_start
        
        update_task_progress(task_id, 85, "正在后处理音频...")
        
        # 确定输出路径
        if request.output_path:
            output_path = Path(request.output_path)
        else:
            output_dir = Path("outputs")
            output_dir.mkdir(exist_ok=True)
            output_path = output_dir / f"{task_id}.wav"
        
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 保存音频 - model.generate() 返回 list[np.ndarray]
        # 取第一个音频（单个任务只生成一个音频）
        if isinstance(audio, list) and len(audio) > 0:
            waveform = audio[0]
        else:
            waveform = audio
        
        # 转换为 torch 张量（如果是 numpy 数组）
        if isinstance(waveform, np.ndarray):
            waveform = torch.from_numpy(waveform)
        
        # 确保 waveform 是 2D 张量 (channels, samples)
        if waveform.dim() == 1:
            waveform = waveform.unsqueeze(0)  # 添加通道维度: (T,) -> (1, T)
        elif waveform.dim() == 3:
            waveform = waveform.squeeze(0)  # 移除批次维度
        
        sf.write(str(output_path), waveform.squeeze(0).numpy(), sampling_rate)
        
        # 计算音频时长和 RTF
        audio_duration = waveform.shape[-1] / sampling_rate
        rtf = calculate_rtf(audio_duration, inference_time)
        
        # 更新任务状态
        task.status = "completed"
        task.progress = 100.0
        task.message = "生成完成"
        task.output_path = str(output_path)
        task.audio_duration = audio_duration
        task.inference_time = inference_time
        task.rtf = rtf
        task.completed_at = time.time()
        
        logger.info(f"任务 {task_id} 完成: RTF={rtf:.4f}, 时长={audio_duration:.2f}s, 推理时间={inference_time:.2f}s")
        stop_task_progress(task_id)
        print_task_completed(task)
        
        # 仅清理上传的参考音频文件（路径引用的不清理）
        if task.is_uploaded_ref:
            try:
                if os.path.exists(ref_audio_path):
                    os.remove(ref_audio_path)
                    logger.debug(f"已清理上传文件: {ref_audio_path}")
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


def generate_voice_design(task_id: str, request: VoiceDesignRequest):
    """执行声音设计（在线程池中运行）"""
    from omnivoice import OmniVoiceGenerationConfig
    
    task = tasks[task_id]
    task.status = "running"
    task.started_at = time.time()
    start_task_progress(task_id, f"声音设计 {task_id[:12]}")
    
    try:
        update_task_progress(task_id, 10, "正在准备生成...")
        
        # 创建生成配置
        gen_config = OmniVoiceGenerationConfig(
            num_step=request.num_steps,
            guidance_scale=request.guidance_scale,
            denoise=request.denoise,
            postprocess_output=True,
        )
        
        update_task_progress(task_id, 30, f"开始生成 ({request.num_steps} 步)...")
        
        # 记录推理开始时间
        inference_start = time.time()
        
        # 生成音频
        audio = model.generate(
            text=request.text,
            instruct=request.instruct,
            language=request.language,
            speed=request.speed,
            duration=request.duration,
            generation_config=gen_config,
        )
        
        # 记录推理结束时间
        inference_time = time.time() - inference_start
        
        update_task_progress(task_id, 85, "正在后处理音频...")
        
        # 确定输出路径
        if request.output_path:
            output_path = Path(request.output_path)
        else:
            output_dir = Path("outputs")
            output_dir.mkdir(exist_ok=True)
            output_path = output_dir / f"{task_id}.wav"
        
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 保存音频 - model.generate() 返回 list[np.ndarray]
        # 取第一个音频（单个任务只生成一个音频）
        if isinstance(audio, list) and len(audio) > 0:
            waveform = audio[0]
        else:
            waveform = audio
        
        # 转换为 torch 张量（如果是 numpy 数组）
        if isinstance(waveform, np.ndarray):
            waveform = torch.from_numpy(waveform)
        
        # 确保 waveform 是 2D 张量 (channels, samples)
        if waveform.dim() == 1:
            waveform = waveform.unsqueeze(0)  # 添加通道维度: (T,) -> (1, T)
        elif waveform.dim() == 3:
            waveform = waveform.squeeze(0)  # 移除批次维度
        
        sf.write(str(output_path), waveform.squeeze(0).numpy(), sampling_rate)
        
        # 计算音频时长和 RTF
        audio_duration = waveform.shape[-1] / sampling_rate
        rtf = calculate_rtf(audio_duration, inference_time)
        
        # 更新任务状态
        task.status = "completed"
        task.progress = 100.0
        task.message = "生成完成"
        task.output_path = str(output_path)
        task.audio_duration = audio_duration
        task.inference_time = inference_time
        task.rtf = rtf
        task.completed_at = time.time()
        
        logger.info(f"任务 {task_id} 完成: RTF={rtf:.4f}, 时长={audio_duration:.2f}s, 推理时间={inference_time:.2f}s")
        stop_task_progress(task_id)
        print_task_completed(task)
        
    except Exception as e:
        logger.error(f"任务 {task_id} 失败: {e}")
        task.status = "failed"
        task.error = str(e)
        task.message = f"生成失败: {e}"
        task.completed_at = time.time()
        stop_task_progress(task_id)
        print_task_failed(task)
        raise


# ============== FastAPI 应用 ==============

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global executor
    
    # 启动时执行
    logger.info("=" * 60)
    logger.info("OmniVoice API Server 启动中...")
    logger.info("=" * 60)
    
    # 解析命令行参数
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--device", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-workers", type=int, default=4)
    args, _ = parser.parse_known_args()
    
    # 初始化线程池
    max_workers = args.max_workers
    executor = ThreadPoolExecutor(max_workers=max_workers)
    logger.info(f"线程池初始化完成，最大工作线程数: {max_workers}")
    
    # 加载模型
    model_path = args.model or os.path.join(os.getcwd(), "models")
    load_model(model_path, args.device)
    
    logger.info("=" * 60)
    logger.info(f"API 文档地址: http://{args.host}:{args.port}/docs")
    logger.info("=" * 60)
    
    yield
    
    # 关闭时执行
    logger.info("正在关闭服务器...")
    stop_task_dashboard()
    executor.shutdown(wait=True)
    logger.info("服务器已关闭")


# 创建 FastAPI 应用
app = FastAPI(
    title="OmniVoice API",
    description="OmniVoice TTS API - 支持声音克隆和声音设计",
    version="1.0.0",
    lifespan=lifespan,
)

# 添加 CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============== API 路由 ==============

@app.get("/", response_model=ServerInfo)
async def root():
    """获取服务器状态信息"""
    return ServerInfo(
        model_loaded=model is not None,
        device=str(device) if device else "unknown",
        dtype=str(dtype) if dtype else "unknown",
        sampling_rate=sampling_rate,
        max_workers=executor._max_workers if executor else 0,
    )


@app.get("/health")
async def health_check():
    """健康检查端点"""
    import torch
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

    status = "healthy" if model is not None else "degraded"
    status_code = 200

    return JSONResponse(
        status_code=status_code,
        content={
            "status": status,
            "model_loaded": model is not None,
            "device": str(device) if device else "unknown",
            "gpu_available": gpu_available,
            "gpu_memory_used_gb": gpu_memory_used,
            "gpu_memory_total_gb": gpu_memory_total,
            "running_tasks": running_tasks,
            "pending_tasks": pending_tasks,
            "max_workers": executor._max_workers if executor else 0,
        },
    )


@app.get("/api/v1/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(task_id: str):
    """获取任务状态"""
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
    status: Optional[str] = Query(None, description="按状态筛选 (pending/running/completed/failed)"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """列出所有任务"""
    task_list = list(tasks.values())
    
    if status:
        task_list = [t for t in task_list if t.status == status]
    
    # 按创建时间倒序
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
        ]
    }


@app.delete("/api/v1/tasks/{task_id}")
async def delete_task(task_id: str):
    """删除任务"""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    task = tasks[task_id]
    
    # 如果任务正在运行，不能删除
    if task.status == "running":
        raise HTTPException(status_code=400, detail="任务正在运行中，无法删除")
    
    # 删除输出文件
    if task.output_path and os.path.exists(task.output_path):
        try:
            os.remove(task.output_path)
        except Exception as e:
            logger.warning(f"删除文件失败: {e}")
    
    del tasks[task_id]
    
    return {"message": "任务已删除", "task_id": task_id}


@app.post("/api/v1/voice/clone", response_model=TaskResponse)
async def voice_clone(
    background_tasks: BackgroundTasks,
    text: str = Form(..., description="要合成的文本"),
    ref_audio: Optional[UploadFile] = File(None, description="参考音频文件（支持 wav, mp3 等格式）"),
    ref_audio_path: Optional[str] = Form(None, description="参考音频文件路径（直接使用本地路径，避免网关传输）"),
    ref_text: Optional[str] = Form(None, description="参考音频的文本（可选）"),
    instruct: Optional[str] = Form(None, description="声音风格控制指令（可选，如 'female, low pitch, whisper'）"),
    language: Optional[str] = Form(None, description="语言代码或名称"),
    output_path: Optional[str] = Form(None, description="输出文件路径（可选）"),
    num_steps: int = Form(32, description="扩散步数"),
    guidance_scale: float = Form(2.0, description="分类器自由引导尺度"),
    speed: float = Form(1.0, description="语速因子"),
    duration: Optional[float] = Form(None, description="固定输出时长（秒）"),
    denoise: bool = Form(True, description="是否启用去噪"),
    max_workers: int = Form(1, ge=1, le=8, description="处理线程数"),
):
    """
    声音克隆接口

    克隆参考音频的声音特征来合成目标文本。
    支持两种方式传入参考音频：
    - **文件上传**: 通过 ref_audio 字段上传音频文件
    - **直接路径**: 通过 ref_audio_path 字段传入本地音频文件路径（推荐本地调用，避免网关传输开销）

    两种方式二选一，同时提供时优先使用 ref_audio_path。
    """
    if model is None:
        raise HTTPException(status_code=503, detail="模型未加载")

    # 确定参考音频来源
    is_uploaded = False
    final_ref_audio_path = None

    if ref_audio_path and ref_audio_path.strip():
        final_ref_audio_path = ref_audio_path.strip()
        if not os.path.exists(final_ref_audio_path):
            raise HTTPException(status_code=400, detail=f"参考音频路径不存在: {final_ref_audio_path}")
    elif ref_audio is not None:
        is_uploaded = True
        upload_dir = Path("uploads")
        upload_dir.mkdir(exist_ok=True)
        task_id_tmp = str(uuid.uuid4())
        saved_path = upload_dir / f"{task_id_tmp}_{ref_audio.filename}"
        with open(saved_path, "wb") as f:
            content = await ref_audio.read()
            f.write(content)
        final_ref_audio_path = str(saved_path)
    else:
        raise HTTPException(status_code=400, detail="请提供 ref_audio（文件上传）或 ref_audio_path（本地路径）")

    # 创建任务
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
        "语速": speed,
        "引导尺度": guidance_scale,
    })

    # 创建请求对象
    request = VoiceCloneRequest(
        text=text,
        ref_text=ref_text,
        instruct=instruct,
        language=language,
        output_path=output_path,
        num_steps=num_steps,
        guidance_scale=guidance_scale,
        speed=speed,
        duration=duration,
        denoise=denoise,
        max_workers=max_workers,
    )

    # 在线程池中执行生成
    def run_clone():
        generate_voice_clone(task_id, request, final_ref_audio_path)

    # 使用线程池执行
    future = executor.submit(run_clone)

    return TaskResponse(
        task_id=task_id,
        status="pending",
        message="声音克隆任务已创建",
    )


@app.post("/api/v1/voice/design", response_model=TaskResponse)
async def voice_design(
    background_tasks: BackgroundTasks,
    text: str = Form(..., description="要合成的文本"),
    instruct: str = Form(..., description="声音描述指令（如 'female, low pitch, british accent'）"),
    language: Optional[str] = Form(None, description="语言代码或名称"),
    output_path: Optional[str] = Form(None, description="输出文件路径（可选）"),
    num_steps: int = Form(32, description="扩散步数"),
    guidance_scale: float = Form(2.0, description="分类器自由引导尺度"),
    speed: float = Form(1.0, description="语速因子"),
    duration: Optional[float] = Form(None, description="固定输出时长（秒）"),
    denoise: bool = Form(True, description="是否启用去噪"),
    max_workers: int = Form(1, ge=1, le=8, description="处理线程数"),
):
    """
    声音设计接口
    
    通过描述性指令设计声音特征，无需参考音频。
    
    支持的指令：
    - 性别: male/female (男/女)
    - 年龄: child, teenager, young adult, middle-aged, elderly (儿童/少年/青年/中年/老年)
    - 音调: very low pitch, low pitch, moderate pitch, high pitch, very high pitch
    - 风格: whisper (耳语)
    - 英语口音: american accent, british accent, australian accent, indian accent 等
    - 汉语方言: 四川话, 陕西话, 河南话, 东北话 等
    """
    if model is None:
        raise HTTPException(status_code=503, detail="模型未加载")
    
    # 创建任务
    task_id = str(uuid.uuid4())
    task = TaskInfo(
        task_id=task_id,
        task_type="design",
        status="pending",
        message="任务已创建，等待处理",
    )
    tasks[task_id] = task
    print_task_created(task, {
        "文本": text[:50] + ("..." if len(text) > 50 else ""),
        "指令": instruct[:50] + ("..." if len(instruct) > 50 else ""),
        "步数": num_steps,
        "语速": speed,
        "引导尺度": guidance_scale,
    })
    
    # 创建请求对象
    request = VoiceDesignRequest(
        text=text,
        instruct=instruct,
        language=language,
        output_path=output_path,
        num_steps=num_steps,
        guidance_scale=guidance_scale,
        speed=speed,
        duration=duration,
        denoise=denoise,
        max_workers=max_workers,
    )
    
    # 在线程池中执行生成
    def run_design():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            generate_voice_design(task_id, request)
        finally:
            loop.close()
    
    # 使用线程池执行
    future = executor.submit(run_design)
    
    return TaskResponse(
        task_id=task_id,
        status="pending",
        message="声音设计任务已创建",
    )


@app.get("/api/v1/voice/download/{task_id}")
async def download_audio(task_id: str):
    """下载生成的音频文件"""
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


# ============== WebSocket 实时进度 ==============

@app.websocket("/ws/tasks/{task_id}")
async def websocket_task_progress(websocket: WebSocket, task_id: str):
    """
    WebSocket 实时获取任务进度
    
    连接后服务端会定期推送任务进度更新，直到任务完成或失败。
    """
    await websocket.accept()
    
    if task_id not in tasks:
        await websocket.send_json({"error": "任务不存在"})
        await websocket.close()
        return
    
    task = tasks[task_id]
    
    try:
        while True:
            # 发送当前状态
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
            
            # 如果任务已完成或失败，关闭连接
            if task.status in ("completed", "failed"):
                await websocket.close()
                break
            
            # 等待一段时间再更新
            await asyncio.sleep(0.5)
            
    except Exception as e:
        logger.error(f"WebSocket 错误: {e}")
        await websocket.close()


# ============== SSE 实时进度 (备选方案) ==============

@app.get("/api/v1/tasks/{task_id}/progress")
async def task_progress_sse(task_id: str):
    """
    SSE (Server-Sent Events) 实时获取任务进度
    
    适合前端使用 EventSource API 接收进度更新。
    """
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


# ============== 批量处理接口 ==============

class BatchCloneItem(BaseModel):
    """批量克隆项目"""
    text: str = Field(..., description="要合成的文本", min_length=1)
    ref_audio: str = Field(..., description="参考音频文件路径或 URL")
    ref_text: Optional[str] = Field(None, description="参考音频的文本（可选）")
    instruct: Optional[str] = Field(None, description="声音风格控制指令（可选）")
    output_name: Optional[str] = Field(None, description="输出文件名（可选，默认使用序号）")


class BatchDesignItem(BaseModel):
    """批量设计项目"""
    text: str = Field(..., description="要合成的文本", min_length=1)
    instruct: str = Field(..., description="声音描述指令")
    output_name: Optional[str] = Field(None, description="输出文件名（可选，默认使用序号）")


class BatchTaskRequest(BaseModel):
    """批量任务请求"""
    items: List[Union[BatchCloneItem, BatchDesignItem]] = Field(..., description="批量任务列表", min_length=1)
    language: Optional[Union[str, List[Optional[str]]]] = Field(
        None, 
        description="语言代码或名称。可以是单个字符串（应用于所有任务）或列表（每个任务单独设置）"
    )
    num_steps: int = Field(32, ge=1, le=100, description="扩散步数")
    guidance_scale: float = Field(2.0, ge=0.0, le=10.0, description="分类器自由引导尺度")
    speed: Optional[Union[float, List[Optional[float]]]] = Field(
        1.0, 
        description="语速因子。可以是单个数字（应用于所有任务）或列表（每个任务单独设置，0.3-3.0，超限自动截断）"
    )
    duration: Optional[Union[float, List[Optional[float]]]] = Field(
        None, 
        description="固定输出时长（秒）。可以是单个数字（应用于所有任务）或列表（每个任务单独设置，1-300）"
    )
    denoise: bool = Field(True, description="是否启用去噪")
    max_workers: int = Field(4, ge=1, le=16, description="最大并行工作线程数")
    
    @field_validator('speed', mode='before')
    @classmethod
    def clamp_speed(cls, v):
        """截断语速到合法范围"""
        if v is None:
            return v
        if isinstance(v, list):
            return [_clamp_omni_speed(x) if x is not None else x for x in v]
        return _clamp_omni_speed(v)

    @field_validator('duration', 'language', mode='before')
    @classmethod
    def validate_list_length(cls, v, info: ValidationInfo):
        """验证列表长度与 items 数量一致"""
        if isinstance(v, list):
            items = info.data.get('items', [])
            if items and len(v) != len(items):
                raise ValueError(f'参数列表长度 ({len(v)}) 必须与 items 数量 ({len(items)}) 一致')
        return v


class BatchTaskResponse(BaseModel):
    """批量任务响应"""
    batch_id: str
    status: str
    message: str
    total_items: int
    task_ids: List[str]


class BatchTaskStatusResponse(BaseModel):
    """批量任务状态响应"""
    batch_id: str
    total_items: int
    completed_items: int
    failed_items: int
    pending_items: int
    running_items: int
    status: str  # "pending", "running", "completed", "failed", "partial"
    tasks: List[Dict[str, Any]]
    created_at: float
    completed_at: Optional[float] = None
    rtf_avg: Optional[float] = None
    duration_total: Optional[float] = None


@app.post("/api/v1/voice/batch", response_model=BatchTaskResponse)
async def batch_process(
    request: BatchTaskRequest,
):
    """
    批量处理接口
    
    一次性提交多个合成任务，支持混合批量（克隆和设计任务可同时提交）。
    使用多线程并行处理，提高处理效率。
    
    **示例请求：**
    ```json
    {
      "items": [
        {
          "text": "这是第一个测试文本",
          "ref_audio": "/path/to/audio1.wav",
          "ref_text": "参考音频文本"
        },
        {
          "text": "这是第二个测试文本",
          "instruct": "female, low pitch"
        }
      ],
      "language": "zh",
      "max_workers": 4
    }
    ```
    """
    if model is None:
        raise HTTPException(status_code=503, detail="模型未加载")
    
    # 创建批量任务 ID
    batch_id = str(uuid.uuid4())
    task_ids = []
    
    # 为每个项目创建子任务
    for idx, item in enumerate(request.items):
        task_id = f"{batch_id}_{idx:04d}"
        task_ids.append(task_id)
        
        # 确定任务类型
        if isinstance(item, BatchCloneItem):
            task_type = "clone"
        else:
            task_type = "design"
        
        # 创建任务信息
        task = TaskInfo(
            task_id=task_id,
            task_type=f"batch_{task_type}",
            status="pending",
            message=f"批量任务 {idx + 1}/{len(request.items)} 等待处理",
        )
        tasks[task_id] = task
        print_task_created(task, {"序号": f"{idx + 1}/{len(request.items)}", "类型": task_type})
    
    # 执行批量任务
    def run_batch():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            execute_batch_tasks(batch_id, request)
        finally:
            loop.close()
    
    executor.submit(run_batch)
    
    return BatchTaskResponse(
        batch_id=batch_id,
        status="pending",
        message=f"批量任务已创建，共 {len(request.items)} 个项目",
        total_items=len(request.items),
        task_ids=task_ids,
    )


def execute_batch_tasks(batch_id: str, request: BatchTaskRequest):
    """执行批量任务 - 使用模型原生批量推理"""
    from omnivoice import OmniVoiceGenerationConfig
    
    # 创建生成配置
    gen_config = OmniVoiceGenerationConfig(
        num_step=request.num_steps,
        guidance_scale=request.guidance_scale,
        denoise=request.denoise,
        postprocess_output=True,
    )
    
    # 创建批量任务的输出目录
    batch_output_dir = Path("outputs") / batch_id
    batch_output_dir.mkdir(parents=True, exist_ok=True)
    
    # 分离克隆和设计任务
    clone_items = []
    clone_indices = []
    design_items = []
    design_indices = []
    
    for idx, item in enumerate(request.items):
        if isinstance(item, BatchCloneItem):
            clone_items.append(item)
            clone_indices.append(idx)
        else:
            design_items.append(item)
            design_indices.append(idx)
    
    # 批量处理声音克隆任务
    if clone_items:
        process_batch_clone(
            batch_id=batch_id,
            items=clone_items,
            indices=clone_indices,
            gen_config=gen_config,
            language=request.language,
            speed=request.speed,
            duration=request.duration,
            output_dir=batch_output_dir,
            total_items=len(request.items),
        )
    
    # 批量处理声音设计任务
    if design_items:
        process_batch_design(
            batch_id=batch_id,
            items=design_items,
            indices=design_indices,
            gen_config=gen_config,
            language=request.language,
            speed=request.speed,
            duration=request.duration,
            output_dir=batch_output_dir,
            total_items=len(request.items),
        )


def process_batch_clone(
    batch_id: str,
    items: List[BatchCloneItem],
    indices: List[int],
    gen_config: "OmniVoiceGenerationConfig",
    language: Optional[Union[str, List[Optional[str]]]],
    speed: Optional[Union[float, List[Optional[float]]]],
    duration: Optional[Union[float, List[Optional[float]]]],
    output_dir: Path,
    total_items: int,
):
    """批量处理声音克隆任务 - 使用模型原生批量推理"""
    # 更新所有相关任务状态为 running
    for idx in indices:
        task_id = f"{batch_id}_{idx:04d}"
        task = tasks[task_id]
        task.status = "running"
        task.started_at = time.time()
        start_task_progress(task_id, f"批量克隆 {task_id[:12]}")
        update_task_progress(task_id, 10, "等待批量处理...")
    
    try:
        # 准备批量输入
        texts = [item.text for item in items]
        ref_audios = [item.ref_audio for item in items]
        ref_texts = [item.ref_text for item in items]
        instructs = [item.instruct for item in items]
        
        # 处理列表参数
        langs = language if isinstance(language, list) else [language] * len(items)
        speeds = speed if isinstance(speed, list) else [speed] * len(items)
        durations = duration if isinstance(duration, list) else [duration] * len(items)
        
        # 记录推理开始时间
        inference_start = time.time()
        
        # 更新进度
        for idx in indices:
            task_id = f"{batch_id}_{idx:04d}"
            update_task_progress(task_id, 30, f"正在批量生成 {len(texts)} 个音频...")
        
        # 使用模型原生批量推理
        audios = model.generate(
            text=texts,
            ref_audio=ref_audios,
            ref_text=ref_texts,
            instruct=instructs,
            language=langs,
            speed=speeds,
            duration=durations,
            generation_config=gen_config,
        )
        
        # 记录推理结束时间
        inference_time = time.time() - inference_start
        
        # 保存每个音频
        for i, (idx, item, audio) in enumerate(zip(indices, items, audios)):
            task_id = f"{batch_id}_{idx:04d}"
            task = tasks[task_id]
            
            try:
                update_task_progress(task_id, 80, "正在保存音频...")
                
                # 确定输出文件名
                if item.output_name:
                    output_name = item.output_name
                    if not output_name.endswith('.wav'):
                        output_name += '.wav'
                else:
                    output_name = f"{idx:04d}.wav"
                
                output_path = output_dir / output_name
                
                # 处理音频
                if isinstance(audio, list) and len(audio) > 0:
                    waveform = audio[0]
                else:
                    waveform = audio
                
                if isinstance(waveform, np.ndarray):
                    waveform = torch.from_numpy(waveform)
                
                if waveform.dim() == 1:
                    waveform = waveform.unsqueeze(0)
                elif waveform.dim() == 3:
                    waveform = waveform.squeeze(0)
                
                # 保存音频
                sf.write(str(output_path), waveform.squeeze(0).numpy(), sampling_rate)
                
                # 计算时长和 RTF
                audio_duration = waveform.shape[-1] / sampling_rate
                # 按比例分配推理时间
                item_inference_time = inference_time / len(items)
                rtf = calculate_rtf(audio_duration, item_inference_time)
                
                # 更新任务状态
                task.status = "completed"
                task.progress = 100.0
                task.message = f"任务 {idx + 1}/{total_items} 生成完成"
                task.output_path = str(output_path)
                task.audio_duration = audio_duration
                task.inference_time = item_inference_time
                task.rtf = rtf
                task.completed_at = time.time()
                
                logger.info(f"批量克隆任务 {task_id} 完成: RTF={rtf:.4f}, 时长={audio_duration:.2f}s")
                stop_task_progress(task_id)
                print_task_completed(task)
                
            except Exception as e:
                logger.error(f"保存批量克隆音频 {task_id} 失败: {e}")
                task.status = "failed"
                task.error = str(e)
                task.message = f"任务 {idx + 1} 保存失败: {e}"
                task.completed_at = time.time()
                stop_task_progress(task_id)
                print_task_failed(task)
        
    except Exception as e:
        logger.error(f"批量克隆任务失败: {e}")
        for idx in indices:
            task_id = f"{batch_id}_{idx:04d}"
            task = tasks[task_id]
            task.status = "failed"
            task.error = str(e)
            task.message = f"批量生成失败: {e}"
            task.completed_at = time.time()
            stop_task_progress(task_id)
            print_task_failed(task)


def process_batch_design(
    batch_id: str,
    items: List[BatchDesignItem],
    indices: List[int],
    gen_config: "OmniVoiceGenerationConfig",
    language: Optional[Union[str, List[Optional[str]]]],
    speed: Optional[Union[float, List[Optional[float]]]],
    duration: Optional[Union[float, List[Optional[float]]]],
    output_dir: Path,
    total_items: int,
):
    """批量处理声音设计任务 - 使用模型原生批量推理"""
    # 更新所有相关任务状态为 running
    for idx in indices:
        task_id = f"{batch_id}_{idx:04d}"
        task = tasks[task_id]
        task.status = "running"
        task.started_at = time.time()
        start_task_progress(task_id, f"批量设计 {task_id[:12]}")
        update_task_progress(task_id, 10, "等待批量处理...")
    
    try:
        # 准备批量输入
        texts = [item.text for item in items]
        instructs = [item.instruct for item in items]
        
        # 处理列表参数
        langs = language if isinstance(language, list) else [language] * len(items)
        speeds = speed if isinstance(speed, list) else [speed] * len(items)
        durations = duration if isinstance(duration, list) else [duration] * len(items)
        
        # 记录推理开始时间
        inference_start = time.time()
        
        # 更新进度
        for idx in indices:
            task_id = f"{batch_id}_{idx:04d}"
            update_task_progress(task_id, 30, f"正在批量生成 {len(texts)} 个音频...")
        
        # 使用模型原生批量推理
        audios = model.generate(
            text=texts,
            instruct=instructs,
            language=langs,
            speed=speeds,
            duration=durations,
            generation_config=gen_config,
        )
        
        # 记录推理结束时间
        inference_time = time.time() - inference_start
        
        # 保存每个音频
        for i, (idx, item, audio) in enumerate(zip(indices, items, audios)):
            task_id = f"{batch_id}_{idx:04d}"
            task = tasks[task_id]
            
            try:
                update_task_progress(task_id, 80, "正在保存音频...")
                
                # 确定输出文件名
                if item.output_name:
                    output_name = item.output_name
                    if not output_name.endswith('.wav'):
                        output_name += '.wav'
                else:
                    output_name = f"{idx:04d}.wav"
                
                output_path = output_dir / output_name
                
                # 处理音频
                if isinstance(audio, list) and len(audio) > 0:
                    waveform = audio[0]
                else:
                    waveform = audio
                
                if isinstance(waveform, np.ndarray):
                    waveform = torch.from_numpy(waveform)
                
                if waveform.dim() == 1:
                    waveform = waveform.unsqueeze(0)
                elif waveform.dim() == 3:
                    waveform = waveform.squeeze(0)
                
                # 保存音频
                sf.write(str(output_path), waveform.squeeze(0).numpy(), sampling_rate)
                
                # 计算时长和 RTF
                audio_duration = waveform.shape[-1] / sampling_rate
                # 按比例分配推理时间
                item_inference_time = inference_time / len(items)
                rtf = calculate_rtf(audio_duration, item_inference_time)
                
                # 更新任务状态
                task.status = "completed"
                task.progress = 100.0
                task.message = f"任务 {idx + 1}/{total_items} 生成完成"
                task.output_path = str(output_path)
                task.audio_duration = audio_duration
                task.inference_time = item_inference_time
                task.rtf = rtf
                task.completed_at = time.time()
                
                logger.info(f"批量设计任务 {task_id} 完成: RTF={rtf:.4f}, 时长={audio_duration:.2f}s")
                stop_task_progress(task_id)
                print_task_completed(task)
                
            except Exception as e:
                logger.error(f"保存批量设计音频 {task_id} 失败: {e}")
                task.status = "failed"
                task.error = str(e)
                task.message = f"任务 {idx + 1} 保存失败: {e}"
                task.completed_at = time.time()
                stop_task_progress(task_id)
                print_task_failed(task)
        
    except Exception as e:
        logger.error(f"批量设计任务失败: {e}")
        for idx in indices:
            task_id = f"{batch_id}_{idx:04d}"
            task = tasks[task_id]
            task.status = "failed"
            task.error = str(e)
            task.message = f"批量生成失败: {e}"
            task.completed_at = time.time()
            stop_task_progress(task_id)
            print_task_failed(task)


@app.get("/api/v1/voice/batch/{batch_id}", response_model=BatchTaskStatusResponse)
async def get_batch_status(batch_id: str):
    """
    获取批量任务状态
    
    返回批量任务的整体进度和每个子任务的详细状态。
    """
    # 查找属于该批量的所有任务
    batch_tasks = [t for t in tasks.values() if t.task_id.startswith(batch_id)]
    
    if not batch_tasks:
        raise HTTPException(status_code=404, detail="批量任务不存在")
    
    # 统计各状态任务数量
    completed = sum(1 for t in batch_tasks if t.status == "completed")
    failed = sum(1 for t in batch_tasks if t.status == "failed")
    running = sum(1 for t in batch_tasks if t.status == "running")
    pending = sum(1 for t in batch_tasks if t.status == "pending")
    total = len(batch_tasks)
    
    # 确定批量任务整体状态
    if completed == total:
        batch_status = "completed"
    elif failed == total:
        batch_status = "failed"
    elif failed > 0 or completed > 0:
        batch_status = "partial"
    elif running > 0:
        batch_status = "running"
    else:
        batch_status = "pending"
    
    # 计算平均 RTF 和总时长
    completed_tasks = [t for t in batch_tasks if t.status == "completed"]
    rtf_avg = None
    duration_total = None
    
    if completed_tasks:
        rtf_values = [t.rtf for t in completed_tasks if t.rtf > 0]
        duration_values = [t.audio_duration for t in completed_tasks if t.audio_duration > 0]
        
        if rtf_values:
            rtf_avg = sum(rtf_values) / len(rtf_values)
        if duration_values:
            duration_total = sum(duration_values)
    
    # 获取创建时间和完成时间
    created_at = min(t.created_at for t in batch_tasks)
    completed_at = None
    
    if batch_status in ("completed", "failed", "partial"):
        completed_times = [t.completed_at for t in batch_tasks if t.completed_at]
        if completed_times:
            completed_at = max(completed_times)
    
    # 构建任务详情列表
    task_details = []
    for t in sorted(batch_tasks, key=lambda x: x.task_id):
        task_details.append({
            "task_id": t.task_id,
            "task_type": t.task_type,
            "status": t.status,
            "progress": t.progress,
            "message": t.message,
            "output_path": t.output_path,
            "rtf": t.rtf if t.status == "completed" else None,
            "audio_duration": t.audio_duration if t.status == "completed" else None,
            "error": t.error,
        })
    
    return BatchTaskStatusResponse(
        batch_id=batch_id,
        total_items=total,
        completed_items=completed,
        failed_items=failed,
        pending_items=pending,
        running_items=running,
        status=batch_status,
        tasks=task_details,
        created_at=created_at,
        completed_at=completed_at,
        rtf_avg=rtf_avg,
        duration_total=duration_total,
    )


@app.get("/api/v1/voice/batch/{batch_id}/download")
async def download_batch_audio(batch_id: str):
    """
    批量下载音频文件
    
    将批量任务中所有成功生成的音频文件打包为 ZIP 文件下载。
    """
    import zipfile
    import io
    
    # 查找属于该批量的所有任务
    batch_tasks = [t for t in tasks.values() if t.task_id.startswith(batch_id)]
    
    if not batch_tasks:
        raise HTTPException(status_code=404, detail="批量任务不存在")
    
    # 收集已完成任务的音频文件
    completed_tasks = [t for t in batch_tasks if t.status == "completed" and t.output_path]
    
    if not completed_tasks:
        raise HTTPException(status_code=400, detail="没有可下载的音频文件")
    
    # 创建 ZIP 文件
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for task in completed_tasks:
            if os.path.exists(task.output_path):
                # 使用任务 ID 的最后一部分作为文件名
                filename = f"{task.task_id.split('_')[-1]}_{Path(task.output_path).name}"
                zip_file.write(task.output_path, filename)
    
    zip_buffer.seek(0)
    
    return StreamingResponse(
        iter([zip_buffer.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=batch_{batch_id}.zip"},
    )


@app.delete("/api/v1/voice/batch/{batch_id}")
async def delete_batch_tasks(batch_id: str):
    """
    删除批量任务
    
    删除批量任务中的所有子任务及其输出文件。
    """
    # 查找属于该批量的所有任务
    batch_tasks = [t for t in tasks.values() if t.task_id.startswith(batch_id)]
    
    if not batch_tasks:
        raise HTTPException(status_code=404, detail="批量任务不存在")
    
    # 检查是否有正在运行的任务
    running_tasks = [t for t in batch_tasks if t.status == "running"]
    if running_tasks:
        raise HTTPException(status_code=400, detail=f"批量任务中有 {len(running_tasks)} 个任务正在运行，无法删除")
    
    # 删除任务和文件
    deleted_count = 0
    for task in batch_tasks:
        # 删除输出文件
        if task.output_path and os.path.exists(task.output_path):
            try:
                os.remove(task.output_path)
            except Exception as e:
                logger.warning(f"删除文件失败: {e}")
        
        # 删除任务记录
        if task.task_id in tasks:
            del tasks[task.task_id]
            deleted_count += 1
    
    # 尝试删除批量任务目录
    batch_output_dir = Path("outputs") / batch_id
    if batch_output_dir.exists():
        try:
            import shutil
            shutil.rmtree(batch_output_dir)
        except Exception as e:
            logger.warning(f"删除批量任务目录失败: {e}")
    
    return {
        "message": f"批量任务已删除，共删除 {deleted_count} 个子任务",
        "batch_id": batch_id,
        "deleted_count": deleted_count,
    }


# ============== 主函数 ==============

if __name__ == "__main__":
    import uvicorn
    
    parser = argparse.ArgumentParser(description="OmniVoice API Server")
    parser.add_argument("--host", default="localhost", help="服务器地址")
    parser.add_argument("--port", type=int, default=8853, help="服务器端口")
    parser.add_argument("--device", default="cuda", help="计算设备 (cuda/cpu/mps)")
    parser.add_argument("--model", default='models/omnivoice', help="模型路径")
    parser.add_argument("--max-workers", type=int, default=2, help="最大工作线程数")
    
    args = parser.parse_args()
    
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        reload=False,
        workers=1,
        access_log=False
    )
