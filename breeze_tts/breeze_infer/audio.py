"""Reference-audio loading for Breeze inference."""

from __future__ import annotations

import hashlib
import io
import threading
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
import torch

# 参考音频编码缓存：以（文件内容哈希, tokenizer id）为 key。
# 同一参考音频（无论是固定 ref_audio_path，还是批量克隆里重复上传的相同文件）
# 只做一次昂贵的神经音频 codec 编码，后续请求直接复用 audio codes，从而加速批量克隆。
# 命中缓存返回的 tensor 视为只读（下游只读取 / 切片 / .to()，不产生原地修改）。
_REF_CACHE: dict[tuple, torch.Tensor] = {}
_REF_LOCK = threading.Lock()
_REF_MAX = 256


def _decode_bytes(audio_tokenizer: Any, data: bytes) -> torch.Tensor:
    wav, sample_rate = sf.read(io.BytesIO(data), always_2d=True, dtype="float32")
    wav = np.mean(wav, axis=1)
    encoded = audio_tokenizer.encode(wav, sr=sample_rate)
    codes = torch.as_tensor(encoded["audio_codes"][0], dtype=torch.int16)
    if codes.ndim != 2:
        raise ValueError(
            f"Expected 2D audio codes, got shape {tuple(codes.shape)}"
        )
    return codes.cpu().contiguous()


def encode_prompt_audio(audio_tokenizer: Any, audio_path: str | Path) -> torch.Tensor:
    data = Path(audio_path).read_bytes()
    key = (hashlib.md5(data).hexdigest(), id(audio_tokenizer))
    with _REF_LOCK:
        cached = _REF_CACHE.get(key)
    if cached is not None:
        return cached
    codes = _decode_bytes(audio_tokenizer, data)
    with _REF_LOCK:
        if len(_REF_CACHE) >= _REF_MAX:
            # 超过上限：淘汰最早插入的一项（近似 LRU）
            _REF_CACHE.pop(next(iter(_REF_CACHE)))
        _REF_CACHE[key] = codes
    return codes
