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

import httpx
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from . import voices
from .asr import get_asr
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

_DEBUG_LOG = Path(__file__).resolve().parent.parent / "tts_hub_debug.log"


def _dbg(msg: str) -> None:
    """临时调试：把关键决策写进 tts_hub_debug.log（环境变量 TTS_HUB_DEBUG=1 开启）。"""
    if not os.environ.get("TTS_HUB_DEBUG"):
        return
    try:
        with open(_DEBUG_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:  # noqa: BLE001
        pass
manager = EngineManager()

# 上传音色时的临时落盘目录（用完即删）
VOICE_TMP = ROOT / "voice" / ".tmp"


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
    name = cfg.get("name")
    inst = manager._active.get(name)
    # 端口上还有进程、但本 hub 没有 Popen 句柄（上一代 hub 遗留 / 外部启动）。
    # 单独暴露，面板才能把「显示已停止、实际仍占显存」的孤儿显示成可关停状态。
    detached = manager.orphan_info(name)
    ttl = float(registry.hub().get("idle_ttl_seconds") or 0)
    return {
        "name": name,
        "display_name": cfg.get("display_name"),
        "aliases": cfg.get("aliases") or [],
        "description": cfg.get("description") or "",
        "enabled": bool(cfg.get("enabled", True)),
        "state": inst.state if inst else "stopped",
        "pid": inst.pid if inst else (detached or {}).get("pid"),
        "managed": inst.managed if inst else False,
        "orphan": bool(inst.orphan if inst else detached),
        "detached": bool(detached),
        "inflight": inst.inflight if inst else 0,
        "idle_seconds": round(time.time() - inst.last_used, 1) if inst else None,
        "idle_expires_in": manager.idle_left(inst, ttl) if inst else None,
        "last_error": inst.last_error if inst else None,
        "log_path": inst.log_path if inst else None,
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


# ---------------------------------------------------------------------------
# 默认音色兜底
# ---------------------------------------------------------------------------
def _body_dict(body: Dict[str, Any]) -> tuple:
    """取出请求体里的表单/JSON 字典与其所在位置（'json' / 'data'）。"""
    if isinstance(body.get("json"), dict):
        return body["json"], "json"
    if isinstance(body.get("data"), dict):
        return body["data"], "data"
    return None, ""


def _has_ref_audio(body: Dict[str, Any], mapper: Any) -> bool:
    """请求里是否已经带了参考音频（路径字段或上传字段都算）。"""
    keys: List[str] = []
    for slot in ("json", "data"):
        d = body.get(slot)
        if isinstance(d, dict):
            keys.extend(d.keys())
    keys.extend(str(k) for k, _ in (body.get("files") or []))
    for k in keys:
        sem = mapper.sem_of(k)
        if sem in ("ref_audio_path", "ref_audio"):
            return True
        native = mapper.map(k)
        if native and mapper.sem_of(native) in ("ref_audio_path", "ref_audio"):
            return True
    return False


def _pick_ref_field(mapper: Any) -> tuple:
    """（兼容旧调用）返回 (路径字段, 是否为上传文件)。"""
    path_field, file_field = _pick_ref_fields(mapper)
    if path_field:
        return path_field, False
    if file_field:
        return file_field, True
    return None, False


def _pick_ref_fields(mapper: Any) -> tuple:
    """挑出该引擎承载「参考音频」的原生字段。

    有些引擎同时声明了 *路径字段 与 *上传字段（甚至引擎只认上传字段，
    例如 Breeze-TTS-2 的 /v1/audio/speech 只接受 ref_audio 文件）。因此这里
    两个都返回，注入时一起给，引擎用哪个都行。

    :return: (路径字段, 上传字段)；路径字段为 None 表示引擎只接受上传
    """
    path_field = None
    file_field = None

    p = mapper.map("ref_audio_path")
    if p and mapper.sem_of(p) == "ref_audio_path":
        path_field = p
    else:
        # 有些引擎把路径字段声明在 ref_audio 组里（类型是 string）
        f = mapper.map("ref_audio")
        if f and not (
            isinstance(mapper.params.get(f), dict) and mapper.params[f].get("type") == "file"
        ):
            path_field = f

    f = mapper.map("ref_audio")
    if f and isinstance(mapper.params.get(f), dict) and mapper.params[f].get("type") == "file":
        file_field = f

    return path_field, file_field


def _sync_default_voice_id() -> None:
    """音色 ID 迁移后，把配置里的默认音色同步成新的唯一 ID。

    早期音色 ID 用文件名（可能重复、含中文），迁移后旧 ID 只留在 legacy_ids，
    这里把 hub.default_voice 里的历史 ID 换成当前唯一 ID，避免配置指向旧值。
    """
    voices.list_voices()  # 触发 ID 迁移（会落盘）
    configured = str(registry.hub().get("default_voice") or "").strip()
    if not configured:
        return
    entry, _path = voices.resolve(configured)
    if not entry:
        return
    current = str(entry.get("id") or "")
    if current and current != configured:
        try:
            registry.update_hub({"default_voice": current})
        except Exception:  # noqa: BLE001
            pass


def _inject_voice(
    body: Dict[str, Any],
    mapper: Any,
    ep_key: str,
    hub: Dict[str, Any],
    voice_id: str = "",
) -> Optional[Dict[str, Any]]:
    """克隆模式未传参考音频时，用指定音色（voice_id）或默认音色兜底。

    触发条件（全部满足）：
      1. 当前端点属于「克隆」语义（端点名含 clone，且不是 batch 批量端点）；
      2. 客户端没有提供任何参考音频（自己传了参考音频就以客户端的为准）；
      3. 目标音色存在且音频文件真实存在。

    音色来源优先级：
      1. 请求参数 `voice_id`（外部程序指定的音色，唯一 ID / 历史 ID / 文件名均可）；
      2. 管家配置 `hub.default_voice`（默认音色）。

    注入方式按引擎能力自动选择（路径与上传字段可以同时给）：
      · 引擎支持**本地路径**字段 → 直接给绝对路径（同机部署，最稳）；
      · 引擎只支持**上传文件**字段 → 把音色音频作为 multipart 文件带上。

    :return: 注入信息 {"voice": id, "fields": [...], "source": "request"|"default"}；未注入返回 None
    """
    ep = str(ep_key or "").lower()
    if "clone" not in ep or ep.startswith("batch"):
        return None
    if _has_ref_audio(body, mapper):
        return None

    want = str(voice_id or "").strip()
    source = "request"
    if not want:
        want = str((hub or {}).get("default_voice") or "").strip()
        source = "default"
    if not want:
        return None

    entry, path = voices.resolve(want)
    if not entry:
        if source == "request":
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "音色不存在",
                    "voice_id": want,
                    "hint": "先 GET /api/voices 取音色列表，用其中的 id 作为 voice_id 传入",
                },
            )
        return None
    if not path:
        if source == "request":
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "音色音频文件缺失，无法用于克隆",
                    "voice_id": want,
                    "file": str(entry.get("file") or ""),
                    "hint": "请在音色库中重新上传该音色的音频",
                },
            )
        return None

    d, slot = _body_dict(body)
    if slot == "":
        return None  # 原始二进制体，无法注入

    path_field, file_field = _pick_ref_fields(mapper)
    if not path_field and not file_field:
        return None

    used: List[str] = []

    if path_field:
        d[path_field] = str(path)
        used.append(path_field)

    if file_field:
        try:
            content = path.read_bytes()
        except OSError:
            content = None
        if content is not None:
            ctype = "audio/wav" if path.suffix.lower() == ".wav" else "application/octet-stream"
            # 文件名必须是 ASCII：中文名在 multipart 的 Content-Disposition 头里
            # 会触发 latin-1 编码错误（httpx 直接报错，请求根本发不出去）
            safe_name = "voice_ref" + (path.suffix if path.suffix.isascii() else ".wav")
            if "files" in body:
                body.setdefault("files", []).append((file_field, (safe_name, content, ctype)))
            else:
                # 客户端发的是 JSON → 改写成 multipart
                data = body.pop("json", {}) or {}
                body.clear()
                body["data"] = data
                body["files"] = [(file_field, (safe_name, content, ctype))]
            used.append(file_field)

    if not used:
        return None

    # 参考文字稿：音色若登记了文本，且客户端没给，一并带上
    # （入库时 ASR 转写的结果存在 voice.json 里，克隆时直接复用，
    #   引擎侧 Audio8 / Breeze / AuK 就不必再跑一遍 ASR）
    vtext = str(entry.get("text") or "").strip()
    text_field: Optional[str] = None
    if vtext:
        tfield = mapper.map("ref_text")
        if tfield and not any(
            (body.get(s) or {}).get(tfield) for s in ("json", "data") if isinstance(body.get(s), dict)
        ):
            d2 = body.get("json") if isinstance(body.get("json"), dict) else body.get("data")
            if isinstance(d2, dict):
                d2[tfield] = vtext
                text_field = tfield

    return {
        "voice": str(entry.get("id") or want),
        "fields": used,
        "source": source,
        "text_field": text_field,
        "text_source": str(entry.get("text_source") or ""),
    }


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

    # 端点键（合成入口用）
    ep_key = endpoint or str(cfg.get("default_endpoint") or "clone")

    _dbg(
        f"model={model} engine={name} ep_key={ep_key} target_path={target_path} "
        f"body_slots={list(body.keys())} ct={request.headers.get('content-type')} "
        f"trace={ {k: v for k, v in trace.items() if k != 'alias_notes'} } "
        f"notes={alias_notes}"
    )

    # ---- 参考音频兜底（音色 ID / 默认音色）--------------------------------
    # 外部程序调用【克隆模式】但没传参考音频时：
    #   1) 请求里带了 voice_id → 用该音色（外部程序指定的音色）；
    #   2) 否则用 hub.default_voice 指定的默认音色。
    # 音频在 voice/ 目录，与引擎同机，直接给绝对路径最稳。
    hub = registry.hub()
    voice_id = str(trace.get("voice_id") or request.query_params.get("voice_id") or "").strip()
    injected_default = None
    if target_path is None:
        injected_default = _inject_voice(body, mapper, ep_key, hub, voice_id)
    if injected_default:
        alias_notes.append(
            {
                "from": "voice_id" if injected_default.get("source") == "request" else "default_voice",
                "to": ",".join(injected_default.get("fields") or []),
                "how": "voice-id" if injected_default.get("source") == "request" else "default-voice",
            }
        )
    _dbg(f"inject_voice voice_id={voice_id!r} default_voice={hub.get('default_voice')!r} -> {injected_default}")
    if injected_default and injected_default.get("text_field"):
        # 复用了音色库里已转写好的参考文字稿（省掉引擎侧再跑一次 ASR）
        alias_notes.append(
            {
                "from": "voice-text",
                "to": str(injected_default.get("text_field")),
                "how": "voice-text",
            }
        )

    # strict=1：由 Hub 先把关必填，缺参直接 400，
    # 避免把注定失败的请求打到引擎上（尤其会白白触发一次冷启动/模型加载）。
    provided: List[str] = list((body.get("json") or body.get("data") or {}).keys())
    provided += [str(k) for k, _ in (body.get("files") or [])]
    native_provided = {mapper.map(k) for k in provided if mapper.map(k)}
    # 规范名集合：契约里既可以写原生名，也可以写规范名（如 prompt_text / ref_text）
    canon_provided = {mapper.sem_of(k) for k in provided if mapper.sem_of(k)}

    # 端点级必填契约（config 里显式声明的 endpoint_requires）：
    # 同一个引擎不同端点要的参数不一样，例如 Breeze-TTS-2 的 clone 必须给
    # 参考音频 + 参考文字稿(ref_text)，缺 ref_text 引擎会直接 400。
    # 这类是「引擎明确会拒绝」的参数，因此默认生效，不受 strict 开关影响。
    # 只对【合成入口】生效（target_path 为 None）；任务查询 / 音频下载等
    # 透传请求不带参数，不能被这条规则误伤。
    contract = (cfg.get("endpoint_requires") or {}).get(ep_key) or {}
    contract_missing: List[str] = []
    if target_path is None:
        for k in contract.get("required") or []:
            if k not in native_provided and k not in canon_provided and k not in contract_missing:
                contract_missing.append(k)
        for group in contract.get("any_of") or []:
            if not group:
                continue
            if not any(
                g in native_provided or g in canon_provided or mapper.sem_of(g) in canon_provided
                for g in group
            ):
                contract_missing.append(" 或 ".join(group))
    if contract_missing:
        _hint = "可用 GET /api/hub/engines/{name}/params 查看该引擎的必填字段与规范别名"
        if str(hub.get("default_voice") or "").strip():
            _hint += (
                f"；管家已配置默认音色「{hub.get('default_voice')}」，"
                "克隆时未传参考音频会自动用它兜底，其余必填项仍需调用方提供"
            )
        raise HTTPException(
            status_code=400,
            detail={
                "message": "缺少必填参数",
                "engine": name,
                "endpoint": ep_key,
                "missing": contract_missing,
                "hint": _hint,
            },
        )

    if _want_strict(request):
        missing = mapper.missing_required(provided)
        if missing:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "缺少必填参数",
                    "engine": name,
                    "endpoint": ep_key,
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
    total_max = int(hub.get("max_inflight_requests") or 0)
    per_max = int(hub.get("max_inflight_per_engine") or 0)
    wait = float(hub.get("queue_wait_timeout") or 0)
    if heavy:
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

    async def _send(target: EngineProcess):
        return await forward(
            target, request.method, path, params=query, headers=headers, body=body, timeout=timeout
        )

    inst.inflight += 1
    inst.touch()
    try:
        client, resp = await _send(inst)
    except Exception as exc:  # noqa: BLE001
        await _release(inst, slot)
        slot = None
        # 连接被拒 = 引擎进程多半已经崩了（显存/CUDA 原生崩溃最常见）。
        # 此时直接 502 会让调用方以为是自己参数的问题，而且引擎再也起不来；
        # 这里自动作废该引擎、重新拉起，再重试一次。
        if isinstance(exc, httpx.ConnectError):
            jr.annotate(rec, error=f"引擎连接失败，自动重启后重试: {exc}"[:300])
            try:
                await manager.invalidate(name, f"转发连接失败: {exc}")
                inst = await manager.ensure(name)
            except EngineError as exc2:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "message": f"引擎 {name} 崩溃后重启失败: {exc2}",
                        "engine": name,
                        "hint": f"引擎日志：{manager.tail_log(name, 30)}",
                    },
                )
            if heavy:
                if not await _GATE.acquire(name, total_max, per_max, wait):
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "message": "管家繁忙：并发已达上限，已拒绝",
                            "engine": name,
                        },
                        headers={"Retry-After": "2"},
                    )
                slot = name
            inst.inflight += 1
            inst.touch()
            try:
                client, resp = await _send(inst)
            except Exception as exc2:  # noqa: BLE001
                await _release(inst, slot)
                raise HTTPException(
                    status_code=502,
                    detail={
                        "message": f"转发到引擎 {name} 失败: {exc2}",
                        "engine": name,
                        "url": f"{inst.base_url}{path}",
                        "hint": f"引擎日志：{manager.tail_log(name, 30)}",
                    },
                )
        else:
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
    if injected_default:
        # 响应头只能放 latin-1，中文音色 id 必须转义，否则整条响应会挂
        _vid = quote(str(injected_default.get("voice") or ""), safe="")
        extra["X-Hub-Voice-Id"] = _vid
        extra["X-Hub-Voice-Source"] = str(injected_default.get("source") or "")
        extra["X-Hub-Default-Voice"] = _vid

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
    # 启动时把音色索引里的 ID 统一成唯一 ID（历史 ID 保留在 legacy_ids），
    # 并把配置里的默认音色从历史 ID 迁移到新的唯一 ID。
    try:
        _sync_default_voice_id()
    except Exception:  # noqa: BLE001
        pass
    # 启动即对账：hub 一旦重启，内存里的活跃表就空了，但上一代拉起的引擎
    # 仍在端口上跑着占显存。这里按系统端口表把它们接管回来（或按策略结束）。
    try:
        report = await manager.reconcile()
        if report.get("orphan_count") or report.get("killed"):
            print(
                f"[hub] 启动对账：发现孤儿引擎 {report['orphan_count']} 个，"
                f"接管 {report['adopted']}，结束 {report['killed']}，"
                f"清理幽灵 {report['dropped']}"
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[hub] 启动对账失败: {exc}")
    try:
        yield
    finally:
        reaper.cancel()
        if registry.hub().get("stop_engines_on_exit", True):
            await manager.stop_all(
                include_orphans=bool(registry.hub().get("cleanup_orphans_on_exit", True))
            )


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
    item["log_path"] = inst.log_path if inst else str(ROOT / str(registry.hub().get("log_dir") or "logs") / f"{name}.log")
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
        "endpoint_requires": cfg.get("endpoint_requires") or {},
        "default_endpoint": cfg.get("default_endpoint") or "clone",
        "current_defaults": cfg.get("defaults") or {},
        "canonical": mapper.canonical_map(),
        "required": required,
        "count": len(params),
        "params": params,
        # 管家级参数：不发给引擎，由管家消费
        "hub_params": {
            "voice_id": {
                "type": "string",
                "required": False,
                "description": (
                    "指定参考音频音色（仅克隆类端点生效）。传音色的唯一 ID，"
                    "可用 GET /api/voices 查询列表；不传则使用管家配置的默认音色。"
                ),
            }
        },
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


# ---------------------------------------------------------------------------
# 项目音色库（voice/ 目录 + voice.json 索引）
# ---------------------------------------------------------------------------
@app.get("/api/hub/voices", summary="列出项目预置音色")
async def list_voices():
    return {"voices": voices.list_voices()}


@app.get("/api/hub/voices/{voice_id}/audio", summary="取音色音频文件")
async def voice_audio(voice_id: str):
    entry, path = voices.resolve(voice_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"音色不存在：{voice_id}")
    if not path:
        raise HTTPException(status_code=404, detail=f"音色音频缺失：{entry.get('file')}")
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


# ---------------------------------------------------------------------------
# 对外开放的音色接口（供外部程序调用）
#   GET /api/voices             取音色列表（含唯一音色 ID）
#   GET /api/voices/{id}/audio  取音色音频
# 外部在克隆请求里带上 voice_id=<音色ID> 即可指定参考音频音色
# ---------------------------------------------------------------------------
@app.get("/api/voices", summary="【对外】音色列表：含唯一音色 ID，可直接用于克隆请求的 voice_id")
async def public_voice_list():
    items = []
    default_id = str(registry.hub().get("default_voice") or "").strip()
    default_effective = ""
    if default_id:
        e, _p = voices.resolve(default_id)
        if e is not None:
            default_effective = str(e.get("id") or "")
    for v in voices.list_voices():
        items.append(
            {
                "id": str(v.get("id") or ""),
                "name": str(v.get("name") or ""),
                "gender": str(v.get("gender") or ""),
                "age": str(v.get("age") or ""),
                "has_text": bool(str(v.get("text") or "").strip()),
                "size": int(v.get("size") or 0),
                "exists": bool(v.get("exists")),
                "is_default": bool(default_effective) and str(v.get("id")) == default_effective,
                "audio_url": f"/api/voices/{v.get('id')}/audio",
            }
        )
    return {
        "count": len(items),
        "default_voice": default_effective,
        "voices": items,
        "usage": {
            "clone": "POST /api/tts/clone?model=<引擎>  body: {\"text\": \"...\", \"voice_id\": \"<音色ID>\"}",
            "note": "voice_id 为空时，管家使用已配置的默认音色；仍未配置则按引擎原样报错",
        },
    }


@app.get("/api/voices/{voice_id}/audio", summary="【对外】取音色音频文件")
async def public_voice_audio(voice_id: str):
    entry, path = voices.resolve(voice_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"音色不存在：{voice_id}")
    if not path:
        raise HTTPException(status_code=404, detail=f"音色音频缺失：{entry.get('file')}")
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@app.get("/api/hub/voices/default", summary="查询默认音色（克隆兜底用）")
async def get_default_voice():
    configured = str(registry.hub().get("default_voice") or "").strip()
    entry = None
    effective = ""
    if configured:
        entry, path = voices.resolve(configured)
        if entry is not None:
            effective = str(entry.get("id") or configured)
            if path is None:
                entry = dict(entry)
                entry["exists"] = False
    return {"default_voice": effective, "configured": configured, "voice": entry}


@app.put("/api/hub/voices/default", summary="设置默认音色")
async def set_default_voice(payload: Dict[str, Any] = Body(...)):
    vid = str(payload.get("voice_id") or payload.get("id") or "").strip()
    if not vid:
        # 空 = 清除默认音色
        hub = registry.update_hub({"default_voice": ""})
        return {"ok": True, "default_voice": "", "hub": hub}
    entry, path = voices.resolve(vid)
    if not entry:
        raise HTTPException(status_code=404, detail=f"音色不存在：{vid}")
    if not path:
        raise HTTPException(status_code=400, detail=f"音色音频缺失，无法设为默认：{entry.get('file')}")
    hub = registry.update_hub({"default_voice": str(entry.get("id") or vid)})
    return {"ok": True, "default_voice": str(entry.get("id") or vid), "voice": entry, "hub": hub}


@app.put("/api/hub/voices/{voice_id}", summary="修改音色属性")
async def update_voice(voice_id: str, patch: Dict[str, Any] = Body(...)):
    entry = voices.update_voice(
        voice_id,
        name=patch.get("name"),
        gender=patch.get("gender"),
        age=patch.get("age"),
        text=patch.get("text"),
    )
    if not entry:
        raise HTTPException(status_code=404, detail=f"音色不存在：{voice_id}")
    return {"ok": True, "voice": entry, "voices": voices.list_voices()}


@app.delete("/api/hub/voices/{voice_id}", summary="删除音色")
async def delete_voice(voice_id: str, remove_file: bool = Query(True)):
    entry = voices.delete_voice(voice_id, remove_file=remove_file)
    if not entry:
        raise HTTPException(status_code=404, detail=f"音色不存在：{voice_id}")
    return {"ok": True, "removed": entry, "voices": voices.list_voices()}


# ---------------------------------------------------------------------------
# 音色入库 ASR 转写
# ---------------------------------------------------------------------------
def _asr_settings() -> Dict[str, Any]:
    """管家级 ASR 配置（hub.asr_*）。"""
    hub = registry.hub()
    return {
        "enabled": bool(hub.get("asr_enabled", True)),
        "backend": str(hub.get("asr_backend") or ""),
        "model": str(hub.get("asr_model") or ""),
        "device": str(hub.get("asr_device") or ""),
        "timeout": float(hub.get("asr_timeout") or 300),
    }


async def _run_asr(
    path: Path,
    *,
    backend: str = "",
    model: str = "",
    device: str = "",
    timeout: float = 0.0,
    check_enabled: bool = True,
) -> Dict[str, Any]:
    """转写音频，返回 {ok, text, error, elapsed}（失败不抛异常，降级为空文本）。

    backend/model/device/timeout 留空时取管家配置；显式传入则覆盖（对外 /api/asr 用）。
    check_enabled=False 时忽略 hub.asr_enabled —— 显式调用不应被「入库自动转写」开关挡住。
    """
    cfg = _asr_settings()
    if check_enabled and not cfg["enabled"]:
        return {
            "ok": False,
            "text": "",
            "error": "ASR 已在管家配置里关闭（hub.asr_enabled=0）",
            "elapsed": 0.0,
        }
    t0 = time.time()
    try:
        # 子进程模式：transcribe 内部用 subprocess.run(timeout=...) 自带超时，且返回
        # {ok,text,error} 字典。不要再套 asyncio.wait_for —— 它只会取消协程、杀不掉
        # 已在跑的子进程线程，反而会留下孤儿进程长期占着 ASR 单例锁。
        info = await asyncio.to_thread(
            get_asr().transcribe,
            path,
            backend=backend or cfg["backend"],
            model=model or cfg["model"],
            device=device or cfg["device"],
            timeout=float(timeout) or cfg["timeout"],
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "text": "",
            "error": f"ASR 转写失败：{exc}",
            "elapsed": round(time.time() - t0, 2),
        }
    info["elapsed"] = round(time.time() - t0, 2)
    return info


async def _transcribe_voice(path: Path) -> Dict[str, Any]:
    """音色入库转写：沿用管家配置，且受 hub.asr_enabled 控制。"""
    return await _run_asr(path)


@app.post("/api/hub/voices", summary="上传音频并登记为项目音色")
async def create_voice(
    file: UploadFile = File(..., description="音频文件"),
    name: str = Form("", description="音色名称"),
    gender: str = Form("", description="性别：female / male / neutral"),
    age: str = Form("", description="年龄：child / teen / young / middle / old"),
    text: str = Form("", description="参考文字稿；留空且 auto_transcribe=1 时由 ASR 自动转写"),
    auto_transcribe: str = Form("1", description="1=未提供文字稿时自动调用 ASR 转写"),
):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in voices.AUDIO_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"不支持的音频格式：{suffix or '未知'}")
    VOICE_TMP.mkdir(parents=True, exist_ok=True)
    tmp = VOICE_TMP / f"upload_{int(time.time() * 1000)}{suffix}"
    try:
        with tmp.open("wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        if tmp.stat().st_size == 0:
            raise HTTPException(status_code=400, detail="上传的音频为空")
        entry = voices.add_voice(
            src=tmp,
            filename=file.filename or f"voice{suffix}",
            name=name,
            gender=gender,
            age=age,
        )
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass

    # ---- 参考文字稿：优先用调用方给的，否则 ASR 自动转写一次并入库 ----
    # 转写结果写进 voice.json，之后克隆直接复用，引擎侧不必再跑一遍 ASR。
    final_text = str(text or "").strip()
    asr_info: Dict[str, Any] = {"ok": None, "text": "", "error": "", "elapsed": 0.0, "skipped": True}
    if final_text:
        entry = voices.update_voice(entry["id"], text=final_text, text_source="manual")
    elif str(auto_transcribe or "").strip().lower() in ("1", "true", "yes", "on"):
        asr_info = await _transcribe_voice(voices.VOICE_DIR / str(entry.get("file") or ""))
        asr_info["skipped"] = False
        if asr_info.get("ok"):
            final_text = str(asr_info.get("text") or "")
            entry = voices.update_voice(entry["id"], text=final_text, text_source="asr")
    _dbg(f"create_voice id={entry.get('id')} text_source={entry.get('text_source')} asr={asr_info}")
    return {"ok": True, "voice": entry, "voices": voices.list_voices(), "asr": asr_info}


@app.post("/api/hub/voices/{voice_id}/transcribe", summary="用 ASR 转写音色音频并更新参考文字稿")
async def transcribe_voice(voice_id: str, overwrite: bool = Query(True)):
    entry, path = voices.resolve(voice_id)
    if not entry or not path:
        raise HTTPException(status_code=404, detail=f"音色不存在或音频缺失：{voice_id}")
    has_text = bool(str(entry.get("text") or "").strip())
    if has_text and not overwrite:
        return {
            "ok": True,
            "voice": entry,
            "voices": voices.list_voices(),
            "asr": {"ok": True, "text": entry.get("text"), "error": "", "elapsed": 0.0, "skipped": True},
        }
    info = await _transcribe_voice(path)
    if info.get("ok"):
        entry = voices.update_voice(entry["id"], text=str(info.get("text") or ""), text_source="asr")
    return {"ok": bool(info.get("ok")), "voice": entry, "voices": voices.list_voices(), "asr": info}


@app.get("/api/hub/asr/status", summary="管家 ASR 状态")
async def asr_status():
    cfg = _asr_settings()
    return {"ok": True, "settings": cfg, "status": get_asr().status()}


@app.post("/api/hub/asr/unload", summary="卸载管家 ASR 模型（子进程模式无需卸载）")
async def asr_unload():
    # ASR 以独立子进程运行，每次转写完自动退出、不常驻显存，无需手动卸载。
    # 保留此端点仅为兼容旧前端调用。
    return {"ok": True, "status": get_asr().status(), "note": "ASR 以独立子进程运行，转写结束即退出，无需卸载"}


@app.post("/api/asr", summary="【对外】语音转写：上传音频或传服务端路径，返回文字")
async def public_asr(request: Request):
    """把任意音频转写成文字（独立于音色库，供外部程序/智能体直接调用）。

    两种传参（二选一）：

    1. multipart 上传：``file=@a.wav``，可选表单字段 ``backend/model/device/timeout``；
    2. JSON 体：``{"path": "/abs/or/relative.wav", "backend": "", "model": "",
       "device": "", "timeout": 0}``，``path`` 也接受键 ``audio`` / ``audio_path``；
       相对路径按项目根解析。

    ``backend/model/device/timeout`` 留空则取管家 ASR 配置（默认 sensevoice，CPU）。
    返回 ``{ok, text, error, elapsed, backend, model, device}``；失败不抛 5xx，
    而是 ``ok=false`` + ``error``，便于调用方统一处理。
    """
    ctype = (request.headers.get("content-type") or "").lower()
    backend = model = device = ""
    timeout = 0.0
    audio_path = ""
    upload = None
    try:
        if ctype.startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("file")
            backend = str(form.get("backend") or "")
            model = str(form.get("model") or "")
            device = str(form.get("device") or "")
            audio_path = str(form.get("path") or form.get("audio_path") or "")
            timeout = float(form.get("timeout") or 0)
        else:
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                raise HTTPException(status_code=400, detail="请求体不是合法 JSON")
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="JSON 请求体必须是对象")
            backend = str(body.get("backend") or "")
            model = str(body.get("model") or "")
            device = str(body.get("device") or "")
            audio_path = str(body.get("path") or body.get("audio") or body.get("audio_path") or "")
            timeout = float(body.get("timeout") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"参数解析失败：{exc}")

    tmp: Optional[Path] = None
    try:
        if upload is not None and hasattr(upload, "filename"):
            suffix = Path(upload.filename or "").suffix.lower()
            if suffix not in voices.AUDIO_SUFFIXES:
                raise HTTPException(status_code=400, detail=f"不支持的音频格式：{suffix or '未知'}")
            VOICE_TMP.mkdir(parents=True, exist_ok=True)
            tmp = VOICE_TMP / f"asr_{int(time.time() * 1000)}{suffix}"
            with tmp.open("wb") as fh:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
            if tmp.stat().st_size == 0:
                raise HTTPException(status_code=400, detail="上传的音频为空")
            target = tmp
        else:
            if not audio_path.strip():
                raise HTTPException(status_code=400, detail="缺少音频：请用 file 上传，或传 path/audio 字段")
            p = Path(audio_path).expanduser()
            if not p.is_absolute():
                p = (ROOT / p).resolve()
            if not p.is_file():
                raise HTTPException(status_code=404, detail=f"音频文件不存在：{p}")
            target = p

        info = await _run_asr(
            target, backend=backend, model=model, device=device, timeout=timeout, check_enabled=False
        )
        last = (get_asr().status().get("last") or {})
        return {
            "ok": bool(info.get("ok")),
            "text": str(info.get("text") or ""),
            "error": str(info.get("error") or ""),
            "elapsed": info.get("elapsed", 0.0),
            "backend": str(last.get("backend") or ""),
            "model": str(last.get("model") or ""),
            "device": str(last.get("device") or ""),
        }
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass


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
    mgr = registry.manager()
    return {
        "hub": {"pid": os.getpid(), "root": str(ROOT), "port": hub.get("port")},
        # 统一管理器（守护进程）的监听地址，供面板调用其控制 API 启停 / 重启后端
        "manager": {
            "host": mgr.get("host") or "0.0.0.0",
            "port": int(mgr.get("port") or 5290),
        },
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


@app.post("/api/hub/reconcile", summary="状态对账：找回/清理端口上的引擎孤儿进程")
async def reconcile_engines(
    kill: bool = Query(False, description="true = 直接结束所有非托管引擎进程，立即释放显存"),
):
    """以系统端口表为准重建引擎状态。

    hub 重启后活跃表为空、引擎却还在跑（占显存）时，可用它立即对账；
    总控台「状态对账 → 清理孤儿进程」走 kill=true，会结束所有引擎端口上的非托管进程。
    """
    try:
        report = await (manager.kill_orphans() if kill else manager.reconcile())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True, "report": report, **manager.status()}


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
# Skill 安装信息（前端「Skill 安装」页用：路径必须是后端给的绝对路径）
# ---------------------------------------------------------------------------
_SKILL_DIR_NAME = "tts-agent-skill"
_SKILL_ID = "tts-hub-api"


