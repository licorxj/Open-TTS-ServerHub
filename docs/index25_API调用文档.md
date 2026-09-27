# IndexTTS-2.5 API 调用文档

## 概述

IndexTTS-2.5 是 IndexTTS-2 的升级版，支持 **零样本语音克隆**、**指令式情感控制（QwenEmotion）** 和多语言合成（中文 / 英文 / 日语 / 西班牙语 / 中英混读）。

| 服务 | 模型 | 默认端口 | 启动脚本 | 采样率 |
|------|------|---------|---------|--------|
| **IndexTTS-2.5 API** | IndexTTS-2.5 | `8858` | `启动_index25_api.bat` | 22050 Hz |

> 首次启动前请确保 `models/index25` 目录完整（主权重 + `hf_cache` + `qwen0.6bemo4-merge` 两个 junction），缺失时运行 `index_tts25\download_index25_weights.py` 自动补齐。

---

## 环境准备

IndexTTS-2.5 官方要求 `transformers==4.52.1`，而主环境 `py312env` 使用 transformers 5.x（供其它服务使用），两者不兼容。采用 **「独立环境 + 专有依赖补丁包」** 方案：

- **独立环境 `index25env`**：以 `py312env` 为基础（`--system-site-packages`），复用其 torch / torchaudio / numpy 等重包，避免重复下载。
- **专有依赖补丁包 `packages/index25`**：扁平的 site-packages 目录，内含 `transformers 4.52.1`、`tokenizers 0.21.4`、`huggingface_hub 0.36.2`、`openai-whisper`、`fugashi`、`unidic_lite`、`tiktoken` 等 Conflict 包。`index_tts25/index_api_server.py` 启动时会把该目录**插入 `sys.path` 最前**，优先于任何环境 site-packages 被调用，从而覆盖主环境的 transformers 5.x。

- **无需手动 pip 安装**：补丁包已随整合包提供，启动时自动优先加载。
- `启动_index25_api.bat` 已指向 `index25env`，**双击即可运行**。

> ⚠️ 不要直接用 `py312env` 启动本服务（会触发 `OffloadedCache`/`QuantizedCacheConfig` 缺失报错）。也不要删改 `packages/index25` 目录，它是 IndexTTS-2.5 的专有依赖来源。

### 重建 index25env（仅当目录丢失时）

```bat
cd /d y:\LcTTSHub
py312env\python.exe -m venv --system-site-packages index25env
```

> `index25env` 仅用于隔离 + 复用 torch；真正的版本补丁来自 `packages/index25`，重建后无需 pip install 任何包。

---

## 接口总览

| 接口 | 方式 | 说明 |
|------|------|------|
| `GET /api/health` | REST | 健康检查 / 模型状态 |
| `POST /api/tts` | REST (JSON) | 合成；不传 `output_path` 返回 JSON（内嵌 `audio_base64`），传则保存文件并返回文件信息 |
| `POST /api/tts/base64` | REST (JSON) | 合成，返回 base64 JSON（兼容接口，等价于 `/api/tts` 不传 `output_path`） |
| `POST /api/tts/form` | REST (Form) | 合成（表单），返回 wav 音频流 |
| `POST /api/tts/stream` | SSE | 流式返回（先推 `start`，完成后推 `done` 含 base64，出错推 `error`） |
| `WS /ws` | WebSocket | 合成，返回 base64 |
| `GET /api/task/{task_id}` | REST | 查询任务状态 |
| `GET /docs` | - | Swagger 交互文档 |

---

## 一、声音克隆接口 `/api/tts`

