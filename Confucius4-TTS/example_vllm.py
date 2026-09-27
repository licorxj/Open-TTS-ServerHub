#!/usr/bin/env python3
"""ConfuciusTTS example: zero-shot synthesis (vLLM backend).

Same as ``example.py`` but uses the vLLM-accelerated ``ConfuciusTTSVLLM`` and
supports streaming output via ``--stream``.

Usage:
    python example_vllm.py --prompt_wav path/to/reference.wav \
                           --text "Your text here" --lang en --out output_vllm.wav
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

import torch
import soundfile as sf

# Make the local package importable when run as a script.
sys.path.insert(0, str(Path(__file__).parent))

from confuciustts.cli.inference_vllm import ConfuciusTTSVLLM


DEFAULT_TEXT = (
    "支持多种语言，轻松实现跨语种朗读。"
)


def parse_args():
    parser = argparse.ArgumentParser(description="ConfuciusTTS zero-shot synthesis (vLLM)")
    parser.add_argument("--config", default="config/inference_config.yaml",
                        help="Inference config YAML path.")
    parser.add_argument("--prompt_wav", required=True,
                        help="Reference voice .wav for zero-shot cloning.")
    parser.add_argument("--text", default=DEFAULT_TEXT,
                        help="Text to synthesize.")
    parser.add_argument("--lang", default="zh",
                        help="Language code (e.g. zh, en, ja, th).")
    parser.add_argument("--out", default="output_vllm.wav",
                        help="Output .wav path.")
    parser.add_argument("--gpu_memory_utilization", type=float, default=0.4,
                        help="vLLM GPU memory utilization (default 0.4).")
    parser.add_argument("--stream", action="store_true",
                        help="Use streaming generation (model.generate_stream).")
    return parser.parse_args()


async def main():
    args = parse_args()
    model = ConfuciusTTSVLLM(
        config_path=args.config,
        gpu_memory_utilization=args.gpu_memory_utilization,
        device="cuda" if torch.cuda.is_available() else "cpu",
    )
    print(f"Loaded. sample_rate={model.sample_rate}")

    t0 = time.time()
    n_chunks = 0
    if args.stream:
        # Streaming: collect the chunks as they arrive and concatenate them.
        chunks = []
        async for chunk in model.generate_stream(
            args.text, args.lang, args.prompt_wav, verbose=True,
        ):
            chunks.append(chunk)
            n_chunks += 1
        audio = torch.cat(chunks, dim=1)
        print(f"Streamed {n_chunks} chunks Generated in {time.time() - t0:.3f}s, "
              f"shape={tuple(audio.shape)}")
    else:
        # One-shot generation of the full waveform.
        audio = await model.generate(args.text, args.lang, args.prompt_wav, verbose=True)
        print(f"Generated in {time.time() - t0:.3f}s, shape={tuple(audio.shape)}")

    wav = audio.cpu().squeeze(0).numpy()
    sf.write(args.out, wav, model.sample_rate)
    print(f"Saved {args.out} ({wav.shape[-1] / model.sample_rate:.3f}s)")


if __name__ == "__main__":
    asyncio.run(main())
