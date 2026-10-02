"""LcTTS 管家 · 请求日志与任务台账

管家作为统一入口，需要能回答两个问题：

    1. 刚才有哪些请求经过了管家？（含外部程序直接调用的）
    2. 产生了哪些异步任务，现在什么状态？

之前这两件事都只存在于**浏览器本地**（localStorage），外部请求创建的异步任务
面板完全看不到。本模块把它们搬到服务端：

    · 请求日志：内存环形缓冲，记录每一次 HTTP 请求（来源、方法、路径、命中的引擎、
      状态码、耗时），默认保留最近 2000 条
    · 任务台账：从合成响应里嗅探出的 task_id（引擎 / 端点 / 创建时间 / 来源客户端）

两者都只驻留内存、进程重启即清空 —— 管家是常驻调度器，不是持久化审计系统。
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Any, Deque, Dict, List, Optional

MAX_REQUESTS = 2000
MAX_TASKS = 1000

# 单条记录里 query 的最大保留长度：外部调用可能把整段文本塞进 query，
# 不截断的话几千条日志就能吃掉几百 MB 内存
QUERY_KEEP = 512

# 请求分类：合成类会记录引擎与 task_id
_KIND_RULES = (
    (re.compile(r"^/api/tts(/.*)?$"), "synth"),
    (re.compile(r"^/api/hub/passthrough/"), "passthrough"),
    (re.compile(r"^/api/hub/engines/[^/]+/(start|stop|restart|log)"), "lifecycle"),
    (re.compile(r"^/api/hub/(switch|unload|shutdown)"), "lifecycle"),
    (re.compile(r"^/api/hub/.*/config"), "config"),
    (re.compile(r"^/api/hub/"), "query"),
    (re.compile(r"^/ui"), "panel"),
    (re.compile(r"^/(docs|openapi.json)"), "panel"),
)


def classify(method: str, path: str) -> str:
    for pattern, kind in _KIND_RULES:
        if pattern.match(path):
            return kind
    return "other"


def now_iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time()).strftime("%H:%M:%S")


def full_iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time()).isoformat(timespec="seconds")


@dataclass
class RequestRecord:
    id: int
    ts: float
    method: str
    path: str
    kind: str
    client: str = "-"
    query: str = ""
    model: Optional[str] = None
    engine: Optional[str] = None
    endpoint: Optional[str] = None
    task_id: Optional[str] = None
    status: int = 0
    duration_ms: float = 0.0
    inflight: bool = True
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "time": full_iso(self.ts),
            "clock": now_iso(self.ts),
            "ts": round(self.ts, 3),
            "method": self.method,
            "path": self.path,
            "query": self.query,
            "kind": self.kind,
            "client": self.client,
            "model": self.model,
            "engine": self.engine,
            "endpoint": self.endpoint,
            "task_id": self.task_id,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 1),
            "inflight": self.inflight,
            "error": self.error,
        }


class Journal:
    """线程/协程安全的请求日志与任务台账。"""

    def __init__(self, max_requests: int = MAX_REQUESTS, max_tasks: int = MAX_TASKS) -> None:
        self._max_requests = max(1, int(max_requests or MAX_REQUESTS))
        self._max_tasks = max(1, int(max_tasks or MAX_TASKS))
        self._requests: Deque[RequestRecord] = deque(maxlen=self._max_requests)
        self._by_id: Dict[int, RequestRecord] = {}
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._order: Deque[str] = deque(maxlen=self._max_tasks)
        self._seq = 0
        self._lock = Lock()
        self.started_at = time.time()

    def configure(self, max_requests: Optional[int] = None, max_tasks: Optional[int] = None) -> None:
        """运行期调整保留上限（PUT /api/hub/config 后调用），立即生效。

        容量变更时按新上限裁剪现有数据，避免内存占用超过设定值。
        """
        with self._lock:
            if max_requests:
                self._max_requests = max(1, int(max_requests))
                if self._requests.maxlen != self._max_requests:
                    self._requests = deque(self._requests, maxlen=self._max_requests)
                while len(self._by_id) > self._max_requests:
                    self._by_id.pop(next(iter(self._by_id)), None)
            if max_tasks:
                self._max_tasks = max(1, int(max_tasks))
                # 先按订单队列算出"保留哪些"，再重建队列：
                # 若先截断 deque，被丢掉的 key 就再也查不到，_tasks 会永远删不干净
                keep = list(self._order)[-self._max_tasks :]
                keep_set = set(keep)
                for key in list(self._tasks):
                    if key not in keep_set:
                        self._tasks.pop(key, None)
                self._order = deque(keep, maxlen=self._max_tasks)

    # ---------------------------------------------------------------- 请求日志
    def open_request(self, method: str, path: str, query: str = "", client: str = "-") -> RequestRecord:
        with self._lock:
            self._seq += 1
            rec = RequestRecord(
                id=self._seq,
                ts=time.time(),
                method=method.upper(),
                path=path,
                kind=classify(method, path),
                client=client,
                query=(query or "")[:QUERY_KEEP],
            )
            self._requests.append(rec)
            self._by_id[rec.id] = rec
            # 环形缓冲淘汰后同步清理索引。
            # dict 自 3.7 起保持插入顺序，next(iter(...)) 就是最旧的一条 → O(1)，
            # 旧实现每次淘汰都要全表扫描 _by_id，高并发下退化成 O(n²)。
            while len(self._by_id) > len(self._requests):
                self._by_id.pop(next(iter(self._by_id)), None)
            return rec

    def annotate(self, rec: Optional[RequestRecord], **fields: Any) -> None:
        """路由内部补写引擎 / 端点 / task_id 等信息。"""
        if rec is None:
            return
        with self._lock:
            for key, value in fields.items():
                if value is not None and hasattr(rec, key):
                    setattr(rec, key, value)

    def close_request(
        self,
        rec: Optional[RequestRecord],
        status: int,
        duration_ms: float,
        error: Optional[str] = None,
    ) -> None:
        if rec is None:
            return
        with self._lock:
            rec.status = status
            rec.duration_ms = duration_ms
            rec.inflight = False
            if error:
                rec.error = error

    def list_requests(
        self,
        limit: int = 100,
        offset: int = 0,
        kind: Optional[str] = None,
        engine: Optional[str] = None,
        only_synth: bool = False,
    ) -> Dict[str, Any]:
        with self._lock:
            items = list(self._requests)
        if kind:
            items = [r for r in items if r.kind == kind]
        if engine:
            items = [r for r in items if r.engine == engine]
        if only_synth:
            items = [r for r in items if r.kind in ("synth", "passthrough")]
        items.reverse()  # 最新在前
        total = len(items)
        page = items[offset : offset + limit]
        return {"total": total, "count": len(page), "offset": offset, "items": [r.to_dict() for r in page]}

    def clear_requests(self) -> int:
        with self._lock:
            n = len(self._requests)
            self._requests.clear()
            self._by_id.clear()
            return n

    # ---------------------------------------------------------------- 任务台账
    def register_task(
        self,
        task_id: str,
        engine: str,
        endpoint: Optional[str] = None,
        client: str = "-",
        request_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        key = f"{engine}:{task_id}"
        with self._lock:
            if key in self._tasks:
                item = self._tasks[key]
                item["last_seen"] = time.time()
                return item
            item = {
                "key": key,
                "task_id": task_id,
                "engine": engine,
                "endpoint": endpoint,
                "created_at": time.time(),
                "created_time": full_iso(),
                "client": client,
                "request_id": request_id,
                "last_seen": time.time(),
            }
            # 先按订单队列淘汰最旧的一条，再入队。
            # 旧实现先 append（deque 满时会自动丢掉最旧 key）再去 _order[0] 取，
            # 取到的已经不是最旧那条 → 真正最旧的记录永远留在 _tasks 里删不掉。
            if len(self._order) >= self._order.maxlen:
                oldest = self._order.popleft()
                if oldest != key:
                    self._tasks.pop(oldest, None)
            self._tasks[key] = item
            self._order.append(key)
            return item

    def list_tasks(self, limit: int = 200, engine: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            items = list(self._tasks.values())
        if engine:
            items = [t for t in items if t["engine"] == engine]
        items.sort(key=lambda t: t["created_at"], reverse=True)
        total = len(items)
        return {
            "total": total,
            "count": len(items[:limit]),
            "items": items[:limit],
        }

    def clear_tasks(self) -> int:
        with self._lock:
            n = len(self._tasks)
            self._tasks.clear()
            self._order.clear()
            return n

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            reqs = list(self._requests)
            by_kind: Dict[str, int] = {}
            by_engine: Dict[str, int] = {}
            for r in reqs:
                by_kind[r.kind] = by_kind.get(r.kind, 0) + 1
                if r.engine:
                    by_engine[r.engine] = by_engine.get(r.engine, 0) + 1
            inflight = sum(1 for r in reqs if r.inflight)
            return {
                "uptime_seconds": round(time.time() - self.started_at, 1),
                "requests_kept": len(reqs),
                "requests_inflight": inflight,
                "tasks_kept": len(self._tasks),
                "by_kind": by_kind,
                "by_engine": by_engine,
            }


_JOURNAL: Optional[Journal] = None


def get_journal() -> Journal:
    global _JOURNAL
    if _JOURNAL is None:
        _JOURNAL = Journal(**_limits_from_hub())
    return _JOURNAL


def _limits_from_hub() -> Dict[str, int]:
    """从管家配置读取观测数据上限；读不到就用代码内默认值。"""
    try:
        from .registry import get_registry  # 延迟导入：registry 不依赖 journal，无循环风险

        hub = get_registry().hub()
        return {
            "max_requests": int(hub.get("journal_max_requests") or MAX_REQUESTS),
            "max_tasks": int(hub.get("journal_max_tasks") or MAX_TASKS),
        }
    except Exception:  # noqa: BLE001
        return {"max_requests": MAX_REQUESTS, "max_tasks": MAX_TASKS}


def sync_limits_from_hub() -> None:
    """PUT /api/hub/config 改动上限后调用，让新上限立即生效。"""
    get_journal().configure(**_limits_from_hub())
