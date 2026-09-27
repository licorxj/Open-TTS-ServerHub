from __future__ import annotations

import hashlib
import logging
import math
import os
import sys
import traceback
from contextlib import ExitStack
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Keep ModelScope snapshots across Studio restarts. Credentials stay in an
# ephemeral directory while model files live on the persistent workspace disk.
MODELSCOPE_CACHE_ROOT = Path(
    os.getenv("AUK_MODELSCOPE_CACHE", "/mnt/workspace/.cache/modelscope")
)
MODELSCOPE_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
MODELSCOPE_CREDENTIALS_ROOT = Path(
    os.getenv("AUK_MODELSCOPE_CREDENTIALS", "/tmp/auk-modelscope-credentials")
)
MODELSCOPE_CREDENTIALS_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
os.environ["MODELSCOPE_CREDENTIALS_PATH"] = str(MODELSCOPE_CREDENTIALS_ROOT)

import gradio as gr
import soundfile as sf
import torch
import torchaudio
from modelscope import HubApi
from modelscope.hub.snapshot_download import snapshot_download
from omegaconf import OmegaConf

ROOT_DIR = Path(__file__).resolve().parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from auk.infer import infer_auk as inference  # noqa: I001
from auk.infer import infer_gradio as gradio_app
from auk.infer import pe as prompt_enhancer
from auk.infer.infer_auk import AukInfer


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("auk-modelscope-studio")
for noisy_logger in ("httpx", "httpcore", "openai"):
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)

torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True

QWEN_REPO_ID = "Qwen/Qwen2.5-Omni-3B"
UPSTREAM_REPO = "git@github.com:Tencent-Hunyuan/AuK.git"
UPSTREAM_COMMIT = "e3828bdac1712bbf7bcb3a2a01eec6fa5168fe32"
MODELSCOPE_ENDPOINT = os.getenv("MODELSCOPE_ENDPOINT", "https://modelscope.cn")
MODEL_LABEL_FLASH = gradio_app.FLASH_LABEL
MODEL_LABEL_BASE = gradio_app.BASE_LABEL
LLM_ENV_NAMES = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL_NAME")
ASR_ENV_NAMES = ("TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY")
SECRET_ENV_NAMES = (
    "MODELSCOPE_API_KEY",
    *LLM_ENV_NAMES,
    *ASR_ENV_NAMES,
    "ASR_ENGINE_MODEL_TYPE",
)
_MODELSCOPE_AUTHENTICATED = False

# Retain the original callbacks so create_demo() can also be tested repeatedly.
_upstream_generate = gradio_app.run_generate
_upstream_generate_with_pe = gradio_app.run_generate_with_pe


class SpaceAudioIO:
    """Keep the upstream 2.7 audio contract on Studio's newer Torch runtime.

    TorchAudio 2.9+ removed info() and moved load/save to TorchCodec, where the
    legacy PCM encoding arguments are ignored. Only AuK's two inference modules
    use this SoundFile facade; transforms and the rest delegate to TorchAudio.
    Gradio supplies local audio files. This is not a replacement for every
    TorchAudio backend or its streaming/URL APIs.
    """

    def __getattr__(self, name):
        return getattr(torchaudio, name)

    @staticmethod
    def info(path):
        try:
            info = sf.info(path)
        except sf.LibsndfileError:
            audio, sample_rate = torchaudio.load(path)
            return SimpleNamespace(
                sample_rate=sample_rate,
                num_frames=audio.shape[-1],
                num_channels=audio.shape[0],
            )
        return SimpleNamespace(
            sample_rate=info.samplerate,
            num_frames=info.frames,
            num_channels=info.channels,
        )

    @staticmethod
    def load(path):
        try:
            samples, sample_rate = sf.read(path, dtype="float32", always_2d=True)
        except sf.LibsndfileError:
            # AAC/M4A and other codecs outside libsndfile use the pinned
            # TorchCodec/FFmpeg stack instead of losing upstream format support.
            return torchaudio.load(path)
        return torch.from_numpy(samples.T.copy()), sample_rate

    @staticmethod
    def save(path, audio, sample_rate, *, encoding=None, bits_per_sample=None):
        if encoding not in (None, "PCM_S") or bits_per_sample not in (None, 16):
            raise ValueError("The Studio audio adapter supports PCM16 WAV output only.")
        samples = audio.detach().to(dtype=torch.float32, device="cpu")
        if samples.ndim != 2:
            raise ValueError(
                "Expected channels-first audio with shape [channels, samples]."
            )
        sf.write(path, samples.T.numpy(), sample_rate, format="WAV", subtype="PCM_16")


