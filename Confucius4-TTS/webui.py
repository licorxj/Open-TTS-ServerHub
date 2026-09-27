#!/usr/bin/env python3
"""Confucius4-TTS Gradio WebUI.

A minimal web UI wrapping ConfuciusTTSVLLM.generate().  All models are
auto-downloaded via HuggingFace Hub — no manual setup needed.

Usage:
    python webui.py --port 8000
    # with env overrides:
    python webui.py --port 8000
"""

import argparse
import asyncio
import sys
import time
import uuid
from pathlib import Path
import tempfile

import gradio as gr
import soundfile as sf
import torch

# Make the local package importable when run as a script.
sys.path.insert(0, str(Path(__file__).parent))

from confuciustts.cli.inference_vllm import ConfuciusTTSVLLM


LANGUAGES = ("zh", "en", "ja", "ko", "de", "fr", "th", "id", "vi", "es", "pt", "it", "ru", "ms")
DEFAULT_TEXT = "支持多种语言，轻松实现跨语种朗读。"


def parse_args():
    parser = argparse.ArgumentParser(description="Confucius4-TTS Gradio WebUI (vLLM)")
    parser.add_argument("--config", default="config/inference_config.yaml",
                        help="Inference config YAML path.")
    parser.add_argument("--port", type=int, default=7860,
                        help="Gradio server port.")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Gradio server host.")
    parser.add_argument("--gpu_memory_utilization", type=float, default=0.4,
                        help="vLLM GPU memory utilization (default 0.4).")
    return parser.parse_args()


def _save_audio_to_temp(audio_tensor: torch.Tensor, sample_rate: int) -> str:
    """Write the generated waveform to a temp WAV and return its path for Gradio."""
    wav = audio_tensor.cpu().squeeze(0).numpy()
    out = Path(tempfile.gettempdir()) / f"confucius4_webui_{uuid.uuid4().hex}.wav"
    sf.write(str(out), wav, sample_rate)
    return str(out)


def build_demo(model: ConfuciusTTSVLLM):
    """Build the Gradio Blocks UI and return it together with its event loop."""
    # Dedicated event loop to run the async model from sync Gradio callbacks.
    loop = asyncio.new_event_loop()

    async def _predict(text: str, language: str, reference_audio):
        # Validate the form inputs; return a Chinese status message on failure.
        if not text or not text.strip():
            return None, "失败：请输入待合成文本。"
        if not reference_audio:
            return None, "失败：请上传参考音频。"
        ref_path = reference_audio if isinstance(reference_audio, str) else None
        if not ref_path or not Path(ref_path).exists():
            return None, "失败：参考音频文件无效，请重新上传。"
        try:
            t0 = time.time()
            audio = await model.generate(
                text, language, ref_path, verbose=False,
            )
            elapsed = time.time() - t0
            out_path = _save_audio_to_temp(audio, model.sample_rate)
            dur = audio.shape[-1] / model.sample_rate
            return out_path, f"成功：合成完成。"
        except Exception as exc:
            return None, f"失败：{type(exc).__name__}: {exc}"

    def predict(text, language, reference_audio):
        # Sync wrapper Gradio calls: drive the async _predict to completion.
        return loop.run_until_complete(
            _predict(text, language, reference_audio)
        )

    # Layout: inputs (text / language / reference) on the left, outputs on the right.
    theme = gr.themes.Soft(font=["Inter", "Arial", "sans-serif"])
    css = """
    .gradio-container {max-width: 100% !important; font-size: 16px !important;}
    .compact-audio audio {height: 60px !important;}
    .compact-audio .waveform {min-height: 80px !important;}
    """
    with gr.Blocks(theme=theme, css=css, title="Confucius4-TTS") as demo:
        gr.Markdown("# Confucius4-TTS")
        with gr.Row():
            with gr.Column(scale=1):
                text = gr.Textbox(
                    label="Text to Synthesize / 待合成文本",
                    lines=4,
                    value=DEFAULT_TEXT,
                    placeholder="请输入待合成文本...",
                )
                language = gr.Dropdown(
                    label="Language / 语种",
                    choices=list(LANGUAGES),
                    value="zh",
                    interactive=True,
                )
                reference_audio = gr.Audio(
                    label="Reference Audio / 参考音频",
                    type="filepath",
                    elem_classes="compact-audio",
                )
                gr.Markdown(
                    "<span style='font-size:0.85em;color:#888;'>"
                    "建议上传 3–10 秒参考音频。"
                    "</span>"
                )
                button = gr.Button("Generate / 生成", variant="primary")
            with gr.Column(scale=1):
                output = gr.Audio(label="Output Audio / 合成结果", type="filepath")
                status = gr.Textbox(label="Status / 状态", lines=2, interactive=False)
        button.click(
            predict,
            inputs=[text, language, reference_audio],
            outputs=[output, status],
        )
    return demo, loop


def main():
    args = parse_args()
    # Load the model once, then hand it to the UI builder.
    model = ConfuciusTTSVLLM(
        config_path=args.config,
        gpu_memory_utilization=args.gpu_memory_utilization,
        device="cuda" if torch.cuda.is_available() else "cpu",
    )
    print(f"Loaded. sample_rate={model.sample_rate}")

    demo, loop = build_demo(model)
    demo.queue().launch(server_name=args.host, server_port=args.port)


if __name__ == "__main__":
    main()
