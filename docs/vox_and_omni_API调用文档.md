# Omnivoice TTS API 调用文档

## 概述

Omnivoice 包含两个独立的 TTS API 服务：

| 服务 | 模型 | 默认端口 | 启动脚本 |
|------|------|---------|---------|
| **VoxCPM API** | VoxCPM | `8854` | `start_vox_api.bat` |
| **OmniVoice API** | OmniVoice | `8853` | `start_omnivoice_api.bat` |

两个服务的音频克隆接口均支持两种参考音频传入方式：
- **文件上传**：通过 multipart form 上传音频文件（适合远程调用）
- **直接路径**：传入服务器本地音频文件路径（推荐本地调用，零拷贝，无网关传输开销）

---

## 一、VoxCPM API（端口 8854）

### 1. 可控克隆 `/api/v1/voice/clone`

克隆参考音频的声音特征来合成目标文本，可通过 `instruct` 指令控制情绪、语速、风格等。

#### 请求参数（Form Data）

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `text` | string | ✅ | 要合成的文本 |
| `ref_audio` | file | 条件 | 参考音频文件（与 ref_audio_path 二选一） |
| `ref_audio_path` | string | 条件 | 参考音频本地路径（与 ref_audio 二选一，同时提供时优先使用此字段） |
| `instruct` | string | ❌ | 声音风格控制指令，如 `"Excited and fast-paced"`、`"温柔地，放慢语速"` |
| `speed` | float | ❌ | 语速因子（0.5-2.0），映射为语速提示词拼接到 instruct 最前面。如 `0.6` → `(语速偏慢，温柔的女声)text` |
| `language` | string | ❌ | 语言代码（zh, en, ja, ko 等） |
| `output_path` | string | ❌ | 输出文件路径（不提供则自动保存到 `outputs/` 目录） |
| `cfg_value` | float | ❌ | CFG 引导强度，1.0-5.0，默认 `2.0` |
| `inference_timesteps` | int | ❌ | LocDiT 迭代步数，默认 `10` |
| `normalize` | bool | ❌ | 是否启用文本规范化，默认 `false` |
| `denoise` | bool | ❌ | 是否对参考音频降噪，默认 `true` |

#### speed 参数语速映射

| speed 范围 | 映射提示词 |
|-----------|----------|
| ≤ 0.5 | 语速很慢 |
| 0.5 ~ 0.75 | 语速偏慢 |
| 0.75 ~ 1.0 | 语速略微偏慢 |
| 1.0 ~ 1.25 | 语速略微偏快 |
| 1.25 ~ 1.5 | 语速偏快 |
| > 1.5 | 语速很快 |
| = 1.0 或不传 | 不添加语速提示 |

> 当 `instruct` 和 `speed` 同时提供时，语速提示词拼接到 instruct 最前面，如 `(语速偏快，温柔的女声)你好世界`。

#### 响应

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "pending",
  "message": "可控克隆任务已创建"
}
```

#### 调用示例

**方式一：文件上传**

```bash
curl -X POST http://localhost:8854/api/v1/voice/clone \
  -F "text=你好，这是一段测试文本" \
  -F "ref_audio=@/path/to/reference.wav" \
  -F "language=zh" \
  -F "instruct=温柔地" \
  -F "speed=0.75" \
  -F "cfg_value=2.0" \
  -F "inference_timesteps=10"
```

```python
import requests

with open("reference.wav", "rb") as f:
    resp = requests.post(
        "http://localhost:8854/api/v1/voice/clone",
        files={"ref_audio": f},
        data={
            "text": "你好，这是一段测试文本",
            "language": "zh",
            "instruct": "温柔地",
            "speed": 0.75,
            "cfg_value": 2.0,
            "inference_timesteps": 10,
        },
    )
print(resp.json())
```

**方式二：直接传路径（推荐本地调用）**

```bash
curl -X POST http://localhost:8854/api/v1/voice/clone \
  -F "text=你好，这是一段测试文本" \
  -F "ref_audio_path=/data/speakers/speaker_001.wav" \
  -F "language=zh" \
  -F "instruct=温柔地" \
  -F "speed=0.6" \
  -F "output_path=/data/outputs/result_001.wav"
```

```python
import requests

