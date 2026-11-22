#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LcTTS 智能体调用 CLI —— 给 AI 智能体用的统一 TTS 调用入口。

设计目标（对应 skill 的三条硬要求）：
  1. 让智能体**先问用户**用哪个接口，或沿用上一次（`interfaces` / `last` / `--interface last`）；
  2. **自动拉起**所需进程（管家 → 引擎），把 pid 记进状态文件（`tmp/tts_agent_state.json`）；
  3. 任务结束后**自动关闭**引擎与管家，释放显存（`--keep` 可保留）。

本地引擎一律走管家（http://127.0.0.1:5190），这样环境变量、日志、健康检查、显存回收
都复用项目现有实现，不需要本脚本重复一套拉起逻辑；在线 mimo 接口不占显存、无需进程。

常用命令（在项目根目录执行）：
    py312env\\python.exe tts-agent-skill\\scripts\\tts_agent.py interfaces
    py312env\\python.exe tts-agent-skill\\scripts\\tts_agent.py last
    py312env\\python.exe tts-agent-skill\\scripts\\tts_agent.py run --interface last \
        --mode clone --ref-audio voice/sample.wav --text "你好，世界"
    py312env\\python.exe tts-agent-skill\\scripts\\tts_agent.py stop
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = ROOT / "tmp" / "tts_agent_state.json"
LOG_DIR = ROOT / "logs"
OUT_DIR = ROOT / "outputs" / "agent"
PY312 = ROOT / "py312env" / "python.exe"
HUB_SCRIPT = ROOT / "tts_hub_server.py"
DEFAULT_HUB_PORT = 5190

MIMO_ID = "mimo"
MIMO_LABEL = "MiMo 在线（小米 mimo-v2.5-tts）"
MIMO_DEFAULT_BASE = "https://api.xiaomimimo.com/v1"
MIMO_MODELS = {
    "preset": "mimo-v2.5-tts",
    "design": "mimo-v2.5-tts-voicedesign",
    "clone": "mimo-v2.5-tts-voiceclone",
}
MIMO_VOICES = ["mimo_default", "冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean"]

# 模式 → 引擎注册表里的 endpoints 键；None = 用 default_endpoint
MODE_ENDPOINT = {
    "clone": "clone",
    "design": "design",
    "plain": None,
    "ultimate": "ultimate_clone",
}

DONE_STATUS = {"completed", "complete", "done", "success", "succeeded", "finished"}
FAIL_STATUS = {"failed", "failure", "error", "cancelled", "canceled", "timeout"}


