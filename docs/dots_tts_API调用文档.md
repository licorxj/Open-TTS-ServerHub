# dots.tts API 调用文档

## 概述

dots.tts 是一个 **2B 参数全连续自回归端到端 TTS 系统**，基于语义编码器 + LLM + 流匹配声学头架构，采样率 **48kHz**，无需离散 token。在 Seed-TTS-Eval 基准测试中达到最佳平均性能。

| 服务 | 模型 | 默认端口 | 启动脚本 |
|------|------|---------|---------|
| **dots.tts API** | dots.tts-soar | `8856` | `启动_dots_api.bat` |

---

## 一、声音克隆接口 `/api/v1/voice/clone`

支持三种克隆模式：续写克隆（最佳音色还原）、X-vector 克隆（仅参考音频）、指令克隆（控制语气/风格）。

### 请求参数（Form Data）

| 参数 | 类型 | 必填 | 默认值 | 范围 | 说明 |
|------|------|------|--------|------|------|
| `text` | string | ✅ | - | - | 要合成的文本 |
| `ref_audio` | file | 条件 | - | - | 参考音频文件（与 ref_audio_path 二选一） |
| `ref_audio_path` | string | 条件 | - | - | 参考音频本地路径（推荐本地调用） |
| `prompt_text` | string | ❌ | null | - | 参考音频对应的文本内容（不提供则自动退化为 X-vector 克隆） |
| `instruct` | string | ❌ | null | - | 声音风格控制指令（如 "温柔的语气说"、"用兴奋的语调"），使用 instruction_tts 模板 |
| `language` | string | ❌ | null | - | 语言代码或名称（见下方语言支持表） |
| `output_path` | string | ❌ | - | - | 输出文件路径（不提供则自动保存到 outputs/） |
| `num_steps` | int | ❌ | 10 | 1-100 | Flow-matching 采样步数（越高音质越好，越低速度越快） |
| `guidance_scale` | float | ❌ | 1.2 | 0.0-5.0 | CFG 引导强度（>2 会逐步放大音频能量） |
| `speaker_scale` | float | ❌ | 1.5 | 0.0-5.0 | 参考说话人嵌入缩放系数 |
| `normalize_text` | bool | ❌ | false | - | 是否启用文本规范化（通过 WeTextProcessing） |
| `max_generate_length` | int | ❌ | 500 | 50-2000 | 最大生成音频 patch 数（影响最大输出时长） |
| `seed` | int | ❌ | 42 | ≥0 | 随机种子（固定种子 = 确定性输出） |

### 响应

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "pending",
  "message": "声音克隆任务已创建"
}
```

---

### 支持的克隆模式

#### 1. 续写克隆（推荐，最佳 SIM）

提供参考音频 + 对应文本转录，模型以音频续写方式完整还原声音细节。

```bash
curl -X POST http://localhost:8856/api/v1/voice/clone \
  -F "text=Hello, this is a voice cloning test." \
  -F "ref_audio_path=voice/reference.wav" \
  -F "prompt_text=The exact transcript of the reference audio." \
  -F "num_steps=10" \
  -F "guidance_scale=1.2"
```

```python
import requests

resp = requests.post(
    "http://localhost:8856/api/v1/voice/clone",
    data={
        "text": "Hello, this is a voice cloning test.",
        "ref_audio_path": "voice/reference.wav",
        "prompt_text": "The exact transcript of the reference audio.",
        "num_steps": 10,
    },
)
print(resp.json())
```

#### 2. X-vector 克隆（仅参考音频）

不提供 prompt_text，模型仅通过说话人嵌入向量克隆音色。

```bash
curl -X POST http://localhost:8856/api/v1/voice/clone \
  -F "text=你好，这是一段测试文本" \
  -F "ref_audio_path=voice/speaker.wav" \
  -F "language=zh" \
  -F "num_steps=10"
```

#### 3. 指令克隆（控制语气/风格）

提供 instruct 参数，使用 instruction_tts 模板控制合成语音的语气和风格。

```bash
curl -X POST http://localhost:8856/api/v1/voice/clone \
  -F "text=你好，欢迎使用语音合成系统" \
  -F "ref_audio_path=voice/speaker.wav" \
  -F "instruct=用温柔的语气说" \
  -F "language=zh"
```

```python
import requests

resp = requests.post(
    "http://localhost:8856/api/v1/voice/clone",
    data={
        "text": "你好，欢迎使用语音合成系统",
        "ref_audio_path": "voice/speaker.wav",
        "instruct": "用温柔的语气说",
        "language": "zh",
    },
)
print(resp.json())
```

> 当提供 instruct 时，模型自动使用 `instruction_tts` 模板，将指令与文本结合生成。

#### 4. 文件上传方式（远程调用）

```bash
curl -X POST http://localhost:8856/api/v1/voice/clone \
  -F "text=这是一段远程合成的测试" \
  -F "ref_audio=@reference.wav" \
  -F "prompt_text=参考音频对应的文本内容" \
  -F "language=auto_detect"
```

```python
import requests

with open("reference.wav", "rb") as f:
    resp = requests.post(
        "http://localhost:8856/api/v1/voice/clone",
        files={"ref_audio": f},
        data={
            "text": "这是一段远程合成的测试",
            "prompt_text": "参考音频对应的文本内容",
            "language": "auto_detect",
        },
    )
