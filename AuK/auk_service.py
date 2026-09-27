#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AuK 服务层（无 Gradio 依赖的编程接口）。

- 模型统一经 tools.models_manager 管理：AuK / AuK-Flash / Qwen2.5-Omni-3B
  缺失时通过魔搭（modelscope）自动下载，集中存放于 models/ 下。
- 复用 AuK 自带的推理入口：
    * auk.infer.infer_gradio.run_generate            —— 指令式生成（合成/编辑/修复）
    * auk.infer.pe.PromptEnhancer                   —— 口语指令 -> 标准化指令（含 18 类任务）
- ASR 复用本项目的 tools/asr.py（Whisper），不依赖腾讯云/ SenseVoice 额外模型。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

logger = logging.getLogger("auk_service")

PROJECT_ROOT = Path(__file__).resolve().parent.parent  # LcTTSHub 根目录
AUK_DIR = Path(__file__).resolve().parent               # LcTTSHub/AuK
SRC_DIR = AUK_DIR / "src"

for _p in (str(AUK_DIR), str(SRC_DIR), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ----------------------------------------------------------------------------
# 与 app.py 一致的音频 I/O 门面（适配新版 TorchAudio 的 info/load 行为）
# ----------------------------------------------------------------------------
class SpaceAudioIO:
    """保留 AuK 上游的音频契约：用 SoundFile 读写 PCM16 WAV，transform 走 TorchAudio。"""

    def __getattr__(self, name):
        import torchaudio
        return getattr(torchaudio, name)

    @staticmethod
    def info(path):
        import soundfile as sf
        try:
            info = sf.info(path)
        except sf.LibsndfileError:
            import torchaudio
            audio, sample_rate = torchaudio.load(path)
            return type("Info", (), {
                "sample_rate": sample_rate,
                "num_frames": audio.shape[-1],
                "num_channels": audio.shape[0],
            })()
        return type("Info", (), {
            "sample_rate": info.samplerate,
            "num_frames": info.frames,
            "num_channels": info.channels,
        })()

    @staticmethod
    def load(path):
        import soundfile as sf
        import torch
        try:
            samples, sample_rate = sf.read(path, dtype="float32", always_2d=True)
        except sf.LibsndfileError:
            import torchaudio
            return torchaudio.load(path)
        return torch.from_numpy(samples.T.copy()), sample_rate

    @staticmethod
    def save(path, audio, sample_rate, *, encoding=None, bits_per_sample=None):
        import soundfile as sf
        import torch
        if encoding not in (None, "PCM_S") or bits_per_sample not in (None, 16):
            raise ValueError("AuK 音频适配器仅支持 PCM16 WAV 输出。")
        samples = audio.detach().to(dtype=torch.float32, device="cpu")
        if samples.ndim != 2:
            raise ValueError("期望 [channels, samples] 形状。")
        sf.write(path, samples.T.numpy(), sample_rate, format="WAV", subtype="PCM_16")


# ----------------------------------------------------------------------------
# ASR 适配器：复用现有 tools/asr.py（Whisper），对接 pe.ASRProvider 协议
# ----------------------------------------------------------------------------
def _detect_language(text: str | None) -> str | None:
    if not text:
        return None
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            return "zh"
    return "en"


class WhisperASRProvider:
    """用项目已有的 tools/asr.py 做本地 ASR，返回 pe.ASRCall 兼容对象。"""

    def __init__(self, model_name: str | None = None, device: str | None = None):
        from tools.asr import ASR  # 复用现成 ASR 代码
        self._model_name = model_name or "openai/whisper-large-v3-turbo"
        self._asr = ASR(model_name=self._model_name, device=device)

    def transcribe(self, audio_path: str):
        from auk.infer.pe import ASRCall
        try:
            text = self._asr.transcribe(audio_path)
            return ASRCall(
                model=f"whisper:{self._model_name}",
                text=text,
                language=_detect_language(text),
                raw_response={},
                error=None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Whisper ASR 失败: %s", exc)
            return ASRCall(
                model=f"whisper:{self._model_name}",
                text=None,
                language=None,
                raw_response={},
                error=str(exc),
            )


# ----------------------------------------------------------------------------
# 引擎构建（等价于 app.py 的 create_demo，但不启动 Gradio）
# ----------------------------------------------------------------------------
_BUILD_LOCK = threading.Lock()
_ENGINES_BUILT = False


def _resolve_model_dirs():
    """经统一模型管理解析 AuK / AuK-Flash / Qwen2.5-Omni-3B 本地目录。"""
    from tools.models_manager import ModelsManager
    mm = ModelsManager(str(PROJECT_ROOT))
    return {
        "auk": mm.get_model_path("auk"),
        "auk_flash": mm.get_model_path("auk_flash"),
        "qwen_omni": mm.get_model_path("qwen_omni"),
    }


def build_engines(
    device: str = "cuda",
    dtype: str = "bf16",
    variants: list[str] | None = None,
    text_encoder_device: str | None = None,
) -> None:
    """下载（如需要）并构建 AuK 引擎，填充 gradio_app 的全局表。

    ``variants`` 限定本次启动要构建的变体（标签集合，如 ["AuK (Base)"]）。
    默认构建全部（Base + Flash）。两引擎合计约需 ~18GB 显存，在 16GB 显卡上
    无法同时驻留；受显存限制时可只构建单个变体，其余变体会在首次请求时按需构建
    （见 ``auk.infer.infer_gradio.get_engine``）。
    """
    global _ENGINES_BUILT
    with _BUILD_LOCK:
        if _ENGINES_BUILT:
            return
        if not (device or "cuda").startswith("cuda") and not _cuda_ok():
            logger.warning("AuK 需要 GPU，当前未检测到 CUDA，将尝试在 CPU 上构建（可能很慢/失败）。")
            device = "cpu" if device == "auto" else device

        from auk.infer import infer_auk, infer_gradio as gradio_app, pe
        from auk.infer.infer_auk import AukInfer

        # 适配新版 TorchAudio 的音频契约
        audio_io = SpaceAudioIO()
        infer_auk.torchaudio = audio_io
        pe.torchaudio = audio_io

        dirs = _resolve_model_dirs()
        qwen_dir = dirs["qwen_omni"]

        # (标签, 模型目录, checkpoint 文件名)
        all_specs = [
            (gradio_app.BASE_LABEL, dirs["auk"], "auk_base.safetensors"),
            (gradio_app.FLASH_LABEL, dirs["auk_flash"], "auk_flash.safetensors"),
        ]
        if variants:
            wanted = set(variants)
            specs = [s for s in all_specs if s[0] in wanted]
        else:
            specs = list(all_specs)

        gradio_app.CKPT_PATHS.clear()
        gradio_app.CONFIG_PATHS.clear()
        gradio_app.ENGINES.clear()

        for label, model_dir, ckpt_name in specs:
            ckpt = os.path.join(model_dir, ckpt_name)
            cfg = os.path.join(model_dir, "config.yaml")
            if not os.path.isfile(ckpt) or not os.path.isfile(cfg):
                raise FileNotFoundError(f"缺少 AuK 模型文件: {ckpt} / {cfg}")
            logger.info("构建 %s 引擎 ...", label)
            engine = AukInfer(
                config_path=cfg, ckpt_path=ckpt,
                qwen_path=qwen_dir, device=device, dtype=dtype,
                text_encoder_device=text_encoder_device,
            )
            gradio_app.CKPT_PATHS[label] = ckpt
            gradio_app.CONFIG_PATHS[label] = cfg
            gradio_app.ENGINES[label] = engine

        if not gradio_app.ENGINES:
            raise RuntimeError(
                f"未构建任何 AuK 引擎，请检查 variants 配置: {variants}"
            )

        _ENGINES_BUILT = True
        logger.info("AuK 引擎构建完成: %s", list(gradio_app.ENGINES.keys()))


def _cuda_ok() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:
        return False


# ----------------------------------------------------------------------------
# 对外生成接口
# ----------------------------------------------------------------------------
def generate(
    variant: str,
    audio: str | None,
    instruction: str,
    gen_seconds: float | None = None,
    ref_text: str | None = None,
    gen_text: str | None = None,
    nfe: int = 32,
    cfg: float = 2.0,
    seed: int | None = None,
    task_type: str | None = None,
):
    """指令式生成，返回 (sample_rate, pcm16 ndarray)。"""
    from auk.infer.infer_gradio import run_generate
    return run_generate(
        variant, audio, instruction, gen_seconds, ref_text, gen_text,
        nfe, cfg, seed, task_type,
    )


def generate_with_pe(
    variant: str,
    audio: str | None,
    instruction: str,
    *,
    use_pe: bool = True,
    gen_seconds: float | None = None,
    ref_text: str | None = None,
    gen_text: str | None = None,
    nfe: int = 32,
    cfg: float = 2.0,
    seed: int | None = None,
    llm_api_key: str | None = None,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    asr_provider=None,
):
    """口语指令经 Prompt Enhancer 转为标准指令后生成。

    返回 (audio, metadata)：
      audio     = (sample_rate, pcm16 ndarray)
      metadata  = dict | None（含 task / duration / asr_content / instruction）
    """
    from auk.infer.infer_gradio import run_generate
    from auk.infer.pe import PromptEnhancer, PromptEnhancerError

    if asr_provider is None:
        asr_provider = WhisperASRProvider()

    prepared = None
    try:
        requested = float(gen_seconds or 0)
        enhancer = PromptEnhancer(
            llm_api_key=llm_api_key,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            asr_provider=asr_provider,
        )
        prepared = enhancer.prepare(
            instruction,
            audio,
            target_duration=requested if requested > 0 else None,
        )
        effective_duration = requested if requested > 0 else prepared.gen_seconds
        generated = run_generate(
            variant,
            prepared.audio,
            prepared.instruction,
            effective_duration,
            prepared.ref_text,
            prepared.gen_text,
            nfe,
            cfg,
            seed,
            prepared.task_type,
        )
        task = str(getattr(prepared, "task_type", ""))
        op = getattr(prepared, "operation_subtype", None)
        if op:
            task = f"{task} / {op}"
        asr = getattr(prepared, "asr", None)
        asr_text = asr.text if asr and asr.text else None
        metadata = {
            "task": task,
            "duration": f"{effective_duration:.2f} s",
            "asr_content": asr_text or "",
            "instruction": prepared.instruction,
        }
        return generated, metadata
    except (PromptEnhancerError, ValueError, FileNotFoundError) as exc:
        raise RuntimeError(f"Prompt Enhancer 处理失败: {type(exc).__name__}: {exc}") from exc
    finally:
        if prepared is not None:
            prepared.cleanup()


def list_supported_tasks() -> list[dict]:
    """读取 pe.config.yaml，列出 AuK 支持的任务类型。"""
    import yaml
    cfg_path = SRC_DIR / "auk" / "infer" / "pe.config.yaml"
    if not cfg_path.is_file():
        return []
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    tasks = data.get("tasks", {})
    out = []
    for name, spec in tasks.items():
        out.append({
            "task_type": name,
            "name": spec.get("name"),
            "needs_audio": spec.get("needs_audio"),
            "needs_text": spec.get("needs_text"),
            "subtypes": spec.get("subtypes"),
        })
    return out


VARIANTS = ["AuK (Base)", "AuK-Flash ⚡"]
