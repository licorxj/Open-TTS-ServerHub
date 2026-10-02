"""TTS API 管家 · 配置注册表

职责：
    1. 加载 `config/tts_hub.yaml`（引擎注册表，人工维护，带注释）
    2. 叠加 `config/tts_hub.overrides.yaml`（运行期通过 API 修改的覆盖层，管家写）
    3. 展开 `{root}` / `{port}` / `{name}` 占位符
    4. 提供模型名 → 引擎的解析（支持 name / alias，大小写不敏感）

设计要点：运行期修改**不回写**主注册表，只写覆盖层，避免破坏注释与结构。
"""

from __future__ import annotations

import copy
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .aliases import AliasMapper

# 仓库根目录：tts_hub/ 的上一级
ROOT = Path(os.environ.get("TTS_HUB_ROOT") or Path(__file__).resolve().parents[1]).resolve()

CONFIG_DIR = ROOT / "config"
REGISTRY_FILE = CONFIG_DIR / "tts_hub.yaml"
OVERRIDES_FILE = CONFIG_DIR / "tts_hub.overrides.yaml"

# 允许通过 API 修改的引擎字段（`params` 属于接口能力声明，不开放运行期修改）
MUTABLE_ENGINE_KEYS = {
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
}

# 允许通过 API 修改的管家级字段
MUTABLE_HUB_KEYS = {
    "max_active",
    "idle_ttl_seconds",
    "idle_check_interval",
    "unload_on_switch",
    "stop_engines_on_exit",
    "forward_timeout",
    "expose_engine_header",
    "adopt_existing",
    "allow_shutdown_api",
    # ---- 高并发保护（外部大量请求时防止任务/内存无限堆积）----
    "max_inflight_requests",
    "max_inflight_per_engine",
    "queue_wait_timeout",
    "inflight_stale_seconds",
    "limit_concurrency",
    # ---- 观测数据与日志上限 ----
    "journal_max_requests",
    "journal_max_tasks",
    "log_max_bytes",
    "log_backups",
}

DEFAULT_HUB = {
    "host": "0.0.0.0",
    "port": 5199,
    "max_active": 1,
    # 空闲自动卸载：默认 5 分钟（0 = 关闭）
    "idle_ttl_seconds": 300,
    "idle_check_interval": 15,
    # false = 按 max_active 配额并存，只有超上限才淘汰（默认）
    # true  = 每次切换都强制独占，无视 max_active>1
    "unload_on_switch": False,
    "stop_engines_on_exit": True,
    "forward_timeout": 0,
    "log_dir": "logs/tts_hub",
    "expose_engine_header": True,
    # true = 端口上已有同名服务时直接接管（不再自己拉起，也不能卸载它）
    "adopt_existing": False,
    "allow_shutdown_api": True,
    # ------------------------------------------------------------------
    # 高并发保护：外部程序批量打请求时，管家必须"快速拒绝"而不是"无限堆积"。
    # 合成请求动辄数秒~数十秒，若不限流，成百个请求会同时挂着请求体、
    # 上游连接与响应流，内存线性上涨直至进程被系统杀掉。
    # ------------------------------------------------------------------
    # 管家全局同时在途的「重请求」上限（合成 / 非 GET 透传）。
    # 超限时立即返回 503 + Retry-After，让调用方自行退避重试。
    # 0 = 不限（不推荐，等同旧行为）
    "max_inflight_requests": 8,
    # 单个引擎同时在途的重请求上限（引擎本身多为串行推理，设大了只会堆积）
    "max_inflight_per_engine": 4,
    # 槽位等待上限（秒）。排队超过该时长直接 503，避免请求无意义地挂着。
    # 0 = 无限等待
    "queue_wait_timeout": 120,
    # 在途计数被认为「已泄漏」的时长（秒）。
    # 客户端中途断开等异常会让 inflight 减不回去，导致引擎永远判定为忙碌、
    # 显存无法回收；超过该时长则强制清零并打印告警。0 = 不做兜底
    "inflight_stale_seconds": 600,
    # ASGI 层最大并发连接数（uvicorn limit_concurrency），超限由 uvicorn 直接 503
    "limit_concurrency": 200,
    # ------------------------------------------------------------------
    # 观测数据与日志上限：管家是常驻调度器，观测数据只保留最近一小段
    # ------------------------------------------------------------------
    # 内存请求日志保留条数（环形缓冲，超出自动淘汰最旧的）
    "journal_max_requests": 2000,
    # 任务台账保留条数
    "journal_max_tasks": 1000,
    # 引擎子进程日志单文件上限（字节），超出自动轮转；0 = 不轮转
    "log_max_bytes": 20 * 1024 * 1024,
    # 轮转保留的历史文件个数
    "log_backups": 2,
}


