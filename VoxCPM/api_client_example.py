tp-c9703pt8qx5hnfjy6heflgxcs11alo26wozj6tdm65a13re0#!/usr/bin/env python3
"""
VoxCPM API 客户端示例

演示如何调用 VoxCPM API 的各种接口：
- 声音设计 (Voice Design)
- 可控克隆 (Controllable Cloning)
- 极致克隆 (Ultimate Cloning)
- 批量处理
- 任务管理

Usage:
    python api_client_example.py [--server http://localhost:8809]
"""

import argparse
import time
import requests
from pathlib import Path

# 服务器地址
BASE_URL = "http://localhost:8809"


def check_server(base_url: str):
    """检查服务器状态"""
    print("=" * 60)
    print("检查服务器状态...")
    resp = requests.get(f"{base_url}/")
    info = resp.json()
    print(f"  模型已加载: {info['model_loaded']}")
    print(f"  设备: {info['device']}")
    print(f"  采样率: {info['sampling_rate']}Hz")
    print(f"  模型类型: {info.get('model_type', 'unknown')}")
    print(f"  最大工作线程: {info['max_workers']}")
    print(f"  GPU 并发数: {info['gpu_concurrency']}")
    print("=" * 60)
    return info


def wait_for_task(base_url: str, task_id: str, timeout: int = 300):
    """等待任务完成并返回结果"""
    start = time.time()
    while time.time() - start < timeout:
        resp = requests.get(f"{base_url}/api/v1/tasks/{task_id}")
        task = resp.json()
        status = task["status"]
        progress = task.get("progress", 0)
        message = task.get("message", "")

        print(f"  [{status}] {progress:.1f}% - {message}")

        if status == "completed":
            return task
        elif status == "failed":
            print(f"  ❌ 任务失败: {task.get('error', 'unknown')}")
            return task

        time.sleep(1)

    print(f"  ⏰ 等待超时 ({timeout}s)")
    return None


def download_audio(base_url: str, task_id: str, output_path: str):
    """下载生成的音频"""
    resp = requests.get(f"{base_url}/api/v1/voice/download/{task_id}")
    if resp.status_code == 200:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(resp.content)
        print(f"  ✅ 音频已保存到: {output_path}")
    else:
        print(f"  ❌ 下载失败: {resp.status_code} - {resp.text}")


# ============== 声音设计示例 ==============

def example_voice_design(base_url: str):
    """声音设计示例 — 无需参考音频"""
    print("\n" + "=" * 60)
    print("🎨 声音设计示例")
    print("=" * 60)

    resp = requests.post(
        f"{base_url}/api/v1/voice/design",
        data={
            "text": "VoxCPM2 is a creative multilingual TTS model designed to generate highly realistic speech.",
            "instruct": "A young woman with a warm, gentle voice. Speaks slowly with a soft tone.",
            "cfg_value": 2.0,
            "inference_timesteps": 10,
        },
    )
    result = resp.json()
    task_id = result["task_id"]
    print(f"任务已创建: {task_id}")

    task = wait_for_task(base_url, task_id)
    if task and task["status"] == "completed":
        download_audio(base_url, task_id, "outputs/example_design.wav")
        print(f"  RTF: {task['rtf']:.4f}")
        print(f"  音频时长: {task['audio_duration']:.2f}s")
        print(f"  推理时间: {task['inference_time']:.2f}s")


# ============== 可控克隆示例 ==============

def example_voice_clone(base_url: str, ref_audio_path: str):
    """可控克隆示例 — 参考音频 + 可选风格控制"""
    print("\n" + "=" * 60)
    print("🎛️ 可控克隆示例")
    print("=" * 60)

    if not Path(ref_audio_path).exists():
        print(f"  ⚠️ 参考音频不存在: {ref_audio_path}，跳过此示例")
        return

    with open(ref_audio_path, "rb") as f:
        resp = requests.post(
            f"{base_url}/api/v1/voice/clone",
            data={
                "text": "这是一段使用声音克隆技术生成的语音。",
                "instruct": "温柔地",
                "speed": 0.75,
                "cfg_value": 2.0,
                "inference_timesteps": 10,
                "denoise": True,
            },
            files={"ref_audio": ("reference.wav", f, "audio/wav")},
        )
    result = resp.json()
    task_id = result["task_id"]
    print(f"任务已创建: {task_id}")

    task = wait_for_task(base_url, task_id)
    if task and task["status"] == "completed":
        download_audio(base_url, task_id, "outputs/example_clone.wav")
        print(f"  RTF: {task['rtf']:.4f}")
        print(f"  音频时长: {task['audio_duration']:.2f}s")
        print(f"  推理时间: {task['inference_time']:.2f}s")


