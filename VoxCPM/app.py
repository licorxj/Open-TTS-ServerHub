import os
import re
import sys
import logging
from datetime import datetime
import numpy as np
import soundfile as sf
import torch
import gradio as gr
from typing import Optional, Tuple
from funasr import AutoModel
from pathlib import Path

os.environ["TOKENIZERS_PARALLELISM"] = "false"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.models_manager import ModelsManager

import voxcpm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ---------- Inline i18n (en + zh-CN only) ----------

_USAGE_INSTRUCTIONS_EN = (
    "**VoxCPM2 — Three Modes of Speech Generation:**\n\n"
    "🎨 **Voice Design** — Create a brand-new voice  \n"
    "No reference audio required. Describe the desired voice characteristics "
    "(gender, age, tone, emotion, pace …) in **Control Instruction**, and VoxCPM2 "
    "will craft a unique voice from your description alone.\n\n"
    "🎛️ **Controllable Cloning** — Clone a voice with optional style guidance  \n"
    "Upload a reference audio clip, then use **Control Instruction** to steer "
    "emotion, speaking pace, and overall style while preserving the original timbre.\n\n"
    "🎙️ **Ultimate Cloning** — Reproduce every vocal nuance through audio continuation  \n"
    "Turn on **Ultimate Cloning Mode** and provide (or auto-transcribe) the reference audio's transcript. "
    "The model treats the reference clip as a spoken prefix and seamlessly **continues** from it, faithfully preserving every vocal detail."
    "Note: This mode will disable Control Instruction."
)

_EXAMPLES_FOOTER_EN = (
    "---\n"
    "**💡 Voice Description Examples:**  \n"
    "Try the following Control Instructions to explore different voices:  \n\n"
    "**Example 1 — Gentle & Melancholic Girl**  \n"
    '`Control Instruction`: *"A young girl with a soft, sweet voice. '
    'Speaks slowly with a melancholic, slightly tsundere tone."*  \n'
    '`Target Text`: *"I never asked you to stay… It\'s not like I care or anything. '
    'But… why does it still hurt so much now that you\'re gone?"*  \n\n'
    "**Example 2 — Laid-Back Surfer Dude**  \n"
    '`Control Instruction`: *"Relaxed young male voice, slightly nasal, '
    'lazy drawl, very casual and chill."*  \n'
    '`Target Text`: *"Dude, did you see that set? The waves out there are totally gnarly today. '
    "Just catching barrels all morning — it's like, totally righteous, you know what I mean?\"*"
)

_USAGE_INSTRUCTIONS_ZH = (
    "**VoxCPM2 — 三种语音生成方式：**\n\n"
    "🎨 **声音设计（Voice Design）**  \n"
    "无需参考音频。在 **Control Instruction** 中描述目标音色特征"
    "（性别、年龄、语气、情绪、语速等），VoxCPM2 即可为你从零创造独一无二的声音。\n\n"
    "🎛️ **可控克隆（Controllable Cloning）**  \n"
    "上传参考音频，同时可选地使用 **Control Instruction** 来指定情绪、语速、风格等表达方式，"
    "在保留原始音色的基础上灵活控制说话风格。\n\n"
    "🎙️ **极致克隆（Ultimate Cloning）**  \n"
    "开启 **极致克隆模式** 并提供参考音频的文字内容（可自动识别）。"
    "模型会将参考音频视为已说出的前文，以**音频续写**的方式完整还原参考音频中的所有声音细节。"
    "注意：该模式与可控克隆模式互斥，将禁用Control Instruction。\n\n"
)

_EXAMPLES_FOOTER_ZH = (
    "---\n"
    "**💡 声音描述示例（中英文均可）：**  \n\n"
    "**示例 1 — 深宫太后**  \n"
    '`Control Instruction`: *"中老年女性，声音低沉阴冷，语速缓慢而有力，'
    '字字深思熟虑，带有深不可测的城府与威慑感。"*  \n'
    '`Target Text`: *"哀家在这深宫待了四十年，什么风浪没见过？你以为瞒得过哀家？"*  \n\n'
    "**示例 2 — 暴躁驾校教练**  \n"
    '`Control Instruction`: *"暴躁的中年男声，语速快，充满无奈和愤怒"*  \n'
    '`Target Text`: *"踩离合！踩刹车啊！你往哪儿开呢？前面是树你看不见吗？'
    '我教了你八百遍了，打死方向盘！你是不是想把车给我开到沟里去？"*  \n\n'
    "---\n"
    "**🗣️ 方言生成指南：**  \n"
    "要生成地道的方言语音，请在 **Target Text** 中直接使用方言词汇和句式，"
    "并在 **Control Instruction** 中描述方言特征。  \n\n"
    "**示例 — 广东话**  \n"
    '`Control Instruction`: *"粤语，中年男性，语气平淡"*  \n'
    '✅ 正确（粤语表达）：*"伙計，唔該一個A餐，凍奶茶少甜！"*  \n'
    '❌ 错误（普通话原文）：*"伙计，麻烦来一个A餐，冻奶茶少甜！"*  \n\n'
    "**示例 — 河南话**  \n"
    '`Control Instruction`: *"河南话，接地气的大叔"*  \n'
    '✅ 正确（河南话表达）：*"恁这是弄啥嘞？晌午吃啥饭？"*  \n'
    '❌ 错误（普通话原文）：*"你这是在干什么呢？中午吃什么饭？"*  \n\n'
    "🤖 **小技巧：** 不知道方言怎么写？可以用豆包、DeepSeek、Kimi 等 AI 助手"
    "将普通话翻译为方言文本，再粘贴到 Target Text 中即可。  \n\n"
)