def _missing(names):
    return [name for name in names if not os.getenv(name, "").strip()]


def _report_environment():
    """Report missing names only; never log configuration values."""
    missing = _missing(LLM_ENV_NAMES)
    if missing:
        logger.warning(
            "Prompt Enhancer needs these Studio settings: %s. Manual mode remains available.",
            ", ".join(missing),
        )
    else:
        logger.info("Prompt Enhancer LLM configuration is present (values hidden).")
    missing_asr = _missing(ASR_ENV_NAMES)
    if missing_asr:
        logger.warning(
            "Cloud ASR settings missing: %s. Upstream SenseVoice CPU fallback remains enabled.",
            ", ".join(missing_asr),
        )
    else:
        logger.info("Cloud ASR credentials are present; CPU fallback remains enabled.")


def _redact(message):
    values = {os.getenv(name, "").strip() for name in SECRET_ENV_NAMES}
    for value in sorted(values - {""}, key=len, reverse=True):
        message = message.replace(value, "[redacted]")
    return message


@wraps(_upstream_generate_with_pe)
def _safe_generate_with_pe(
    use_pe, variant, audio, instruction, gen_seconds, ref_text, gen_text, nfe, cfg, seed
):
    if use_pe:
        missing = _missing(LLM_ENV_NAMES)
        if missing:
            raise gr.Error(
                "Prompt Enhancer is missing Studio settings: "
                + ", ".join(missing)
                + ". Configure them in Settings, or disable Use Prompt Enhancer."
            )
    else:
        try:
            duration = float(gen_seconds)
        except (TypeError, ValueError, OverflowError):
            duration = float("nan")
        if not math.isfinite(duration) or duration <= 0:
            raise gr.Error(
                "Duration is required when Prompt Enhancer is off. "
                "Enter a duration greater than 0 seconds."
            )
        gen_seconds = duration
    try:
        return _upstream_generate_with_pe(
            use_pe,
            variant,
            audio,
            instruction,
            gen_seconds,
            ref_text,
            gen_text,
            nfe,
            cfg,
            seed,
        )
    except gr.Error as exc:
        # Providers can echo a token or endpoint in authentication failures.
        raise gr.Error(_redact(str(exc.message))) from None
    except Exception as exc:  # noqa: BLE001 -- public error boundary must not expose provider credentials.
        logger.error(
            "Request failed (%s); provider details redacted.\n%s",
            type(exc).__name__,
            _redact(traceback.format_exc()),
        )
        raise gr.Error(
            f"Request failed ({type(exc).__name__}). Check the Studio configuration and provider availability."
        ) from None


def _duration_controls(use_pe):
    return gr.update(
        interactive=True,
        label=(
            "Duration (sec; 0 = automatic with Prompt Enhancer)"
            if use_pe
            else "Duration (sec; required, greater than 0)"
        ),
    )


