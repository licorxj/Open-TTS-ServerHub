#!/usr/bin/env python3
"""
VoxCPM FastAPI Server

提供完整的 TTS API 接口，支持：
- 声音设计 (Voice Design) — 无需参考音频，通过描述创造声音
- 可控克隆 (Controllable Cloning) — 参考音频 + 可选风格控制
- 极致克隆 (Ultimate Cloning) — 音频续写模式，完整还原声音细节
- 异步多线程处理 + GPU 并发控制
- 实时进度推送 (WebSocket/SSE)
- RTF 速率显示
- 批量处理

Usage:
    python api_server.py [--host 0.0.0.0] [--port 8854] [--device cuda] [--model path]
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import uuid
import shutil
import tempfile
import zipfile
import io
import yaml
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import numpy as np
import torch
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# 服务器配置（统一由 config/vox_server.yaml 提供，文件缺失回退内置默认）
# ---------------------------------------------------------------------------
SERVER_CONFIG_PATH = os.path.join(_project_root, "config", "vox_server.yaml")

DEFAULT_SERVER_CONFIG = {
    "server": {
        "host": "0.0.0.0",
        "port": 8854,
        "device": "cuda",
        "max_workers": 2,
        "gpu_concurrency": 1,
    },
    "model": {
        "model_dir": "models/VoxCPM2",
        "optimize": False,
    },
    "inference": {
        "cfg_value": 2.0,
        "inference_timesteps": 10,
        "normalize": False,
        "denoise": True,
    },
    "batch": {
        "enabled": False,
        "inbound_limit": 15,
        "batch_size": 3,
        "max_assembly_length": 60.0,
        "batch_timeout": 0.05,
    },
}


def load_server_config():
    """加载 config/vox_server.yaml，缺失/异常时回退到默认配置。"""
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
INF = CONFIG.get("inference", {})

_py312env = os.path.join(_project_root, "py312env")
_ffmpeg_bin = os.path.join(_py312env, "conda_pkgs",
                           "ffmpeg-7.1.1-gpl_he3062b8_911", "Library", "bin")
if os.path.isdir(_ffmpeg_bin):
    os.add_dll_directory(_ffmpeg_bin)
    _torchcodec_dir = os.path.join(_py312env, "Lib", "site-packages", "torchcodec")
    _dll_names = ["avutil-59.dll", "swresample-5.dll", "swscale-8.dll",
                  "avcodec-61.dll", "avformat-61.dll", "avdevice-61.dll",
                  "avfilter-10.dll", "postproc-58.dll"]
    for _dll in _dll_names:
        _src = os.path.join(_ffmpeg_bin, _dll)
        _dst = os.path.join(_torchcodec_dir, _dll)
        if os.path.isfile(_src) and not os.path.isfile(_dst):
            try:
                shutil.copy2(_src, _dst)
            except Exception:
                pass

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
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
    "design": "🎨 声音设计",
    "clone": "🎛️ 可控克隆",
    "ultimate_clone": "🎙️ 极致克隆",
    "batch_design": "🎨 批量声音设计",
    "batch_clone": "🎛️ 批量可控克隆",
    "batch_ultimate_clone": "🎙️ 批量极致克隆",
}

task_progress_map: Dict[str, Progress] = {}
task_progress_task_ids: Dict[str, int] = {}
task_panel_params: Dict[str, dict] = {}
task_live: Optional[Live] = None
task_live_lock = threading.RLock()

# 是否启用实时任务面板（console.clear 清屏刷新）。
# 默认关闭：清屏刷新会造成控制台高频刷屏，影响日志可读性。
# 设置环境变量 TTS_DASHBOARD=1 可恢复旧版实时面板。
TTS_DASHBOARD = os.environ.get("TTS_DASHBOARD", "0") == "1"


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
    # 默认关闭清屏刷屏面板（TTS_DASHBOARD=1 时恢复），避免高频刷新刷屏控制台。
    if not TTS_DASHBOARD:
        return
    console.clear()
    console.print(_render_task_dashboard())


def _log_task_status(task: "TaskInfo", event: str):
    """关闭实时面板时的轻量状态日志，不刷屏。"""
    if TTS_DASHBOARD:
        return
    logger.info(
        f"[任务 {task.task_id[:12]}] {event} | 类型={task.task_type} | 状态={task.status}"
        + (f" | 进度={task.progress:.0f}%" if task.progress else "")
        + (f" | 消息={task.message}" if task.message else "")
        + (f" | 耗时={task.inference_time:.2f}s" if task.inference_time else "")
    )


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
    _log_task_status(task, "创建")


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
            _log_task_status(task, "开始")


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
            # 关闭面板模式下，按 10% 步进输出一行进度日志，避免刷屏
            last_pct = getattr(task, "_last_log_pct", -10.0)
            if not TTS_DASHBOARD and int(progress_val // 10) != int(last_pct // 10):
                task._last_log_pct = progress_val
                logger.info(
                    f"[任务 {task.task_id[:12]}] 进度 {min(100.0, max(0.0, progress_val)):.0f}%"
                    f" | {message or '处理中...'}"
                )


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
            _log_task_status(task, "结束")


def print_task_completed(task: "TaskInfo"):
    show_task_card(task)


def print_task_failed(task: "TaskInfo"):
    show_task_card(task)

# 全局变量
voxcpm_model = None
asr_model = None
device = None
sampling_rate = 16000  # VoxCPM 默认采样率

# 任务管理器
tasks: Dict[str, "TaskInfo"] = {}
executor = None
gpu_semaphore = None  # GPU 并发控制信号量

# ============== 透明批量推理流水线（CPU 组装 / GPU 消费） ==============
# 开启 --batch 后：外部「单条」请求进入入队队列，CPU 动态组装成批次任务放入组装队列，
# GPU 消费组装队列跑 generate_batch，产出的片段由 CPU 拆分并路由回各个请求。
batch_enabled = False
batch_inbound_limit = 15       # 入队队列上限（堆积的外部请求数），默认 15
batch_size = 3                 # 组装队列批次大小（一次 generate_batch 的条目数），默认 3
max_assembly_length = 60.0     # 单次组装参考/续写音频总时长上限（秒），防止组装过长爆显存
batch_timeout = 0.05           # 组装超时（秒）：低流量时尽快 flush 当前批次
inbound_queue = None           # asyncio.Queue：外部请求入队
batch_queue = None             # asyncio.Queue：组装好的批次
_batch_tasks = []              # 后台任务句柄，避免被 GC


@dataclass
class TaskInfo:
    """任务信息"""
    task_id: str
    task_type: str  # "design", "clone", "ultimate_clone", "batch_design", "batch_clone", "batch_ultimate_clone"
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

SPEED_MIN = 0.3
SPEED_MAX = 3.0


def _clamp_speed(v: Optional[float]) -> Optional[float]:
    """截断语速到合法范围，避免报错"""
    if v is None:
        return v
    return max(SPEED_MIN, min(SPEED_MAX, v))


class VoiceDesignRequest(BaseModel):
    """声音设计请求 — 无需参考音频"""
    text: str = Field(..., description="要合成的文本", min_length=1)
    instruct: str = Field(..., description="声音描述指令（如 'A young woman with a warm, gentle voice'）")
    language: Optional[str] = Field(None, description="语言代码（如 'zh', 'en'）— 用于提示，非强制")
    output_path: Optional[str] = Field(None, description="输出文件路径（可选，默认使用任务ID）")
    cfg_value: float = Field(INF.get("cfg_value", 2.0), ge=1.0, le=5.0, description="CFG 引导强度（1.0-5.0）")
    inference_timesteps: int = Field(INF.get("inference_timesteps", 10), ge=1, le=50, description="LocDiT 流匹配迭代步数")
    normalize: bool = Field(INF.get("normalize", False), description="是否启用文本规范化")
    denoise: bool = Field(INF.get("denoise", False), description="是否对参考音频降噪（声音设计模式下无效）")
    speed: Optional[float] = Field(None, description="语速因子（>1 加快，<1 放慢，映射为语速提示词，超限自动截断）")

    @field_validator('speed', mode='before')
    @classmethod
    def clamp_speed(cls, v):
        return _clamp_speed(v)


class VoiceCloneRequest(BaseModel):
    """可控克隆请求 — 参考音频 + 可选风格控制"""
    text: str = Field(..., description="要合成的文本", min_length=1)
    instruct: Optional[str] = Field(None, description="可选的声音风格控制指令（如 'Excited and fast-paced'）")
    language: Optional[str] = Field(None, description="语言代码")
    output_path: Optional[str] = Field(None, description="输出文件路径（可选）")
    cfg_value: float = Field(INF.get("cfg_value", 2.0), ge=1.0, le=5.0, description="CFG 引导强度")
    inference_timesteps: int = Field(INF.get("inference_timesteps", 10), ge=1, le=50, description="LocDiT 流匹配迭代步数")
    normalize: bool = Field(INF.get("normalize", False), description="是否启用文本规范化")
    denoise: bool = Field(INF.get("denoise", True), description="是否对参考音频降噪增强")
    speed: Optional[float] = Field(None, description="语速因子（>1 加快，<1 放慢，映射为语速提示词，超限自动截断）")

    @field_validator('speed', mode='before')
    @classmethod
    def clamp_speed(cls, v):
        return _clamp_speed(v)


class UltimateCloneRequest(BaseModel):
    """极致克隆请求 — 音频续写模式"""
    text: str = Field(..., description="要续写的文本", min_length=1)
    prompt_text: Optional[str] = Field(None, description="参考音频的文本内容（不提供则自动 ASR 识别）")
    language: Optional[str] = Field(None, description="语言代码")
    output_path: Optional[str] = Field(None, description="输出文件路径（可选）")
    cfg_value: float = Field(INF.get("cfg_value", 2.0), ge=1.0, le=5.0, description="CFG 引导强度")
    inference_timesteps: int = Field(INF.get("inference_timesteps", 10), ge=1, le=50, description="LocDiT 流匹配迭代步数")
    normalize: bool = Field(INF.get("normalize", False), description="是否启用文本规范化")
    denoise: bool = Field(INF.get("denoise", True), description="是否对参考音频降噪增强")
    speed: Optional[float] = Field(None, description="语速因子（>1 加快，<1 放慢，映射为语速提示词，超限自动截断）")

    @field_validator('speed', mode='before')
    @classmethod
    def clamp_speed(cls, v):
        return _clamp_speed(v)


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
    sampling_rate: int
    max_workers: int
    gpu_concurrency: int
    model_type: Optional[str] = None


# ============== 辅助函数 ==============

def get_best_device():
    """自动检测最佳可用设备"""
    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def load_voxcpm_model(model_path: str, device_str: str, optimize: bool = True):
    """加载 VoxCPM 模型"""
    global voxcpm_model, asr_model, device, sampling_rate

    # 优先使用项目内置源码版 voxcpm（含 generate_batch），覆盖可能已安装的旧版站点包
    _src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
    if os.path.isdir(_src) and _src not in sys.path:
        sys.path.insert(0, _src)
    import voxcpm
    from funasr import AutoModel

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from tools.models_manager import ModelsManager

    mgr = ModelsManager()
    resolved_path = mgr.resolve_path(model_path, model_type="vox")

    device = device_str if device_str else get_best_device()

    logger.info(f"正在加载 VoxCPM 模型: {resolved_path}")
    logger.info(f"设备: {device}")

    # 加载 VoxCPM 模型
    voxcpm_model = voxcpm.VoxCPM.from_pretrained(
        resolved_path,
        optimize=optimize,
    )
    sampling_rate = voxcpm_model.tts_model.sample_rate

    # FlashAttention 提醒：VoxCPM 的 CFM 注意力层走 PyTorch SDPA（minicpm4/model.py），
    # flash_attn 安装后会自动选用 flash 后端，无需改推理代码。
    try:
        import torch
        if torch.cuda.is_available() and torch.backends.cuda.flash_sdp_enabled():
            import flash_attn
            print(f"[FlashAttention] VoxCPM CFM 注意力层已自动选用 flash 后端 (flash_attn {flash_attn.__version__})")
        else:
            print("[FlashAttention] VoxCPM 注意力走 SDPA（flash 后端未启用，将用 memory-efficient/eager）")
    except Exception:
        print("[FlashAttention] VoxCPM 注意力走 SDPA（flash_attn 未安装）")

    # 加载 ASR 模型（用于极致克隆模式的自动转录）
    logger.info("正在加载 ASR 模型...")
    asr_device = "cuda:0" if device == "cuda" else "cpu"
    asr_model = AutoModel(
        model="iic/SenseVoiceSmall",
        disable_update=True,
        log_level="WARNING",
        device=asr_device,
    )

    # 检测模型类型
    from voxcpm.model.voxcpm2 import VoxCPM2Model
    model_type = "VoxCPM2" if isinstance(voxcpm_model.tts_model, VoxCPM2Model) else "VoxCPM"

    logger.info(f"模型加载完成！类型: {model_type}, 采样率: {sampling_rate}Hz")
    return voxcpm_model


def recognize_audio_text(audio_path: str) -> str:
    """使用 ASR 识别音频文本"""
    if asr_model is None:
        raise RuntimeError("ASR 模型未加载")
    res = asr_model.generate(input=audio_path, language="auto", use_itn=True)
    return res[0]["text"].split("|>")[-1]


def update_task_progress(task_id: str, progress: float, message: str = ""):
    """更新任务进度"""
    if task_id in tasks:
        tasks[task_id].progress = min(100.0, max(0.0, progress))
        if message:
            tasks[task_id].message = message
        logger.debug(f"任务 {task_id}: {progress:.1f}% - {message}")
        _update_task_progress(task_id, progress, message)


def calculate_rtf(audio_duration: float, inference_time: float) -> float:
    """计算 RTF (Real-Time Factor)"""
    if audio_duration <= 0:
        return 0.0
    return inference_time / audio_duration


def save_audio(waveform: np.ndarray, output_path: Path, sr: int):
    """保存音频波形到文件"""
    import soundfile as sf
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(output_path), waveform, sr)


def speed_to_instruct(speed: float) -> str:
    """将 speed 浮点数映射为语速提示词"""
    if speed is None or speed == 1.0:
        return ""
    if speed <= 0.4:
        return "语速极慢"
    elif speed <= 0.6:
        return "语速很慢"
    elif speed <= 0.8:
        return "语速偏慢"
    elif speed < 1.0:
        return "语速略微偏慢"
    elif speed <= 1.25:
        return "语速略微偏快"
    elif speed <= 1.5:
        return "语速偏快"
    elif speed <= 2.0:
        return "语速很快"
    else:
        return "语速极快"


def build_generate_text(text: str, instruct: Optional[str] = None, speed: Optional[float] = None) -> str:
    """构建带控制指令的文本格式。speed 提示词放在最前面。"""
    text = text.strip()
    speed_hint = speed_to_instruct(speed) if speed is not None else ""
    existing = ""
    if instruct:
        instruct = instruct.strip()
        instruct = re.sub(r"[()（）]", "", instruct).strip()
        if instruct:
            existing = instruct
    if speed_hint and speed_hint not in existing:
        merged_instruct = f"{speed_hint}，{existing}" if existing else speed_hint
    else:
        merged_instruct = existing if existing else None
    if merged_instruct:
        return f"({merged_instruct}){text}"
    return text


# ============== 推理执行函数 ==============

def execute_voice_design(task_id: str, request: VoiceDesignRequest):
    """执行声音设计（在线程池中运行）"""
    task = tasks[task_id]
    task.status = "running"
    task.started_at = time.time()
    start_task_progress(task_id, f"声音设计 {task_id[:12]}")

    try:
        update_task_progress(task_id, 10, "正在准备声音设计...")

        # 构建带控制指令的文本
        final_text = build_generate_text(request.text, request.instruct, speed=request.speed)

        update_task_progress(task_id, 25, f"开始生成 (timesteps={request.inference_timesteps})...")

        # 记录推理开始时间
        inference_start = time.time()

        # 获取 GPU 信号量（如果有）
        acquired = gpu_semaphore is not None
        if acquired:
            gpu_semaphore.acquire()
        try:
            audio = voxcpm_model.generate(
                text=final_text,
                cfg_value=request.cfg_value,
                inference_timesteps=request.inference_timesteps,
                normalize=request.normalize,
                denoise=request.denoise,
            )
        finally:
            if acquired:
                gpu_semaphore.release()

        inference_time = time.time() - inference_start

        update_task_progress(task_id, 85, "正在保存音频...")

        # 确定输出路径
        if request.output_path:
            output_path = Path(request.output_path)
        else:
            output_dir = Path("outputs")
            output_dir.mkdir(exist_ok=True)
            output_path = output_dir / f"{task_id}.wav"

        # 保存音频
        save_audio(audio, output_path, sampling_rate)

        # 计算音频时长和 RTF
        audio_duration = len(audio) / sampling_rate
        rtf = calculate_rtf(audio_duration, inference_time)

        # 更新任务状态
        task.status = "completed"
        task.progress = 100.0
        task.message = "声音设计完成"
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
        task.message = f"声音设计失败: {e}"
        task.completed_at = time.time()
        stop_task_progress(task_id)
        print_task_failed(task)
        raise


def execute_voice_clone(task_id: str, request: VoiceCloneRequest, ref_audio_path: str):
    """执行可控克隆（在线程池中运行）"""
    task = tasks[task_id]
    task.status = "running"
    task.started_at = time.time()
    start_task_progress(task_id, f"可控克隆 {task_id[:12]}")

    try:
        update_task_progress(task_id, 5, "正在加载参考音频...")

        if not os.path.exists(ref_audio_path):
            raise FileNotFoundError(f"参考音频不存在: {ref_audio_path}")

        update_task_progress(task_id, 15, "正在构建克隆参数...")

        # 构建带控制指令的文本
        final_text = build_generate_text(request.text, request.instruct, speed=request.speed)

        update_task_progress(task_id, 25, f"开始生成 (timesteps={request.inference_timesteps})...")

        # 记录推理开始时间
        inference_start = time.time()

        acquired = gpu_semaphore is not None
        if acquired:
            gpu_semaphore.acquire()
        try:
            audio = voxcpm_model.generate(
                text=final_text,
                reference_wav_path=ref_audio_path,
                cfg_value=request.cfg_value,
                inference_timesteps=request.inference_timesteps,
                normalize=request.normalize,
                denoise=request.denoise,
            )
        finally:
            if acquired:
                gpu_semaphore.release()

        inference_time = time.time() - inference_start

        update_task_progress(task_id, 85, "正在保存音频...")

        # 确定输出路径
        if request.output_path:
            output_path = Path(request.output_path)
        else:
            output_dir = Path("outputs")
            output_dir.mkdir(exist_ok=True)
            output_path = output_dir / f"{task_id}.wav"

        # 保存音频
        save_audio(audio, output_path, sampling_rate)

        # 计算音频时长和 RTF
        audio_duration = len(audio) / sampling_rate
        rtf = calculate_rtf(audio_duration, inference_time)

        # 更新任务状态
        task.status = "completed"
        task.progress = 100.0
        task.message = "可控克隆完成"
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
        task.message = f"可控克隆失败: {e}"
        task.completed_at = time.time()
        stop_task_progress(task_id)
        print_task_failed(task)
        raise


def execute_ultimate_clone(task_id: str, request: UltimateCloneRequest, ref_audio_path: str):
    """执行极致克隆（在线程池中运行）
    当提供 speed 参数时，自动切换为指令克隆模式以支持语速控制。
    """
    task = tasks[task_id]
    task.status = "running"
    task.started_at = time.time()

    # 有 speed 参数时，切换为指令克隆模式
    use_clone_mode = request.speed is not None and request.speed != 1.0
    mode_label = "指令克隆(语速控制)" if use_clone_mode else "极致克隆"
    start_task_progress(task_id, f"{mode_label} {task_id[:12]}")

    try:
        update_task_progress(task_id, 5, "正在加载参考音频...")

        if not os.path.exists(ref_audio_path):
            raise FileNotFoundError(f"参考音频不存在: {ref_audio_path}")

        if use_clone_mode:
            # 切换为指令克隆模式：用 reference_wav_path + speed instruct
            update_task_progress(task_id, 20, f"已切换为指令克隆模式 (speed={request.speed})")
            final_text = build_generate_text(request.text, speed=request.speed)

            update_task_progress(task_id, 30, f"开始生成 (timesteps={request.inference_timesteps})...")
            inference_start = time.time()

            acquired = gpu_semaphore is not None
            if acquired:
                gpu_semaphore.acquire()
            try:
                audio = voxcpm_model.generate(
                    text=final_text,
                    reference_wav_path=ref_audio_path,
                    cfg_value=request.cfg_value,
                    inference_timesteps=request.inference_timesteps,
                    normalize=request.normalize,
                    denoise=request.denoise,
                )
            finally:
                if acquired:
                    gpu_semaphore.release()
        else:
            # 极致克隆模式：用 prompt_wav_path + prompt_text 续写
            # 获取 GPU 信号量：ASR(SenseVoiceSmall, 在 cuda 上) 与 generate 都在 GPU 上，
            # 必须纳入同一信号量，避免与其他请求的 GPU 推理并发争抢 / 显存叠加 OOM。
            acquired = gpu_semaphore is not None
            if acquired:
                gpu_semaphore.acquire()
            try:
                prompt_text = request.prompt_text
                if not prompt_text or not prompt_text.strip():
                    update_task_progress(task_id, 10, "正在 ASR 识别参考音频文本...")
                    prompt_text = recognize_audio_text(ref_audio_path)
                    logger.info(f"ASR 识别结果: {prompt_text[:80]}...")
                prompt_text = prompt_text.strip()

                update_task_progress(task_id, 20, "正在构建续写参数...")
                update_task_progress(task_id, 30, f"开始生成 (timesteps={request.inference_timesteps})...")
                inference_start = time.time()

                audio = voxcpm_model.generate(
                    text=request.text,
                    prompt_wav_path=ref_audio_path,
                    prompt_text=prompt_text,
                    cfg_value=request.cfg_value,
                    inference_timesteps=request.inference_timesteps,
                    normalize=request.normalize,
                    denoise=request.denoise,
                )
            finally:
                if acquired:
                    gpu_semaphore.release()

        inference_time = time.time() - inference_start

        update_task_progress(task_id, 85, "正在保存音频...")

        # 确定输出路径
        if request.output_path:
            output_path = Path(request.output_path)
        else:
            output_dir = Path("outputs")
            output_dir.mkdir(exist_ok=True)
            output_path = output_dir / f"{task_id}.wav"

        # 保存音频
        save_audio(audio, output_path, sampling_rate)

        # 计算音频时长和 RTF
        audio_duration = len(audio) / sampling_rate
        rtf = calculate_rtf(audio_duration, inference_time)

        # 更新任务状态
        task.status = "completed"
        task.progress = 100.0
        task.message = "极致克隆完成"
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
        task.message = f"极致克隆失败: {e}"
        task.completed_at = time.time()
        stop_task_progress(task_id)
        print_task_failed(task)
        raise


# ============== FastAPI 应用 ==============

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global executor, gpu_semaphore, batch_enabled, batch_inbound_limit, batch_size, max_assembly_length, batch_timeout

    logger.info("=" * 60)
    logger.info("VoxCPM API Server 启动中...")
    logger.info("=" * 60)

    # 解析命令行参数（未传则一律以 config/vox_server.yaml 为准）
    srv_cfg = CONFIG.get("server", {})
    model_cfg = CONFIG.get("model", {})

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--device", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-workers", type=int, default=None)
    parser.add_argument("--gpu-concurrency", type=int, default=None, help="GPU 并发推理数量")
    parser.add_argument("--optimize", dest="optimize", action="store_true", default=None, help="启用 torch.compile 优化（默认读 config）")
    parser.add_argument("--no-optimize", dest="optimize", action="store_false", help="禁用 torch.compile 优化")
    parser.add_argument("--batch", dest="batch", action="store_true", default=None, help="启用透明批量推理流水线（覆盖 config batch.enabled）")
    parser.add_argument("--no-batch", dest="batch", action="store_false", help="禁用透明批量推理流水线（覆盖 config batch.enabled）")
    parser.add_argument("--inbound-limit", type=int, default=None, help="批量入队队列上限（覆盖 config batch.inbound_limit）")
    parser.add_argument("--batch-size", type=int, default=None, help="批量组装批次大小（覆盖 config batch.batch_size）")
    parser.add_argument("--max-assembly-length", type=float, default=None, help="单次组装参考/续写音频总时长上限秒数（覆盖 config batch.max_assembly_length）")
    parser.add_argument("--batch-timeout", type=float, default=None, help="组装超时秒数，低流量尽快 flush（覆盖 config batch.batch_timeout）")
    args, _ = parser.parse_known_args()

    # 批量推理流水线参数（命令行 > config/vox_server.yaml > 代码内置默认）
    batch_cfg = CONFIG.get("batch", {})
    batch_enabled = (args.batch if args.batch is not None
                     else batch_cfg.get("enabled", False))
    batch_inbound_limit = args.inbound_limit if args.inbound_limit is not None else batch_cfg.get("inbound_limit", batch_inbound_limit)
    batch_size = args.batch_size if args.batch_size is not None else batch_cfg.get("batch_size", batch_size)
    max_assembly_length = args.max_assembly_length if args.max_assembly_length is not None else batch_cfg.get("max_assembly_length", max_assembly_length)
    batch_timeout = args.batch_timeout if args.batch_timeout is not None else batch_cfg.get("batch_timeout", batch_timeout)

    # 解析有效值：命令行 > config > 内置默认
    host = args.host or srv_cfg.get("host", "0.0.0.0")
    port = args.port if args.port is not None else srv_cfg.get("port", 8854)
    device = args.device or srv_cfg.get("device") or None
    max_workers = args.max_workers if args.max_workers is not None else srv_cfg.get("max_workers", 2)
    gpu_concurrency = args.gpu_concurrency if args.gpu_concurrency is not None else srv_cfg.get("gpu_concurrency", 1)
    model_dir = args.model or model_cfg.get("model_dir", "models/VoxCPM2")
    optimize = (args.optimize if args.optimize is not None else model_cfg.get("optimize", True))

    # 初始化线程池
    executor = ThreadPoolExecutor(max_workers=max_workers)
    logger.info(f"线程池初始化完成，最大工作线程数: {max_workers}")

    # 初始化 GPU 并发信号量（使用 threading.Semaphore，兼容线程池执行环境）
    gpu_semaphore = threading.Semaphore(gpu_concurrency)
    logger.info(f"GPU 并发信号量初始化完成，最大并发数: {gpu_concurrency}")

    # 加载模型
    load_voxcpm_model(model_dir, device, optimize=optimize)

    # 启动透明批量推理流水线（仅 --batch 开启时）
    if batch_enabled:
        global inbound_queue, batch_queue
        inbound_queue = asyncio.Queue(maxsize=batch_inbound_limit)
        batch_queue = asyncio.Queue()
        _batch_tasks.append(asyncio.create_task(_batch_assembler()))
        _batch_tasks.append(asyncio.create_task(_batch_gpu_worker()))
        logger.info(
            f"透明批量推理已启用：入队上限={batch_inbound_limit}, 批次大小={batch_size}, "
            f"最大组装长度={max_assembly_length}s, 组装超时={batch_timeout}s"
        )

    logger.info("=" * 60)
    logger.info("配置来源: config/vox_server.yaml")
    logger.info(f"API 文档地址: http://{host}:{port}/docs")
    logger.info("=" * 60)

    yield

    # 关闭时执行
    logger.info("正在关闭服务器...")
    for _t in _batch_tasks:
        _t.cancel()
    _batch_tasks.clear()
    stop_task_dashboard()
    executor.shutdown(wait=True)
    logger.info("服务器已关闭")


# 创建 FastAPI 应用
app = FastAPI(
    title="VoxCPM API",
    description=(
        "VoxCPM TTS API — 支持声音设计、可控克隆和极致克隆\n\n"
        "**三种语音生成方式：**\n"
        "- 🎨 **声音设计 (Voice Design)**: 无需参考音频，通过描述创造声音\n"
        "- 🎛️ **可控克隆 (Controllable Cloning)**: 参考音频 + 可选风格控制\n"
        "- 🎙️ **极致克隆 (Ultimate Cloning)**: 音频续写模式，完整还原声音细节\n"
    ),
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
    from voxcpm.model.voxcpm2 import VoxCPM2Model
    model_type = None
    if voxcpm_model is not None:
        model_type = "VoxCPM2" if isinstance(voxcpm_model.tts_model, VoxCPM2Model) else "VoxCPM"

    return ServerInfo(
        model_loaded=voxcpm_model is not None,
        device=str(device) if device else "unknown",
        sampling_rate=sampling_rate,
        max_workers=executor._max_workers if executor else 0,
        gpu_concurrency=gpu_semaphore._value if gpu_semaphore else 0,
        model_type=model_type,
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

    status = "healthy" if voxcpm_model is not None else "degraded"
    status_code = 200

    return JSONResponse(
        status_code=status_code,
        content={
            "status": status,
            "model_loaded": voxcpm_model is not None,
            "device": str(device) if device else "unknown",
            "gpu_available": gpu_available,
            "gpu_memory_used_gb": gpu_memory_used,
            "gpu_memory_total_gb": gpu_memory_total,
            "running_tasks": running_tasks,
            "pending_tasks": pending_tasks,
            "max_workers": executor._max_workers if executor else 0,
        },
    )


# ============== 声音设计接口 ==============

@app.post("/api/v1/voice/design", response_model=TaskResponse)
async def voice_design(
    background_tasks: BackgroundTasks,
    text: str = Form(..., description="要合成的文本"),
    instruct: str = Form(..., description="声音描述指令（如 'A young woman with a warm, gentle voice'）"),
    language: Optional[str] = Form(None, description="语言代码"),
    output_path: Optional[str] = Form(None, description="输出文件路径（可选）"),
    cfg_value: float = Form(INF.get("cfg_value", 2.0), description="CFG 引导强度（1.0-5.0）"),
    inference_timesteps: int = Form(INF.get("inference_timesteps", 10), description="LocDiT 流匹配迭代步数"),
    normalize: bool = Form(INF.get("normalize", False), description="是否启用文本规范化"),
    denoise: bool = Form(INF.get("denoise", False), description="是否降噪（声音设计模式下通常为 False）"),
    speed: Optional[float] = Form(None, description="语速因子（>1 加快，<1 放慢，映射为语速提示词）"),
):
    """
    🎨 声音设计接口

    无需参考音频。通过描述性指令设计声音特征，VoxCPM 会从零创造声音。

    **支持的描述维度：**
    - 性别: male/female (男/女)
    - 年龄: child, teenager, young adult, middle-aged, elderly
    - 音调: low pitch, moderate pitch, high pitch
    - 情绪: happy, sad, angry, calm, excited
    - 风格: whisper, fast-paced, gentle, dramatic
    - 口音/方言: american accent, british accent, 粤语, 四川话 等

    **示例 instruct：**
    - "A young girl with a soft, sweet voice. Speaks slowly with a melancholic tone."
    - "年轻女性，温柔甜美"
    - "暴躁的中年男声，语速快，充满无奈和愤怒"
    """
    if voxcpm_model is None:
        raise HTTPException(status_code=503, detail="模型未加载")

    # 创建任务
    task_id = str(uuid.uuid4())
    task = TaskInfo(
        task_id=task_id,
        task_type="design",
        status="pending",
        message="声音设计任务已创建，等待处理",
    )
    tasks[task_id] = task
    print_task_created(task, {
        "文本": text[:50] + ("..." if len(text) > 50 else ""),
        "指令": instruct,
        "CFG": cfg_value,
        "步数": inference_timesteps,
        "输出路径": output_path or "默认",
    })

    # 创建请求对象
    request = VoiceDesignRequest(
        text=text,
        instruct=instruct,
        language=language,
        output_path=output_path,
        cfg_value=cfg_value,
        inference_timesteps=inference_timesteps,
        normalize=normalize,
        denoise=denoise,
        speed=speed,
    )

    # 在线程池中执行生成
    def run_design():
        execute_voice_design(task_id, request)

    executor.submit(run_design)

    return TaskResponse(
        task_id=task_id,
        status="pending",
        message="声音设计任务已创建",
    )


# ============== 可控克隆接口 ==============

@app.post("/api/v1/voice/clone", response_model=TaskResponse)
async def voice_clone(
    background_tasks: BackgroundTasks,
    text: str = Form(..., description="要合成的文本"),
    ref_audio: Optional[UploadFile] = File(None, description="参考音频文件（支持 wav, mp3 等格式）"),
    ref_audio_path: Optional[str] = Form(None, description="参考音频文件路径（直接使用本地路径，避免网关传输）"),
    instruct: Optional[str] = Form(None, description="可选的声音风格控制指令"),
    language: Optional[str] = Form(None, description="语言代码"),
    output_path: Optional[str] = Form(None, description="输出文件路径（可选）"),
    cfg_value: float = Form(INF.get("cfg_value", 2.0), description="CFG 引导强度（1.0-5.0）"),
    inference_timesteps: int = Form(INF.get("inference_timesteps", 10), description="LocDiT 流匹配迭代步数"),
    normalize: bool = Form(INF.get("normalize", False), description="是否启用文本规范化"),
    denoise: bool = Form(INF.get("denoise", True), description="是否对参考音频降噪增强"),
    speed: Optional[float] = Form(None, description="语速因子（>1 加快，<1 放慢，映射为语速提示词）"),
):
    """
    🎛️ 可控克隆接口

    克隆参考音频的声音特征来合成目标文本。
    支持两种方式传入参考音频：
    - **文件上传**: 通过 ref_audio 字段上传音频文件
    - **直接路径**: 通过 ref_audio_path 字段传入本地音频文件路径（推荐本地调用，避免网关传输开销）

    两种方式二选一，同时提供时优先使用 ref_audio_path。
    可选地通过 instruct 指令控制情绪、语速、风格等表达方式。

    **示例 instruct：**
    - "Excited and fast-paced"
    - "温柔地，放慢语速"
    - "用愤怒的语气"
    """
    if voxcpm_model is None:
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
        message="可控克隆任务已创建，等待处理",
        is_uploaded_ref=is_uploaded,
    )
    tasks[task_id] = task
    print_task_created(task, {
        "文本": text[:50] + ("..." if len(text) > 50 else ""),
        "参考音频": ref_audio_path or (ref_audio.filename if ref_audio else ""),
        "指令": instruct or "-",
        "CFG": cfg_value,
        "步数": inference_timesteps,
        "降噪": "是" if denoise else "否",
    })

    # 创建请求对象
    request = VoiceCloneRequest(
        text=text,
        instruct=instruct,
        language=language,
        output_path=output_path,
        cfg_value=cfg_value,
        inference_timesteps=inference_timesteps,
        normalize=normalize,
        denoise=denoise,
        speed=speed,
    )

    # 在线程池中执行生成
    def run_clone():
        execute_voice_clone(task_id, request, final_ref_audio_path)

    executor.submit(run_clone)

    return TaskResponse(
        task_id=task_id,
        status="pending",
        message="可控克隆任务已创建",
    )


# ============== 极致克隆接口 ==============

@app.post("/api/v1/voice/ultimate_clone", response_model=TaskResponse)
async def voice_ultimate_clone(
    background_tasks: BackgroundTasks,
    text: str = Form(..., description="要续写的文本"),
    ref_audio: Optional[UploadFile] = File(None, description="参考音频文件"),
    ref_audio_path: Optional[str] = Form(None, description="参考音频文件路径（直接使用本地路径，避免网关传输）"),
    prompt_text: Optional[str] = Form(None, description="参考音频的文本内容（不提供则自动 ASR 识别）"),
    language: Optional[str] = Form(None, description="语言代码"),
    output_path: Optional[str] = Form(None, description="输出文件路径（可选）"),
    cfg_value: float = Form(INF.get("cfg_value", 2.0), description="CFG 引导强度（1.0-5.0）"),
    inference_timesteps: int = Form(INF.get("inference_timesteps", 10), description="LocDiT 流匹配迭代步数"),
    normalize: bool = Form(INF.get("normalize", False), description="是否启用文本规范化"),
    denoise: bool = Form(INF.get("denoise", True), description="是否对参考音频降噪增强"),
    speed: Optional[float] = Form(None, description="语速因子（>1 加快，<1 放慢，映射为语速提示词）"),
):
    """
    🎙️ 极致克隆接口 — 音频续写模式

    模型将参考音频视为已说出的前文，以音频续写的方式完整还原参考音频中的所有声音细节。
    需要提供参考音频的文本内容（可自动 ASR 识别）。

    支持两种方式传入参考音频：
    - **文件上传**: 通过 ref_audio 字段上传音频文件
    - **直接路径**: 通过 ref_audio_path 字段传入本地音频文件路径（推荐本地调用，避免网关传输开销）

    两种方式二选一，同时提供时优先使用 ref_audio_path。

    **注意：** 此模式不支持 Control Instruction，模型会尽可能还原参考音频的所有声音特征。
    """
    if voxcpm_model is None:
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
        task_type="ultimate_clone",
        status="pending",
        message="极致克隆任务已创建，等待处理",
        is_uploaded_ref=is_uploaded,
    )
    tasks[task_id] = task
    print_task_created(task, {
        "文本": text[:50] + ("..." if len(text) > 50 else ""),
        "参考音频": ref_audio_path or (ref_audio.filename if ref_audio else ""),
        "提示文本": (prompt_text[:30] + "...") if prompt_text and len(prompt_text) > 30 else (prompt_text or "自动识别"),
        "CFG": cfg_value,
        "步数": inference_timesteps,
    })

    # 创建请求对象
    request = UltimateCloneRequest(
        text=text,
        prompt_text=prompt_text,
        language=language,
        output_path=output_path,
        cfg_value=cfg_value,
        inference_timesteps=inference_timesteps,
        normalize=normalize,
        denoise=denoise,
        speed=speed,
    )

    # 在线程池中执行生成
    def run_ultimate_clone():
        execute_ultimate_clone(task_id, request, final_ref_audio_path)

    executor.submit(run_ultimate_clone)

    return TaskResponse(
        task_id=task_id,
        status="pending",
        message="极致克隆任务已创建",
    )


# ============== 任务管理接口 ==============

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
        ],
    }


@app.delete("/api/v1/tasks/{task_id}")
async def delete_task(task_id: str):
    """删除任务"""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="任务不存在")

    task = tasks[task_id]

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


# ============== 音频下载接口 ==============

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


# ============== SSE 实时进度 ==============

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

class BatchDesignItem(BaseModel):
    """批量声音设计项目"""
    text: str = Field(..., description="要合成的文本", min_length=1)
    instruct: str = Field(..., description="声音描述指令")
    output_name: Optional[str] = Field(None, description="输出文件名（可选）")
    speed: Optional[float] = Field(None, description="语速因子（超限自动截断）")

    @field_validator('speed', mode='before')
    @classmethod
    def clamp_speed(cls, v):
        return _clamp_speed(v)


class BatchCloneItem(BaseModel):
    """批量克隆项目"""
    text: str = Field(..., description="要合成的文本", min_length=1)
    ref_audio: str = Field(..., description="参考音频文件路径")
    instruct: Optional[str] = Field(None, description="可选的声音风格控制指令")
    output_name: Optional[str] = Field(None, description="输出文件名（可选）")
    speed: Optional[float] = Field(None, description="语速因子（超限自动截断）")

    @field_validator('speed', mode='before')
    @classmethod
    def clamp_speed(cls, v):
        return _clamp_speed(v)


class BatchUltimateCloneItem(BaseModel):
    """批量极致克隆项目"""
    text: str = Field(..., description="要续写的文本", min_length=1)
    ref_audio: str = Field(..., description="参考音频文件路径")
    prompt_text: Optional[str] = Field(None, description="参考音频文本（不提供则自动识别）")
    output_name: Optional[str] = Field(None, description="输出文件名（可选）")
    speed: Optional[float] = Field(None, description="语速因子（超限自动截断）")

    @field_validator('speed', mode='before')
    @classmethod
    def clamp_speed(cls, v):
        return _clamp_speed(v)


class BatchTaskRequest(BaseModel):
    """批量任务请求"""
    items: List[Union[BatchDesignItem, BatchCloneItem, BatchUltimateCloneItem]] = Field(
        ..., description="批量任务列表", min_length=1
    )
    cfg_value: float = Field(INF.get("cfg_value", 2.0), ge=1.0, le=5.0, description="CFG 引导强度")
    inference_timesteps: int = Field(INF.get("inference_timesteps", 10), ge=1, le=50, description="LocDiT 流匹配迭代步数")
    normalize: bool = Field(INF.get("normalize", False), description="是否启用文本规范化")
    denoise: bool = Field(INF.get("denoise", True), description="是否对参考音频降噪增强")
    max_workers: int = Field(4, ge=1, le=16, description="最大并行工作线程数")


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
    status: str
    tasks: List[Dict[str, Any]]
    created_at: float
    completed_at: Optional[float] = None
    rtf_avg: Optional[float] = None
    duration_total: Optional[float] = None


@app.post("/api/v1/voice/batch", response_model=BatchTaskResponse)
async def batch_process(request: BatchTaskRequest):
    """
    批量处理接口

    一次性提交多个合成任务，支持混合批量（设计、克隆、极致克隆可同时提交）。
    使用多线程并行处理，提高处理效率。

    **示例请求：**
    ```json
    {
      "items": [
        {
          "text": "Hello world",
          "instruct": "A warm young woman"
        },
        {
          "text": "这是测试文本",
          "ref_audio": "/path/to/audio.wav",
          "instruct": "温柔地"
        }
      ],
      "max_workers": 4
    }
    ```
    """
    if voxcpm_model is None:
        raise HTTPException(status_code=503, detail="模型未加载")

    batch_id = str(uuid.uuid4())
    task_ids = []

    for idx, item in enumerate(request.items):
        task_id = f"{batch_id}_{idx:04d}"
        task_ids.append(task_id)

        if isinstance(item, BatchDesignItem):
            task_type = "batch_design"
        elif isinstance(item, BatchUltimateCloneItem):
            task_type = "batch_ultimate_clone"
        else:
            task_type = "batch_clone"

        task = TaskInfo(
            task_id=task_id,
            task_type=task_type,
            status="pending",
            message=f"批量任务 {idx + 1}/{len(request.items)} 等待处理",
        )
        tasks[task_id] = task

    # 执行批量任务
    def run_batch():
        execute_batch_tasks(batch_id, request)

    executor.submit(run_batch)

    return BatchTaskResponse(
        batch_id=batch_id,
        status="pending",
        message=f"批量任务已创建，共 {len(request.items)} 个项目",
        total_items=len(request.items),
        task_ids=task_ids,
    )


def execute_batch_tasks(batch_id: str, request: BatchTaskRequest):
    """执行批量任务"""
    batch_output_dir = Path("outputs") / batch_id
    batch_output_dir.mkdir(parents=True, exist_ok=True)

    for idx, item in enumerate(request.items):
        task_id = f"{batch_id}_{idx:04d}"
        task = tasks[task_id]
        task.status = "running"
        task.started_at = time.time()
        start_task_progress(task_id, f"批量 {idx+1}/{len(request.items)} {task_id[:12]}")
        update_task_progress(task_id, 10, f"正在处理 {idx + 1}/{len(request.items)}...")

        try:
            inference_start = time.time()

            acquired = gpu_semaphore is not None
            if acquired:
                gpu_semaphore.acquire()
            try:
                if isinstance(item, BatchDesignItem):
                    final_text = build_generate_text(item.text, item.instruct, speed=item.speed)
                    audio = voxcpm_model.generate(
                        text=final_text,
                        cfg_value=request.cfg_value,
                        inference_timesteps=request.inference_timesteps,
                        normalize=request.normalize,
                        denoise=request.denoise,
                    )
                elif isinstance(item, BatchUltimateCloneItem):
                    use_clone_mode = item.speed is not None and item.speed != 1.0
                    if use_clone_mode:
                        final_text = build_generate_text(item.text, speed=item.speed)
                        audio = voxcpm_model.generate(
                            text=final_text,
                            reference_wav_path=item.ref_audio,
                            cfg_value=request.cfg_value,
                            inference_timesteps=request.inference_timesteps,
                            normalize=request.normalize,
                            denoise=request.denoise,
                        )
                    else:
                        prompt_text = item.prompt_text
                        if not prompt_text or not prompt_text.strip():
                            prompt_text = recognize_audio_text(item.ref_audio)
                        audio = voxcpm_model.generate(
                            text=item.text,
                            prompt_wav_path=item.ref_audio,
                            prompt_text=prompt_text.strip(),
                            cfg_value=request.cfg_value,
                            inference_timesteps=request.inference_timesteps,
                            normalize=request.normalize,
                            denoise=request.denoise,
                        )
                else:
                    final_text = build_generate_text(item.text, item.instruct, speed=item.speed)
                    audio = voxcpm_model.generate(
                        text=final_text,
                        reference_wav_path=item.ref_audio,
                        cfg_value=request.cfg_value,
                        inference_timesteps=request.inference_timesteps,
                        normalize=request.normalize,
                        denoise=request.denoise,
                    )
            finally:
                if acquired:
                    gpu_semaphore.release()

            inference_time = time.time() - inference_start

            # 确定输出文件名
            if item.output_name:
                output_name = item.output_name
                if not output_name.endswith(".wav"):
                    output_name += ".wav"
            else:
                output_name = f"{idx:04d}.wav"

            output_path = batch_output_dir / output_name
            save_audio(audio, output_path, sampling_rate)

            audio_duration = len(audio) / sampling_rate
            rtf = calculate_rtf(audio_duration, inference_time)

            task.status = "completed"
            task.progress = 100.0
            task.message = f"任务 {idx + 1}/{len(request.items)} 生成完成"
            task.output_path = str(output_path)
            task.audio_duration = audio_duration
            task.inference_time = inference_time
            task.rtf = rtf
            task.completed_at = time.time()

            logger.info(f"批量任务 {task_id} 完成: RTF={rtf:.4f}, 时长={audio_duration:.2f}s")
            stop_task_progress(task_id)

        except Exception as e:
            logger.error(f"批量任务 {task_id} 失败: {e}")
            task.status = "failed"
            task.error = str(e)
            task.message = f"任务 {idx + 1} 失败: {e}"
            task.completed_at = time.time()
            stop_task_progress(task_id)


@app.get("/api/v1/voice/batch/{batch_id}", response_model=BatchTaskStatusResponse)
async def get_batch_status(batch_id: str):
    """获取批量任务状态"""
    batch_tasks = [t for t in tasks.values() if t.task_id.startswith(batch_id)]

    if not batch_tasks:
        raise HTTPException(status_code=404, detail="批量任务不存在")

    completed = sum(1 for t in batch_tasks if t.status == "completed")
    failed = sum(1 for t in batch_tasks if t.status == "failed")
    running = sum(1 for t in batch_tasks if t.status == "running")
    pending = sum(1 for t in batch_tasks if t.status == "pending")
    total = len(batch_tasks)

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

    created_at = min(t.created_at for t in batch_tasks)
    completed_at = None

    if batch_status in ("completed", "failed", "partial"):
        completed_times = [t.completed_at for t in batch_tasks if t.completed_at]
        if completed_times:
            completed_at = max(completed_times)

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
    """批量下载音频文件（打包为 ZIP）"""
    batch_tasks = [t for t in tasks.values() if t.task_id.startswith(batch_id)]

    if not batch_tasks:
        raise HTTPException(status_code=404, detail="批量任务不存在")

    completed_tasks = [t for t in batch_tasks if t.status == "completed" and t.output_path]

    if not completed_tasks:
        raise HTTPException(status_code=400, detail="没有可下载的音频文件")

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for task in completed_tasks:
            if os.path.exists(task.output_path):
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
    """删除批量任务"""
    batch_tasks = [t for t in tasks.values() if t.task_id.startswith(batch_id)]

    if not batch_tasks:
        raise HTTPException(status_code=404, detail="批量任务不存在")

    running_tasks = [t for t in batch_tasks if t.status == "running"]
    if running_tasks:
        raise HTTPException(
            status_code=400,
            detail=f"批量任务中有 {len(running_tasks)} 个任务正在运行，无法删除",
        )

    deleted_count = 0
    for task in batch_tasks:
        if task.output_path and os.path.exists(task.output_path):
            try:
                os.remove(task.output_path)
            except Exception as e:
                logger.warning(f"删除文件失败: {e}")

        if task.task_id in tasks:
            del tasks[task.task_id]
            deleted_count += 1

    batch_output_dir = Path("outputs") / batch_id
    if batch_output_dir.exists():
        try:
            shutil.rmtree(batch_output_dir)
        except Exception as e:
            logger.warning(f"删除批量任务目录失败: {e}")

    return {
        "message": f"批量任务已删除，共删除 {deleted_count} 个子任务",
        "batch_id": batch_id,
        "deleted_count": deleted_count,
    }


# ============== 批量合成（张量级前向加速，独立于单链路） ==============

class GenerateBatchItem(BaseModel):
    text: str
    prompt_wav_path: Optional[str] = None
    prompt_text: Optional[str] = None
    reference_wav_path: Optional[str] = None


class GenerateBatchRequest(BaseModel):
    items: List[GenerateBatchItem]
    cfg_value: float = 2.0
    inference_timesteps: int = 10
    min_len: int = 2
    max_len: int = 4096
    normalize: bool = False
    denoise: bool = False


class GenerateBatchResult(BaseModel):
    index: int
    audio_url: str
    duration: float
    rtf: float
    text: str


class GenerateBatchResponse(BaseModel):
    results: List[GenerateBatchResult]
    total_rtf: float
    batch_size: int


@app.post("/api/v1/voice/generate_batch", response_model=GenerateBatchResponse)
def api_generate_batch(request: GenerateBatchRequest):
    """批量合成端点：多条文本/不同参考音频一次性张量级前向，返回各条音频下载地址。

    在 ``gpu_semaphore`` 保护下执行单次批量推理，不干扰既有单链路接口。
    """
    if voxcpm_model is None:
        raise HTTPException(status_code=503, detail="模型尚未加载")
    if not request.items:
        raise HTTPException(status_code=400, detail="items 不能为空")

    n = len(request.items)
    texts = [it.text for it in request.items]
    pwp = [it.prompt_wav_path for it in request.items]
    ptx = [it.prompt_text for it in request.items]
    rwp = [it.reference_wav_path for it in request.items]

    out_dir = Path("outputs") / f"batch_{uuid.uuid4().hex[:8]}"
    out_dir.mkdir(parents=True, exist_ok=True)

    inference_start = time.time()
    acquired = gpu_semaphore is not None
    if acquired:
        gpu_semaphore.acquire()
    try:
        audios = voxcpm_model.generate_batch(
            texts=texts,
            prompt_wav_paths=pwp,
            prompt_texts=ptx,
            reference_wav_paths=rwp,
            cfg_value=request.cfg_value,
            inference_timesteps=request.inference_timesteps,
            min_len=request.min_len,
            max_len=request.max_len,
            normalize=request.normalize,
            denoise=request.denoise,
        )
    finally:
        if acquired:
            gpu_semaphore.release()

    inference_time = time.time() - inference_start
    results = []
    total_duration = 0.0
    for idx, (it, audio) in enumerate(zip(request.items, audios)):
        op = out_dir / f"{idx}.wav"
        save_audio(audio, op, sampling_rate)
        duration = float(len(audio)) / sampling_rate
        total_duration += duration
        results.append(
            GenerateBatchResult(
                index=idx,
                audio_url=f"/api/v1/voice/batch_files/{out_dir.name}/{idx}.wav",
                duration=round(duration, 3),
                rtf=round(calculate_rtf(duration, inference_time / n), 4),
                text=it.text[:80],
            )
        )
    return GenerateBatchResponse(
        results=results,
        total_rtf=round(calculate_rtf(total_duration, inference_time), 4),
        batch_size=n,
    )


@app.get("/api/v1/voice/batch_files/{batch_id}/{idx}.wav")
def api_batch_file(batch_id: str, idx: int):
    p = Path("outputs") / batch_id / f"{idx}.wav"
    if not p.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(str(p), media_type="audio/wav")


# ============== 透明批量推理（CPU 组装 / GPU 消费 / CPU 拆分路由） ==============

@dataclass
class _BatchTask:
    future: "asyncio.Future"
    params: dict
    cost: float


def _config_sig(params: dict):
    """同一批次内共享 cfg/timesteps/normalize/denoise，避免不同设置被强行合批。"""
    return (
        round(float(params.get("cfg_value", 2.0)), 2),
        int(params.get("inference_timesteps", 10)),
        bool(params.get("normalize", False)),
        bool(params.get("denoise", False)),
    )


def _estimate_audio_seconds(params: dict) -> float:
    """粗略估算本条请求占用的参考/续写音频时长（秒），用于组装长度预算。"""
    total = 0.0
    for key in ("prompt_wav_path", "reference_wav_path"):
        p = params.get(key)
        if not p:
            continue
        try:
            import soundfile as sf

            info = sf.info(p)
            total += info.frames / max(info.samplerate, 1)
        except Exception:
            pass
    return total


async def _enqueue_generate(params: dict) -> "np.ndarray":
    """将单条请求入队，等待 CPU 组装 + GPU 批量推理完成后返回音频。"""
    loop = asyncio.get_running_loop()
    fut: "asyncio.Future" = loop.create_future()
    task = _BatchTask(future=fut, params=params, cost=_estimate_audio_seconds(params))
    await inbound_queue.put(task)  # 队列满时自然形成背压
    return await fut


async def _batch_assembler():
    """CPU：从入队队列取请求，按配置签名与长度预算动态组装成批次，放入组装队列。"""
    while True:
        try:
            task = await inbound_queue.get()
            sig = _config_sig(task.params)
            batch = [task]
            cost_sum = task.cost
            try:
                while len(batch) < batch_size:
                    try:
                        nxt = await asyncio.wait_for(inbound_queue.get(), timeout=batch_timeout)
                    except asyncio.TimeoutError:
                        break
                    nsig = _config_sig(nxt.params)
                    if nsig != sig or (cost_sum + nxt.cost) > max_assembly_length:
                        # 配置不一致或超出长度预算：放回队列，结束当前批次
                        await inbound_queue.put(nxt)
                        break
                    batch.append(nxt)
                    cost_sum += nxt.cost
                    sig = nsig
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("批量组装器异常")
                continue
            await batch_queue.put(batch)
        except asyncio.CancelledError:
            raise


async def _batch_gpu_worker():
    """GPU：消费组装队列跑 generate_batch；产出的片段由 CPU 拆分路由回各请求。"""
    loop = asyncio.get_running_loop()
    while True:
        try:
            batch = await batch_queue.get()
        except asyncio.CancelledError:
            raise
        try:
            audios = await loop.run_in_executor(None, _run_batch_sync, batch)
        except Exception as e:  # 推理失败：将异常分发到各请求
            logger.exception("批量推理失败")
            for t in batch:
                if not t.future.done():
                    t.future.set_exception(e)
            continue
        # CPU 拆分与最终输出：按序把每段音频路由回对应请求的 Future
        for t, audio in zip(batch, audios):
            if not t.future.done():
                t.future.set_result(audio)


def _run_batch_sync(batch: list) -> "list":
    """在线程中同步执行批量推理（持有 GPU 信号量）。"""
    texts = [t.params["text"] for t in batch]
    pwp = [t.params.get("prompt_wav_path") for t in batch]
    ptx = [t.params.get("prompt_text") for t in batch]
    rwp = [t.params.get("reference_wav_path") for t in batch]
    p0 = batch[0].params
    cfg = p0.get("cfg_value", 2.0)
    timesteps = p0.get("inference_timesteps", 10)
    normalize = p0.get("normalize", False)
    denoise = p0.get("denoise", False)
    acquired = gpu_semaphore is not None
    if acquired:
        gpu_semaphore.acquire()
    try:
        gen_batch = getattr(voxcpm_model, "generate_batch", None)
        if gen_batch is not None:
            try:
                return gen_batch(
                    texts=texts,
                    prompt_wav_paths=pwp,
                    prompt_texts=ptx,
                    reference_wav_paths=rwp,
                    cfg_value=cfg,
                    inference_timesteps=timesteps,
                    normalize=normalize,
                    denoise=denoise,
                )
            except Exception as e:  # 兼容：部分模型版本的批量前向在克隆/参考模式下存在形状 bug
                logger.warning(
                    f"generate_batch 批量前向失败({type(e).__name__})，回退逐条 generate: {e}"
                )
        # 防御性回退：逐条 generate（保证可用；克隆/参考模式下模型批量前向在本版本不稳定）
        return [
            voxcpm_model.generate(
                text=t,
                prompt_wav_path=p,
                prompt_text=pt,
                reference_wav_path=r,
                cfg_value=cfg,
                inference_timesteps=timesteps,
                normalize=normalize,
                denoise=denoise,
            )
            for t, p, pt, r in zip(texts, pwp, ptx, rwp)
        ]
    finally:
        if acquired:
            gpu_semaphore.release()


def _run_single_sync(params: dict) -> "np.ndarray":
    """未开启批量时的单条直跑（与原单链路行为一致）。"""
    acquired = gpu_semaphore is not None
    if acquired:
        gpu_semaphore.acquire()
    try:
        return voxcpm_model.generate(
            text=params["text"],
            prompt_wav_path=params.get("prompt_wav_path"),
            prompt_text=params.get("prompt_text"),
            reference_wav_path=params.get("reference_wav_path"),
            cfg_value=params.get("cfg_value", 2.0),
            inference_timesteps=params.get("inference_timesteps", 10),
            normalize=params.get("normalize", False),
            denoise=params.get("denoise", False),
        )
    finally:
        if acquired:
            gpu_semaphore.release()


class GenerateAutoRequest(BaseModel):
    """透明批量合成请求：调用方只发单条，服务端负责组装批次。"""
    text: str = Field(..., description="要合成的文本", min_length=1)
    prompt_wav_path: Optional[str] = Field(None, description="续写模式参考音频路径")
    prompt_text: Optional[str] = Field(None, description="续写模式参考文本（prompt）")
    reference_wav_path: Optional[str] = Field(None, description="克隆模式参考音频路径")
    cfg_value: float = Field(INF.get("cfg_value", 2.0), ge=1.0, le=5.0, description="CFG 引导强度")
    inference_timesteps: int = Field(INF.get("inference_timesteps", 10), ge=1, le=50, description="LocDiT 迭代步数")
    normalize: bool = Field(INF.get("normalize", False), description="是否启用文本规范化")
    denoise: bool = Field(INF.get("denoise", False), description="是否对参考音频降噪")


@app.post("/api/v1/voice/generate_auto")
async def api_generate_auto(request: GenerateAutoRequest):
    """透明批量合成端点：单条请求入队，由服务端 CPU 动态组装批次、GPU 消费产出。

    开启 --batch 时走批量流水线（与 generate_batch 同链路，但调用方无需组装请求）；
    未开启时退化为单条直跑，行为与原单链路一致。
    """
    if voxcpm_model is None:
        raise HTTPException(status_code=503, detail="模型尚未加载")

    params = dict(
        text=request.text,
        prompt_wav_path=request.prompt_wav_path,
        prompt_text=request.prompt_text,
        reference_wav_path=request.reference_wav_path,
        cfg_value=request.cfg_value,
        inference_timesteps=request.inference_timesteps,
        normalize=request.normalize,
        denoise=request.denoise,
    )

    if batch_enabled:
        audio = await _enqueue_generate(params)
    else:
        loop = asyncio.get_running_loop()
        audio = await loop.run_in_executor(None, _run_single_sync, params)

    out_dir = Path("outputs") / f"auto_{uuid.uuid4().hex[:8]}"
    out_dir.mkdir(parents=True, exist_ok=True)
    op = out_dir / "0.wav"
    save_audio(audio, op, sampling_rate)
    duration = float(len(audio)) / sampling_rate
    return {
        "audio_url": f"/api/v1/voice/batch_files/{out_dir.name}/0.wav",
        "duration": round(duration, 3),
        "batch_mode": batch_enabled,
    }


# ============== 主函数 ==============

if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="VoxCPM API Server")
    parser.add_argument("--host", default=None, help="服务器地址（默认读 config）")
    parser.add_argument("--port", type=int, default=None, help="服务器端口（默认读 config）")
    parser.add_argument("--device", default=None, help="计算设备 (cuda/cpu/mps)（默认读 config）")
    parser.add_argument("--model", default=None, help="模型路径（默认读 config）")
    parser.add_argument("--max-workers", type=int, default=None, help="最大工作线程数（默认读 config）")
    parser.add_argument("--gpu-concurrency", type=int, default=None, help="GPU 并发推理数量（默认读 config）")
    parser.add_argument("--optimize", dest="optimize", action="store_true", default=None, help="启用 torch.compile 优化（默认读 config）")
    parser.add_argument("--no-optimize", dest="optimize", action="store_false", help="禁用 torch.compile 优化")
    parser.add_argument("--batch", dest="batch", action="store_true", default=None, help="启用透明批量推理流水线（覆盖 config batch.enabled）")
    parser.add_argument("--no-batch", dest="batch", action="store_false", help="禁用透明批量推理流水线（覆盖 config batch.enabled）")
    parser.add_argument("--inbound-limit", type=int, default=None, help="批量入队队列上限（覆盖 config batch.inbound_limit）")
    parser.add_argument("--batch-size", type=int, default=None, help="批量组装批次大小（覆盖 config batch.batch_size）")
    parser.add_argument("--max-assembly-length", type=float, default=None, help="单次组装参考/续写音频总时长上限秒数（覆盖 config batch.max_assembly_length）")
    parser.add_argument("--batch-timeout", type=float, default=None, help="组装超时秒数，低流量尽快 flush（覆盖 config batch.batch_timeout）")

    args = parser.parse_args()

    srv_cfg = CONFIG.get("server", {})
    host = args.host or srv_cfg.get("host", "0.0.0.0")
    port = args.port if args.port is not None else srv_cfg.get("port", 8854)

    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=False,
        workers=1,
        access_log=False,
    )
