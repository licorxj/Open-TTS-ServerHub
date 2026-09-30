import os
import sys
import json
import logging
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)

MODEL_REGISTRY = {
    "vox": {
        "repo": "openbmb/VoxCPM2",
        "local_dir": "models/VoxCPM2",
        "source": "huggingface",
        "required_files": ["config.json", "tokenizer.json"],
    },
    "omnivoice": {
        "repo": "k2-fsa/OmniVoice",
        "local_dir": "models/omnivoice",
        "source": "huggingface",
        "required_files": ["config.json"],
    },
    "indextts": {
        "repo": "IndexTeam/IndexTTS-2",
        "local_dir": "models/index2",
        "source": "modelscope",
        "required_files": ["bpe.model", "gpt.pth", "s2mel.pth", "wav2vec2bert_stats.pt"],
    },
    "dots": {
        "repo": "rednote-hilab/dots.tts-soar",
        "local_dir": "models/dot",
        "source": "modelscope",
        "required_files": ["config.json", "llm_config.json"],
    },
    "audio8": {
        "repo": "Audio8/Audio8-TTS-Preview-0.6b",
        "local_dir": "models/Audio8",
        "source": "huggingface",
        "required_files": ["config.json"],
    },
    # ---- AuK 语音生成/编辑模型（腾讯混元，魔搭托管）----
    # 对应命令行：modelscope download --model Tencent-Hunyuan/AuK
    #            modelscope download --model Tencent-Hunyuan/AuK-Flash
    # 说明：snapshot_download 是 `modelscope download --model <repo>` 的程序化等价形式，
    #       且支持 local_dir + allow_patterns，便于只拉取推理所需文件（checkpoint/config/vae）。
    "auk": {
        "repo": "Tencent-Hunyuan/AuK",
        "local_dir": "models/AuK",
        "source": "modelscope",
        "checkpoint_name": "auk_base.safetensors",
        "required_files": ["auk_base.safetensors", "config.yaml", "vae.safetensors"],
        "allow_patterns": ["auk_base.safetensors", "config.yaml", "vae.safetensors"],
    },
    "auk_flash": {
        "repo": "Tencent-Hunyuan/AuK-Flash",
        "local_dir": "models/AuK-Flash",
        "source": "modelscope",
        "checkpoint_name": "auk_flash.safetensors",
        "required_files": ["auk_flash.safetensors", "config.yaml", "vae.safetensors"],
        "allow_patterns": ["auk_flash.safetensors", "config.yaml", "vae.safetensors"],
    },
    # Qwen2.5-Omni-3B 文本/音频编码器。集中放 models/ 单一位置，若其它引擎已下载则复用（candidate_dirs）。
    "qwen_omni": {
        "repo": "Qwen/Qwen2.5-Omni-3B",
        "local_dir": "models/Qwen2.5-Omni-3B",
        "source": "modelscope",
        "required_files": ["config.json"],
        "candidate_dirs": [
            "models/Qwen2.5-Omni-3B",
        ],
    },
    # ---- Breeze-TTS-2 语音合成模型（breezeblue-ai，魔搭托管）----
    # 对应命令行：modelscope download --model BreezeBlue/Breeze-TTS-2
    # 说明：权重为自包含 checkpoint（根 config.json + 分片模型 + tokenizer + audio_tokenizer 子目录）。
    # allow_patterns 只拉推理必需文件，跳过 README / assets / LICENSE 等非权重内容。
    # 注意：audio_tokenizer 子目录是 Qwen3-TTS 音频分词器，load_runtime 强制要求存在。
    "breeze_tts": {
        "repo": "BreezeBlue/Breeze-TTS-2",
        "local_dir": "models/Breeze-TTS-2",
        "source": "modelscope",
        "required_files": [
            "config.json",
            "model.safetensors.index.json",
            "audio_tokenizer/config.json",
            "tokenizer.json",
        ],
        "allow_patterns": [
            "config.json",
            "configuration.json",
            "generation_config.json",
            "model-00001-of-00002.safetensors",
            "model-00002-of-00002.safetensors",
            "model.safetensors.index.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
            "audio_tokenizer/*",
        ],
    },
}


def _get_project_root() -> str:
    current = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(current)


def _validate_local_model(model_path: str, required_files: List[str]) -> bool:
    if not os.path.isdir(model_path):
        return False
    for fname in required_files:
        if not os.path.isfile(os.path.join(model_path, fname)):
            return False
    return True


def _download_from_huggingface(repo_id: str, local_dir: str) -> str:
    from huggingface_hub import snapshot_download

    os.makedirs(local_dir, exist_ok=True)
    logger.info(f"Downloading model from huggingface: {repo_id} -> {local_dir}")
    snapshot_download(repo_id=repo_id, local_dir=local_dir)
    logger.info(f"Download complete: {local_dir}")
    return local_dir