def _configure_prompt_enhancer_ui(demo):
    """Apply deployment-only policy to the unmodified upstream UI."""
    checkboxes = [
        block
        for block in demo.blocks.values()
        if isinstance(block, gr.Checkbox) and block.label == "Use Prompt Enhancer"
    ]
    durations = [
        block
        for block in demo.blocks.values()
        if isinstance(block, gr.Slider)
        and (
            block.label == "Duration (sec)"
            or (block.label or "").startswith("Duration (sec;")
        )
    ]
    if len(checkboxes) != 1 or len(durations) != 1:
        raise RuntimeError("Upstream Prompt Enhancer/duration controls have changed.")
    use_pe, duration = checkboxes[0], durations[0]
    use_pe.value = True
    use_pe.info = (
        "Enabled by default: prepares the task and instruction. "
        "Keep duration at 0 for automatic estimation, or enter a value to override it. "
        "When disabled, a duration greater than 0 is required."
    )
    duration.label = _duration_controls(True)["label"]
    duration.interactive = True
    for block in demo.blocks.values():
        if isinstance(block, gr.Markdown) and isinstance(block.value, str):
            block.value = block.value.replace(
                "Duration priority: Duration > reference-text + target-text estimate > match the source length.",
                "Prompt Enhancer is optional and enabled by default. "
                "With it enabled, 0 estimates duration automatically and a value above 0 overrides it. "
                "With it disabled, an explicit duration greater than 0 seconds is required, "
                "including when reference audio or text is provided.",
            )
            block.value = block.value.replace(
                "0 = PE auto-estimation (PE required); values above 0 override PE duration.",
                "With Prompt Enhancer: 0 = automatic estimation; values above 0 override the duration. "
                "Without Prompt Enhancer: enter a value greater than 0.",
            )
    with demo:
        use_pe.change(
            _duration_controls,
            inputs=use_pe,
            outputs=duration,
            queue=False,
            api_visibility="private",
        )


@dataclass(frozen=True)
class ModelSpec:
    repo_id: str
    checkpoint_name: str
    revision_env: str

    @property
    def revision(self) -> str:
        return os.getenv(self.revision_env, "master")


@dataclass(frozen=True)
class ReleaseFiles:
    config: Path
    checkpoint: Path
    vae: Path


MODEL_SPECS = {
    MODEL_LABEL_BASE: ModelSpec(
        "Tencent-Hunyuan/AuK", "auk_base.safetensors", "AUK_REVISION"
    ),
    MODEL_LABEL_FLASH: ModelSpec(
        "Tencent-Hunyuan/AuK-Flash",
        "auk_flash.safetensors",
        "AUK_FLASH_REVISION",
    ),
}


def _modelscope_token() -> str | None:
    """Return the optional token used to read private ModelScope repositories."""
    return os.getenv("MODELSCOPE_API_KEY") or None


def _authenticate_modelscope() -> None:
    """Exchange the access token for the cookie used by ModelScope SDK 1.37."""
    global _MODELSCOPE_AUTHENTICATED
    if _MODELSCOPE_AUTHENTICATED:
        return
    token = _modelscope_token()
    if token:
        HubApi(endpoint=MODELSCOPE_ENDPOINT).login(
            access_token=token,
            endpoint=MODELSCOPE_ENDPOINT,
        )
    _MODELSCOPE_AUTHENTICATED = True


def _download_snapshot(
    repo_id: str,
    *,
    revision: str = "master",
    allow_patterns: list[str] | None = None,
) -> Path:
    logger.info("Resolving %s at revision %s from ModelScope", repo_id, revision)
    try:
        _authenticate_modelscope()
        return Path(
            snapshot_download(
                model_id=repo_id,
                revision=revision,
                cache_dir=MODELSCOPE_CACHE_ROOT,
                allow_patterns=allow_patterns,
                endpoint=MODELSCOPE_ENDPOINT,
                max_workers=4,
            )
        )
    except Exception as exc:
        raise RuntimeError(
            f"Cannot download {repo_id} from ModelScope. If the repository is private, "
            "add MODELSCOPE_API_KEY as a Studio secret with read access."
        ) from exc


def _download_release(spec: ModelSpec) -> ReleaseFiles:
    snapshot_dir = _download_snapshot(
        spec.repo_id,
        revision=spec.revision,
        allow_patterns=[spec.checkpoint_name, "config.yaml", "vae.safetensors"],
    )

    files = ReleaseFiles(
        config=snapshot_dir / "config.yaml",
        checkpoint=snapshot_dir / spec.checkpoint_name,
        vae=snapshot_dir / "vae.safetensors",
    )
    missing = [
        str(path)
        for path in (files.config, files.checkpoint, files.vae)
        if not path.is_file()
    ]
    if missing:
        raise FileNotFoundError(f"Incomplete {spec.repo_id} release: {missing}")
    return files


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _vae_signature(config):
    signature = OmegaConf.to_container(config.model.vae, resolve=True)
    signature.pop("vae_model_path", None)
    return signature


