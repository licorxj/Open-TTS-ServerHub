"""Offline contract tests: real UI/configuration, no model or paid API calls."""

import importlib
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import gradio as gr
import numpy as np
import pytest
import soundfile as sf
import torch
import yaml
from omegaconf import OmegaConf

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("AUK_MODELSCOPE_CACHE", "/tmp/auk-modelscope-tests/cache")
os.environ.setdefault(
    "AUK_MODELSCOPE_CREDENTIALS", "/tmp/auk-modelscope-tests/credentials"
)
sys.path.insert(0, str(ROOT))
app = importlib.import_module("app")


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch):
    # Never use a developer's real credentials in tests.
    for name in (*app.SECRET_ENV_NAMES, "SecretId", "SecretKey"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(app.gradio_app, "CKPT_PATHS", {})
    monkeypatch.setattr(app.gradio_app, "CONFIG_PATHS", {})
    monkeypatch.setattr(app.gradio_app, "ENGINES", {})
    monkeypatch.setattr(app.gradio_app, "run_generate", app._upstream_generate)
    monkeypatch.setattr(
        app.gradio_app, "run_generate_with_pe", app._upstream_generate_with_pe
    )


@pytest.fixture
def releases(tmp_path):
    result = {}
    for label, spec in app.MODEL_SPECS.items():
        folder = tmp_path / spec.checkpoint_name
        folder.mkdir()
        config = folder / "config.yaml"
        config.write_text(
            yaml.safe_dump(
                {
                    "model": {
                        "name": "AuK-Flash"
                        if label == app.MODEL_LABEL_FLASH
                        else "AuK",
                        "text_encoder": {"text_encoder_path": "stub-qwen"},
                        "vae": {
                            "vae_name": "stub-vae",
                            "target_sample_rate": 24000,
                            "downsample_rate": 480,
                            "latent_dim": 4,
                            "model_init_kwargs": {},
                        },
                        "arch": {},
                    }
                }
            )
        )
        checkpoint = folder / spec.checkpoint_name
        checkpoint.write_bytes(b"stub checkpoint")
        vae = folder / "vae.safetensors"
        vae.write_bytes(b"same stub VAE")
        result[label] = app.ReleaseFiles(config, checkpoint, vae)
    return result


def test_audio_round_trip(tmp_path):
    audio_io = app.SpaceAudioIO()
    t = torch.arange(12000) / 24000
    original = torch.stack(
        [0.2 * torch.sin(t * 440 * 2 * torch.pi), torch.zeros_like(t)]
    )
    path = tmp_path / "roundtrip.wav"
    audio_io.save(path, original, 24000, encoding="PCM_S", bits_per_sample=16)
    restored, sample_rate = audio_io.load(path)
    info = audio_io.info(path)
    assert sample_rate == info.sample_rate == 24000
    assert info.num_frames == 12000
    assert info.num_channels == 2
    assert restored.shape == original.shape
    assert (restored - original).abs().max().item() <= 1 / 32768 + 1e-6
    assert sf.info(path).subtype == "PCM_16"
    assert audio_io.transforms is app.torchaudio.transforms


def test_duration_works_without_torchaudio_info(tmp_path, monkeypatch):
    path = tmp_path / "duration.wav"
    app.SpaceAudioIO.save(path, torch.zeros(1, 12000), 24000)
    monkeypatch.setattr(app.inference, "torchaudio", app.SpaceAudioIO())
    monkeypatch.setattr(app.prompt_enhancer, "torchaudio", app.SpaceAudioIO())
    assert app.inference.get_gen_duration(audio=str(path)) == pytest.approx(0.5)
    assert app.prompt_enhancer._audio_duration(str(path)) == pytest.approx(0.5)
    wav = app.prompt_enhancer._load_asr_wav(str(path))
    assert wav[:4] == b"RIFF"


def test_audio_codec_fallback(tmp_path, monkeypatch):
    path = tmp_path / "test.m4a"
    path.write_bytes(b"fixture decoded by a native codec test double")
    waveform = torch.zeros(2, 4800)
    calls = []

    def native_load(value):
        calls.append(value)
        return waveform, 48000

    monkeypatch.setattr(app.torchaudio, "load", native_load)
    audio_io = app.SpaceAudioIO()
    assert audio_io.load(path)[0] is waveform
    info = audio_io.info(path)
    assert (info.sample_rate, info.num_frames, info.num_channels) == (48000, 4800, 2)
    assert calls == [path, path]


def test_environment_wiring_and_asr_order(monkeypatch):
    values = {
        "LLM_API_KEY": "test-llm-credential",
        "LLM_BASE_URL": "https://llm.invalid/v1/",
        "LLM_MODEL_NAME": "test-model",
        "TENCENTCLOUD_SECRET_ID": "test-cloud-id",
        "TENCENTCLOUD_SECRET_KEY": "test-cloud-key",
        "ASR_ENGINE_MODEL_TYPE": "16k_zh_en",
        "MODELSCOPE_API_KEY": "test-modelscope-token",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    clients = []
    monkeypatch.setattr(app.prompt_enhancer, "OpenAI", lambda **kw: clients.append(kw))
    calls = []

    class Cloud:
        def __init__(self, **kwargs):
            calls.append(("cloud_init", kwargs))

        def transcribe(self, path):
            calls.append("cloud")
            return app.prompt_enhancer.ASRCall(
                "cloud", None, None, None, "test failure"
            )

    class Local:
        def transcribe(self, path):
            calls.append("cpu")
            return app.prompt_enhancer.ASRCall("local", "test transcript", "en", None)

    monkeypatch.setattr(app.prompt_enhancer, "TencentCloudRecordingASR", Cloud)
    monkeypatch.setattr(app.prompt_enhancer, "SenseVoiceSmallASR", Local)
    enhancer = app.prompt_enhancer.PromptEnhancer()
    assert clients[0]["api_key"] == values["LLM_API_KEY"]
    assert clients[0]["base_url"] == "https://llm.invalid/v1"
    assert enhancer.llm_model == values["LLM_MODEL_NAME"]
    assert enhancer._transcribe("not-read.wav").text == "test transcript"
    assert calls[0][1]["secret_id"] == values["TENCENTCLOUD_SECRET_ID"]
    assert calls[0][1]["secret_key"] == values["TENCENTCLOUD_SECRET_KEY"]
    assert calls[0][1]["engine_model_type"] == values["ASR_ENGINE_MODEL_TYPE"]
    assert calls[1:] == ["cloud", "cpu"]
    assert app._modelscope_token() == values["MODELSCOPE_API_KEY"]


def test_missing_llm_settings_leave_manual_mode_available(monkeypatch, caplog):
    monkeypatch.setenv("LLM_API_KEY", "do-not-log-test-value")
    app._report_environment()
    assert "LLM_MODEL_NAME" in caplog.text
    assert "do-not-log-test-value" not in caplog.text
    with pytest.raises(gr.Error, match="LLM_MODEL_NAME"):
        app._safe_generate_with_pe(
            True, app.MODEL_LABEL_BASE, None, "hello", 0, "", "", 32, 2, None
        )
    monkeypatch.setattr(
        app, "_upstream_generate_with_pe", lambda *a, **kw: "manual works"
    )
    assert (
        app._safe_generate_with_pe(
            False, app.MODEL_LABEL_BASE, None, "hello", 1, "", "", 32, 2, None
        )
        == "manual works"
    )


@pytest.mark.parametrize(
    "duration", [None, 0, -1, float("nan"), float("inf"), -float("inf"), "", "invalid"]
)
@pytest.mark.parametrize("keyword_call", [False, True])
def test_manual_duration_required_before_any_generation(
    monkeypatch, duration, keyword_call
):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid duration must not reach PE, audio IO, or GPU generation.")

    monkeypatch.setattr(app, "_upstream_generate_with_pe", unexpected)
    request = {
        "use_pe": False,
        "variant": app.MODEL_LABEL_BASE,
        "audio": "reference.wav",
        "instruction": "Edit speech",
        "gen_seconds": duration,
        "ref_text": "reference text",
        "gen_text": "target text",
        "nfe": 32,
        "cfg": 2,
        "seed": None,
    }
    with pytest.raises(gr.Error, match="Duration is required"):
        if keyword_call:
            app._safe_generate_with_pe(**request)
        else:
            app._safe_generate_with_pe(*request.values())


@pytest.mark.parametrize("use_pe", [True, False])
def test_duration_control_policy(use_pe):
    update = app._duration_controls(use_pe)
    assert update["interactive"] is True
    assert ("0 = automatic" if use_pe else "required") in update["label"]
    # Toggling modes preserves a duration already entered by the user.
    assert "value" not in update


def test_provider_error_redaction(monkeypatch, caplog):
    for name in app.LLM_ENV_NAMES:
        monkeypatch.setenv(name, f"test-value-{name}")

    def fail(*args, **kwargs):
        raise gr.Error(
            "Provider rejected test-value-LLM_API_KEY at test-value-LLM_BASE_URL"
        )

    monkeypatch.setattr(app, "_upstream_generate_with_pe", fail)
    with pytest.raises(gr.Error) as error:
        app._safe_generate_with_pe(
            True, app.MODEL_LABEL_BASE, None, "hello", 0, "", "", 32, 2, None
        )
    assert "[redacted]" in error.value.message
    assert "test-value-" not in error.value.message
    assert "test-value-" not in caplog.text


def test_runtime_error_is_diagnosable_only_in_redacted_logs(monkeypatch, caplog):
    for name in app.SECRET_ENV_NAMES:
        monkeypatch.setenv(name, f"test-value-{name}")

    def fail(*args, **kwargs):
        raise RuntimeError(
            "Audio failed near test-value-TENCENTCLOUD_SECRET_KEY at "
            "test-value-LLM_BASE_URL"
        )

    monkeypatch.setattr(app, "_upstream_generate_with_pe", fail)
    with pytest.raises(gr.Error) as error:
        app._safe_generate_with_pe(
            True, app.MODEL_LABEL_BASE, "reference.wav", "hello", 1, "", "", 32, 2, None
        )
    assert error.value.message == (
        "Request failed (RuntimeError). Check the Studio configuration and provider availability."
    )
    assert "Audio failed near [redacted] at [redacted]" in caplog.text
    assert "Traceback (most recent call last)" in caplog.text
    assert "in fail" in caplog.text
    assert "test-value-" not in caplog.text


@pytest.mark.parametrize("difference", [None, "weights", "config", "failed_flash_load"])
def test_real_upstream_constructor_shares_components(
    releases, monkeypatch, difference, tmp_path
):
    calls = {"qwen": 0, "processor": 0, "vae": 0, "checkpoint": 0}

    class Module:
        def __init__(self, **kwargs):
            self.visual = object()
            self.__dict__.update(kwargs)

        def to(self, *args, **kwargs):
            return self

        def eval(self):
            return self

        def requires_grad_(self, value):
            return self

    def factory(name):
        def load(*args, **kwargs):
            calls[name] += 1
            return Module()

        return load

    thinker_factory = SimpleNamespace(from_pretrained=factory("qwen"))
    processor_factory = SimpleNamespace(from_pretrained=factory("processor"))
    vae_factory = factory("vae")
    monkeypatch.setattr(
        app.inference, "Qwen2_5OmniThinkerForConditionalGeneration", thinker_factory
    )
    monkeypatch.setattr(app.inference, "Qwen2_5OmniProcessor", processor_factory)
    monkeypatch.setattr(app.inference, "load_vae_model", vae_factory)
    monkeypatch.setattr(
        app.inference, "BigVGANFlowVAEConfig", SimpleNamespace(from_dict=lambda x: x)
    )
    monkeypatch.setattr(app.inference, "Flux2Edit", Module)
    monkeypatch.setattr(app.inference, "CFMEdit", Module)
    monkeypatch.setattr(app.torch.cuda, "is_available", lambda: True)

    def checkpoint(self, model, path):
        calls["checkpoint"] += 1
        if difference == "failed_flash_load" and self.is_flash:
            raise RuntimeError("simulated Flash checkpoint failure")

    monkeypatch.setattr(app.AukInfer, "_load_ema_weights", checkpoint)
    flash_files = releases[app.MODEL_LABEL_FLASH]
    if difference == "weights":
        flash_files.vae.write_bytes(b"different VAE")
    elif difference == "config":
        config = OmegaConf.load(flash_files.config)
        config.model.vae.target_sample_rate = 48000
        OmegaConf.save(config, flash_files.config)

    if difference == "failed_flash_load":
        with pytest.raises(RuntimeError, match="simulated Flash"):
            app._build_engines(releases, tmp_path / "qwen")
        assert (
            app.inference.Qwen2_5OmniThinkerForConditionalGeneration is thinker_factory
        )
        assert app.inference.Qwen2_5OmniProcessor is processor_factory
        assert app.inference.load_vae_model is vae_factory
        return

    engines = app._build_engines(releases, tmp_path / "qwen")
    base, flash = engines[app.MODEL_LABEL_BASE], engines[app.MODEL_LABEL_FLASH]
    assert base.model.text_encoder is flash.model.text_encoder
    assert base.model.text_processor is flash.model.text_processor
    assert (base.vae_model is flash.vae_model) == (difference is None)
    assert base.is_flash is False and flash.is_flash is True
    assert calls == {
        "qwen": 1,
        "processor": 1,
        "vae": 1 if difference is None else 2,
        "checkpoint": 2,
    }
    assert app.inference.Qwen2_5OmniThinkerForConditionalGeneration is thinker_factory
    assert app.inference.Qwen2_5OmniProcessor is processor_factory
    assert app.inference.load_vae_model is vae_factory


def test_real_gradio_ui_and_xgpu_boundary(releases, monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(
        app,
        "_download_release",
        lambda spec: releases[
            next(
                label
                for label, candidate in app.MODEL_SPECS.items()
                if spec == candidate
            )
        ],
    )

    class Engine:
        def generate(self, messages, **kwargs):
            events.append("generate")
            return torch.zeros(1, 2400), 24000

    monkeypatch.setattr(app, "_download_snapshot", lambda *a, **kw: tmp_path / "qwen")
    monkeypatch.setattr(
        app, "_build_engines", lambda r, q: {label: Engine() for label in r}
    )

    class Enhancer:
        def prepare(self, instruction, audio, *, target_duration=None):
            assert target_duration is None
            events.append("pe_prepare")
            return SimpleNamespace(
                audio=None,
                instruction="test instruction",
                gen_seconds=0.1,
                ref_text=None,
                gen_text="hello",
                task_type="instruct_tts",
                asr=None,
                llm_calls=[],
                cleanup=lambda: events.append("cleanup"),
            )

    monkeypatch.setattr(app.gradio_app, "PromptEnhancer", Enhancer)
    for name in app.LLM_ENV_NAMES:
        monkeypatch.setenv(name, f"fake-{name}")
    demo = app.create_demo()
    try:
        components = [component["props"] for component in demo.config["components"]]
        by_label = {props["label"]: props for props in components if "label" in props}
        assert by_label["Use Prompt Enhancer"]["value"] is True
        duration_label = app._duration_controls(True)["label"]
        assert by_label[duration_label]["interactive"] is True
        assert by_label[duration_label]["value"] == 0
        assert "Duration priority:" not in str(components)
        assert "Without Prompt Enhancer: enter a value greater than 0." in str(
            components
        )
        pe_component = next(
            c
            for c in demo.config["components"]
            if c["props"].get("label") == "Use Prompt Enhancer"
        )
        duration_component = next(
            c
            for c in demo.config["components"]
            if c["props"].get("label") == duration_label
        )
        toggle = next(fn for fn in demo.fns.values() if fn.fn is app._duration_controls)
        assert toggle.inputs[0]._id == pe_component["id"]
        assert toggle.outputs[0]._id == duration_component["id"]
        assert toggle.queue is False
        assert {"Summary", "ASR Content", "Instruction"} <= by_label.keys()
        assert by_label["Model"]["value"] == app.MODEL_LABEL_BASE
        assert by_label["NFE steps"]["value"] == 32
        assert by_label["CFG strength"]["value"] == 2.0
        api_names = {dep["api_name"] for dep in demo.config["dependencies"]}
        assert "run_generate_with_pe" in api_names
        flash_controls = app.gradio_app.update_sampling_controls(app.MODEL_LABEL_FLASH)
        assert (
            flash_controls[0]["value"] == 4
            and flash_controls[0]["interactive"] is False
        )
        assert flash_controls[1]["value"] == 0
        result, metadata = app.gradio_app.run_generate_with_pe(
            True, app.MODEL_LABEL_FLASH, None, "hello", 0, "", "", 4, 0, None
        )
        assert events == ["pe_prepare", "generate", "cleanup"]
        assert result[0] == 24000 and result[1].dtype == np.int16
        assert metadata["task"] == "instruct_tts"
        pe_outputs = app.gradio_app.update_pe_outputs(metadata)
        assert "Task: instruct_tts" in pe_outputs[0]
        assert pe_outputs[1] == ""
        assert pe_outputs[2] == "test instruction"
        events.clear()
        result, metadata = app.gradio_app.run_generate_with_pe(
            False, app.MODEL_LABEL_BASE, None, "hello", 0.1, "", "", 32, 2, None
        )
        assert events == ["generate"]
        assert metadata is None
        events.clear()

        def fail_generate(*args, **kwargs):
            raise RuntimeError("fake-LLM_API_KEY must never reach the browser")

        monkeypatch.setattr(Engine, "generate", fail_generate)
        with pytest.raises(gr.Error) as error:
            app.gradio_app.run_generate_with_pe(
                True, app.MODEL_LABEL_FLASH, None, "hello", 0, "", "", 4, 0, None
            )
        assert events == ["pe_prepare", "cleanup"]
        assert "fake-LLM_API_KEY" not in error.value.message
    finally:
        demo.close()


def test_all_upstream_demo_examples_are_packaged():
    total = 0
    for tasks in app.gradio_app.DEMO_EXAMPLE_GROUPS.values():
        for examples in tasks.values():
            resolved = app.gradio_app._resolve_demo_examples(examples)
            assert len(resolved) == len(examples)
            total += len(resolved)
            for audio, _, _ in resolved:
                assert audio is None or Path(audio).is_file()
    assert total > 0


def test_vendored_runtime_matches_upstream_commit():
    source = os.getenv("AUK_SOURCE_REPO")
    if not source:
        pytest.skip(
            "Set AUK_SOURCE_REPO to check provenance against the original checkout."
        )
    paths = subprocess.check_output(
        [
            "git",
            "-C",
            source,
            "ls-tree",
            "-r",
            "--name-only",
            app.UPSTREAM_COMMIT,
            "src/auk/__init__.py",
            "src/auk/infer",
            "src/auk/model",
            "assets/demo-input-audio",
        ],
        text=True,
    ).splitlines()
    for path in paths:
        expected = subprocess.check_output(
            ["git", "-C", source, "show", f"{app.UPSTREAM_COMMIT}:{path}"]
        )
        assert (ROOT / path).read_bytes() == expected, path
