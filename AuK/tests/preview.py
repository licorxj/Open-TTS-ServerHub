"""Local UI-only preview. All generated audio is a diagnostic tone, not AuK.

Run from the Studio directory: python tests/preview.py
No credentials, model downloads, or remote provider requests are used.
"""

import os
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

os.environ["GRADIO_ANALYTICS_ENABLED"] = "false"
os.environ.setdefault("AUK_MODELSCOPE_CACHE", "/tmp/auk-modelscope-preview/cache")
os.environ.setdefault(
    "AUK_MODELSCOPE_CREDENTIALS", "/tmp/auk-modelscope-preview/credentials"
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app


class PreviewEngine:
    def generate(self, messages, **kwargs):
        t = app.torch.arange(6000) / 24000
        return (0.02 * app.torch.sin(t * 440 * 2 * app.torch.pi)).unsqueeze(0), 24000


class PreviewEnhancer:
    def prepare(self, instruction, audio, *, target_duration=None):
        return SimpleNamespace(
            audio=None,
            instruction="LOCAL UI TEST ONLY: diagnostic tone, no speech model or API call.",
            gen_seconds=0.25,
            ref_text=None,
            gen_text="UI test",
            task_type="instruct_tts",
            asr=None,
            llm_calls=[
                SimpleNamespace(returned_model="UI test double", requested_model=None)
            ],
            cleanup=lambda: None,
        )


def main():
    with (
        TemporaryDirectory(prefix="auk-ui-preview-") as directory,
        ExitStack() as stack,
    ):
        for name in app.SECRET_ENV_NAMES:
            os.environ.pop(name, None)
        for name in app.LLM_ENV_NAMES:
            os.environ[name] = f"local-test-{name}"
        releases = {}
        for label, spec in app.MODEL_SPECS.items():
            checkpoint = Path(directory) / spec.checkpoint_name
            checkpoint.touch()
            releases[label] = app.ReleaseFiles(checkpoint, checkpoint, checkpoint)
        by_repo = {
            spec.repo_id: releases[label] for label, spec in app.MODEL_SPECS.items()
        }
        stack.enter_context(
            patch.object(app, "_download_release", lambda spec: by_repo[spec.repo_id])
        )
        stack.enter_context(
            patch.object(
                app,
                "_build_engines",
                lambda _, __: {label: PreviewEngine() for label in releases},
            )
        )
        stack.enter_context(
            patch.object(
                app, "_download_snapshot", lambda *args, **kwargs: Path(directory)
            )
        )
        stack.enter_context(
            patch.object(app.gradio_app, "PromptEnhancer", PreviewEnhancer)
        )
        demo = app.create_demo()
        with demo:
            app.gr.Markdown(
                "**LOCAL UI TEST ONLY — diagnostic tone; no AuK model or external API is running.**"
            )
        demo.launch(
            server_name="127.0.0.1",
            server_port=7862,
            theme=app.gr.themes.Soft(),
            css=app.gradio_app.DEMO_CSS,
        )


if __name__ == "__main__":
    main()
