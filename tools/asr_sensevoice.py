#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SenseVoice ASR 工具（funASR / ``iic/SenseVoiceSmall``）。

与 ``tools/asr.py`` 的 Whisper ASR 平级的第二套后端，**接口保持完全一致**：
    ``__init__(model_name, device)`` / ``transcribe(audio)`` /
    ``transcribe_with_timestamps(audio)`` / ``is_loaded`` / ``device``

为什么单独一个模块、且默认优先用它：
* Whisper 后端走 transformers ASR pipeline：import 时要拉起 torch、还要处理本机
  缺 FFmpeg 动态库导致的 torchcodec 崩溃（见 tools/asr.py 顶部注释），自回归解码
  在 CPU 上约 40s 级；
* SenseVoice 是非自回归模型，funASR 自带音频前端，不需要 transformers / torchcodec，
  本机 CPU 实测「加载 ~7.6s + 6s 音频推理 ~1.4s」，快一个数量级；
* 两套后端互不 import，由 ``tools.asr_factory`` 按名字选择 —— 用 SenseVoice 时
  完全不会被 Whisper 那套重型依赖拖慢。

约定：
* 模型懒加载：构造时不碰 CUDA / 不下载权重，首次 ``transcribe`` 才 AutoModel；
* 权重走 ModelScope 缓存（``MODELSCOPE_CACHE``），``model_name`` 也可直接传本地目录；
* 长音频按 25s 切片（SenseVoice 一次吃整段长音频易截断），逐段转写后拼接；
* 输出清理 ``<|zh|><|NEUTRAL|><|Speech|><|withitn|>`` 这类 rich 标签。
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional, Tuple, Union

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "iic/SenseVoiceSmall"

# funASR 的 rich 转录标签；末尾的 </s> 部分版本会带
_TAG_RE = re.compile(r"<\|[^|]*\|>|</?s>")
_LANG_RE = re.compile(r"<\|([a-z]{2,3})\|>")
_CJK = r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]"

AudioInput = Union[str, os.PathLike, Tuple[Any, int]]


def _ensure_torchcodec_blocked() -> None:
    """与 tools/asr.py 对齐的兼容入口。

    各引擎 / runner 会在创建 ASR 实例前统一调用它。SenseVoice 不 import
    transformers、也不碰 torchcodec 解码，因此这里什么都不用做。
    """
    return None


def clean_text(raw: str) -> str:
    """把 funASR 原始输出收拾成能直接当参考文字稿用的单行文本。"""
    s = _TAG_RE.sub("", str(raw or ""))
    s = " ".join(part.strip() for part in s.replace("\r", "\n").split("\n") if part.strip())
    # 中文之间的空格去掉（"你 好" -> "你好"），中英之间保留
    s = re.sub(rf"(?<={_CJK})\s+(?={_CJK})", "", s).strip()
    return s


def _extract_language(raw: str) -> str:
    m = _LANG_RE.search(str(raw or ""))
    return m.group(1) if m else ""


