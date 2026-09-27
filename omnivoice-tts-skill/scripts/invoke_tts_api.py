#!/usr/bin/env python3
"""CLI for the local OmniVoice and VoxCPM FastAPI services."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import requests
except ImportError as exc:  # pragma: no cover - import failure is a runtime environment issue
    raise SystemExit(
        "Missing dependency 'requests'. Install it in the Python environment that runs this script."
    ) from exc


DEFAULT_BASE_URLS = {
    "vox": "http://localhost:8854",
    "omni": "http://localhost:8853",
}

FINAL_STATUSES = {"completed", "failed"}


def parse_bool(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "y", "on"}:
        return True
    if lowered in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"Invalid boolean value: {value}")


def compact_dict(payload: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, bool):
            result[key] = "true" if value else "false"
        else:
            result[key] = str(value)
    return result


def get_base_url(args: argparse.Namespace) -> str:
    if args.base_url:
        return args.base_url.rstrip("/")
    return DEFAULT_BASE_URLS[args.service]


def print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def handle_response(response: requests.Response) -> Any:
    try:
        payload = response.json()
    except ValueError:
        response.raise_for_status()
        return response.text

    if response.ok:
        return payload

    detail = payload.get("detail") if isinstance(payload, dict) else payload
    raise SystemExit(f"HTTP {response.status_code}: {detail}")


def do_get(url: str, timeout: float) -> Any:
    return handle_response(requests.get(url, timeout=timeout))


def submit_form(
    url: str,
    data: Dict[str, Any],
    timeout: float,
    ref_audio: Optional[str] = None,
) -> Any:
    form_data = compact_dict(data)
    if ref_audio:
        file_path = Path(ref_audio)
        if not file_path.is_file():
            raise SystemExit(f"Reference audio not found: {file_path}")
        with file_path.open("rb") as handle:
            files = {"ref_audio": (file_path.name, handle, "audio/wav")}
            response = requests.post(url, data=form_data, files=files, timeout=timeout)
            return handle_response(response)

    response = requests.post(url, data=form_data, timeout=timeout)
    return handle_response(response)


def require_ref_audio(args: argparse.Namespace) -> None:
    if not args.ref_audio and not args.ref_audio_path:
        raise SystemExit("Provide either --ref-audio or --ref-audio-path.")


def cmd_health(args: argparse.Namespace) -> int:
    payload = do_get(f"{get_base_url(args)}/health", args.timeout)
    print_json(payload)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    payload = do_get(f"{get_base_url(args)}/api/v1/tasks/{args.task_id}", args.timeout)
    print_json(payload)
    return 0


def wait_for_task(base_url: str, task_id: str, timeout: float, interval: float, deadline: float) -> Dict[str, Any]:
    while True:
        payload = do_get(f"{base_url}/api/v1/tasks/{task_id}", timeout)
        status = payload.get("status")
        progress = payload.get("progress")
        message = payload.get("message")
        print(f"status={status} progress={progress} message={message}")
        if status in FINAL_STATUSES:
            return payload
        if time.time() >= deadline:
            raise SystemExit("Timed out while waiting for task completion.")
        time.sleep(interval)


def cmd_wait(args: argparse.Namespace) -> int:
    deadline = time.time() + args.max_wait
    payload = wait_for_task(
        get_base_url(args),
        args.task_id,
        args.timeout,
        args.interval,
        deadline,
    )
    print_json(payload)
    return 0 if payload.get("status") == "completed" else 1


def cmd_download(args: argparse.Namespace) -> int:
    response = requests.get(
        f"{get_base_url(args)}/api/v1/voice/download/{args.task_id}",
        timeout=args.timeout,
    )
    if not response.ok:
        error = handle_response(response)
        print_json(error)
        return 1

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(response.content)
    print(str(output.resolve()))
    return 0


def cmd_clone(args: argparse.Namespace) -> int:
    require_ref_audio(args)
    data = {
        "text": args.text,
        "ref_audio_path": args.ref_audio_path,
        "language": args.language,
        "output_path": args.output_path,
        "instruct": args.instruct,
        "denoise": args.denoise,
    }
    if args.service == "vox":
        data.update(
            {
                "cfg_value": args.cfg_value,
                "inference_timesteps": args.inference_timesteps,
                "normalize": args.normalize,
            }
        )
    else:
        data.update(
            {
                "ref_text": args.ref_text,
                "num_steps": args.num_steps,
                "guidance_scale": args.guidance_scale,
                "speed": args.speed,
                "duration": args.duration,
                "max_workers": args.max_workers,
            }
        )

    payload = submit_form(
        f"{get_base_url(args)}/api/v1/voice/clone",
        data=data,
        timeout=args.timeout,
        ref_audio=args.ref_audio,
    )
    print_json(payload)
    return 0


def cmd_ultimate_clone(args: argparse.Namespace) -> int:
    if args.service != "vox":
        raise SystemExit("ultimate-clone is only available for --service vox.")
    require_ref_audio(args)
    data = {
        "text": args.text,
        "ref_audio_path": args.ref_audio_path,
        "prompt_text": args.prompt_text,
        "language": args.language,
        "output_path": args.output_path,
        "cfg_value": args.cfg_value,
        "inference_timesteps": args.inference_timesteps,
        "normalize": args.normalize,
        "denoise": args.denoise,
    }
    payload = submit_form(
        f"{get_base_url(args)}/api/v1/voice/ultimate_clone",
        data=data,
        timeout=args.timeout,
        ref_audio=args.ref_audio,
    )
    print_json(payload)
    return 0


def cmd_design(args: argparse.Namespace) -> int:
    data = {
        "text": args.text,
        "instruct": args.instruct,
        "language": args.language,
        "output_path": args.output_path,
    }
    if args.service == "vox":
        data.update(
            {
                "cfg_value": args.cfg_value,
                "inference_timesteps": args.inference_timesteps,
                "normalize": args.normalize,
            }
        )
    else:
        data.update(
            {
                "num_steps": args.num_steps,
                "guidance_scale": args.guidance_scale,
                "speed": args.speed,
                "duration": args.duration,
                "denoise": args.denoise,
            }
        )

    payload = submit_form(
        f"{get_base_url(args)}/api/v1/voice/design",
        data=data,
        timeout=args.timeout,
    )
    print_json(payload)
    return 0


def add_common_service_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--service", choices=sorted(DEFAULT_BASE_URLS), required=True)
    parser.add_argument("--base-url", help="Override the default base URL for the selected service.")
    parser.add_argument("--timeout", type=float, default=60.0, help="Single request timeout in seconds.")


def add_task_args(parser: argparse.ArgumentParser) -> None:
    add_common_service_args(parser)
    parser.add_argument("--task-id", required=True)


def add_clone_args(parser: argparse.ArgumentParser) -> None:
    add_common_service_args(parser)
    parser.add_argument("--text", required=True)
    parser.add_argument("--ref-audio", help="Upload a local audio file via multipart form.")
    parser.add_argument("--ref-audio-path", help="Server-side reference audio path.")
    parser.add_argument("--language")
    parser.add_argument("--output-path")
    parser.add_argument("--instruct")
    parser.add_argument("--denoise", type=parse_bool)
    parser.add_argument("--cfg-value", type=float)
    parser.add_argument("--inference-timesteps", type=int)
    parser.add_argument("--normalize", type=parse_bool)
    parser.add_argument("--ref-text")
    parser.add_argument("--num-steps", type=int)
    parser.add_argument("--guidance-scale", type=float)
    parser.add_argument("--speed", type=float)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--max-workers", type=int)


def add_design_args(parser: argparse.ArgumentParser) -> None:
    add_common_service_args(parser)
    parser.add_argument("--text", required=True)
    parser.add_argument("--instruct", required=True)
    parser.add_argument("--language")
    parser.add_argument("--output-path")
    parser.add_argument("--cfg-value", type=float)
    parser.add_argument("--inference-timesteps", type=int)
    parser.add_argument("--normalize", type=parse_bool)
    parser.add_argument("--num-steps", type=int)
    parser.add_argument("--guidance-scale", type=float)
    parser.add_argument("--speed", type=float)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--denoise", type=parse_bool)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    health = subparsers.add_parser("health", help="Check service health.")
    add_common_service_args(health)
    health.set_defaults(func=cmd_health)

    status = subparsers.add_parser("status", help="Get task status.")
    add_task_args(status)
    status.set_defaults(func=cmd_status)

    wait = subparsers.add_parser("wait", help="Poll a task until it finishes.")
    add_task_args(wait)
    wait.add_argument("--interval", type=float, default=1.0, help="Polling interval in seconds.")
    wait.add_argument("--max-wait", type=float, default=1800.0, help="Maximum wait time in seconds.")
    wait.set_defaults(func=cmd_wait)

    download = subparsers.add_parser("download", help="Download generated audio by task ID.")
    add_task_args(download)
    download.add_argument("--output", required=True, help="Output file path for the downloaded WAV.")
    download.set_defaults(func=cmd_download)

    clone = subparsers.add_parser("clone", help="Submit a clone task.")
    add_clone_args(clone)
    clone.set_defaults(func=cmd_clone)

    ultimate = subparsers.add_parser("ultimate-clone", help="Submit a VoxCPM ultimate clone task.")
    add_clone_args(ultimate)
    ultimate.add_argument("--prompt-text")
    ultimate.set_defaults(func=cmd_ultimate_clone)

    design = subparsers.add_parser("design", help="Submit a design task.")
    add_design_args(design)
    design.set_defaults(func=cmd_design)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
