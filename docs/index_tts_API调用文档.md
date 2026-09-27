# IndexTTS2 API 调用文档

## 概述

IndexTTS2 是一个 **零样本语音合成系统**，支持高质量语音克隆和丰富的情感控制。支持精确的时长控制和情感-说话人身份解耦，可独立控制音色和情感。

| 服务 | 模型 | 默认端口 | 启动脚本 |
|------|------|---------|---------|
| **IndexTTS2 API** | IndexTTS-2 | `8855` | `启动_index_api.bat` |

---

## 一、声音克隆接口 `/api/v1/voice/clone`

支持多种情感控制方式的声音克隆。

### 请求参数（Form Data）

| 参数 | 类型 | 必填 | 默认值 | 范围 | 说明 |
|------|------|------|--------|------|------|
| `text` | string | ✅ | - | - | 要合成的文本 |
| `spk_audio` | file | 条件 | - | - | 音色参考音频文件（与 spk_audio_path 二选一） |
| `spk_audio_path` | string | 条件 | - | - | 音色参考音频本地路径（推荐本地调用） |
| `instruct` | string | ❌ | - | - | 声音风格控制指令（如 "害怕、紧张的语气"），传入后自动使用 text 情感控制模式 |
| `emo_control_method` | string | ❌ | `speaker` | - | 情感控制方式（见下方模式说明） |
| `emo_audio` | file | 条件 | - | - | 情感参考音频文件（reference 模式时需要） |
| `emo_audio_path` | string | 条件 | - | - | 情感参考音频本地路径 |
| `emo_vector` | string | ❌ | - | - | 8维情感向量，逗号分隔（vector 模式时需要） |
| `emo_text` | string | ❌ | - | - | 情感描述文本（text 模式时可选） |
| `emo_alpha` | float | ❌ | 1.0 | 0.0-1.0 | 情感权重（控制情感表达强度） |
| `use_random` | bool | ❌ | false | - | 情感随机采样（会降低音色还原度） |
| `output_path` | string | ❌ | - | - | 输出文件路径（不提供则自动保存） |
| `max_text_tokens_per_segment` | int | ❌ | 120 | 20-500 | 分句最大 Token 数（影响分句长度和音频质量） |
| `do_sample` | bool | ❌ | true | - | 是否进行采样 |
| `top_p` | float | ❌ | 0.8 | 0.0-1.0 | nucleus 采样概率阈值 |
| `top_k` | int | ❌ | 30 | 0-100 | top-k 采样数量（0 表示不限制） |
| `temperature` | float | ❌ | 0.8 | 0.1-2.0 | 采样温度（越高越随机） |
| `num_beams` | int | ❌ | 3 | 1-10 | beam search 宽度 |
| `repetition_penalty` | float | ❌ | 10.0 | 0.1-20.0 | 重复惩罚系数 |
| `length_penalty` | float | ❌ | 0.0 | -2.0-2.0 | 长度惩罚系数 |
| `max_mel_tokens` | int | ❌ | 1500 | 50-3000 | 最大生成 Mel Token 数（影响最大输出时长） |

### 响应

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "pending",
  "message": "声音克隆任务已创建"
}
```

---

### 情感控制模式

#### 0. instruct（指令克隆）— 自然语言情感控制

提供 `instruct` 参数，自动使用 text 情感控制模式，将指令送入 Qwen 情感模型提取情感向量。

```bash
curl -X POST http://localhost:8855/api/v1/voice/clone \
  -F "text=你好，欢迎使用语音合成系统" \
  -F "spk_audio_path=voice/speaker.wav" \
  -F "instruct=害怕、紧张的语气" \
  -F "num_beams=3"
```

```python
import requests

resp = requests.post(
    "http://localhost:8855/api/v1/voice/clone",
    data={
        "text": "你好，欢迎使用语音合成系统",
        "spk_audio_path": "voice/speaker.wav",
        "instruct": "害怕、紧张的语气",
        "num_beams": 3,
    },
)
print(resp.json())
```

> 当提供 `instruct` 时，`emo_control_method` 自动忽略，直接使用 text 情感控制。

#### 1. speaker（默认）— 音色参考音频的情感

使用音色参考音频的情感表达，无需额外参数。

```bash
curl -X POST http://localhost:8855/api/v1/voice/clone \
  -F "text=你好，这是一段测试文本" \
  -F "spk_audio_path=voice/speaker.wav" \
  -F "num_steps=32"
```

```python
import requests

