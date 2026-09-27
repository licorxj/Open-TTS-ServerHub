#!/usr/bin/env python3
"""Confucius4-TTS FastAPI service (vLLM backend).

Supports both streaming and non-streaming synthesis.  All models are
auto-downloaded via HuggingFace Hub — no manual setup needed.

Endpoints
---------
POST /api/tts                 non-streaming: returns a .wav file (multipart upload)
POST /api/tts/stream          streaming: raw PCM int16 chunks via chunked transfer
GET  /health                  health check

Usage
-----
    CUDA_VISIBLE_DEVICES=3 HF_ENDPOINT=https://hf-mirror.com \
    python server.py --port 8000

Client examples
---------------
# non-streaming
curl -F "text=你好" -F "lang=zh" -F "reference=@prompt.wav" \
     http://localhost:8000/api/tts --output out.wav

# streaming (raw int16 PCM, 1 channel, sample-rate in X-Sample-Rate header)
curl -F "text=你好" -F "lang=zh" -F "reference=@prompt.wav" \
     http://localhost:8000/api/tts/stream --output out.pcm
"""

import argparse
import asyncio
import io
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf
import torch
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

# Make the local package importable when run as a script.
sys.path.insert(0, str(Path(__file__).parent))

from confuciustts.cli.inference_vllm import ConfuciusTTSVLLM

# Supported language codes and request limits.
LANGUAGES = ("zh", "en", "ja", "ko", "de", "fr", "th", "id", "vi", "es", "pt", "it", "ru", "ms")
ALLOWED_AUDIO_EXTENSIONS = {".wav", ".flac", ".mp3", ".m4a", ".ogg"}
MAX_REFERENCE_AUDIO_BYTES = 50 * 1024 * 1024
MAX_TEXT_LENGTH = 1024


class AppState:
    """Holds the singleton model, loaded once at startup and shared by requests."""

    model: Optional[ConfuciusTTSVLLM] = None


state = AppState()


def _validate_audio(filename: Optional[str]) -> str:
    """Validate the upload's extension and return it (defaulting to .wav)."""
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_AUDIO_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_AUDIO_EXTENSIONS))
        raise HTTPException(status_code=400, detail=f"Unsupported audio type: {ext}. Allowed: {allowed}")
    return ext or ".wav"


async def _save_upload(upload: UploadFile) -> Path:
    """Stream an uploaded reference file to a temp path, enforcing the size cap."""
    ext = _validate_audio(upload.filename)
    path = Path("/tmp") / f"confucius4_upload_{uuid.uuid4().hex}{ext}"
    size = 0
    try:
        # Read in 1 MiB chunks so a huge upload can be rejected without buffering it all.
        with path.open("wb") as f:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_REFERENCE_AUDIO_BYTES:
                    raise HTTPException(status_code=413, detail="Reference audio too large")
                f.write(chunk)
        return path
    except HTTPException:
        path.unlink(missing_ok=True)  # Clean up the partial file on rejection.
        raise
    finally:
        await upload.close()