# ============== 极致克隆示例 ==============

def example_ultimate_clone(base_url: str, ref_audio_path: str):
    """极致克隆示例 — 音频续写模式"""
    print("\n" + "=" * 60)
    print("🎙️ 极致克隆示例")
    print("=" * 60)

    if not Path(ref_audio_path).exists():
        print(f"  ⚠️ 参考音频不存在: {ref_audio_path}，跳过此示例")
        return

    with open(ref_audio_path, "rb") as f:
        resp = requests.post(
            f"{base_url}/api/v1/voice/ultimate_clone",
            data={
                "text": "然后我继续说下去，声音应该和前面完全一致。",
                "prompt_text": "",  # 留空则自动 ASR 识别
                "cfg_value": 2.0,
                "inference_timesteps": 10,
                "denoise": True,
            },
            files={"ref_audio": ("reference.wav", f, "audio/wav")},
        )
    result = resp.json()
    task_id = result["task_id"]
    print(f"任务已创建: {task_id}")

    task = wait_for_task(base_url, task_id)
    if task and task["status"] == "completed":
        download_audio(base_url, task_id, "outputs/example_ultimate_clone.wav")
        print(f"  RTF: {task['rtf']:.4f}")
        print(f"  音频时长: {task['audio_duration']:.2f}s")
        print(f"  推理时间: {task['inference_time']:.2f}s")


# ============== 批量处理示例 ==============

def example_batch(base_url: str):
    """批量处理示例"""
    print("\n" + "=" * 60)
    print("📦 批量处理示例")
    print("=" * 60)

    resp = requests.post(
        f"{base_url}/api/v1/voice/batch",
        json={
            "items": [
                {
                    "text": "Hello, this is the first batch item.",
                    "instruct": "A warm young woman",
                },
                {
                    "text": "This is the second batch item with a different voice.",
                    "instruct": "A deep male voice, authoritative",
                },
                {
                    "text": "And the third one, a cheerful child.",
                    "instruct": "A cheerful child, excited",
                },
            ],
            "cfg_value": 2.0,
            "inference_timesteps": 10,
            "max_workers": 2,
        },
    )
    result = resp.json()
    batch_id = result["batch_id"]
    print(f"批量任务已创建: {batch_id}")
    print(f"子任务数: {result['total_items']}")
    print(f"子任务 ID: {result['task_ids']}")

    # 等待批量任务完成
    start = time.time()
    while time.time() - start < 600:
        resp = requests.get(f"{base_url}/api/v1/voice/batch/{batch_id}")
        batch = resp.json()
        print(
            f"  [{batch['status']}] "
            f"完成: {batch['completed_items']}/{batch['total_items']}, "
            f"失败: {batch['failed_items']}"
        )

        if batch["status"] in ("completed", "failed", "partial"):
            if batch.get("rtf_avg"):
                print(f"  平均 RTF: {batch['rtf_avg']:.4f}")
            if batch.get("duration_total"):
                print(f"  总音频时长: {batch['duration_total']:.2f}s")
            break

        time.sleep(2)

    # 下载批量音频
    resp = requests.get(f"{base_url}/api/v1/voice/batch/{batch_id}/download")
    if resp.status_code == 200:
        zip_path = f"outputs/batch_{batch_id}.zip"
        Path(zip_path).parent.mkdir(parents=True, exist_ok=True)
        with open(zip_path, "wb") as f:
            f.write(resp.content)
        print(f"  ✅ 批量音频已保存到: {zip_path}")


# ============== 主函数 ==============

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VoxCPM API 客户端示例")
    parser.add_argument(
        "--server", default="http://localhost:8809", help="API 服务器地址"
    )
    parser.add_argument(
        "--ref-audio",
        default="examples/reference_speaker.wav",
        help="参考音频文件路径（用于克隆示例）",
    )
    parser.add_argument(
        "--example",
        choices=["all", "design", "clone", "ultimate", "batch"],
        default="all",
        help="要运行的示例",
    )
    args = parser.parse_args()

    base_url = args.server.rstrip("/")

    # 检查服务器
    try:
        check_server(base_url)
    except requests.ConnectionError:
        print(f"❌ 无法连接到服务器: {base_url}")
        print("请确保 API 服务器已启动: python api_server.py")
        exit(1)

    # 运行示例
    if args.example in ("all", "design"):
        example_voice_design(base_url)

    if args.example in ("all", "clone"):
        example_voice_clone(base_url, args.ref_audio)

    if args.example in ("all", "ultimate"):
        example_ultimate_clone(base_url, args.ref_audio)

    if args.example in ("all", "batch"):
        example_batch(base_url)

    print("\n✅ 所有示例运行完成！")