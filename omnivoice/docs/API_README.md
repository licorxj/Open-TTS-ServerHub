# OmniVoice API 文档

## 概述

OmniVoice API 提供完整的 TTS（文本转语音）服务，支持声音克隆和声音设计功能。

**基础地址：** `http://localhost:8853`

## 主要功能

- 🎤 **声音克隆** - 上传参考音频，克隆声音特征
- 🎨 **声音设计** - 通过文字描述设计声音
- 📦 **批量处理** - 一次提交多个任务，支持混合批量
- 🔄 **异步处理** - 非阻塞任务提交，实时进度查询
- 📊 **RTF 显示** - 实时因子（Real-Time Factor）监控
- 🔌 **WebSocket/SSE** - 实时进度推送

## API 接口

### 1. 服务器状态

```
GET /
```

返回服务器信息和模型加载状态。

**响应示例：**
```json
{
  "model_loaded": true,
  "device": "cuda",
  "dtype": "torch.float16",
  "sampling_rate": 24000,
  "max_workers": 4
}
```

---

### 2. 声音克隆

```
POST /api/v1/voice/clone
```

上传参考音频文件，克隆其声音特征来合成目标文本。

**参数（Form Data）：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `text` | string | ✅ | 要合成的文本 |
| `ref_audio` | file | ✅ | 参考音频文件（wav, mp3 等） |
| `ref_text` | string | ❌ | 参考音频的文本（不提供则自动识别） |
| `language` | string | ❌ | 语言代码（如 'en', 'zh'） |
| `output_path` | string | ❌ | 输出文件路径 |
| `num_steps` | int | ❌ | 扩散步数（1-100，默认 32） |
| `guidance_scale` | float | ❌ | 引导尺度（0-10，默认 2.0） |
| `speed` | float | ❌ | 语速因子（0.5-2.0，默认 1.0） |
| `duration` | float | ❌ | 固定时长（秒） |
| `denoise` | bool | ❌ | 是否去噪（默认 true） |

**响应：**
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "pending",
  "message": "声音克隆任务已创建"
}
```

---

### 3. 声音设计

```
POST /api/v1/voice/design
```

通过描述性指令设计声音特征，无需参考音频。

**参数（Form Data）：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `text` | string | ✅ | 要合成的文本 |
| `instruct` | string | ✅ | 声音描述指令 |
| `language` | string | ❌ | 语言代码 |
| `output_path` | string | ❌ | 输出文件路径 |
| `num_steps` | int | ❌ | 扩散步数（默认 32） |
| `guidance_scale` | float | ❌ | 引导尺度（默认 2.0） |
| `speed` | float | ❌ | 语速因子（默认 1.0） |
| `duration` | float | ❌ | 固定时长（秒） |
| `denoise` | bool | ❌ | 是否去噪（默认 true） |

**支持的声音描述指令：**

- **性别**: `male`, `female`（男/女）
- **年龄**: `child`, `teenager`, `young adult`, `middle-aged`, `elderly`
- **音调**: `very low pitch`, `low pitch`, `moderate pitch`, `high pitch`, `very high pitch`
- **风格**: `whisper`（耳语）
- **英语口音**: `american accent`, `british accent`, `australian accent`, `indian accent`
- **汉语方言**: `四川话`, `陕西话`, `河南话`, `东北话`

**响应：**
```json
{
  "task_id": "660e8400-e29b-41d4-a716-446655440001",
  "status": "pending",
  "message": "声音设计任务已创建"
}
```

---

### 4. 批量处理 ⭐

```
POST /api/v1/voice/batch
```

一次性提交多个合成任务，支持混合批量（克隆和设计任务可同时提交）。

**请求体（JSON）：**

```json
{
  "items": [
    {
      "text": "这是第一个测试文本",
      "ref_audio": "/path/to/audio1.wav",
      "ref_text": "参考音频文本（可选）",
      "output_name": "output1.wav"
    },
    {
      "text": "这是第二个测试文本",
      "instruct": "female, low pitch",
      "output_name": "output2.wav"
    }
  ],
  "language": "zh",
  "num_steps": 32,
  "guidance_scale": 2.0,
  "speed": 1.0,
  "duration": null,
  "denoise": true,
  "max_workers": 4
}
```

**参数说明：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `items` | array | ✅ | 任务列表（1-100个任务） |
| `language` | string | ❌ | 统一语言设置 |
| `num_steps` | int | ❌ | 统一扩散步数 |
| `guidance_scale` | float | ❌ | 统一引导尺度 |
| `speed` | float | ❌ | 统一语速 |
| `duration` | float | ❌ | 统一时长 |
| `denoise` | bool | ❌ | 统一去噪设置 |
| `max_workers` | int | ❌ | 最大并行线程数（1-16） |

**items 中的项目类型：**

- **声音克隆项目**：包含 `text`, `ref_audio`（必填）, `ref_text`（可选）, `output_name`（可选）
- **声音设计项目**：包含 `text`, `instruct`（必填）, `output_name`（可选）

**响应：**
```json
{
  "batch_id": "770e8400-e29b-41d4-a716-446655440002",
  "status": "pending",
  "message": "批量任务已创建，共 2 个项目",
  "total_items": 2,
  "task_ids": [
    "770e8400-e29b-41d4-a716-446655440002_0000",
    "770e8400-e29b-41d4-a716-446655440002_0001"
  ]
}
```

---

### 5. 查询批量任务状态

```
GET /api/v1/voice/batch/{batch_id}
```

获取批量任务的整体进度和每个子任务的详细状态。

**响应：**
```json
{
  "batch_id": "770e8400-e29b-41d4-a716-446655440002",
  "total_items": 2,
  "completed_items": 1,
  "failed_items": 0,
  "pending_items": 0,
  "running_items": 1,
  "status": "running",
  "tasks": [
    {
      "task_id": "770e8400-e29b-41d4-a716-446655440002_0000",
      "task_type": "batch_clone",
      "status": "completed",
      "progress": 100.0,
      "message": "任务 1 生成完成",
      "output_path": "outputs/batch_id/0000.wav",
      "rtf": 0.85,
      "audio_duration": 3.5,
      "error": null
    },
    {
      "task_id": "770e8400-e29b-41d4-a716-446655440002_0001",
      "task_type": "batch_design",
      "status": "running",
      "progress": 45.0,
      "message": "正在执行声音设计...",
      "output_path": null,
      "rtf": null,
      "audio_duration": null,
      "error": null
    }
  ],
  "created_at": 1714060800.0,
  "completed_at": null,
  "rtf_avg": 0.85,
  "duration_total": 3.5
}
```

**状态说明：**
- `pending` - 等待处理
- `running` - 处理中
- `completed` - 已完成
- `failed` - 已失败
- `partial` - 部分完成（有成功有失败）

---

### 6. 批量下载音频

```
GET /api/v1/voice/batch/{batch_id}/download
```

将批量任务中所有成功生成的音频文件打包为 ZIP 文件下载。

**响应：** ZIP 文件下载

---

### 7. 删除批量任务

```
DELETE /api/v1/voice/batch/{batch_id}
```

删除批量任务中的所有子任务及其输出文件。

**响应：**
```json
{
  "message": "批量任务已删除，共删除 2 个子任务",
  "batch_id": "770e8400-e29b-41d4-a716-446655440002",
  "deleted_count": 2
}
```

---

### 8. 查询单个任务状态

```
GET /api/v1/tasks/{task_id}
```

**响应：**
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

---

### 9. 列出所有任务

```
GET /api/v1/tasks?status=completed&limit=20&offset=0
```

**参数：**
- `status` - 按状态筛选（pending/running/completed/failed）
- `limit` - 每页数量（1-100）
- `offset` - 偏移量

---

### 10. 下载音频

```
GET /api/v1/voice/download/{task_id}
```

下载单个任务生成的音频文件。

---

### 11. WebSocket 实时进度

```
WebSocket /ws/tasks/{task_id}
```

连接后服务端每 0.5 秒推送任务进度更新。

**消息格式：**
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "task_type": "clone",
  "status": "running",
  "progress": 45.5,
  "message": "正在生成...",
  "rtf": null,
  "audio_duration": null,
  "inference_time": null
}
```