resp = requests.post(
    "http://localhost:8855/api/v1/voice/clone",
    data={
        "text": "你好，这是一段测试文本",
        "spk_audio_path": "voice/speaker.wav",
    },
)
print(resp.json())
```

#### 2. reference — 独立情感参考音频

使用独立的情感参考音频控制情感表达，音色来自 spk_audio_prompt。

```bash
curl -X POST http://localhost:8855/api/v1/voice/clone \
  -F "text=酒楼丧尽天良，开始借机竞拍房间" \
  -F "spk_audio_path=voice/speaker.wav" \
  -F "emo_control_method=reference" \
  -F "emo_audio_path=voice/emo_sad.wav" \
  -F "emo_alpha=0.9"
```

```python
import requests

resp = requests.post(
    "http://localhost:8855/api/v1/voice/clone",
    data={
        "text": "酒楼丧尽天良，开始借机竞拍房间",
        "spk_audio_path": "voice/speaker.wav",
        "emo_control_method": "reference",
        "emo_audio_path": "voice/emo_sad.wav",
        "emo_alpha": 0.9,
    },
)
```

#### 3. vector — 8维情感向量

通过 8 维情感向量精确控制每种情感的强度。

```bash
curl -X POST http://localhost:8855/api/v1/voice/clone \
  -F "text=对不起嘛！我的记性真的不太好" \
  -F "spk_audio_path=voice/speaker.wav" \
  -F "emo_control_method=vector" \
  -F "emo_vector=0,0,0.8,0,0,0,0,0" \
  -F "emo_alpha=0.6"
```

**情感向量维度说明：**

| 序号 | 情感 | 英文 | 说明 |
|------|------|------|------|
| 1 | 高兴 | happy | 开心、愉快 |
| 2 | 愤怒 | angry | 生气、恼怒 |
| 3 | 悲伤 | sad | 难过、悲伤 |
| 4 | 恐惧 | afraid | 害怕、恐惧 |
| 5 | 厌恶 | disgusted | 厌烦、反感 |
| 6 | 低落 | melancholic | 忧郁、低沉 |
| 7 | 惊喜 | surprised | 惊讶、意外 |
| 8 | 平静 | calm | 平和、自然 |

每个维度值范围 0.0-1.0，总和建议不超过 0.8（超出会自动缩放）。

#### 4. text — 自然语言情感描述

通过自然语言描述情感，模型自动转换为情感向量。

```bash
curl -X POST http://localhost:8855/api/v1/voice/clone \
  -F "text=快躲起来！是他要来了！他要来抓我们了！" \
  -F "spk_audio_path=voice/speaker.wav" \
  -F "emo_control_method=text" \
  -F "emo_text=害怕、紧张的语气" \
  -F "emo_alpha=0.6"
```

```python
import requests

resp = requests.post(
    "http://localhost:8855/api/v1/voice/clone",
    data={
        "text": "快躲起来！是他要来了！他要来抓我们了！",
        "spk_audio_path": "voice/speaker.wav",
        "emo_control_method": "text",
        "emo_text": "害怕、紧张的语气",
        "emo_alpha": 0.6,
    },
)
```

---

### 文件上传方式（远程调用）

```bash
curl -X POST http://localhost:8855/api/v1/voice/clone \
  -F "text=这是一段远程合成的测试" \
  -F "spk_audio=@reference.wav" \
  -F "language=zh"
```

```python
import requests

with open("reference.wav", "rb") as f:
    resp = requests.post(
        "http://localhost:8855/api/v1/voice/clone",
        files={"spk_audio": f},
        data={"text": "这是一段远程合成的测试"},
    )
print(resp.json())
```

---

### 参数调优指南

| 参数 | 建议值 | 说明 |
|------|--------|------|
| `num_beams` | 3 | 值越大生成质量越好，但速度越慢 |
| `temperature` | 0.8 | 越低越稳定，越高越有变化 |
| `top_p` | 0.8 | nucleus 采样阈值 |
| `repetition_penalty` | 10.0 | 防止重复生成 |
| `max_mel_tokens` | 1500 | 影响最大输出时长，过小会被截断 |
| `max_text_tokens_per_segment` | 80-200 | 建议 80-200；过小分句碎，过大影响质量 |
| `emo_alpha` | 0.6-1.0 | instruct/text 模式建议 0.6-0.8；vector 模式建议 0.6 |
| `instruct` | - | 传入后自动使用 text 情感控制，优先级高于 emo_control_method |

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
  "gpu_memory_used_gb": 4.2,
  "gpu_memory_total_gb": 16.0,
  "running_tasks": 1,
  "pending_tasks": 0,
  "max_workers": 2
}
```

#### 调用示例

```bash
curl http://localhost:8855/health
```

