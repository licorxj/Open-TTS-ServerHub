# Confucius4-TTS API 接口文档

## 概述

Confucius4-TTS API 服务器提供基于 Confucius4 模型的语音合成服务。该模型由网易有道团队开发，采用 Speech Encoder + LLM + 流式语音 Token 生成 + 扩散模型的四阶段架构，支持 **14 种语言**。

- **端口**: 8857
- **模型**: Confucius4-TTS (参数量 0.5B)
- **架构**: Qwen3-0.5B + HanLP + CAM++ Style Encoder + GigaSpeech2 数据集
- **支持语言**: 中文(zh)、英文(en)、日文(ja)、韩文(ko)、德语(de)、法语(fr)、泰语(th)、印尼语(id)、越南语(vi)、西班牙语(es)、葡萄牙语(pt)、意大利语(it)、俄语(ru)、马来语(ms)

## 快速启动

### 方式一：双击启动脚本

```
启动_confucius4_api.bat
```

### 方式二：命令行启动

```bash
python confucius4_api_server.py --host 0.0.0.0 --port 8857 --device cuda
```

**启动参数**:
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--host` | 0.0.0.0 | 服务器地址 |
| `--port` | 8857 | 服务器端口 |
| `--device` | auto | 计算设备 (cuda/cpu/mps) |
| `--max-workers` | 2 | 最大工作线程数 |

### WebUI 启动

```
启动Confucius4 WebUI.bat
```

访问 `http://localhost:7860` 使用 Gradio WebUI。

## API 端点

### 健康检查

```
GET /health
```

**响应示例**:
```json
{
  "status": "healthy",
  "model_loaded": true,
  "model_type": "Confucius4-TTS",
  "device": "cuda",
  "sampling_rate": 22050,
  "gpu_available": true,
  "gpu_memory_used_gb": 1.2,
  "gpu_memory_total_gb": 8.0,
  "running_tasks": 0,
  "pending_tasks": 0,
  "supported_languages": ["zh", "en", "ja", "ko", "de", "fr", "th", "id", "vi", "es", "pt", "it", "ru", "ms"]
}
```

### 服务器信息

```
GET /
```

### 声音克隆

```
POST /api/v1/voice/clone
```

使用参考音频的声音特征合成目标文本。

**请求参数 (Form 表单)**:

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| text | string | 是 | - | 要合成的文本 |
| ref_audio | file | 否* | - | 参考音频文件上传 |
| ref_audio_path | string | 否* | - | 参考音频本地路径 |
| lang | string | 否 | "zh" | 语言代码 |
| temperature | float | 否 | 0.8 | 采样温度 (0.1-2.0) |
| top_p | float | 否 | 0.8 | 核采样阈值 (0.0-1.0) |
| top_k | int | 否 | 30 | Top-K 采样 (0-100) |
| num_beams | int | 否 | 3 | Beam Search 宽度 (1-10) |
| repetition_penalty | float | 否 | 10.0 | 重复惩罚 (1.0-20.0) |
| max_length | int | 否 | 1520 | 最大序列长度 |
| n_timesteps | int | 否 | 25 | S2A 扩散步数 (1-100) |
| inference_cfg_rate | float | 否 | 0.7 | CFG 引导强度 (0.0-2.0) |
| output_path | string | 否 | auto | 输出文件路径 |

> *注: `ref_audio` 和 `ref_audio_path` 二选一，同时提供时优先使用 `ref_audio_path`。

**响应示例**:
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "pending",
  "message": "声音克隆任务已创建"
}
```

### 查询任务状态

```
GET /api/v1/tasks/{task_id}
```

**响应示例**:
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "task_type": "clone",
  "status": "completed",
  "progress": 100.0,
  "message": "生成完成",
  "output_path": "outputs/confucius4_550e8400.wav",
  "rtf": 0.45,
  "audio_duration": 5.23,
  "inference_time": 2.35,
  "created_at": 1640995200.0,
  "started_at": 1640995201.0,
  "completed_at": 1640995203.5
}
```

### 列出任务

```
GET /api/v1/tasks?status=completed&limit=20&offset=0
```

### 下载音频

```
GET /api/v1/voice/download/{task_id}
```

返回 WAV 格式音频文件。

### WebSocket 实时进度

```
ws://localhost:8857/ws/tasks/{task_id}
```

**推送消息格式**:
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "task_type": "clone",
  "status": "running",
  "progress": 65.0,
  "message": "扩散模型去噪中...",
  "rtf": null,
  "audio_duration": null,
  "inference_time": null
}
```

### SSE 实时进度 (备选)

```
GET /api/v1/tasks/{task_id}/progress
```

返回 `text/event-stream` 格式的进度数据。

## 调用示例

### Python (requests)

```python
import requests
import time