_I18N_TRANSLATIONS = {
    "en": {
        "reference_audio_label": "🎤 Reference Audio (optional — upload for cloning)",
        "show_prompt_text_label": "🎙️ Ultimate Cloning Mode (transcript-guided cloning)",
        "show_prompt_text_info": "Auto-transcribes reference audio for every vocal nuance reproduced. Control Instruction will be disabled when active.",
        "prompt_text_label": "Transcript of Reference Audio (auto-filled via ASR, editable)",
        "prompt_text_placeholder": "The transcript of your reference audio will appear here …",
        "control_label": "🎛️ Control Instruction (optional — supports Chinese & English)",
        "control_placeholder": "e.g. A warm young woman / 年轻女性，温柔甜美 / Excited and fast-paced",
        "target_text_label": "✍️ Target Text — the content to speak",
        "generate_btn": "🔊 Generate Speech",
        "generated_audio_label": "Generated Audio",
        "advanced_settings_title": "⚙️ Advanced Settings",
        "ref_denoise_label": "Reference audio enhancement",
        "ref_denoise_info": "Apply ZipEnhancer denoising to the reference audio before cloning",
        "normalize_label": "Text normalization",
        "normalize_info": "Normalize numbers, dates, and abbreviations via wetext",
        "cfg_label": "CFG (guidance scale)",
        "cfg_info": "Higher → closer to the prompt / reference; lower → more creative variation",
        "dit_steps_label": "LocDiT flow-matching steps",
        "dit_steps_info": "LocDiT flow-matching steps — more steps → maybe better audio quality, but slower",
        "usage_instructions": _USAGE_INSTRUCTIONS_EN,
        "examples_footer": _EXAMPLES_FOOTER_EN,
    },
    "zh-CN": {
        "reference_audio_label": "🎤 参考音频（可选 — 上传后用于克隆）",
        "show_prompt_text_label": "🎙️ 极致克隆模式（基于文本引导的极致克隆）",
        "show_prompt_text_info": "自动识别参考音频文本，完整还原音色、节奏、情感等全部声音细节。开启后 Control Instruction 将暂时禁用",
        "prompt_text_label": "参考音频内容文本（ASR 自动填充，可手动编辑）",
        "prompt_text_placeholder": "参考音频的文字内容将自动识别并显示在此处 …",
        "control_label": "🎛️ Control Instruction（可选 — 支持中英文描述）",
        "control_placeholder": "如：年轻女性，温柔甜美 / A warm young woman / 暴躁老哥，语速飞快",
        "target_text_label": "✍️ Target Text — 要合成的目标文本",
        "generate_btn": "🔊 开始生成",
        "generated_audio_label": "生成结果",
        "advanced_settings_title": "⚙️ 高级设置",
        "ref_denoise_label": "参考音频降噪增强",
        "ref_denoise_info": "克隆前使用 ZipEnhancer 对参考音频进行降噪处理",
        "normalize_label": "文本规范化",
        "normalize_info": "自动规范化数字、日期及缩写（基于 wetext）",
        "cfg_label": "CFG（引导强度）",
        "cfg_info": "数值越高 → 越贴合提示/参考音色；数值越低 → 生成风格更自由",
        "dit_steps_label": "LocDiT 流匹配迭代步数",
        "dit_steps_info": "LocDiT 流匹配生成迭代步数 — 步数越多 → 可能生成更好的音频质量，但速度变慢",
        "usage_instructions": _USAGE_INSTRUCTIONS_ZH,
        "examples_footer": _EXAMPLES_FOOTER_ZH,
    },
    "zh-Hans": None,  # alias, filled below
    "zh": None,       # alias, filled below
}
_I18N_TRANSLATIONS["zh-Hans"] = _I18N_TRANSLATIONS["zh-CN"]
_I18N_TRANSLATIONS["zh"] = _I18N_TRANSLATIONS["zh-CN"]

for _d in _I18N_TRANSLATIONS.values():
    if _d is not None:
        for _k, _v in _I18N_TRANSLATIONS["en"].items():
            _d.setdefault(_k, _v)

I18N = gr.I18n(**_I18N_TRANSLATIONS)

DEFAULT_TARGET_TEXT = (
    "VoxCPM2 is a creative multilingual TTS model from ModelBest, "
    "designed to generate highly realistic speech."
)