### 请求参数（JSON）

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `input_text` | string | ✅ | - | 要合成的文本 |
| `speaker_audio` | string | 条件 | - | 音色参考音频，**自适应识别**：优先按路径判断（该字符串是存在的文件，或相对 LcTTSHub 根目录 / `uploads/` / `voice/` 存在），否则按 base64 解码（wav/mp3/m4a）（与 `speaker_audio_path` 二选一） |
| `speaker_audio_path` | string | 条件 | - | 服务端参考音频路径（`uploads/`、`voice/` 下的文件名或绝对路径）；显式指定时优先于 `speaker_audio` |
| `lang` | string | ❌ | `zh` | 语言：`zh` / `en` / `ja` / `es` / `zhen`（中英混读） |
| `speed` | float | ❌ | `1.0` | 语速因子，`>1` 加快、`<1` 放慢（建议 0.5~2.0） |
| `text_normalization` | bool | ❌ | `true` | 是否做文本归一化 |
| `interval_silence` | int | ❌ | `200` | 句间停顿毫秒数 |
| `max_text_tokens_per_segment` | int | ❌ | `120` | 每段最大 token 数 |
| `use_random` | bool | ❌ | `false` | 每次随机采样（同一文本声音略有差异） |
| `emo_control_method` | string | ❌ | `reference` | 情感控制方式：`reference` / `vector` / `instruct` |
| `emo_vector` | string | ❌ | - | 8 维情感向量 JSON，如 `[0,0,0.8,0,0,0,0,0]`（vector 模式） |
| `emo_audio` | string | ❌ | - | 独立情感参考音频，同样**自适应识别**路径或 base64（reference 模式且音色≠情感时使用） |
| `instruct` | string | ❌ | - | 情感指令文本（instruct 模式），如 `用开心的语气说话` |
| `emotion_text` | string | ❌ | - | 从该文本自动推断情感（与 instruct 二选一） |
| `emotion_alpha` | float | ❌ | `1.0` | 情感强度 0~1（instruct / vector 有效） |
| `do_sample` | bool | ❌ | `true` | 是否采样 |
| `top_p` | float | ❌ | `0.9` | nucleus 采样阈值 |
| `top_k` | int | ❌ | `50` | top-k 采样数量 |
| `temperature` | float | ❌ | `1.0` | 采样温度 |
| `num_beams` | int | ❌ | `1` | beam 宽度 |
| `repetition_penalty` | float | ❌ | `1.2` | 重复惩罚 |
| `length_penalty` | float | ❌ | `1.0` | 长度惩罚 |
| `max_mel_tokens` | int | ❌ | `3000` | 最大生成 mel token 数 |
| `output_path` | string | ❌ | - | 输出音频保存路径（绝对路径或相对 LcTTSHub 根目录）；**不传**则返回 JSON 内嵌 `audio_base64`，**传入**则保存文件并返回文件信息（见响应格式） |
| `verbose` | bool | ❌ | `false` | 打印详细推理日志 |
| `need_progress` | bool | ❌ | `false` | 是否通过 SSE 推送进度 |

### 响应格式（`/api/tts`）

根据是否传 `output_path` 返回两种 JSON：

**不传 `output_path`** —— 返回内嵌 base64：

```json
{
  "code": 0,
  "audio_base64": "UklGRi4AAABXQVZF...",  // 不含 data: 前缀
  "format": "wav",
  "sample_rate": 22050,
  "duration": 3.42
}
```

**传入 `output_path`** —— 音频保存到指定文件，返回文件信息：

```json
{
  "code": 0,
  "file": "outputs/index25/tts_xxx.wav",
  "format": "wav",
  "sample_rate": 22050,
  "duration": 3.42,
  "elapsed": 4.12
}
```

> 不传 `output_path` 时，音频默认保存到 `outputs/index25/tts_<task_id>.wav`（同时返回 base64，便于二次使用）。

### 8 维情感向量顺序

`[happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]`
即：`[高兴, 愤怒, 悲伤, 恐惧, 反感, 低落, 惊讶, 自然]`

---

## 二、情感控制模式

### 1. reference（默认）— 参考音频情感

音色来自 `speaker_audio`，情感来自参考音频本身，无需额外参数。

```python
import requests

resp = requests.post(
    "http://localhost:8858/api/tts/base64",
    json={
        "input_text": "你好，这是一段测试文本。",
        "speaker_audio_path": "voice/speaker.wav",
        "lang": "zh",
    },
)
data = resp.json()
print(data["audio_base64"])  # wav base64
```

### 2. vector — 8 维情感向量

```python
import requests

resp = requests.post(
    "http://localhost:8858/api/tts/base64",
    json={
        "input_text": "对不起嘛！我的记性真的不太好。",
        "speaker_audio_path": "voice/speaker.wav",
        "lang": "zh",
        "emo_control_method": "vector",
        "emo_vector": "[0,0,0.8,0,0,0,0,0]",  # 悲伤
        "emotion_alpha": 0.6,
    },
)
print(resp.json()["code"], resp.json()["file"])
```

### 3. instruct — 文本指令情感控制

依赖 QwenEmotion 模型（`use_qwen_emo=True` 加载，启动脚本默认开启）。

```python
import requests

resp = requests.post(
    "http://localhost:8858/api/tts/base64",
    json={
        "input_text": "快躲起来！是他要来了！他要来抓我们了！",
        "speaker_audio_path": "voice/speaker.wav",
        "lang": "zh",
        "emo_control_method": "instruct",
        "instruct": "害怕、紧张的语气",
        "emotion_alpha": 0.8,
    },
)
print(resp.json())
```

---

## 三、多语言合成

通过 `lang` 指定语言：`zh`（中文）/ `en`（英文）/ `ja`（日语）/ `es`（西班牙语）/ `zhen`（中英混读）。

```bash
curl -X POST http://localhost:8858/api/tts \
  -H "Content-Type: application/json" \
  -d '{
    "input_text": "Hello! This is an English demo.",
    "speaker_audio_path": "voice/speaker.wav",
    "lang": "en",
    "output_path": "outputs/output_en.wav"
  }'
```

语速控制：`speed` 大于 1 语速加快，小于 1 语速放慢。

```bash
curl -X POST http://localhost:8858/api/tts \
  -H "Content-Type: application/json" \
  -d '{
    "input_text": "这段语速较快。",
    "speaker_audio_path": "voice/speaker.wav",
    "lang": "zh",
    "speed": 1.25,
    "output_path": "outputs/output_fast.wav"
  }'
```

---

