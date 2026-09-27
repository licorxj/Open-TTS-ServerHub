#!/usr/bin/env python3
"""
OmniVoice API 调用示例

提供多种调用方式示例：
1. 基础 HTTP 调用（声音克隆/设计）
2. WebSocket 实时进度监控
3. 批量处理
4. 异步并发请求

Usage:
    python api_client_example.py --mode clone --server http://localhost:8853
    python api_client_example.py --mode design --server http://localhost:8853
    python api_client_example.py --mode websocket --task-id <task_id>
    python api_client_example.py --mode batch --server http://localhost:8853
"""

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import requests
import websockets


# 默认服务器地址
DEFAULT_SERVER = "http://localhost:8853"
DEFAULT_WS_SERVER = "ws://localhost:8853"


class OmniVoiceAPIClient:
    """OmniVoice API 客户端"""
    
    def __init__(self, server_url: str = DEFAULT_SERVER):
        self.server_url = server_url.rstrip("/")
        self.ws_url = server_url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/")
    
    def check_server(self) -> bool:
        """检查服务器状态"""
        try:
            response = requests.get(f"{self.server_url}/", timeout=5)
            if response.status_code == 200:
                info = response.json()
                print(f"✅ 服务器连接成功")
                print(f"   模型加载状态: {'已加载' if info.get('model_loaded') else '未加载'}")
                print(f"   设备: {info.get('device', 'unknown')}")
                print(f"   数据类型: {info.get('dtype', 'unknown')}")
                print(f"   采样率: {info.get('sampling_rate', 0)}Hz")
                print(f"   最大工作线程数: {info.get('max_workers', 0)}")
                return True
            return False
        except Exception as e:
            print(f"❌ 无法连接服务器: {e}")
            return False
    
    def clone_voice(
        self,
        text: str,
        ref_audio_path: str,
        ref_text: Optional[str] = None,
        language: Optional[str] = None,
        output_path: Optional[str] = None,
        num_steps: int = 32,
        guidance_scale: float = 2.0,
        speed: float = 1.0,
        duration: Optional[float] = None,
        denoise: bool = True,
        max_workers: int = 1,
    ) -> dict:
        """
        声音克隆
        
        Args:
            text: 要合成的文本
            ref_audio_path: 参考音频文件路径
            ref_text: 参考音频的文本（可选）
            language: 语言代码
            output_path: 输出路径（可选）
            num_steps: 扩散步数
            guidance_scale: 引导尺度
            speed: 语速
            duration: 固定时长
            denoise: 是否去噪
            max_workers: 工作线程数
        
        Returns:
            包含 task_id 的字典
        """
        if not os.path.exists(ref_audio_path):
            raise FileNotFoundError(f"参考音频不存在: {ref_audio_path}")
        
        url = f"{self.server_url}/api/v1/voice/clone"
        
        # 准备表单数据
        data = {
            "text": text,
            "ref_text": ref_text or "",
            "language": language or "",
            "output_path": output_path or "",
            "num_steps": num_steps,
            "guidance_scale": guidance_scale,
            "speed": speed,
            "duration": duration if duration else "",
            "denoise": denoise,
            "max_workers": max_workers,
        }
        
        # 移除空值
        data = {k: v for k, v in data.items() if v != ""}
        
        # 上传文件
        with open(ref_audio_path, "rb") as f:
            files = {"ref_audio": (os.path.basename(ref_audio_path), f, "audio/wav")}
            response = requests.post(url, data=data, files=files, timeout=30)
        
        if response.status_code != 200:
            raise Exception(f"请求失败: {response.status_code} - {response.text}")
        
        return response.json()
    
    def design_voice(
        self,
        text: str,
        instruct: str,
        language: Optional[str] = None,
        output_path: Optional[str] = None,
        num_steps: int = 32,
        guidance_scale: float = 2.0,
        speed: float = 1.0,
        duration: Optional[float] = None,
        denoise: bool = True,
        max_workers: int = 1,
    ) -> dict:
        """
        声音设计
        
        Args:
            text: 要合成的文本
            instruct: 声音描述指令
            language: 语言代码
            output_path: 输出路径
            num_steps: 扩散步数
            guidance_scale: 引导尺度
            speed: 语速
            duration: 固定时长
            denoise: 是否去噪
            max_workers: 工作线程数
        
        Returns:
            包含 task_id 的字典
        """
        url = f"{self.server_url}/api/v1/voice/design"
        
        data = {
            "text": text,
            "instruct": instruct,
            "language": language or "",
            "output_path": output_path or "",
            "num_steps": num_steps,
            "guidance_scale": guidance_scale,
            "speed": speed,
            "duration": duration if duration else "",
            "denoise": denoise,
            "max_workers": max_workers,
        }
        
        # 移除空值
        data = {k: v for k, v in data.items() if v != ""}
        
        response = requests.post(url, data=data, timeout=30)
        
        if response.status_code != 200:
            raise Exception(f"请求失败: {response.status_code} - {response.text}")
        
        return response.json()
    
    def get_task_status(self, task_id: str) -> dict:
        """获取任务状态"""
        url = f"{self.server_url}/api/v1/tasks/{task_id}"
        response = requests.get(url, timeout=10)
        
        if response.status_code != 200:
            raise Exception(f"查询失败: {response.status_code} - {response.text}")
        
        return response.json()
    
    def download_audio(self, task_id: str, save_path: str) -> str:
        """下载生成的音频"""
        url = f"{self.server_url}/api/v1/voice/download/{task_id}"
        response = requests.get(url, stream=True, timeout=60)
        
        if response.status_code != 200:
            raise Exception(f"下载失败: {response.status_code} - {response.text}")
        
        # 保存文件
        with open(save_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        return save_path
    
    async def monitor_progress_websocket(
        self,
        task_id: str,
        on_progress: Optional[callable] = None,
        on_complete: Optional[callable] = None,
        on_error: Optional[callable] = None,
    ) -> dict:
        """
        使用 WebSocket 监控任务进度
        
        Args:
            task_id: 任务ID
            on_progress: 进度回调函数 (progress, message)
            on_complete: 完成回调函数 (result)
            on_error: 错误回调函数 (error)
        
        Returns:
            最终任务状态
        """
        ws_url = f"{self.ws_url}/ws/tasks/{task_id}"
        
        print(f"🔗 连接到 WebSocket: {ws_url}")
        
        async with websockets.connect(ws_url) as websocket:
            while True:
                try:
                    message = await websocket.recv()
                    data = json.loads(message)
                    
                    if "error" in data:
                        if on_error:
                            on_error(data["error"])
                        return data
                    
                    # 显示进度
                    progress = data.get("progress", 0)
                    status = data.get("status", "")
                    msg = data.get("message", "")
                    rtf = data.get("rtf")
                    
                    # 格式化 RTF 显示
                    rtf_str = f", RTF: {rtf:.4f}" if rtf else ""
                    
                    print(f"\r📊 进度: {progress:5.1f}% | 状态: {status:10s} | {msg}{rtf_str}", end="", flush=True)
                    
                    if on_progress:
                        on_progress(progress, msg)
                    
                    # 任务完成或失败
                    if status in ("completed", "failed"):
                        print()  # 换行
                        
                        if status == "completed" and on_complete:
                            on_complete(data)
                        elif status == "failed" and on_error:
                            on_error(data.get("error", "Unknown error"))
                        
                        return data
                    
                except websockets.exceptions.ConnectionClosed:
                    print("\n⚠️ WebSocket 连接已关闭")
                    break
                except Exception as e:
                    print(f"\n❌ WebSocket 错误: {e}")
                    if on_error:
                        on_error(str(e))
                    break
        
        return {}
    
    def wait_for_completion(
        self,
        task_id: str,
        poll_interval: float = 1.0,
        timeout: float = 300.0,
    ) -> dict:
        """
        轮询等待任务完成
        
        Args:
            task_id: 任务ID
            poll_interval: 轮询间隔（秒）
            timeout: 超时时间（秒）
        
        Returns:
            最终任务状态
        """
        start_time = time.time()
        
        while True:
            status = self.get_task_status(task_id)
            current_status = status.get("status", "")
            progress = status.get("progress", 0)
            message = status.get("message", "")
            rtf = status.get("rtf")
            
            # 格式化 RTF 显示
            rtf_str = f", RTF: {rtf:.4f}" if rtf else ""
            
            print(f"\r📊 进度: {progress:5.1f}% | 状态: {current_status:10s} | {message}{rtf_str}", end="", flush=True)
            
            if current_status in ("completed", "failed"):
                print()  # 换行
                return status
            
            # 检查超时
            if time.time() - start_time > timeout:
                print("\n⏱️ 等待超时")
                raise TimeoutError(f"任务 {task_id} 执行超时")
            
            time.sleep(poll_interval)


def example_clone_voice(client: OmniVoiceAPIClient, args):
    """声音克隆示例"""
    print("\n" + "=" * 60)
    print("🎙️ 声音克隆示例")
    print("=" * 60)
    
    # 参数设置
    text = args.text or "Hello, this is a test of voice cloning using OmniVoice API."
    ref_audio = args.ref_audio or "ref.wav"
    ref_text = args.ref_text
    language = args.language
    
    print(f"📝 合成文本: {text}")
    print(f"🎵 参考音频: {ref_audio}")
    print(f"📄 参考文本: {ref_text or '自动识别'}")
    print(f"🌐 语言: {language or '自动检测'}")
    print(f"⚙️ 扩散步数: {args.num_steps}")
    print(f"⚙️ 工作线程: {args.max_workers}")
    print("-" * 60)
    
    try:
        # 1. 提交任务
        print("📤 提交声音克隆任务...")
        result = client.clone_voice(
            text=text,
            ref_audio_path=ref_audio,
            ref_text=ref_text,
            language=language,
            num_steps=args.num_steps,
            guidance_scale=args.guidance_scale,
            speed=args.speed,
            duration=args.duration,
            max_workers=args.max_workers,
        )
        
        task_id = result.get("task_id")
        print(f"✅ 任务已创建: {task_id}")
        
        # 2. 监控进度
        print("\n⏳ 等待任务完成...")
        
        if args.use_websocket:
            # 使用 WebSocket 实时监控
            final_status = asyncio.run(client.monitor_progress_websocket(task_id))
        else:
            # 使用轮询
            final_status = client.wait_for_completion(task_id)
        
        # 3. 下载结果
        if final_status.get("status") == "completed":
            output_file = args.output or f"output_clone_{task_id[:8]}.wav"
            print(f"\n📥 下载音频到: {output_file}")
            client.download_audio(task_id, output_file)
            
            # 显示统计信息
            print("\n📈 生成统计:")
            print(f"   音频时长: {final_status.get('audio_duration', 0):.2f} 秒")
            print(f"   推理时间: {final_status.get('inference_time', 0):.2f} 秒")
            print(f"   RTF 速率: {final_status.get('rtf', 0):.4f}")
            print(f"   实时比: {1/final_status.get('rtf', 1):.1f}x")
            print(f"\n✨ 完成！输出文件: {output_file}")
        else:
            print(f"\n❌ 任务失败: {final_status.get('error', 'Unknown error')}")
    
    except FileNotFoundError as e:
        print(f"\n❌ 错误: {e}")
        print("提示: 请确保参考音频文件存在，或使用 --ref-audio 指定正确的路径")
    except Exception as e:
        print(f"\n❌ 错误: {e}")


def example_design_voice(client: OmniVoiceAPIClient, args):
    """声音设计示例"""
    print("\n" + "=" * 60)
    print("🎨 声音设计示例")
    print("=" * 60)
    
    # 参数设置
    text = args.text or "Hello, this is a test of voice design using OmniVoice API."
    instruct = args.instruct or "female, low pitch, british accent"
    language = args.language
    
    print(f"📝 合成文本: {text}")
    print(f"🎨 声音指令: {instruct}")
    print(f"🌐 语言: {language or '自动检测'}")
    print(f"⚙️ 扩散步数: {args.num_steps}")
    print(f"⚙️ 工作线程: {args.max_workers}")
    print("-" * 60)
    
    # 显示可用指令
    print("\n💡 可用声音指令:")
    print("   性别: male, female")
    print("   年龄: child, teenager, young adult, middle-aged, elderly")
    print("   音调: very low pitch, low pitch, moderate pitch, high pitch, very high pitch")
    print("   风格: whisper")
    print("   英语口音: american accent, british accent, australian accent, indian accent")
    print("   汉语方言: 四川话, 陕西话, 河南话, 东北话, ...")
    print("-" * 60)
    
    try:
        # 1. 提交任务
        print("📤 提交声音设计任务...")
        result = client.design_voice(
            text=text,
            instruct=instruct,
            language=language,
            num_steps=args.num_steps,
            guidance_scale=args.guidance_scale,
            speed=args.speed,
            duration=args.duration,
            max_workers=args.max_workers,
        )
        
        task_id = result.get("task_id")
        print(f"✅ 任务已创建: {task_id}")
        
        # 2. 监控进度
        print("\n⏳ 等待任务完成...")
        
        if args.use_websocket:
            # 使用 WebSocket 实时监控
            final_status = asyncio.run(client.monitor_progress_websocket(task_id))
        else:
            # 使用轮询
            final_status = client.wait_for_completion(task_id)
        
        # 3. 下载结果
        if final_status.get("status") == "completed":
            output_file = args.output or f"output_design_{task_id[:8]}.wav"
            print(f"\n📥 下载音频到: {output_file}")
            client.download_audio(task_id, output_file)
            
            # 显示统计信息
            print("\n📈 生成统计:")
            print(f"   音频时长: {final_status.get('audio_duration', 0):.2f} 秒")
            print(f"   推理时间: {final_status.get('inference_time', 0):.2f} 秒")
            print(f"   RTF 速率: {final_status.get('rtf', 0):.4f}")
            print(f"   实时比: {1/final_status.get('rtf', 1):.1f}x")
            print(f"\n✨ 完成！输出文件: {output_file}")
        else:
            print(f"\n❌ 任务失败: {final_status.get('error', 'Unknown error')}")
    
    except Exception as e:
        print(f"\n❌ 错误: {e}")


def example_websocket_monitor(client: OmniVoiceAPIClient, args):
    """WebSocket 进度监控示例"""
    print("\n" + "=" * 60)
    print("🔌 WebSocket 实时进度监控示例")
    print("=" * 60)
    
    task_id = args.task_id
    if not task_id:
        print("❌ 请提供任务ID: --task-id <task_id>")
        return
    
    print(f"📋 监控任务: {task_id}")
    
    def on_progress(progress, message):
        # 这里可以添加自定义处理逻辑
        pass
    
    def on_complete(result):
        print(f"\n✅ 任务完成!")
        print(f"   RTF: {result.get('rtf', 0):.4f}")
        print(f"   音频时长: {result.get('audio_duration', 0):.2f}s")
    
    def on_error(error):
        print(f"\n❌ 任务错误: {error}")
    
    asyncio.run(client.monitor_progress_websocket(
        task_id,
        on_progress=on_progress,
        on_complete=on_complete,
        on_error=on_error,
    ))


def example_batch_process(client: OmniVoiceAPIClient, args):
    """批量处理示例"""
    print("\n" + "=" * 60)
    print("📦 批量处理示例")
    print("=" * 60)
    print("此功能演示如何并发提交多个任务")
    
    # 示例：提交多个声音设计任务
    texts = [
        "Hello, this is the first sentence.",
        "This is the second sentence with a different content.",
        "And here is the third sentence for testing.",
    ]
    
    instructs = [
        "male, american accent",
        "female, british accent",
        "male, low pitch",
    ]
    
    task_ids = []
    
    print("\n📤 提交批量任务...")
    for i, (text, instruct) in enumerate(zip(texts, instructs)):
        try:
            result = client.design_voice(
                text=text,
                instruct=instruct,
                num_steps=args.num_steps,
            )
            task_id = result.get("task_id")
            task_ids.append(task_id)
            print(f"  任务 {i+1}: {task_id} - {instruct}")
        except Exception as e:
            print(f"  任务 {i+1} 提交失败: {e}")
    
    print(f"\n⏳ 等待 {len(task_ids)} 个任务完成...")
    print("提示: 可以使用 --mode websocket --task-id <id> 监控特定任务")


def example_api_documentation():
    """打印 API 文档"""
    print("""
╔══════════════════════════════════════════════════════════════════════════════╗
║                        OmniVoice API 接口文档                                 ║
╠══════════════════════════════════════════════════════════════════════════════╣

📌 基础信息
   - 服务器: http://localhost:8853
   - API 文档: http://localhost:8853/docs (Swagger UI)
   - 替代文档: http://localhost:8853/redoc (ReDoc)

📌 接口列表

1️⃣  获取服务器信息
   GET /
   返回: {"model_loaded": true, "device": "cuda", "sampling_rate": 24000, ...}

2️⃣  声音克隆
   POST /api/v1/voice/clone
   表单参数:
     - text (必填): 要合成的文本
     - ref_audio (必填): 参考音频文件 (multipart/form-data)
     - ref_text (可选): 参考音频的文本
     - language (可选): 语言代码
     - output_path (可选): 自定义输出路径
     - num_steps (默认32): 扩散步数
     - guidance_scale (默认2.0): 引导尺度
     - speed (默认1.0): 语速因子
     - duration (可选): 固定时长(秒)
     - denoise (默认True): 是否去噪
     - max_workers (默认1): 工作线程数
   返回: {"task_id": "uuid", "status": "pending", "message": "..."}

3️⃣  声音设计
   POST /api/v1/voice/design
   表单参数:
     - text (必填): 要合成的文本
     - instruct (必填): 声音描述指令
     - language (可选): 语言代码
     - output_path (可选): 自定义输出路径
     - num_steps (默认32): 扩散步数
     - guidance_scale (默认2.0): 引导尺度
     - speed (默认1.0): 语速因子
     - duration (可选): 固定时长(秒)
     - denoise (默认True): 是否去噪
     - max_workers (默认1): 工作线程数
   返回: {"task_id": "uuid", "status": "pending", "message": "..."}

4️⃣  获取任务状态
   GET /api/v1/tasks/{task_id}
   返回: 完整的任务状态信息，包括进度、RTF、音频时长等

5️⃣  列出所有任务
   GET /api/v1/tasks?status=&limit=20&offset=0
   查询参数:
     - status: 按状态筛选 (pending/running/completed/failed)
     - limit: 返回数量 (默认20)
     - offset: 偏移量 (默认0)
   返回: 任务列表

6️⃣  删除任务
   DELETE /api/v1/tasks/{task_id}
   返回: {"message": "任务已删除", "task_id": "..."}

7️⃣  下载音频
   GET /api/v1/voice/download/{task_id}
   返回: 音频文件 (WAV 格式)

8️⃣  WebSocket 实时进度
   WS /ws/tasks/{task_id}
   推送格式: {"task_id": "...", "status": "...", "progress": 50.0, "rtf": 0.025}

9️⃣  SSE 实时进度 (备选)
   GET /api/v1/tasks/{task_id}/progress
   返回: text/event-stream

📌 声音设计指令参考

   性别: male, female
   年龄: child, teenager, young adult, middle-aged, elderly
   音调: very low pitch, low pitch, moderate pitch, high pitch, very high pitch
   风格: whisper
   英语口音: american accent, british accent, australian accent, 
            indian accent, scottish accent, irish accent
   汉语方言: 四川话, 陕西话, 河南话, 东北话, 山东话, 甘肃话, 宁夏话

   示例组合:
     - "female, low pitch, british accent"
     - "male, middle-aged, american accent"
     - "female, child, very high pitch"
     - "男，中年，四川话"
     - "女，青年，陕西话，耳语"

📌 Python 调用示例

   import requests

   # 声音克隆
   with open("ref.wav", "rb") as f:
       files = {"ref_audio": f}
       data = {"text": "Hello world", "language": "en"}
       response = requests.post("http://localhost:8853/api/v1/voice/clone", 
                               files=files, data=data)
   task_id = response.json()["task_id"]

   # 查询状态
   status = requests.get(f"http://localhost:8853/api/v1/tasks/{task_id}").json()

   # 下载音频
   audio = requests.get(f"http://localhost:8853/api/v1/voice/download/{task_id}")
   with open("output.wav", "wb") as f:
       f.write(audio.content)

📌 curl 调用示例

   # 声音克隆
   curl -X POST "http://localhost:8853/api/v1/voice/clone" \\
        -F "text=Hello world" \\
        -F "ref_audio=@ref.wav" \\
        -F "language=en"

   # 声音设计
   curl -X POST "http://localhost:8853/api/v1/voice/design" \\
        -F "text=Hello world" \\
        -F "instruct=female, british accent"

   # 查询状态
   curl "http://localhost:8853/api/v1/tasks/{task_id}"

   # 下载音频
   curl "http://localhost:8853/api/v1/voice/download/{task_id}" -o output.wav

╚══════════════════════════════════════════════════════════════════════════════╝
""")


def main():
    parser = argparse.ArgumentParser(
        description="OmniVoice API 调用示例",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法:
  # 声音克隆
  python api_client_example.py --mode clone --ref-audio ref.wav --text "Hello world"
  
  # 声音设计
  python api_client_example.py --mode design --instruct "female, british accent"
  
  # 使用 WebSocket 监控
  python api_client_example.py --mode clone --websocket
  
  # 查看 API 文档
  python api_client_example.py --mode docs
        """
    )
    
    parser.add_argument("--mode", choices=["clone", "design", "websocket", "batch", "docs"],
                       default="docs", help="运行模式")
    parser.add_argument("--server", default=DEFAULT_SERVER, help="API 服务器地址")
    parser.add_argument("--task-id", help="任务ID（用于 WebSocket 监控）")
    parser.add_argument("--text", help="要合成的文本")
    parser.add_argument("--ref-audio", help="参考音频路径（克隆模式）")
    parser.add_argument("--ref-text", help="参考音频文本（克隆模式，可选）")
    parser.add_argument("--instruct", help="声音指令（设计模式）")
    parser.add_argument("--language", help="语言代码（如 en, zh）")
    parser.add_argument("--output", help="输出文件路径")
    parser.add_argument("--num-steps", type=int, default=32, help="扩散步数")
    parser.add_argument("--guidance-scale", type=float, default=2.0, help="引导尺度")
    parser.add_argument("--speed", type=float, default=1.0, help="语速因子")
    parser.add_argument("--duration", type=float, help="固定时长(秒)")
    parser.add_argument("--max-workers", type=int, default=2, help="工作线程数")
    parser.add_argument("--websocket", action="store_true", help="使用 WebSocket 监控进度")
    
    args = parser.parse_args()
    
    if args.mode == "docs":
        example_api_documentation()
        return
    
    # 创建客户端
    client = OmniVoiceAPIClient(args.server)
    
    # 检查服务器连接
    if not client.check_server():
        print("\n⚠️ 无法连接到服务器，请确保服务器正在运行:")
        print(f"   python api_server.py --port 8853")
        return
    
    # 根据模式执行示例
    if args.mode == "clone":
        example_clone_voice(client, args)
    elif args.mode == "design":
        example_design_voice(client, args)
    elif args.mode == "websocket":
        example_websocket_monitor(client, args)
    elif args.mode == "batch":
        example_batch_process(client, args)


if __name__ == "__main__":
    main()