resp = requests.post(
    "http://localhost:8854/api/v1/voice/clone",
    data={
        "text": "你好，这是一段测试文本",
        "ref_audio_path": "/data/speakers/speaker_001.wav",
        "language": "zh",
        "instruct": "温柔地",
        "speed": 0.6,
        "output_path": "/data/outputs/result_001.wav",
    },
)
print(resp.json())
```

---

### 2. 极致克隆 `/api/v1/voice/ultimate_clone`

音频续写模式，完整还原参考音频中的所有声音细节，适合对保真度要求极高的场景。

> **语速控制**：当传入 `speed` 参数时，自动切换为指令克隆模式（使用 `reference_wav_path` + 语速 instruct），不再使用续写模式。任务标题会显示"指令克隆(语速控制)"。

#### 请求参数（Form Data）

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `text` | string | ✅ | 要续写的文本 |
| `ref_audio` | file | 条件 | 参考音频文件（与 ref_audio_path 二选一） |
| `ref_audio_path` | string | 条件 | 参考音频本地路径（与 ref_audio 二选一，同时提供时优先使用此字段） |
| `prompt_text` | string | ❌ | 参考音频对应的文本（不提供则自动 ASR 识别） |
| `speed` | float | ❌ | 语速因子（0.5-2.0）。传入后自动切换为指令克隆模式以支持语速控制 |
| `language` | string | ❌ | 语言代码 |
| `output_path` | string | ❌ | 输出文件路径（不提供则自动保存到 `outputs/` 目录） |
| `cfg_value` | float | ❌ | CFG 引导强度，默认 `2.0` |
| `inference_timesteps` | int | ❌ | 迭代步数，默认 `10` |
| `normalize` | bool | ❌ | 文本规范化，默认 `false` |
| `denoise` | bool | ❌ | 参考音频降噪，默认 `true` |

#### 响应

```json
{
  "task_id": "660e8400-e29b-41d4-a716-446655440001",
  "status": "pending",
  "message": "极致克隆任务已创建"
}
```

#### 调用示例

**极致克隆（续写模式，无 speed）**

```bash
curl -X POST http://localhost:8854/api/v1/voice/ultimate_clone \
  -F "text=继续说下去的内容" \
  -F "ref_audio_path=/data/speakers/target.wav" \
  -F "prompt_text=参考音频对应的文本内容" \
  -F "language=zh"
```

```python
import requests

resp = requests.post(
    "http://localhost:8854/api/v1/voice/ultimate_clone",
    data={
        "text": "继续说下去的内容",
        "ref_audio_path": "/data/speakers/target.wav",
        "prompt_text": "参考音频对应的文本内容",
        "language": "zh",
    },
)
print(resp.json())
```

**带 speed 参数（自动切换为指令克隆模式）**

```bash
curl -X POST http://localhost:8854/api/v1/voice/ultimate_clone \
  -F "text=继续说下去的内容" \
  -F "ref_audio_path=/data/speakers/target.wav" \
  -F "speed=0.6" \
  -F "language=zh"
```

```python
import requests

resp = requests.post(
    "http://localhost:8854/api/v1/voice/ultimate_clone",
    data={
        "text": "继续说下去的内容",
        "ref_audio_path": "/data/speakers/target.wav",
        "speed": 0.6,
        "language": "zh",
    },
)
print(resp.json())
```

---

### 3. 声音设计 `/api/v1/voice/design`

无需参考音频，通过文字描述直接设计声音。

#### 请求参数（Form Data）

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `text` | string | ✅ | 要合成的文本 |
| `instruct` | string | ✅ | 声音描述指令 |
| `speed` | float | ❌ | 语速因子（0.5-2.0），映射为语速提示词拼接到 instruct 最前面 |
| `language` | string | ❌ | 语言代码 |
| `output_path` | string | ❌ | 输出文件路径 |
| `cfg_value` | float | ❌ | CFG 引导强度，默认 `2.0` |
| `inference_timesteps` | int | ❌ | 迭代步数，默认 `10` |
| `normalize` | bool | ❌ | 文本规范化，默认 `false` |

#### 调用示例

```bash
curl -X POST http://localhost:8854/api/v1/voice/design \
  -F "text=你好，这是一段测试" \
  -F "instruct=A young female speaker with a warm voice" \
  -F "speed=1.5" \
  -F "language=zh"
```

---

## 二、OmniVoice API（端口 8853）

### 1. 声音克隆 `/api/v1/voice/clone`

#### 请求参数（Form Data）

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `text` | string | ✅ | 要合成的文本 |
| `ref_audio` | file | 条件 | 参考音频文件（与 ref_audio_path 二选一） |
| `ref_audio_path` | string | 条件 | 参考音频本地路径（与 ref_audio 二选一，同时提供时优先使用此字段） |
| `ref_text` | string | ❌ | 参考音频的文本（不提供则自动识别） |
| `instruct` | string | ❌ | 声音风格控制指令，如 `"female, low pitch, whisper"` |
| `language` | string | ❌ | 语言代码或名称 |
| `output_path` | string | ❌ | 输出文件路径（不提供则自动保存到 `outputs/` 目录） |
| `num_steps` | int | ❌ | 扩散步数，默认 `32` |
| `guidance_scale` | float | ❌ | 分类器自由引导尺度，默认 `2.0` |
| `speed` | float | ❌ | 语速因子，默认 `1.0` |
| `duration` | float | ❌ | 固定输出时长（秒） |
| `denoise` | bool | ❌ | 是否去噪，默认 `true` |
| `max_workers` | int | ❌ | 处理线程数，默认 `1` |

#### 响应

```json
{
  "task_id": "770e8400-e29b-41d4-a716-446655440000",
  "status": "pending",
  "message": "声音克隆任务已创建"
}
```

#### 调用示例

**方式一：文件上传**

```bash
curl -X POST http://localhost:8853/api/v1/voice/clone \
  -F "text=你好，这是一段测试文本" \
  -F "ref_audio=@/path/to/reference.wav" \
  -F "language=zh" \
  -F "speed=1.0" \
  -F "num_steps=32"
