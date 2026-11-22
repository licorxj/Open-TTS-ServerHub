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

from . import procutil
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
    # orphan = 本 hub 进程没有拉起它，是上一代 hub / 外部遗留的进程。
    # 关键：即使 managed=False，只要用户显式 force 关停仍必须能按 pid 杀掉，
    # 否则「状态丢失 → 看得到端口却关不掉 → 显存堆积」会无限循环。
    orphan: bool = False
    started_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    last_error: Optional[str] = None
    log_path: Optional[str] = None
    inflight: int = 0
    proc: Optional[subprocess.Popen] = None
    _log_fp: Any = None
    # 该引擎共享的上游连接池：所有转发复用它，
    # 避免"每个请求 new 一个 AsyncClient"在批量调用时把连接与内存打满。
    # 键 = forward_timeout（httpx 的 send() 不支持逐个请求传 timeout，
    #      只能按超时值分别持有一个 client；实际通常只有 1 个键）
    _clients: Dict[float, Any] = field(default_factory=dict)
    # 最近一次有请求进出的时间，用于判定 inflight 是否泄漏
    last_activity: float = field(default_factory=time.time)

    # ------------------------------------------------------------------ 基础
    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def get_client(self, timeout: float = 0) -> httpx.AsyncClient:
        """取该引擎共享的 httpx 客户端（懒创建，按超时值缓存一个）。

        httpx 的 `send()` 不接受逐个请求的 timeout 参数，超时只能建在 client 上，
        因此按 timeout 值分别持有 client —— 运行期改 hub.forward_timeout 也能立即生效。

        连接池上限刻意放宽：真正的并发闸门在管家侧（hub.max_inflight_*），
        这里只负责复用连接、减少握手与对象开销。
        """
        key = float(timeout or 0)
        client = self._clients.get(key)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(None if not key else key, connect=10.0),
                trust_env=False,
                follow_redirects=False,
                limits=httpx.Limits(
                    max_connections=64,
                    max_keepalive_connections=16,
                    keepalive_expiry=30.0,
                ),
            )
            self._clients[key] = client
        return client

    async def aclose_client(self) -> None:
        if not self._clients:
            return
        for key, client in list(self._clients.items()):
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._clients.pop(key, None)

    @property
    def display_name(self) -> str:
        return str(self.cfg.get("display_name") or self.name)

    def touch(self) -> None:
        self.last_used = time.time()
        self.last_activity = self.last_used

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "state": self.state,
            "pid": self.pid,
            "managed": self.managed,
            "orphan": self.orphan,
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
        # 只保护 _active 字典本身，临界区内不做任何耗时等待
        self._lock = asyncio.Lock()
        # 每个引擎一把"拉起锁"：A 引擎冷启动几分钟不会堵住 B 引擎的请求
        self._boot_locks: Dict[str, asyncio.Lock] = {}
        self._active: Dict[str, EngineProcess] = {}
        # 端口上发现、但本 hub 进程没有 Popen 句柄的引擎（孤儿 / 外部启动）。
        # 独立于 _active 保存，便于面板显示「未被托管但仍占显存」的进程。
        self._orphans: Dict[str, Dict[str, Any]] = {}
        self._reconcile_report: Dict[str, Any] = {}

    def _boot_lock(self, name: str) -> asyncio.Lock:
        lock = self._boot_locks.get(name)
        if lock is None:
            lock = asyncio.Lock()
            self._boot_locks[name] = lock
        return lock

    # -------------------------------------------------------------- 内部工具
    def _log_dir(self) -> Path:
        hub = self.registry.hub()
        d = Path(str(hub.get("log_dir") or "logs"))
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

        # 引擎日志被重定向到文件时，Windows 下 stdout 默认编码是 GBK(cp936)：
        # 引擎里任何中文/特殊字符输出都可能触发编码异常，rich 面板甚至直接崩。
        # 统一强制 UTF-8（引擎若显式配了 PYTHONIOENCODING 则以配置为准）。
        if not (rt.get("env") or {}).get("PYTHONIOENCODING"):
            env["PYTHONIOENCODING"] = "utf-8"
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
        self._rotate_log(log_path)
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
            # （引擎输出已重定向到 logs/{engine}.log，桌面端模式下不该有黑框）
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
        """结束引擎进程树。

        两种句柄都要覆盖：
          · inst.proc  —— 本 hub 亲自 Popen 出来的子进程（最可靠）；
          · inst.pid   —— 接管/对账发现的外部进程，没有 Popen 句柄。
        旧实现遇到 inst.proc is None 直接 return，导致「接管的引擎永远关不掉、
        端口一直被占、显存一直不释放」，正是孤儿进程积压的直接原因。
        """
        proc = inst.proc
        pid = inst.pid or (proc.pid if proc is not None else None)
        if proc is not None:
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True,
                        timeout=20,
                    )
                else:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (ProcessLookupError, AttributeError, PermissionError):
                        proc.kill()
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            try:
                proc.wait(timeout=15)
            except Exception:  # noqa: BLE001
                pass
            inst.proc = None
        elif pid:
            # 外部/孤儿进程：按 PID 反查并结束整棵树（Windows taskkill /T）
            procutil.kill_tree(pid)
        inst.pid = None

    def _rotate_log(self, path: Path, max_bytes: int = 0, backups: int = 2) -> None:
        """引擎子进程日志按体积轮转：{name}.log → .log.1 → .log.2（超出丢弃）。

        引擎是长驻进程、日志只追加不清理，跑几天就能涨到几百 MB；
        不轮转的话 tail_log 一次全量读取会直接把管家内存打爆。
        """
        if max_bytes <= 0:
            hub = self.registry.hub()
            max_bytes = int(hub.get("log_max_bytes") or 0)
            backups = int(hub.get("log_backups") or 0)
        if max_bytes <= 0:
            return
        try:
            if not path.is_file() or path.stat().st_size < max_bytes:
                return
        except OSError:
            return

        for i in range(backups, 0, -1):
            src = path.with_suffix(path.suffix + f".{i}")
            dst = path.with_suffix(path.suffix + f".{i + 1}")
            try:
                if i == backups:
                    src.unlink(missing_ok=True)
                    continue
                if src.is_file():
                    src.replace(dst)
            except OSError:
                pass
        try:
            path.replace(path.with_suffix(path.suffix + ".1"))
        except OSError:
            pass

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

        async with self._lock:
            if name in self._active:
                return self._active[name]
            inst = EngineProcess(name=name, cfg=cfg, port=port, host=host)
            self._active[name] = inst

        hub = self.registry.hub()
        adopt = bool(hub.get("adopt_existing", False))
        # 端口探测是阻塞 socket，丢到线程里，避免堵住事件循环
        already = await asyncio.to_thread(_is_port_open, host, port)
        if already:
            if adopt and await self._probe(inst):
                inst.managed = False
                inst.orphan = True
                inst.state = "ready"
                # 记录真实 PID：否则 stop() / force 关停拿不到句柄，端口永远释放不掉
                inst.pid = await asyncio.to_thread(procutil.pid_for_port, port)
                inst.touch()
                async with self._lock:
                    self._orphans[name] = {
                        "name": name, "host": host, "port": port,
                        "pid": inst.pid, "since": time.time(), "policy": "adopt",
                    }
                return inst
            # 端口被占用且不接管：清掉占位实例，避免留下一个永远 start 不了的"幽灵引擎"
            async with self._lock:
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
            async with self._lock:
                self._active.pop(name, None)
            raise EngineError(inst.last_error) from exc
        try:
            await self._wait_ready(inst)
        except EngineError:
            raise
        inst.touch()
        return inst

    async def stop(self, name: str, force: bool = False) -> bool:
        """结束引擎进程并释放显存。内部自带锁，调用方不要再持 `self._lock`。

        即使 `_active` 里没有登记（hub 重启导致状态丢失），force=True 也会按
        配置端口反查 PID 并结束进程 —— 保证「端口还在就一定关得掉」。
        """
        async with self._lock:
            inst = self._active.pop(name, None)
            if inst is not None and inst.inflight > 0 and not force:
                self._active[name] = inst  # 拒载不能顺手把状态弄丢
                raise EngineError(
                    f"引擎 {name} 正在处理 {inst.inflight} 个请求，无法卸载（可加 force=true 强制）"
                )
        if inst is None:
            if not force:
                return False
            return await self._kill_orphan_by_port(name)
        await inst.aclose_client()
        host, port, had_pid = inst.host, inst.port, inst.pid is not None
        self._kill(inst)
        self._close_log(inst)
        inst.state = "stopped"
        if had_pid:
            # 等端口真正释放，避免紧接着的 start/restart 又把正在退出的旧进程接管回来
            await asyncio.to_thread(procutil.wait_port_closed, host, port, 8.0)
        return True

    async def _kill_orphan_by_port(self, name: str) -> bool:
        """状态已丢失时的兜底关停：按引擎配置端口找到占用者并结束进程树。"""
        cfg = self.registry.engine(name)
        if cfg is None:
            return False
        server = cfg.get("server") or {}
        host = str(server.get("host") or "127.0.0.1")
        try:
            port = int(server.get("port") or 0)
        except (TypeError, ValueError):
            return False
        if not port:
            return False
        pid = await asyncio.to_thread(procutil.pid_for_port, port)
        if not pid:
            return False
        ok = await asyncio.to_thread(procutil.kill_tree, pid)
        await asyncio.to_thread(procutil.wait_port_closed, host, port, 10.0)
        print(f"[reconcile] 状态丢失兜底：已按端口 {host}:{port} 结束引擎 {name} (PID={pid})")
        return ok

    async def stop_all(self, include_orphans: bool = True) -> List[str]:
        """退出前统一收尾：先对账把孤儿纳入管理，再全部结束，避免遗留占显存。"""
        if include_orphans:
            try:
                await self.reconcile()
            except Exception as exc:  # noqa: BLE001
                print(f"[stop_all] 退出前对账失败: {exc}")
        stopped: List[str] = []
        for name in list(self._active.keys()):
            try:
                if await self.stop(name, force=True):
                    stopped.append(name)
            except Exception:  # noqa: BLE001
                pass
        return stopped

    # ------------------------------------------------------- 对外主入口
    async def _alive(self, inst: EngineProcess) -> bool:
        """引擎是否还活着。

        引擎进程可能自己崩掉（显存/原生库崩溃，常见于 CUDA 相关推理）：
        管家此时仍把它记成 ready，转发就会直接撞上
        "All connection attempts failed"，而且**永远拉不起来**。

        · 自家拉起的（有 Popen 句柄）：看子进程退出码，poll() 非阻塞、零开销；
        · 接管/对账来的外部进程（无句柄）：端口是唯一可靠的事实来源。

        注意：绝不能对「无句柄但 managed=True」直接返回 True —— 那是幽灵实例，
        会让管家永远认为引擎活着，既不重启也不释放显存。
        """
        if inst.proc is not None:
            return inst.proc.poll() is None
        return await asyncio.to_thread(_is_port_open, inst.host, inst.port, 1.5)

    async def invalidate(self, name: str, reason: str = "") -> None:
        """把引擎标记为失效并从活跃表里摘掉（下次 ensure 会重新拉起）。"""
        async with self._lock:
            inst = self._active.pop(name, None)
        if inst is None:
            return
        inst.state = "failed"
        inst.last_error = reason or "引擎已失效"
        try:
            await inst.aclose_client()
        except Exception:  # noqa: BLE001
            pass
        self._kill(inst)
        self._close_log(inst)

    async def ensure(self, name: str) -> EngineProcess:
        """保证目标引擎就绪；按 max_active 上限腾位后拉起。

        配额语义（改配置即时生效，无需重启）：
          · max_active = 1  → 切换即卸载当前引擎，始终只常驻 1 个（经典行为）
          · max_active = N  → 允许 N 个引擎并存；只有"再加一个会超上限"时，
                              才按最久未使用（LRU）淘汰到刚好放得下
          · unload_on_switch = true（默认 false）→ 每次切换都强制独占，
                              等价于无视 max_active 只留 1 个

        并发设计：
          · 已就绪的引擎走**无锁快路径** —— 批量请求不会因为任何冷启动而排隊；
          · 只有"确实需要拉起"才进该引擎自己的拉起锁，
            A 引擎几分钟的模型加载不会堵住 B 引擎；
          · 等待被淘汰引擎收尾放在锁外且有超时上限，不会无限期挂住。
        """
        inst = self._active.get(name)
        if inst is not None and inst.state == "ready":
            if await self._alive(inst):
                inst.touch()
                return inst
            # 引擎进程已经自己崩了：标记失效，走下面的重启路径
            print(f"[ensure] 引擎 {name} 进程已退出，自动重新拉起")
            inst.state = "failed"
            inst.last_error = "引擎进程已退出（可能崩溃），将自动重新拉起"

        async with self._boot_lock(name):
            # 双检：等锁期间可能已经被别的协程拉起来了
            inst = self._active.get(name)
            if inst is not None and inst.state == "ready" and await self._alive(inst):
                inst.touch()
                return inst

            if inst is not None:  # 处于 failed/starting，先清掉
                async with self._lock:
                    self._active.pop(name, None)
                await inst.aclose_client()
                self._kill(inst)
                self._close_log(inst)

            hub = self.registry.hub()
            max_active = max(1, int(hub.get("max_active") or 1))
            exclusive = bool(hub.get("unload_on_switch", False))

            async with self._lock:
                others = [i for i in self._active.values() if i.name != name]
                if exclusive:
                    victims = list(others)  # 独占：其它都清掉
                else:
                    # 目标引擎自己也占一个名额，所以是 len(others) + 1
                    need_free = max(0, len(others) + 1 - max_active)
                    others.sort(key=lambda i: i.last_used)  # 最久未使用的优先淘汰
                    victims = others[:need_free]

            for victim in victims:
                await self._wait_idle(victim)
                try:
                    await self.stop(victim.name, force=True)
                except EngineError as exc:
                    print(f"[ensure] 腾位卸载 {victim.name} 失败: {exc}")

            return await self.start(name)

    async def _wait_idle(self, inst: EngineProcess, timeout: float = 60.0) -> None:
        """等待在途请求归零（在锁外调用，不阻塞其它请求）。

        超时后仍由调用方强制卸载 —— 宁可打断，也不能让后续请求无限期排队。
        """
        if inst.inflight <= 0:
            return
        deadline = time.time() + timeout
        while inst.inflight > 0 and time.time() < deadline:
            await asyncio.sleep(0.2)
        if inst.inflight > 0:
            print(
                f"[ensure] 引擎 {inst.name} 仍有 {inst.inflight} 个在途请求，"
                f"等待 {int(timeout)}s 未结束，将强制卸载"
            )

    async def unload_current(self, force: bool = False) -> List[str]:
        stopped = []
        for name in list(self._active.keys()):
            # stop() 内部自带锁，这里不能再持 self._lock（asyncio.Lock 不可重入）
            if await self.stop(name, force=force):
                stopped.append(name)
        return stopped

    # ------------------------------------------------------------ 状态对账
    def orphan_info(self, name: str) -> Optional[Dict[str, Any]]:
        """面板用：该引擎端口上是否存在本 hub 未亲自拉起的进程。"""
        return self._orphans.get(name)

    def _engine_endpoints(self) -> Dict[str, tuple]:
        endpoints: Dict[str, tuple] = {}
        for name in self.registry.engine_names():
            cfg = self.registry.engine(name)
            if cfg is None or not cfg.get("enabled", True):
                continue
            server = cfg.get("server") or {}
            try:
                port = int(server.get("port") or 0)
            except (TypeError, ValueError):
                continue
            if port:
                endpoints[str(name)] = (str(server.get("host") or "127.0.0.1"), port)
        return endpoints

    async def reconcile(self, kill_orphans: bool = False) -> Dict[str, Any]:
        """以「端口上真实的监听进程」为准，重建管家丢失的引擎状态。

        这是修复状态丢失的核心：_active 只是内存字典，hub 一重启就空了，
        而引擎子进程（独立进程组）还活着占显存和端口。系统端口表则永远在，
        因此用它作为外部事实来源做对账：

          · 内存表有登记、进程已死 / 端口消失 → 摘掉（清理幽灵实例）
          · 内存表无登记、端口有进程        → 按 hub.orphan_policy 处理：
                adopt（默认）登记为 orphan，纳入 max_active 配额 + 空闲回收
                kill  直接结束进程树，立即释放显存
                ignore 只在面板提示，不接管

        触发时机：hub 启动、空闲巡检每轮、面板「扫描/清理」、退出前收尾。
        """
        hub = self.registry.hub()
        policy = str(hub.get("orphan_policy") or "adopt").lower()
        if policy not in {"adopt", "kill", "ignore"}:
            policy = "adopt"
        if kill_orphans:
            policy = "kill"
        endpoints = self._engine_endpoints()
        port_map = await asyncio.to_thread(procutil.listening_pids)
        now = time.time()

        found: Dict[str, Dict[str, Any]] = {}
        adopted: List[str] = []
        killed: List[str] = []
        dropped: List[str] = []
        ignored: List[str] = []

        for name, (host, port) in endpoints.items():
            pid = port_map.get(port)
            inst = self._active.get(name)

            if inst is not None and inst.proc is not None:
                if inst.proc.poll() is None:
                    continue  # 本 hub 亲自拉起且仍存活，不算孤儿
                # 自家进程已退出但端口上还有别的进程：旧登记作废
                async with self._lock:
                    self._active.pop(name, None)
                await inst.aclose_client()
                dropped.append(name)
                inst = None

            if pid is None:
                if inst is not None and inst.proc is None:
                    # 接管态的进程没了：清掉，避免永远显示 ready 的幽灵
                    async with self._lock:
                        self._active.pop(name, None)
                    await inst.aclose_client()
                    dropped.append(name)
                continue

            if policy == "kill":
                await asyncio.to_thread(procutil.kill_tree, pid)
                await asyncio.to_thread(procutil.wait_port_closed, host, port, 10.0)
                if inst is not None:
                    async with self._lock:
                        self._active.pop(name, None)
                    await inst.aclose_client()
                killed.append(name)
                continue

            found[name] = {
                "name": name, "host": host, "port": port,
                "pid": pid, "since": now, "policy": policy,
            }
            if inst is None:
                if policy == "adopt":
                    cfg = self.registry.engine(name) or {}
                    self._active[name] = EngineProcess(
                        name=name, cfg=cfg, port=port, host=host,
                        state="ready", pid=pid, managed=False, orphan=True,
                        started_at=now, last_used=now,
                    )
                    adopted.append(name)
                else:
                    ignored.append(name)
            else:
                # 已登记但无 Popen 句柄（接管态）：同步真实 PID，保证能关停
                inst.pid = pid
                inst.orphan = True
                inst.managed = False
                if inst.state != "starting":
                    inst.state = "ready"

        self._orphans = found
        self._reconcile_report = {
            "at": now,
            "policy": policy,
            "scanned": len(endpoints),
            "adopted": adopted,
            "killed": killed,
            "dropped": dropped,
            "ignored": ignored,
            "orphan_count": len(found),
            "orphans": list(found.values()),
        }
        if adopted or killed or dropped:
            print(
                f"[reconcile] 对账完成：接管={adopted} 结束={killed} "
                f"清理幽灵={dropped} 仍为孤儿={list(found)}"
            )
        return self._reconcile_report

    async def kill_orphans(self) -> Dict[str, Any]:
        """面板「一键清理」：结束所有引擎端口上的非托管进程。"""
        return await self.reconcile(kill_orphans=True)

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
            "orphan_policy": str(hub.get("orphan_policy") or "adopt"),
            "orphan_idle_ttl_seconds": float(hub.get("orphan_idle_ttl_seconds") or ttl),
            "orphans": list(self._orphans.values()),
            "orphan_count": len(self._orphans),
            "reconcile": dict(self._reconcile_report),
        }

    def tail_log(self, name: str, lines: int = 100) -> str:
        """只从文件**尾部**反向扫描出需要的行数。

        旧实现 `readlines()` 会把整个日志读进内存 —— 引擎日志只追加不清理，
        跑久了上百 MB，一次 tail 就能把管家内存打爆（且该接口被高频轮询）。
        """
        path = self._log_dir() / f"{name}.log"
        if not path.is_file():
            return ""
        lines = max(1, int(lines or 1))
        try:
            size = path.stat().st_size
        except OSError:
            return ""
        if size <= 0:
            return ""

        # 最多回扫 4MB，避免超大文件（或单行超长）时无限循环
        floor = max(0, size - 4 * 1024 * 1024)
        step = 8192
        chunks: List[bytes] = []
        found = 0
        pos = size
        try:
            with path.open("rb") as f:
                while pos > floor and found <= lines:
                    read_len = min(step, pos - floor)
                    pos -= read_len
                    f.seek(pos)
                    chunk = f.read(read_len)
                    found += chunk.count(b"\n")
                    chunks.append(chunk)
        except OSError:
            return ""

        text = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])

    # ------------------------------------------------------------ 空闲回收
    async def _reap_stale_inflight(self) -> None:
        """兜底清理泄漏的在途计数。

        客户端中途断开、响应流被提前关闭等异常会让 inflight 减不回去。
        一旦卡住，引擎会被永久判定为「忙碌」：空闲回收不再触发、腾位卸载也停住，
        显存再也释放不掉 —— 最终表现为"跑着跑着就崩"。
        超过 hub.inflight_stale_seconds 无活动即视为泄漏，清零并告警。
        """
        limit = float(self.registry.hub().get("inflight_stale_seconds") or 0)
        if limit <= 0:
            return
        now = time.time()
        async with self._lock:
            for inst in list(self._active.values()):
                if inst.inflight <= 0:
                    continue
                stale = now - inst.last_activity
                if stale > limit:
                    print(
                        f"[reaper] 引擎 {inst.name} 的 {inst.inflight} 个在途请求已 "
                        f"{int(stale)}s 无活动，判定为泄漏，已清零（避免显存无法回收）"
                    )
                    inst.inflight = 0
                    inst.last_activity = now

    async def idle_reaper(self) -> None:
        """空闲自动卸载 + 状态对账：无在途请求且空闲超时的引擎会被结束、释放显存。

        注意：正在拉起（starting）的引擎不会被回收 —— 模型加载动辄数分钟，
        若此时被回收会造成"刚加载完就被杀掉"。_wait_ready 期间也会持续刷新 last_used。
        """
        while True:
            try:
                hub_cfg = self.registry.hub()
                interval = max(3.0, float(hub_cfg.get("idle_check_interval") or 15))
                await asyncio.sleep(interval)

                await self._reap_stale_inflight()

                # 每轮以系统端口表为准重建状态：hub 重启后遗留的引擎会被自动找回，
                # 之后一样纳入配额与空闲回收，不会无限期占着显存。
                try:
                    await self.reconcile()
                except Exception as exc:  # noqa: BLE001
                    print(f"[idle-reaper] 状态对账失败: {exc}")

                ttl = float(hub_cfg.get("idle_ttl_seconds") or 0)
                if ttl <= 0:
                    continue
                orphan_ttl = float(hub_cfg.get("orphan_idle_ttl_seconds") or ttl)

                # 临界区内只挑出该回收的名字，绝不 await 任何生命周期方法。
                # 旧实现在 `async with self._lock` 内 await self.stop()，而 stop()
                # 内部还要拿同一把 asyncio.Lock —— 不可重入，必然永久死锁：
                # 一旦空闲回收触发，所有启停/切换请求全部挂起、引擎再也关不掉。
                due: List[tuple] = []
                async with self._lock:
                    for name, inst in list(self._active.items()):
                        if inst.state != "ready" or inst.inflight > 0:
                            continue
                        idle = time.time() - inst.last_used
                        limit = orphan_ttl if inst.orphan else ttl
                        if idle >= limit:
                            due.append((name, idle, limit))

                for name, idle, limit in due:
                    try:
                        await self.stop(name, force=True)
                        print(
                            f"[idle-reaper] {datetime.now():%H:%M:%S} 空闲 {int(idle)}s ≥ "
                            f"{int(limit)}s，已自动卸载引擎 {name} 并释放显存"
                        )
                    except Exception as exc:  # noqa: BLE001
                        print(f"[idle-reaper] 自动卸载 {name} 失败: {exc}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                print(f"[idle-reaper] 巡检异常: {exc}")
                await asyncio.sleep(10)