def deep_merge(base: Any, patch: Any) -> Any:
    """把 patch 深度合并进 base（base 不被修改，返回新对象）。"""
    if isinstance(base, dict) and isinstance(patch, dict):
        out = dict(base)
        for k, v in patch.items():
            out[k] = deep_merge(out.get(k), v) if k in out else copy.deepcopy(v)
        return out
    return copy.deepcopy(patch)


def _expand(value: Any, mapping: Dict[str, str]) -> Any:
    """递归展开字符串里的占位符。"""
    if isinstance(value, str):
        out = value
        for k, v in mapping.items():
            if v is not None:
                out = out.replace("{%s}" % k, str(v))
        return out
    if isinstance(value, dict):
        return {k: _expand(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, mapping) for v in value]
    return value


class Registry:
    """注册表单例：带 mtime 自动热重载。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._mtime = (0.0, 0.0)
        self._data: Dict[str, Any] = {}
        self.reload()

    # ------------------------------------------------------------------ 加载
    def _read_yaml(self, path: Path) -> Dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            with path.open("r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"配置文件解析失败: {path} -> {exc}") from exc

    def reload(self) -> None:
        with self._lock:
            raw = self._read_yaml(REGISTRY_FILE)
            overrides = self._read_yaml(OVERRIDES_FILE)
            data = deep_merge(raw, overrides)

            data.setdefault("hub", {})
            data["hub"] = deep_merge(DEFAULT_HUB, data.get("hub") or {})
            data.setdefault("runtime", {})
            data.setdefault("engines", {})

            data["_overrides"] = copy.deepcopy(overrides)
            data["_overrides_path"] = str(OVERRIDES_FILE)
            self._data = data
            self._mtime = (
                REGISTRY_FILE.stat().st_mtime if REGISTRY_FILE.is_file() else 0.0,
                OVERRIDES_FILE.stat().st_mtime if OVERRIDES_FILE.is_file() else 0.0,
            )

    def maybe_reload(self) -> None:
        """若配置文件在磁盘上被改动则自动热重载。"""
        cur = (
            REGISTRY_FILE.stat().st_mtime if REGISTRY_FILE.is_file() else 0.0,
            OVERRIDES_FILE.stat().st_mtime if OVERRIDES_FILE.is_file() else 0.0,
        )
        if cur != self._mtime:
            self.reload()

    # ------------------------------------------------------------------ 读取
    @property
    def data(self) -> Dict[str, Any]:
        self.maybe_reload()
        return self._data

    def hub(self) -> Dict[str, Any]:
        return _expand(self.data.get("hub") or {}, {"root": str(ROOT)})

    def runtime(self) -> Dict[str, Any]:
        return _expand(self.data.get("runtime") or {}, {"root": str(ROOT)})

    def engine_names(self) -> List[str]:
        return list((self.data.get("engines") or {}).keys())

    # ------------------------------------------------------- 参数别名归一化
    def alias_mapper(self, name: str) -> AliasMapper:
        """构造该引擎的参数名映射器。

        入参时用 `mapper.normalize(payload)` 把规范名翻译成引擎原生名；
        出参时用 `mapper.canonical_view()` 生成带规范别名的参数表。
        """
        cfg = self.engine(name) or {}
        return AliasMapper(cfg.get("params") or {}, cfg.get("params_alias") or {})

    def params_view(self, name: str) -> Dict[str, Any]:
        """带规范别名的参数表，供 GET /api/hub/engines/{name}/params 返回。"""
        return self.alias_mapper(name).canonical_view()

    def engine(self, name: str) -> Optional[Dict[str, Any]]:
        engines = self.data.get("engines") or {}
        if name in engines:
            return self._materialize(name, engines[name])
        # 大小写不敏感兜底
        for k, v in engines.items():
            if k.lower() == str(name).lower():
                return self._materialize(k, v)
        return None

    def resolve(self, model: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        """按 name 或 alias 解析引擎，返回 (引擎 key, 引擎配置)。"""
        if not model:
            return None
        engines = self.data.get("engines") or {}
        target = str(model).strip().lower()
        for key, cfg in engines.items():
            names = {str(key).lower(), str(cfg.get("display_name") or "").lower()}
            names |= {str(a).lower() for a in (cfg.get("aliases") or [])}
            names.discard("")
            if target in names:
                return key, self._materialize(key, cfg)
        return None

    # -------------------------------------------------------------- 占位符展开
    def _materialize(self, name: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
        cfg = copy.deepcopy(cfg)
        cfg["name"] = name
        port = ((cfg.get("server") or {}).get("port")) or 0

        mapping_all = {"root": str(ROOT), "name": name}
        cfg = _expand(cfg, mapping_all)

        # {port} / {host} 只在 runtime 段展开（endpoints 里还有 {task_id} 需保留原样）
        rt = cfg.get("runtime") or {}
        mapping_rt = dict(mapping_all)
        mapping_rt["port"] = str(port)
        mapping_rt["host"] = str((cfg.get("server") or {}).get("host") or "127.0.0.1")
        cfg["runtime"] = _expand(rt, mapping_rt)
        return cfg

    # ------------------------------------------------------------ 运行期修改
    def _persist(self, overrides: Dict[str, Any]) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(overrides, allow_unicode=True, sort_keys=False, default_flow_style=False)
        header = (
            "# TTS API 管家 · 运行期配置覆盖层（由管家自动生成，勿手工改结构）\n"
            "# 修改方式：PUT /api/hub/engines/{name}/config  或  PUT /api/hub/config\n"
            "# 删除本文件即恢复 config/tts_hub.yaml 的原始配置\n"
        )
        fd, tmp = tempfile.mkstemp(dir=str(CONFIG_DIR), suffix=".yaml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(header + text)
            os.replace(tmp, OVERRIDES_FILE)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        self.reload()

    def update_engine(self, name: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        """合并修改某引擎配置并持久化。只接受白名单字段。"""
        if name not in (self.data.get("engines") or {}):
            raise KeyError(f"未注册的引擎: {name}")
        bad = set(patch) - MUTABLE_ENGINE_KEYS
        if bad:
            raise ValueError(f"不允许修改的字段: {sorted(bad)}")

        overrides = copy.deepcopy(self._data.get("_overrides") or {})
        engines_ov = overrides.setdefault("engines", {})
        cur = engines_ov.get(name) or {}
        engines_ov[name] = deep_merge(cur, patch)
        self._persist(overrides)
        return self.engine(name)  # type: ignore[return-value]

    def reset_engine(self, name: str) -> Dict[str, Any]:
        """清空某引擎的运行期覆盖，恢复主注册表配置。"""
        overrides = copy.deepcopy(self._data.get("_overrides") or {})
        engines_ov = overrides.get("engines") or {}
        if name in engines_ov:
            del engines_ov[name]
            if not engines_ov:
                overrides.pop("engines", None)
            self._persist(overrides)
        return self.engine(name)  # type: ignore[return-value]

    def update_hub(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        bad = set(patch) - MUTABLE_HUB_KEYS
        if bad:
            raise ValueError(f"不允许修改的字段: {sorted(bad)}")
        overrides = copy.deepcopy(self._data.get("_overrides") or {})
        overrides["hub"] = deep_merge(overrides.get("hub") or {}, patch)
        self._persist(overrides)
        return self.hub()

    def overrides(self) -> Dict[str, Any]:
        return copy.deepcopy(self._data.get("_overrides") or {})


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------
_REGISTRY: Optional[Registry] = None


def get_registry() -> Registry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = Registry()
    return _REGISTRY


def now() -> float:
    return time.time()
