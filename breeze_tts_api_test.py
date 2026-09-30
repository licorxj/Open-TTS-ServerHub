#!/usr/bin/env python3
"""Breeze-TTS-2 API 服务测试。

不加载模型（设置 BREEZE_TTS_SKIP_LOAD=1），覆盖：
- 路由注册
- 请求分类纯函数
- 健康检查（未加载时 model_loaded=false）
- 参数校验（缺参考音频 / 缺 ref_text / 设计缺指令 / 设计带参考音频）
- 模型自动下载注册表条目
"""

import os

os.environ["BREEZE_TTS_SKIP_LOAD"] = "1"

from fastapi.testclient import TestClient

import breeze_tts_api as m
from tools import models_manager


def test_routes_registered():
    paths = {r.path for r in m.app.routes}
    for p in [
        "/health",
        "/api/v1/voice/clone",
        "/api/v1/voice/design",
        "/api/v1/tasks/{task_id}",
        "/api/v1/voice/download/{task_id}",
        "/api/v1/tasks/{task_id}/progress",
    ]:
        assert p in paths, f"缺少路由: {p}"


def test_classify_request():
    assert m.classify_request("t", "a.wav", "ref", None) == "clone"
    assert m.classify_request("t", "a.wav", "ref", "serious") == "direction"
    assert m.classify_request("t", None, None, "warm voice") == "design"
    assert m.classify_request("t", None, None, None) == "plain"


def test_health_unloaded():
    with TestClient(m.app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["model_loaded"] is False
        assert body["sample_rate"] == 24000
        assert body["status"] == "degraded"


def test_clone_requires_ref():
    with TestClient(m.app) as client:
        r = client.post("/api/v1/voice/clone", data={"text": "你好"})
        assert r.status_code == 400


def test_clone_requires_ref_text():
    with TestClient(m.app) as client:
        r = client.post(
            "/api/v1/voice/clone",
            data={"text": "你好", "ref_audio_path": "some.wav"},
        )
        assert r.status_code == 400


def test_clone_ref_path_missing_file():
    with TestClient(m.app) as client:
        r = client.post(
            "/api/v1/voice/clone",
            data={"text": "你好", "ref_audio_path": "no_such_file.wav", "ref_text": "x"},
        )
        assert r.status_code == 400


def test_design_requires_instruction():
    with TestClient(m.app) as client:
        r = client.post("/api/v1/voice/design", data={"text": "你好"})
        assert r.status_code == 400


def test_design_rejects_ref():
    with TestClient(m.app) as client:
        r = client.post(
            "/api/v1/voice/design",
            data={"text": "你好", "instruction": "warm voice", "ref_audio_path": "x.wav"},
        )
        assert r.status_code == 400


def test_models_manager_registry():
    reg = models_manager.ModelsManager().list_models()
    assert "breeze_tts" in reg
    assert reg["breeze_tts"]["repo"] == "BreezeBlue/Breeze-TTS-2"
    assert reg["breeze_tts"]["source"] == "modelscope"
    assert reg["breeze_tts"]["local_dir"] == "models/Breeze-TTS-2"
    assert "audio_tokenizer/config.json" in reg["breeze_tts"]["required_files"]


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
