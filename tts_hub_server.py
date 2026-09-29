"""TTS API 管家 · 启动入口

    python tts_hub_server.py                      # 默认 0.0.0.0:9000
    python tts_hub_server.py --port 9100          # 自定义端口
    python tts_hub_server.py --host 127.0.0.1     # 只监听本机

文档：http://<host>:<port>/docs        面板：http://<host>:<port>/ui
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 保证以脚本方式直接运行时也能 import tts_hub
sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402

from tts_hub.registry import get_registry  # noqa: E402
from tts_hub.server import app  # noqa: E402


def main() -> None:
    hub = get_registry().hub()
    parser = argparse.ArgumentParser(description="TTS API 管家")
    parser.add_argument("--host", type=str, default=None, help="监听地址（默认读 config/tts_hub.yaml）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认读 config/tts_hub.yaml）")
    parser.add_argument("--reload", action="store_true", help="开发模式：代码热重载")
    args = parser.parse_args()

    host = args.host or str(hub.get("host") or "0.0.0.0")
    port = args.port or int(hub.get("port") or 9000)

    ttl = float(hub.get("idle_ttl_seconds") or 0)
    idle_txt = f"{int(ttl)}s（{'%g' % (ttl / 60)} 分钟）" if ttl > 0 else "关闭"

    print("=" * 70)
    print("  LcTTS 管家 · 统一 TTS API 调度 by licor")
    print(f"  监听: http://{host}:{port}")
    print(f"  文档: http://127.0.0.1:{port}/docs     面板: http://127.0.0.1:{port}/ui")
    print(f"  已注册引擎: {len(get_registry().engine_names())} 个（GET /api/hub/engines）")
    print(f"  空闲自动卸载: {idle_txt}   （可 PUT /api/hub/config 在线修改）")
    print("  合成示例: POST /api/tts?model=voxcpm  (JSON 体原样透传给引擎)")
    print("=" * 70)

    uvicorn.run(app, host=host, port=port, reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