BASE_URL = "http://localhost:8857"

# 1. 提交声音克隆任务
with open("reference.wav", "rb") as f:
    resp = requests.post(f"{BASE_URL}/api/v1/voice/clone", data={
        "text": "你好，这是一段测试文本。",
        "lang": "zh",
        "ref_audio_path": "/path/to/reference.wav",
        "temperature": 0.8,
        "top_p": 0.8,
        "n_timesteps": 25,
    })

task_id = resp.json()["task_id"]
print(f"任务已创建: {task_id}")

# 2. 轮询查询状态
while True:
    status_resp = requests.get(f"{BASE_URL}/api/v1/tasks/{task_id}")
    status = status_resp.json()
    print(f"状态: {status['status']}, 进度: {status['progress']:.1f}%")
    
    if status["status"] in ("completed", "failed"):
        break
    time.sleep(1)

# 3. 下载音频
if status["status"] == "completed":
    audio_resp = requests.get(f"{BASE_URL}/api/v1/voice/download/{task_id}")
    with open("output.wav", "wb") as f:
        f.write(audio_resp.content)
    print(f"音频已保存, RTF: {status['rtf']:.4f}")
```

### Python (WebSocket)

```python
import asyncio
import websockets
import json

async def monitor_progress(task_id):
    uri = f"ws://localhost:8857/ws/tasks/{task_id}"
    async with websockets.connect(uri) as ws:
        while True:
            data = json.loads(await ws.recv())
            print(f"进度: {data['progress']:.1f}%, 消息: {data['message']}")
            if data["status"] in ("completed", "failed"):
                break

asyncio.run(monitor_progress("your-task-id"))
```

### cURL

```bash
# 提交任务 (本地路径模式)
curl -X POST http://localhost:8857/api/v1/voice/clone \
  -F "text=你好世界" \
  -F "lang=zh" \
  -F "ref_audio_path=/path/to/reference.wav"

# 提交任务 (文件上传模式)
curl -X POST http://localhost:8857/api/v1/voice/clone \
  -F "text=Hello World" \
  -F "lang=en" \
  -F "ref_audio=@reference.wav"

# 查询状态
curl http://localhost:8857/api/v1/tasks/{task_id}

# 下载音频
curl -O http://localhost:8857/api/v1/voice/download/{task_id}
```

## 参数调优建议

### temperature (采样温度)
- **低值 (0.1-0.5)**: 更稳定、一致的语音，适合正式场景
- **中值 (0.6-1.0)**: 平衡多样性和质量（推荐 0.8）
- **高值 (1.0-2.0)**: 更多变化，可能引入不稳定性

### n_timesteps (扩散步数)
- **少 (10-15)**: 速度快，质量略低
- **中 (20-30)**: 平衡速度和质量（推荐 25）
- **多 (30-50)**: 质量最高，速度较慢

### num_beams (Beam Search)
- **1**: 贪心解码，最快
- **3-5**: 推荐范围，平衡质量和速度
- **5-10**: 最高质量，显著增加延迟

### inference_cfg_rate (CFG 引导)
- **0.0**: 无引导，完全依赖模型
- **0.5-1.0**: 推荐范围（默认 0.7）
- **高值**: 更强的引导，可能过度约束

## 与其他 TTS 接口对比

| 特性 | Confucius4 | OmniVoice | IndexTTS | DotTTS | VoxCPM |
|------|------------|-----------|----------|--------|--------|
| 端口 | 8857 | 8853 | 8855 | 8856 | 8854 |
| 声音克隆 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 声音设计 | ❌ | ✅ | ❌ | ❌ | ✅ |
| 多语言 | 14语言 | 中英日 | 中英 | 中英 | 中英日韩 |
| 极致克隆 | ❌ | ❌ | ❌ | ❌ | ✅ |

## 常见问题

### Q: 首次使用新语言时很慢？
A: 首次使用新语言需要下载对应的语言词表，后续使用会直接从缓存加载。

### Q: 模型文件在哪里？
A: 模型文件存放在 `models/Confucius4/` 目录下，API 服务器会自动从该目录加载。

### Q: 如何提高生成速度？
A: 可以减少 `n_timesteps`（如 15-20）或减少 `num_beams`（如 1-2）。

### Q: 支持流式输出吗？
A: 当前版本不支持流式输出，但支持 WebSocket 实时进度推送。

## 技术架构

```
文本 → [HanLP分词] → [语言ID + 音素序列]
                              ↓
参考音频 → [CAM++ Style Encoder] → 说话人嵌入
                              ↓
              [Qwen3-0.5B T2S 模型] → 语音 Token
                              ↓
              [S2A 扩散模型 (25步)] → 波形重建
                              ↓
                         输出音频 (22050Hz)
```