class TtsError(RuntimeError):
    """可读的业务错误（直接打印给智能体/用户）。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def say(msg: str = "") -> None:
    print(msg, flush=True)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def load_state() -> Dict[str, Any]:
    try:
        # utf-8-sig：PowerShell Set-Content 等工具写出的 BOM 也能读
        return json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return {}


def save_state(state: Dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def request(
    method: str,
    url: str,
    payload: Optional[Any] = None,
    timeout: float = 60,
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[str, bytes]:
    """发 HTTP 请求，返回 (content-type, 响应字节)。4xx/5xx 抛 TtsError（带响应体摘要）。"""
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Accept", "application/json, audio/*")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=max(1.0, float(timeout))) as r:
            return r.headers.get("content-type") or "", r.read()
    except urllib.error.HTTPError as exc:  # 把管家/引擎的结构化错误原样抛给智能体
        body = b""
        try:
            body = exc.read()
        except Exception:  # noqa: BLE001
            pass
        text = body.decode("utf-8", "replace")[:1500]
        raise TtsError(f"HTTP {exc.code} {method} {url}\n{text}") from None
    except urllib.error.URLError as exc:
        raise TtsError(f"连接失败 {method} {url}: {exc.reason}") from None


def request_json(
    method: str,
    url: str,
    payload: Optional[Any] = None,
    timeout: float = 60,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    ctype, body = request(method, url, payload, timeout, headers)
    text = body.decode("utf-8", "replace").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001
        raise TtsError(f"响应不是 JSON（{ctype}）：{text[:500]}") from None
    return data if isinstance(data, dict) else {"data": data}


def pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout
            return str(int(pid)) in out
        os.kill(int(pid), 0)  # type: ignore[attr-defined]
        return True
    except Exception:  # noqa: BLE001
        return False


def kill_tree(pid: Optional[int]) -> None:
    if not pid:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(int(pid))],
                capture_output=True,
                timeout=20,
            )
        else:
            os.kill(int(pid), 9)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 引擎目录（优先复用项目注册表，避免重复解析 yaml）
# ---------------------------------------------------------------------------
def load_engines() -> List[Dict[str, Any]]:
    """返回本地引擎清单。优先 import 项目注册表；失败再退回管家 API；都失败则报错。"""
    engines: List[Dict[str, Any]] = []
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from tts_hub.registry import get_registry  # noqa: WPS433

        reg = get_registry()
        for name in reg.engine_names():
            cfg = reg.engine(name) or {}
            if cfg.get("enabled") is False:
                continue
            engines.append(
                {
                    "name": name,
                    "display_name": cfg.get("display_name") or name,
                    "port": (cfg.get("server") or {}).get("port"),
                    "description": cfg.get("description") or "",
                    "endpoints": list((cfg.get("endpoints") or {}).keys()),
                    "default_endpoint": cfg.get("default_endpoint") or "clone",
                    "params": list((cfg.get("params") or {}).keys()),
                    "aliases": cfg.get("aliases") or [],
                }
            )
        if engines:
            return engines
    except Exception:  # noqa: BLE001
        pass

    state = load_state()
    port = int((state.get("hub") or {}).get("port") or hub_port())
    try:
        data = request_json("GET", f"http://127.0.0.1:{port}/api/hub/engines?with_params=1", timeout=10)
        for item in data.get("engines") or []:
            if not item.get("enabled", True):
                continue
            engines.append(
                {
                    "name": item.get("name"),
                    "display_name": item.get("display_name"),
                    "port": item.get("port"),
                    "description": item.get("description") or "",
                    "endpoints": list((item.get("endpoints") or {}).keys()),
                    "default_endpoint": item.get("default_endpoint") or "clone",
                    "params": list((item.get("params") or {}).keys()),
                    "aliases": item.get("aliases") or [],
                }
            )
    except Exception as exc:  # noqa: BLE001
        raise TtsError(f"读取引擎注册表失败（config/tts_hub.yaml 无法解析，管家也未运行）：{exc}") from None
    return engines


def resolve_engine(name: str, engines: List[Dict[str, Any]]) -> Dict[str, Any]:
    key = str(name or "").strip()
    low = key.lower()
    for e in engines:
        if low == str(e["name"]).lower():
            return e
        if low == str(e.get("display_name") or "").lower():
            return e
        if low in [str(a).lower() for a in (e.get("aliases") or [])]:
            return e
    # 常见中文/口语别名
    fuzzy = {
        "index": "indextts2",
        "index2": "indextts2",
        "index25": "indextts25",
        "vox": "voxcpm",
        "omni": "omnivoice",
        "说书": "omnivoice_story",
        "confucius": "confucius4",
        "breeze": "breeze_tts",
    }
    target = fuzzy.get(low)
    if target:
        for e in engines:
            if e["name"] == target:
                return e
    raise TtsError(
        "未知接口 '%s'。可用：%s" % (key, ", ".join(e["name"] for e in engines))
    )


# ---------------------------------------------------------------------------
# 管家进程管理
# ---------------------------------------------------------------------------
def hub_port() -> int:
    port = DEFAULT_HUB_PORT
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from tts_hub.registry import get_registry  # noqa: WPS433

        port = int(get_registry().hub().get("port") or DEFAULT_HUB_PORT)
    except Exception:  # noqa: BLE001
        pass
    return port


def hub_alive(port: int, timeout: float = 2.0) -> Optional[Dict[str, Any]]:
    try:
        data = request_json("GET", f"http://127.0.0.1:{port}/health", timeout=timeout)
        if str(data.get("status") or "").lower() == "ok":
            return data
    except Exception:  # noqa: BLE001
        return None
    return None


def ensure_hub(state: Dict[str, Any], start: bool = True, wait: float = 90) -> Tuple[str, Dict[str, Any]]:
    """确保管家在跑。返回 (base_url, state["hub"])。由本脚本拉起的才登记 pid。"""
    port = int((state.get("hub") or {}).get("port") or hub_port())
    base = f"http://127.0.0.1:{port}"
    if hub_alive(port):
        # 管家本来就在跑：只登记端口，不接管、不关闭（started_by_skill=False）
        hub = state.get("hub") or {}
        hub["port"] = port
        hub.setdefault("started_by_skill", False)
        if not hub.get("pid"):
            try:
                st = request_json("GET", f"{base}/api/hub/status", timeout=10)
                hub["pid"] = (st.get("hub") or {}).get("pid")
                hub["external"] = True
            except Exception:  # noqa: BLE001
                pass
        state["hub"] = hub
        save_state(state)
        return base, hub

    if not start:
        raise TtsError(f"管家未运行（{base}）。去掉 --no-start 可自动拉起。")
    if not HUB_SCRIPT.is_file():
        raise TtsError(f"找不到管家入口：{HUB_SCRIPT}")
    python = PY312 if PY312.is_file() else Path(sys.executable)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "hub_agent.log"
    logf = open(log_path, "a", encoding="utf-8", errors="replace")
    flags = 0
    if os.name == "nt":
        # 独立进程组便于整树结束；无控制台窗口，避免弹黑框
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    logf.write(f"\n{'=' * 70}\n[{now_iso()}] START hub (port={port}) by tts_agent\n{'=' * 70}\n")
    logf.flush()
    proc = subprocess.Popen(
        [str(python), str(HUB_SCRIPT), "--port", str(port)],
        cwd=str(ROOT),
        stdout=logf,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
    )
    hub = {
        "port": port,
        "pid": proc.pid,
        "started_by_skill": True,
        "started_at": now_iso(),
        "log": str(log_path),
    }
    state["hub"] = hub
    save_state(state)

    deadline = time.time() + wait
    while time.time() < deadline:
        if hub_alive(port):
            return base, hub
        if proc.poll() is not None:
            tail = ""
            try:
                tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-15:])
            except Exception:  # noqa: BLE001
                pass
            raise TtsError(f"管家启动失败（退出码 {proc.returncode}），日志 {log_path}：\n{tail}")
        time.sleep(0.5)
    raise TtsError(f"管家 {wait:.0f}s 未就绪，见日志 {log_path}")


def engine_info(base: str, name: str) -> Dict[str, Any]:
    return request_json("GET", f"{base}/api/hub/engines/{urllib.parse.quote(name)}", timeout=15)


def engine_capabilities(base: str, name: str) -> Dict[str, Any]:
    """能力声明（不拉起引擎）：endpoints / canonical / required / params / body_mode。"""
    return request_json(
        "GET", f"{base}/api/hub/engines/{urllib.parse.quote(name)}/params", timeout=20
    )


def stop_engine(base: str, name: str) -> bool:
    try:
        request_json(
            "POST",
            f"{base}/api/hub/engines/{urllib.parse.quote(name)}/stop?force=true",
            timeout=60,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def shutdown_hub(base: str, pid: Optional[int], wait: float = 25) -> bool:
    """关闭管家（内部会先卸载全部引擎）。进程不退则 taskkill 兜底。"""
    try:
        request("POST", f"{base}/api/hub/shutdown", timeout=90)
    except Exception:  # noqa: BLE001
        pass
    deadline = time.time() + wait
    while time.time() < deadline:
        if not pid_alive(pid) and not hub_alive(int(base.rsplit(":", 1)[-1]), timeout=1.0):
            return True
        time.sleep(0.5)
    kill_tree(pid)
    time.sleep(1.0)
    return not pid_alive(pid)


def cleanup(state: Dict[str, Any], keep: bool = False, quiet: bool = False) -> None:
    """任务结束后的收尾：停引擎 → 若管家由本脚本拉起则一并关掉（释放显存）。"""
    if keep:
        if not quiet:
            say("[keep] 按 --keep 保留进程，稍后可手动执行 stop 释放显存。")
        return
    hub = state.get("hub") or {}
    engine = state.get("engine") or {}
    port = int(hub.get("port") or hub_port())
    base = f"http://127.0.0.1:{port}"

    if engine.get("name") and hub_alive(port, timeout=1.5):
        stopped = stop_engine(base, str(engine["name"]))
        if not quiet:
            say(f"[stop] 引擎 {engine['name']} {'已停止' if stopped else '停止失败（见日志）'}")
        if not stopped and pid_alive(engine.get("pid")):
            kill_tree(engine.get("pid"))
    elif engine.get("pid") and pid_alive(engine.get("pid")):
        kill_tree(engine.get("pid"))

    if hub.get("started_by_skill"):
        ok = shutdown_hub(base, hub.get("pid"))
        if not quiet:
            say(f"[stop] 管家 {'已关闭' if ok else '关闭失败，请检查 pid %s' % hub.get('pid')}")
    state["engine"] = {}
    if hub.get("started_by_skill"):
        state["hub"] = {}
    save_state(state)


# ---------------------------------------------------------------------------
# 参数与请求体
# ---------------------------------------------------------------------------
def coerce(value: str) -> Any:
    low = value.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    if value.strip().startswith(("{", "[")):
        try:
            return json.loads(value)
        except Exception:  # noqa: BLE001
            return value
    return value


def build_body(args: argparse.Namespace, caps: Dict[str, Any], out_path: Optional[Path]) -> Dict[str, Any]:
    body: Dict[str, Any] = {"text": args.text}
    if args.ref_audio:
        p = Path(args.ref_audio).expanduser()
        if not p.is_absolute():
            p = (ROOT / p).resolve()
        if not p.is_file():
            raise TtsError(f"参考音频不存在：{p}")
        body["ref_audio_path"] = str(p)
    if getattr(args, "voice_id", ""):
        # 管家内部参数：clone 端点没传参考音频时，用它从音色库取参考音频
        body["voice_id"] = str(args.voice_id).strip()
    if args.ref_text:
        body["ref_text"] = args.ref_text
    if args.instruct:
        body["instruct"] = args.instruct
    if args.language:
        body["language"] = args.language
    if args.speed is not None:
        body["speed"] = args.speed
    for item in args.param or []:
        if "=" not in item:
            raise TtsError(f"--param 需要 key=value 形式：{item}")
        k, v = item.split("=", 1)
        body[k.strip()] = coerce(v)

    params = caps.get("params") or {}
    if out_path is not None and "output_path" in params:
        body["output_path"] = str(out_path)
    return body


def pick_endpoint(args: argparse.Namespace, caps: Dict[str, Any]) -> Optional[str]:
    if args.endpoint:
        return args.endpoint
    mode = (args.mode or "plain").lower()
    if mode not in MODE_ENDPOINT:
        raise TtsError(f"未知模式 '{mode}'，可选：{', '.join(MODE_ENDPOINT)}")
    ep = MODE_ENDPOINT[mode]
    if ep is None:
        return None
    eps = caps.get("endpoints") or {}
    if ep in eps:
        return ep
    if ep == "clone" and "clone" not in eps:  # 有些引擎只声明 tts
        return None
    raise TtsError(
        f"引擎 {caps.get('engine')} 不支持 '{ep}' 端点（模式 {mode}）。可用端点：{', '.join(eps)}"
    )


# ---------------------------------------------------------------------------
# 响应解析：音频字节 / 落盘路径 / 任务轮询
# ---------------------------------------------------------------------------
def parse_payload(ctype: str, body: bytes) -> Dict[str, Any]:
    ctype_low = (ctype or "").split(";")[0].strip().lower()
    if ctype_low.startswith("audio/") or body[:4] == b"RIFF" or body[:3] == b"ID3":
        return {"kind": "bytes", "data": body}
    text = body.decode("utf-8", "replace").strip()
    if not text:
        return {"kind": "none"}
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001
        return {"kind": "text", "text": text}
    if isinstance(data, dict):
        return {"kind": "json", "data": data}
    return {"kind": "json", "data": {"data": data}}


def iter_dicts(obj: Any, depth: int = 0):
    if depth > 4:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from iter_dicts(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_dicts(v, depth + 1)


def find_path_in_json(data: Any) -> Optional[Path]:
    keys = ("output_path", "file", "audio_path", "wav_path", "path", "output", "audio_file")
    for d in iter_dicts(data):
        for k in keys:
            v = d.get(k)
            if isinstance(v, str) and len(v) < 400 and v.lower().endswith((".wav", ".mp3", ".flac")):
                p = Path(v)
                if not p.is_absolute():
                    p = (ROOT / p).resolve()
                if p.is_file():
                    return p
    return None


def find_b64_in_json(data: Any) -> Optional[bytes]:
    keys = ("audio", "audio_base64", "b64", "data", "wav_base64")
    for d in iter_dicts(data):
        for k in keys:
            v = d.get(k)
            if isinstance(v, str) and len(v) > 256:
                try:
                    raw = base64.b64decode(v, validate=False)
                except Exception:  # noqa: BLE001
                    continue
                if raw[:4] == b"RIFF" or raw[:3] == b"ID3" or raw[:2] == b"\xff\xfb":
                    return raw
    return None


def find_status(data: Any) -> str:
    for d in iter_dicts(data):
        for k in ("status", "state"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip().lower()
    return ""


def find_task_id(data: Any) -> Optional[str]:
    for d in iter_dicts(data):
        for k in ("task_id", "taskId", "id"):
            v = d.get(k)
            if isinstance(v, str) and v.strip() and len(v) < 200:
                if k == "id" and not any(x in d for x in ("status", "state")):
                    continue
                return v.strip()
    return None


def poll_task(
    base: str,
    engine: str,
    eps: Dict[str, str],
    task_id: str,
    timeout: float,
    interval: float = 2.0,
) -> Dict[str, Any]:
    tpl = str(eps.get("task") or "/api/v1/tasks/{task_id}")
    path = tpl.replace("{task_id}", task_id)
    url = f"{base}/api/hub/passthrough{path}?model={urllib.parse.quote(engine)}"
    deadline = time.time() + timeout
    last_status = ""
    while time.time() < deadline:
        data = request_json("GET", url, timeout=60)
        status = find_status(data)
        if status and status != last_status:
            say(f"[task] {task_id} → {status}")
            last_status = status
        if status in DONE_STATUS:
            return data
        if status in FAIL_STATUS:
            raise TtsError(f"任务 {task_id} 失败：{json.dumps(data, ensure_ascii=False)[:800]}")
        time.sleep(interval)
    raise TtsError(f"任务 {task_id} 轮询超时（{timeout:.0f}s），可加大 --wait 或查看 logs/{engine}.log")


def download_task(base: str, engine: str, eps: Dict[str, str], task_id: str, timeout: float) -> Optional[bytes]:
    tpl = str(eps.get("download") or "/api/v1/voice/download/{task_id}")
    path = tpl.replace("{task_id}", task_id)
    try:
        ctype, body = request(
            "GET",
            f"{base}/api/hub/passthrough{path}?model={urllib.parse.quote(engine)}",
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001
        return None
    if body and (body[:4] == b"RIFF" or body[:3] == b"ID3" or "audio" in (ctype or "")):
        return body
    return None


# ---------------------------------------------------------------------------
# 本地引擎调用
# ---------------------------------------------------------------------------
def run_local(args: argparse.Namespace, state: Dict[str, Any], engine: Dict[str, Any]) -> Dict[str, Any]:
    name = str(engine["name"])
    base, hub = ensure_hub(state, start=not args.no_start)
    caps = engine_capabilities(base, name)
    endpoint = pick_endpoint(args, {**caps, "engine": name})

    out_path = None
    if args.out:
        out_path = Path(args.out).expanduser()
        if not out_path.is_absolute():
            out_path = (ROOT / out_path).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUT_DIR / f"tts_{name}_{time.strftime('%Y%m%d_%H%M%S')}.wav"

    body = build_body(args, caps, out_path)

    # clone 类端点必须给参考音频：--ref-audio（本地文件）或 --voice-id（音色库 ID，由管家注入）
    ep_key = str(endpoint or caps.get("default_endpoint") or "").lower()
    if "clone" in ep_key and not args.ref_audio and not getattr(args, "voice_id", ""):
        raise TtsError(
            "clone 模式需要参考音频，二选一：\n"
            "  --ref-audio <本地音频路径>\n"
            "  --voice-id  <音色ID>（管家从项目音色库取参考音频，GET /api/voices 可查）"
        )

    query = {"model": name}
    if endpoint:
        query["endpoint"] = endpoint
    url = f"{base}/api/tts?{urllib.parse.urlencode(query)}"
    say(f"[run] 接口={name} 端点={endpoint or '默认'} 输出={out_path}")
    say(f"[run] 请求体={json.dumps(body, ensure_ascii=False)}")

    ctype, raw = request("POST", url, body, timeout=args.timeout)
    parsed = parse_payload(ctype, raw)

    # 记录引擎 pid（管家拉起后才有）
    try:
        info = engine_info(base, name)
        pid = info.get("pid")
    except Exception:  # noqa: BLE001
        pid = None
    state["engine"] = {"name": name, "pid": pid, "port": engine.get("port"), "endpoint": endpoint}
    save_state(state)
    if pid:
        say(f"[run] 引擎已拉起：{name} pid={pid} port={engine.get('port')}（日志 logs/{name}.log）")

    result: Dict[str, Any] = {"interface": name, "endpoint": endpoint or "default", "engine_pid": pid}

    audio_bytes: Optional[bytes] = None
    audio_path: Optional[Path] = None
    task_id = None

    if parsed["kind"] == "bytes":
        audio_bytes = parsed["data"]
    else:
        data = parsed.get("data") or {}
        task_id = find_task_id(data)
        audio_path = find_path_in_json(data)
        if audio_path is None and task_id is None:
            audio_bytes = find_b64_in_json(data)
        if audio_path is None and audio_bytes is None and task_id:
            final = poll_task(base, name, caps.get("endpoints") or {}, task_id, args.wait)
            audio_path = find_path_in_json(final)
            if audio_path is None:
                audio_bytes = find_b64_in_json(final)
            if audio_path is None and audio_bytes is None:
                audio_bytes = download_task(base, name, caps.get("endpoints") or {}, task_id, args.timeout)
            result["task_id"] = task_id
        if audio_path is None and audio_bytes is None and not task_id:
            raise TtsError(
                "响应里既没有 task_id 也没有音频/文件路径，原始响应：\n"
                + json.dumps(data, ensure_ascii=False)[:1200]
            )

    # 引擎已按 output_path 落盘时优先用它
    if out_path.is_file() and out_path.stat().st_size > 1000:
        audio_path = out_path
    elif audio_path is None and audio_bytes is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio_bytes)
        audio_path = out_path

    if audio_path is None:
        raise TtsError("合成返回成功但未能定位音频文件，请检查引擎日志")

    if audio_path != out_path:
        try:
            shutil.copyfile(audio_path, out_path)
            result["engine_path"] = str(audio_path)
            audio_path = out_path
        except Exception:  # noqa: BLE001
            pass

    size = audio_path.stat().st_size if audio_path.is_file() else 0
    if size < 1000:
        raise TtsError(f"输出音频异常（{size} 字节）：{audio_path}；若是静音/空音频请检查引擎日志")
    result.update({"output_path": str(audio_path), "bytes": size, "ok": True})
    return result


# ---------------------------------------------------------------------------
# 在线 MiMo 接口
# ---------------------------------------------------------------------------
def mimo_config() -> Tuple[str, str]:
    key = os.environ.get("MIMO_API_KEY", "").strip()
    base = os.environ.get("MIMO_API_BASE", "").strip() or MIMO_DEFAULT_BASE
    cfg_path = ROOT / "mimotts_online" / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        key = key or str(cfg.get("api_key") or "").strip()
        base = str(cfg.get("api_base") or base)
    except Exception:  # noqa: BLE001
        pass
    if not key:
        raise TtsError(
            "缺少 MiMo API Key：请在 mimotts_online/config.json 填 api_key，或设置环境变量 MIMO_API_KEY"
        )
    return key, base.rstrip("/")


def run_mimo(args: argparse.Namespace) -> Dict[str, Any]:
    if getattr(args, "voice_id", ""):
        raise TtsError("MiMo 在线接口不支持 --voice-id（音色库是本机管家的功能）；请用 --ref-audio 传参考音频")
    key, base = mimo_config()
    mode = (args.mode or "preset").lower()
    if mode == "plain":
        mode = "preset"
    if mode not in MIMO_MODELS:
        raise TtsError("MiMo 只支持 --mode preset / design / clone")
    model = MIMO_MODELS[mode]

    messages: List[Dict[str, str]] = []
    audio_cfg: Dict[str, Any] = {"format": "wav"}
    if mode == "preset":
        voice = args.voice or "冰糖"
        if voice not in MIMO_VOICES:
            say(f"[warn] 未知音色 {voice}，改用 冰糖。可选：{', '.join(MIMO_VOICES)}")
            voice = "冰糖"
        if args.instruct:
            messages.append({"role": "user", "content": args.instruct})
        audio_cfg["voice"] = voice
    elif mode == "design":
        if not args.instruct:
            raise TtsError("MiMo 音色设计必须给 --instruct（音色描述），例如 \"温柔的年轻女性，语速稍慢\"")
        messages.append({"role": "user", "content": args.instruct})
    else:  # clone
        if not args.ref_audio:
            raise TtsError("MiMo 音色克隆必须给 --ref-audio（参考音频路径）")
        p = Path(args.ref_audio).expanduser()
        if not p.is_absolute():
            p = (ROOT / p).resolve()
        if not p.is_file():
            raise TtsError(f"参考音频不存在：{p}")
        mime = "audio/wav" if p.suffix.lower() == ".wav" else "audio/mpeg"
        b64 = base64.b64encode(p.read_bytes()).decode("utf-8")
        audio_cfg["voice"] = f"data:{mime};base64,{b64}"
        messages.append({"role": "user", "content": args.instruct or ""})
    messages.append({"role": "assistant", "content": args.text})

    payload = {"model": model, "messages": messages, "audio": audio_cfg}
    say(f"[run] 接口={MIMO_ID} 模型={model} 模式={mode}")
    data = request_json(
        "POST",
        f"{base}/chat/completions",
        payload,
        timeout=args.timeout,
        headers={"Authorization": f"Bearer {key}"},
    )
    audio_b64 = None
    try:
        audio_obj = (data.get("choices") or [{}])[0].get("message", {}).get("audio") or {}
        if isinstance(audio_obj, dict):
            audio_b64 = audio_obj.get("data")
        elif isinstance(audio_obj, str):
            audio_b64 = audio_obj
    except Exception:  # noqa: BLE001
        audio_b64 = None
    audio_b64 = audio_b64 or find_b64_in_json(data)
    if not audio_b64:
        raise TtsError(f"MiMo 未返回音频：{json.dumps(data, ensure_ascii=False)[:800]}")

    raw = base64.b64decode(audio_b64)
    out_path = Path(args.out).expanduser() if args.out else (
        OUT_DIR / f"tts_mimo_{time.strftime('%Y%m%d_%H%M%S')}.wav"
    )
    if not out_path.is_absolute():
        out_path = (ROOT / out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(raw)
    if out_path.stat().st_size < 1000:
        raise TtsError(f"MiMo 返回音频异常（{out_path.stat().st_size} 字节）")
    return {
        "interface": MIMO_ID,
        "model": model,
        "mode": mode,
        "ok": True,
        "output_path": str(out_path),
        "bytes": out_path.stat().st_size,
        "engine_pid": None,
    }


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------
def cmd_interfaces(args: argparse.Namespace) -> int:
    engines = load_engines()
    state = load_state()
    last = state.get("last") or {}
    if args.json:
        say(
            json.dumps(
                {
                    "last": last or None,
                    "local": engines,
                    "online": [
                        {
                            "name": MIMO_ID,
                            "display_name": MIMO_LABEL,
                            "models": MIMO_MODELS,
                            "voices": MIMO_VOICES,
                            "note": "云端接口，不占显存、无需拉起进程，需要 API Key",
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    say("可用 TTS 接口：")
    say("")
    say("  [本机引擎]  走 5190 管家，自动拉起/关闭，占用显存")
    for e in engines:
        mark = " ← 上次使用" if last.get("interface") == e["name"] else ""
        eps = "/".join(e.get("endpoints") or []) or "-"
        say(f"   - {e['name']:<16} {e.get('display_name') or '':<18} 端口 {e.get('port')}  端点 {eps}{mark}")
        if e.get("description"):
            say(f"       {e['description']}")
    say("")
    say("  [在线接口]  不占显存、无需本地进程")
    say(f"   - {MIMO_ID:<16} {MIMO_LABEL}")
    say(f"       模式 preset（--voice {MIMO_VOICES[1]} / {MIMO_VOICES[2]} …）/ design（--instruct 音色描述）/ clone（--ref-audio）")
    say("")
    if last:
        say(f"上次使用：{last.get('interface')}（mode={last.get('mode')}）")
    else:
        say("上次使用：无")
    return 0


def cmd_last(args: argparse.Namespace) -> int:
    state = load_state()
    last = state.get("last") or {}
    if args.json:
        say(json.dumps({"last": last or None, "runtime": {"hub": state.get("hub"), "engine": state.get("engine")}}, ensure_ascii=False, indent=2))
        return 0
    if not last:
        say("没有上次使用记录。先跑一次 run，或直接指定 --interface。")
        return 0
    say(f"上次接口：{last.get('interface')}  模式：{last.get('mode')}  时间：{last.get('updated')}")
    if last.get("ref_audio"):
        say(f"上次参考音频：{last.get('ref_audio')}")
    if last.get("voice_id"):
        say(f"上次音色 ID：{last.get('voice_id')}")
    if last.get("instruct"):
        say(f"上次指令：{last.get('instruct')}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state = load_state()
    port = int((state.get("hub") or {}).get("port") or hub_port())
    info: Dict[str, Any] = {"hub_port": port, "hub_alive": False, "engines": [], "recorded": state.get("engine") or {}}
    if hub_alive(port):
        info["hub_alive"] = True
        try:
            st = request_json("GET", f"http://127.0.0.1:{port}/api/hub/status", timeout=10)
            info["hub_pid"] = (st.get("hub") or {}).get("pid")
            info["engines"] = st.get("engines") or st.get("active") or []
        except Exception as exc:  # noqa: BLE001
            info["error"] = str(exc)
    if args.json:
        say(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    say(f"管家 127.0.0.1:{port}：{'运行中' if info['hub_alive'] else '未运行'}（pid={info.get('hub_pid') or '-'}）")
    engs = info.get("engines") or []
    if engs:
        for e in engs:
            say(f"  引擎 {e.get('name')} state={e.get('state')} pid={e.get('pid')} port={e.get('port')}")
    else:
        say("  没有活跃引擎")
    rec = info["recorded"]
    if rec:
        say(f"  本 skill 记录：{rec.get('name')} pid={rec.get('pid')}")
    return 0


def cmd_asr(args: argparse.Namespace) -> int:
    """语音转写：音频文件 → 文字。走管家 /api/asr（子进程 ASR，跑完即退、不占显存）。"""
    audio = str(getattr(args, "audio", "") or "").strip()
    if not audio:
        raise TtsError("缺少 --audio（要转写的音频文件）")
    p = Path(audio).expanduser()
    if not p.is_absolute():
        p = (ROOT / p).resolve()
    if not p.is_file():
        raise TtsError(f"音频文件不存在：{p}")

    state = load_state()
    base, _hub = ensure_hub(state, start=not args.no_start)
    payload: Dict[str, Any] = {"path": str(p)}
    if getattr(args, "backend", ""):
        payload["backend"] = args.backend
    if getattr(args, "model", ""):
        payload["model"] = args.model
    if getattr(args, "device", ""):
        payload["device"] = args.device
    if getattr(args, "timeout", 0):
        payload["timeout"] = args.timeout

    client_timeout = max(300.0, float(getattr(args, "timeout", 0) or 300))
    say(f"[asr] 转写：{p}")
    try:
        data = request_json("POST", f"{base}/api/asr", payload, timeout=client_timeout)
    finally:
        # ASR 不拉起常驻引擎；若管家是本脚本拉起的则关掉，释放资源。
        cleanup(state, keep=False, quiet=True)

    if not data.get("ok"):
        raise TtsError(f"ASR 转写失败：{data.get('error') or json.dumps(data, ensure_ascii=False)[:500]}")

    text = str(data.get("text") or "")
    if args.json:
        say(json.dumps({**data, "audio": str(p)}, ensure_ascii=False, indent=2))
    else:
        say(text)
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    state = load_state()
    hub = state.get("hub") or {}
    engine = state.get("engine") or {}
    if not hub and not engine:
        say("没有需要关闭的进程（状态文件为空）。")
        return 0
    cleanup(state, keep=False)
    say("已清理。")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if not args.text or not args.text.strip():
        raise TtsError("缺少 --text（要合成的文本）")

    engines = load_engines()
    state = load_state()
    last = state.get("last") or {}

    iface = (args.interface or "").strip()
    if iface.lower() in ("last", "上次", ""):
        if not last.get("interface"):
            raise TtsError(
                "没有可沿用的上次接口。请先用 interfaces 让用户选一个，再指定 --interface <名字>。"
            )
        iface = str(last["interface"])
        say(f"[run] 沿用上次接口：{iface}")

    if iface.lower() == MIMO_ID:
        result = run_mimo(args)
    else:
        engine = resolve_engine(iface, engines)
        try:
            result = run_local(args, state, engine)
        finally:
            cleanup(state, keep=args.keep)
        state = load_state()
        state["last"] = {
            "interface": engine["name"],
            "mode": args.mode,
            "ref_audio": args.ref_audio or "",
            "voice_id": getattr(args, "voice_id", "") or "",
            "instruct": args.instruct or "",
            "updated": now_iso(),
        }
        save_state(state)
        if args.json:
            say(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            say("")
            say(f"✅ 完成：{result['output_path']}（{result['bytes']} 字节）")
            say("   引擎与管家已关闭，显存已释放。" if not args.keep else "   进程按 --keep 保留。")
        return 0

    state["last"] = {
        "interface": MIMO_ID,
        "mode": args.mode,
        "ref_audio": args.ref_audio or "",
        "instruct": args.instruct or "",
        "updated": now_iso(),
    }
    save_state(state)
    if args.json:
        say(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        say("")
        say(f"✅ 完成：{result['output_path']}（{result['bytes']} 字节）")
        say("   在线接口不占显存，无需关闭进程。")
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tts_agent.py",
        description="LcTTS 智能体调用 CLI：先问接口 → 自动拉起 → 合成 → 自动关闭释放显存",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("interfaces", help="列出可用接口（本机引擎 + 在线 MiMo）")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_interfaces)

    s = sub.add_parser("last", help="查看上次使用的接口")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_last)

    s = sub.add_parser("status", help="管家/引擎运行状态与记录的 pid")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("stop", help="关闭本次拉起的引擎与管家（释放显存）")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("asr", help="语音转写：把音频文件转成文字（SenseVoice/Whisper）")
    s.add_argument("--audio", "-a", required=True, help="要转写的音频文件路径（支持相对项目根）")
    s.add_argument("--backend", help="ASR 后端：sensevoice（默认，快）/ whisper；留空用管家配置")
    s.add_argument("--model", help="ASR 模型名，留空用后端默认（SenseVoiceSmall / whisper-large-v3-turbo）")
    s.add_argument("--device", help="推理设备：cpu（默认）/ cuda；留空用管家配置")
    s.add_argument("--timeout", type=float, default=0, help="转写超时秒数；0=用管家配置 asr_timeout")
    s.add_argument("--no-start", action="store_true", help="不允许自动拉起管家（必须已在运行）")
    s.add_argument("--json", action="store_true", help="机器可读输出（含 text/backend/model/elapsed）")
    s.set_defaults(func=cmd_asr)

    s = sub.add_parser("run", help="合成一句话并落盘 wav")
    s.add_argument("--interface", "-i", default="last", help="引擎名 / mimo / last（默认 last）")
    s.add_argument(
        "--mode",
        "-m",
        default="plain",
        choices=["plain", "clone", "design", "ultimate", "preset"],
        help="plain/clone/design/ultimate 用于本机引擎；preset/design/clone 用于 MiMo 在线",
    )
    s.add_argument("--text", "-t", required=True, help="要合成的文本")
    s.add_argument("--ref-audio", dest="ref_audio", help="参考音频路径（clone 必需；支持相对项目根）")
    s.add_argument(
        "--voice-id",
        dest="voice_id",
        help="音色库唯一 ID（v+8位十六进制）/历史 ID/音频文件名；clone 时可替代 --ref-audio，"
        "由管家从项目音色库注入参考音频（GET /api/voices 可查）",
    )
    s.add_argument("--ref-text", dest="ref_text", help="参考音频对应文本（部分引擎必需）")
    s.add_argument("--instruct", help="音色/情绪指令；MiMo design 模式必填")
    s.add_argument("--voice", help="MiMo preset 音色（冰糖/茉莉/苏打/白桦/Mia…）")
    s.add_argument("--language", help="语言代码，如 zh/en/ja")
    s.add_argument("--speed", type=float, help="语速因子")
    s.add_argument("--out", "-o", help="输出 wav 路径（默认 outputs/agent/）")
    s.add_argument("--endpoint", "-e", help="直接指定引擎端点键（覆盖 --mode），如 clone/design/ultimate_clone/batch")
    s.add_argument("--param", action="append", metavar="K=V", help="额外参数，可重复（自动识别数字/布尔/JSON）")
    s.add_argument("--timeout", type=float, default=1800, help="合成请求超时秒数（首次加载模型可能几分钟）")
    s.add_argument("--wait", type=float, default=1800, help="任务轮询总超时秒数")
    s.add_argument("--keep", action="store_true", help="结束后不关闭引擎/管家")
    s.add_argument("--no-start", action="store_true", help="不允许自动拉起管家（必须已在运行）")
    s.add_argument("--json", action="store_true", help="机器可读输出")
    s.set_defaults(func=cmd_run)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except TtsError as exc:
        say(f"[错误] {exc}")
        return 2
    except KeyboardInterrupt:
        say("[中断] 正在清理进程…")
        try:
            cleanup(load_state(), keep=False, quiet=True)
        except Exception:  # noqa: BLE001
            pass
        return 130


if __name__ == "__main__":
    sys.exit(main())
