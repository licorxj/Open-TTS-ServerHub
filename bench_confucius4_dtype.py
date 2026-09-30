#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Confucius4-TTS 半精度速率对比测试

对每个 dtype (fp32 / fp16 / bf16) 分别加载模型，合成同一段文本，
并分别计时各子模型的推理耗时：
  - Wav2Vec2-BERT   (参考音频语义特征提取, CPU)
  - CAMPPlus        (说话人风格嵌入提取,     CPU)
  - ref mel         (参考 mel 频谱提取,       CPU)
  - T2S             (自回归 LLM: 文本 -> 语义 token, GPU)
  - S2A             (flow-matching 扩散: 语义 -> mel,  GPU)
  - BigVGAN         (声码器: mel -> 波形,            GPU, 恒 fp32)

用法:
  py312env/python.exe bench_confucius4_dtype.py
  py312env/python.exe bench_confucius4_dtype.py --dtypes fp32,fp16 --runs 5
"""
import os
import sys
import time
import argparse
import functools
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFUCIUS4_ROOT = os.path.join(PROJECT_ROOT, "Confucius4-TTS")
MODELS_DIR = os.path.join(PROJECT_ROOT, "models", "Confucius4")
PRETRAINED_DIR = os.path.join(PROJECT_ROOT, "models", "pretrained")

sys.path.insert(0, CONFUCIUS4_ROOT)
PATCH_PKG = os.path.join(PROJECT_ROOT, "packages", "index25")
if os.path.isdir(PATCH_PKG):
    sys.path.insert(0, PATCH_PKG)

import yaml
import torch
import huggingface_hub

# ============== 本地模型映射补丁 (与 confucius4_api_server.py 一致) ==============
_ORIG_HF = huggingface_hub.hf_hub_download
_BIGVGAN_LOCAL = os.path.normpath(os.path.join(MODELS_DIR, "..", "index2", "hf_cache", "bigvgan"))
_LOCAL_MODEL_MAP = {
    "netease-youdao/Confucius4-TTS": MODELS_DIR,
    "funasr/campplus": os.path.join(PRETRAINED_DIR, "campplus"),
    "nvidia/bigvgan_v2_22khz_80band_256x": _BIGVGAN_LOCAL,
}


def _patched_hf_download(repo_id, filename, **kwargs):
    for prefix, local_dir in _LOCAL_MODEL_MAP.items():
        if repo_id == prefix or repo_id.endswith(prefix):
            lp = os.path.join(local_dir, filename)
            if os.path.exists(lp):
                return lp
    return _ORIG_HF(repo_id, filename, **kwargs)


huggingface_hub.hf_hub_download = _patched_hf_download


def apply_patches():
    """应用与 confucius4_api_server.py:load_model 一致的兼容性补丁。"""
    # w2v-bert from_pretrained 本地化
    from transformers import AutoModel, Wav2Vec2BertModel, SeamlessM4TFeatureExtractor
    w2v_local = os.path.join(PRETRAINED_DIR, "w2v-bert-2.0")
    if os.path.isdir(w2v_local):
        for cls in [AutoModel, Wav2Vec2BertModel]:
            if not hasattr(cls.from_pretrained, "_patched"):
                _o = cls.from_pretrained

                def _mk(o):
                    def _lp(n, *a, **k):
                        if n == "facebook/w2v-bert-2.0":
                            n = w2v_local
                        return o(n, *a, **k)
                    _lp._patched = True
                    return _lp
                cls.from_pretrained = _mk(_o)
        if not hasattr(SeamlessM4TFeatureExtractor.from_pretrained, "_patched"):
            _ose = SeamlessM4TFeatureExtractor.from_pretrained

            def _lse(n, *a, **k):
                if n == "facebook/w2v-bert-2.0":
                    n = w2v_local
                return _ose(n, *a, **k)
            _lse._patched = True
            SeamlessM4TFeatureExtractor.from_pretrained = _lse

    # Text2SemanticConfig 兼容补丁
    try:
        from confuciustts.llm.llm import Text2SemanticConfig
        _o = Text2SemanticConfig.__init__

        def _ci(self, *a, **k):
            _o(self, *a, **k)
            if not hasattr(self, "num_hidden_layers"):
                self.num_hidden_layers = self.num_layers
            if not hasattr(self, "hidden_size"):
                self.hidden_size = self.model_dim
            if not hasattr(self, "num_attention_heads"):
                self.num_attention_heads = self.num_heads
        Text2SemanticConfig.__init__ = _ci
    except Exception as e:
        print("[WARN] Text2SemanticConfig patch failed:", e)

    # Text2Semantic._reorder_cache 兼容补丁 (None past_state)
    try:
        from confuciustts.llm.llm import Text2Semantic
        if not hasattr(Text2Semantic._reorder_cache, "_patched"):
            def _src(pkv, beam_idx):
                out = []
                for lp in pkv:
                    rl = []
                    for ps in lp:
                        rl.append(ps.index_select(0, beam_idx.to(ps.device)) if ps is not None else None)
                    out.append(tuple(rl))
                return tuple(out)
            _src._patched = True
            Text2Semantic._reorder_cache = staticmethod(_src)
    except Exception as e:
        print("[WARN] _reorder_cache patch failed:", e)

    # 注意：本环境固定使用 packages/index25 的 transformers 4.52.1（原生支持 tuple KV cache），
    # 不再需要给 forward/prepare_inputs_for_generation 套 *args/**kwargs 包装做 DynamicCache 转换
    # —— 那类包装会导致 inspect.signature(self.forward) 无法穿透到含 attention_mask 的原始签名，
    # 触发 transformers 的 _validate_model_kwargs 误报 "attention_mask not used by model"。
    # 因此此处【不】对 forward 施加任何会改变签名的补丁。

    # BigVGAN._from_pretrained 兼容补丁
    try:
        from external.bigvgan.bigvgan import BigVGAN
        if not hasattr(BigVGAN._from_pretrained, "_patched"):
            _ob = BigVGAN._from_pretrained.__func__

            @functools.wraps(_ob)
            def _cb(cls, *, proxies=None, resume_download=None, **k):
                return _ob(cls, proxies=proxies, resume_download=resume_download, **k)
            _cb._patched = True
            BigVGAN._from_pretrained = classmethod(_cb)
    except Exception as e:
        print("[WARN] BigVGAN patch failed:", e)


def load_inf_cfg():
    defaults = dict(
        temperature=0.8, top_p=0.8, top_k=30, num_beams=1,
        repetition_penalty=10.0, max_length=1520, n_timesteps=15,
        inference_cfg_rate=0.7, max_text_tokens_per_segment=80,
        cross_fade_duration=0.3,
    )
    path = os.path.join(PROJECT_ROOT, "config", "confucius4_server.yaml")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            defaults.update(cfg.get("inference", {}))
        except Exception:
            pass
    return defaults


def synth_once(model, ref_wav, text, lang, inf_cfg, timed):
    """合成一次, 可选返回各阶段耗时 (秒)。"""
    t0 = time.perf_counter()
    wav_16k, wav_tgt = model._load_prompt(ref_wav)
    t_load = time.perf_counter() - t0

    t0 = time.perf_counter()
    sem = model._extract_semantic(wav_16k)
    t_w2v = time.perf_counter() - t0

    t0 = time.perf_counter()
    sty = model._extract_style(wav_16k)
    t_camp = time.perf_counter() - t0

    t0 = time.perf_counter()
    mel = model._ref_mel(wav_tgt)
    t_mel = time.perf_counter() - t0

    # 将条件张量搬到推理设备 + 各自模型 dtype (半精度必须对齐)。
    # T2S 主模型保持 fp32；S2A 可单独半精度，故分别取 dtype。
    dt_t2s = next(model.t2s_model.parameters()).dtype
    dt_s2a = next(model.s2a_model.parameters()).dtype
    sem = sem.to(model.device, dt_t2s)   # T2S 的 condition
    sty = sty.to(model.device, dt_s2a)   # S2A 的说话人嵌入
    mel = mel.to(model.device, dt_s2a)   # S2A 的参考 mel

    text = model.normalizer.normalize(text, language=lang)
    segs = model.normalizer.segment_text(
        text, tokenize_fn=model.tokenizer.tokenize, language=lang,
        max_tokens=inf_cfg["max_text_tokens_per_segment"],
    )
    if not segs:
        segs = [text]

    from confuciustts.utils.text_utils import LANGUAGE_TOKEN_MAP
    t2s_times, s2a_times, voc_times = [], [], []
    chunks = []
    t_synth_start = time.perf_counter()
    for seg in segs:
        lang_token = LANGUAGE_TOKEN_MAP.get(lang, f"请用{lang}朗读接下来的文字")
        formatted = f"You are a helpful assistant. {lang_token}:{seg}"
        token_ids = model.tokenizer.encode(formatted, return_tensors="pt").to(model.device)

        t1 = time.perf_counter()
        t2s_out = model.t2s_model.generate(
            text_inputs=token_ids, condition_vector=sem,
            max_length=inf_cfg["max_length"], num_beams=inf_cfg["num_beams"],
            do_sample=True, top_p=inf_cfg["top_p"], top_k=inf_cfg["top_k"],
            temperature=inf_cfg["temperature"], repetition_penalty=inf_cfg["repetition_penalty"],
            return_latent=True,
        )
        t2s_times.append(time.perf_counter() - t1)

        semantic_codes = t2s_out["semantic_codes"]
        lm_latent = t2s_out["latent"]
        T = semantic_codes.shape[1]
        target_lengths = torch.tensor([int(T * 1.72)], device=model.device)

        t2 = time.perf_counter()
        mel_out = model.s2a_model.inference(
            semantic_token=semantic_codes, lm_latent=lm_latent.to(dt_s2a), prompt_feat=mel,
            embedding=sty, target_feat_len=target_lengths,
            n_timesteps=inf_cfg["n_timesteps"], inference_cfg_rate=inf_cfg["inference_cfg_rate"],
        )
        s2a_times.append(time.perf_counter() - t2)

        t3 = time.perf_counter()
        # BigVGAN 恒 fp32: mel 先 .float() 再入声码器
        audio = model.bigvgan(mel_out.float().to(model.device)).squeeze(1)
        voc_times.append(time.perf_counter() - t3)
        if audio.dim() == 1:
            audio = audio.unsqueeze(0)
        chunks.append(audio)

    from confuciustts.utils.audio_post import cross_fade_concat
    merged = cross_fade_concat(chunks, model.sample_rate, silence_duration=inf_cfg["cross_fade_duration"])
    synth_time = time.perf_counter() - t_synth_start
    audio_dur = merged.shape[-1] / model.sample_rate

    if timed:
        return dict(load=t_load, w2v=t_w2v, camp=t_camp, mel=t_mel,
                    t2s=t2s_times, s2a=s2a_times, voc=voc_times,
                    synth=synth_time, audio_dur=audio_dur, n_seg=len(segs))
    return None


def aggregate(results, dtype, peak):
    n = len(results)

    def mean(lst):
        return sum(lst) / len(lst) if lst else 0.0

    t2s_all = [x for r in results for x in r["t2s"]]
    s2a_all = [x for r in results for x in r["s2a"]]
    voc_all = [x for r in results for x in r["voc"]]
    avg_audio = mean([r["audio_dur"] for r in results])
    avg_synth = mean([r["synth"] for r in results])
    avg_w2v = mean([r["w2v"] for r in results])
    avg_camp = mean([r["camp"] for r in results])
    avg_mel = mean([r["mel"] for r in results])
    avg_load = mean([r["load"] for r in results])
    return dict(
        dtype=dtype, peak=peak, n_seg_total=len(t2s_all),
        avg_audio=avg_audio, avg_synth=avg_synth, avg_load=avg_load,
        avg_w2v=avg_w2v, avg_camp=avg_camp, avg_mel=avg_mel,
        avg_t2s=mean(t2s_all), avg_s2a=mean(s2a_all), avg_voc=mean(voc_all),
        rtf_synth=avg_synth / avg_audio if avg_audio > 0 else 0,
        rtf_total=(avg_w2v + avg_camp + avg_mel + avg_synth) / avg_audio if avg_audio > 0 else 0,
    )


def run_config(mode_key, attn_backend, half_s2a, ref_wav, text, lang, device, inf_cfg, runs, warmup):
    # 注意力后端需在 import confuciustts 之前设置环境变量
    os.environ["CONFUCIUS4_ATTN"] = attn_backend
    apply_patches()
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    torch.backends.cudnn.benchmark = True

    orig = os.getcwd()
    os.chdir(CONFUCIUS4_ROOT)
    try:
        from confuciustts.cli.inference import ConfuciusTTS
        model = ConfuciusTTS(
            config_path="config/inference_config.yaml", device=device,
            use_cuda_kernel=True, enable_ref_cache=False, ref_device="cpu",
        )
    finally:
        os.chdir(orig)

    # 重要：T2S(自回归 GPT2) 整体转半精度会在 LayerNorm/c_proj 处出现
    # Half/Float 错位 (RuntimeError: expected scalar type Float but found Half)，
    # 故 T2S 始终保持 fp32。可用的半精度提速路径有两条：
    #   1) 注意力后端用 flash_attention_2 —— fp32 模型内注意力以 fp16 计算，输出回 fp32；
    #   2) S2A(DiT 扩散) 单独转 bf16 —— 扩散模型对半精度耐受好。
    s2a_dtype_name = "fp32"
    if half_s2a:
        model.s2a_model = model.s2a_model.to(torch.bfloat16)
        s2a_dtype_name = "bf16"
        print(f"[{mode_key}] S2A 已单独转换为 bf16 (T2S 保持 fp32)")

    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()

    try:
        for _ in range(warmup):
            synth_once(model, ref_wav, text, lang, inf_cfg, timed=False)

        results = []
        for r in range(runs):
            res = synth_once(model, ref_wav, text, lang, inf_cfg, timed=True)
            results.append(res)
            print(f"[{mode_key}] run {r + 1}/{runs} 完成 | 音频 {res['audio_dur']:.2f}s | "
                  f"合成 {res['synth']:.2f}s | T2S {sum(res['t2s']):.2f}s "
                  f"S2A {sum(res['s2a']):.2f}s 声码器 {sum(res['voc']):.2f}s")
    except Exception as e:
        import traceback
        print(f"[{mode_key}] 运行失败: {e}")
        traceback.print_exc()
        del model
        torch.cuda.empty_cache()
        return {"dtype": mode_key, "error": str(e)}

    peak = torch.cuda.max_memory_allocated() / 1024 ** 3
    out = aggregate(results, mode_key, peak)
    del model
    torch.cuda.empty_cache()
    return out


def print_summary(all_res):
    print("\n" + "=" * 92)
    print(" Confucius4-TTS 推理速率对比 (各子模型平均耗时, 单位: 秒)")
    print("=" * 92)
    hdr = (f"{'mode':<14}{'音频':>8}{'T2S':>9}{'S2A':>9}{'声码器':>9}"
           f"{'w2v':>8}{'CAMP':>8}{'refMel':>8}{'合成总':>9}{'RTF合成':>9}{'显存GB':>8}")
    print(hdr)
    print("-" * 92)
    for r in all_res:
        if "error" in r:
            print(f"{r['dtype']:<14}  ERROR: {r['error']}")
            continue
        print(f"{r['dtype']:<14}{r['avg_audio']:>8.2f}{r['avg_t2s']:>9.3f}{r['avg_s2a']:>9.3f}"
              f"{r['avg_voc']:>9.3f}{r['avg_w2v']:>8.3f}{r['avg_camp']:>8.3f}{r['avg_mel']:>8.3f}"
              f"{r['avg_synth']:>9.2f}{r['rtf_synth']:>9.3f}{r['peak']:>8.2f}")
    print("-" * 92)
    # 加速比（以第一个成功模式为基线）
    base = next((r for r in all_res if "error" not in r), None)
    if base:
        print(f"相对基线 [{base['dtype']}] 的加速比 (合成总耗时):")
        for r in all_res:
            if "error" in r or r["dtype"] == base["dtype"]:
                continue
            speedup = base["avg_synth"] / r["avg_synth"] if r["avg_synth"] > 0 else 0
            print(f"  {r['dtype']:<14} 合成 {speedup:.2f}x  | T2S {base['avg_t2s'] / r['avg_t2s']:.2f}x"
                  f"  S2A {base['avg_s2a'] / r['avg_s2a']:.2f}x"
                  f"  声码器 {base['avg_voc'] / r['avg_voc']:.2f}x")
    print("=" * 92)


MODE_MAP = {
    "sdpa": ("sdpa", False),
    "flash": ("flash_attention_2", False),
    "flash+s2a16": ("flash_attention_2", True),
    "sdpa+s2a16": ("sdpa", True),
}


def main():
    ap = argparse.ArgumentParser(description="Confucius4-TTS 推理速率对比测试")
    ap.add_argument("--ref", default=os.path.join(PROJECT_ROOT, "voice", "women.wav"),
                    help="参考音频路径 (默认 voice/women.wav)")
    ap.add_argument("--text", default=(
        "床前明月光，疑是地上霜。举头望明月，低头思故乡。"
        "春风又绿江南岸，明月何时照我还。两个黄鹂鸣翠柳，一行白鹭上青天。"))
    ap.add_argument("--lang", default="zh")
    ap.add_argument("--modes", default="sdpa,flash,flash+s2a16",
                    help="逗号分隔: sdpa / flash / flash+s2a16")
    ap.add_argument("--runs", type=int, default=3, help="每个模式的合成次数")
    ap.add_argument("--warmup", type=int, default=1, help="热身次数 (不计入统计)")
    ap.add_argument("--device", default=None)
    args = ap.parse_args()

    inf_cfg = load_inf_cfg()
    device = args.device or ("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device} | 推理参数: n_timesteps={inf_cfg['n_timesteps']}, "
          f"num_beams={inf_cfg['num_beams']}, max_text_tokens/seg={inf_cfg['max_text_tokens_per_segment']}")

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    all_res = []
    for m in modes:
        if m not in MODE_MAP:
            print(f"[跳过] 未知模式: {m} (可选: sdpa, flash, flash+s2a16)")
            continue
        attn, half_s2a = MODE_MAP[m]
        print(f"\n========== 加载模式={m} (attn={attn}, s2a_half={half_s2a}) ==========")
        res = run_config(m, attn, half_s2a, args.ref, args.text, args.lang, device, inf_cfg, args.runs, args.warmup)
        all_res.append(res)

    print_summary(all_res)


if __name__ == "__main__":
    main()
