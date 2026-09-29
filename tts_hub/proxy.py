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
from fastapi.responses import StreamingResponse
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
) -> Dict[str, Any]:
    """根据请求 Content-Type 构造 httpx 请求体参数。

    返回可直接解包给 httpx `client.build_request(..., **kwargs)` 的字典。
    未识别的形态退化为原始字节转发（保证"透传"语义）。
    """
    ct = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    defaults = defaults or {}

    if body_mode == "json":
        raw = await request.body()
        payload: Any = None
        if raw:
            try:
                payload = json.loads(raw)
            except Exception:  # noqa: BLE001
                payload = None
        if isinstance(payload, dict):
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
            if defaults:
                merged = dict(defaults)
                merged.update(payload)
                payload = merged
            return {"json": strip_control(payload)}
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
        for k, v in defaults.items():
            data.setdefault(k, v)
        data = strip_control(data)
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
    """把请求转发到引擎，返回 (client, response)；调用方负责关闭。"""
    url = f"{inst.base_url}{path}"
    client = httpx.AsyncClient(
        timeout=None if not timeout else timeout,
        trust_env=False,
        follow_redirects=False,
    )
    try:
        req = client.build_request(
            method=method.upper(),
            url=url,
            params=params or {},
            headers=headers or {},
            **(body or {}),
        )
        resp = await client.send(req, stream=True)
    except Exception:
        await client.aclose()
        raise
    return client, resp


def to_streaming_response(
    client: httpx.AsyncClient,
    resp: httpx.Response,
    extra_headers: Optional[Dict[str, str]] = None,
) -> StreamingResponse:
    """把上游响应原样包装成 FastAPI StreamingResponse。"""

    async def _close() -> None:
        try:
            await resp.aclose()
        except Exception:  # noqa: BLE001
            pass
        try:
            await client.aclose()
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
