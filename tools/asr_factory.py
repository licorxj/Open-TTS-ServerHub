#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ASR 后端统一工厂：在两套实现之间做选择、提供默认配置。

两套后端（接口完全一致：``transcribe`` / ``transcribe_with_timestamps`` /
``is_loaded`` / ``device``）：

============== ============================================ ===========================
backend        实现                                          默认模型
============== ============================================ ===========================
``sensevoice`` ``tools/asr_sensevoice.py``（funASR，非自回归） ``iic/SenseVoiceSmall``
``whisper``    ``tools/asr.py``（transformers pipeline）       ``openai/whisper-large-v3-turbo``
============== ============================================ ===========================

默认走 **sensevoice**（CPU 实测比 Whisper 快一个数量级）。选择优先级：
显式 ``backend`` 参数 > 环境变量 ``LC_ASR_BACKEND`` > ``DEFAULT_ASR_BACKEND``。

本模块顶层**只依赖标准库**：后端模块用 importlib 按文件加载，避免 ``tools`` 包
``__init__`` 连带 import transformers —— 走 SenseVoice 时完全不碰 Whisper 那套
重型依赖；管家主进程也可以安全地只加载本模块来读默认配置。
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

ASR_BACKENDS = ("sensevoice", "whisper")
DEFAULT_ASR_BACKEND = "sensevoice"
ENV_BACKEND = "LC_ASR_BACKEND"
ENV_MODEL = "LC_ASR_MODEL"

DEFAULT_MODELS = {
    "sensevoice": "iic/SenseVoiceSmall",
    "whisper": "openai/whisper-large-v3-turbo",
}

_BACKEND_FILES = {
    "sensevoice": "asr_sensevoice.py",
    "whisper": "asr.py",
}
_BACKEND_CLASSES = {
    "sensevoice": "SenseVoiceASR",
    "whisper": "ASR",
}
# 兼容历史/口语化叫法，避免配置里写错一个词就直接崩
_ALIASES = {
    "funasr": "sensevoice",
    "sense_voice": "sensevoice",
    "sense-voice": "sensevoice",
    "sensevoicesmall": "sensevoice",
    "iic/sensevoicesmall": "sensevoice",
    "sv": "sensevoice",
    "fast_whisper": "whisper",
    "faster_whisper": "whisper",
    "faster-whisper": "whisper",
    "whisper-turbo": "whisper",
    "transformers": "whisper",
    "hf": "whisper",
}

_TOOLS_DIR = Path(__file__).resolve().parent

__all__ = [
    "ASR_BACKENDS",
    "DEFAULT_ASR_BACKEND",
    "DEFAULT_MODELS",
    "ENV_BACKEND",
    "ENV_MODEL",
    "normalize_backend",
    "resolve_backend",
    "default_model",
    "coerce_model",
    "load_asr_class",
    "create_asr",
    "transcribe_audio",
]


def normalize_backend(value: Any) -> str:
    """把配置值归一成 ``sensevoice`` / ``whisper``；空值 = 默认后端。"""
    raw = str(value or "").strip().lower()
    if not raw:
        return DEFAULT_ASR_BACKEND
    if raw in ASR_BACKENDS:
        return raw
    if raw in _ALIASES:
        return _ALIASES[raw]
    # 未知值不让整个服务起不来：告警并回退默认
    logger.warning(
        "未知 ASR 后端 %r，回退到 %s（可用：%s）",
        value,
        DEFAULT_ASR_BACKEND,
        ", ".join(ASR_BACKENDS),
    )
    return DEFAULT_ASR_BACKEND


def resolve_backend(backend: Optional[str] = None) -> str:
    """显式参数 > 环境变量 > 默认。"""
    raw = str(backend or "").strip() or os.environ.get(ENV_BACKEND, "").strip()
    return normalize_backend(raw)


def default_model(backend: Optional[str] = None) -> str:
    return DEFAULT_MODELS[normalize_backend(backend)]


def coerce_model(backend: Optional[str] = None, model_name: Optional[str] = None) -> str:
    """给出该后端实际该用的模型名。

    历史配置里 ``asr_model`` 常写着 ``openai/whisper-large-v3-turbo``；切到
    SenseVoice 后如果原样传进去会加载失败，这里自动纠正为后端默认模型并告警。
    """
    be = normalize_backend(backend)
    model = str(model_name or "").strip() or os.environ.get(ENV_MODEL, "").strip()
    if not model:
        return DEFAULT_MODELS[be]
    low = model.lower()
    mismatched = (be == "sensevoice" and "whisper" in low) or (
        be == "whisper" and ("sensevoice" in low or low.startswith("iic/"))
    )
    if mismatched:
        logger.warning(
            "ASR 模型 %r 与后端 %s 不匹配，改用默认模型 %s",
            model,
            be,
            DEFAULT_MODELS[be],
        )
        return DEFAULT_MODELS[be]
    return model


def load_asr_class(backend: Optional[str] = None, tools_dir: Optional[str] = None) -> type:
    """按文件加载后端模块并返回其 ASR 类（不经过 ``tools`` 包 ``__init__``）。

    这样 SenseVoice 分支只加载 ``tools/asr_sensevoice.py``，不会连带拉起
    transformers / torch；Whisper 分支则加载原本的 ``tools/asr.py``（其内部的
    torchcodec 兼容处理照旧生效）。
    """
    be = normalize_backend(backend)
    base = Path(tools_dir) if tools_dir else _TOOLS_DIR
    path = base / _BACKEND_FILES[be]
    if not path.is_file():
        raise RuntimeError(f"找不到 ASR 后端实现：{path}")
    mod_name = f"tools_asr_backend_{be}"
    mod = sys.modules.get(mod_name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(mod_name, str(path))
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载 ASR 后端：{path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    cls = getattr(mod, _BACKEND_CLASSES[be], None)
    if cls is None:
        raise RuntimeError(f"{path} 缺少 {_BACKEND_CLASSES[be]} 类")
    return cls


def create_asr(
    backend: Optional[str] = None,
    model_name: Optional[str] = None,
    device: Optional[str] = None,
    **kwargs: Any,
) -> Any:
    """创建后端实例（默认 SenseVoice）。

    ``device`` 为空时：SenseVoice 走 CPU、Whisper 走其自身的自动设备选择
    （历史行为）。``kwargs`` 只对 SenseVoice 后端透传（ncpu / language / ...），
    Whisper 后端构造只接受 ``model_name`` / ``device``。
    """
    be = resolve_backend(backend)
    model = coerce_model(be, model_name)
    cls = load_asr_class(be)
    if be == "whisper":
        kwargs = {}
    return cls(model_name=model, device=device, **kwargs)


def transcribe_audio(
    audio: Any,
    backend: Optional[str] = None,
    model_name: Optional[str] = None,
    device: Optional[str] = None,
) -> str:
    """便捷函数；批量转写请复用 ``create_asr`` 返回的实例。"""
    return create_asr(backend=backend, model_name=model_name, device=device).transcribe(audio)


if __name__ == "__main__":
    import json

    if len(sys.argv) < 2:
        print("Usage: python tools/asr_factory.py <audio_file> [sensevoice|whisper]")
        sys.exit(1)
    be = resolve_backend(sys.argv[2] if len(sys.argv) > 2 else None)
    print(json.dumps({
        "backend": be,
        "model": default_model(be),
        "text": transcribe_audio(sys.argv[1], backend=be),
    }, ensure_ascii=False))
