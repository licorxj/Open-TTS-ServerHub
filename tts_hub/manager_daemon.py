#!/usr/bin/env python3
"""TTS 管家 · 统一管理器（守护进程）

作为「后端 hub」与「前端面板」的父进程，统一负责：
  · 拉起后端与前端（前端可选：生产由 hub 在 /ui 直接托管）；
  · 端口守护：后端进程崩溃或端口掉线后自动重启（带退避）；
  · 控制 API（独立端口、CORS 开放）：供面板启停 / 重启后端；
  · 自带控制页面：后端挂掉时也能从管理器端口把后端拉起来（避免「鸡生蛋」）。

与 hub.port 相互独立 —— 管理器始终存活，后端只是它看护的子进程。
启动：python tts_hub/manager_daemon.py
（生产由 启动_TTS管家_管理器.bat 以 pythonw 无窗口方式拉起）
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(os.environ.get("TTS_HUB_ROOT") or Path(__file__).resolve().parents[1]).resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402

from tts_hub.registry import ROOT as _ROOT, get_registry  # noqa: E402

registry = get_registry()

_BOOT = time.time()


def _log_dir() -> Path:
    d = ROOT / "logs" / "manager"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _pid_for_port(host: str, port: int) -> Optional[int]:
    """按端口反查监听进程的 PID（用于接管/强杀外部拉起的服务）。"""
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, timeout=10
            ).stdout
        except Exception:  # noqa: BLE001
            return None
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].startswith("TCP"):
                local = parts[1]
                if local.endswith(f":{port}") and parts[3] == "LISTENING":
                    try:
                        return int(parts[4])
                    except ValueError:
                        return None
    else:
        try:
            out = subprocess.run(
                ["ss", "-ltnp"], capture_output=True, text=True, timeout=10
            ).stdout
        except Exception:  # noqa: BLE001
            return None
        import re

        for line in out.splitlines():
            m = re.search(rf":{port}\s.*pid=(\d+)", line)
            if m:
                return int(m.group(1))
    return None


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Managed:
    """一个被管理器看护的子进程（后端 / 前端）。"""

    def __init__(self, name: str, cmd: list, port: int, host: str = "127.0.0.1", log_name: Optional[str] = None):
        self.name = name
        self.cmd = list(cmd)
        self.port = port
        self.host = host
        self.log_name = log_name or (name + ".log")
        self.want_up = True
        self.proc: Optional[subprocess.Popen] = None
        self.pid: Optional[int] = None
        self.started_at = 0.0
        self.restarts = 0
        self.last_error: Optional[str] = None
        self._fp = None
        # 端口上已有同名服务时直接接管（不重复拉起，也不能强制杀它）
        self.adopted = False

    @property
    def alive(self) -> bool:
        if self.proc is not None:
            return self.proc.poll() is None
        if self.adopted:
            return self.port_open
        return False

    @property
    def port_open(self) -> bool:
        return _is_port_open(self.host, self.port)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "pid": self.pid,
            "alive": self.alive,
            "want_up": self.want_up,
            "port": self.port,
            "host": self.host,
            "port_open": self.port_open,
            "restarts": self.restarts,
            "started_at": self.started_at,
            "uptime": round(time.time() - self.started_at, 1) if self.started_at else 0,
            "last_error": self.last_error,
        }

    # -------------------------------------------------------------- 进程控制
    def _rotate(self, path: Path, max_bytes: int = 20 * 1024 * 1024, backups: int = 2) -> None:
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

    def spawn(self) -> None:
        log_path = _log_dir() / self.log_name
        self._rotate(log_path)
        fp = open(log_path, "a", encoding="utf-8", errors="replace")
        self._fp = fp
        fp.write(
            "\n" + "=" * 78 + "\n"
            f"[{_now()}] START {self.name} (port={self.port})\n"
            f"cwd = {ROOT}\ncmd = {' '.join(self.cmd)}\n" + "=" * 78 + "\n"
        )
        fp.flush()

        flags = 0
        if os.name == "nt":
            # 独立进程组 + 不弹控制台窗口（输出已重定向到日志）
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            )
        env = os.environ.copy()
        self.proc = subprocess.Popen(
            self.cmd,
            cwd=str(ROOT),
            env=env,
            stdout=fp,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
        )
        self.pid = self.proc.pid
        self.started_at = time.time()
        self.last_error = None
        self.want_up = True
        self.adopted = False

    def kill(self) -> None:
        pid = self.proc.pid if self.proc is not None else None
        if pid is None and self.adopted:
            # 接管来的进程没有子进程句柄，按端口反查 PID 再杀
            pid = _pid_for_port(self.host, self.port)
        if pid is None:
            return
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
                    os.kill(pid, signal.SIGKILL)
        except Exception:  # noqa: BLE001
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:  # noqa: BLE001
                pass
        # 等待端口释放
        for _ in range(30):
            if not self.port_open:
                break
            time.sleep(0.5)
        self.proc = None
        self.pid = None
        self.adopted = False

    def close_log(self) -> None:
        if self._fp is not None:
            try:
                self._fp.write(f"[{_now()}] STOP {self.name}\n")
                self._fp.flush()
                self._fp.close()
            except Exception:  # noqa: BLE001
                pass
            self._fp = None

    def tail_log(self, lines: int = 200) -> str:
        path = _log_dir() / self.log_name
        if not path.is_file():
            return ""
        try:
            size = path.stat().st_size
        except OSError:
            return ""
        if size <= 0:
            return ""
        floor = max(0, size - 4 * 1024 * 1024)
        step = 8192
        chunks: list = []
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


class ManagerDaemon:
    """管理器主逻辑：看护后端 / 前端，做端口守护。"""

    def __init__(self) -> None:
        self.mcfg = registry.manager()
        hub_cfg = registry.hub()
        self.hub_port = int(hub_cfg.get("port") or 5190)
        self.hub_host = "127.0.0.1"

        runtime = registry.runtime() or {}
        python = str(runtime.get("python") or str(ROOT / "py312env" / "python.exe"))
        backend_cmd = [python, str(ROOT / "tts_hub_server.py"), "--port", str(self.hub_port)]
        self.backend = Managed("backend", backend_cmd, self.hub_port, self.hub_host, "backend.log")

        fcfg = self.mcfg.get("frontend") or {}
        fcmd = (fcfg.get("command") or "").strip()
        fport = int(fcfg.get("port") or 0)
        self.frontend: Optional[Managed] = None
        if fcmd:
            self.frontend = Managed(
                "frontend", fcmd.split(), fport, self.hub_host, "frontend.log"
            )

        self.guard_interval = max(1.0, float(self.mcfg.get("guard_interval") or 5))
        self.backend_start_timeout = max(10.0, float(self.mcfg.get("backend_start_timeout") or 180))
        self.max_restarts = int(self.mcfg.get("max_restarts") or 0)
        self.open_panel = bool(self.mcfg.get("open_panel", True))
        self._panel_opened = False
        self._shutdown = False

    # ------------------------------------------------------------------ 启停
    def start_all(self) -> None:
        self.backend.want_up = True
        if _is_port_open(self.backend.host, self.backend.port):
            # 端口上已有同名服务（直接启动 / 上一轮管理器遗留）→ 接管，不重复拉起
            self.backend.adopted = True
            self.backend.proc = None
            self.backend.pid = _pid_for_port(self.backend.host, self.backend.port)
            self.backend.started_at = time.time()
            print(
                f"[manager] 检测到 hub 已在 :{self.backend.port} 运行，接管"
                f"（PID={self.backend.pid}），不再重复拉起"
            )
        elif not self.backend.alive and self.backend.proc is None:
            try:
                self.backend.spawn()
            except Exception as exc:  # noqa: BLE001
                self.backend.last_error = f"拉起失败: {exc}"
        if self.frontend:
            self.frontend.want_up = True
            if _is_port_open(self.frontend.host, self.frontend.port):
                self.frontend.adopted = True
                self.frontend.proc = None
                self.frontend.pid = _pid_for_port(self.frontend.host, self.frontend.port)
                self.frontend.started_at = time.time()
            elif not self.frontend.alive and self.frontend.proc is None:
                try:
                    self.frontend.spawn()
                except Exception as exc:  # noqa: BLE001
                    self.frontend.last_error = f"拉起失败: {exc}"

    def start_backend(self) -> None:
        self.backend.want_up = True
        if not self.backend.alive and self.backend.proc is None:
            try:
                self.backend.spawn()
            except Exception as exc:  # noqa: BLE001
                self.backend.last_error = f"拉起失败: {exc}"

    def stop_backend(self, graceful: bool = True, force: bool = False) -> None:
        """停止后端。graceful：先走 hub 的 /api/hub/shutdown（优雅卸载引擎），失败再强杀。"""
        self.backend.want_up = False
        if graceful and not force and self.backend.port_open:
            try:
                with httpx.Client(timeout=5.0, trust_env=False) as c:
                    c.post(f"http://127.0.0.1:{self.hub_port}/api/hub/shutdown")
            except Exception:  # noqa: BLE001
                pass
            # 给后端一点时间优雅退出
            for _ in range(20):
                if not self.backend.alive:
                    break
                time.sleep(0.3)
        self.backend.kill()
        self.backend.close_log()

    def restart_backend(self, force: bool = False) -> None:
        self.backend.want_up = True
        self.backend.kill()
        try:
            self.backend.spawn()
            self.backend.restarts += 1
        except Exception as exc:  # noqa: BLE001
            self.backend.last_error = f"拉起失败: {exc}"

    def shutdown_all(self) -> None:
        self._shutdown = True
        self.backend.want_up = False
        self.backend.kill()
        self.backend.close_log()
        if self.frontend:
            self.frontend.want_up = False
            self.frontend.kill()
            self.frontend.close_log()

    def _open_panel(self) -> None:
        url = f"http://127.0.0.1:{self.hub_port}/ui"
        try:
            if os.name == "nt":
                os.startfile(url)  # type: ignore[attr-defined]
            else:
                import webbrowser

                webbrowser.open(url)
            print(f"[manager] 已打开面板: {url}")
        except Exception as exc:  # noqa: BLE001
            print(f"[manager] 打开面板失败: {exc}")

    # -------------------------------------------------------------- 端口守护
    async def guard_loop(self) -> None:
        while not self._shutdown:
            await asyncio.sleep(self.guard_interval)
            if self._shutdown:
                break
            self._guard_one(self.backend, self.max_restarts, self.backend_start_timeout)
            if self.frontend:
                self._guard_one(self.frontend, 0, 0)

    def _guard_one(self, m: Managed, max_restarts: int, start_timeout: float) -> None:
        if m.want_up:
            if not m.alive:
                # 进程不在 → 重启
                if max_restarts and m.restarts >= max_restarts:
                    m.last_error = f"已达最大重启次数 {max_restarts}，停止看护"
                    return
                print(f"[manager] {_now()} 检测到 {m.name} 未运行，重新拉起…")
                try:
                    if m.proc is not None:
                        m.kill()
                    m.spawn()
                    m.restarts += 1
                except Exception as exc:  # noqa: BLE001
                    m.last_error = f"拉起失败: {exc}"
            else:
                # 进程在、但端口长时间未开 → 判定卡死，强制重启
                if (
                    start_timeout
                    and m.started_at
                    and (time.time() - m.started_at) > start_timeout
                    and not m.port_open
                ):
                    print(
                        f"[manager] {_now()} {m.name} 拉起超过 {int(start_timeout)}s 端口仍关闭，"
                        f"判定卡死，强制重启"
                    )
                    m.kill()
                    try:
                        m.spawn()
                        m.restarts += 1
                    except Exception as exc:  # noqa: BLE001
                        m.last_error = f"拉起失败: {exc}"
                else:
                    # 健康：后端首次就绪时打开面板
                    if m is self.backend and self.open_panel and not self._panel_opened and m.port_open:
                        self._open_panel()
                        self._panel_opened = True
        else:
            if m.alive:
                m.kill()


# ===========================================================================
# FastAPI 控制面（独立端口，CORS 开放）
# ===========================================================================
daemon = ManagerDaemon()

CONTROL_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>TTS 管家 · 统一管理器</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--panel2:#1e222b;--border:#2a2f3a;--fg:#e6e9ef;--muted:#8b93a3;--accent:#4f8cff;--accent2:#7c5cff;--ok:#3ecf8e;--err:#ff6b6b;--warn:#ffb454}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--fg);font-size:14px}
header{padding:14px 20px;background:linear-gradient(90deg,#1b2030,#14171f);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:14px;flex-wrap:wrap}
header h1{font-size:17px;margin:0;font-weight:600}
.pill{font-size:12px;padding:4px 10px;border-radius:999px;border:1px solid var(--border)}
.pill.ok{color:var(--ok);border-color:var(--ok)}.pill.err{color:var(--err);border-color:var(--err)}.pill.warn{color:var(--warn);border-color:var(--warn)}
main{padding:16px;display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start}
@media(max-width:820px){main{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px}
.card h2{margin:0 0 12px;font-size:14px;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
.kv{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px dashed var(--border);font-size:13px}
.kv span:first-child{color:var(--muted)}.kv span:last-child{font-family:Consolas,monospace}
.btns{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}
button{cursor:pointer;border:none;border-radius:8px;padding:8px 14px;font-size:13px;background:var(--panel2);color:var(--fg);border:1px solid var(--border);transition:.15s}
button:hover{border-color:var(--accent)}button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
button.danger{border-color:var(--err);color:var(--err)}
pre{background:#0b0d12;border:1px solid var(--border);border-radius:8px;padding:12px;max-height:320px;overflow:auto;font-size:12px;color:#b8e0c8;white-space:pre-wrap;margin-top:12px}
.msg{padding:10px 12px;border-radius:8px;margin-top:12px;font-size:13px;display:none}
.msg.ok{display:block;background:rgba(62,207,142,.12);border:1px solid var(--ok);color:var(--ok)}
.msg.err{display:block;background:rgba(255,107,107,.12);border:1px solid var(--err);color:var(--err)}
.hint{font-size:11px;color:var(--muted);margin-top:4px}
.row{display:flex;align-items:center;gap:10px;margin-top:10px}
.row label{font-size:12px;color:var(--muted)}
</style></head>
<body>
<header>
  <h1>🛡️ TTS 管家 · 统一管理器</h1>
  <span id="mstatus" class="pill warn">连接中…</span>
  <span class="hint" id="minfo"></span>
</header>
<main>
  <div class="card">
    <h2>后端 (hub) · 端口守护</h2>
    <div class="kv"><span>状态</span><span id="b_state">-</span></div>
    <div class="kv"><span>PID</span><span id="b_pid">-</span></div>
    <div class="kv"><span>端口</span><span id="b_port">-</span></div>
    <div class="kv"><span>端口连通</span><span id="b_open">-</span></div>
    <div class="kv"><span>已重启</span><span id="b_restarts">-</span></div>
    <div class="kv"><span>运行时长</span><span id="b_uptime">-</span></div>
    <div class="kv"><span>最近错误</span><span id="b_err">-</span></div>
    <div class="btns">
      <button class="primary" id="b_start">▶ 启动</button>
      <button class="danger" id="b_stop">■ 停止</button>
      <button id="b_restart">⟳ 重启</button>
    </div>
    <div class="hint">停止后管理器不会自动拉起；启动 / 重启由上方按钮或面板触发。后端崩溃时管理器会自动重启。</div>
  </div>

  <div class="card">
    <h2>前端 (面板)</h2>
    <div class="kv"><span>面板地址</span><span id="panel_url">-</span></div>
    <div class="kv"><span>独立前端</span><span id="f_state">-</span></div>
    <div class="hint" id="f_hint"></div>
    <div class="row">
      <button id="open_panel">打开面板</button>
    </div>
    <div class="hint">生产面板由后端在 /ui 直接托管；仅 dev 模式才会单独拉起前端开发服务器。</div>
  </div>

  <div class="card" style="grid-column:1 / -1">
    <h2>后端日志 (logs/manager/backend.log)</h2>
    <div class="btns">
      <button id="log_refresh">刷新日志</button>
      <label><input type="checkbox" id="log_auto" checked/> 自动刷新 (3s)</label>
    </div>
    <pre id="log">加载中…</pre>
  </div>
</main>

<script>
const base = location.origin;
const hubPort = HUB_PORT_PLACEHOLDER;
function fmt(v){return v===undefined||v===null?"-":v;}
async function api(path, opts){const r=await fetch(base+path,opts||{});const t=await r.text();let d=null;try{d=t?JSON.parse(t):null}catch{e}d=d||{};if(!r.ok)throw new Error(d.detail||d.message||("HTTP "+r.status));return d;}
async function refresh(){
  try{
    const s=await api("/api/manager/status");
    document.getElementById("mstatus").className="pill ok";
    document.getElementById("mstatus").textContent="管理器在线";
    document.getElementById("minfo").textContent="守护间隔 "+s.guard_interval+"s · 管理器 PID "+s.manager.pid;
    const b=s.backend;
    document.getElementById("b_state").textContent=b.alive?"运行中":(b.want_up?"待拉起":"已停止");
    document.getElementById("b_pid").textContent=fmt(b.pid);
    document.getElementById("b_port").textContent=fmt(b.port);
    document.getElementById("b_open").textContent=b.port_open?"✅":"❌";
    document.getElementById("b_open").style.color=b.port_open?"var(--ok)":"var(--err)";
    document.getElementById("b_restarts").textContent=fmt(b.restarts);
    document.getElementById("b_uptime").textContent=fmt(b.uptime)+"s";
    document.getElementById("b_err").textContent=fmt(b.last_error);
    document.getElementById("panel_url").textContent="http://127.0.0.1:"+s.manager.hub_port+"/ui";
    if(s.frontend){
      document.getElementById("f_state").textContent=s.frontend.alive?"运行中":"未运行";
      document.getElementById("f_hint").textContent="独立前端进程 (端口 "+s.frontend.port+")";
    }else{
      document.getElementById("f_state").textContent="未配置（使用后端 /ui）";
    }
  }catch(e){
    document.getElementById("mstatus").className="pill err";
    document.getElementById("mstatus").textContent="管理器离线";
    document.getElementById("minfo").textContent=e.message;
  }
}
async function doLog(){
  try{const r=await api("/api/manager/backend/log?lines=200");document.getElementById("log").textContent=r.content||"(空)";}catch(e){document.getElementById("log").textContent="读取失败: "+e.message;}
}
function toast(t,ok){const el=document.getElementById("mstatus");el.textContent=t;el.className="pill "+(ok?"ok":"err");}
document.getElementById("b_start").onclick=async()=>{try{await api("/api/manager/backend/start",{method:"POST"});toast("已发送启动指令",true);await refresh();}catch(e){toast(e.message,false);}};
document.getElementById("b_stop").onclick=async()=>{try{await api("/api/manager/backend/stop",{method:"POST"});toast("已发送停止指令",true);await refresh();}catch(e){toast(e.message,false);}};
document.getElementById("b_restart").onclick=async()=>{try{await api("/api/manager/backend/restart",{method:"POST"});toast("已发送重启指令",true);await refresh();}catch(e){toast(e.message,false);}};
document.getElementById("open_panel").onclick=()=>{window.open("http://127.0.0.1:"+hubPort+"/ui","_blank");};
document.getElementById("log_refresh").onclick=doLog;
refresh();doLog();
setInterval(refresh,3000);
let at=setInterval(doLog,3000);
document.getElementById("log_auto").onchange=(e)=>{if(e.target.checked){at=setInterval(doLog,3000);}else{clearInterval(at);}};
</script>
</body></html>"""

