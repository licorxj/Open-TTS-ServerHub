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

from .manager import EngineError, EngineManager, EngineProcess
from .proxy import CONTROL_PARAMS, filter_headers, forward, prepare_body, strip_control, to_streaming_response
from .registry import ROOT, get_registry

registry = get_registry()
manager = EngineManager()


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
    """JSON 请求体里也可以带 model 字段。"""
    ct = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ct != "application/json":
        return None
    raw = await request.body()
    if not raw:
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


def _release(inst: EngineProcess) -> None:
    inst.inflight = max(0, inst.inflight - 1)
    inst.touch()


async def _do_forward(
    request: Request,
    model: str,
    endpoint: Optional[str],
    extra_query: Optional[Dict[str, Any]] = None,
    target_path: Optional[str] = None,
) -> StreamingResponse:
    name, cfg = _resolve_model(model)
    try:
        inst = await manager.ensure(name)
    except EngineError as exc:
        raise HTTPException(status_code=503, detail={"message": str(exc), "engine": name})

    path = target_path or _resolve_endpoint(cfg, endpoint)
    if not path.startswith("/"):
        path = "/" + path

    body_mode = str(cfg.get("body_mode") or "auto")
    defaults = cfg.get("defaults") or {}
    try:
        body = await prepare_body(request, defaults, body_mode)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"请求体解析失败: {exc}")

    query = strip_control(dict(request.query_params))
    if extra_query:
        query.update(extra_query)
    headers = filter_headers(request.headers, drop_content_type=True)

    hub = registry.hub()
    timeout = float(hub.get("forward_timeout") or 0)

    inst.inflight += 1
    inst.touch()
    try:
        client, resp = await forward(
            inst, request.method, path, params=query, headers=headers, body=body, timeout=timeout
        )
    except Exception as exc:  # noqa: BLE001
        _release(inst)
        raise HTTPException(
            status_code=502,
            detail={"message": f"转发到引擎 {name} 失败: {exc}", "engine": name, "url": f"{inst.base_url}{path}"},
        )

    extra = {}
    if hub.get("expose_engine_header", True):
        extra["X-TTS-Engine"] = name
        extra["X-TTS-Engine-Display"] = str(cfg.get("display_name") or name)
        extra["X-TTS-Engine-Port"] = str(inst.port)

    sr = to_streaming_response(client, resp, extra)
    if sr.background is not None:
        sr.background.add_task(_release, inst)
    else:
        _release(inst)
    return sr


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


@app.get("/api/hub/engines/{name}/params", summary="查询该引擎支持的参数")
async def get_engine_params(name: str):
    cfg = _require_engine(name)
    params = cfg.get("params") or {}
    required = [k for k, v in params.items() if isinstance(v, dict) and v.get("required")]
    return {
        "engine": name,
        "display_name": cfg.get("display_name") or name,
        "body_mode": cfg.get("body_mode") or "auto",
        "file_fields": cfg.get("file_fields") or [],
        "endpoints": cfg.get("endpoints") or {},
        "default_endpoint": cfg.get("default_endpoint") or "clone",
        "current_defaults": cfg.get("defaults") or {},
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
    return {"ok": True, "hub": {k: hub.get(k) for k in sorted(_HUB_MUTABLE)}}


_HUB_MUTABLE = {
    "max_active",
    "idle_ttl_seconds",
    "idle_check_interval",
    "unload_on_switch",
    "stop_engines_on_exit",
    "forward_timeout",
    "expose_engine_header",
    "adopt_existing",
    "allow_shutdown_api",
}


# ---------------------------------------------------------------------------
# 引擎生命周期
# ---------------------------------------------------------------------------
@app.get("/api/hub/status", summary="管家与引擎运行状态")
async def status():
    return {
        "hub": {"pid": os.getpid(), "root": str(ROOT), "port": registry.hub().get("port")},
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
    return await _do_forward(request, model, None, target_path="/" + path)


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