```python
import requests

resp = requests.get("http://localhost:8855/health")
health = resp.json()

if health["status"] == "healthy":
    print("服务正常")
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
  "audio_duration": 3.5,
  "inference_time": 2.98,
  "created_at": 1714060800.0,
  "started_at": 1714060801.0,
  "completed_at": 1714060804.0,
  "error": null
}
```

### 下载生成音频

```
GET /api/v1/voice/download/{task_id}
```

返回 WAV 音频文件（22050Hz）。

### 实时进度推送（WebSocket）

```
ws://localhost:8855/ws/tasks/{task_id}
```

连接后服务端每 0.5 秒推送任务进度。

### SSE 进度推送

```
GET /api/v1/tasks/{task_id}/progress
```

---

## 三、典型调用流程

### 完整调用流程（默认情感模式）

```python
import requests
import time

API = "http://localhost:8855"

# 0. 健康检查
health = requests.get(f"{API}/health").json()
assert health["status"] == "healthy", f"服务不可用: {health}"

# 1. 提交任务（直接传路径）
resp = requests.post(f"{API}/api/v1/voice/clone", data={
    "text": "你好，欢迎使用 IndexTTS2 语音合成系统。",
    "spk_audio_path": "voice/speaker.wav",
    "output_path": "outputs/greeting.wav",
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

### 带情感向量控制的调用流程

```python
import requests
import time

API = "http://localhost:8855"

resp = requests.post(f"{API}/api/v1/voice/clone", data={
    "text": "对不起嘛！我的记性真的不太好，但是和你在一起的事情，我都会努力记住的~",
    "spk_audio_path": "voice/speaker.wav",
    "emo_control_method": "vector",
    "emo_vector": "0,0,0.8,0,0,0,0,0",
    "emo_alpha": 0.6,
    "output_path": "outputs/emotional.wav",
})
task_id = resp.json()["task_id"]

while True:
    status = requests.get(f"{API}/api/v1/tasks/{task_id}").json()
    if status["status"] in ("completed", "failed"):
        break
    time.sleep(1)

if status["status"] == "completed":
    print(f"生成完成: {status['output_path']}")
```

### 带情感文本控制的调用流程

```python
import requests
import time

API = "http://localhost:8855"

resp = requests.post(f"{API}/api/v1/voice/clone", data={
    "text": "快躲起来！是他要来了！他要来抓我们了！",
    "spk_audio_path": "voice/speaker.wav",
    "emo_control_method": "text",
    "emo_text": "害怕、紧张的语气",
    "emo_alpha": 0.6,
    "output_path": "outputs/scared.wav",
})
task_id = resp.json()["task_id"]

while True:
    status = requests.get(f"{API}/api/v1/tasks/{task_id}").json()
    if status["status"] in ("completed", "failed"):
        break
    time.sleep(1)

if status["status"] == "completed":
    print(f"生成完成: {status['output_path']}")
```

### 带 instruct 指令克隆的调用流程

```python
import requests
import time

API = "http://localhost:8855"

resp = requests.post(f"{API}/api/v1/voice/clone", data={
    "text": "你好，欢迎使用 IndexTTS2 语音合成系统。",
    "spk_audio_path": "voice/speaker.wav",
    "instruct": "害怕、紧张的语气",
    "emo_alpha": 0.8,
    "output_path": "outputs/instruct_clone.wav",
})
task_id = resp.json()["task_id"]

while True:
    status = requests.get(f"{API}/api/v1/tasks/{task_id}").json()
    if status["status"] in ("completed", "failed"):
        break
    time.sleep(1)

if status["status"] == "completed":
    print(f"生成完成: {status['output_path']}")
    print(f"音频时长: {status['audio_duration']:.2f}s, RTF: {status['rtf']:.4f}")
```

> `instruct` 参数优先级高于 `emo_control_method`。传入 instruct 后，自动使用 Qwen 情感模型提取情感向量。

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
| `400` | 请求参数错误（如音频路径不存在、emo_vector 维度错误） |
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

IndexTTS2 采样率 22050Hz，RTF 值相对合理。

---

## 六、技术规格

| 项目 | 规格 |
|------|------|
| 架构 | 自回归大模型 + 流匹配声学头 |
| 采样率 | 22050Hz |
| 情感控制 | 5 种方式（instruct/speaker/reference/vector/text） |
| 情感维度 | 8 维（happy/angry/sad/afraid/disgusted/melancholic/surprised/calm） |
| GPT 采样参数 | do_sample, top_p, top_k, temperature, num_beams, repetition_penalty, length_penalty |
| 情感文本引擎 | Qwen3 微调模型 |
| 模型大小 | 约 5.5GB（含多个子模型） |

---

## 七、API 文档地址

启动服务后，访问以下地址查看完整的 Swagger 交互式文档：

```
http://localhost:8855/docs
```
