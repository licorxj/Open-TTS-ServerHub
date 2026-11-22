"""Tools package —— 共享工具层（ASR / 模型管理 / 文本归一化）。

采用 PEP 562 惰性导出：``tools.asr``（Whisper 后端）在 import 时会拉起
torch / transformers，而 ``tools.asr_sensevoice``（默认 SenseVoice 后端）只需要
funasr。若包级 ``__init__`` 直接 ``from .asr import ASR``，那么只想用
SenseVoice 的调用方也会被连带拖进 transformers 的加载与 torchcodec 兼容处理。

    from tools import ASR              # 仅此时才 import tools.asr
    from tools import SenseVoiceASR   # 仅此时才 import tools.asr_sensevoice
    from tools.asr import ASR         # 直接导入子模块，行为与以前一致
    from tools import create_asr      # 统一工厂，默认后端 sensevoice
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_LAZY = {
    "ASR": ("tools.asr", "ASR"),
    "transcribe_audio": ("tools.asr", "transcribe_audio"),
    "SenseVoiceASR": ("tools.asr_sensevoice", "SenseVoiceASR"),
    "clean_text": ("tools.asr_sensevoice", "clean_text"),
    "ModelsManager": ("tools.models_manager", "ModelsManager"),
    "TextNormalizer": ("tools.text_normalize", "TextNormalizer"),
    "ASR_BACKENDS": ("tools.asr_factory", "ASR_BACKENDS"),
    "DEFAULT_ASR_BACKEND": ("tools.asr_factory", "DEFAULT_ASR_BACKEND"),
    "DEFAULT_MODELS": ("tools.asr_factory", "DEFAULT_MODELS"),
    "normalize_backend": ("tools.asr_factory", "normalize_backend"),
    "resolve_backend": ("tools.asr_factory", "resolve_backend"),
    "default_model": ("tools.asr_factory", "default_model"),
    "coerce_model": ("tools.asr_factory", "coerce_model"),
    "load_asr_class": ("tools.asr_factory", "load_asr_class"),
    "create_asr": ("tools.asr_factory", "create_asr"),
}

__all__ = list(_LAZY)


def __getattr__(name: str) -> Any:
    entry = _LAZY.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    mod_name, attr = entry
    value = getattr(import_module(mod_name), attr)
    globals()[name] = value  # 缓存，后续访问不再走 __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