def _build_engines(
    releases: dict[str, ReleaseFiles], qwen_path: Path
) -> dict[str, AukInfer]:
    """Construct upstream engines during startup with scoped factory injection.

    The canonical AukInfer constructor has no component-sharing arguments. The
    factories below are replaced only while constructing Flash, before the app
    accepts requests, then restored even on failure. No source file is patched.
    All model construction, checkpoint loading and sampling stay upstream.
    """
    if not torch.cuda.is_available():
        raise RuntimeError(
            "AuK needs a GPU. Select an xGPU or dedicated GPU in Studio settings."
        )
    base_files = releases[MODEL_LABEL_BASE]
    flash_files = releases[MODEL_LABEL_FLASH]
    device = "cuda"

    logger.info("Loading %s on %s", MODEL_LABEL_BASE, device)
    base_engine = AukInfer(
        config_path=str(base_files.config),
        ckpt_path=str(base_files.checkpoint),
        qwen_path=str(qwen_path),
        device=device,
        dtype="bf16",
    )

    flash_config = OmegaConf.load(flash_files.config)
    vae_is_shared = _vae_signature(base_engine.config) == _vae_signature(
        flash_config
    ) and _sha256(base_files.vae) == _sha256(flash_files.vae)
    if vae_is_shared:
        logger.info(
            "AuK and AuK-Flash have identical VAE weights; sharing one VAE instance."
        )
    else:
        logger.warning(
            "The VAE weights/config differ across releases; loading a separate Flash VAE."
        )

    logger.info("Loading %s on %s", MODEL_LABEL_FLASH, device)
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                inference,
                "Qwen2_5OmniThinkerForConditionalGeneration",
                SimpleNamespace(
                    from_pretrained=lambda *a, **kw: base_engine.model.text_encoder
                ),
            )
        )
        stack.enter_context(
            patch.object(
                inference,
                "Qwen2_5OmniProcessor",
                SimpleNamespace(
                    from_pretrained=lambda *a, **kw: base_engine.model.text_processor
                ),
            )
        )
        if vae_is_shared:
            stack.enter_context(
                patch.object(
                    inference, "load_vae_model", lambda *a, **kw: base_engine.vae_model
                )
            )
        flash_engine = AukInfer(
            config_path=str(flash_files.config),
            ckpt_path=str(flash_files.checkpoint),
            qwen_path=str(qwen_path),
            device=device,
            dtype="bf16",
        )
    return {MODEL_LABEL_BASE: base_engine, MODEL_LABEL_FLASH: flash_engine}


def create_demo():
    logger.info(
        "Building AuK ModelScope Studio from %s at %s", UPSTREAM_REPO, UPSTREAM_COMMIT
    )
    _report_environment()
    audio_io = SpaceAudioIO()
    inference.torchaudio = audio_io
    prompt_enhancer.torchaudio = audio_io
    releases = {label: _download_release(spec) for label, spec in MODEL_SPECS.items()}
    qwen_path = _download_snapshot(QWEN_REPO_ID)

    # New upstream build_demo() validates these paths to populate the model UI.
    gradio_app.CKPT_PATHS.clear()
    gradio_app.CONFIG_PATHS.clear()
    gradio_app.ENGINES.clear()
    for label, files in releases.items():
        gradio_app.CKPT_PATHS[label] = str(files.checkpoint)
        gradio_app.CONFIG_PATHS[label] = str(files.config)
    gradio_app.ENGINES.update(_build_engines(releases, qwen_path))

    # ModelScope xGPU is attached to the whole Studio process, so both resident
    # engines and generation can use CUDA directly. Prompt Enhancer, ASR, VAD,
    # and temporary-audio cleanup retain the upstream callback boundaries.
    gradio_app.run_generate = _upstream_generate
    gradio_app.run_generate_with_pe = _safe_generate_with_pe
    demo = gradio_app.build_demo()
    _configure_prompt_enhancer_ui(demo)
    demo.queue(default_concurrency_limit=1, max_size=16)
    return demo


if __name__ == "__main__":
    demo = create_demo()
    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
        theme=gr.themes.Soft(),
        css=gradio_app.DEMO_CSS,
        show_error=False,
    )