app = FastAPI(title="LcTTS 管家 · 统一管理器", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
async def control_page():
    html = CONTROL_PAGE.replace("HUB_PORT_PLACEHOLDER", str(daemon.hub_port))
    return HTMLResponse(html)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "uptime": round(time.time() - _BOOT, 1),
        "manager_port": daemon.mcfg.get("port"),
        "hub_port": daemon.hub_port,
    }


@app.get("/api/manager/status")
async def status():
    return {
        "manager": {
            "pid": os.getpid(),
            "uptime": round(time.time() - _BOOT, 1),
            "port": daemon.mcfg.get("port"),
            "hub_port": daemon.hub_port,
        },
        "backend": daemon.backend.to_dict(),
        "frontend": daemon.frontend.to_dict() if daemon.frontend else None,
        "guard_interval": daemon.guard_interval,
    }


@app.post("/api/manager/backend/start")
async def start_backend():
    daemon.start_backend()
    return {"ok": True, "backend": daemon.backend.to_dict()}


@app.post("/api/manager/backend/stop")
async def stop_backend(force: bool = False):
    daemon.stop_backend(graceful=not force, force=force)
    return {"ok": True, "backend": daemon.backend.to_dict()}


@app.post("/api/manager/backend/restart")
async def restart_backend(force: bool = False):
    daemon.restart_backend(force=force)
    return {"ok": True, "backend": daemon.backend.to_dict()}