class SenseVoiceASR:
    """SenseVoiceSmall ASR（接口同 :class:`tools.asr.ASR`）。

    Example:
        >>> asr = SenseVoiceASR()
        >>> text = asr.transcribe("audio.wav")
        >>> print(text)
        "你好，欢迎使用 LCTTS 管家统一语音调度。"

        >>> import soundfile as sf
        >>> wave, sr = sf.read("audio.wav")
        >>> asr.transcribe((wave, sr))
    """

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        device: Optional[str] = None,
        *,
        ncpu: int = 4,
        language: str = "auto",
        use_itn: bool = True,
        chunk_sec: float = 25.0,
    ):
        """Args:
            model_name: ModelScope 仓库名或本地权重目录（默认 iic/SenseVoiceSmall）。
            device: ``"cpu"`` / ``"cuda"`` / ``"cuda:0"``；None/空 = CPU
                （与管家 HubASR 默认一致：入库转写低频、不抢 GPU 显存）。
            ncpu: CPU 推理线程数。
            language: 传给 funASR 的语言（auto / zh / en / yue / ja / ko）。
            use_itn: 是否做逆文本归一化（标点、数字写法）。
            chunk_sec: 长音频分段长度（秒）。
        """
        self.model_name = model_name or DEFAULT_MODEL
        self.device = self._normalize_device(device)
        self.ncpu = max(1, int(ncpu))
        self.language = language or "auto"
        self.use_itn = bool(use_itn)
        self.chunk_sec = float(chunk_sec) if chunk_sec and chunk_sec > 0 else 25.0
        self._model: Any = None

    @staticmethod
    def _normalize_device(device: Optional[str]) -> str:
        d = str(device or "").strip()
        if not d:
            return "cpu"
        # funASR 用 "cuda:0" 这种带索引的写法；"cuda" 补上索引更稳
        if d.startswith("cuda") and ":" not in d:
            return d + ":0"
        return d

    def _load_model(self) -> Any:
        if self._model is None:
            try:
                from funasr import AutoModel
            except ImportError as exc:  # pragma: no cover - 依赖缺失时给出可操作提示
                raise RuntimeError(
                    "缺少 funASR 依赖，请安装 funasr（见根目录 requirements.txt）"
                ) from exc
            logger.info("Loading SenseVoice ASR: %s on %s", self.model_name, self.device)
            self._model = AutoModel(
                model=self.model_name,
                device=self.device,
                ncpu=self.ncpu,
                disable_update=True,   # 不联网检查版本，离线可用
                disable_pbar=True,
                log_level="WARNING",
            )
            logger.info("SenseVoice ASR loaded on %s", self.device)
        return self._model

    def _generate(self, wav_path: str) -> str:
        """跑一次 funASR，返回**未清理**的原始文本（含 rich 标签）。"""
        res = self._load_model().generate(
            input=wav_path,
            language=self.language,
            use_itn=self.use_itn,
            disable_pbar=True,
            batch_size=1,
        )
        if not res or not isinstance(res[0], dict):
            raise RuntimeError("funASR 返回空结果")
        return str(res[0].get("text") or "")

    @staticmethod
    def _probe_duration(path: str) -> float:
        import soundfile as sf

        try:
            info = sf.info(path)
            return float(info.frames) / float(info.samplerate or 1)
        except Exception:  # noqa: BLE001
            return 0.0

    @staticmethod
    def _load_waveform(path: str) -> Tuple[Any, int]:
        import soundfile as sf

        wave, sr = sf.read(path, dtype="float32", always_2d=True)
        return wave.mean(axis=1), int(sr)

    @staticmethod
    def _to_waveform(value: Any) -> Any:
        """把 numpy / torch.Tensor / 多维数组统一成单声道 float32 一维波形。"""
        import numpy as np

        if hasattr(value, "detach"):
            value = value.detach().cpu().numpy()
        arr = np.asarray(value, dtype=np.float32)
        arr = np.squeeze(arr)
        if arr.ndim > 1:
            # (channels, T) 或 (T, channels) 都压成单声道
            arr = arr.mean(axis=0 if arr.shape[0] < arr.shape[1] else 1)
        return arr

    def _write_chunks(self, wave: Any, sr: int, tmpdir: str) -> list[tuple[float, float, str]]:
        import soundfile as sf

        step = int(self.chunk_sec * sr)
        if step <= 0:
            raise ValueError("采样率异常，无法分段")
        min_len = int(0.3 * sr)  # 末尾过短片段跳过
        pieces: list[tuple[float, float, str]] = []
        n = wave.shape[0]
        for idx, start in enumerate(range(0, n, step)):
            seg = wave[start : start + step]
            if seg.shape[0] < min_len:
                continue
            out = os.path.join(tmpdir, f"seg_{idx:04d}.wav")
            sf.write(out, seg, sr, subtype="PCM_16")
            pieces.append((start / sr, (start + seg.shape[0]) / sr, out))
        return pieces

    def _transcribe_pieces(self, audio: AudioInput) -> list[tuple[float, float, str]]:
        """返回 [(起始秒, 结束秒, funASR 原始文本), ...]。

        所有临时 wav 都在这个上下文里创建并随上下文销毁；短音频直接传原路径，
        避免任何重编码。
        """
        import soundfile as sf

        with tempfile.TemporaryDirectory(prefix="lc_sensevoice_") as td:
            if isinstance(audio, (str, os.PathLike)):
                path = str(audio)
                if not Path(path).is_file():
                    raise FileNotFoundError(f"音频文件不存在：{path}")
                duration = self._probe_duration(path)
                if duration > 30.0:
                    wave, sr = self._load_waveform(path)
                    pieces = self._write_chunks(wave, sr, td)
                else:
                    pieces = [(0.0, duration, path)]
            else:
                waveform, sr = audio
                wave = self._to_waveform(waveform)
                sr = int(sr)
                if sr <= 0:
                    raise ValueError("采样率异常")
                duration = wave.shape[0] / sr
                if duration > 30.0:
                    pieces = self._write_chunks(wave, sr, td)
                else:
                    out = os.path.join(td, "audio.wav")
                    sf.write(out, wave, sr, subtype="PCM_16")
                    pieces = [(0.0, duration, out)]

            results: list[tuple[float, float, str]] = []
            for start, end, wav_path in pieces:
                try:
                    raw = self._generate(wav_path)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("分段转写失败 (start=%.1fs): %s", start, exc)
                    continue
                results.append((start, end, raw))
            return results

    def transcribe(self, audio: AudioInput) -> str:
        """转写音频为文本（与 tools.asr.ASR.transcribe 同签名）。

        Args:
            audio: 音频文件路径，或 ``(waveform, sample_rate)`` 元组
                （waveform 支持 numpy 数组 / torch.Tensor，单声道或多通道）。

        Returns:
            清理后的文本字符串。
        """
        texts = [clean_text(raw) for _, _, raw in self._transcribe_pieces(audio)]
        return " ".join(t for t in texts if t).strip()

    def transcribe_with_timestamps(
        self, audio: AudioInput, return_timestamps: bool = True
    ) -> dict:
        """与 Whisper 后端同构的返回：``{text, chunks, language, backend}``。

        SenseVoice 没有词级时间戳，这里给出**段级** ``chunks``（每段 25s 内一条，
        含 ``timestamp: [start, end]``）；短音频只有一条整段 chunk。
        """
        chunks: list[dict] = []
        for start, end, raw in self._transcribe_pieces(audio):
            text = clean_text(raw)
            if not text:
                continue
            chunks.append(
                {
                    "text": text,
                    "timestamp": [round(start, 2), round(end, 2)],
                    "language": _extract_language(raw),
                }
            )
        return {
            "text": " ".join(c["text"] for c in chunks).strip(),
            "chunks": chunks if return_timestamps else [],
            "language": chunks[0]["language"] if chunks else "",
            "backend": "sensevoice",
            "note": "SenseVoice 无原生词级时间戳，chunks 为段级结果",
        }

    @property
    def is_loaded(self) -> bool:
        return self._model is not None


def transcribe_audio(
    audio: AudioInput,
    model_name: str = DEFAULT_MODEL,
    device: Optional[str] = None,
) -> str:
    """便捷函数：一次性转写（多次调用请复用 SenseVoiceASR 实例）。"""
    return SenseVoiceASR(model_name=model_name, device=device).transcribe(audio)


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("Usage: python tools/asr_sensevoice.py <audio_file_path>")
        sys.exit(1)
    _asr = SenseVoiceASR()
    print(json.dumps({"text": _asr.transcribe(sys.argv[1])}, ensure_ascii=False))