_CUSTOM_CSS = """
.logo-container {
    text-align: center;
    margin: 0.5rem 0 1rem 0;
}
.logo-container img {
    height: 80px;
    width: auto;
    max-width: 200px;
    display: inline-block;
}

/* Toggle switch style */
.switch-toggle {
    padding: 8px 12px;
    border-radius: 8px;
    background: var(--block-background-fill);
}
.switch-toggle input[type="checkbox"] {
    appearance: none;
    -webkit-appearance: none;
    width: 44px;
    height: 24px;
    background: #ccc;
    border-radius: 12px;
    position: relative;
    cursor: pointer;
    transition: background 0.3s ease;
    flex-shrink: 0;
}
.switch-toggle input[type="checkbox"]::after {
    content: "";
    position: absolute;
    top: 2px;
    left: 2px;
    width: 20px;
    height: 20px;
    background: white;
    border-radius: 50%;
    transition: transform 0.3s ease;
    box-shadow: 0 1px 3px rgba(0,0,0,0.2);
}
.switch-toggle input[type="checkbox"]:checked {
    background: var(--color-accent);
}
.switch-toggle input[type="checkbox"]:checked::after {
    transform: translateX(20px);
}
.vox-subtitle {
    margin: 0 0 1rem 0;
    padding: 14px 16px;
    border-radius: 14px;
    border: 1px solid #dbeafe;
    background: linear-gradient(135deg, #eff6ff, #f8fafc);
    color: #334155;
    line-height: 1.7;
}
.vox-subtitle a {
    color: #2563eb;
    text-decoration: none;
    font-weight: 600;
}
.lf-panel {
    border: 1px solid rgba(148, 163, 184, 0.3);
    border-radius: 16px;
    padding: 16px;
    background: rgba(255, 255, 255, 0.68);
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
    flex-wrap: wrap;
    margin-bottom: 12px;
}
.lf-toolbar .gradio-button {
    min-width: 120px;
}
.lf-empty {
    padding: 18px;
    border: 1px dashed rgba(148, 163, 184, 0.5);
    border-radius: 14px;
    text-align: center;
    color: #64748b;
    background: rgba(248, 250, 252, 0.9);
}
.lf-sentence {
    border: 1px solid rgba(148, 163, 184, 0.35);
    border-radius: 14px;
    padding: 12px;
    margin-bottom: 10px;
    background: rgba(255, 255, 255, 0.92);
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
    display: inline-flex;
    gap: 8px;
    align-items: center;
    font-size: 0.85rem;
}
.lf-status.pending {background: #eff6ff; color: #1d4ed8;}
.lf-status.success {background: #ecfdf5; color: #047857;}
.lf-status.error {background: #fef2f2; color: #b91c1c;}
.lf-status.running {background: #fff7ed; color: #c2410c;}
"""

_APP_THEME = gr.themes.Soft(
    primary_hue="blue",
    secondary_hue="gray",
    neutral_hue="slate",
    font=[gr.themes.GoogleFont("Inter"), "Arial", "sans-serif"],
)


# ---------- Model ----------

class VoxCPMDemo:
    def __init__(self, model_id: str = "models/VoxCPM2") -> None:
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info(f"Running on device: {self.device}")

        self.asr_model_id = "iic/SenseVoiceSmall"
        self.asr_model: Optional[AutoModel] = AutoModel(
            model=self.asr_model_id,
            disable_update=True,
            log_level="DEBUG",
            device="cuda:0" if self.device == "cuda" else "cpu",
        )

        self.voxcpm_model: Optional[voxcpm.VoxCPM] = None
        self._mgr = ModelsManager()
        self._model_id = self._mgr.resolve_path(model_id, model_type="vox")

    def get_or_load_voxcpm(self) -> voxcpm.VoxCPM:
        if self.voxcpm_model is not None:
            return self.voxcpm_model
        logger.info(f"Loading model: {self._model_id}")
        self.voxcpm_model = voxcpm.VoxCPM.from_pretrained(self._model_id, optimize=True)
        logger.info("Model loaded successfully.")
        return self.voxcpm_model

    def prompt_wav_recognition(self, prompt_wav: Optional[str]) -> str:
        if prompt_wav is None:
            return ""
        res = self.asr_model.generate(input=prompt_wav, language="auto", use_itn=True)
        return res[0]["text"].split("|>")[-1]

    def _build_generate_kwargs(
        self,
        *,
        final_text: str,
        audio_path: Optional[str],
        prompt_text_clean: Optional[str],
        cfg_value_input: float,
        do_normalize: bool,
        denoise: bool,
        inference_timesteps: int = 10,
    ) -> dict:
        generate_kwargs = dict(
            text=final_text,
            reference_wav_path=audio_path,
            cfg_value=float(cfg_value_input),
            inference_timesteps=inference_timesteps,
            normalize=do_normalize,
            denoise=denoise,
        )
        if prompt_text_clean and audio_path:
            generate_kwargs["prompt_wav_path"] = audio_path
            generate_kwargs["prompt_text"] = prompt_text_clean
        return generate_kwargs

    def generate_tts_audio(
        self,
        text_input: str,
        control_instruction: str = "",
        reference_wav_path_input: Optional[str] = None,
        prompt_text: str = "",
        cfg_value_input: float = 2.0,
        do_normalize: bool = True,
        denoise: bool = True,
        inference_timesteps: int = 10,
    ) -> Tuple[int, np.ndarray]:
        current_model = self.get_or_load_voxcpm()

        text = (text_input or "").strip()
        if len(text) == 0:
            raise ValueError("Please input text to synthesize.")

        control = (control_instruction or "").strip()
        # Strip any parentheses (half-width/full-width) from control text to avoid
        # breaking the "(control)text" prompt format expected by the model.
        control = re.sub(r"[()（）]", "", control).strip()
        final_text = f"({control}){text}" if control else text

        audio_path = reference_wav_path_input if reference_wav_path_input else None
        prompt_text_clean = (prompt_text or "").strip() or None

        if audio_path and prompt_text_clean:
            logger.info(f"[Voice Cloning] prompt_wav + prompt_text + reference_wav")
        elif audio_path:
            logger.info(f"[Voice Control] reference_wav only")
        else:
            logger.info(f"[Voice Design] control: {control[:50] if control else 'None'}...")

        logger.info(f"Generating audio for text: '{final_text[:80]}...'")
        generate_kwargs = self._build_generate_kwargs(
            final_text=final_text,
            audio_path=audio_path,
            prompt_text_clean=prompt_text_clean,
            cfg_value_input=cfg_value_input,
            do_normalize=do_normalize,
            denoise=denoise,
            inference_timesteps=inference_timesteps,
        )
        wav = current_model.generate(**generate_kwargs)
        return (current_model.tts_model.sample_rate, wav)


