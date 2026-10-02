"""TTS API 管家 · FastAPI 服务

轻量常驻：管家自身只做「路由 + 生命周期 + 配置」，不加载任何模型。

    POST /api/tts?model=voxcpm           → 自动卸载当前引擎、拉起 voxcpm、透传合成请求
    GET  /api/hub/engines                → 查询所有已注册引擎
    GET  /api/hub/engines/voxcpm/params  → 查询该引擎支持的参数
    PUT  /api/hub/engines/voxcpm/config  → 在线修改该引擎的默认参数（持久化）
    GET  /api/hub/passthrough/<任意路径>  → 任务查询 / 音频下载 / 进度流 全透传
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from .journal import get_journal, sync_limits_from_hub
from .manager import EngineError, EngineManager, EngineProcess
from .proxy import (
    CONTROL_PARAMS,
    filter_headers,
    forward,
    prepare_body,
    strip_control,
    to_response,
    to_streaming_response,
)
from .registry import MUTABLE_HUB_KEYS, ROOT, get_registry

registry = get_registry()
manager = EngineManager()


# ---------------------------------------------------------------------------
# 并发闸门
# ---------------------------------------------------------------------------
class _Gate:
    """重请求（合成类）并发闸门。

    设计取舍：**宁可快速失败，也不要无限堆积**。
    TTS 单条合成动辄数秒~数十秒，若来者不拒，成百个请求会同时挂着请求体、
    上游连接与响应流，内存线性上涨直至进程被杀（这是"外部大量请求就崩溃"的主因）。
    超限时直接 503 + Retry-After，让调用方退避重试，管家自身保持健康。

    上限每次从 hub 配置读取 → PUT /api/hub/config 改完立即生效，无需重启。
    0 = 不限制（等同旧行为）。
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._total = 0
        self._per: Dict[str, int] = {}

    async def acquire(self, engine: str, total_max: int, per_max: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout if timeout and timeout > 0 else None
        while True:
            async with self._lock:
                if (not total_max or self._total < total_max) and (
                    not per_max or self._per.get(engine, 0) < per_max
                ):
                    self._total += 1
                    self._per[engine] = self._per.get(engine, 0) + 1
                    return True
            wait = 0.2
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                wait = min(wait, left)
            await asyncio.sleep(wait)

    async def release(self, engine: str) -> None:
        async with self._lock:
            self._total = max(0, self._total - 1)
            left = self._per.get(engine, 0) - 1
            if left > 0:
                self._per[engine] = left
            else:
                self._per.pop(engine, None)

    def snapshot(self) -> Dict[str, Any]:
        return {"inflight_total": self._total, "inflight_by_engine": dict(self._per)}


_GATE = _Gate()

# JSON 体里嗅探 model 字段的大小上限：更大的体（音频 base64 等）不做解析，
# 避免把几十 MB 的请求体完整读进内存只为找 model
_PEEK_BODY_LIMIT = 2 * 1024 * 1024


def _drop_body_cache(request: Request) -> None:
    """转发完成后主动丢掉大请求体的缓存副本。

    Starlette 会把 `request.body()` 的结果缓存在 `request._body` 上直到请求结束；
    上传参考音频时，每个并发请求都会长期持有一份完整副本 —— 批量调用时这部分
    内存相当可观。转发既已完成，缓存可以安全释放。
    """
    try:
        raw = getattr(request, "_body", None)
        if isinstance(raw, (bytes, bytearray)) and len(raw) > 1024 * 1024:
            request._body = b""
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _engine_public(cfg: Dict[str, Any]) -> Dict[str, Any]:
    inst = manager._active.get(cfg.get("name"))
    return {
        "name": cfg.get("name"),
        "display_name": cfg.get("display_name"),
        "aliases": cfg.get("aliases") or [],
        "description": cfg.get("description") or "",
        "enabled": bool(cfg.get("enabled", True)),
        "state": inst.state if inst else "stopped",
        "pid": inst.pid if inst else None,
        "port": (cfg.get("server") or {}).get("port"),
        "host": (cfg.get("server") or {}).get("host"),
        "health": cfg.get("health") or {},
        "endpoints": cfg.get("endpoints") or {},
        "default_endpoint": cfg.get("default_endpoint") or "clone",
        "file_fields": cfg.get("file_fields") or [],
        "body_mode": cfg.get("body_mode") or "auto",
        "defaults": cfg.get("defaults") or {},
    }


def _require_engine(name: str) -> Dict[str, Any]:
    cfg = registry.engine(name)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"未注册的引擎: {name}")
    return cfg