## 四、表单方式 `/api/tts/form`

以 `multipart/form-data` 提交，字段与 JSON 接口一致；`speaker_audio` / `speaker_audio_path` 均为**字符串字段**（传服务端路径或 base64，不接受二进制文件上传）。返回 **wav 音频流**，直接保存到本地即可。

```bash
curl -X POST http://localhost:8858/api/tts/form \
  -F "input_text=这是一段远程合成的测试" \
  -F "lang=zh" \
  -F "speaker_audio_path=voice/speaker.wav" \
  --output remote_test.wav
```

> 若参考音频在服务端磁盘上，优先用 `speaker_audio_path`；若只有 base64 字符串，则通过 `speaker_audio` 传入。想以文件形式上传请改用 JSON 接口并在客户端自行 base64 编码。

---

## 五、SSE 流式 `/api/tts/stream`

先推送 `{"status": "start"}`，合成完成后推送 `{"status": "done", "audio_base64": ...}`，出错推送 `{"status": "error", "error": ...}`。请求参数与 `/api/tts` 相同（支持 `need_progress`）。

```python
import json
import requests

with requests.post(
    "http://localhost:8858/api/tts/stream",
    json={
        "input_text": "流式接口测试",
        "speaker_audio_path": "voice/speaker.wav",
        "lang": "zh",
    },
    stream=True,
) as r:
    for line in r.iter_lines():
        if line.startswith("data: "):
            msg = json.loads(line[6:])
            if msg["status"] == "done":
                audio_b64 = msg["audio_base64"]
                print("合成完成，音频长度:", len(audio_b64))
```

---

## 六、WebSocket `/ws`

```python
import asyncio
import json
import websockets

async def main():
    async with websockets.connect("ws://localhost:8858/ws") as ws:
        await ws.send(json.dumps({
            "input_text": "WebSocket 接口测试",
            "speaker_audio_path": "voice/speaker.wav",
            "lang": "zh",
        }))
        while True:
            msg = json.loads(await ws.recv())
            if msg["type"] == "done":
                print("合成完成，base64 长度:", len(msg["audio_base64"]))
                break
            elif msg["type"] == "error":
                print("错误:", msg["error"])
                break

asyncio.run(main())
```

---

## 七、健康检查

```bash
curl http://localhost:8858/api/health
```

```json
{
  "status": "ok",
  "model_loaded": true,
  "model_dir": "models/index25",
  "use_bf16": true,
  "use_qwen_emo": true,
  "sampling_rate": 22050,
  "languages": ["zh", "en", "ja", "es", "zhen"],
  "emotion_methods": ["reference", "vector", "instruct"]
}
```

---

## 八、任务状态查询 `GET /api/task/{task_id}`

服务端为每次合成生成 `task_id`（同时保存到 `outputs/index25/tts_<task_id>.wav`）。若想异步轮询任务进度，可调用：

```bash
curl http://localhost:8858/api/task/<task_id>
```

```json
{
  "task_id": "3f2a9c8e-...",
  "status": "done",        // running / done / error / not_found
  "current": 1,
  "total": 1,
  "error": null,
  "elapsed": 4.12
}
```

> `status=not_found` 表示任务不存在（可能服务器重启导致内存中的任务记录丢失）。

---

## 九、启动参数

`启动_index25_api.bat` 实际执行的命令：

```bat
index25env\python.exe index_tts25\index_api_server.py --host 0.0.0.0 --port 8858 --model models/index25 --max-workers 1
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--host` | `0.0.0.0` | 监听地址 |
| `--port` | `8858` | 端口 |
| `--model` | `models/index25` | 模型目录 |
| `--max-workers` | `1` | 进程数 |
| `--bf16` | 开 | 使用 bf16 推理（默认开启） |
| `--no-bf16` | - | 关闭 bf16，改用 fp32（省显存、速度下降） |
| `--use-qwen-emo` | 开 | 加载 QwenEmotion 支持指令情感（默认开启） |
| `--no-qwen-emo` | - | 不加载 QwenEmotion（省显存，无法用 instruct） |
| `--device` | 自动 | 推理设备，自动选择 cuda/cpu |

---

## 常见问题

- **instruct 模式报错 `use_emo_text=True requires QwenEmotion`**：服务器需以 `use_qwen_emo=True` 启动（默认开启）；若用 `--no-qwen-emo` 启动则无法使用指令情感。
- **显存不足**：加 `--no-bf16` 使用 fp32 可降低显存占用（速度下降），或 `--no-qwen-emo` 省掉情感模型显存。
- **输出采样率**：固定 22050 Hz，16-bit 单声道 WAV。
- **`speaker_audio` 传 base64 报「既不是有效路径也不是合法 base64」**：确认传入的是标准 base64（可用 `base64.b64encode(open(f,'rb').read()).decode()` 生成，并去掉 `data:` 前缀）；若字符串恰好与某文件同名会被当作路径处理。
- **想直接下载 wav 文件**：用 `/api/tts/form`（返回音频流），或 `/api/tts` 传 `output_path` 后读取返回的 `file` 字段。
