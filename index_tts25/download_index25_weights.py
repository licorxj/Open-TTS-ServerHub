# -*- coding: utf-8 -*-
"""
IndexTTS-2.5 权重准备脚本
- 主权重：从 ModelScope IndexTeam/IndexTTS-2.5 下载到 models/index25/（跳过 qwen0.6bemo4-merge，复用已有）
- 子模型：复用项目内已有缓存（models/index2/hf_cache 中的 w2v-bert-2.0 / campplus / bigvgan）
          以及 models/index2/qwen0.6bemo4-merge，通过目录联接(junction)零拷贝复用
"""
import os
import sys
import subprocess
import shutil

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_DIR = os.path.join(PROJECT_ROOT, "models", "index25")
HF_CACHE_SRC = os.path.join(PROJECT_ROOT, "models", "index2", "hf_cache")
QWEN_SRC = os.path.join(PROJECT_ROOT, "models", "index2", "qwen0.6bemo4-merge")

MAIN_FILES = [
    "config.yaml",
    "gpt.pth",
    "codec.pth",
    "s2mel.pth",
    "feat1.pt",
    "feat2.pt",
    "wav2vec2bert_stats.pt",
    "multilingual_zh_ja_yue_char_del.tiktoken",
]


def make_junction(dst: str, src: str) -> bool:
    """创建 Windows 目录联接(junction)，无需管理员权限。失败返回 False。"""
    if os.path.isdir(dst):
        return True
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", dst, src],
            capture_output=True, text=True,
        )
        return r.returncode == 0
    except Exception:
        return False


def main():
    os.makedirs(MODEL_DIR, exist_ok=True)

    # 1) 主权重下载（允许断点续传，跳过 qwen0.6bemo4-merge 子目录）
    missing = [f for f in MAIN_FILES if not os.path.isfile(os.path.join(MODEL_DIR, f))]
    if missing:
        print(">> 需要下载的主权重:", missing)
        try:
            from modelscope.hub.snapshot_download import snapshot_download
            snapshot_download(
                "IndexTeam/IndexTTS-2.5",
                local_dir=MODEL_DIR,
                allow_file_pattern=["*.pth", "*.pt", "*.yaml", "*.tiktoken"],
            )
        except Exception as e:
            print(f"!! ModelScope 下载失败: {e}")
            sys.exit(1)
    else:
        print(">> 主权重已齐全，跳过下载")

    # 2) 子模型复用：hf_cache（w2v-bert-2.0 / campplus / bigvgan）
    hf_dst = os.path.join(MODEL_DIR, "hf_cache")
    if os.path.isdir(HF_CACHE_SRC) and not os.path.isdir(hf_dst):
        if make_junction(hf_dst, HF_CACHE_SRC):
            print(f">> hf_cache 已通过联接复用: {HF_CACHE_SRC}")
        else:
            print(">> 联接失败，改为复制 hf_cache ...")
            shutil.copytree(HF_CACHE_SRC, hf_dst, dirs_exist_ok=True)

    # 3) Qwen 情感模型复用
    qwen_dst = os.path.join(MODEL_DIR, "qwen0.6bemo4-merge")
    if os.path.isdir(QWEN_SRC) and not os.path.isdir(qwen_dst):
        if make_junction(qwen_dst, QWEN_SRC):
            print(f">> qwen0.6bemo4-merge 已通过联接复用: {QWEN_SRC}")
        else:
            print(">> 联接失败，改为复制 qwen0.6bemo4-merge ...")
            shutil.copytree(QWEN_SRC, qwen_dst, dirs_exist_ok=True)

    # 4) 校验
    print("\n===== 校验 =====")
    ok = True
    for f in MAIN_FILES:
        p = os.path.join(MODEL_DIR, f)
        size = os.path.getsize(p) if os.path.isfile(p) else 0
        print(f"  {f}: {size/1024/1024:.1f} MB" if size else f"  {f}: 缺失!")
        if not size:
            ok = False
    print(f"  hf_cache: {'OK' if os.path.isdir(hf_dst) else '缺失!'}")
    print(f"  qwen0.6bemo4-merge: {'OK' if os.path.isdir(qwen_dst) else '缺失!'}")
    print("\n完成。" if ok else "\n有文件缺失，请检查网络后重试。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
