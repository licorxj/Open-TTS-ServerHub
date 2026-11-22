#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ASR (Automatic Speech Recognition) module.

Provides a simple class interface for speech-to-text transcription
using Whisper models from HuggingFace Transformers.
"""

import logging
import sys
import types
from typing import Optional, Tuple, Union

# ---------------------------------------------------------------------------
# 禁用 torchcodec（本机缺 FFmpeg 动态库，libtorchcodec_core*.dll 加载即失败）
#
# ⚠️ 绝不能写成 sys.modules["torchcodec"] = None：
#   · 在「管家 ASR 独立子进程」里，先设 None 再 import transformers 是能工作的，
#     但 Breeze / Audio8 / AuK 等引擎进程在**启动时就已 import transformers**，
#     等到请求时懒加载本模块再设 None，之后任何一处真实的
#     `import torchcodec` 都会抛
#     "ModuleNotFoundError: import of torchcodec halted; None in sys.modules"
#     （这正是用户遇到的 Breeze 克隆报「ASR 自动转写失败」的根因）。
#   · 采用 omnivoice/omni_api_server.py 里已实弹验证的做法：
#       1) 预置一个假的 torchcodec._core 子模块，让探测性 import 不会去加载
#          缺 DLL 的真实 C 扩展；
#       2) 把 transformers 的 is_torchcodec_available 覆盖成恒 False，
#          使其音频加载走 soundfile / librosa 路径。
#     转写时本模块已经用 soundfile 把音频读成 numpy 再喂给 pipeline，
#     完全不依赖 torchcodec 解码，因此禁用它对功能零影响。
#   这段必须在 import transformers **之前**执行（OmniVoice 同款顺序）。
# ---------------------------------------------------------------------------
_tc_core = types.ModuleType("torchcodec._core")
_tc_core.ops = types.SimpleNamespace()
_tc_core.__all__ = ["ops", "VideoDecoder"]
_tc_core.VideoDecoder = type("VideoDecoder", (), {})
sys.modules.setdefault("torchcodec._core", _tc_core)
sys.modules.setdefault("torchcodec._core.ops", _tc_core.ops)


def _disable_transformers_torchcodec() -> None:
    """把 transformers 的 torchcodec 可用性探测覆盖成恒 False（幂等、可重复调用）。

    覆盖的是**已加载模块的属性**，所以即便引擎进程启动时 transformers 早已
    import 完成，这里依然生效；同时清掉 lru_cache 缓存的旧结果。
    """
    try:
        import transformers.utils as transformers_utils
        import transformers.utils.import_utils as import_utils

        try:
            import_utils.is_torchcodec_available.cache_clear()
        except Exception:  # noqa: BLE001 - 旧版本不是 lru_cache
            pass

        def _torchcodec_unavailable() -> bool:
            return False

        import_utils.is_torchcodec_available = _torchcodec_unavailable
        transformers_utils.is_torchcodec_available = _torchcodec_unavailable

        audio_utils = sys.modules.get("transformers.audio_utils")
        if audio_utils is not None:
            audio_utils.is_torchcodec_available = _torchcodec_unavailable
            if hasattr(audio_utils, "TORCHCODEC_VERSION"):
                delattr(audio_utils, "TORCHCODEC_VERSION")
    except Exception:  # noqa: BLE001 - 任何 transformers 版本差异都不能阻断 ASR
        pass


_disable_transformers_torchcodec()

import numpy as np  # noqa: E402
import torch  # noqa: E402
import transformers  # noqa: E402

logger = logging.getLogger(__name__)


def _ensure_torchcodec_blocked() -> None:
    """兼容入口：禁用逻辑已在模块导入时完成，这里幂等地再兜底一次。

    保留此函数是因为各引擎 / runner 仍会在创建 ASR 实例前调用它。
    """
    _disable_transformers_torchcodec()


class ASR:
    """Automatic Speech Recognition class using Whisper models.
    
    This class provides a simple interface for transcribing audio
    to text using HuggingFace's Whisper pipeline.
    
    Example:
        >>> asr = ASR(model_name="openai/whisper-large-v3-turbo")
        >>> text = asr.transcribe("audio.wav")
        >>> print(text)
        "Hello, how are you?"
    
    Example with waveform:
        >>> import torchaudio
        >>> waveform, sr = torchaudio.load("audio.wav")
        >>> text = asr.transcribe((waveform, sr))
        >>> print(text)
        "Hello, how are you?"
    """
    
    def __init__(
        self,
        model_name: str = "openai/whisper-large-v3-turbo",
        device: Optional[str] = None,
    ):
        """Initialize the ASR model.
        
        Args:
            model_name: HuggingFace model name or local path for the Whisper model.
                Default is "openai/whisper-large-v3-turbo".
                Other options: "openai/whisper-large-v3", "openai/whisper-medium", etc.
            device: Device to run the model on. If None, auto-detect CUDA availability.
                Options: "cuda", "cpu", "mps", etc.
        """
        # 必须在 import transformers 之前阻止 torchcodec（见函数注释），否则
        # transformers 会把 torchcodec 当可用包登记，ASR pipeline 解码时崩
        _ensure_torchcodec_blocked()

        from transformers import pipeline as hf_pipeline

        # Auto-detect device if not specified
        if device is None:
            if torch.cuda.is_available():
                self.device = "cuda"
            elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                self.device = "mps"
            else:
                self.device = "cpu"
        else:
            self.device = device
        
        logger.info(f"Initializing ASR model: {model_name} on {self.device}")
        
        # Determine dtype based on device
        asr_dtype = torch.float16 if self.device.startswith("cuda") else torch.float32
        
        # Load the ASR pipeline
        # transformers 4.x 使用 torch_dtype 参数，5.x 更名为 dtype，按版本兼容
        if int(transformers.__version__.split(".")[0]) >= 5:
            self._asr_pipe = hf_pipeline(
                "automatic-speech-recognition",
                model=model_name,
                device=self.device,
                dtype=asr_dtype,
            )
        else:
            self._asr_pipe = hf_pipeline(
                "automatic-speech-recognition",
                model=model_name,
                device=self.device,
                torch_dtype=asr_dtype,
            )
        
        logger.info(f"ASR model loaded successfully on {self.device}")
    
    @torch.inference_mode()
    def transcribe(
        self,
        audio: Union[str, Tuple[np.ndarray, int], Tuple[torch.Tensor, int]],
    ) -> str:
        """Transcribe audio to text.

        Args:
            audio: Input audio, can be one of:
                - str: Path to audio file
                - Tuple[np.ndarray, int]: (waveform, sample_rate) where waveform
                  is a numpy array of shape (1, T) or (T,)
                - Tuple[torch.Tensor, int]: (waveform, sample_rate) where waveform
                  is a torch.Tensor of shape (1, T) or (T,)

        Returns:
            Transcribed text string.

        Raises:
            RuntimeError: If the ASR model is not loaded.

        注意：无论音频是文件路径还是波形，都先用 soundfile 读成 numpy 数组再喂给
        pipeline（dict 形式）。**不要**把文件路径直接交给 pipeline —— transformers 的
        ASR pipeline 会自行解码音频，在部分环境（本机 torchcodec 解码会卡死）下会
        直接 hang 住，预加载能彻底绕开解码后端的不确定性。
        """
        if self._asr_pipe is None:
            raise RuntimeError("ASR model is not loaded. Initialize the ASR class first.")

        if isinstance(audio, str):
            wave, sr = self._load_waveform(audio)
            return self._run_waveform(wave, sr)

        # (waveform, sample_rate) tuple
        waveform, sr = audio
        if isinstance(waveform, torch.Tensor):
            waveform = waveform.cpu().numpy()
        waveform = np.squeeze(waveform)
        return self._run_waveform(waveform, sr)

    def _run_waveform(self, waveform: "np.ndarray", sr: int) -> str:
        """把单声道波形喂给 pipeline 推理；超长则分段。"""
        waveform = np.squeeze(waveform)
        duration = (waveform.shape[0] / sr) if sr else 0.0
        if duration > 30.0:
            return self._transcribe_chunked(waveform, sr)
        audio_input = {"array": waveform, "sampling_rate": int(sr)}
        result = self._asr_pipe(audio_input)
        return self._extract_text(result)

    @staticmethod
    def _load_waveform(audio_path: str) -> Tuple[np.ndarray, int]:
        """读成单声道波形（与 breeze 的 _load_audio_for_asr 一致）。"""
        import soundfile as sf

        wave, sr = sf.read(audio_path, dtype="float32", always_2d=True)
        return wave.mean(axis=1), int(sr)

    def _transcribe_chunked(
        self, waveform: np.ndarray, sr: int, chunk_sec: float = 25.0
    ) -> str:
        """超过 30s 的音频：切成 <=30s 的小段逐段转写后拼接。

        彻底绕开 Whisper long-form（>3000 mel 必须 return_timestamps 的限制），
        对超长参考音频（说书 / 长段落）也能稳定出稿而不报 400。
        """
        if waveform.ndim != 1:
            waveform = np.squeeze(waveform)
        chunk = int(chunk_sec * sr)
        if chunk <= 0:
            raise ValueError("采样率异常，无法分段")
        min_len = int(0.3 * sr)  # 末尾过短片段跳过
        texts: list[str] = []
        n = waveform.shape[0]
        for start in range(0, n, chunk):
            seg = waveform[start : start + chunk]
            if seg.shape[0] < min_len:
                continue
            audio_input = {"array": seg, "sampling_rate": sr}
            try:
                result = self._asr_pipe(audio_input)
            except Exception as exc:  # noqa: BLE001
                logger.warning("分段转写失败 (start=%d): %s", start, exc)
                continue
            t = self._extract_text(result)
            if t:
                texts.append(t)
        return " ".join(texts).strip()

    @staticmethod
    def _extract_text(result: dict) -> str:
        """从 pipeline 结果里取文本，兼容 long-form 的 chunks 回退。"""
        text = (result.get("text") or "").strip()
        if not text and "chunks" in result:
            text = " ".join(c.get("text", "") for c in result["chunks"]).strip()
        return text

    @staticmethod
    def _is_long_audio(audio_path: str) -> bool:
        """粗略判断音频是否超过 30s（Whisper long-form 阈值）。无法判断时按 True 处理。"""
        try:
            import soundfile as sf

            info = sf.info(audio_path)
            return (info.frames / info.samplerate) > 30.0
        except Exception:  # noqa: BLE001
            return True
    
    def transcribe_with_timestamps(
        self,
        audio: Union[str, Tuple[np.ndarray, int], Tuple[torch.Tensor, int]],
        return_timestamps: bool = True,
    ) -> dict:
        """Transcribe audio with timestamps.
        
        Args:
            audio: Input audio (same format as transcribe method).
            return_timestamps: Whether to include timestamps in output.
        
        Returns:
            Dictionary containing 'text' and optionally 'chunks' with timestamps.
        """
        if self._asr_pipe is None:
            raise RuntimeError("ASR model is not loaded.")
        
        if isinstance(audio, str):
            result = self._asr_pipe(audio, return_timestamps=return_timestamps)
        else:
            waveform, sr = audio
            if isinstance(waveform, torch.Tensor):
                waveform = waveform.cpu().numpy()
            waveform = np.squeeze(waveform)
            
            audio_input = {
                "array": waveform,
                "sampling_rate": sr,
            }
            result = self._asr_pipe(audio_input, return_timestamps=return_timestamps)
        
        return result
    
    @property
    def is_loaded(self) -> bool:
        """Check if the ASR model is loaded."""
        return self._asr_pipe is not None


# Convenience function for quick transcription
def transcribe_audio(
    audio: Union[str, Tuple[np.ndarray, int], Tuple[torch.Tensor, int]],
    model_name: str = "openai/whisper-large-v3-turbo",
    device: Optional[str] = None,
) -> str:
    """Quick transcription function without class instantiation.
    
    Note: This creates a new ASR instance each time, which is inefficient
    for multiple transcriptions. Use the ASR class for batch processing.
    
    Args:
        audio: Input audio (file path or (waveform, sample_rate) tuple).
        model_name: Whisper model name.
        device: Device to use.
    
    Returns:
        Transcribed text string.
    """
    asr = ASR(model_name=model_name, device=device)
    return asr.transcribe(audio)


if __name__ == "__main__":
    # Example usage
    import sys
    
    if len(sys.argv) > 1:
        audio_path = sys.argv[1]
        print(f"Transcribing: {audio_path}")
        
        asr = ASR()
        text = asr.transcribe(audio_path)
        print(f"Transcription: {text}")
    else:
        print("Usage: python asr.py <audio_file_path>")
        print("\nExample:")
        print("  python asr.py audio.wav")