```

```python
import requests

with open("reference.wav", "rb") as f:
    resp = requests.post(
        "http://localhost:8853/api/v1/voice/clone",
        files={"ref_audio": f},
        data={
            "text": "你好，这是一段测试文本",
            "language": "zh",
            "speed": 1.0,
            "num_steps": 32,
        },
    )
print(resp.json())
```

**方式二：直接传路径（推荐本地调用）**

```bash
curl -X POST http://localhost:8853/api/v1/voice/clone \
  -F "text=你好，这是一段测试文本" \
  -F "ref_audio_path=/data/speakers/speaker_001.wav" \
  -F "language=zh" \
  -F "instruct=female, whisper" \
  -F "output_path=/data/outputs/result_001.wav"
```

```python
import requests

resp = requests.post(
    "http://localhost:8853/api/v1/voice/clone",
    data={
        "text": "你好，这是一段测试文本",
        "ref_audio_path": "/data/speakers/speaker_001.wav",
        "language": "zh",
        "instruct": "female, whisper",
        "output_path": "/data/outputs/result_001.wav",
    },
)
print(resp.json())
```

---

### 2. 声音设计 `/api/v1/voice/design`

#### 请求参数（Form Data）

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `text` | string | ✅ | 要合成的文本 |
| `instruct` | string | ✅ | 声音描述指令 |
| `language` | string | ❌ | 语言代码 |
| `output_path` | string | ❌ | 输出文件路径 |
| `num_steps` | int | ❌ | 扩散步数，默认 `32` |
| `guidance_scale` | float | ❌ | 引导尺度，默认 `2.0` |
| `speed` | float | ❌ | 语速因子，默认 `1.0` |
| `duration` | float | ❌ | 固定时长（秒） |
| `denoise` | bool | ❌ | 是否去噪，默认 `true` |

#### 支持的声音描述指令

- **性别**: `male`, `female`
- **年龄**: `child`, `teenager`, `young adult`, `middle-aged`, `elderly`
- **音调**: `very low pitch`, `low pitch`, `moderate pitch`, `high pitch`, `very high pitch`
- **风格**: `whisper`（耳语）
- **英语口音**: `american accent`, `british accent`, `australian accent`, `indian accent`
- **汉语方言**: `四川话`, `陕西话`, `河南话`, `东北话`

#### 调用示例

```bash
curl -X POST http://localhost:8853/api/v1/voice/design \
  -F "text=你好，这是一段测试" \
  -F "instruct=female, young adult, moderate pitch" \
  -F "language=zh"
```

---

## 三、通用查询接口

### 健康检查

两个服务均提供健康检查端点，可用于监控服务存活状态、模型加载情况及 GPU 资源占用。

```
GET /health
```

#### 响应示例

```json
{
  "status": "healthy",
  "model_loaded": true,
  "device": "cuda:0",
  "gpu_available": true,
  "gpu_memory_used_gb": 3.42,
  "gpu_memory_total_gb": 24.0,
  "running_tasks": 1,
  "pending_tasks": 0,
  "max_workers": 4
}
```

#### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | 服务状态：`healthy`（模型已加载）或 `degraded`（模型未加载） |
| `model_loaded` | bool | 模型是否已成功加载 |
| `device` | string | 当前推理设备（如 `cuda:0`、`cpu`） |
| `gpu_available` | bool | GPU 是否可用 |
| `gpu_memory_used_gb` | float/null | GPU 已用显存（GB），无 GPU 时为 null |
| `gpu_memory_total_gb` | float/null | GPU 总显存（GB），无 GPU 时为 null |
| `running_tasks` | int | 当前正在执行的任务数 |
| `pending_tasks` | int | 当前排队等待的任务数 |
| `max_workers` | int | 最大并发工作线程数 |

#### 调用示例

```bash
curl http://localhost:8854/health
curl http://localhost:8853/health
```

```python
import requests

resp = requests.get("http://localhost:8854/health")
health = resp.json()

