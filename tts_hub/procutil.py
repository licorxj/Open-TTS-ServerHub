"""引擎进程的「端口 ↔ PID」对账工具（零第三方依赖）。

管家把引擎状态放在内存字典里，一旦 hub 进程重启（崩溃被守护进程拉起、
Skill 调 /api/hub/shutdown、手动重启），这份字典就丢了，而引擎子进程因为用了
CREATE_NEW_PROCESS_GROUP 会继续活着占着显存与端口 —— 即「孤儿引擎」。

本模块提供不依赖任何运行期状态的外部事实来源：
    · listening_pids()  一次系统调用拿到全部 LISTEN 端口 → PID
    · pid_alive()       进程是否仍存在
    · kill_tree()       按 PID 结束整棵进程树（taskkill /T 或 killpg）
    · wait_port_closed() 等端口真正释放

跨平台：Windows 走 netstat/taskkill/tasklist，POSIX 走 ss + /proc/net/tcp 兜底。
"""
from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import time
from typing import Dict, Optional

_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# 端口
# ---------------------------------------------------------------------------
def port_open(host: str, port: int, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _parse_windows_netstat(out: str) -> Dict[int, int]:
    result: Dict[int, int] = {}
    for line in out.splitlines():
        parts = line.split()
        # TCP    0.0.0.0:8858    0.0.0.0:0    LISTENING    12345
        # TCP    [::]:8858       [::]:0       LISTENING    12345
        if len(parts) < 5 or not parts[0].upper().startswith("TCP"):
            continue
        if not parts[3].upper().startswith("LISTEN"):
            continue
        try:
            port = int(parts[1].rsplit(":", 1)[1])
            pid = int(parts[4])
        except (ValueError, IndexError):
            continue
        if pid > 0:
            result[port] = pid
    return result


def _parse_ss(out: str) -> Dict[int, int]:
    result: Dict[int, int] = {}
    for line in out.splitlines():
        if "pid=" not in line:
            continue
        pm = re.search(r"pid=(\d+)", line)
        if not pm:
            continue
        # LISTEN 0 128 0.0.0.0:8858 0.0.0.0:* users:(("python",pid=123,fd=4))
        cols = line.split()
        if len(cols) < 4:
            continue
        try:
            port = int(cols[3].rsplit(":", 1)[1])
        except (ValueError, IndexError):
            continue
        result[port] = int(pm.group(1))
    return result


def _proc_net_tcp_pids() -> Dict[int, int]:
    """没有 ss 时，用 /proc/net/tcp{,6} 的 LISTEN inode 反查 /proc/*/fd。"""
    inodes: Dict[str, int] = {}
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                next(fh, None)
                for line in fh:
                    cols = line.split()
                    if len(cols) < 10 or cols[3] != "0A":  # 0A = TCP_LISTEN
                        continue
                    port = int(cols[1].rsplit(":", 1)[1], 16)
                    inodes[cols[9]] = port
        except (OSError, ValueError, IndexError):
            continue
    if not inodes:
        return {}
    result: Dict[int, int] = {}
    try:
        entries = list(os.scandir("/proc"))
    except OSError:
        return {}
    for entry in entries:
        if not entry.name.isdigit():
            continue
        fd_dir = os.path.join(entry.path, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            m = re.match(r"socket:\[(\d+)\]", target)
            if m and m.group(1) in inodes:
                result[inodes[m.group(1)]] = int(entry.name)
                break
    return result


def listening_pids() -> Dict[int, int]:
    """返回 {监听端口: 进程 PID}。单次系统调用，供 reconcile 批量对账。"""
    try:
        if _WINDOWS:
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=12,
            ).stdout
            return _parse_windows_netstat(out)
        out = subprocess.run(
            ["ss", "-ltnpH"], capture_output=True, text=True, timeout=12
        ).stdout
        parsed = _parse_ss(out)
        return parsed or _proc_net_tcp_pids()
    except Exception:  # noqa: BLE001
        return {}


def pid_for_port(port: int) -> Optional[int]:
    return listening_pids().get(int(port))


def wait_port_closed(host: str, port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_open(host, port, timeout=0.3):
            return True
        time.sleep(0.3)
    return not port_open(host, port, timeout=0.3)


# ---------------------------------------------------------------------------
# 进程
# ---------------------------------------------------------------------------
def pid_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    if _WINDOWS:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=8,
            ).stdout
            return str(pid) in out
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def kill_tree(pid: Optional[int]) -> bool:
    """结束 PID 及其子进程树。返回该 PID 是否已消失。"""
    if not pid or pid <= 0 or pid == os.getpid():
        return False
    try:
        if _WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=20,
            )
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, AttributeError, PermissionError):
                os.kill(pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001
        if not _WINDOWS:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:  # noqa: BLE001
                pass
    # 给一点时间让进程真正退出
    for _ in range(10):
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    return not pid_alive(pid)