def _resolve_model(model: Optional[str]) -> tuple[str, Dict[str, Any]]:
    if not model:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "缺少 model 参数（可用 query ?model= 或请求头 X-TTS-Model）",
                "available": registry.engine_names(),
            },
        )
    hit = registry.resolve(model)
    if hit is None:
        raise HTTPException(
            status_code=404,
            detail={"message": f"未知模型: {model}", "available": registry.engine_names()},
        )
    return hit


def _resolve_endpoint(cfg: Dict[str, Any], endpoint: Optional[str]) -> str:
    endpoints = cfg.get("endpoints") or {}
    if not endpoint:
        endpoint = cfg.get("default_endpoint") or "clone"
    if str(endpoint).startswith("/"):
        return str(endpoint)
    if endpoint in endpoints:
        return str(endpoints[endpoint])
    raise HTTPException(
        status_code=400,
        detail={"message": f"引擎 {cfg.get('name')} 没有名为 {endpoint} 的端点", "available": list(endpoints)},
    )


async def _peek_body_model(request: Request) -> Optional[str]:
    """JSON 请求体里也可以带 model 字段（超过 2MB 的体不嗅探，省内存）。"""
    ct = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ct != "application/json":
        return None
    try:
        if int(request.headers.get("content-length") or 0) > _PEEK_BODY_LIMIT:
            return None
    except ValueError:
        pass
    raw = await request.body()
    if not raw or len(raw) > _PEEK_BODY_LIMIT:
        return None
    try:
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(payload, dict):
        for key in payload:
            if key.lower() in CONTROL_PARAMS and key.lower() == "model":
                return str(payload[key])
    return None


_TRUTHY_OFF = {"0", "false", "no", "off", "none"}


def _flag(request: Request, query_key: str, header_key: str, default: bool) -> bool:
    """读取布尔型开关：query > header > default。

    识别关闭语义的词：0 / false / no / off / none（大小写不敏感）。
    """
    raw = request.query_params.get(query_key)
    if raw is None:
        raw = request.headers.get(header_key) or request.headers.get(header_key.lower())
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() not in _TRUTHY_OFF


def _want_inject_defaults(request: Request) -> bool:
    """是否注入服务端配置的 defaults。默认开；显式传 0 则退化为纯透传。"""
    return _flag(request, "inject_defaults", "X-Hub-Inject-Defaults", True)


def _want_strict(request: Request) -> bool:
    """是否让 Hub 先把关必填参数。默认关（交给引擎返回 422），可按需打开。"""
    return _flag(request, "strict", "X-Hub-Strict", False)


async def _release(inst: EngineProcess, engine: Optional[str] = None) -> None:
    """在途计数 -1；`engine` 非空时同时归还并发闸门槽位。"""
    inst.inflight = max(0, inst.inflight - 1)
    inst.touch()
    if engine:
        await _GATE.release(engine)


def _wrap_release(stream: Any, inst: EngineProcess, engine: Optional[str]) -> Any:
    """给流式响应套一层"传完即释放"。

    旧实现把释放挂在 background task 上：客户端中途断开时 background 不一定执行，
    inflight 就永久泄漏，引擎被判定为永远忙碌、显存再也回收不掉。
    这里改成跟响应体的生命周期绑定 —— 正常结束或连接中断都会走到 finally。
    """

    async def _gen():
        try:
            async for chunk in stream:
                yield chunk
        finally:
            await _release(inst, engine)

    return _gen()