if health["status"] == "healthy":
    print("服务正常")
    print(f"GPU 显存: {health['gpu_memory_used_gb']}/{health['gpu_memory_total_gb']} GB")
    print(f"运行中任务: {health['running_tasks']}, 排队任务: {health['pending_tasks']}")
else:
    print("服务降级: 模型未加载")
```

---

### 查询任务状态

两个服务均支持以下通用查询接口：

```
GET /api/v1/tasks/{task_id}
```

#### 响应示例

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "task_type": "clone",
  "status": "completed",
  "progress": 100.0,
  "message": "生成完成",
  "output_path": "outputs/550e8400-e29b-41d4-a716-446655440000.wav",
  "rtf": 0.85,
  "audio_duration": 3.5,
  "inference_time": 2.98,
  "created_at": 1714060800.0,
  "started_at": 1714060801.0,
  "completed_at": 1714060804.0,
  "error": null
}
```

#### 任务状态说明

| 状态 | 说明 |
|------|------|
| `pending` | 等待处理 |
| `running` | 处理中 |
| `completed` | 已完成 |
| `failed` | 已失败 |

### 下载生成音频

```
GET /api/v1/voice/download/{task_id}
```

返回 WAV 音频文件。

### 实时进度推送（WebSocket）

```
ws://localhost:{port}/ws/tasks/{task_id}
```

连接后服务端每 0.5 秒推送任务进度。

---

## 四、典型调用流程

### 流程一：本地网关场景（推荐 ref_audio_path）

适合 Omnivoice 与调用方运行在同一台机器或同一局域网的场景，参考音频直接通过文件系统路径传入，避免 Base64 编码和 HTTP 传输开销。

```python
import requests
import time

API = "http://localhost:8854"

# 0. 健康检查
health = requests.get(f"{API}/health").json()
assert health["status"] == "healthy", f"服务不可用: {health}"

# 1. 提交任务（直接传路径）
resp = requests.post(f"{API}/api/v1/voice/clone", data={
    "text": "你好，欢迎使用 Omnivoice 语音合成系统。",
    "ref_audio_path": "/data/speakers/speaker_001.wav",
    "output_path": "/data/outputs/greeting_001.wav",
    "language": "zh",
    "instruct": "温柔地",
    "speed": 0.75,
})
task_id = resp.json()["task_id"]
print(f"任务已提交: {task_id}")

# 2. 轮询等待完成
while True:
    status = requests.get(f"{API}/api/v1/tasks/{task_id}").json()
    print(f"  状态: {status['status']}, 进度: {status['progress']:.1f}%")
    if status["status"] in ("completed", "failed"):
        break
    time.sleep(1)

# 3. 获取结果
if status["status"] == "completed":
    print(f"生成完成，文件路径: {status['output_path']}")
    print(f"音频时长: {status['audio_duration']:.2f}s, RTF: {status['rtf']:.4f}")
else:
    print(f"生成失败: {status['error']}")
```

### 流程二：远程调用场景（文件上传）

适合调用方与服务不在同一台机器的场景，通过 multipart 上传参考音频。

```python
import requests
import time

API = "http://remote-server:8854"

# 0. 健康检查
health = requests.get(f"{API}/health").json()
assert health["status"] == "healthy", f"服务不可用: {health}"

# 1. 上传文件并提交任务
with open("my_voice.wav", "rb") as f:
    resp = requests.post(f"{API}/api/v1/voice/clone", files={"ref_audio": f}, data={
        "text": "这是一段远程合成的测试。",
        "language": "zh",
    })
task_id = resp.json()["task_id"]

# 2. 等待完成
while True:
    status = requests.get(f"{API}/api/v1/tasks/{task_id}").json()
    if status["status"] in ("completed", "failed"):
        break
    time.sleep(1)

# 3. 下载音频
if status["status"] == "completed":
    audio = requests.get(f"{API}/api/v1/voice/download/{task_id}")
    with open("output.wav", "wb") as f:
        f.write(audio.content)
    print("音频已下载到 output.wav")
```

---

## 五、错误处理

所有接口出错时返回标准 HTTP 错误格式：

```json
{
  "detail": "错误描述信息"
}
```

| 状态码 | 含义 |
|--------|------|
| `400` | 请求参数错误（如参考音频路径不存在、未提供 ref_audio 或 ref_audio_path） |
| `404` | 资源不存在（任务 ID 无效） |
| `503` | 模型未加载 |

---

## 六、RTF 指标说明

**RTF (Real-Time Factor)** = 推理时间 / 音频时长

| RTF 范围 | 含义 |
|----------|------|
| < 1.0 | 快于实时 |
| = 1.0 | 刚好实时 |
| > 1.0 | 慢于实时 |

例如：生成 10 秒音频用了 8 秒推理，RTF = 0.8，即快于实时。
