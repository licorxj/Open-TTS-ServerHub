"""TTS API 管家 · 请求透传层

目标：**输入参数原样透传给引擎，通信与输出原样透回请求端**。

支持的请求体形态：
    · application/json              —— 解析后可与引擎 defaults 合并，再以 JSON 转发
    · multipart/form-data           —— 保留文件上传（参考音频），可与 defaults 合并
    · application/x-www-form-urlencoded
    · 其它（二进制 / 未知）          —— 原始字节直接转发

响应形态：
    · 普通 JSON
    · 音频二进制流（wav / mp3）
    · SSE（text/event-stream，如进度接口）
    全部以 StreamingResponse 原样回吐，保留状态码与 Content-Type / Content-Disposition。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTasks
from starlette.datastructures import UploadFile

from .manager import EngineProcess, HOP_BY_HOP

# 管家自身的控制参数，不会透传给引擎
CONTROL_PARAMS = {"model", "endpoint", "engine", "wait_ready"}


def filter_headers(src: Any, drop_content_type: bool = False) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in src.items():
        lk = k.lower()
        if lk in HOP_BY_HOP:
            continue
        if drop_content_type and lk in ("content-type", "content-length"):
            continue
        if lk in ("host",):
            continue
        out[k] = v
    return out


def strip_control(params: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in params.items() if k.lower() not in CONTROL_PARAMS}


async def prepare_body(
    request: Request,
    defaults: Optional[Dict[str, Any]],
    body_mode: str = "auto",
    mapper: Optional[Any] = None,
    trace: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """根据请求 Content-Type 构造 httpx 请求体参数。

    返回可直接解包给 httpx `client.build_request(..., **kwargs)` 的字典。
    未识别的形态退化为原始字节转发（保证“透传”语义）。

    :param mapper: `aliases.AliasMapper`，非 None 时先把客户端字段翻译成引擎原生名
    :param trace:  若传入，归一化明细会写进 `trace["alias_notes"]`，便于排错

    ⚠️ 顺序很重要：**先归一化、再合并 defaults**。
       否则服务端配置的原生名默认值会占住位置，客户端用规范名发的同名参数反而被去重覆盖。
    """
    ct = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    defaults = defaults or {}

    def to_native(box: Dict[str, Any]) -> Dict[str, Any]:
        """名字翻译 + 剔除管家控制参数。"""
        if mapper is None:
            return strip_control(box)
        native, notes = mapper.normalize(box)
        if trace is not None:
            trace["alias_notes"] = (trace.get("alias_notes") or []) + notes
        return strip_control(native)

    if body_mode == "json":
        raw = await request.body()
        payload: Any = None
        if raw:
            try:
                payload = json.loads(raw)
            except Exception:  # noqa: BLE001
                payload = None
        if isinstance(payload, dict):
            payload = to_native(payload)
            merged = dict(defaults)
            merged.update(payload)
            return {"json": merged}
        if raw:
            return {"content": raw}
        return {"json": dict(defaults)}

    if ct == "application/json":
        raw = await request.body()
        if not raw:
            return {"json": dict(defaults)} if defaults else {"content": b""}
        try:
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001
            return {"content": raw}
        if isinstance(payload, dict):
            payload = to_native(payload)
            if defaults:
                merged = dict(defaults)
                merged.update(payload)
                payload = merged
            return {"json": payload}
        return {"content": raw}

    if ct in ("multipart/form-data", "application/x-www-form-urlencoded"):
        form = await request.form()
        data: Dict[str, Any] = {}
        files: List[Tuple[str, Tuple[str, bytes, Optional[str]]]] = []
        for key, value in form.items():
            if isinstance(value, UploadFile):
                content = await value.read()
                ctype = value.content_type or "application/octet-stream"
                files.append((key, (value.filename or key, content, ctype)))
            else:
                data[key] = value

        # 文件字段同样要做别名翻译（如 AuK 的参考音频叫 audio，客户端发的是 ref_audio）
        if mapper is not None:
            seen: set = set()
            renames: List[Tuple[str, str]] = []
            new_files: List[Tuple[str, Tuple[str, bytes, Optional[str]]]] = []
            for key, ft in files:
                native = mapper.map(key) or key
                renames.append((key, native))
                if native in seen:
                    continue  # 规范名与原生名同时上传时只保留一份
                seen.add(native)
                new_files.append((native, ft))
            files = new_files
            if trace is not None:
                trace["alias_notes"] = (trace.get("alias_notes") or []) + [
                    {"from": a, "to": b, "how": "alias" if a != b else "native"}
                    for a, b in renames
                    if a != b
                ]

        data = to_native(data)
        for k, v in defaults.items():
            data.setdefault(k, v)
        if files:
            return {"data": data, "files": files}
        return {"data": data}

    raw = await request.body()
    return {"content": raw}


async def forward(
    inst: EngineProcess,
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[Dict[str, Any]] = None,
    timeout: float = 0,
) -> Tuple[httpx.AsyncClient, httpx.Response]:
    """把请求转发到引擎，返回 (client, response)。

    连接池复用：客户端挂在 `EngineProcess` 上（每个引擎一个），
    不再"每个请求 new 一个 AsyncClient" —— 批量调用时那会同时产生成百个
    TCP 连接与 httpx 内部缓冲，是内存与句柄暴涨的主因之一。

    返回 `shared=True` 语义：调用方**只**负责关闭 resp，不要关 client
    （client 由引擎停止时统一关闭）。
    """
    url = f"{inst.base_url}{path}"
    # 超时建在 client 上（httpx 的 send() 不支持 per-request timeout），
    # get_client 按 timeout 值缓存 client，改动 forward_timeout 依然即时生效
    client = await inst.get_client(timeout=timeout)
    req = client.build_request(
        method=method.upper(),
        url=url,
        params=params or {},
        headers=headers or {},
        **(body or {}),
    )
    resp = await client.send(req, stream=True)
    return client, resp


# 小于该体积的 JSON 响应会被完整读取，用于嗅探 task_id（任务创建响应通常只有几百字节）
JSON_SNIFF_LIMIT = 64 * 1024


def to_streaming_response(
    client: httpx.AsyncClient,
    resp: httpx.Response,
    extra_headers: Optional[Dict[str, str]] = None,
) -> StreamingResponse:
    """把上游响应原样包装成 FastAPI StreamingResponse。"""

    async def _close() -> None:
        # client 是引擎级共享连接池，这里只关本次响应流，不能关 client
        try:
            await resp.aclose()
        except Exception:  # noqa: BLE001
            pass

    headers = filter_headers(resp.headers)
    if extra_headers:
        headers.update(extra_headers)

    tasks = BackgroundTasks()
    tasks.add_task(_close)
    return StreamingResponse(
        resp.aiter_raw(),
        status_code=resp.status_code,
        headers=headers,
        background=tasks,
    )


async def to_response(
    client: httpx.AsyncClient,
    resp: httpx.Response,
    extra_headers: Optional[Dict[str, str]] = None,
    on_json: Optional[Any] = None,
):
    """包装上游响应；对小体积 JSON 额外做一次嗅探（拿到 task_id 登记任务台账）。

    · `application/json` 且 `Content-Length` 较小 → 完整读取后原样回吐，并调用 `on_json(data)`
    · 其余（音频 wav、SSE、大 JSON、未知长度）→ 保持流式转发，字节级透传
    """

    async def _close() -> None:
        # client 是引擎级共享连接池，这里只关本次响应流，不能关 client
        try:
            await resp.aclose()
        except Exception:  # noqa: BLE001
            pass

    headers = filter_headers(resp.headers)
    if extra_headers:
        headers.update(extra_headers)

    tasks = BackgroundTasks()
    tasks.add_task(_close)

    ctype = (resp.headers.get("content-type") or "").lower()
    try:
        clen = int(resp.headers.get("content-length") or 0)
    except ValueError:
        clen = 0

    if on_json is not None and ctype.startswith("application/json") and 0 < clen <= JSON_SNIFF_LIMIT:
        raw = await resp.aread()
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            data = None
        if isinstance(data, dict):
            try:
                on_json(data)
            except Exception:  # noqa: BLE001
                pass
        return Response(content=raw, status_code=resp.status_code, headers=headers, background=tasks)

    return StreamingResponse(
        resp.aiter_raw(),
        status_code=resp.status_code,
        headers=headers,
        background=tasks,
    )