def _download_from_modelscope(repo_id: str, local_dir: str, allow_patterns: Optional[List[str]] = None) -> str:
    from modelscope import snapshot_download

    os.makedirs(local_dir, exist_ok=True)
    logger.info(f"Downloading model from modelscope: {repo_id} -> {local_dir}")
    # snapshot_download 等价于 `modelscope download --model <repo>`，并支持 local_dir / allow_patterns
    kwargs = {}
    if allow_patterns:
        kwargs["allow_patterns"] = allow_patterns
    snapshot_download(repo_id, local_dir=local_dir, **kwargs)
    logger.info(f"Download complete: {local_dir}")
    return local_dir


class ModelsManager:
    _instance = None
    _project_root: str = None

    def __new__(cls, project_root: Optional[str] = None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, project_root: Optional[str] = None):
        if self._initialized:
            if project_root is not None:
                self._project_root = project_root
            return
        self._initialized = True
        self._project_root = project_root or _get_project_root()
        self._registry = dict(MODEL_REGISTRY)

    @classmethod
    def reset(cls):
        cls._instance = None

    @property
    def project_root(self) -> str:
        return self._project_root

    def _resolve_local_dir(self, local_dir: str) -> str:
        if os.path.isabs(local_dir):
            return local_dir
        return os.path.normpath(os.path.join(self._project_root, local_dir))

    def register_model(
        self,
        name: str,
        repo: str,
        local_dir: str,
        source: str = "huggingface",
        required_files: Optional[List[str]] = None,
    ):
        self._registry[name] = {
            "repo": repo,
            "local_dir": local_dir,
            "source": source,
            "required_files": required_files or ["config.json"],
        }

    def get_model_path(self, model_name: str) -> str:
        if model_name not in self._registry:
            raise ValueError(
                f"Unknown model: '{model_name}'. "
                f"Available models: {list(self._registry.keys())}"
            )

        model_info = self._registry[model_name]
        local_dir = self._resolve_local_dir(model_info["local_dir"])
        required_files = model_info.get("required_files", ["config.json"])
        allow_patterns = model_info.get("allow_patterns")
        candidate_dirs = model_info.get("candidate_dirs") or []

        # 1) 主目录已就绪，直接复用
        if _validate_local_model(local_dir, required_files):
            logger.info(f"Model '{model_name}' found at: {local_dir}")
            return local_dir

        # 2) 检查候选目录（其它引擎已下载的同一模型，直接引用，避免重复下载）
        for cand in candidate_dirs:
            cand_dir = self._resolve_local_dir(cand)
            if cand_dir != local_dir and _validate_local_model(cand_dir, required_files):
                logger.info(f"Model '{model_name}' reused from candidate dir: {cand_dir}")
                return cand_dir

        # 3) 缺失则自动下载（魔搭 / HuggingFace）
        logger.info(f"Model '{model_name}' not found locally, downloading...")
        repo = model_info["repo"]
        source = model_info.get("source", "huggingface")

        if source == "huggingface":
            _download_from_huggingface(repo, local_dir)
        elif source == "modelscope":
            _download_from_modelscope(repo, local_dir, allow_patterns=allow_patterns)
        else:
            raise ValueError(f"Unknown download source: {source}")

        if not _validate_local_model(local_dir, required_files):
            raise RuntimeError(
                f"Model download completed but validation failed for: {local_dir}. "
                f"Required files: {required_files}"
            )

        return local_dir

    def validate_model_path(self, model_path: str, model_name: Optional[str] = None) -> str:
        if model_name and model_name in self._registry:
            return self.get_model_path(model_name)
        if not os.path.isdir(model_path):
            raise ValueError(f"Model path does not exist: {model_path}")
        config_path = os.path.join(model_path, "config.json")
        if not os.path.isfile(config_path):
            raise ValueError(f"No config.json found in model path: {model_path}")
        return model_path

    def resolve_path(self, path_or_name: str, model_type: str = "omnivoice") -> str:
        if path_or_name in self._registry:
            return self.get_model_path(path_or_name)
        if os.path.isdir(path_or_name):
            return path_or_name
        resolved = os.path.normpath(os.path.join(self._project_root, path_or_name))
        if os.path.isdir(resolved):
            return resolved
        return self.get_model_path(model_type)

    def list_models(self) -> Dict[str, dict]:
        return dict(self._registry)

    def download_model(self, model_name: str) -> str:
        """无条件（重新）触发下载，忽略本地目录是否已存在。

        适用于：本地权重被中断/损坏导致不完整，需要补齐或重建。
        返回最终本地目录路径。
        """
        if model_name not in self._registry:
            raise ValueError(
                f"Unknown model: '{model_name}'. "
                f"Available models: {list(self._registry.keys())}"
            )
        info = self._registry[model_name]
        local_dir = self._resolve_local_dir(info["local_dir"])
        source = info.get("source", "huggingface")
        if source == "huggingface":
            _download_from_huggingface(info["repo"], local_dir)
        elif source == "modelscope":
            _download_from_modelscope(
                info["repo"], local_dir, allow_patterns=info.get("allow_patterns")
            )
        else:
            raise ValueError(f"Unknown download source: {source}")
        return local_dir