print(resp.json())
```

---

### 支持的语言

| 参数值 | 说明 |
|--------|------|
| `none` | 不添加语言标签（默认） |
| `auto_detect` | 自动检测文本语言 |
| `EN` / `en` / `english` | 英语 |
| `ZH` / `zh` / `chinese` | 普通话 |
| `Cantonese` | 粤语 |
| 其他 | 支持 24+ 语言（MiniMax 多语言基准） |

---

### 参数调优指南

| 参数 | 建议值 | 说明 |
|------|--------|------|
| `num_steps` | 10-32 | 10 为默认推荐；32 可获得更好音质；4 仅用于 MeanFlow 模型 |
| `guidance_scale` | 1.2 | 默认值；>2 会放大音频能量，可能失真 |
| `speaker_scale` | 1.5 | 默认值；越大音色越接近参考音频 |
| `seed` | 42 | 固定种子可复现结果；不同种子产生不同变体 |
| `max_generate_length` | 500 | 控制最大输出时长；约 500 patches ≈ 10 秒音频 |

---

## 二、通用查询接口

### 健康检查

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
  "gpu_memory_used_gb": 5.2,
  "gpu_memory_total_gb": 16.0,
  "running_tasks": 1,
  "pending_tasks": 0,
  "max_workers": 2,
  "sample_rate": 48000
}
```

#### 调用示例

```bash
curl http://localhost:8856/health
```

```python
import requests

resp = requests.get("http://localhost:8856/health")
health = resp.json()

if health["status"] == "healthy":
    print("服务正常")
    print(f"采样率: {health['sample_rate']}Hz")
    print(f"GPU 显存: {health['gpu_memory_used_gb']}/{health['gpu_memory_total_gb']} GB")
else:
    print("服务降级: 模型未加载")
```

---

### 查询任务状态

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
  "audio_duration": 5.2,
  "inference_time": 4.42,
  "created_at": 1714060800.0,
  "started_at": 1714060801.0,
  "completed_at": 1714060805.4,
  "error": null
}
```

### 下载生成音频

```
GET /api/v1/voice/download/{task_id}
```

返回 WAV 音频文件（48kHz）。

### 实时进度推送（WebSocket）

```
ws://localhost:8856/ws/tasks/{task_id}
```

连接后服务端每 0.5 秒推送任务进度。

### SSE 进度推送

```
GET /api/v1/tasks/{task_id}/progress
```

---

## 三、典型调用流程

### 完整调用流程（本地路径方式）

```python
import requests
import time

API = "http://localhost:8856"

# 0. 健康检查
health = requests.get(f"{API}/health").json()
assert health["status"] == "healthy", f"服务不可用: {health}"
print(f"采样率: {health['sample_rate']}Hz")

# 1. 提交续写克隆任务
resp = requests.post(f"{API}/api/v1/voice/clone", data={
    "text": "你好，欢迎使用 dots.tts 语音合成系统。这是一个高质量的零样本语音克隆演示。",
    "ref_audio_path": "voice/test_speaker.wav",
    "prompt_text": "参考音频中实际说出的文本内容",
    "language": "zh",
    "num_steps": 10,
    "guidance_scale": 1.2,
    "speaker_scale": 1.5,
    "output_path": "outputs/test_clone.wav",
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
    print(f"生成完成: {status['output_path']}")
    print(f"音频时长: {status['audio_duration']:.2f}s, RTF: {status['rtf']:.4f}")
else:
    print(f"生成失败: {status['error']}")
```

### X-vector 克隆流程（无文本转录）

```python
import requests
import time

API = "http://localhost:8856"

resp = requests.post(f"{API}/api/v1/voice/clone", data={
    "text": "这是一段通过说话人嵌入向量克隆的语音。",
    "ref_audio_path": "voice/speaker.wav",
    "language": "zh",
    "num_steps": 10,
})
task_id = resp.json()["task_id"]

while True:
    status = requests.get(f"{API}/api/v1/tasks/{task_id}").json()
    if status["status"] in ("completed", "failed"):
        break
    time.sleep(1)

if status["status"] == "completed":
    # 下载音频
    audio = requests.get(f"{API}/api/v1/voice/download/{task_id}")
    with open("output.wav", "wb") as f:
        f.write(audio.content)
    print("音频已保存到 output.wav")
```

---

## 四、错误处理

所有接口出错时返回标准 HTTP 错误格式：

```json
{
  "detail": "错误描述信息"
}
```

| 状态码 | 含义 |
|--------|------|
| `400` | 请求参数错误（如参考音频路径不存在、prompt_text 未提供 ref_audio） |
| `404` | 资源不存在（任务 ID 无效） |
| `503` | 模型未加载 |

---

## 五、RTF 指标说明

**RTF (Real-Time Factor)** = 推理时间 / 音频时长

| RTF 范围 | 含义 |
|----------|------|
| < 1.0 | 快于实时 |
| = 1.0 | 刚好实时 |
| > 1.0 | 慢于实时 |

dots.tts 默认采样率 48kHz，相同时长下数据量是 16kHz 的 3 倍，RTF 值会相对偏高属正常现象。

---

## 六、技术规格

| 项目 | 规格 |
|------|------|
| 模型参数量 | 2B |
| 架构 | 语义编码器 + LLM + 流匹配声学头 |
| 采样率 | 48kHz |
| 精度 | bfloat16（GPU）/ float32（CPU） |
| 默认推理步数 | 10 |
| 支持语言 | 24+ 语言 |
| 音频格式 | WAV |

---

## 七、API 文档地址

启动服务后，访问以下地址查看完整的 Swagger 交互式文档：

```
http://localhost:8856/docs
```