def _read_skill_frontmatter(skill_md: Path) -> Dict[str, str]:
    """读 SKILL.md 的 YAML frontmatter（只要 name / description 两行，够用即可）。"""
    meta: Dict[str, str] = {}
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return meta
    if not text.startswith("---"):
        return meta
    for line in text.splitlines()[1:]:
        if line.strip() == "---":
            break
        if line[:1] in (" ", "\t", "#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta


@app.get("/api/hub/skill", summary="Skill 安装信息（绝对路径 / 元数据 / 安装状态）")
async def get_skill_info():
    skill_dir = (ROOT / _SKILL_DIR_NAME).resolve()
    skill_md = skill_dir / "SKILL.md"
    script = skill_dir / "scripts" / "tts_agent.py"
    meta = _read_skill_frontmatter(skill_md) if skill_md.is_file() else {}

    home = Path.home()
    candidates = [
        home / ".codebuddy" / "skills" / _SKILL_ID,
        ROOT / ".codebuddy" / "skills" / _SKILL_ID,
    ]
    installed = next((str(c) for c in candidates if c.exists()), None)

    refs = skill_dir / "references"
    references = (
        [p.name for p in sorted(refs.glob("*.md"))] if refs.is_dir() else []
    )

    return {
        "ok": True,
        "id": meta.get("name") or _SKILL_ID,
        "name": meta.get("name") or _SKILL_ID,
        "description": meta.get("description", ""),
        "exists": skill_dir.is_dir(),
        "root": str(ROOT),
        "skill_dir": str(skill_dir),
        "skill_md": str(skill_md),
        "script": str(script),
        "references": references,
        "installed_path": installed,
        "install_targets": [str(c) for c in candidates],
        "python": str(ROOT / "py312env" / "python.exe"),
        "hub_port": int(registry.hub().get("port") or 5190),
    }


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
            # 必须走 stop_all 而不是 unload_current：后者只关「当前」引擎，
            # 状态丢失后 _active 可能为空，遗留的引擎会全部留在后台占显存。
            stopped = await manager.stop_all(
                include_orphans=bool(registry.hub().get("cleanup_orphans_on_exit", True))
            )
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