# ---------- UI ----------

def create_demo_interface(demo: VoxCPMDemo):
    gr.set_static_paths(paths=[Path.cwd().absolute() / "assets"])
    output_root = os.path.join("outputs", "webui_longform", "voxcpm")

    def _generate(
        text: str,
        control_instruction: str,
        ref_wav: Optional[str],
        use_prompt_text: bool,
        prompt_text_value: str,
        cfg_value: float,
        do_normalize: bool,
        denoise: bool,
        dit_steps: int,
    ):
        actual_prompt_text = prompt_text_value.strip() if use_prompt_text else ""
        actual_control = "" if use_prompt_text else control_instruction
        sr, wav_np = demo.generate_tts_audio(
            text_input=text,
            control_instruction=actual_control,
            reference_wav_path_input=ref_wav,
            prompt_text=actual_prompt_text,
            cfg_value_input=cfg_value,
            do_normalize=do_normalize,
            denoise=denoise,
            inference_timesteps=int(dit_steps),
        )
        return (sr, wav_np)

    def _build_longform_instruction(*parts) -> str:
        cleaned = []
        for part in parts:
            if not part:
                continue
            value = str(part).strip()
            if value:
                cleaned.append(value)
        return "，".join(cleaned)

    def _new_session_dir() -> str:
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = os.path.join(output_root, session_id)
        os.makedirs(path, exist_ok=True)
        return path

    def _sentence_status(status: str, error: str = "") -> str:
        label_map = {
            "pending": "待生成",
            "success": "已生成",
            "error": "生成失败",
            "running": "生成中",
        }
        label = label_map.get(status or "pending", "待生成")
        extra = f"<span>{gr.utils.sanitize_html(error)}</span>" if error else ""
        return (
            f"<div class='lf-status {status or 'pending'}'>"
            f"<strong>{label}</strong>{extra}</div>"
        )

    def _blank_export():
        return gr.update(value=None)

    def _toggle_long_mode(mode: str):
        is_clone = mode == "克隆模式"
        return gr.update(visible=is_clone), gr.update(visible=not is_clone)

    def _toggle_long_ultimate(checked: bool):
        return gr.update(visible=checked), gr.update(visible=not checked)

    def _split_text(text: str, rules):
        raw = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not raw:
            return [], "", "请输入需要断句的长文内容。", _blank_export()

        parts = [raw]
        selected_rules = set(rules or [])
        if "换行符" in selected_rules:
            line_parts = []
            for chunk in parts:
                line_parts.extend([p.strip() for p in re.split(r"\n+", chunk) if p.strip()])
            parts = line_parts or parts

        punctuation_chars = ""
        if "句号" in selected_rules:
            punctuation_chars += r"\.。"
        if "问号" in selected_rules:
            punctuation_chars += r"\?？"
        if "感叹号" in selected_rules:
            punctuation_chars += r"!！"

        if punctuation_chars:
            pattern = rf"(?<=[{punctuation_chars}])\s*"
            split_parts = []
            for chunk in parts:
                split_parts.extend([p.strip() for p in re.split(pattern, chunk) if p.strip()])
            parts = split_parts or parts

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

    def _select_all(items):
        updated = [dict(item, selected=True) for item in (items or [])]
        return updated, (f"已全选 {len(updated)} 句。" if updated else "暂无句子可选。"), _blank_export()

    def _update_selected(value, items, idx):
        updated = [dict(item) for item in (items or [])]
        if 0 <= idx < len(updated):
            updated[idx]["selected"] = bool(value)
        return updated, _blank_export()

    def _reset_item(item, *, text=None, instruction=None):
        updated = dict(item)
        if text is not None:
            updated["text"] = text
        if instruction is not None:
            updated["instruction_override"] = instruction
        updated["audio_path"] = None
        updated["status"] = "pending"
        updated["error"] = ""
        return updated

    def _update_text(value, items, idx):
        updated = [dict(item) for item in (items or [])]
        if 0 <= idx < len(updated):
            updated[idx] = _reset_item(updated[idx], text=(value or "").strip())
        return updated, _blank_export()

    def _update_instruction(value, items, idx):
        updated = [dict(item) for item in (items or [])]
        if 0 <= idx < len(updated):
            updated[idx] = _reset_item(updated[idx], instruction=(value or "").strip())
        return updated, _blank_export()

    def _save_audio(audio_data, session_dir, idx):
        if not session_dir:
            session_dir = _new_session_dir()
        os.makedirs(session_dir, exist_ok=True)
        sr, wav = audio_data
        output_path = os.path.join(
            session_dir,
            f"{idx:04d}_{datetime.now().strftime('%H%M%S_%f')}.wav",
        )
        sf.write(output_path, wav, sr)
        return session_dir, output_path

    def _generate_one(
        item,
        idx,
        session_dir,
        mode,
        clone_ref_audio,
        clone_use_prompt_text,
        clone_prompt_text,
        clone_instruction,
        cfg_value,
        do_normalize,
        denoise,
        dit_steps,
        design_free_text,
        design_gender,
        design_age,
        design_emotion,
        design_pace,
        design_accent,
        design_style,
    ):
        text_value = (item.get("text") or "").strip()
        if not text_value:
            updated = _reset_item(item, text="")
            updated["status"] = "error"
            updated["error"] = "句子内容为空。"
            return updated, session_dir

        sentence_instruction = (item.get("instruction_override") or "").strip()
        if mode == "克隆模式":
            if not clone_ref_audio:
                updated = _reset_item(item)
                updated["status"] = "error"
                updated["error"] = "请先上传参考音频。"
                return updated, session_dir

            actual_prompt_text = ""
            actual_control = _build_longform_instruction(clone_instruction, sentence_instruction)
            if clone_use_prompt_text:
                actual_control = ""
                actual_prompt_text = (clone_prompt_text or "").strip()
                if not actual_prompt_text:
                    actual_prompt_text = demo.prompt_wav_recognition(clone_ref_audio)

            audio_data = demo.generate_tts_audio(
                text_input=text_value,
                control_instruction=actual_control,
                reference_wav_path_input=clone_ref_audio,
                prompt_text=actual_prompt_text,
                cfg_value_input=float(cfg_value),
                do_normalize=bool(do_normalize),
                denoise=bool(denoise),
                inference_timesteps=int(dit_steps),
            )
        else:
            actual_control = _build_longform_instruction(
                design_free_text,
                design_gender if design_gender != "自动" else "",
                design_age if design_age != "自动" else "",
                design_emotion if design_emotion != "自动" else "",
                design_pace if design_pace != "自动" else "",
                design_accent if design_accent != "自动" else "",
                design_style if design_style != "自动" else "",
                sentence_instruction,
            )
            audio_data = demo.generate_tts_audio(
                text_input=text_value,
                control_instruction=actual_control,
                reference_wav_path_input=None,
                prompt_text="",
                cfg_value_input=float(cfg_value),
                do_normalize=bool(do_normalize),
                denoise=bool(denoise),
                inference_timesteps=int(dit_steps),
            )

        updated = dict(item)
        try:
            session_dir, audio_path = _save_audio(audio_data, session_dir, idx)
        except Exception as exc:
            updated["audio_path"] = None
            updated["status"] = "error"
            updated["error"] = str(exc)
            return updated, session_dir

        updated["audio_path"] = audio_path
        updated["status"] = "success"
        updated["error"] = ""
        return updated, session_dir

    def _run_generation(
        items,
        session_dir,
        run_all,
        mode,
        clone_ref_audio,
        clone_use_prompt_text,
        clone_prompt_text,
        clone_instruction,
        cfg_value,
        do_normalize,
        denoise,
        dit_steps,
        design_free_text,
        design_gender,
        design_age,
        design_emotion,
        design_pace,
        design_accent,
        design_style,
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
            try:
                updated, current_session = _generate_one(
                    current_items[idx],
                    idx,
                    current_session,
                    mode,
                    clone_ref_audio,
                    clone_use_prompt_text,
                    clone_prompt_text,
                    clone_instruction,
                    cfg_value,
                    do_normalize,
                    denoise,
                    dit_steps,
                    design_free_text,
                    design_gender,
                    design_age,
                    design_emotion,
                    design_pace,
                    design_accent,
                    design_style,
                )
            except Exception as exc:
                updated = dict(current_items[idx])
                updated["audio_path"] = None
                updated["status"] = "error"
                updated["error"] = str(exc)
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
        clone_ref_audio,
        clone_use_prompt_text,
        clone_prompt_text,
        clone_instruction,
        cfg_value,
        do_normalize,
        denoise,
        dit_steps,
        design_free_text,
        design_gender,
        design_age,
        design_emotion,
        design_pace,
        design_accent,
        design_style,
    ):
        current_items = [dict(item) for item in (items or [])]
        if not (0 <= idx < len(current_items)):
            return current_items, session_dir, "句子索引无效。", _blank_export()

        current_session = session_dir or _new_session_dir()
        try:
            updated, current_session = _generate_one(
                current_items[idx],
                idx,
                current_session,
                mode,
                clone_ref_audio,
                clone_use_prompt_text,
                clone_prompt_text,
                clone_instruction,
                cfg_value,
                do_normalize,
                denoise,
                dit_steps,
                design_free_text,
                design_gender,
                design_age,
                design_emotion,
                design_pace,
                design_accent,
                design_style,
            )
        except Exception as exc:
            updated = dict(current_items[idx])
            updated["audio_path"] = None
            updated["status"] = "error"
            updated["error"] = str(exc)
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
            data, sr = sf.read(item["audio_path"], dtype="float32")
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
        output_path = os.path.join(
            export_dir,
            f"voxcpm_longform_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav",
        )
        sf.write(output_path, merged, export_sr or 24000)
        return output_path, f"导出完成：{os.path.basename(output_path)}"

    def _on_toggle_instant(checked):
        """Instant UI toggle — no ASR, no blocking."""
        if checked:
            return (
                gr.update(visible=True, value="", placeholder="Recognizing reference audio..."),
                gr.update(visible=False),
            )
        return (
            gr.update(visible=False),
            gr.update(visible=True, interactive=True),
        )

    def _run_asr_if_needed(checked, audio_path):
        """Run ASR after the UI has updated. Only when toggled ON."""
        if not checked or not audio_path:
            return gr.update()
        try:
            logger.info("Running ASR on reference audio...")
            asr_text = demo.prompt_wav_recognition(audio_path)
            logger.info(f"ASR result: {asr_text[:60]}...")
            return gr.update(value=asr_text)
        except Exception as e:
            logger.warning(f"ASR recognition failed: {e}")
            return gr.update(value="")

    with gr.Blocks() as interface:
        gr.HTML(
            '<div class="logo-container">'
            '<img src="/gradio_api/file=assets/voxcpm_logo.png" alt="VoxCPM Logo">'
            "</div>"
        )

        gr.HTML(
            """
<div class="vox-subtitle">
  <div><strong>整合包作者：</strong>licor</div>
  <div><strong>B站ID：</strong>单手飞机</div>
  <div><strong>更多AI整合包资源：</strong><a href="https://www.kdocs.cn/l/ch0DFzxFCWhA" target="_blank">https://www.kdocs.cn/l/ch0DFzxFCWhA</a></div>
</div>
"""
        )

        with gr.Tabs():
            with gr.Tab("基础配音"):
                gr.Markdown(I18N("usage_instructions"))

                with gr.Row():
                    with gr.Column():
                        reference_wav = gr.Audio(
                            sources=["upload", "microphone"],
                            type="filepath",
                            label=I18N("reference_audio_label"),
                        )
                        show_prompt_text = gr.Checkbox(
                            value=False,
                            label=I18N("show_prompt_text_label"),
                            info=I18N("show_prompt_text_info"),
                            elem_classes=["switch-toggle"],
                        )
                        prompt_text = gr.Textbox(
                            value="",
                            label=I18N("prompt_text_label"),
                            placeholder=I18N("prompt_text_placeholder"),
                            lines=2,
                            visible=False,
                        )
                        control_instruction = gr.Textbox(
                            value="",
                            label=I18N("control_label"),
                            placeholder=I18N("control_placeholder"),
                            lines=2,
                        )
                        text = gr.Textbox(
                            value=DEFAULT_TARGET_TEXT,
                            label=I18N("target_text_label"),
                            lines=3,
                        )

                        with gr.Accordion(I18N("advanced_settings_title"), open=False):
                            DoDenoisePromptAudio = gr.Checkbox(
                                value=False,
                                label=I18N("ref_denoise_label"),
                                elem_classes=["switch-toggle"],
                                info=I18N("ref_denoise_info"),
                            )
                            DoNormalizeText = gr.Checkbox(
                                value=False,
                                label=I18N("normalize_label"),
                                elem_classes=["switch-toggle"],
                                info=I18N("normalize_info"),
                            )
                            cfg_value = gr.Slider(
                                minimum=1.0,
                                maximum=3.0,
                                value=2.0,
                                step=0.1,
                                label=I18N("cfg_label"),
                                info=I18N("cfg_info"),
                            )
                            dit_steps = gr.Slider(
                                minimum=1,
                                maximum=50,
                                value=10,
                                step=1,
                                label=I18N("dit_steps_label"),
                                info=I18N("dit_steps_info"),
                            )

                        run_btn = gr.Button(I18N("generate_btn"), variant="primary", size="lg")

                    with gr.Column():
                        audio_output = gr.Audio(label=I18N("generated_audio_label"))
                        gr.Markdown(I18N("examples_footer"))

                show_prompt_text.change(
                    fn=_on_toggle_instant,
                    inputs=[show_prompt_text],
                    outputs=[prompt_text, control_instruction],
                ).then(
                    fn=_run_asr_if_needed,
                    inputs=[show_prompt_text, reference_wav],
                    outputs=[prompt_text],
                )

                run_btn.click(
                    fn=_generate,
                    inputs=[
                        text,
                        control_instruction,
                        reference_wav,
                        show_prompt_text,
                        prompt_text,
                        cfg_value,
                        DoNormalizeText,
                        DoDenoisePromptAudio,
                        dit_steps,
                    ],
                    outputs=[audio_output],
                    show_progress=True,
                    api_name="generate",
                )

            with gr.Tab("长文配音"):
                sentence_state = gr.State([])
                session_dir_state = gr.State("")

                with gr.Group(elem_classes="lf-panel"):
                    lf_text = gr.Textbox(
                        label="长文输入",
                        lines=14,
                        placeholder="粘贴需要批量配音的长文内容，支持滚动编辑。",
                    )
                    lf_mode = gr.Radio(
                        ["克隆模式", "音色描述模式"],
                        value="克隆模式",
                        label="配音模式",
                    )
                    lf_rules = gr.CheckboxGroup(
                        label="断句规则",
                        choices=["换行符", "句号", "问号", "感叹号"],
                        value=["换行符", "句号", "问号", "感叹号"],
                    )

                    with gr.Group(visible=True) as lf_clone_group:
                        lf_clone_ref_audio = gr.Audio(
                            sources=["upload", "microphone"],
                            type="filepath",
                            label="参考音频",
                        )
                        lf_clone_use_prompt = gr.Checkbox(
                            value=False,
                            label="极致克隆模式",
                            info="开启后将基于参考音频文本进行续写，长文内将禁用风格控制指令。",
                            elem_classes=["switch-toggle"],
                        )
                        lf_clone_prompt_text = gr.Textbox(
                            value="",
                            label="参考音频文本（可留空，生成时自动识别）",
                            lines=2,
                            visible=False,
                        )
                        lf_clone_instruction = gr.Textbox(
                            value="",
                            label="克隆风格控制指令",
                            placeholder="如：温柔、低沉、带一点疲惫感",
                            lines=2,
                        )

                    with gr.Group(visible=False) as lf_design_group:
                        lf_design_free = gr.Textbox(
                            value="",
                            label="音色描述主指令",
                            placeholder="如：年轻女性，温柔甜美，语速轻快，像电台主持人",
                            lines=3,
                        )
                        with gr.Row():
                            lf_design_gender = gr.Dropdown(
                                choices=["自动", "男声", "女声", "少年感", "少女感"],
                                value="自动",
                                label="性别/主体",
                            )
                            lf_design_age = gr.Dropdown(
                                choices=["自动", "儿童", "青年", "中年", "老年"],
                                value="自动",
                                label="年龄感",
                            )
                        with gr.Row():
                            lf_design_emotion = gr.Dropdown(
                                choices=["自动", "开心", "兴奋", "悲伤", "愤怒", "温柔", "平静", "严肃"],
                                value="自动",
                                label="情绪",
                            )
                            lf_design_pace = gr.Dropdown(
                                choices=["自动", "语速很慢", "语速偏慢", "语速适中", "语速偏快", "语速很快"],
                                value="自动",
                                label="节奏/语速",
                            )
                        with gr.Row():
                            lf_design_accent = gr.Dropdown(
                                choices=["自动", "普通话", "四川话", "粤语", "吴语", "东北话", "河南话", "陕西话", "美式口音", "英式口音", "澳式口音"],
                                value="自动",
                                label="方言/口音",
                            )
                            lf_design_style = gr.Dropdown(
                                choices=["自动", "耳语", "磁性", "元气", "慵懒", "播音腔", "故事感", "纪录片旁白"],
                                value="自动",
                                label="音色风格",
                            )

                    with gr.Accordion("长文配音高级设置", open=False):
                        lf_denoise = gr.Checkbox(
                            value=False,
                            label=I18N("ref_denoise_label"),
                            elem_classes=["switch-toggle"],
                            info=I18N("ref_denoise_info"),
                        )
                        lf_normalize = gr.Checkbox(
                            value=False,
                            label=I18N("normalize_label"),
                            elem_classes=["switch-toggle"],
                            info=I18N("normalize_info"),
                        )
                        lf_cfg_value = gr.Slider(
                            minimum=1.0,
                            maximum=3.0,
                            value=2.0,
                            step=0.1,
                            label=I18N("cfg_label"),
                            info=I18N("cfg_info"),
                        )
                        lf_dit_steps = gr.Slider(
                            minimum=1,
                            maximum=50,
                            value=10,
                            step=1,
                            label=I18N("dit_steps_label"),
                            info=I18N("dit_steps_info"),
                        )
                    lf_split_btn = gr.Button("断句", variant="primary")

                with gr.Group(elem_classes="lf-panel"):
                    with gr.Row(elem_classes="lf-toolbar"):
                        lf_select_all_btn = gr.Button("全选")
                        lf_generate_btn = gr.Button("批量生成选中句子", variant="primary")
                        lf_regenerate_all_btn = gr.Button("全部重新生成")
                        lf_gap = gr.Number(value=0.3, precision=2, label="句子间隔（秒）")
                        lf_export_btn = gr.Button("导出")
                    lf_export_file = gr.File(label="导出音频", interactive=False)
                    lf_status = gr.Markdown("请先输入长文并点击 `断句`。")

                    @gr.render(inputs=sentence_state)
                    def render_sentences(items):
                        if not items:
                            gr.HTML(
                                "<div class='lf-empty'>断句后，这里会出现句子列表，可逐句编辑文本、补充配音指令并单独重生成。</div>"
                            )
                            return

                        for idx, item in enumerate(items):
                            with gr.Group(elem_classes="lf-sentence", key=f"vox_lf_sentence_{item['id']}"):
                                with gr.Row(elem_classes="lf-sentence-head"):
                                    selected = gr.Checkbox(
                                        value=item.get("selected", True),
                                        label=f"第 {idx + 1} 句",
                                        scale=2,
                                        key=f"vox_lf_selected_{item['id']}",
                                    )
                                    gr.HTML(
                                        _sentence_status(item.get("status"), item.get("error", "")),
                                        scale=3,
                                        key=f"vox_lf_status_{item['id']}",
                                    )
                                    regen_btn = gr.Button(
                                        "重生成",
                                        scale=1,
                                        key=f"vox_lf_regen_{item['id']}",
                                    )

                                with gr.Row(elem_classes="lf-sentence-fields"):
                                    sentence_box = gr.Textbox(
                                        value=item.get("text", ""),
                                        label="句子文本（可编辑）",
                                        lines=2,
                                        key=f"vox_lf_text_{item['id']}",
                                    )
                                    instruction_box = gr.Textbox(
                                        value=item.get("instruction_override", ""),
                                        label="配音指令描述",
                                        lines=2,
                                        placeholder="默认留空。可单独给当前句补充情绪、语气、停顿感等描述。",
                                        key=f"vox_lf_instruction_{item['id']}",
                                    )
                                with gr.Row(elem_classes="lf-sentence-audio"):
                                    gr.Audio(
                                        value=item.get("audio_path") if item.get("audio_path") else None,
                                        label="播放",
                                        interactive=False,
                                        scale=1,
                                        elem_classes="lf-compact-audio",
                                        key=f"vox_lf_audio_{item['id']}",
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
                                    lambda items, session_dir, mode, clone_ref_audio, clone_use_prompt, clone_prompt_text, clone_instruction, cfg_value, do_normalize, denoise, dit_steps, design_free_text, design_gender, design_age, design_emotion, design_pace, design_accent, design_style, idx=idx: _regenerate_single(
                                        items,
                                        session_dir,
                                        idx,
                                        mode,
                                        clone_ref_audio,
                                        clone_use_prompt,
                                        clone_prompt_text,
                                        clone_instruction,
                                        cfg_value,
                                        do_normalize,
                                        denoise,
                                        dit_steps,
                                        design_free_text,
                                        design_gender,
                                        design_age,
                                        design_emotion,
                                        design_pace,
                                        design_accent,
                                        design_style,
                                    ),
                                    inputs=[
                                        sentence_state,
                                        session_dir_state,
                                        lf_mode,
                                        lf_clone_ref_audio,
                                        lf_clone_use_prompt,
                                        lf_clone_prompt_text,
                                        lf_clone_instruction,
                                        lf_cfg_value,
                                        lf_normalize,
                                        lf_denoise,
                                        lf_dit_steps,
                                        lf_design_free,
                                        lf_design_gender,
                                        lf_design_age,
                                        lf_design_emotion,
                                        lf_design_pace,
                                        lf_design_accent,
                                        lf_design_style,
                                    ],
                                    outputs=[sentence_state, session_dir_state, lf_status, lf_export_file],
                                )

                lf_mode.change(
                    _toggle_long_mode,
                    inputs=[lf_mode],
                    outputs=[lf_clone_group, lf_design_group],
                )
                lf_clone_use_prompt.change(
                    _toggle_long_ultimate,
                    inputs=[lf_clone_use_prompt],
                    outputs=[lf_clone_prompt_text, lf_clone_instruction],
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
                    lambda items, session_dir, mode, clone_ref_audio, clone_use_prompt, clone_prompt_text, clone_instruction, cfg_value, do_normalize, denoise, dit_steps, design_free_text, design_gender, design_age, design_emotion, design_pace, design_accent, design_style: _run_generation(
                        items,
                        session_dir,
                        False,
                        mode,
                        clone_ref_audio,
                        clone_use_prompt,
                        clone_prompt_text,
                        clone_instruction,
                        cfg_value,
                        do_normalize,
                        denoise,
                        dit_steps,
                        design_free_text,
                        design_gender,
                        design_age,
                        design_emotion,
                        design_pace,
                        design_accent,
                        design_style,
                    ),
                    inputs=[
                        sentence_state,
                        session_dir_state,
                        lf_mode,
                        lf_clone_ref_audio,
                        lf_clone_use_prompt,
                        lf_clone_prompt_text,
                        lf_clone_instruction,
                        lf_cfg_value,
                        lf_normalize,
                        lf_denoise,
                        lf_dit_steps,
                        lf_design_free,
                        lf_design_gender,
                        lf_design_age,
                        lf_design_emotion,
                        lf_design_pace,
                        lf_design_accent,
                        lf_design_style,
                    ],
                    outputs=[sentence_state, session_dir_state, lf_status, lf_export_file],
                )
                lf_regenerate_all_btn.click(
                    lambda items, session_dir, mode, clone_ref_audio, clone_use_prompt, clone_prompt_text, clone_instruction, cfg_value, do_normalize, denoise, dit_steps, design_free_text, design_gender, design_age, design_emotion, design_pace, design_accent, design_style: _run_generation(
                        items,
                        session_dir,
                        True,
                        mode,
                        clone_ref_audio,
                        clone_use_prompt,
                        clone_prompt_text,
                        clone_instruction,
                        cfg_value,
                        do_normalize,
                        denoise,
                        dit_steps,
                        design_free_text,
                        design_gender,
                        design_age,
                        design_emotion,
                        design_pace,
                        design_accent,
                        design_style,
                    ),
                    inputs=[
                        sentence_state,
                        session_dir_state,
                        lf_mode,
                        lf_clone_ref_audio,
                        lf_clone_use_prompt,
                        lf_clone_prompt_text,
                        lf_clone_instruction,
                        lf_cfg_value,
                        lf_normalize,
                        lf_denoise,
                        lf_dit_steps,
                        lf_design_free,
                        lf_design_gender,
                        lf_design_age,
                        lf_design_emotion,
                        lf_design_pace,
                        lf_design_accent,
                        lf_design_style,
                    ],
                    outputs=[sentence_state, session_dir_state, lf_status, lf_export_file],
                )
                lf_export_btn.click(
                    _export_audio,
                    inputs=[sentence_state, session_dir_state, lf_gap],
                    outputs=[lf_export_file, lf_status],
                )

    return interface

def run_demo(
    server_name: str = "localhost",
    server_port: int = 8851,
    show_error: bool = True,
    model_id: str = "./models/VoxCPM2",
):
    demo = VoxCPMDemo(model_id=model_id)
    interface = create_demo_interface(demo)
    interface.queue(max_size=10, default_concurrency_limit=1).launch(
        server_name=server_name,
        server_port=server_port,
        show_error=show_error,
        i18n=I18N,
        theme=_APP_THEME,
        css=_CUSTOM_CSS,
    )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model-id", type=str, default="./models/VoxCPM2",
        help="Local path or HuggingFace repo ID (default: openbmb/VoxCPM2)",
    )
    parser.add_argument("--port", type=int, default=8808, help="Server port")
    args = parser.parse_args()
    run_demo(model_id=args.model_id, server_port=args.port)