async def _do_forward(
    request: Request,
    model: str,
    endpoint: Optional[str],
    extra_query: Optional[Dict[str, Any]] = None,
    target_path: Optional[str] = None,
    heavy: bool = True,
):
    """转发到引擎。

    :param heavy: 是否为重请求。合成 / 写操作 = True（受并发闸门限制）；
                  任务查询、音频下载等 GET 透传是轻量高频轮询，只计数不限流。
    """
    jr = get_journal()
    rec = getattr(request.state, "journal_rec", None)
    client_ip = request.client.host if request.client else "-"

    name, cfg = _resolve_model(model)
    jr.annotate(rec, model=model, engine=name)
    try:
        inst = await manager.ensure(name)
    except EngineError as exc:
        jr.annotate(rec, error=str(exc)[:300])
        raise HTTPException(status_code=503, detail={"message": str(exc), "engine": name})

    path = target_path or _resolve_endpoint(cfg, endpoint)
    if not path.startswith("/"):
        path = "/" + path
    jr.annotate(rec, endpoint=path)

    body_mode = str(cfg.get("body_mode") or "auto")

    # 服务端配置的默认参数是否注入：默认注入（面板「填充默认」依赖它），
    # 调用方要**纯透传**语义时传 inject_defaults=0，Hub 就不会补任何客户端没发的字段。
    defaults = (cfg.get("defaults") or {}) if _want_inject_defaults(request) else {}
    mapper = registry.alias_mapper(name)

    trace: Dict[str, Any] = {}
    try:
        body = await prepare_body(request, defaults, body_mode, mapper=mapper, trace=trace)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"请求体解析失败: {exc}")
    alias_notes: List[Dict[str, str]] = trace.get("alias_notes") or []

    # strict=1：由 Hub 先把关必填，缺参直接 400，
    # 避免把注定失败的请求打到引擎上（尤其会白白触发一次冷启动/模型加载）。
    if _want_strict(request):
        provided: List[str] = list((body.get("json") or body.get("data") or {}).keys())
        provided += [str(k) for k, _ in (body.get("files") or [])]
        missing = mapper.missing_required(provided)
        if missing:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "缺少必填参数",
                    "engine": name,
                    "missing": missing,
                    "hint": "可用 GET /api/hub/engines/{name}/params 查看该引擎的必填字段与规范别名",
                },
            )

    query = strip_control(dict(request.query_params))
    if extra_query:
        query.update(extra_query)
    headers = filter_headers(request.headers, drop_content_type=True)

    hub = registry.hub()
    timeout = float(hub.get("forward_timeout") or 0)

    # ---- 并发闸门：满了就快速失败，绝不无限堆积 ----
    # 刻意放在所有本地校验之后 —— 注定 400 的请求不该占着槽位
    slot: Optional[str] = None
    if heavy:
        total_max = int(hub.get("max_inflight_requests") or 0)
        per_max = int(hub.get("max_inflight_per_engine") or 0)
        wait = float(hub.get("queue_wait_timeout") or 0)
        if not await _GATE.acquire(name, total_max, per_max, wait):
            jr.annotate(rec, error="管家繁忙：并发已达上限，已拒绝")
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "管家繁忙：并发已达上限，请稍后重试",
                    "engine": name,
                    "hint": (
                        "可调大 hub.max_inflight_requests / hub.max_inflight_per_engine，"
                        "或按响应头 Retry-After 退避重试"
                    ),
                    "limits": {"max_inflight_requests": total_max, "max_inflight_per_engine": per_max},
                },
                headers={"Retry-After": "2"},
            )
        slot = name

    inst.inflight += 1
    inst.touch()
    try:
        client, resp = await forward(
            inst, request.method, path, params=query, headers=headers, body=body, timeout=timeout
        )
    except Exception as exc:  # noqa: BLE001
        await _release(inst, slot)
        raise HTTPException(
            status_code=502,
            detail={"message": f"转发到引擎 {name} 失败: {exc}", "engine": name, "url": f"{inst.base_url}{path}"},
        )

    extra = {}
    if hub.get("expose_engine_header", True):
        extra["X-TTS-Engine"] = name
        extra["X-TTS-Engine-Display"] = str(cfg.get("display_name") or name)
        extra["X-TTS-Engine-Port"] = str(inst.port)
    # 回显本次的字段翻译明细，便于排查"客户端发了但引擎没生效"
    renamed = [n for n in alias_notes if n.get("how") == "alias"]
    if renamed:
        extra["X-Hub-Alias"] = ";".join(f"{n['from']}>{n['to']}" for n in renamed[:20])

    def _on_json(data: Dict[str, Any]) -> None:
        """嗅探任务型引擎的创建响应，把 task_id 登记进服务端任务台账。

        这样**外部程序直接调用管家**创建的任务，面板上也能看到。
        """
        tid = data.get("task_id")
        if isinstance(tid, str) and tid.strip():
            jr.register_task(tid.strip(), name, path, client_ip, rec.id if rec else None)
            jr.annotate(rec, task_id=tid.strip())

    try:
        out = await to_response(client, resp, extra, on_json=_on_json)
    except Exception as exc:  # noqa: BLE001
        await _release(inst, slot)
        raise HTTPException(
            status_code=502,
            detail={"message": f"读取引擎响应失败: {exc}", "engine": name, "url": f"{inst.base_url}{path}"},
        )

    if isinstance(out, StreamingResponse):
        # 音频流 / SSE：释放动作挂在迭代器上，客户端断开也能归位
        out.body_iterator = _wrap_release(out.body_iterator, inst, slot)
    else:
        await _release(inst, slot)
    return out


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    reaper = asyncio.create_task(manager.idle_reaper())
    try:
        yield
    finally:
        reaper.cancel()
        if registry.hub().get("stop_engines_on_exit", True):
            await manager.stop_all()