@app.get("/api/manager/backend/log")
async def backend_log(lines: int = 200):
    return {"backend": daemon.backend.log_name, "lines": lines, "content": daemon.backend.tail_log(lines)}


@app.post("/api/manager/shutdown")
async def shutdown_manager():
    daemon.shutdown_all()
    async def _bye():
        await asyncio.sleep(0.5)
        os._exit(0)
    asyncio.create_task(_bye())
    return {"ok": True, "message": "管理器正在退出（已停止后端与前端）"}


from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动即拉起后端（与前端）
    daemon.start_all()
    guard = asyncio.create_task(daemon.guard_loop())
    try:
        yield
    finally:
        daemon._shutdown = True
        daemon.shutdown_all()
        guard.cancel()


app.router.lifespan_context = lifespan


def main() -> None:
    mcfg = registry.manager()
    host = mcfg.get("host") or "0.0.0.0"
    port = int(mcfg.get("port") or 5290)
    hub_port = int(registry.hub().get("port") or 5190)
    print("=" * 64)
    print("  LcTTS 管家 · 统一管理器（守护进程）")
    print(f"  监听     : http://{host}:{port}")
    print(f"  控制页面 : http://127.0.0.1:{port}/")
    print(f"  后端 hub : http://127.0.0.1:{hub_port}/ui")
    print("=" * 64)
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