def _wav_bytes(audio: torch.Tensor, sample_rate: int) -> bytes:
    """Encode a waveform tensor as an in-memory 16-bit WAV file."""
    wav = audio.cpu().squeeze(0).numpy()
    buf = io.BytesIO()
    sf.write(buf, wav, sample_rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def _pcm_int16_bytes(audio: torch.Tensor) -> bytes:
    """Encode a waveform tensor as raw little-endian int16 PCM bytes."""
    wav = audio.cpu().squeeze(0).numpy()
    return (np.clip(wav, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def parse_args():
    parser = argparse.ArgumentParser(description="Confucius4-TTS FastAPI service (vLLM)")
    parser.add_argument("--config", default="config/inference_config.yaml",
                        help="Inference config YAML path.")
    parser.add_argument("--host", default="0.0.0.0", help="Server host.")
    parser.add_argument("--port", type=int, default=8000, help="Server port.")
    parser.add_argument("--gpu_memory_utilization", type=float, default=0.4,
                        help="vLLM GPU memory utilization (default 0.4).")
    return parser.parse_args()


def build_app() -> FastAPI:
    """Construct the FastAPI app and register its routes."""
    app = FastAPI(title="Confucius4-TTS", description="vLLM-accelerated multilingual TTS")

    @app.get("/health")
    async def health():
        # Readiness probe: "ok" once the model has finished loading.
        return {"status": "ok" if state.model is not None else "loading",
                "sample_rate": state.model.sample_rate if state.model else None}

    @app.post("/api/tts")
    async def tts(
        text: str = Form(...),
        lang: str = Form("zh"),
        reference: UploadFile = File(...),
    ):
        """Synthesize the whole utterance and return it as a single WAV file."""
        # Validate inputs before touching the model.
        if state.model is None:
            raise HTTPException(status_code=503, detail="Model not ready")
        if lang not in LANGUAGES:
            raise HTTPException(status_code=400, detail=f"Unsupported language: {lang}")
        if not text.strip():
            raise HTTPException(status_code=400, detail="text is required")
        if len(text) > MAX_TEXT_LENGTH:
            raise HTTPException(status_code=400, detail=f"text too long (max {MAX_TEXT_LENGTH})")

        # Persist the reference audio, synthesize, and always clean it up after.
        ref_path = await _save_upload(reference)
        try:
            t0 = time.time()
            audio = await state.model.generate(text, lang, str(ref_path), verbose=False)
            elapsed = time.time() - t0
            wav_bytes = _wav_bytes(audio, state.model.sample_rate)
            dur = audio.shape[-1] / state.model.sample_rate
            # Return the WAV with timing/metadata surfaced in response headers.
            return StreamingResponse(
                io.BytesIO(wav_bytes),
                media_type="audio/wav",
                headers={
                    "Content-Disposition": 'attachment; filename="output.wav"',
                    "X-Sample-Rate": str(state.model.sample_rate),
                    "X-Duration-Sec": f"{dur:.3f}",
                    "X-Elapsed-Sec": f"{elapsed:.3f}",
                },
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")
        finally:
            ref_path.unlink(missing_ok=True)

    @app.post("/api/tts/stream")
    async def tts_stream(
        text: str = Form(...),
        lang: str = Form("zh"),
        reference: UploadFile = File(...),
    ):
        """Stream synthesized audio as raw int16 PCM as soon as chunks are ready."""
        if state.model is None:
            raise HTTPException(status_code=503, detail="Model not ready")
        if lang not in LANGUAGES:
            raise HTTPException(status_code=400, detail=f"Unsupported language: {lang}")
        if not text.strip():
            raise HTTPException(status_code=400, detail="text is required")
        if len(text) > MAX_TEXT_LENGTH:
            raise HTTPException(status_code=400, detail=f"text too long (max {MAX_TEXT_LENGTH})")

        ref_path = await _save_upload(reference)
        model = state.model
        sample_rate = model.sample_rate

        async def _audio_stream():
            # Generator that yields PCM chunks; errors are reported inline in the
            # body, and the reference file is removed once streaming ends.
            try:
                async for chunk in model.generate_stream(
                    text, lang, str(ref_path), verbose=False,
                ):
                    yield _pcm_int16_bytes(chunk)
            except Exception as exc:
                yield f"\n[ERROR] {type(exc).__name__}: {exc}".encode()
            finally:
                ref_path.unlink(missing_ok=True)

        return StreamingResponse(
            _audio_stream(),
            media_type="application/octet-stream",
            headers={
                "X-Sample-Rate": str(sample_rate),
                "X-Channels": "1",
                "X-Encoding": "int16-LE-PCM",
            },
        )

    return app


def main():
    args = parse_args()
    # Load the model once up front; a single worker shares it across requests.
    state.model = ConfuciusTTSVLLM(
        config_path=args.config,
        gpu_memory_utilization=args.gpu_memory_utilization,
        device="cuda" if torch.cuda.is_available() else "cpu",
    )
    print(f"Loaded. sample_rate={state.model.sample_rate}")

    app = build_app()
    uvicorn.run(app, host=args.host, port=args.port, workers=1, log_level="info")


if __name__ == "__main__":
    main()
