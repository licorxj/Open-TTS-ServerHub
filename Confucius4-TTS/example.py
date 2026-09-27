#!/usr/bin/env python3
"""ConfuciusTTS example: zero-shot synthesis.

Usage:
    python example.py --prompt_wav path/to/reference.wav \
                      --text "Your text here" --lang en --out output.wav
"""

import argparse
import sys
import time
from pathlib import Path

import torch
import torchaudio

# TODO: drop once pyproject.toml lands and the package is pip-installable.
sys.path.insert(0, str(Path(__file__).parent))

from confuciustts.cli.inference import ConfuciusTTS


DEFAULT_TEXT = (
    "支持多种语言，轻松实现跨语种朗读。"
)


def parse_args():
    parser = argparse.ArgumentParser(description="ConfuciusTTS zero-shot synthesis")
    parser.add_argument("--config", default="config/inference_config.yaml",
                        help="Inference config YAML path.")
    parser.add_argument("--prompt_wav", required=True,
                        help="Reference voice .wav for zero-shot cloning.")
    parser.add_argument("--text", default=DEFAULT_TEXT,
                        help="Text to synthesize.")
    parser.add_argument("--lang", default="zh",
                        help="Language code (e.g. zh, en, ja, th).")
    parser.add_argument("--out", default="output.wav",
                        help="Output .wav path.")
    return parser.parse_args()


def main():
    args = parse_args()
    model = ConfuciusTTS(
        config_path=args.config,
        device="cuda" if torch.cuda.is_available() else "cpu",
    )
    print(f"Loaded. sample_rate={model.sample_rate}")

    t0 = time.time()
    audio = model.generate(args.text, args.lang, args.prompt_wav, verbose=True)
    print(f"Generated in {time.time() - t0:.3f}s, shape={tuple(audio.shape)}")

    torchaudio.save(args.out, audio.cpu(), model.sample_rate)
    print(f"Saved {args.out} ({audio.shape[-1] / model.sample_rate:.3f}s)")


if __name__ == "__main__":
    main()
