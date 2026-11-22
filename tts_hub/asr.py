"""管家级 ASR：音色入库时转写一次，之后克隆直接复用文本。

背景
----
各 TTS 引擎在「克隆模式没给参考文字稿」时会各自跑一遍 ASR，同一段参考音频被
反复转写，既慢又占显存。本模块把转写挂到**管家进程**，但**用独立子进程加载
模型并推理**（见 ``_asr_runner.py``）：

* 管家主进程（uvicorn）里在线程中 import torch / 初始化 CUDA / 加载模型，本机
  实测会**死锁**（进程 CPU 长期 0、无线程活动）；子进程隔离彻底绕开它；
* 主进程里连 ``torch.cuda.*`` 都不调用，避免初始化 CUDA 带来的任何不确定性；
* 新增音色时（``POST /api/hub/voices``）转写一次，结果写进 ``voice/voice.json``
  的 ``text`` 字段（并记 ``text_source="asr"``）；
* 克隆时管家用 ``_inject_voice`` 把这条文本直接塞进请求的参考文字稿字段，
  引擎侧就不需要再跑 ASR 了。

两套后端（由 ``tools/asr_factory.py`` 统一裁决，配置项 ``hub.asr_backend``）：

* ``sensevoice``（**默认**）：funASR ``iic/SenseVoiceSmall``，非自回归，CPU 实测
  加载 ~8s + 6s 音频推理 ~1.4s；权重走 ModelScope 缓存（``MODELSCOPE_CACHE``）；
* ``whisper``：transformers ``openai/whisper-large-v3-turbo``，兼容旧配置，
  CPU 推理约 40s 级；权重走 HuggingFace 缓存（``HF_HOME``）。

子进程内部已强制离线 +（Whisper 分支）禁用 torchcodec；默认用 CPU（稳定、
不抢 GPU 显存），要更快可在 ``hub.asr_device`` 显式填 ``cuda``。
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent

# Whisper 后端的默认模型（SenseVoice 的默认模型见 tools/asr_factory.DEFAULT_MODELS）
DEFAULT_MODEL = "openai/whisper-large-v3-turbo"
DEFAULT_BACKEND = "sensevoice"


def _factory():
    """按文件加载 tools/asr_factory.py（纯标准库，主进程不 import torch）。"""
    import importlib.util

    mod_name = "hub_tools_asr_factory"
    mod = sys.modules.get(mod_name)
    if mod is None:
        path = ROOT / "tools" / "asr_factory.py"
        spec = importlib.util.spec_from_file_location(mod_name, str(path))
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法定位 {path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    return mod


class ASRUnavailable(RuntimeError):
    """ASR 不可用（依赖缺失、模型加载失败、转写超时等）。"""


def _clean_text(text: str) -> str:
    """把 Whisper 输出收拾成能直接当参考文字稿用的单行文本。

    Whisper 中文输出常带多余空格（"你好 世界"），克隆引擎对文字稿比较敏感，
    这里统一压掉空白、去掉首尾标点噪声。
    """
    s = str(text or "").strip()
    if not s:
        return ""
    # 行内换行/多余空格压成单空格
    s = " ".join(part.strip() for part in s.replace("\r", "\n").split("\n") if part.strip())
    # 中文之间的空格去掉（"你 好" -> "你好"），中英之间保留。
    # 用前后查找判断空格两侧是否都是 CJK；旧的逐字符循环在遇到空格时
    # 拿空格本身算 cjk（恒 False），条件永不成立，等于没压缩。
    _CJK = r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]"
    s = re.sub(rf"(?<={_CJK})\s+(?={_CJK})", "", s).strip()
    return s


class HubASR:
    """管家级 ASR：通过子进程调用 ASR 后端，线程安全地串行转写。

    默认后端 ``sensevoice``（funASR，快），可切回 ``whisper``；见 tools/asr_factory.py。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()  # 串行子进程，避免并发拉起多个 ASR 模型
        self._last: Dict[str, Any] = {"text": "", "device": "", "elapsed": 0.0, "error": ""}

    def transcribe(
        self,
        path: os.PathLike | str,
        *,
        backend: str = "",
        model: str = "",
        device: str = "",
        timeout: float = 300.0,
    ) -> Dict[str, Any]:
        """转写音频文件，返回 {ok, text, error, elapsed}（失败不抛异常，降级为空文本）。

        实现：用 ``_asr_runner.py`` 子进程按 backend 加载模型并推理，解析 stdout JSON。
        """
        t0 = time.time()
        device = device or self._auto_device()
        p = Path(str(path))

        # 后端/默认模型交给 tools/asr_factory.py 统一裁决（纯标准库，主进程不碰 torch）。
        # 历史配置里 asr_model 常写 whisper 模型名，coerce_model 会自动纠正。
        try:
            factory = _factory()
            backend = factory.resolve_backend(backend)
            model = factory.coerce_model(backend, model)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning("ASR 后端工厂不可用，回退 Whisper：%s", exc)
            backend, model = "whisper", DEFAULT_MODEL

        def _ret(ok: bool, text: str, error: str) -> Dict[str, Any]:
            info = {
                "ok": ok,
                "text": text,
                "error": error,
                "elapsed": round(time.time() - t0, 2),
            }
            # status() 暴露最近一次结果（含后端/模型/耗时/错误），便于前端与排障
            self._last = {
                "ok": ok,
                "text": text,
                "backend": backend,
                "model": model,
                "device": device,
                "elapsed": info["elapsed"],
                "error": error,
            }
            return info

        if not p.is_file():
            return _ret(False, "", f"音频文件不存在：{p}")

        runner = ROOT / "tts_hub" / "_asr_runner.py"
        py = sys.executable
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"  # 子进程 print / tqdm 日志统一 UTF-8
        env.setdefault("HF_HUB_OFFLINE", "1")
        env.setdefault("TRANSFORMERS_OFFLINE", "1")

        with self._lock:
            try:
                proc = subprocess.run(
                    [py, str(runner), backend, model, device, str(p)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    cwd=str(ROOT),
                    env=env,
                )
            except subprocess.TimeoutExpired:
                return _ret(False, "", f"ASR 转写超时（>{int(timeout)}s）")
            except Exception as exc:  # noqa: BLE001
                return _ret(False, "", f"ASR 子进程启动失败：{exc}")

        if proc.returncode != 0:
            err_lines = [ln for ln in (proc.stderr or "").splitlines() if ln.strip()]
            err = err_lines[-1] if err_lines else (proc.stdout or "").strip()
            return _ret(False, "", f"ASR 转写失败：{err}")

        # 从后往前找最后一个可解析成 JSON 对象的行（progress bar 等日志走 stderr，
        # 但即便偶有日志混进 stdout 也能容忍）
        data = None
        for ln in reversed([x for x in (proc.stdout or "").splitlines() if x.strip()]):
            try:
                obj = json.loads(ln)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(obj, dict):
                data = obj
                break
        if data is None:
            return _ret(False, "", f"ASR 输出无法解析：{(proc.stdout or '')[:200]}")
        if "error" in data:
            return _ret(False, "", str(data["error"]))

        return _ret(True, _clean_text(data.get("text", "")), "")

    @staticmethod
    def _auto_device() -> str:
        """auto 默认用 CPU（入库转写低频、稳定、不抢 GPU 显存，也不在主进程碰 CUDA）。

        想更快可在 ``hub.asr_device`` 显式填 ``cuda``——此时 ``device`` 非空，
        本方法根本不会被调用，GPU 选择权完全交给显式配置。
        """
        return "cpu"

    def status(self) -> Dict[str, Any]:
        """当前状态（给前端/排错用）。子进程模式模型不常驻。"""
        return {"loaded": False, "mode": "subprocess", "last": self._last}


_SINGLETON: Optional[HubASR] = None
_SINGLETON_LOCK = threading.Lock()


def get_asr() -> HubASR:
    """取全局单例。"""
    global _SINGLETON
    if _SINGLETON is None:
        with _SINGLETON_LOCK:
            if _SINGLETON is None:
                _SINGLETON = HubASR()
    return _SINGLETON
