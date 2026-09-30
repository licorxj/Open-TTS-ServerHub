"""参数别名归一化层。

**为什么需要它**：各 TTS 子引擎对同一个概念的字段名各不相同 ——

    文本        text / input_text / instruction(AuK)
    参考音频    ref_audio_path / speaker_audio_path(IndexTTS) / spk_audio_path(Confucius4)
    参考原文    prompt_text / ref_text / ref_text_en
    音色指令    instruct / instruction(Breeze)

调用方（客户端）只知道一组**约定俗成的规范名**，不该为每个引擎改代码。
所以 Hub 在统一入口层做双向处理：

    入参方向 —— 客户端发规范名（或任意异名）→ Hub 翻译成该引擎的原生名再转发；
                引擎不支持该语义时丢弃，而不是把未知字段塞给引擎。
    出参方向 —— GET /params 的返回里额外暴露规范名字段（标记 alias_of），
                让客户端按统一名字探测能力、分发字段。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 语义组定义
#   canonical —— 主规范名（组标识）
#   expose    —— 需要在 /params 中对外暴露的规范名（调用方按这些名字探测能力）
#   pool      —— 该语义的**所有**已知写法：各引擎原生名 + 常见异名，用于入参归一化
#
# ⚠️ 维护约定：同一 key 只能出现在一个组里，否则归一化结果会因遍历顺序而不确定。
# ---------------------------------------------------------------------------
SEMANTIC_GROUPS: List[Dict[str, Any]] = [
    {
        "canonical": "text",
        "expose": ("text", "input_text"),
        "pool": ("text", "input_text", "gen_text", "sentence", "sentences", "ssml", "content"),
    },
    {
        "canonical": "ref_audio_path",
        # 路径语义：同一引擎若同时声明了路径字段和上传字段，优先用**路径字符串**
        # （audio8 二者都有；Hub 与项目同机部署，按 §3.1 本地路径优先）
        "prefer_type": "string",
        "expose": (
            "ref_audio_path",
            "speaker_audio_path",
            "spk_audio_path",
            "reference_audio",
            "reference_audio_file",
        ),
        "pool": (
            "ref_audio_path",
            "speaker_audio_path",
            "spk_audio_path",
            "reference_audio",
            "reference_audio_file",
            "prompt_audio_path",
            "ref_wav_path",
            "reference_wav_path",
            "clone_audio_path",
            "spk_ref",
        ),
    },
    {
        "canonical": "ref_audio",
        # 上传语义反过来：优先挑 file 类型的字段
        "prefer_type": "file",
        "expose": ("ref_audio", "speaker_audio", "spk_audio", "audio"),
        "pool": (
            "ref_audio",
            "speaker_audio",
            "spk_audio",
            "audio",
            "prompt_audio",
            "reference_wav",
            "clone_audio",
            "spk_wav",
            "ref_wav",
            "ref_audio_file",
        ),
    },
    {
        "canonical": "ref_text",
        "expose": ("prompt_text", "ref_text", "ref_text_en"),
        "pool": (
            "prompt_text",
            "ref_text",
            "ref_text_en",
            "ref_text_zh",
            "transcript",
            "asr_text",
            "prompt_transcript",
            "reference_text",
            "subtitle",
        ),
    },
    {
        "canonical": "speed",
        "expose": ("speed",),
        "pool": ("speed", "speed_factor", "speed_rate", "rate", "rate_scale"),
    },
    {
        "canonical": "instruct",
        "expose": ("instruct",),
        "pool": (
            "instruct",
            "instruction",
            "voice_instruct",
            "style_instruct",
            "emo_instruct",
            "emo_prompt",
            "style_prompt",
        ),
    },
    {
        "canonical": "emo_control_method",
        "expose": ("emo_control_method",),
        "pool": ("emo_control_method", "emotion_control_method", "emo_mode", "emotion_mode"),
    },
    {
        "canonical": "emo_vector",
        "expose": ("emo_vector",),
        "pool": ("emo_vector", "emotion_vector", "emo_weights", "emo_weight"),
    },
    {
        "canonical": "output_path",
        "expose": ("output_path",),
        # 刻意不含 audio_path：那是 §5 的**响应**字段名，放在一起会让入参/出参混淆
        "pool": ("output_path", "save_path", "output_file", "wav_path", "out_path"),
    },
    {
        "canonical": "language",
        "expose": ("language",),
        "pool": ("language", "lang", "language_id", "locale"),
    },
    {
        "canonical": "seed",
        "expose": ("seed",),
        "pool": ("seed", "random_seed", "noise_seed"),
    },
]

# key → 所属语义组（canonical）
_KEY_TO_GROUP: Dict[str, str] = {}
_GROUP_INDEX: Dict[str, Dict[str, Any]] = {}

for _g in SEMANTIC_GROUPS:
    _GROUP_INDEX[_g["canonical"]] = _g
    for _k in (*_g["pool"], *_g["expose"]):
        _KEY_TO_GROUP.setdefault(_k, _g["canonical"])


def group_of(field: str) -> Optional[str]:
    """某字段名属于哪个语义组；不属于任何已知语义则返回 None。"""
    return _KEY_TO_GROUP.get(str(field))


def _canon(value: Any) -> Optional[str]:
    """把显式声明的语义名归一到 canonical（防止 yaml 里写成同义字）。"""
    key = str(value)
    return _KEY_TO_GROUP.get(key, key)


class AliasMapper:
    """针对单个引擎的参数名映射器。

    :param params:   该引擎的原生参数表（registry 中的 engine.params）
    :param explicit: 显式语义声明 {原生名: 规范名}，来自 yaml 的 `params_alias`。
                     用于消歧 —— 同一个原生名在不同引擎里可能承担不同语义，例如
                     `instruction` 在 AuK 是**正文**、在 Breeze 是**音色指令**。
    """

    def __init__(
        self,
        params: Optional[Dict[str, Any]] = None,
        explicit: Optional[Dict[str, str]] = None,
    ):
        self.params: Dict[str, Any] = params or {}
        self.explicit: Dict[str, str] = {str(k): _canon(v) for k, v in (explicit or {}).items() if v}

        self._order: List[str] = [str(k) for k in self.params.keys()]
        # 语义组 → [(原生名, 是否必填)]
        self._by_group: Dict[str, List[Tuple[str, bool]]] = {}
        for name in self._order:
            sc = self.params.get(name) if isinstance(self.params.get(name), dict) else {}
            sem = self.sem_of(name)
            if sem:
                self._by_group.setdefault(sem, []).append((name, bool(sc.get("required"))))

    def sem_of(self, name: str) -> Optional[str]:
        """判断某个**原生字段**归属哪个语义组。

        优先级：yaml 的显式声明 > 内置组判定。

        ⚠️ 特例：pool 里 `reference_audio_file` 按 §1.2 被列在「本地路径」组，
           但具体引擎可能把它声明成 `type: file`（如 audio8 的上传字段）。
           此时以**实际声明类型**为准，归入「上传」语义，
           否则 §3.1 的路径优先会错误地挑中一个上传字段。
        """
        declared = self.explicit.get(str(name))
        if declared:
            return declared
        sem = group_of(name)
        if sem == "ref_audio_path":
            sc = self.params.get(name)
            if isinstance(sc, dict) and sc.get("type") == "file":
                return "ref_audio"
        return sem

    # ---------------------------------------------------------------- 入参归一化
    def map(self, key: str) -> Optional[str]:
        """把客户端发来的字段名翻译成引擎原生名。

        :return: 原生名；该引擎不支持此语义时返回 None（调用方据此丢弃该字段）。
        """
        name = str(key)

        # 1) 原生名直通
        if name in self.params:
            return name

        # 2) 显式声明优先 —— 解决 instruction 这类跨引擎歧义
        for native, sem in self.explicit.items():
            if sem == name and native in self.params:
                return native

        # 3) 按语义组找引擎里真正存在的那个原生字段
        sem = group_of(name)
        if not sem:
            return None
        candidates = [
            native
            for native, _req in self._by_group.get(sem, [])
            # 被显式钉成其它语义的原生字段不能拿来复用
            # （如 AuK 的 instruction 已被声明为 text，就不能再当作音色指令）
            if self.sem_of(native) == sem
        ]
        if not candidates:
            return None
        prefer = (_GROUP_INDEX.get(sem) or {}).get("prefer_type")

        def rank(n: str) -> tuple:
            sc = self.params.get(n)
            sc = sc if isinstance(sc, dict) else {}
            t = str(sc.get("type") or "")
            return (
                0 if sc.get("required") else 1,  # 必填者优先
                0 if (prefer and t == prefer) else 1,  # 类型贴合该语义者优先
                self._order.index(n),  # 最后保持 yaml 声明顺序
            )

        candidates.sort(key=rank)
        return candidates[0]

    def normalize(self, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
        """整包归一化。

        :return: (归一化后的 payload, 变更明细)。明细形如
                 [{"from": "text", "to": "instruction", "how": "alias"},
                  {"from": "foo",  "to": "",            "how": "dropped"}]
                 便于排查"客户端发了但引擎没生效"的问题。
        """
        if not isinstance(payload, dict):
            return payload, []
        out: Dict[str, Any] = {}
        notes: List[Dict[str, str]] = []
        for key, value in payload.items():
            native = self.map(key)
            if native is None:
                # 未知字段：保持透传（管家契约是"不丢参数"），但记一笔方便排错
                out[key] = value
                notes.append({"from": str(key), "to": str(key), "how": "passthrough-unknown"})
                continue
            if native != key:
                if native in out and str(payload.get(native)) != "":
                    # 客户端同时发了原生名与规范名 → 以原生名为准，避免歧义覆盖
                    notes.append({"from": str(key), "to": native, "how": "suppressed"})
                    continue
                notes.append({"from": str(key), "to": native, "how": "alias"})
            out[native] = value
        return out, notes

    # ---------------------------------------------------------------- 必填校验
    def missing_required(self, keys: Iterable[str]) -> List[str]:
        """返回尚未提供的必填字段（原生名）。

        会先把客户端给的 key 归一化到原生名再比对，因此客户端用 `text`
        去满足 AuK 的必填 `instruction` 也算已提供。
        """
        provided = set()
        for key in keys or []:
            native = self.map(key)
            if native:
                provided.add(native)
        miss: List[str] = []
        for name, sc in self.params.items():
            if isinstance(sc, dict) and sc.get("required") and name not in provided:
                miss.append(name)
        return miss

    # ---------------------------------------------------------------- 出参增强
    def canonical_view(self) -> Dict[str, Any]:
        """返回增强后的参数表：原生字段 + 规范别名字段（标记 alias_of）。

        客户端据此按统一名字探测能力；真正转发时别名会被 `normalize` 翻译回原生名，
        **不会**把别名和原生名同时发给引擎。
        """
        out: Dict[str, Any] = dict(self.params)
        for name in self._order:
            sc = self.params.get(name)
            sc = sc if isinstance(sc, dict) else {}
            sem = self.sem_of(name)
            grp = _GROUP_INDEX.get(sem or "")
            if not grp:
                continue
            for alias in grp["expose"]:
                # ⚠️ 判重要看**输出表**而不是原生表：
                #    同一个语义可能有多个原生字段（如 AuK 的 instruction 与 gen_text 都能承担文本），
                #    此时应由声明顺序在前、或必填的那个胜出，不能被后来者覆盖。
                if alias == name or alias in out:
                    continue
                view = dict(sc)
                view["alias_of"] = name
                view["canonical"] = True
                # 别名条目不重复声明默认值，避免前端/客户端重复填充同一个值
                view.pop("default", None)
                out[alias] = view
        return out

    def canonical_map(self) -> Dict[str, str]:
        """该引擎支持的「规范名 → 原生名」快照（不含别名本体，只看真正生效的字段）。

        例：AuK → {"ref_audio": "audio", "prompt_text": "ref_text", "text": "instruction"}
        """
        out: Dict[str, str] = {}
        for name in self._order:
            sem = self.sem_of(name)
            if sem:
                # 用 map() 的结果，保证与真正转发时选中同一个原生字段
                # （同组多候选时的 prefer_type / 必填优先规则才会一致）
                out.setdefault(sem, self.map(sem) or name)
        return out