app = FastAPI(
    title="LcTTS 管家",
    description=(
        "LcTTS 管家：统一 TTS API 管家。以 model 名称驱动引擎的按需拉起与卸载，"
        "输入参数、通信与输出全部透传。管家自身不加载任何模型。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# 请求日志中间件
# 记录每一次有意义的 API 调用（含外部程序直接调用的）；
# 面板静态资源、接口文档与 /health 探活不记，避免刷屏。
# ---------------------------------------------------------------------------
_SKIP_LOG_PREFIXES = ("/ui", "/docs", "/redoc", "/openapi.json", "/favicon.ico")
_SKIP_LOG_PATHS = {"/health"}


@app.middleware("http")
async def _journal_middleware(request: Request, call_next):
    path = request.url.path
    if path in _SKIP_LOG_PATHS or any(path.startswith(p) for p in _SKIP_LOG_PREFIXES):
        return await call_next(request)

    jr = get_journal()
    client_ip = request.client.host if request.client else "-"
    rec = jr.open_request(request.method, path, str(request.url.query or ""), client_ip)
    request.state.journal_rec = rec

    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:  # noqa: BLE001
        jr.close_request(rec, 500, (time.perf_counter() - started) * 1000, error=str(exc)[:300])
        raise
    finally:
        # 请求已处理完，丢掉大请求体的缓存副本（批量调用时这部分内存很可观）
        _drop_body_cache(request)
    jr.close_request(rec, response.status_code, (time.perf_counter() - started) * 1000)
    return response


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------
@app.get("/", summary="管家概览")
async def index():
    hub = registry.hub()
    return {
        "service": "LcTTS 管家",
        "version": "1.0.0",
        "root": str(ROOT),
        "self": f"http://127.0.0.1:{hub.get('port')}",
        "engines_registered": len(registry.engine_names()),
        "active": manager.status().get("active"),
        "endpoints": {
            "查询引擎列表": "GET /api/hub/engines",
            "查询引擎参数": "GET /api/hub/engines/{name}/params",
            "读取引擎配置": "GET /api/hub/engines/{name}/config",
            "修改引擎配置": "PUT /api/hub/engines/{name}/config",
            "切换/预拉起模型": "POST /api/hub/switch",
            "卸载当前模型": "POST /api/hub/unload",
            "统一合成入口": "POST /api/tts?model=<模型名>",
            "任意路径透传": "ANY /api/hub/passthrough/<path>?model=<模型名>",
            "可视化面板": "GET /ui",
            "接口文档": "GET /docs",
        },
    }


@app.get("/health", summary="管家健康检查")
async def health():
    return {"status": "ok", "uptime": round(time.time() - _BOOT, 1), **manager.status()}


# ---------------------------------------------------------------------------
# 引擎查询
# ---------------------------------------------------------------------------
@app.get("/api/hub/models", summary="所有可用模型名（含别名）")
async def list_models():
    out: List[Dict[str, Any]] = []
    for name in registry.engine_names():
        cfg = registry.engine(name) or {}
        out.append(
            {
                "engine": name,
                "model": name,
                "display_name": cfg.get("display_name") or name,
                "aliases": cfg.get("aliases") or [],
                "names": [name] + [str(cfg.get("display_name") or name)] + list(cfg.get("aliases") or []),
                "enabled": bool(cfg.get("enabled", True)),
                "port": (cfg.get("server") or {}).get("port"),
                "description": cfg.get("description") or "",
            }
        )
    return {"count": len(out), "models": out}


@app.get("/api/hub/engines", summary="所有已注册引擎")
async def list_engines(with_params: bool = Query(False, description="是否一并返回参数表")):
    out = []
    for name in registry.engine_names():
        cfg = registry.engine(name)
        if cfg is None:
            continue
        item = _engine_public(cfg)
        if with_params:
            item["params"] = cfg.get("params") or {}
        out.append(item)
    return {"count": len(out), "engines": out}


@app.get("/api/hub/engines/{name}", summary="单个引擎详情")
async def get_engine(name: str):
    cfg = _require_engine(name)
    item = _engine_public(cfg)
    item["params"] = cfg.get("params") or {}
    item["runtime"] = {
        "python": (cfg.get("runtime") or {}).get("python"),
        "script": (cfg.get("runtime") or {}).get("script"),
        "args": (cfg.get("runtime") or {}).get("args") or [],
        "cwd": (cfg.get("runtime") or {}).get("cwd"),
    }
    inst = manager._active.get(name)
    item["log_path"] = inst.log_path if inst else str(ROOT / str(registry.hub().get("log_dir")) / f"{name}.log")
    return item


# §1.1：task / download 端点缺省约定，客户端按这两个模板轮询与下载
DEFAULT_TASK_TPL = "/api/v1/tasks/{task_id}"
DEFAULT_DOWNLOAD_TPL = "/api/v1/voice/download/{task_id}"


@app.get("/api/hub/engines/{name}/params", summary="查询该引擎支持的参数（含规范化别名）")
async def get_engine_params(name: str):
    """能力声明接口：**只读取注册表 manifest，不会拉起引擎**，可安全高频调用。

    `params` 同时包含两类字段：

    * 引擎原生字段名 —— 直接按原生名发送亦可；
    * 该语义的规范化别名 —— 条目上带 `alias_of`，客户端按统一规范名探测/发送即可，
      管家会在转发前翻译回原生名，不会把别名与原生名重复发给引擎。

    另提供 `canonical`（规范名 → 原生名）供客户端做一次性映射。
    """
    cfg = _require_engine(name)
    mapper = registry.alias_mapper(name)
    params = mapper.canonical_view()

    eps = dict(cfg.get("endpoints") or {})
    eps.setdefault("task", DEFAULT_TASK_TPL)
    eps.setdefault("download", DEFAULT_DOWNLOAD_TPL)

    file_fields = [k for k, v in params.items() if isinstance(v, dict) and v.get("type") == "file"]
    # 别名条目不进 required 列表，避免客户端看到同一必填项重复出现两次
    required = [
        k
        for k, v in params.items()
        if isinstance(v, dict) and v.get("required") and not v.get("alias_of")
    ]
    return {
        "engine": name,
        "display_name": cfg.get("display_name") or name,
        "body_mode": cfg.get("body_mode") or "auto",
        "file_fields": file_fields,
        "endpoints": eps,
        "default_endpoint": cfg.get("default_endpoint") or "clone",
        "current_defaults": cfg.get("defaults") or {},
        "canonical": mapper.canonical_map(),
        "required": required,
        "count": len(params),
        "params": params,
    }


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------
@app.get("/api/hub/engines/{name}/config", summary="读取引擎可修改配置")
async def get_engine_config(name: str):
    cfg = _require_engine(name)
    ov = (registry.overrides().get("engines") or {}).get(name) or {}
    return {
        "engine": name,
        "current": {
            "enabled": cfg.get("enabled", True),
            "display_name": cfg.get("display_name"),
            "aliases": cfg.get("aliases") or [],
            "description": cfg.get("description") or "",
            "server": cfg.get("server") or {},
            "health": cfg.get("health") or {},
            "endpoints": cfg.get("endpoints") or {},
            "default_endpoint": cfg.get("default_endpoint") or "clone",
            "file_fields": cfg.get("file_fields") or [],
            "body_mode": cfg.get("body_mode") or "auto",
            "defaults": cfg.get("defaults") or {},
            "runtime": cfg.get("runtime") or {},
        },
        "overrides": ov,
        "mutable_keys": sorted(
            [
                "enabled",
                "display_name",
                "aliases",
                "description",
                "runtime",
                "server",
                "health",
                "endpoints",
                "default_endpoint",
                "defaults",
                "file_fields",
                "body_mode",
            ]
        ),
        "note": "PUT 本接口即可合并修改；修改写入 config/tts_hub.overrides.yaml，params 为接口能力声明不可改。",
    }


@app.put("/api/hub/engines/{name}/config", summary="修改引擎配置（持久化）")
async def put_engine_config(name: str, patch: Dict[str, Any] = Body(...)):
    _require_engine(name)
    try:
        cfg = registry.update_engine(name, patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    need_restart = any(k in patch for k in ("runtime", "server", "health"))
    return {
        "ok": True,
        "engine": name,
        "need_restart": need_restart,
        "message": ("配置已保存；runtime/server/health 变更需重启引擎生效" if need_restart else "配置已保存，下次请求即生效"),
        "defaults": cfg.get("defaults") or {},
        "overrides": (registry.overrides().get("engines") or {}).get(name) or {},
    }


@app.post("/api/hub/engines/{name}/config/reset", summary="恢复引擎默认配置")
async def reset_engine_config(name: str):
    _require_engine(name)
    cfg = registry.reset_engine(name)
    return {"ok": True, "engine": name, "defaults": cfg.get("defaults") or {}}


@app.get("/api/hub/config", summary="读取管家级配置")
async def get_hub_config():
    hub = registry.hub()
    return {
        "current": {k: hub.get(k) for k in sorted(set(list(hub) + list(_HUB_MUTABLE)))},
        "overrides": registry.overrides().get("hub") or {},
        "mutable_keys": sorted(_HUB_MUTABLE),
    }


@app.put("/api/hub/config", summary="修改管家级配置（持久化）")
async def put_hub_config(patch: Dict[str, Any] = Body(...)):
    try:
        hub = registry.update_hub(patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # 观测数据上限改动后立刻裁剪内存中的日志/台账
    if "journal_max_requests" in patch or "journal_max_tasks" in patch:
        sync_limits_from_hub()
    return {"ok": True, "hub": {k: hub.get(k) for k in sorted(_HUB_MUTABLE)}}


# 可运行期修改的管家级字段，与 registry.MUTABLE_HUB_KEYS 保持同一份定义，
# 避免两处白名单各自漂移
_HUB_MUTABLE = MUTABLE_HUB_KEYS


# ---------------------------------------------------------------------------
# 引擎生命周期
# ---------------------------------------------------------------------------
@app.get("/api/hub/status", summary="管家与引擎运行状态")
async def status():
    hub = registry.hub()
    return {
        "hub": {"pid": os.getpid(), "root": str(ROOT), "port": hub.get("port")},
        "gate": {
            **_GATE.snapshot(),
            "max_inflight_requests": int(hub.get("max_inflight_requests") or 0),
            "max_inflight_per_engine": int(hub.get("max_inflight_per_engine") or 0),
            "queue_wait_timeout": float(hub.get("queue_wait_timeout") or 0),
        },
        **manager.status(),
    }


@app.post("/api/hub/switch", summary="切换/预拉起模型（会自动卸载当前引擎）")
async def switch(model: str = Query(..., description="模型名或别名")):
    name, cfg = _resolve_model(model)
    try:
        inst = await manager.ensure(name)
    except EngineError as exc:
        raise HTTPException(status_code=503, detail={"message": str(exc), "engine": name})
    return {
        "ok": True,
        "elapsed": round(time.time() - inst.started_at, 1),
        "engine": inst.to_dict(),
    }


@app.post("/api/hub/unload", summary="卸载当前引擎（释放显存）")
async def unload(force: bool = Query(False, description="有请求在途时是否强制卸载")):
    try:
        stopped = await manager.unload_current(force=force)
    except EngineError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "stopped": stopped, **manager.status()}


@app.post("/api/hub/engines/{name}/start", summary="拉起指定引擎")
async def start_engine(name: str):
    _require_engine(name)
    try:
        inst = await manager.ensure(name)
    except EngineError as exc:
        raise HTTPException(status_code=503, detail={"message": str(exc), "engine": name})
    return {"ok": True, "engine": inst.to_dict()}


@app.post("/api/hub/engines/{name}/stop", summary="停止指定引擎")
async def stop_engine(name: str, force: bool = Query(False)):
    _require_engine(name)
    try:
        ok = await manager.stop(name, force=force)
    except EngineError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": ok, **manager.status()}


@app.post("/api/hub/engines/{name}/restart", summary="重启指定引擎")
async def restart_engine(name: str, force: bool = Query(True)):
    _require_engine(name)
    try:
        await manager.stop(name, force=force)
        inst = await manager.ensure(name)
    except EngineError as exc:
        raise HTTPException(status_code=503, detail={"message": str(exc), "engine": name})
    return {"ok": True, "engine": inst.to_dict()}


@app.get("/api/hub/engines/{name}/log", summary="查看引擎子进程日志（排错/看加载进度）")
async def engine_log(name: str, lines: int = Query(200, ge=1, le=5000)):
    _require_engine(name)
    return {"engine": name, "lines": lines, "content": manager.tail_log(name, lines)}


# ---------------------------------------------------------------------------
# 观测：请求日志与任务台账
# 让"经过管家的每一次调用"和"由外部请求创建的异步任务"都能在面板上看到
# ---------------------------------------------------------------------------
@app.get("/api/hub/journal/stats", summary="请求日志统计")
async def journal_stats():
    return get_journal().stats()


@app.get("/api/hub/requests", summary="经过管家的请求记录（含外部程序调用）")
async def list_requests(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    kind: Optional[str] = Query(None, description="synth / passthrough / lifecycle / config / query / other"),
    engine: Optional[str] = None,
    only_synth: bool = Query(False, description="只看合成与透传请求"),
):
    return get_journal().list_requests(limit, offset, kind, engine, only_synth)


@app.post("/api/hub/requests/clear", summary="清空请求记录")
async def clear_requests():
    return {"ok": True, "cleared": get_journal().clear_requests()}


@app.get("/api/hub/tasks", summary="服务端任务台账（外部请求创建的任务也在内）")
async def list_server_tasks(limit: int = Query(200, ge=1, le=1000), engine: Optional[str] = None):
    return get_journal().list_tasks(limit, engine)


@app.post("/api/hub/tasks/clear", summary="清空任务台账")
async def clear_server_tasks():
    return {"ok": True, "cleared": get_journal().clear_tasks()}


@app.post("/api/hub/shutdown", summary="关闭管家（先卸载全部引擎，再退出进程）")
async def shutdown():
    """桌面端「关闭」脚本依赖此接口：先优雅结束引擎释放显存，再退出管家进程。

    使用 taskkill 强杀管家会留下孤儿引擎进程继续占显存，所以务必走这里。
    """
    if not registry.hub().get("allow_shutdown_api", True):
        raise HTTPException(status_code=403, detail="shutdown 接口已在配置中禁用（hub.allow_shutdown_api: false）")

    stopped: List[str] = []
    if registry.hub().get("stop_engines_on_exit", True):
        try:
            stopped = await manager.unload_current(force=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[shutdown] 卸载引擎时出错: {exc}")

    async def _bye() -> None:
        await asyncio.sleep(0.6)
        os._exit(0)

    asyncio.create_task(_bye())
    return {"ok": True, "stopped": stopped, "message": "管家正在退出（引擎已卸载，显存已释放）"}


# ---------------------------------------------------------------------------
# 合成透传
# ---------------------------------------------------------------------------
@app.post("/api/tts", summary="统一合成入口（model 决定拉起哪个引擎）")
@app.post("/api/tts/{endpoint:path}", summary="统一合成入口 + 指定端点")
async def tts(request: Request, endpoint: Optional[str] = None):
    model = (
        request.query_params.get("model")
        or request.headers.get("X-TTS-Model")
        or request.headers.get("x-tts-model")
        or await _peek_body_model(request)
    )
    ep = endpoint or request.query_params.get("endpoint") or request.headers.get("X-TTS-Endpoint")
    return await _do_forward(request, model, ep)


@app.api_route(
    "/api/hub/passthrough/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    summary="任意路径透传到指定引擎（任务查询 / 音频下载 / SSE 进度）",
)
async def passthrough(request: Request, path: str):
    model = (
        request.query_params.get("model")
        or request.headers.get("X-TTS-Model")
        or request.headers.get("x-tts-model")
    )
    if not model:
        active = list(manager._active.keys())
        if len(active) == 1:
            model = active[0]
        elif not active:
            raise HTTPException(
                status_code=400,
                detail={"message": "没有活跃引擎，请用 ?model= 指定", "available": registry.engine_names()},
            )
        else:
            raise HTTPException(
                status_code=400,
                detail={"message": f"有多个活跃引擎，请用 ?model= 指定", "active": active},
            )
    # 任务查询 / 音频下载 / 进度流都是轻量高频轮询，不占并发闸门名额；
    # 只有写操作（提交合成等）才受限，避免"批量轮询把闸门堵死"
    heavy = request.method.upper() not in ("GET", "HEAD", "OPTIONS")
    return await _do_forward(request, model, None, target_path="/" + path, heavy=heavy)


# ---------------------------------------------------------------------------
# 可视化面板
# ---------------------------------------------------------------------------
# Vue3 + Element Plus 单页应用：由 frontend/ 构建输出到 tts_hub/static/
# 构建：cd frontend && npm install && npm run build
# ---------------------------------------------------------------------------
STATIC_DIR = Path(__file__).resolve().parent / "static"

_NO_BUILD_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>TTS 管家 · 面板未构建</title>
<style>body{background:#08090b;color:#e9e6df;font:14px/1.8 Consolas,'Microsoft YaHei',monospace;padding:40px}
code{color:#ffa42b}a{color:#3fd8c0}h2{font-weight:600}</style></head><body>
<h2>控制台尚未构建</h2>
<p>请先构建前端产物：</p>
<pre>cd frontend
npm install
npm run build</pre>
<p>产物会输出到 <code>tts_hub/static/</code>，完成后刷新本页即可。</p>
<p>期间可直接使用接口文档：<a href="/docs">/docs</a></p>
</body></html>"""


def _spa_target(full_path: str) -> Optional[Path]:
    """解析 SPA 资源：命中真实文件则返回，否则回退 index.html（交给前端路由）。"""
    root = STATIC_DIR.resolve()
    if full_path:
        target = (root / full_path).resolve()
        if target.is_file() and str(target).startswith(str(root)):
            return target
    return root / "index.html"


@app.get("/ui", include_in_schema=False)
@app.get("/ui/{full_path:path}", include_in_schema=False)
async def ui(full_path: str = ""):
    if not (STATIC_DIR / "index.html").is_file():
        return HTMLResponse(_NO_BUILD_HTML)
    target = _spa_target(full_path)
    # index.html 不缓存；assets 带 hash 指纹可长缓存
    cache = "no-cache" if target.name == "index.html" else "public, max-age=604800"
    return FileResponse(target, headers={"Cache-Control": cache})


_BOOT = time.time()
