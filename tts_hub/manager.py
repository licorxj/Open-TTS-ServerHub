"""TTS API 管家 · 引擎进程生命周期管理

核心能力：
    · ensure(name)  —— 保证目标引擎已就绪；若当前活跃的是别的模型，先卸载再拉起
    · unload(name)  —— 结束引擎进程树，真正释放显存
    · 健康检查轮询（http / tcp / none），支持首次启动的模型下载 + 加载长超时
    · 子进程日志落盘，可通过 /api/hub/engines/{name}/log 实时查看加载进度
    · 空闲自动回收（hub.idle_ttl_seconds > 0 时）
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from .registry import ROOT, get_registry

# 需要被剥离的逐跳首部（转发给引擎时不应携带）
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@dataclass
class EngineProcess:
    """一个被管家托管的引擎实例。"""

    name: str
    cfg: Dict[str, Any]
    port: int
    host: str
    state: str = "starting"  # starting | ready | failed | stopped
    pid: Optional[int] = None
    managed: bool = True  # False = 端口上已存在、由外部启动，管家不会去杀它
    started_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    last_error: Optional[str] = None
    log_path: Optional[str] = None
    inflight: int = 0
    proc: Optional[subprocess.Popen] = None
    _log_fp: Any = None

    # ------------------------------------------------------------------ 基础
    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def display_name(self) -> str:
        return str(self.cfg.get("display_name") or self.name)

    def touch(self) -> None:
        self.last_used = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "state": self.state,
            "pid": self.pid,
            "managed": self.managed,
            "host": self.host,
            "port": self.port,
            "base_url": self.base_url,
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "idle_seconds": round(time.time() - self.last_used, 1),
            "inflight": self.inflight,
            "last_error": self.last_error,
            "log_path": self.log_path,
        }


class EngineError(RuntimeError):
    """引擎拉起/健康检查失败。"""


class EngineManager:
    def __init__(self) -> None:
        self.registry = get_registry()
        self._lock = asyncio.Lock()
        self._active: Dict[str, EngineProcess] = {}

    # -------------------------------------------------------------- 内部工具
    def _log_dir(self) -> Path:
        hub = self.registry.hub()
        d = Path(str(hub.get("log_dir") or "logs/tts_hub"))
        if not d.is_absolute():
            d = ROOT / d
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _build_env(self, cfg: Dict[str, Any]) -> Dict[str, str]:
        rt_global = self.registry.runtime()
        rt = cfg.get("runtime") or {}

        env = os.environ.copy()
        for k in list(rt_global.get("env_unset") or []) + list(rt.get("env_unset") or []):
            env.pop(str(k), None)

        # PATH 前置（等价于各 启动_xxx.bat 的 PATH 处理）
        prepend = list(rt_global.get("path_prepend") or []) + list(rt.get("path_prepend") or [])
        if prepend:
            sep = os.pathsep
            env["PATH"] = sep.join(str(p) for p in prepend) + sep + env.get("PATH", "")

        for src in (rt_global.get("env") or {}, rt.get("env") or {}):
            for k, v in src.items():
                env[str(k)] = "" if v is None else str(v)
        return env

    def _build_cmd(self, cfg: Dict[str, Any]) -> List[str]:
        rt_global = self.registry.runtime()
        rt = cfg.get("runtime") or {}
        python = str(rt.get("python") or rt_global.get("python") or "python")
        script = str(rt.get("script") or "")
        if not script:
            raise EngineError(f"引擎 {cfg.get('name')} 未配置 runtime.script")
        args = [str(a) for a in (rt.get("args") or [])]
        return [python, script] + args

    # ------------------------------------------------------------ 进程控制
    def _spawn(self, inst: EngineProcess) -> None:
        cfg = inst.cfg
        rt_global = self.registry.runtime()
        rt = cfg.get("runtime") or {}
        cwd = str(rt.get("cwd") or rt_global.get("cwd") or ROOT)
        cmd = self._build_cmd(cfg)
        env = self._build_env(cfg)

        log_path = self._log_dir() / f"{inst.name}.log"
        inst.log_path = str(log_path)
        fp = open(log_path, "a", encoding="utf-8", errors="replace")
        inst._log_fp = fp
        fp.write(
            "\n" + "=" * 78 + "\n"
            f"[{datetime.now():%Y-%m-%d %H:%M:%S}] START {inst.display_name} (port={inst.port})\n"
            f"cwd = {cwd}\ncmd = {' '.join(cmd)}\n" + "=" * 78 + "\n"
        )
        fp.flush()

        flags = 0
        if os.name == "nt":
            # 独立进程组（便于 taskkill /T 整树结束）+ 不弹控制台窗口
            # （引擎输出已重定向到 logs/tts_hub/{engine}.log，桌面端模式下不该有黑框）
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            )
        inst.proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdout=fp,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
        )
        inst.pid = inst.proc.pid
        inst.managed = True
        inst.state = "starting"

    def _kill(self, inst: EngineProcess) -> None:
        if inst.proc is None:
            return
        pid = inst.proc.pid
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    timeout=20,
                )
            else:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (ProcessLookupError, AttributeError, PermissionError):
                    inst.proc.kill()
        except Exception:  # noqa: BLE001
            try:
                inst.proc.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            inst.proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            pass
        inst.proc = None

    def _close_log(self, inst: EngineProcess) -> None:
        if inst._log_fp is not None:
            try:
                inst._log_fp.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] STOP {inst.display_name}\n")
                inst._log_fp.flush()
                inst._log_fp.close()
            except Exception:  # noqa: BLE001
                pass
            inst._log_fp = None

    # ------------------------------------------------------------ 健康检查
    async def _probe(self, inst: EngineProcess) -> bool:
        health = inst.cfg.get("health") or {}
        mode = str(health.get("mode") or "http").lower()
        if mode == "none":
            return True
        if mode == "tcp":
            return _is_port_open(inst.host, inst.port, timeout=1.5)

        path = str(health.get("path") or "/health")
        expect = health.get("expect")
        url = f"{inst.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
                resp = await client.get(url)
            if resp.status_code >= 400:
                return False
            if expect:
                return str(expect) in resp.text
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _wait_ready(self, inst: EngineProcess) -> None:
        health = inst.cfg.get("health") or {}
        timeout = float(health.get("timeout") or 900)
        interval = float(health.get("interval") or 3)
        deadline = time.time() + timeout
        last_state = ""
        while time.time() < deadline:
            if inst.proc is not None and inst.proc.poll() is not None:
                rc = inst.proc.returncode
                tail = self.tail_log(inst.name, 40)
                inst.state = "failed"
                inst.last_error = f"引擎进程提前退出（returncode={rc}）"
                raise EngineError(f"{inst.last_error}\n---- {inst.log_path} 末尾 ----\n{tail}")

            if await self._probe(inst):
                inst.state = "ready"
                inst.last_error = None
                return

            if inst.log_path:
                try:
                    size = os.path.getsize(inst.log_path)
                    state = f"{size}B"
                    if state != last_state:
                        last_state = state
                except OSError:
                    pass
            # 拉起/加载期间持续刷新活跃时间，避免长时间加载被空闲回收误杀
            inst.touch()
            await asyncio.sleep(interval)

        inst.state = "failed"
        tail = self.tail_log(inst.name, 40)
        inst.last_error = f"等待引擎就绪超时（{int(timeout)}s）"
        raise EngineError(f"{inst.last_error}\n---- {inst.log_path} 末尾 ----\n{tail}")

    # ---------------------------------------------------------------- 启动
    async def start(self, name: str) -> EngineProcess:
        cfg = self.registry.engine(name)
        if cfg is None:
            raise EngineError(f"未注册的引擎: {name}")
        if not cfg.get("enabled", True):
            raise EngineError(f"引擎 {name} 已在配置中禁用（enabled: false）")

        server = cfg.get("server") or {}
        host = str(server.get("host") or "127.0.0.1")
        port = int(server.get("port") or 0)
        if not port:
            raise EngineError(f"引擎 {name} 未配置 server.port")

        if name in self._active:
            return self._active[name]

        inst = EngineProcess(name=name, cfg=cfg, port=port, host=host)
        self._active[name] = inst

        hub = self.registry.hub()
        adopt = bool(hub.get("adopt_existing", False))
        already = _is_port_open(host, port)
        if already:
            if adopt and await self._probe(inst):
                inst.managed = False
                inst.state = "ready"
                inst.pid = None
                inst.touch()
                return inst
            # 端口被占用且不接管：清掉占位实例，避免留下一个永远 start 不了的"幽灵引擎"
            self._active.pop(name, None)
            raise EngineError(
                f"端口 {host}:{port} 已被其它进程占用，无法拉起引擎 {name}。"
                f"请先关闭占用该端口的程序，或在 config/tts_hub.overrides.yaml 中设置 hub.adopt_existing: true 由管家接管。"
            )

        try:
            self._spawn(inst)
        except EngineError:
            raise
        except Exception as exc:  # noqa: BLE001
            inst.state = "failed"
            inst.last_error = f"启动引擎进程失败: {exc}"
            self._active.pop(name, None)
            raise EngineError(inst.last_error) from exc
        try:
            await self._wait_ready(inst)
        except EngineError:
            raise
        inst.touch()
        return inst

    async def stop(self, name: str, force: bool = False) -> bool:
        inst = self._active.get(name)
        if inst is None:
            return False
        if inst.inflight > 0 and not force:
            raise EngineError(f"引擎 {name} 正在处理 {inst.inflight} 个请求，无法卸载（可加 force=true 强制）")
        self._kill(inst)
        self._close_log(inst)
        inst.state = "stopped"
        inst.pid = None
        self._active.pop(name, None)
        return True

    async def stop_all(self) -> None:
        for name in list(self._active.keys()):
            try:
                await self.stop(name, force=True)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------- 对外主入口
    async def ensure(self, name: str) -> EngineProcess:
        """保证目标引擎就绪；按 max_active 上限腾位后拉起。

        配额语义（改配置即时生效，无需重启）：
          · max_active = 1  → 切换即卸载当前引擎，始终只常驻 1 个（经典行为）
          · max_active = N  → 允许 N 个引擎并存；只有"再加一个会超上限"时，
                              才按最久未使用（LRU）淘汰到刚好放得下
          · unload_on_switch = true（默认 false）→ 每次切换都强制独占，
                              等价于无视 max_active 只留 1 个
        """
        async with self._lock:
            inst = self._active.get(name)
            if inst is not None and inst.state == "ready":
                inst.touch()
                return inst
            if inst is not None:  # 处于 failed/starting，先清掉
                self._kill(inst)
                self._close_log(inst)
                self._active.pop(name, None)

            hub = self.registry.hub()
            max_active = max(1, int(hub.get("max_active") or 1))
            exclusive = bool(hub.get("unload_on_switch", False))

            others = [i for i in self._active.values() if i.name != name]
            if exclusive:
                need_free = len(others)  # 独占：其它都清掉
            else:
                # 目标引擎自己也占一个名额，所以是 len(others) + 1
                need_free = max(0, len(others) + 1 - max_active)

            if need_free > 0:
                # 最久未使用的优先淘汰
                others.sort(key=lambda i: i.last_used)
                for victim in others[:need_free]:
                    if victim.inflight > 0:
                        # 正在处理请求，等它结束（最多 5 分钟）
                        deadline = time.time() + 300
                        while victim.inflight > 0 and time.time() < deadline:
                            await asyncio.sleep(0.5)
                    await self.stop(victim.name, force=True)

            return await self.start(name)

    async def unload_current(self, force: bool = False) -> List[str]:
        async with self._lock:
            stopped = []
            for name in list(self._active.keys()):
                try:
                    if await self.stop(name, force=force):
                        stopped.append(name)
                except EngineError:
                    raise
            return stopped

    # ------------------------------------------------------------ 状态/日志
    def idle_left(self, inst: EngineProcess, ttl: float) -> Optional[float]:
        """距空闲回收还剩多少秒；不适用（未就绪/有在途请求/功能关闭）时返回 None。"""
        if ttl <= 0 or inst.state != "ready" or inst.inflight > 0:
            return None
        return max(0.0, round(ttl - (time.time() - inst.last_used), 1))

    def status(self) -> Dict[str, Any]:
        hub = self.registry.hub()
        ttl = float(hub.get("idle_ttl_seconds") or 0)
        actives = []
        for inst in self._active.values():
            item = inst.to_dict()
            item["idle_ttl_seconds"] = ttl
            item["idle_expires_in"] = self.idle_left(inst, ttl)
            actives.append(item)
        return {
            "active": actives,
            "active_count": len(self._active),
            "max_active": hub.get("max_active", 1),
            "idle_ttl_seconds": ttl,
            "idle_check_interval": float(hub.get("idle_check_interval") or 15),
            "available": self.registry.engine_names(),
        }

    def tail_log(self, name: str, lines: int = 100) -> str:
        path = self._log_dir() / f"{name}.log"
        if not path.is_file():
            return ""
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                content = f.readlines()
            return "".join(content[-lines:])
        except OSError:
            return ""

    # ------------------------------------------------------------ 空闲回收
    async def idle_reaper(self) -> None:
        """空闲自动卸载：无在途请求且空闲超过 hub.idle_ttl_seconds 的引擎会被结束进程、释放显存。

        注意：正在拉起（starting）的引擎不会被回收 —— 模型加载动辄数分钟，
        若此时被回收会造成"刚加载完就被杀掉"。_wait_ready 期间也会持续刷新 last_used。
        """
        while True:
            try:
                interval = max(3.0, float(self.registry.hub().get("idle_check_interval") or 15))
                await asyncio.sleep(interval)

                ttl = float(self.registry.hub().get("idle_ttl_seconds") or 0)
                if ttl <= 0:
                    continue

                async with self._lock:
                    for name, inst in list(self._active.items()):
                        if inst.state != "ready" or inst.inflight > 0:
                            continue
                        idle = time.time() - inst.last_used
                        if idle < ttl:
                            continue
                        try:
                            await self.stop(name, force=True)
                            print(
                                f"[idle-reaper] {datetime.now():%H:%M:%S} 空闲 {int(idle)}s ≥ "
                                f"{int(ttl)}s，已自动卸载引擎 {name} 并释放显存"
                            )
                        except Exception as exc:  # noqa: BLE001
                            print(f"[idle-reaper] 自动卸载 {name} 失败: {exc}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                print(f"[idle-reaper] 巡检异常: {exc}")
                await asyncio.sleep(10)
