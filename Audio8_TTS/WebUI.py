#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Audio8_TTS WebUI

基于 Gradio 的图形界面，暴露 Audio8_TTS 的全部能力：
  - 纯文本合成 / 参考音频 + 参考文本（音色克隆）
  - 采样参数：temperature / top_p / top_k / seed / greedy
  - 解码长度：max_new_tokens / retry_max_new_tokens
  - 设备与精度：device / dtype
  - 保存声学 token：save_codes
  - 批量合成（JSONL，每行可独立配置）

启动：
    python Audio8_TTS/WebUI.py --device auto --dtype auto
或双击 "Audio8 WebUI.bat"
"""

from __future__ import annotations

import argparse
import sys
import uuid
import warnings
from pathlib import Path

import threading
from contextlib import contextmanager

import yaml

import gradio as gr
import soundfile as sf
import torch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUDIO8_DIR = PROJECT_ROOT / "Audio8_TTS"
DEFAULT_MODEL = PROJECT_ROOT / "models" / "Audio8"
UPLOAD_DIR = PROJECT_ROOT / "uploads"
OUTPUT_DIR = PROJECT_ROOT / "outputs" / "audio8_tts"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ----------------------------------------------------------------------------
# 服务器配置（统一由 config/audio8_webui.yaml 提供，文件缺失回退内置默认）
# ----------------------------------------------------------------------------
WEBUI_CONFIG_PATH = PROJECT_ROOT / "config" / "audio8_webui.yaml"

DEFAULT_WEBUI_CONFIG = {
    "server": {
        "host": "0.0.0.0",
        "port": 7868,
        "device": "auto",
        "dtype": "auto",
        "share": False,
        "concurrency_limit": 2,
        "gpu_concurrency": 1,
    }
}


def load_webui_config():
    """加载 config/audio8_webui.yaml，缺失/异常时回退到默认配置。"""
    cfg = {k: dict(v) for k, v in DEFAULT_WEBUI_CONFIG.items()}
    if WEBUI_CONFIG_PATH.is_file():
        try:
            with open(WEBUI_CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = yaml.safe_load(f) or {}
            for section, values in user_cfg.items():
                if isinstance(values, dict) and section in cfg:
                    cfg[section].update(values)
        except Exception as e:
            print(f"[WARN] 读取 {WEBUI_CONFIG_PATH} 失败，使用内置默认配置: {e}")
    return cfg


CONFIG = load_webui_config()


# 并发控制全局对象（在 __main__ 启动前初始化）
_gpu_semaphore = None       # GPU 并发信号量（串行化 GPU 推理，避免多用户/批量并发抢卡）
_model_load_lock = threading.Lock()  # 惰性加载模型的重入保护


@contextmanager
def _gpu_lock():
    """GPU 临界区：若已初始化信号量则获取，否则直接放行。"""
    if _gpu_semaphore is None:
        yield
    else:
        _gpu_semaphore.acquire()
        try:
            yield
        finally:
            _gpu_semaphore.release()

sys.path.insert(0, str(AUDIO8_DIR))

from audio8_tts_infer import resolve_device, resolve_dtype, _patch_mistral_regex_safe  # noqa: E402
from tools.models_manager import ModelsManager  # noqa: E402

models_manager = ModelsManager()

_STATE: dict = {"model": None, "processor": None, "sample_rate": None, "model_path": None}


def ensure_model(model_name: str = "audio8") -> Path:
    """确保模型统一存放在 models/Audio8（不存在则自动下载）。"""
    try:
        return Path(models_manager.get_model_path(model_name))
    except Exception as e:
        print(f"[audio8] 确保模型 '{model_name}' 失败: {e}")
        return DEFAULT_MODEL


def load_model(model_path: Path, device, dtype):
    if _STATE["model"] is not None and _STATE["model_path"] == model_path:
        return
    with _model_load_lock:
        # 二次检查（double-checked locking），避免并发首请求重复加载
        if _STATE["model"] is not None and _STATE["model_path"] == model_path:
            return
        from transformers import AutoModel, AutoProcessor

    _patch_mistral_regex_safe()

    # 模型/处理器内部会调用已被弃用的 torch.nn.utils.weight_norm，
    # 其 FutureWarning 无实际影响，加载期间静默过滤避免刷屏。
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"`torch\.nn\.utils\.weight_norm` is deprecated",
            category=FutureWarning,
        )
        processor = AutoProcessor.from_pretrained(model_path, trust_remote_code=True)
        model = AutoModel.from_pretrained(model_path, trust_remote_code=True, dtype=dtype)
        model = model.eval().to(device)
    _STATE.update(
        {
            "model": model,
            "processor": processor,
            "sample_rate": int(model.config.codec_sample_rate),
            "model_path": model_path,
        }
    )


def gen(
    text,
    ref_audio,
    ref_text,
    device,
    dtype,
    max_new_tokens,
    retry_max_new_tokens,
    temperature,
    top_p,
    top_k,
    seed,
    greedy,
    save_codes,
):
    if not text or not text.strip():
        raise ValueError("请输入要合成的文本")
    if (ref_audio is None) != (ref_text in (None, "")):
        raise ValueError("参考音频与参考文本必须同时提供")
    dev = resolve_device(device)
    dt = resolve_dtype(dtype, dev)
    load_model(ensure_model(), dev, dt)
    model = _STATE["model"]
    processor = _STATE["processor"]
    sample_rate = _STATE["sample_rate"]
    generator = torch.Generator(device=dev).manual_seed(int(seed))

    processor_kwargs = {"text": [text], "return_tensors": "pt"}
    if ref_audio is not None:
        ref_path = Path(ref_audio)
        # 若是 Gradio 临时上传文件，先落盘到 uploads
        if not ref_path.is_absolute() or not ref_path.is_file():
            dst = UPLOAD_DIR / f"ref_{uuid.uuid4().hex}.wav"
            with dst.open("wb") as f:
                f.write(Path(ref_audio).read_bytes())
            ref_path = dst
        processor_kwargs.update(
            reference_audio=[str(ref_path)], reference_text=[ref_text or ""]
        )
    inputs = processor(**processor_kwargs)
    inputs = {k: v.to(dev) for k, v in inputs.items()}
    with _gpu_lock():
        out = model.generate(
            **inputs,
            max_new_tokens=int(max_new_tokens),
            temperature=float(temperature),
            top_p=float(top_p),
            top_k=int(top_k),
            do_sample=not greedy,
            generator=generator,
            return_dict_in_generate=True,
        )
        waveforms, lengths = model.decode_audio(out.codes)
    wav = waveforms[0, : int(lengths[0])].float().cpu().numpy()
    out_path = OUTPUT_DIR / f"{uuid.uuid4().hex}.wav"
    sf.write(out_path, wav, sample_rate)
    info = f"状态: {'OK' if bool(out.finished[0]) else 'NO_EOS'} | 采样率: {sample_rate} | 时长: {len(wav)/sample_rate:.2f}s"
    if save_codes:
        import numpy as np

        np.save(out_path.with_suffix(".npy"), out.codes[0, :, : int(out.code_lengths[0])].cpu().numpy())
        info += " | 已保存 codes(.npy)"
    return str(out_path), info


def batch_gen(jsonl_text, device, dtype, max_new_tokens, temperature, top_p, top_k, seed, greedy, save_codes):
    if not jsonl_text.strip():
        raise ValueError("请粘贴 JSONL 内容")
    import json

    rows = []
    for i, line in enumerate(jsonl_text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise ValueError(f"第 {i+1} 行不是合法 JSON: {e}")
    dev = resolve_device(device)
    dt = resolve_dtype(dtype, dev)
    load_model(ensure_model(), dev, dt)
    model = _STATE["model"]
    processor = _STATE["processor"]
    sample_rate = _STATE["sample_rate"]

    results = []
    for row in rows:
        text = row.get("text")
        ref = row.get("reference_audio")
        ref_text = row.get("reference_text")
        if (ref is None) != (ref_text is None):
            results.append({"text": text, "status": "ERROR", "error": "reference_audio 与 reference_text 必须同时提供"})
            continue
        try:
            generator = torch.Generator(device=dev).manual_seed(int(seed))
            processor_kwargs = {"text": [text], "return_tensors": "pt"}
            if ref is not None:
                processor_kwargs.update(reference_audio=[str(Path(ref))], reference_text=[ref_text])
            inputs = processor(**processor_kwargs)
            inputs = {k: v.to(dev) for k, v in inputs.items()}
            with _gpu_lock():
                out = model.generate(
                    **inputs,
                    max_new_tokens=int(max_new_tokens),
                    temperature=float(temperature),
                    top_p=float(top_p),
                    top_k=int(top_k),
                    do_sample=not greedy,
                    generator=generator,
                    return_dict_in_generate=True,
                )
                waveforms, lengths = model.decode_audio(out.codes)
            wav = waveforms[0, : int(lengths[0])].float().cpu().numpy()
            out_path = OUTPUT_DIR / f"{uuid.uuid4().hex}.wav"
            sf.write(out_path, wav, sample_rate)
            results.append(
                {
                    "text": text,
                    "status": "OK" if bool(out.finished[0]) else "NO_EOS",
                    "output": str(out_path),
                }
            )
        except Exception as e:  # noqa: BLE001
            results.append({"text": text, "status": "ERROR", "error": str(e)})
    md = "## 批量结果\n\n"
    for r in results:
        if r["status"] == "OK" or r["status"] == "NO_EOS":
            md += f"- ✅ {r['status']}: `{r['text'][:30]}...` → {r['output']}\n"
        else:
            md += f"- ❌ ERROR: `{str(r.get('text'))[:30]}` → {r.get('error')}\n"
    return md


# ---------- 界面 ----------
css = """
#title { text-align: center; margin-bottom: 10px; }
.param-row { gap: 8px; }
"""

with gr.Blocks(title="Audio8_TTS WebUI") as demo:
    gr.Markdown("# Audio8_TTS WebUI\n多模态 TTS（文本 / 参考音频音色克隆）", elem_id="title")

    with gr.Tabs():
        with gr.TabItem("文本合成 / 音色克隆"):
            with gr.Row():
                with gr.Column(scale=2):
                    text = gr.Textbox(
                        label="合成文本 (text)",
                        placeholder="请输入要合成语音的文本……",
                        lines=4,
                    )
                    ref_audio = gr.Audio(
                        label="参考音频 (可选，用于音色克隆)",
                        sources=["upload", "microphone"],
                        type="filepath",
                    )
                    ref_text = gr.Textbox(
                        label="参考音频文本 (reference_text，与参考音频同时提供)",
                        placeholder="参考音频对应的文字内容",
                        lines=2,
                    )
                with gr.Column(scale=1):
                    device = gr.Dropdown(
                        ["auto", "cpu", "cuda"], value="auto", label="设备 device"
                    )
                    dtype = gr.Dropdown(
                        ["auto", "bfloat16", "float16", "float32"],
                        value="auto",
                        label="精度 dtype",
                    )
                    max_new_tokens = gr.Number(value=1024, label="max_new_tokens", precision=0)
                    retry_max_new_tokens = gr.Number(value=2000, label="retry_max_new_tokens", precision=0)
                    temperature = gr.Slider(0.1, 2.0, value=0.8, step=0.05, label="temperature")
                    top_p = gr.Slider(0.0, 1.0, value=0.95, step=0.01, label="top_p")
                    top_k = gr.Number(value=50, label="top_k", precision=0)
                    seed = gr.Number(value=42, label="seed", precision=0)
                    greedy = gr.Checkbox(value=False, label="greedy (贪心解码，关闭采样)")
                    save_codes = gr.Checkbox(value=False, label="保存声学 token (.npy)")
                    btn = gr.Button("开始合成", variant="primary")
            with gr.Row():
                audio_out = gr.Audio(label="合成结果", type="filepath")
                info_out = gr.Textbox(label="状态信息", lines=2)

            btn.click(
                fn=gen,
                inputs=[
                    text, ref_audio, ref_text, device, dtype,
                    max_new_tokens, retry_max_new_tokens, temperature,
                    top_p, top_k, seed, greedy, save_codes,
                ],
                outputs=[audio_out, info_out],
            )

        with gr.TabItem("批量合成 (JSONL)"):
            gr.Markdown(
                "每行一个 JSON：`{\"text\": \"...\", \"reference_audio\": \"路径\", \"reference_text\": \"...\"}`\n"
                "无参考音频时只需 `{\"text\": \"...\"}`。"
            )
            jsonl = gr.Textbox(label="JSONL 内容", lines=10, placeholder='{"text": "你好"}\n{"text": "克隆音色", "reference_audio": "uploads/ref.wav", "reference_text": "参考文字"}')
            with gr.Row():
                b_device = gr.Dropdown(["auto", "cpu", "cuda"], value="auto", label="device")
                b_dtype = gr.Dropdown(["auto", "bfloat16", "float16", "float32"], value="auto", label="dtype")
                b_max = gr.Number(value=1024, label="max_new_tokens", precision=0)
                b_temp = gr.Slider(0.1, 2.0, value=0.8, step=0.05, label="temperature")
                b_topp = gr.Slider(0.0, 1.0, value=0.95, step=0.01, label="top_p")
                b_topk = gr.Number(value=50, label="top_k", precision=0)
                b_seed = gr.Number(value=42, label="seed", precision=0)
                b_greedy = gr.Checkbox(value=False, label="greedy")
                b_save = gr.Checkbox(value=False, label="save_codes")
            b_btn = gr.Button("开始批量合成", variant="primary")
            b_out = gr.Markdown()
            b_btn.click(
                fn=batch_gen,
                inputs=[jsonl, b_device, b_dtype, b_max, b_temp, b_topp, b_topk, b_seed, b_greedy, b_save],
                outputs=b_out,
            )


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default=None, help="计算设备 (cuda/cpu/mps)（默认读 config）")
    parser.add_argument("--dtype", default=None, help="推理精度 (auto/bf16/f16/f32)（默认读 config）")
    parser.add_argument("--server-name", default=None, help="监听地址（默认读 config）")
    parser.add_argument("--server-port", type=int, default=None, help="监听端口（默认读 config）")
    parser.add_argument("--share", action="store_true", default=None, help="生成公网分享链接（默认读 config）")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    srv_cfg = CONFIG.get("server", {})

    # 解析有效值：命令行 > config > 内置默认
    host = args.server_name or srv_cfg.get("host", "0.0.0.0")
    port = args.server_port if args.server_port is not None else srv_cfg.get("port", 7868)
    device = args.device or srv_cfg.get("device", "auto")
    dtype = args.dtype or srv_cfg.get("dtype", "auto")
    share = args.share if args.share is not None else srv_cfg.get("share", False)
    concurrency_limit = srv_cfg.get("concurrency_limit", 2)
    gpu_concurrency = srv_cfg.get("gpu_concurrency", 1)

    # 初始化 GPU 信号量（串行化 GPU 推理，避免多用户/批量并发抢卡）
    _gpu_semaphore = threading.Semaphore(gpu_concurrency)
    print(
        f"[audio8] WebUI 并发配置: concurrency_limit={concurrency_limit}, "
        f"gpu_concurrency={gpu_concurrency}（配置来源 config/audio8_webui.yaml）"
    )

    demo.queue(concurrency_limit=concurrency_limit).launch(
        server_name=host,
        server_port=port,
        share=share,
        inbrowser=True,
        css=css,
    )
