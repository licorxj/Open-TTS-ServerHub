#!/usr/bin/env python3
# Copyright    2026  Xiaomi Corp.        (authors:  Han Zhu)
#
# See ../../LICENSE for clarification regarding multiple authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Gradio demo for OmniVoice.

Supports voice cloning and voice design.

Usage:
    omnivoice-demo --model /path/to/checkpoint --port 8000
"""

import argparse
import logging
import os
import re
import sys
from datetime import datetime
from typing import Any, Dict

import gradio as gr
import numpy as np
import soundfile as sf
import torch

from omnivoice import OmniVoice, OmniVoiceGenerationConfig
from omnivoice.utils.lang_map import LANG_NAMES, lang_display_name


def get_best_device():
    """Auto-detect the best available device: CUDA > MPS > CPU."""
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


# ---------------------------------------------------------------------------
# Language list — all 600+ supported languages
# ---------------------------------------------------------------------------
_ALL_LANGUAGES = ["Auto"] + sorted(lang_display_name(n) for n in LANG_NAMES)


# ---------------------------------------------------------------------------
# Voice Design instruction templates
# ---------------------------------------------------------------------------
# Each option is displayed as "English / 中文".
# The model expects English for accents and Chinese for dialects.
_CATEGORIES = {
    "Gender / 性别": ["Male / 男", "Female / 女"],
    "Age / 年龄": [
        "Child / 儿童",
        "Teenager / 少年",
        "Young Adult / 青年",
        "Middle-aged / 中年",
        "Elderly / 老年",
    ],
    "Pitch / 音调": [
        "Very Low Pitch / 极低音调",
        "Low Pitch / 低音调",
        "Moderate Pitch / 中音调",
        "High Pitch / 高音调",
        "Very High Pitch / 极高音调",
    ],
    "Style / 风格": ["Whisper / 耳语"],
    "English Accent / 英文口音": [
        "American Accent / 美式口音",
        "Australian Accent / 澳大利亚口音",
        "British Accent / 英国口音",
        "Chinese Accent / 中国口音",
        "Canadian Accent / 加拿大口音",
        "Indian Accent / 印度口音",
        "Korean Accent / 韩国口音",
        "Portuguese Accent / 葡萄牙口音",
        "Russian Accent / 俄罗斯口音",
        "Japanese Accent / 日本口音",
    ],
    "Chinese Dialect / 中文方言": [
        "Henan Dialect / 河南话",
        "Shaanxi Dialect / 陕西话",
        "Sichuan Dialect / 四川话",
        "Guizhou Dialect / 贵州话",
        "Yunnan Dialect / 云南话",
        "Guilin Dialect / 桂林话",
        "Jinan Dialect / 济南话",
        "Shijiazhuang Dialect / 石家庄话",
        "Gansu Dialect / 甘肃话",
        "Ningxia Dialect / 宁夏话",
        "Qingdao Dialect / 青岛话",
        "Northeast Dialect / 东北话",
    ],
}

_ATTR_INFO = {
    "English Accent / 英文口音": "Only effective for English speech.",
    "Chinese Dialect / 中文方言": "Only effective for Chinese speech.",
}

# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omnivoice-demo",
        description="Launch a Gradio demo for OmniVoice.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--model",
        default="k2-fsa/OmniVoice",
        help="Model checkpoint path or HuggingFace repo id.",
    )
    parser.add_argument(
        "--device", default=None, help="Device to use. Auto-detected if not specified."
    )
    parser.add_argument("--ip", default="0.0.0.0", help="Server IP (default: 0.0.0.0).")
    parser.add_argument(
        "--port", type=int, default=7860, help="Server port (default: 7860)."
    )
    parser.add_argument(
        "--root-path",
        default=None,
        help="Root path for reverse proxy.",
    )
    parser.add_argument(
        "--share", action="store_true", default=False, help="Create public link."
    )
    parser.add_argument(
        "--no-asr",
        action="store_true",
        default=False,
        help="Skip loading Whisper ASR model. Reference text auto-transcription"
        " will be unavailable.",
    )
    parser.add_argument(
        "--asr-model",
        default="openai/whisper-large-v3-turbo",
        help="ASR model path or HuggingFace repo id"
        " (default: openai/whisper-large-v3-turbo).",
    )
    return parser


# ---------------------------------------------------------------------------
# Build demo
# ---------------------------------------------------------------------------


def build_demo(
    model: OmniVoice,
    checkpoint: str,
    generate_fn=None,
) -> gr.Blocks:
    sampling_rate = model.sampling_rate
    output_root = os.path.join("outputs", "webui_longform", "omnivoice")

    # -- shared generation core --
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

        waveform = (audio[0] * 32767).astype(np.int16)
        return (sampling_rate, waveform), "Done."

    _gen = generate_fn if generate_fn is not None else _gen_core

    def _build_instruct(groups):
        selected = [g for g in groups if g and g != "Auto"]
        if not selected:
            return None

        parts = []
        for value in selected:
            if " / " in value:
                left, right = value.split(" / ", 1)
                if "Dialect" in value.split(" / ")[0]:
                    parts.append(right.strip())
                else:
                    parts.append(left.strip())
            else:
                parts.append(value)
        return ", ".join(parts)

    def _merge_instruct(*parts):
        merged = [str(part).strip() for part in parts if part and str(part).strip()]
        return ", ".join(merged) if merged else None

    def _new_session_dir():
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = os.path.join(output_root, session_id)
        os.makedirs(path, exist_ok=True)
        return path

    def _sentence_status(status, error=""):
        cls_map = {
            "pending": "pending",
            "success": "success",
            "error": "error",
            "running": "running",
        }
        label_map = {
            "pending": "待生成",
            "success": "已生成",
            "error": "生成失败",
            "running": "生成中",
        }
        label = label_map.get(status or "pending", "待生成")
        extra = f"<span>{gr.utils.sanitize_html(error)}</span>" if error else ""
        return (
            f"<div class='lf-status {cls_map.get(status or 'pending', 'pending')}'>"
            f"<strong>{label}</strong>{extra}</div>"
        )

    def _blank_export():
        return gr.update(value=None)

    def _reset_sentence(item, text=None, instruction=None):
        updated = dict(item)
        if text is not None:
            updated["text"] = text
        if instruction is not None:
            updated["instruction_override"] = instruction
        updated["audio_path"] = None
        updated["status"] = "pending"
        updated["error"] = ""
        return updated

    def _split_text(text, rules):
        raw = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not raw:
            return [], "", "请输入需要断句的长文内容。", _blank_export()

        parts = [raw]
        selected_rules = set(rules or [])
        if "换行符" in selected_rules:
            newline_parts = []
            for chunk in parts:
                newline_parts.extend([p.strip() for p in re.split(r"\n+", chunk) if p.strip()])
            parts = newline_parts or parts

        punctuation_chars = ""
        if "句号" in selected_rules:
            punctuation_chars += r"\.。"
        if "问号" in selected_rules:
            punctuation_chars += r"\?？"
        if "感叹号" in selected_rules:
            punctuation_chars += r"!！"

        if punctuation_chars:
            pattern = rf"(?<=[{punctuation_chars}])\s*"
            punct_parts = []
            for chunk in parts:
                punct_parts.extend([p.strip() for p in re.split(pattern, chunk) if p.strip()])
            parts = punct_parts or parts

        items = [
            {
                "id": idx,
                "selected": True,
                "text": part,
                "instruction_override": "",
                "audio_path": None,
                "status": "pending",
                "error": "",
            }
            for idx, part in enumerate(parts)
        ]
        return items, "", f"已断句，共 {len(items)} 句。", _blank_export()

    def _toggle_mode(mode):
        is_clone = mode == "克隆模式"
        return gr.update(visible=is_clone), gr.update(visible=not is_clone)

    def _select_all(items):
        updated = [dict(item, selected=True) for item in (items or [])]
        message = f"已全选 {len(updated)} 句。" if updated else "暂无句子可选。"
        return updated, message, _blank_export()

    def _update_selected(value, items, idx):
        updated = [dict(item) for item in (items or [])]
        if 0 <= idx < len(updated):
            updated[idx]["selected"] = bool(value)
        return updated, _blank_export()

    def _update_text(value, items, idx):
        updated = [dict(item) for item in (items or [])]
        if 0 <= idx < len(updated):
            updated[idx] = _reset_sentence(updated[idx], text=(value or "").strip())
        return updated, _blank_export()

    def _update_instruction(value, items, idx):
        updated = [dict(item) for item in (items or [])]
        if 0 <= idx < len(updated):
            updated[idx] = _reset_sentence(updated[idx], instruction=(value or "").strip())
        return updated, _blank_export()

    def _save_audio(audio_data, session_dir, idx):
        if not session_dir:
            session_dir = _new_session_dir()
        os.makedirs(session_dir, exist_ok=True)
        sr, waveform = audio_data
        file_path = os.path.join(
            session_dir,
            f"{idx:04d}_{datetime.now().strftime('%H%M%S_%f')}.wav",
        )
        sf.write(file_path, waveform, sr)
        return session_dir, file_path

    def _generate_one(
        item,
        idx,
        session_dir,
        mode,
        ref_audio,
        ref_text,
        language,
        clone_instruct,
        ns,
        gs,
        dn,
        sp,
        du,
        pp,
        po,
        design_groups,
    ):
        target_text = (item.get("text") or "").strip()
        if not target_text:
            updated = _reset_sentence(item, text="")
            updated["status"] = "error"
            updated["error"] = "句子内容为空。"
            return updated, session_dir

        if mode == "克隆模式":
            if not ref_audio:
                updated = _reset_sentence(item)
                updated["status"] = "error"
                updated["error"] = "请先上传参考音频。"
                return updated, session_dir
            instruct = _merge_instruct(clone_instruct, item.get("instruction_override"))
            audio_data, status = _gen(
                target_text,
                language,
                ref_audio,
                instruct,
                ns,
                gs,
                dn,
                sp,
                du,
                pp,
                po,
                mode="clone",
                ref_text=(ref_text or None),
            )
        else:
            instruct = _merge_instruct(_build_instruct(design_groups), item.get("instruction_override"))
            audio_data, status = _gen(
                target_text,
                language,
                None,
                instruct,
                ns,
                gs,
                dn,
                sp,
                du,
                pp,
                po,
                mode="design",
            )

        updated = dict(item)
        if audio_data is None:
            updated["audio_path"] = None
            updated["status"] = "error"
            updated["error"] = status
            return updated, session_dir

        session_dir, audio_path = _save_audio(audio_data, session_dir, idx)
        updated["audio_path"] = audio_path
        updated["status"] = "success"
        updated["error"] = ""
        return updated, session_dir

    def _run_generation(
        items,
        session_dir,
        run_all,
        mode,
        ref_audio,
        ref_text,
        language,
        clone_instruct,
        ns,
        gs,
        dn,
        sp,
        du,
        pp,
        po,
        *design_groups,
    ):
        current_items = [dict(item) for item in (items or [])]
        if not current_items:
            return current_items, session_dir, "请先完成断句。", _blank_export()

        target_indices = list(range(len(current_items))) if run_all else [
            idx for idx, item in enumerate(current_items) if item.get("selected")
        ]
        if not target_indices:
            return current_items, session_dir, "请先勾选需要生成的句子。", _blank_export()

        current_session = session_dir or _new_session_dir()
        success_count = 0
        failure_count = 0
        for idx in target_indices:
            updated, current_session = _generate_one(
                current_items[idx],
                idx,
                current_session,
                mode,
                ref_audio,
                ref_text,
                language,
                clone_instruct,
                ns,
                gs,
                dn,
                sp,
                du,
                pp,
                po,
                design_groups,
            )
            current_items[idx] = updated
            if updated["status"] == "success":
                success_count += 1
            else:
                failure_count += 1

        return (
            current_items,
            current_session,
            f"处理完成：成功 {success_count} 句，失败 {failure_count} 句。",
            _blank_export(),
        )

    def _regenerate_single(
        items,
        session_dir,
        idx,
        mode,
        ref_audio,
        ref_text,
        language,
        clone_instruct,
        ns,
        gs,
        dn,
        sp,
        du,
        pp,
        po,
        *design_groups,
    ):
        current_items = [dict(item) for item in (items or [])]
        if not (0 <= idx < len(current_items)):
            return current_items, session_dir, "句子索引无效。", _blank_export()

        current_session = session_dir or _new_session_dir()
        updated, current_session = _generate_one(
            current_items[idx],
            idx,
            current_session,
            mode,
            ref_audio,
            ref_text,
            language,
            clone_instruct,
            ns,
            gs,
            dn,
            sp,
            du,
            pp,
            po,
            design_groups,
        )
        current_items[idx] = updated
        message = f"第 {idx + 1} 句已重新生成。" if updated["status"] == "success" else updated["error"]
        return current_items, current_session, message, _blank_export()

    def _export_audio(items, session_dir, gap_seconds):
        current_items = [dict(item) for item in (items or [])]
        if not current_items:
            return _blank_export(), "请先断句并生成音频。"

        missing = [
            idx + 1
            for idx, item in enumerate(current_items)
            if item.get("status") != "success" or not item.get("audio_path")
        ]
        if missing:
            preview = "、".join(str(i) for i in missing[:8])
            suffix = " 等" if len(missing) > 8 else ""
            return _blank_export(), f"以下句子尚未生成完成：{preview}{suffix}。"

        export_dir = session_dir or _new_session_dir()
        gap = max(float(gap_seconds or 0.0), 0.0)
        chunks = []
        export_sr = None
        for idx, item in enumerate(current_items):
            audio_path = item["audio_path"]
            data, sr = sf.read(audio_path, dtype="float32")
            if data.ndim > 1:
                data = data[:, 0]
            if export_sr is None:
                export_sr = sr
            elif sr != export_sr:
                return _blank_export(), "检测到句子采样率不一致，暂时无法导出。"
            chunks.append(data)
            if idx < len(current_items) - 1 and gap > 0:
                chunks.append(np.zeros(int(export_sr * gap), dtype=np.float32))

        merged = np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float32)
        export_path = os.path.join(
            export_dir,
            f"omnivoice_longform_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav",
        )
        sf.write(export_path, merged, export_sr or sampling_rate)
        return export_path, f"导出完成：{os.path.basename(export_path)}"

    theme = gr.themes.Soft(
        font=["Inter", "Arial", "sans-serif"],
    )
    css = """
    .gradio-container {max-width: 100% !important; font-size: 16px !important;}
    .gradio-container h1 {font-size: 1.5em !important;}
    .gradio-container .prose {font-size: 1.05em !important;}
    .compact-audio audio {height: 60px !important;}
    .compact-audio .waveform {min-height: 80px !important;}
    .ov-subtitle {
        margin: 0.5rem 0 1.25rem 0;
        padding: 14px 16px;
        border: 1px solid #dbe4ff;
        border-radius: 14px;
        background: linear-gradient(135deg, #f8fbff, #eef4ff);
        color: #334155;
        font-size: 0.95rem;
        line-height: 1.7;
    }
    .ov-subtitle a {
        color: #2563eb;
        text-decoration: none;
        font-weight: 600;
    }
    .lf-panel {
        border: 1px solid #dbe4ff;
        border-radius: 14px;
        padding: 16px;
        background: #fbfdff;
    }
    .lf-panel + .lf-panel {
        margin-top: 14px;
    }
    .lf-panel textarea {
        min-height: 220px !important;
        max-height: 420px;
    }
    .lf-toolbar {
        gap: 10px;
        align-items: end;
        margin-bottom: 12px;
        flex-wrap: wrap;
    }
    .lf-toolbar .gradio-button {
        min-width: 130px;
    }
    .lf-empty {
        padding: 18px;
        border: 1px dashed #cbd5e1;
        border-radius: 12px;
        background: #f8fafc;
        color: #64748b;
        text-align: center;
    }
    .lf-sentence {
        border: 1px solid #e2e8f0;
        border-radius: 14px;
        padding: 12px;
        margin-bottom: 10px;
        background: white;
    }
    .lf-sentence textarea {
        min-height: 74px !important;
    }
    .lf-sentence-head {
        align-items: center;
        gap: 10px;
        margin-bottom: 8px;
    }
    .lf-sentence-fields {
        gap: 10px;
        margin-bottom: 8px;
    }
    .lf-sentence-audio {
        align-items: end;
        gap: 10px;
    }
    .lf-compact-audio audio {height: 42px !important;}
    .lf-compact-audio .waveform {min-height: 56px !important;}
    .lf-status {
        border-radius: 999px;
        padding: 6px 12px;
        font-size: 0.86rem;
        display: inline-flex;
        gap: 8px;
        align-items: center;
    }
    .lf-status.pending {background: #eff6ff; color: #1d4ed8;}
    .lf-status.success {background: #ecfdf5; color: #047857;}
    .lf-status.error {background: #fef2f2; color: #b91c1c;}
    .lf-status.running {background: #fff7ed; color: #c2410c;}
    """

    def _lang_dropdown(label="Language (optional) / 语种 (可选)", value="Auto"):
        return gr.Dropdown(
            label=label,
            choices=_ALL_LANGUAGES,
            value=value,
            allow_custom_value=False,
            interactive=True,
            info="Keep as Auto to auto-detect the language.",
        )

    def _gen_settings():
        with gr.Accordion("Generation Settings (optional)", open=False):
            sp = gr.Slider(
                0.5,
                1.5,
                value=1.0,
                step=0.05,
                label="Speed",
                info="1.0 = normal. >1 faster, <1 slower. Ignored if Duration is set.",
            )
            du = gr.Number(
                value=None,
                label="Duration (seconds)",
                info=(
                    "Leave empty to use speed."
                    " Set a fixed duration to override speed."
                ),
            )
            ns = gr.Slider(
                4,
                64,
                value=32,
                step=1,
                label="Inference Steps",
                info="Default: 32. Lower = faster, higher = better quality.",
            )
            dn = gr.Checkbox(
                label="Denoise",
                value=True,
                info="Default: enabled. Uncheck to disable denoising.",
            )
            gs = gr.Slider(
                0.0,
                4.0,
                value=2.0,
                step=0.1,
                label="Guidance Scale (CFG)",
                info="Default: 2.0.",
            )
            pp = gr.Checkbox(
                label="Preprocess Prompt",
                value=True,
                info="apply silence removal and trimming to the reference "
                "audio, add punctuation in the end of reference text (if not already)",
            )
            po = gr.Checkbox(
                label="Postprocess Output",
                value=True,
                info="Remove long silences from generated audio.",
            )
        return ns, gs, dn, sp, du, pp, po

    with gr.Blocks(theme=theme, css=css, title="OmniVoice Demo") as demo:
        gr.Markdown(
            """
# OmniVoice Demo

State-of-the-art text-to-speech model for **600+ languages**, supporting:

- **Voice Clone** — Clone any voice from a reference audio
- **Voice Design** — Create custom voices with speaker attributes

Built with [OmniVoice](https://github.com/k2-fsa/OmniVoice)
by Xiaomi AI Lab Next-gen Kaldi team.
"""
        )
        gr.HTML(
            """
<div class="ov-subtitle">
  <div><strong>整合包作者：</strong>licor</div>
  <div><strong>B站ID：</strong>单手飞机</div>
  <div><strong>更多AI整合包资源：</strong><a href="https://www.kdocs.cn/l/ch0DFzxFCWhA" target="_blank">https://www.kdocs.cn/l/ch0DFzxFCWhA</a></div>
</div>
"""
        )

        with gr.Tabs():
            with gr.TabItem("Voice Clone"):
                with gr.Row():
                    with gr.Column(scale=1):
                        vc_text = gr.Textbox(
                            label="Text to Synthesize / 待合成文本",
                            lines=4,
                            placeholder="Enter the text you want to synthesize...",
                        )
                        vc_ref_audio = gr.Audio(
                            label="Reference Audio / 参考音频",
                            type="filepath",
                            elem_classes="compact-audio",
                        )
                        gr.Markdown(
                            "<span style='font-size:0.85em;color:#888;'>"
                            "Recommended: 3-10 seconds audio."
                            "</span>"
                        )
                        vc_ref_text = gr.Textbox(
                            label="Reference Text (optional) / 参考音频文本（可选）",
                            lines=2,
                            placeholder="Transcript of the reference audio. Leave empty to auto-transcribe via ASR models.",
                        )
                        vc_lang = _lang_dropdown("Language (optional) / 语种 (可选)")
                        with gr.Accordion("Instruct (optional)", open=False):
                            vc_instruct = gr.Textbox(label="Instruct", lines=2)
                        (
                            vc_ns,
                            vc_gs,
                            vc_dn,
                            vc_sp,
                            vc_du,
                            vc_pp,
                            vc_po,
                        ) = _gen_settings()
                        vc_btn = gr.Button("Generate / 生成", variant="primary")
                    with gr.Column(scale=1):
                        vc_audio = gr.Audio(
                            label="Output Audio / 合成结果",
                            type="numpy",
                        )
                        vc_status = gr.Textbox(label="Status / 状态", lines=2)

                def _clone_fn(
                    text, lang, ref_aud, ref_text, instruct, ns, gs, dn, sp, du, pp, po
                ):
                    return _gen(
                        text,
                        lang,
                        ref_aud,
                        instruct,
                        ns,
                        gs,
                        dn,
                        sp,
                        du,
                        pp,
                        po,
                        mode="clone",
                        ref_text=ref_text or None,
                    )

                vc_btn.click(
                    _clone_fn,
                    inputs=[
                        vc_text,
                        vc_lang,
                        vc_ref_audio,
                        vc_ref_text,
                        vc_instruct,
                        vc_ns,
                        vc_gs,
                        vc_dn,
                        vc_sp,
                        vc_du,
                        vc_pp,
                        vc_po,
                    ],
                    outputs=[vc_audio, vc_status],
                )

            with gr.TabItem("Voice Design"):
                with gr.Row():
                    with gr.Column(scale=1):
                        vd_text = gr.Textbox(
                            label="Text to Synthesize / 待合成文本",
                            lines=4,
                            placeholder="Enter the text you want to synthesize...",
                        )
                        vd_lang = _lang_dropdown()

                        _AUTO = "Auto"
                        vd_groups = []
                        for _cat, _choices in _CATEGORIES.items():
                            vd_groups.append(
                                gr.Dropdown(
                                    label=_cat,
                                    choices=[_AUTO] + _choices,
                                    value=_AUTO,
                                    info=_ATTR_INFO.get(_cat),
                                )
                            )

                        (
                            vd_ns,
                            vd_gs,
                            vd_dn,
                            vd_sp,
                            vd_du,
                            vd_pp,
                            vd_po,
                        ) = _gen_settings()
                        vd_btn = gr.Button("Generate / 生成", variant="primary")
                    with gr.Column(scale=1):
                        vd_audio = gr.Audio(
                            label="Output Audio / 合成结果",
                            type="numpy",
                        )
                        vd_status = gr.Textbox(label="Status / 状态", lines=2)

                def _design_fn(text, lang, ns, gs, dn, sp, du, pp, po, *groups):
                    return _gen(
                        text,
                        lang,
                        None,
                        _build_instruct(groups),
                        ns,
                        gs,
                        dn,
                        sp,
                        du,
                        pp,
                        po,
                        mode="design",
                    )

                vd_btn.click(
                    _design_fn,
                    inputs=[
                        vd_text,
                        vd_lang,
                        vd_ns,
                        vd_gs,
                        vd_dn,
                        vd_sp,
                        vd_du,
                        vd_pp,
                        vd_po,
                    ]
                    + vd_groups,
                    outputs=[vd_audio, vd_status],
                )

            with gr.TabItem("长文配音 / Long-form Dubbing"):
                sentence_state = gr.State([])
                session_dir_state = gr.State("")

                with gr.Group(elem_classes="lf-panel"):
                    lf_text = gr.Textbox(
                        label="长文输入 / Long-form Text",
                        lines=14,
                        placeholder="粘贴需要配音的长文内容，支持滚动编辑。",
                    )
                    lf_mode = gr.Radio(
                        ["克隆模式", "音色描述模式"],
                        value="克隆模式",
                        label="配音模式",
                    )
                    lf_lang = _lang_dropdown("Language / 语种", value="Auto")
                    lf_rules = gr.CheckboxGroup(
                        label="断句规则",
                        choices=["换行符", "句号", "问号", "感叹号"],
                        value=["换行符", "句号", "问号", "感叹号"],
                    )

                    with gr.Group(visible=True) as lf_clone_group:
                        lf_ref_audio = gr.Audio(
                            label="Reference Audio / 参考音频",
                            type="filepath",
                            elem_classes="compact-audio",
                        )
                        lf_ref_text = gr.Textbox(
                            label="Reference Text (optional) / 参考文本（可选）",
                            lines=2,
                            placeholder="如果已有参考音频对应文本，可在此填写。",
                        )
                        lf_clone_instruct = gr.Textbox(
                            label="Clone Instruct (optional) / 克隆附加指令（可选）",
                            lines=2,
                            placeholder="可选：补充情绪、风格、语气等控制描述。",
                        )

                    with gr.Group(visible=False) as lf_design_group:
                        gr.Markdown("### 音色描述属性 / Voice Design Attributes")
                        lf_design_groups = []
                        for _cat, _choices in _CATEGORIES.items():
                            lf_design_groups.append(
                                gr.Dropdown(
                                    label=_cat,
                                    choices=["Auto"] + _choices,
                                    value="Auto",
                                    info=_ATTR_INFO.get(_cat),
                                )
                            )

                    (
                        lf_ns,
                        lf_gs,
                        lf_dn,
                        lf_sp,
                        lf_du,
                        lf_pp,
                        lf_po,
                    ) = _gen_settings()
                    lf_split_btn = gr.Button("断句 / Split", variant="primary")

                with gr.Group(elem_classes="lf-panel"):
                    with gr.Row(elem_classes="lf-toolbar"):
                        lf_select_all_btn = gr.Button("全选")
                        lf_generate_btn = gr.Button("批量生成选中句子", variant="primary")
                        lf_regenerate_all_btn = gr.Button("全部重新生成")
                        lf_gap = gr.Number(
                            value=0.3,
                            precision=2,
                            label="句子间隔（秒）",
                        )
                        lf_export_btn = gr.Button("导出")
                    lf_export_file = gr.File(
                        label="导出音频 / Export Audio",
                        interactive=False,
                    )
                    lf_status = gr.Markdown("请先输入长文并点击 `断句 / Split`。")

                    @gr.render(inputs=sentence_state)
                    def render_sentences(items):
                        if not items:
                            gr.HTML(
                                "<div class='lf-empty'>断句完成后，这里会显示可编辑的句子列表、单句播放与重生成按钮。</div>"
                            )
                            return

                        for idx, item in enumerate(items):
                            with gr.Group(elem_classes="lf-sentence", key=f"lf_sentence_{item['id']}"):
                                with gr.Row(elem_classes="lf-sentence-head"):
                                    selected = gr.Checkbox(
                                        value=item.get("selected", True),
                                        label=f"第 {idx + 1} 句",
                                        scale=2,
                                        key=f"lf_selected_{item['id']}",
                                    )
                                    status_html = gr.HTML(
                                        _sentence_status(item.get("status"), item.get("error", "")),
                                        scale=3,
                                        key=f"lf_status_{item['id']}",
                                    )
                                    regen_btn = gr.Button(
                                        "重生成",
                                        scale=1,
                                        key=f"lf_regen_{item['id']}",
                                    )

                                with gr.Row(elem_classes="lf-sentence-fields"):
                                    sentence_box = gr.Textbox(
                                        value=item.get("text", ""),
                                        label="句子文本（可编辑）",
                                        lines=2,
                                        key=f"lf_text_{item['id']}",
                                    )
                                    instruction_box = gr.Textbox(
                                        value=item.get("instruction_override", ""),
                                        label="配音指令描述",
                                        lines=2,
                                        placeholder="默认留空。可单独补充当前句的情绪、语速、语气描述。",
                                        key=f"lf_instruction_{item['id']}",
                                    )
                                with gr.Row(elem_classes="lf-sentence-audio"):
                                    audio = gr.Audio(
                                        value=item.get("audio_path") if item.get("audio_path") else None,
                                        label="播放",
                                        interactive=False,
                                        scale=1,
                                        elem_classes="lf-compact-audio",
                                        key=f"lf_audio_{item['id']}",
                                    )

                                selected.change(
                                    lambda value, items, idx=idx: _update_selected(value, items, idx),
                                    inputs=[selected, sentence_state],
                                    outputs=[sentence_state, lf_export_file],
                                )
                                sentence_box.change(
                                    lambda value, items, idx=idx: _update_text(value, items, idx),
                                    inputs=[sentence_box, sentence_state],
                                    outputs=[sentence_state, lf_export_file],
                                )
                                instruction_box.change(
                                    lambda value, items, idx=idx: _update_instruction(value, items, idx),
                                    inputs=[instruction_box, sentence_state],
                                    outputs=[sentence_state, lf_export_file],
                                )
                                regen_btn.click(
                                    lambda items, session_dir, mode, ref_audio, ref_text, language, clone_instruct, ns, gs, dn, sp, du, pp, po, *design_groups, idx=idx: _regenerate_single(
                                        items,
                                        session_dir,
                                        idx,
                                        mode,
                                        ref_audio,
                                        ref_text,
                                        language,
                                        clone_instruct,
                                        ns,
                                        gs,
                                        dn,
                                        sp,
                                        du,
                                        pp,
                                        po,
                                        *design_groups,
                                    ),
                                    inputs=[
                                        sentence_state,
                                        session_dir_state,
                                        lf_mode,
                                        lf_ref_audio,
                                        lf_ref_text,
                                        lf_lang,
                                        lf_clone_instruct,
                                        lf_ns,
                                        lf_gs,
                                        lf_dn,
                                        lf_sp,
                                        lf_du,
                                        lf_pp,
                                        lf_po,
                                    ]
                                    + lf_design_groups,
                                    outputs=[
                                        sentence_state,
                                        session_dir_state,
                                        lf_status,
                                        lf_export_file,
                                    ],
                                )

                lf_mode.change(
                    _toggle_mode,
                    inputs=[lf_mode],
                    outputs=[lf_clone_group, lf_design_group],
                )
                lf_split_btn.click(
                    _split_text,
                    inputs=[lf_text, lf_rules],
                    outputs=[sentence_state, session_dir_state, lf_status, lf_export_file],
                )
                lf_select_all_btn.click(
                    _select_all,
                    inputs=[sentence_state],
                    outputs=[sentence_state, lf_status, lf_export_file],
                )
                lf_generate_btn.click(
                    lambda items, session_dir, mode, ref_audio, ref_text, language, clone_instruct, ns, gs, dn, sp, du, pp, po, *design_groups: _run_generation(
                        items,
                        session_dir,
                        False,
                        mode,
                        ref_audio,
                        ref_text,
                        language,
                        clone_instruct,
                        ns,
                        gs,
                        dn,
                        sp,
                        du,
                        pp,
                        po,
                        *design_groups,
                    ),
                    inputs=[
                        sentence_state,
                        session_dir_state,
                        lf_mode,
                        lf_ref_audio,
                        lf_ref_text,
                        lf_lang,
                        lf_clone_instruct,
                        lf_ns,
                        lf_gs,
                        lf_dn,
                        lf_sp,
                        lf_du,
                        lf_pp,
                        lf_po,
                    ]
                    + lf_design_groups,
                    outputs=[sentence_state, session_dir_state, lf_status, lf_export_file],
                )
                lf_regenerate_all_btn.click(
                    lambda items, session_dir, mode, ref_audio, ref_text, language, clone_instruct, ns, gs, dn, sp, du, pp, po, *design_groups: _run_generation(
                        items,
                        session_dir,
                        True,
                        mode,
                        ref_audio,
                        ref_text,
                        language,
                        clone_instruct,
                        ns,
                        gs,
                        dn,
                        sp,
                        du,
                        pp,
                        po,
                        *design_groups,
                    ),
                    inputs=[
                        sentence_state,
                        session_dir_state,
                        lf_mode,
                        lf_ref_audio,
                        lf_ref_text,
                        lf_lang,
                        lf_clone_instruct,
                        lf_ns,
                        lf_gs,
                        lf_dn,
                        lf_sp,
                        lf_du,
                        lf_pp,
                        lf_po,
                    ]
                    + lf_design_groups,
                    outputs=[sentence_state, session_dir_state, lf_status, lf_export_file],
                )
                lf_export_btn.click(
                    _export_audio,
                    inputs=[sentence_state, session_dir_state, lf_gap],
                    outputs=[lf_export_file, lf_status],
                )

    return demo


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args(argv)

    device = args.device or get_best_device()

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from tools.models_manager import ModelsManager

    mgr = ModelsManager()
    checkpoint = mgr.resolve_path(args.model, model_type="omnivoice")
    if not args.model:
        parser.print_help()
        return 0
    logging.info(f"Loading model from {checkpoint}, device={device} ...")
    model = OmniVoice.from_pretrained(
        checkpoint,
        device_map=device,
        dtype=torch.float16,
        load_asr=not args.no_asr,
        asr_model_name=args.asr_model,
    )
    print("Model loaded.")

    demo = build_demo(model, checkpoint)

    demo.queue().launch(
        server_name=args.ip,
        server_port=args.port,
        share=args.share,
        root_path=args.root_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
