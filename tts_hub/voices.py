"""项目音色库（voice/ 目录 + voice.json 目录索引）

设计要点：
  1. 音色文件统一放在仓库根目录的 `voice/` 下，索引文件为 `voice/voice.json`；
  2. `voice.json` 记录每个音色的**唯一 ID**、名称、文件路径、性别、年龄，结构：
     {
       "voices": [
         { "id": "v1a2b3c4d", "name": "女声-青年", "file": "women.wav",
           "gender": "female", "age": "young",
           "legacy_ids": ["women"] }   # 旧 ID（历史遗留，仅用于兼容查找）
       ]
     }
  3. **ID 必须是唯一且稳定的**：早期版本直接拿文件名当 ID，同名/改名/长中文名都会
     造成调用冲突；现在统一使用 `v` + 8 位十六进制（uuid4 前 8 位），
     旧 ID 保留在 `legacy_ids` 里仅用于反查兼容；
  4. 目录里存在但索引里没收录的音频文件，会在读取时被自动补录（避免手工维护遗漏）；
  5. 一切路径操作都限制在 `voice/` 目录内，防止越权读写项目其它文件。

对外接口（见 server.py）：
    GET  /api/hub/voices              列出音色
    GET  /api/hub/voices/{id}/audio   取音色音频（供前端预览 / 直接合成）
    POST /api/hub/voices              上传音频并登记为新音色
    GET  /api/voices                  对外开放：音色列表（供外部程序选择音色）
    GET  /api/voices/{id}/audio       对外开放：取音色音频
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .registry import ROOT

VOICE_DIR = ROOT / "voice"
VOICE_INDEX = VOICE_DIR / "voice.json"

AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".opus", ".aac", ".wma"}

GENDERS = ("female", "male", "neutral")
AGES = ("child", "teen", "young", "middle", "old")

# 音色唯一 ID 的格式：v + 8 位十六进制
ID_PATTERN = re.compile(r"^v[0-9a-f]{8}$")


# ---------------------------------------------------------------------------
# 索引读写
# ---------------------------------------------------------------------------
def _empty_index() -> Dict[str, Any]:
    return {"voices": []}


def index_path() -> Path:
    return VOICE_DIR / "voice.json"


def load_index() -> Dict[str, Any]:
    """读取 voice.json；文件缺失或损坏时返回空索引（不抛异常）。"""
    p = index_path()
    if not p.is_file():
        return _empty_index()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return _empty_index()
    if not isinstance(data, dict):
        return _empty_index()
    voices = data.get("voices")
    if not isinstance(voices, list):
        data["voices"] = []
    return data


def save_index(data: Dict[str, Any]) -> None:
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    data.setdefault("voices", [])
    index_path().write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _safe_name(raw: str, fallback: str = "voice") -> str:
    """把任意名字规整成安全文件名（不含路径分隔符与特殊字符）。"""
    name = (raw or "").strip()
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", name)
    name = name.strip("._")
    return name or fallback


def _match_entry(entries: List[Dict[str, Any]], key: str) -> Optional[Dict[str, Any]]:
    """按 id → 历史 id → 文件名 依次匹配音色条目。"""
    k = (key or "").strip()
    if not k:
        return None
    for e in entries:
        if str(e.get("id")) == k:
            return e
    for e in entries:
        legacy = e.get("legacy_ids")
        legacy = [str(x) for x in legacy] if isinstance(legacy, list) else []
        if k in legacy:
            return e
    for e in entries:
        if str(e.get("file")) == k:
            return e
    return None


def _gen_id(taken: set) -> str:
    """生成一个全局唯一的音色 ID（v + 8 位十六进制）。"""
    for _ in range(50):
        vid = "v" + uuid.uuid4().hex[:8]
        if vid not in taken:
            taken.add(vid)
            return vid
    # 极端情况下退化为自增后缀
    i = 1
    while True:
        vid = "v" + uuid.uuid4().hex[:7] + str(i % 10)
        if vid not in taken:
            taken.add(vid)
            return vid
        i += 1


def ensure_unique_ids(entries: List[Dict[str, Any]]) -> bool:
    """把索引里的音色 ID 统一成唯一 ID（就地修改 entries）。

    · ID 缺失 / 不符合 `vxxxxxxxx` 格式 / 与他人重复 → 重新分配；
    · 旧 ID 记进 `legacy_ids`，保证历史调用（配置文件、旧脚本）仍能反查到音色。

    :return: 是否有改动（调用方据此决定是否落盘）
    """
    changed = False
    taken: set = set()
    seen: set = set()

    for e in entries:
        vid = str(e.get("id") or "")
        if not (ID_PATTERN.match(vid) and vid not in seen):
            continue
        seen.add(vid)
        taken.add(vid)

    for e in entries:
        vid = str(e.get("id") or "")
        if ID_PATTERN.match(vid) and vid in seen:
            continue
        legacy = e.get("legacy_ids")
        legacy = [str(x) for x in legacy] if isinstance(legacy, list) else []
        if vid and vid not in legacy:
            legacy.insert(0, vid)
        # 只保留最近 8 个历史 ID，避免索引无限膨胀
        e["legacy_ids"] = legacy[:8]
        e["id"] = _gen_id(taken)
        seen.add(e["id"])
        changed = True
    return changed


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------
def list_voices() -> List[Dict[str, Any]]:
    """列出全部音色，并把目录里未收录的音频自动补录进索引。"""
    index = load_index()
    entries: List[Dict[str, Any]] = [e for e in index.get("voices", []) if isinstance(e, dict)]
    known_files = {str(e.get("file") or "") for e in entries}
    known_ids = {str(e.get("id") or "") for e in entries}

    changed = ensure_unique_ids(entries)
    known_ids = {str(e.get("id") or "") for e in entries}

    if VOICE_DIR.is_dir():
        for p in sorted(VOICE_DIR.iterdir()):
            if not p.is_file() or p.suffix.lower() not in AUDIO_SUFFIXES:
                continue
            if p.name in known_files:
                continue
            vid = _gen_id(known_ids)
            entries.append(
                {
                    "id": vid,
                    "name": p.stem,
                    "file": p.name,
                    "gender": "",
                    "age": "",
                    "text": "",
                    "text_source": "none",
                    "legacy_ids": [],
                    "source": "scan",
                }
            )
            known_ids.add(vid)
            known_files.add(p.name)
            changed = True

    if changed:
        index["voices"] = entries
        try:
            save_index(index)
        except Exception:
            pass

    # 附带音频是否真实存在，前端可据此过滤
    out = []
    for e in entries:
        item = dict(e)
        item.setdefault("id", "")
        item.setdefault("name", item["id"])
        item.setdefault("file", "")
        item.setdefault("gender", "")
        item.setdefault("age", "")
        item.setdefault("text", "")
        item.setdefault("text_source", "manual" if str(item.get("text") or "").strip() else "none")
        item.setdefault("legacy_ids", [])
        item["exists"] = bool(item["file"]) and (VOICE_DIR / str(item["file"])).is_file()
        if item["exists"]:
            try:
                item["size"] = (VOICE_DIR / str(item["file"])).stat().st_size
            except OSError:
                item["size"] = 0
        else:
            item["size"] = 0
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# 取音频
# ---------------------------------------------------------------------------
def resolve(voice_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[Path]]:
    """按 id（或历史 id / 文件名）找到音色条目与音频路径，找不到返回 (None, None)。

    查找优先级：当前唯一 ID → 历史 ID（legacy_ids）→ 文件名。
    ⚠️ 不按 name 查找：名称允许重复，按名称查会把调用指向不确定的音色。
    """
    vid = (voice_id or "").strip()
    if not vid:
        return None, None
    for e in list_voices():
        legacy = e.get("legacy_ids")
        legacy = [str(x) for x in legacy] if isinstance(legacy, list) else []
        if str(e.get("id")) == vid or vid in legacy or str(e.get("file")) == vid:
            fn = str(e.get("file") or "")
            if not fn:
                return e, None
            p = (VOICE_DIR / fn).resolve()
            try:
                p.relative_to(VOICE_DIR.resolve())
            except ValueError:
                return e, None
            return e, (p if p.is_file() else None)
    return None, None


# ---------------------------------------------------------------------------
# 新增音色
# ---------------------------------------------------------------------------
def add_voice(
    *,
    src: Path,
    filename: str,
    name: str = "",
    gender: str = "",
    age: str = "",
    text: str = "",
    text_source: str = "manual",
) -> Dict[str, Any]:
    """把 src 指向的音频复制进 voice/ 并登记到 voice.json，返回新条目。

    ``text`` 是该音色的参考文字稿（克隆时直接复用，免去引擎侧再跑 ASR）；
    ``text_source`` 记录来源：``manual``（人工填写）/ ``asr``（入库时自动转写）。
    """
    VOICE_DIR.mkdir(parents=True, exist_ok=True)

    suffix = Path(filename or src.name).suffix.lower()
    if suffix not in AUDIO_SUFFIXES:
        suffix = src.suffix.lower() if src.suffix.lower() in AUDIO_SUFFIXES else ".wav"

    base = _safe_name(Path(filename or src.name).stem, "voice")
    index = load_index()
    entries = [e for e in index.get("voices", []) if isinstance(e, dict)]
    taken_ids = {str(e.get("id") or "") for e in entries}
    taken_files = {str(e.get("file") or "") for e in entries}

    # ID 与文件名彻底解耦：ID 全局唯一，文件名只用于存储
    vid = _gen_id(taken_ids)
    fn = f"{base}{suffix}"
    i = 2
    while fn in taken_files:
        fn = f"{base}_{i}{suffix}"
        i += 1
    taken_files.add(fn)

    dst = VOICE_DIR / fn
    shutil.copyfile(src, dst)

    entry = {
        "id": vid,
        "name": (name or "").strip() or base,
        "file": fn,
        "gender": (gender or "").strip().lower(),
        "age": (age or "").strip().lower(),
        "text": (text or "").strip(),
        "text_source": (text_source or "manual").strip() or "manual",
        "legacy_ids": [],
        "source": "upload",
    }
    entries.append(entry)
    index["voices"] = entries
    save_index(index)
    return entry


# ---------------------------------------------------------------------------
# 更新 / 删除
# ---------------------------------------------------------------------------
def update_voice(
    voice_id: str,
    *,
    name: Optional[str] = None,
    gender: Optional[str] = None,
    age: Optional[str] = None,
    text: Optional[str] = None,
    text_source: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """更新音色属性（只改索引，不动音频文件）。找不到返回 None。"""
    index = load_index()
    entries = [e for e in index.get("voices", []) if isinstance(e, dict)]
    target = _match_entry(entries, voice_id)
    if target is None:
        return None

    if name is not None and str(name).strip():
        target["name"] = str(name).strip()
    if gender is not None:
        target["gender"] = str(gender).strip().lower()
    if age is not None:
        target["age"] = str(age).strip().lower()
    if text is not None:
        target["text"] = str(text).strip()
        # 文本被改动 → 来源改回人工（除非调用方显式指定）
        target["text_source"] = (text_source or "manual").strip() or "manual"
    elif text_source is not None:
        target["text_source"] = str(text_source).strip() or "manual"

    index["voices"] = entries
    save_index(index)
    return target


def delete_voice(voice_id: str, *, remove_file: bool = True) -> Optional[Dict[str, Any]]:
    """删除音色条目（默认同时删除其音频文件）。找不到返回 None。"""
    index = load_index()
    entries = [e for e in index.get("voices", []) if isinstance(e, dict)]
    target = _match_entry(entries, voice_id)
    if target is None:
        return None

    fn = str(target.get("file") or "")
    if remove_file and fn:
        p = (VOICE_DIR / fn).resolve()
        try:
            p.relative_to(VOICE_DIR.resolve())
        except ValueError:
            p = None
        if p is not None and p.is_file():
            try:
                p.unlink()
            except OSError:
                pass

    index["voices"] = [e for e in entries if e is not target]
    save_index(index)
    return target