---

### 12. SSE 实时进度

```
GET /api/v1/tasks/{task_id}/progress
```

使用 Server-Sent Events 实时获取任务进度。

---

## RTF 说明

**RTF (Real-Time Factor)** = 推理时间 / 音频时长

- RTF < 1：实时生成（更快）
- RTF = 1：刚好实时
- RTF > 1：慢于实时

例如：生成 10 秒音频用了 8 秒推理，RTF = 0.8

---

## 使用示例

### Python 示例

```python
import requests

# 声音克隆
with open("reference.wav", "rb") as f:
    response = requests.post(
        "http://localhost:8853/api/v1/voice/clone",
        files={"ref_audio": f},
        data={
            "text": "要合成的文本",
            "language": "zh",
            "speed": 1.0
        }
    )
    task_id = response.json()["task_id"]

# 查询状态
status = requests.get(f"http://localhost:8853/api/v1/tasks/{task_id}").json()
print(f"状态: {status['status']}, 进度: {status['progress']}%")

# 下载音频
if status["status"] == "completed":
    audio = requests.get(f"http://localhost:8853/api/v1/voice/download/{task_id}")
    with open("output.wav", "wb") as f:
        f.write(audio.content)
```

### 批量处理示例

```python
import requests

# 批量任务
batch_request = {
    "items": [
        {"text": "第一段文本", "instruct": "female, high pitch"},
        {"text": "第二段文本", "instruct": "male, low pitch"},
        {"text": "第三段文本", "ref_audio": "/path/to/audio.wav"}
    ],
    "language": "zh",
    "max_workers": 4
}

response = requests.post(
    "http://localhost:8853/api/v1/voice/batch",
    json=batch_request
)
batch_id = response.json()["batch_id"]

# 查询批量状态
status = requests.get(f"http://localhost:8853/api/v1/voice/batch/{batch_id}").json()
print(f"完成: {status['completed_items']}/{status['total_items']}")

# 下载所有音频
if status["status"] == "completed":
    zip_file = requests.get(f"http://localhost:8853/api/v1/voice/batch/{batch_id}/download")
    with open("batch_output.zip", "wb") as f:
        f.write(zip_file.content)
```

### cURL 示例

```bash
# 声音克隆
curl -X POST http://localhost:8853/api/v1/voice/clone \
  -F "text=要合成的文本" \
  -F "ref_audio=@reference.wav" \
  -F "language=zh"

# 声音设计
curl -X POST http://localhost:8853/api/v1/voice/design \
  -F "text=要合成的文本" \
  -F "instruct=female, low pitch" \
  -F "language=zh"

# 批量处理
curl -X POST http://localhost:8853/api/v1/voice/batch \
  -H "Content-Type: application/json" \
  -d '{
    "items": [
      {"text": "第一段", "instruct": "male"},
      {"text": "第二段", "instruct": "female"}
    ],
    "language": "zh"
  }'
```

---

## 错误处理

所有接口在出错时返回标准错误格式：

```json
{
  "detail": "错误描述信息"
}
```

常见错误码：
- `400` - 请求参数错误
- `404` - 资源不存在
- `503` - 模型未加载