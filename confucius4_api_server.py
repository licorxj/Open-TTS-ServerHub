#!/usr/bin/env python3
"""
Confucius4-TTS FastAPI Server

提供 TTS API 接口，支持：
- 声音克隆 (Voice Cloning) - 需要参考音频
- 异步多线程处理
- 实时进度推送 (WebSocket/SSE)
- RTF 速率显示

Usage:
    python confucius4_api_server.py [--host 0.0.0.0] [--port 8857] [--device cuda]
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

# 设置项目路径
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFUCIUS4_ROOT = os.path.join(PROJECT_ROOT, "Confucius4-TTS")
MODELS_DIR = os.path.join(PROJECT_ROOT, "models", "Confucius4")
PRETRAINED_DIR = os.path.join(PROJECT_ROOT, "models", "pretrained")

# 将 Confucius4-TTS 加入 Python 路径
sys.path.insert(0, CONFUCIUS4_ROOT)

# ============== 专有依赖补丁包优先加载 ==============
# Confucius4-TTS 的 T2S (Qwen3/GPT2) 生成代码针对 transformers 4.x 设计，
# 在 py312env 默认的 transformers 5.3.0 下 generate 行为异常，会生成极短静音。
# 优先加载 packages/index25 内的 transformers 4.52.1 / tokenizers 0.21.4 /
# huggingface_hub 0.36.2，与 IndexTTS-2.5 共用同一套已验证补丁依赖。
PATCH_PKG = os.path.join(PROJECT_ROOT, "packages", "index25")
if os.path.isdir(PATCH_PKG):
    sys.path.insert(0, PATCH_PKG)

# ============== Monkey-patch hf_hub_download ==============
# 让模型权重从本地加载，避免重复下载
import huggingface_hub

_original_hf_hub_download = huggingface_hub.hf_hub_download

# 本地模型路径映射
# BigVGAN 声码器：配置文件写死的是 HF repo id（nvidia/bigvgan_v2_22khz_80band_256x），
# 离线环境下 hf_hub_download 会联网探活并崩溃。这里映射到本地权重目录
# （models/index2/hf_cache/bigvgan 内含 config.json + bigvgan_generator.pt，版本一致），
# 使 hf_hub_download 直接返回本地文件，服务可离线加载。
_BIGVGAN_LOCAL = os.path.normpath(os.path.join(MODELS_DIR, "..", "index2", "hf_cache", "bigvgan"))
_LOCAL_MODEL_MAP = {
    "netease-youdao/Confucius4-TTS": MODELS_DIR,
    "funasr/campplus": os.path.join(PRETRAINED_DIR, "campplus"),
    "nvidia/bigvgan_v2_22khz_80band_256x": _BIGVGAN_LOCAL,
}

def _patched_hf_hub_download(repo_id, filename, **kwargs):
    """优先从本地 models/ 目录加载模型文件"""
    # 检查是否有该 repo 的本地映射
    for repo_prefix, local_dir in _LOCAL_MODEL_MAP.items():
        if repo_id == repo_prefix or repo_id.endswith(repo_prefix):
            local_path = os.path.join(local_dir, filename)
            if os.path.exists(local_path):
                logging.getLogger(__name__).info(f"使用本地模型文件: {local_path}")
                return local_path
    return _original_hf_hub_download(repo_id, filename, **kwargs)

huggingface_hub.hf_hub_download = _patched_hf_hub_download

# ============== Monkey-patch transformers from_pretrained ==============
# 让 facebook/w2v-bert-2.0 从本地加载
_original_auto_model_from_pretrained = None

def _patch_from_pretrained(cls, pretrained_model_name_or_path, *args, **kwargs):
    """重定向本地已有的预训练模型"""
    w2v_local = os.path.join(PRETRAINED_DIR, "w2v-bert-2.0")
    if pretrained_model_name_or_path == "facebook/w2v-bert-2.0" and os.path.isdir(w2v_local):
        logging.getLogger(__name__).info(f"使用本地 w2v-bert-2.0 模型: {w2v_local}")
        pretrained_model_name_or_path = w2v_local
    return cls._original_from_pretrained(pretrained_model_name_or_path, *args, **kwargs)


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

from confuciustts.cli.inference import ConfuciusTTS

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
logging.getLogger("uvicorn.access").disabled = True

# ============== 服务器专用配置（推理 + 性能参数统一在此） ==============
import yaml

SERVER_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "confucius4_server.yaml")

DEFAULT_SERVER_CONFIG = {
    "server": {
        "host": "0.0.0.0",
        "port": 8857,
        "device": None,
        "max_workers": 1,
        "use_gpu_lock": True,
    },
    "inference": {
        "temperature": 0.8,
        "top_p": 0.8,
        "top_k": 30,
        "num_beams": 1,
        "repetition_penalty": 10.0,
        "max_length": 1520,
        "n_timesteps": 15,
        "inference_cfg_rate": 0.7,
        "max_text_tokens_per_segment": 80,
        "cross_fade_duration": 0.3,
        "edge_fade_duration": 0.1,
        "edge_pad_duration": 0.1,
    },
    "optimization": {
        "bigvgan_cuda_kernel": True,
        "enable_tf32": True,
        "cudnn_benchmark": True,
        "ref_audio_cache": True,
    },
    "logging": {
        "print_rtf": True,
    },
}


def load_server_config():
    """加载 config/confucius4_server.yaml，缺失时回退到默认配置。"""
    cfg = DEFAULT_SERVER_CONFIG
    if os.path.isfile(SERVER_CONFIG_PATH):
        try:
            with open(SERVER_CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = yaml.safe_load(f) or {}
            for section, values in user_cfg.items():
                if isinstance(values, dict) and section in cfg:
                    cfg[section].update(values)
                else:
                    cfg[section] = values
            logger.info(f"已加载服务器配置: {SERVER_CONFIG_PATH}")
        except Exception as e:
            logger.warning(f"加载服务器配置失败，使用默认配置: {e}")
    else:
        logger.warning(f"未找到配置文件 {SERVER_CONFIG_PATH}，使用默认配置")
    return cfg


CONFIG = load_server_config()

console = Console()

# 禁用 tqdm 输出
os.environ["TQDM_DISABLE"] = "1"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
os.environ["HF_DATASETS_DISABLE_PROGRESS_BARS"] = "1"

# 支持的语言
LANGUAGES = ("zh", "en", "ja", "ko", "de", "fr", "th", "id", "vi", "es", "pt", "it", "ru", "ms")

# ============== 任务管理 ==============

TASK_TYPE_LABELS = {
    "clone": "🎵 声音克隆",
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
        f"[任务 {task.task_id[:12]}] {event} | 类型={TASK_TYPE_LABELS.get(task.task_type, task.task_type)}"
        f" | 状态={_task_status_label(task.status)}"
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


# 全局变量
model = None
device = None
sampling_rate = 22050

# 任务管理器
tasks: Dict[str, "TaskInfo"] = {}
executor = None

# GPU 推理全局锁：保证同一时刻仅一个任务占用 GPU（避免显存争抢与 os.chdir 线程竞争）
gpu_lock = threading.Lock()


@dataclass
class TaskInfo:
    """任务信息"""
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


# ============== Pydantic 模型 ==============

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
    model_type: str = "Confucius4-TTS"
    supported_languages: List[str]


# ============== 辅助函数 ==============

def get_best_device():
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def load_model(device_str: str):
    """加载 Confucius4-TTS 模型"""
    global model, device, sampling_rate

    device = device_str if device_str else get_best_device()

    # 记录实际生效的 transformers 版本（补丁包 4.52.1 应优先于 py312env 的 5.x）
    import transformers as _tf
    logger.info(f"transformers 版本: {_tf.__version__} (来自 {_tf.__file__})")

    logger.info(f"正在加载 Confucius4-TTS 模型...")
    logger.info(f"设备: {device}")
    logger.info(f"模型目录: {MODELS_DIR}")
    logger.info(f"预训练模型目录: {PRETRAINED_DIR}")

    # 确保 Amphion 符号链接存在
    amphion_external = os.path.join(PRETRAINED_DIR, "Amphion")
    amphion_target = os.path.join(CONFUCIUS4_ROOT, "external", "Amphion")
    if os.path.isdir(amphion_external) and not os.path.exists(amphion_target):
        try:
            import subprocess
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", amphion_target, amphion_external],
                check=True, capture_output=True
            )
            logger.info(f"已创建 Amphion 符号链接: {amphion_target} -> {amphion_external}")
        except Exception as e:
            logger.warning(f"创建 Amphion 符号链接失败: {e}")

    # Monkey-patch transformers from_pretrained 以使用本地 w2v-bert-2.0
    try:
        from transformers import AutoModel, Wav2Vec2BertModel, SeamlessM4TFeatureExtractor
        w2v_local = os.path.join(PRETRAINED_DIR, "w2v-bert-2.0")

        if os.path.isdir(w2v_local):
            for cls in [AutoModel, Wav2Vec2BertModel]:
                if not hasattr(cls.from_pretrained, '_patched'):
                    _orig_fp = cls.from_pretrained
                    def _make_patch(orig):
                        def _local_fp(name_or_path, *args, **kwargs):
                            if name_or_path == "facebook/w2v-bert-2.0":
                                logger.info(f"使用本地 w2v-bert-2.0 模型: {w2v_local}")
                                name_or_path = w2v_local
                            return orig(name_or_path, *args, **kwargs)
                        _local_fp._patched = True
                        return _local_fp
                    cls.from_pretrained = _make_patch(_orig_fp)

            # Patch SeamlessM4TFeatureExtractor
            if not hasattr(SeamlessM4TFeatureExtractor.from_pretrained, '_patched'):
                _orig_se = SeamlessM4TFeatureExtractor.from_pretrained
                def _local_se(name_or_path, *args, **kwargs):
                    if name_or_path == "facebook/w2v-bert-2.0":
                        logger.info(f"使用本地 w2v-bert-2.0 processor: {w2v_local}")
                        name_or_path = w2v_local
                    return _orig_se(name_or_path, *args, **kwargs)
                _local_se._patched = True
                SeamlessM4TFeatureExtractor.from_pretrained = _local_se

            logger.info("已应用 transformers.from_pretrained 本地化补丁 (w2v-bert-2.0)")
    except Exception as e:
        logger.warning(f"无法应用 transformers 补丁: {e}")

    # 设置工作目录到 Confucius4-TTS
    original_dir = os.getcwd()
    os.chdir(CONFUCIUS4_ROOT)

    # Monkey-patch Text2SemanticConfig 兼容新版 transformers (添加 num_hidden_layers)
    try:
        from confuciustts.llm.llm import Text2SemanticConfig
        _orig_t2s_init = Text2SemanticConfig.__init__
        def _compat_t2s_init(self, *args, **kwargs):
            _orig_t2s_init(self, *args, **kwargs)
            # transformers GenerationMixin 需要 num_hidden_layers
            if not hasattr(self, 'num_hidden_layers'):
                self.num_hidden_layers = self.num_layers
            if not hasattr(self, 'hidden_size'):
                self.hidden_size = self.model_dim
            if not hasattr(self, 'num_attention_heads'):
                self.num_attention_heads = self.num_heads
        Text2SemanticConfig.__init__ = _compat_t2s_init
        logger.info("已应用 Text2SemanticConfig 兼容补丁")
    except Exception as e:
        logger.warning(f"无法应用 Text2SemanticConfig 补丁: {e}")

    # Monkey-patch Text2Semantic._reorder_cache 兼容 beam search 中的 None past_state
    try:
        from confuciustts.llm.llm import Text2Semantic
        if not hasattr(Text2Semantic._reorder_cache, '_patched'):
            def _safe_reorder_cache(past_key_values, beam_idx):
                """兼容 past_state 可能为 None 的情况"""
                reordered = []
                for layer_past in past_key_values:
                    reordered_layer = []
                    for past_state in layer_past:
                        if past_state is not None:
                            reordered_layer.append(
                                past_state.index_select(0, beam_idx.to(past_state.device))
                            )
                        else:
                            reordered_layer.append(None)
                    reordered.append(tuple(reordered_layer))
                return tuple(reordered)
            _safe_reorder_cache._patched = True
            Text2Semantic._reorder_cache = staticmethod(_safe_reorder_cache)
            logger.info("已应用 Text2Semantic._reorder_cache 兼容补丁")
    except Exception as e:
        logger.warning(f"无法应用 _reorder_cache 补丁: {e}")

    # Monkey-patch Text2Semantic 以兼容 transformers 5.x DynamicCache
    try:
        import functools as _functools
        from confuciustts.llm.llm import Text2Semantic
        from transformers.cache_utils import DynamicCache

        def _tuple_to_cache(pkv):
            """将 tuple 格式的 past_key_values 转换为 DynamicCache"""
            if pkv is None or isinstance(pkv, DynamicCache):
                return pkv
            if not isinstance(pkv, tuple):
                return pkv
            cache = DynamicCache()
            for i, layer_past in enumerate(pkv):
                if isinstance(layer_past, tuple) and len(layer_past) == 2:
                    key, value = layer_past
                    if key is not None and value is not None:
                        cache.update(key, value, i)
            return cache

        def _cache_to_tuple(pkv):
            """将 DynamicCache 转换为 tuple 格式（供原始 _reorder_cache 使用）"""
            if pkv is None or not isinstance(pkv, DynamicCache):
                return pkv
            return tuple(
                (pkv.key_cache[i], pkv.value_cache[i])
                for i in range(pkv.num_layers)
            )

        # Patch forward: 输出 past_key_values 转为 DynamicCache
        if not hasattr(Text2Semantic.forward, '_patched'):
            _orig_forward = Text2Semantic.forward
            def _compat_forward(self, *args, **kwargs):
                kwargs.pop('token_type_ids', None)
                output = _orig_forward(self, *args, **kwargs)
                if hasattr(output, 'past_key_values') and isinstance(output.past_key_values, tuple):
                    cache = _tuple_to_cache(output.past_key_values)
                    output.past_key_values = cache
                return output
            _compat_forward = _functools.wraps(_orig_forward)(_compat_forward)
            _compat_forward._patched = True
            Text2Semantic.forward = _compat_forward
            logger.info("已应用 Text2Semantic.forward DynamicCache 兼容补丁")

        # Patch prepare_inputs_for_generation: 输入 past_key_values tuple 转 DynamicCache
        if not hasattr(Text2Semantic.prepare_inputs_for_generation, '_patched'):
            _orig_prepare = Text2Semantic.prepare_inputs_for_generation
            def _compat_prepare(self, input_ids, past_key_values=None, **kwargs):
                result = _orig_prepare(self, input_ids, past_key_values=past_key_values, **kwargs)
                if isinstance(result.get("past_key_values"), tuple):
                    result["past_key_values"] = _tuple_to_cache(result["past_key_values"])
                return result
            _compat_prepare._patched = True
            Text2Semantic.prepare_inputs_for_generation = _compat_prepare
            logger.info("已应用 prepare_inputs_for_generation DynamicCache 兼容补丁")

        # Patch _reorder_cache: beam search 重排后返回 DynamicCache
        if not hasattr(Text2Semantic._reorder_cache, '_patched'):
            _orig_reorder = Text2Semantic._reorder_cache
            def _compat_reorder_cache(past_key_values, beam_idx):
                pkv_tuple = _cache_to_tuple(past_key_values)
                reordered = _orig_reorder(pkv_tuple, beam_idx)
                return _tuple_to_cache(reordered)
            _compat_reorder_cache._patched = True
            Text2Semantic._reorder_cache = staticmethod(_compat_reorder_cache)
            logger.info("已应用 _reorder_cache DynamicCache 兼容补丁")
    except Exception as e:
        logger.warning(f"无法应用 Text2Semantic.forward 补丁: {e}")

    # Monkey-patch BigVGAN._from_pretrained 兼容新版 huggingface_hub
    try:
        from external.bigvgan.bigvgan import BigVGAN
        if not hasattr(BigVGAN._from_pretrained, '_patched'):
            _orig_bigvgan_fp = BigVGAN._from_pretrained.__func__
            import functools
            @functools.wraps(_orig_bigvgan_fp)
            def _compat_bigvgan_fp(cls, *, proxies=None, resume_download=None, **kwargs):
                return _orig_bigvgan_fp(cls, proxies=proxies, resume_download=resume_download, **kwargs)
            _compat_bigvgan_fp._patched = True
            BigVGAN._from_pretrained = classmethod(_compat_bigvgan_fp)
            logger.info("已应用 BigVGAN._from_pretrained 兼容补丁")
    except Exception as e:
        logger.warning(f"无法应用 BigVGAN 补丁: {e}")

    # 性能优化：TF32 / cuDNN benchmark（来自配置文件）
    opt = CONFIG.get("optimization", {})

    # T2S 注意力后端（须在 import confuciustts 之前设定，Confucius4-TTS 在加载时读取）。
    # sdpa = 本项目实测最快最稳；flash_attention_2 省显存但 T2S 更慢。
    attn_backend = opt.get("attn_backend", "sdpa")
    os.environ["CONFUCIUS4_ATTN"] = attn_backend
    logger.info(f"T2S 注意力后端: {attn_backend}")

    if opt.get("enable_tf32", False):
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
    if opt.get("cudnn_benchmark", False):
        torch.backends.cudnn.benchmark = True

    try:
        model = ConfuciusTTS(
            config_path="config/inference_config.yaml",
            device=device,
            use_cuda_kernel=opt.get("bigvgan_cuda_kernel", False),
            enable_ref_cache=opt.get("ref_audio_cache", False),
            ref_device=opt.get("ref_feature_device", "cpu"),
        )
        sampling_rate = model.sample_rate

        # 最优精度策略：T2S 保持 fp32（整体半精度会因 GPT2 LayerNorm/c_proj 的
        # Half/Float 错位而崩溃），S2A(DiT 扩散) 单独转 bf16（半精度耐受好、
        # 提速约 1.5–2×、峰值显存减半）。BigVGAN 声码器恒 fp32（半精度易 NaN）。
        s2a_dtype = opt.get("s2a_dtype", "fp32")
        if s2a_dtype in ("bf16", "fp16"):
            target = torch.bfloat16 if s2a_dtype == "bf16" else torch.float16
            model.s2a_model = model.s2a_model.to(target)
            logger.info(f"S2A 已单独转换为 {target} (T2S 保持 fp32，避免 GPT2 半精度错位) | 注意力后端: {attn_backend}")

        logger.info(f"模型加载完成！采样率: {sampling_rate}Hz | 特征提取设备: {model.ref_device} | 推理设备: {device}")
    finally:
        os.chdir(original_dir)

    return model


def update_task_progress(task_id: str, progress: float, message: str = ""):
    if task_id in tasks:
        tasks[task_id].progress = min(100.0, max(0.0, progress))
        if message:
            tasks[task_id].message = message
        _update_task_progress(task_id, progress, message)


def calculate_rtf(audio_duration: float, inference_time: float) -> float:
    if audio_duration <= 0:
        return 0.0
    return inference_time / audio_duration


def generate_voice_clone(task_id: str, text: str, lang: str, ref_audio_path: str, params: dict):
    """执行声音克隆（在线程池中运行）

    流水线优化（CPU 生产 / GPU 消费）：
      阶段 1 [CPU，无需 gpu_lock]：加载参考音频 → 提取特征（Wav2Vec2/CAMPPlus/mel）
      阶段 2 [GPU，持有 gpu_lock]   ：T2S → S2A扩散 → BigVGAN 声码器
    当 max_workers=2 时，请求 B 可在请求 A 占用 GPU 的同时在 CPU 上提取特征，
    实现 GPU 近乎满载运行。
    """
    task = tasks[task_id]
    task.status = "running"
    task.started_at = time.time()
    start_task_progress(task_id, f"声音克隆 {task_id[:12]}")

    try:
        update_task_progress(task_id, 5, "正在加载参考音频...")

        if not os.path.exists(ref_audio_path):
            raise FileNotFoundError(f"参考音频不存在: {ref_audio_path}")

        # ================================================================
        # 阶段 1：CPU 特征提取（不占用 GPU 锁，可与其它任务的 GPU 推理并行）
        # ================================================================
        update_task_progress(task_id, 15, "正在提取语音特征 (CPU)...")

        original_dir = os.getcwd()
        os.chdir(CONFUCIUS4_ROOT)
        try:
            semantic_features, style_embedding, reference_mel = model.extract_ref_features(ref_audio_path)
        finally:
            os.chdir(original_dir)

        update_task_progress(task_id, 25, "特征提取完成，等待 GPU...")

        # ================================================================
        # 阶段 2：GPU 推理（T2S → S2A → BigVGAN），串行化避免显存争抢
        # ================================================================
        gpu_wait_start = time.time()
        with gpu_lock:
            gpu_wait_time = time.time() - gpu_wait_start
            # gpu_wait_time > 0 说明本任务的 CPU 特征提取与另一任务的 GPU 推理发生了重叠（流水线生效）
            if gpu_wait_time > 0.05:
                logger.info(f"[任务 {task_id[:12]}] CPU特征已就绪，等待GPU锁 {gpu_wait_time:.2f}s（流水线并行中）")
            update_task_progress(task_id, 35, f"开始生成 (timesteps={params.get('n_timesteps', 15)}, beams={params.get('num_beams', 1)})...")

            original_dir = os.getcwd()
            os.chdir(CONFUCIUS4_ROOT)
            try:
                inference_start = time.time()

                # 使用预提取的特征直接生成音频（跳过特征提取阶段）
                audio = model.generate_from_features(
                    text=text,
                    lang=lang,
                    semantic_features=semantic_features,
                    style_embedding=style_embedding,
                    reference_mel=reference_mel,
                    temperature=params.get("temperature", 0.8),
                    top_p=params.get("top_p", 0.8),
                    top_k=params.get("top_k", 30),
                    num_beams=params.get("num_beams", 1),
                    repetition_penalty=params.get("repetition_penalty", 10.0),
                    max_length=params.get("max_length", 1520),
                    n_timesteps=params.get("n_timesteps", 15),
                    inference_cfg_rate=params.get("inference_cfg_rate", 0.7),
                    verbose=False,
                )

                inference_time = time.time() - inference_start
            finally:
                os.chdir(original_dir)

        update_task_progress(task_id, 85, "正在后处理音频...")

        # 确定输出路径
        output_path_param = params.get("output_path")
        if output_path_param:
            output_path = Path(output_path_param)
        else:
            output_dir = Path("outputs")
            output_dir.mkdir(exist_ok=True)
            output_path = output_dir / f"confucius4_{task_id}.wav"

        output_path.parent.mkdir(parents=True, exist_ok=True)

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

        sf.write(str(output_path), waveform.squeeze(0).cpu().numpy(), sampling_rate)

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

        # 每次任务完成打印 RTF，方便评估优化效果
        if CONFIG.get("logging", {}).get("print_rtf", True):
            print(
                f"\n================== [RTF] 任务 {task_id[:12]} 完成 ==================\n"
                f"  RTF            : {rtf:.4f}\n"
                f"  音频时长       : {audio_duration:.2f}s\n"
                f"  推理时间       : {inference_time:.2f}s\n"
                f"  n_timesteps    : {params.get('n_timesteps')}\n"
                f"  num_beams      : {params.get('num_beams')}\n"
                f"  输出           : {output_path}\n"
                f"==============================================================\n"
            )
        stop_task_progress(task_id)

        # 清理上传的参考音频
        if task.is_uploaded_ref:
            try:
                if os.path.exists(ref_audio_path):
                    os.remove(ref_audio_path)
            except Exception as e:
                logger.warning(f"清理上传文件失败: {e}")

    except Exception as e:
        logger.error(f"任务 {task_id} 失败: {e}")
        task.status = "failed"
        task.error = str(e)
        task.message = f"生成失败: {e}"
        task.completed_at = time.time()
        stop_task_progress(task_id)
        raise


# ============== FastAPI 应用 ==============

@asynccontextmanager
async def lifespan(app: FastAPI):
    global executor

    logger.info("=" * 60)
    logger.info("Confucius4-TTS API Server 启动中...")
    logger.info("=" * 60)

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--device", default=None)
    parser.add_argument("--max-workers", type=int, default=None)
    args, _ = parser.parse_known_args()

    srv_cfg = CONFIG.get("server", {})
    host = args.host or srv_cfg.get("host", "0.0.0.0")
    port = args.port or srv_cfg.get("port", 8857)
    device_arg = args.device or srv_cfg.get("device")
    max_workers = args.max_workers if args.max_workers is not None else srv_cfg.get("max_workers", 1)
    executor = ThreadPoolExecutor(max_workers=max_workers)
    logger.info(f"线程池初始化完成，最大工作线程数: {max_workers}")

    load_model(device_arg)

    logger.info("=" * 60)
    logger.info(f"API 文档地址: http://{host}:{port}/docs")
    logger.info("=" * 60)

    yield

    logger.info("正在关闭服务器...")
    executor.shutdown(wait=True)
    logger.info("服务器已关闭")


app = FastAPI(
    title="Confucius4-TTS API",
    description="Confucius4-TTS API - 支持 14 语言声音克隆，基于 Speech Encoder + LLM 架构",
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


# ============== API 路由 ==============

@app.get("/", response_model=ServerInfo)
async def root():
    return ServerInfo(
        model_loaded=model is not None,
        device=str(device) if device else "unknown",
        sampling_rate=sampling_rate,
        model_type="Confucius4-TTS",
        supported_languages=list(LANGUAGES),
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

    return JSONResponse(
        status_code=200,
        content={
            "status": "healthy" if model is not None else "degraded",
            "model_loaded": model is not None,
            "model_type": "Confucius4-TTS",
            "device": str(device) if device else "unknown",
            "sampling_rate": sampling_rate,
            "gpu_available": gpu_available,
            "gpu_memory_used_gb": gpu_memory_used,
            "gpu_memory_total_gb": gpu_memory_total,
            "running_tasks": running_tasks,
            "pending_tasks": pending_tasks,
            "supported_languages": list(LANGUAGES),
        },
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
    status: Optional[str] = Query(None),
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
        ]
    }


@app.post("/api/v1/voice/clone", response_model=TaskResponse)
async def voice_clone(
    background_tasks: BackgroundTasks,
    text: str = Form(..., description="要合成的文本"),
    ref_audio: Optional[UploadFile] = File(None, description="参考音频文件"),
    ref_audio_path: Optional[str] = Form(None, description="参考音频文件路径"),
    lang: str = Form("zh", description="语言代码 (zh/en/ja/ko/de/fr/th/id/vi/es/pt/it/ru/ms)"),
    output_path: Optional[str] = Form(None, description="输出文件路径"),
    temperature: float = Form(CONFIG["inference"]["temperature"], description="采样温度 (0.1-2.0)"),
    top_p: float = Form(CONFIG["inference"]["top_p"], description="核采样阈值 (0.0-1.0)"),
    top_k: int = Form(CONFIG["inference"]["top_k"], description="Top-K 采样 (0-100)"),
    num_beams: int = Form(CONFIG["inference"]["num_beams"], description="Beam Search 宽度 (1-10)"),
    repetition_penalty: float = Form(CONFIG["inference"]["repetition_penalty"], description="重复惩罚 (1.0-20.0)"),
    max_length: int = Form(CONFIG["inference"]["max_length"], description="最大序列长度"),
    n_timesteps: int = Form(CONFIG["inference"]["n_timesteps"], description="S2A 扩散步数 (1-100)"),
    inference_cfg_rate: float = Form(CONFIG["inference"]["inference_cfg_rate"], description="CFG 引导强度 (0.0-2.0)"),
):
    """
    声音克隆接口

    使用参考音频的声音特征合成目标文本。
    支持 14 种语言: zh, en, ja, ko, de, fr, th, id, vi, es, pt, it, ru, ms

    参考音频传入方式:
    - **文件上传**: 通过 ref_audio 字段
    - **本地路径**: 通过 ref_audio_path 字段（推荐本地调用）

    两种方式二选一，同时提供时优先使用 ref_audio_path。
    """
    if model is None:
        raise HTTPException(status_code=503, detail="模型未加载")

    if lang not in LANGUAGES:
        raise HTTPException(status_code=400, detail=f"不支持的语言: {lang}，支持: {', '.join(LANGUAGES)}")

    if not text.strip():
        raise HTTPException(status_code=400, detail="文本不能为空")

    # 【配置驱动】num_beams 与 n_timesteps 统一由 config/confucius4_server.yaml 控制，
    # 忽略客户端（网页/测试页）传来的覆盖值，确保“改配置 -> 重启即生效”。
    inf_cfg = CONFIG.get("inference", {})
    num_beams = inf_cfg.get("num_beams", 1)
    n_timesteps = inf_cfg.get("n_timesteps", 15)

    # 确定参考音频来源
    is_uploaded = False
    final_ref_audio_path = None

    if ref_audio_path and ref_audio_path.strip():
        final_ref_audio_path = ref_audio_path.strip()
        if not os.path.exists(final_ref_audio_path):
            raise HTTPException(status_code=400, detail=f"参考音频路径不存在: {final_ref_audio_path}")
    elif ref_audio is not None:
        is_uploaded = True
        upload_dir = Path(PROJECT_ROOT) / "uploads"
        upload_dir.mkdir(exist_ok=True)
        task_id_tmp = str(uuid.uuid4())
        # 使用安全文件名，避免中文字符导致读取失败
        ext = Path(ref_audio.filename).suffix or ".wav"
        saved_path = upload_dir / f"{task_id_tmp}{ext}"
        with open(saved_path, "wb") as f:
            content = await ref_audio.read()
            f.write(content)
        final_ref_audio_path = str(saved_path.resolve())
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

    params = {
        "语言": lang,
        "文本": text[:50] + ("..." if len(text) > 50 else ""),
        "参考音频": ref_audio_path or (ref_audio.filename if ref_audio else ""),
        "温度": temperature,
        "top_p": top_p,
        "top_k": top_k,
        "num_beams": num_beams,
        "n_timesteps": n_timesteps,
        "cfg_rate": inference_cfg_rate,
    }
    show_task_card(task, params=params)

    gen_params = {
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k,
        "num_beams": num_beams,
        "repetition_penalty": repetition_penalty,
        "max_length": max_length,
        "n_timesteps": n_timesteps,
        "inference_cfg_rate": inference_cfg_rate,
        "output_path": output_path,
    }

    def run_clone():
        generate_voice_clone(task_id, text, lang, final_ref_audio_path, gen_params)

    executor.submit(run_clone)

    return TaskResponse(
        task_id=task_id,
        status="pending",
        message="声音克隆任务已创建",
    )


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
        filename=f"confucius4_{task_id}.wav",
    )


# ============== WebSocket 实时进度 ==============

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


# ============== SSE 实时进度 ==============

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


# ============== 主函数 ==============

if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Confucius4-TTS API Server")
    parser.add_argument("--host", default=None, help="服务器地址（默认读配置）")
    parser.add_argument("--port", type=int, default=None, help="服务器端口（默认读配置）")
    parser.add_argument("--device", default=None, help="计算设备 (cuda/cpu/mps)")
    parser.add_argument("--max-workers", type=int, default=None, help="最大工作线程数（默认读配置）")

    args = parser.parse_args()

    srv_cfg = CONFIG.get("server", {})
    host = args.host or srv_cfg.get("host", "0.0.0.0")
    port = args.port or srv_cfg.get("port", 8857)

    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=False,
        workers=1,
        access_log=False,
    )
