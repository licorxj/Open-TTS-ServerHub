#!/usr/bin/env python3
"""
Local deployment entry point for OmniVoice demo.

Usage:
    python webuiapp.py [--device cpu|cuda] [--port 7860] [--share]
"""

import argparse
import logging
import os
import shutil
import sys
import time
import types
from datetime import datetime
from typing import Any, Dict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
)
logging.getLogger("omnivoice").setLevel(logging.DEBUG)
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Force Transformers ASR audio decoding to use librosa/soundfile on Windows.
_torchcodec_core_mock = types.ModuleType("torchcodec._core")
_torchcodec_core_mock.ops = types.SimpleNamespace()
_torchcodec_core_mock.__all__ = ["ops", "VideoDecoder"]
_torchcodec_core_mock.VideoDecoder = type("VideoDecoder", (), {})
sys.modules["torchcodec._core"] = _torchcodec_core_mock
sys.modules["torchcodec._core.ops"] = _torchcodec_core_mock.ops


def _disable_transformers_torchcodec():
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

import numpy as np
import soundfile as sf
import torch
from omnivoice import OmniVoice, OmniVoiceGenerationConfig
from omnivoice.cli.demo import build_demo

#将上级文件夹添加到sys.path


# ---------------------------------------------------------------------------
# Parse command line arguments
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser(description="OmniVoice Local Demo")
parser.add_argument("--device", type=str, default=None,
                    help="Device to use: 'cpu' or 'cuda' (default: auto-detect)")
parser.add_argument("--port", type=int, default=8852,
                    help="Port to run the demo on (default: 8852)")
parser.add_argument("--share", action="store_true",
                    help="Create a public shareable link")
parser.add_argument("--model", type=str, default='models/omnivoice',
                    help="Model path or name (default: k2-fsa/OmniVoice)")
args = parser.parse_args()

# ---------------------------------------------------------------------------
# Device selection
# ---------------------------------------------------------------------------
if args.device:
    device = args.device
else:
    device = "cuda" if torch.cuda.is_available() else "cpu"

print(f"Using device: {device}")
if device == "cuda" and not torch.cuda.is_available():
    print("Warning: CUDA selected but not available, falling back to CPU")
    device = "cpu"

# Set dtype based on device
dtype = torch.float16 if device == "cuda" else torch.float32

# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.models_manager import ModelsManager

mgr = ModelsManager()
CHECKPOINT = mgr.resolve_path(args.model or "models/omnivoice", model_type="omnivoice")

print(f"Loading model from {CHECKPOINT} to {device} ...")
try:
    model = OmniVoice.from_pretrained(
        CHECKPOINT,
        device_map=device,
        dtype=dtype,
        load_asr=True,
    )
    sampling_rate = model.sampling_rate
    print("Model loaded successfully!")
except Exception as e:
    print(f"Error loading model: {e}")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Generation logic
# ---------------------------------------------------------------------------


def _gen_core(
    text,
    language,
    ref_audio,
    instruct,
    num_step,
    guidance_scale,
    denoise,
    speed,
    duration,
    preprocess_prompt,
    postprocess_output,
    mode,
    ref_text=None,
):
    if not text or not text.strip():
        return None, "Please enter the text to synthesize."

    gen_config = OmniVoiceGenerationConfig(
        num_step=int(num_step or 32),
        guidance_scale=float(guidance_scale) if guidance_scale is not None else 2.0,
        denoise=bool(denoise) if denoise is not None else True,
        preprocess_prompt=bool(preprocess_prompt),
        postprocess_output=bool(postprocess_output),
    )

    lang = language if (language and language != "Auto") else None

    kw: Dict[str, Any] = dict(
        text=text.strip(), language=lang, generation_config=gen_config
    )

    if speed is not None and float(speed) != 1.0:
        kw["speed"] = float(speed)
    if duration is not None and float(duration) > 0:
        kw["duration"] = float(duration)

    if mode == "clone":
        if not ref_audio:
            return None, "Please upload a reference audio."
        kw["voice_clone_prompt"] = model.create_voice_clone_prompt(
            ref_audio=ref_audio,
            ref_text=ref_text,
        )

    if instruct and instruct.strip():
        kw["instruct"] = instruct.strip()

    try:
        audio = model.generate(**kw)
    except Exception as e:
        return None, f"Error: {type(e).__name__}: {e}"

    if isinstance(audio, list) and len(audio) > 0:
        waveform = audio[0]
    else:
        waveform = audio

    if isinstance(waveform, torch.Tensor):
        waveform = waveform.detach().cpu().numpy()

    waveform = np.asarray(waveform)
    if waveform.ndim == 3 and waveform.shape[0] == 1:
        waveform = waveform.squeeze(0)
    if waveform.ndim == 2 and waveform.shape[0] == 1:
        waveform = waveform.squeeze(0)
    if waveform.ndim != 1:
        return None, f"Error: unexpected waveform shape {waveform.shape}"

    waveform = np.clip(waveform, -1.0, 1.0)
    waveform = (waveform * 32767).astype(np.int16)
    return (sampling_rate, waveform), "Done."


# ---------------------------------------------------------------------------
# Generation function (no GPU wrapper needed for local deployment)
# ---------------------------------------------------------------------------


def generate_fn(*args, **kwargs):
    return _gen_core(*args, **kwargs)


# ---------------------------------------------------------------------------
# Build and launch demo
# ---------------------------------------------------------------------------
demo = build_demo(model, CHECKPOINT, generate_fn=generate_fn)

if __name__ == "__main__":
    print(f"\nStarting OmniVoice demo on http://localhost:{args.port}")
    if args.share:
        print("Creating public shareable link...")
    demo.queue().launch(server_name="localhost", server_port=args.port, share=args.share,inbrowser=True)
