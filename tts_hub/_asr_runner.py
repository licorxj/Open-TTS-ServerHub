"""ASR 子进程 runner：被管家调用，独立进程加载 ASR 后端做转写，stdout 输出 JSON。

为什么独立进程：
    管家主进程（uvicorn）里在线程中 import torch / 初始化 CUDA / 加载模型，
    本机实测会**死锁/卡死**（进程 CPU 长期 0、无线程活动）。把整套模型加载
    完全隔离到子进程，既能绕开主进程的线程/import 死锁，也避免常驻 torch 占显存。

两套后端由 ``tools/asr_factory.py`` 选择（默认 ``sensevoice`` = funASR
SenseVoiceSmall，CPU 实测比 Whisper 快一个数量级），按文件加载，互不牵连
重型依赖：走 SenseVoice 时不会 import transformers / torchcodec。

协议（新）：python _asr_runner.py <backend> <model> <device> <audio_path>
协议（旧）：python _asr_runner.py <model> <device> <audio_path>  -> 视为 whisper，保持兼容
成功 -> stdout 打印 {"text": "...", "backend": "...", "model": "..."}
失败 -> stderr 打印 {"error": "..."}，退出码 1
"""
import importlib.util
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # Y:\LcTTSHub


def _load_factory():
    """按文件加载 tools/asr_factory.py（纯标准库，不碰 torch/transformers）。"""
    mod_name = "hub_tools_asr_factory"
    mod = sys.modules.get(mod_name)
    if mod is None:
        path = os.path.join(ROOT, "tools", "asr_factory.py")
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法定位 {path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    return mod


def main() -> None:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    # torchcodec 的禁用由 tools/asr.py 在 import 时统一完成（仅 Whisper 后端用），
    # 这里**不要**再写 sys.modules["torchcodec"] = None —— 那会让引擎进程里
    # 真实的 import torchcodec 抛 "import of torchcodec halted"。

    args = sys.argv[1:]
    if len(args) >= 4:
        backend, model, device, audio = args[0], args[1], args[2], args[3]
    elif len(args) == 3:
        # 旧协议：没有 backend 参数，等价于 Whisper
        backend, model, device, audio = "whisper", args[0], args[1], args[2]
    else:
        raise RuntimeError("用法: python _asr_runner.py <backend> <model> <device> <audio_path>")

    factory = _load_factory()
    backend = factory.normalize_backend(backend)
    model = factory.coerce_model(backend, model)
    device = (device or "").strip() or "cpu"

    ASRCls = factory.load_asr_class(backend)
    asr = ASRCls(model_name=model, device=device)
    text = asr.transcribe(audio)
    # ensure_ascii=True：stdout 全是 ASCII（中文转义成 \uXXXX），
    # 避免 Windows 下父进程按 GBK 解码中文时乱码 / UnicodeDecodeError。
    # json.loads 会把 \uXXXX 还原成真正的中文。
    print(json.dumps({"text": text, "backend": backend, "model": model}, ensure_ascii=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(json.dumps({"error": str(exc)}, ensure_ascii=True) + "\n")
        sys.exit(1)
