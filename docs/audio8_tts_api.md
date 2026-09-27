# Audio8_TTS API 调用文档

`Audio8_TTS` 是一个多模态 TTS 模型（0.6B Preview）。本项目已按统一范式为其封装了：

- **API 服务**：`audio8_api_server.py` (FastAPI, 端口 `8007`)
- **WebUI**：`Audio8_TTS/WebUI.py` (Gradio, 端口 `7868`)
- **启动脚本**：`启动_audio8_api.bat`、`Audio8 WebUI.bat`

> 模型默认路径：`models/Audio8`（即 `Y:\LcTTSHub\models\Audio8`），与项目其它 TTS（如 `models/dot`、`models/VoxCPM2`、`models/Confucius4`）保持一致。
> 也可通过 `model_name` 字段指定其它已注册名称（如 `audio8`）或绝对路径。
> **确保统一存放**：API 服务启动时会主动调用 `ensure_model()`，WebUI/API 首次合成时也会再次确保——若 `models/Audio8` 不存在，将自动从 Hugging Face（`Audio8/Audio8-TTS-Preview-0.6b`）下载到该统一目录；下载失败（无网络等）则回退并打印提示，需手动放置模型。
> `ensure_model()` 还会校验关键权重文件（`model.safetensors`、`codec.pth`），缺失时明确提示需补齐。

### 模型就绪状态（已验证）

本项目环境下的 `models/Audio8` 权重**已就位**，无需再次下载：

| 文件 | 大小 | 作用 |
| --- | --- | --- |
| `model.safetensors` | ~1.2 GB | 主模型权重 |
| `codec.pth` | ~1.35 GB | 声码器/codec 权重 |
| `config.json`、`tokenizer.json`、自定义 `*.py` | — | 配置与 `trust_remote_code` 代码 |

> **本环境网络说明**：直接访问 `huggingface.co` 受限；下载权重请走 HF 镜像：
> ```bash
> set HF_ENDPOINT=https://hf-mirror.com
> # 或用下方脚本从镜像逐个下载缺失文件到 models/Audio8
> ```
> 若 `models/Audio8` 缺失权重，可手动从镜像下载上述必需文件（含 `model.safetensors`、`codec.pth` 及 `*.py`/`*.json`）放到该目录即可。

### 固化启动流程

1. 双击 `启动_audio8_api.bat` 启动 API（端口 8007），或 `Audio8 WebUI.bat` 启动 WebUI（端口 7868）。
2. 启动后 `ensure_model()` 会校验 `models/Audio8` 权重；若齐全则直接加载，缺失则按上文提示补齐。
3. 验证：`http://127.0.0.1:8007/health` 返回 `{"status":"ok"}` 即服务可用。

---

## 一、启动

### 1. API 服务

```bat
启动_audio8_api.bat
```

或手动：

```bash
py312env\python.exe audio8_api_server.py --host 0.0.0.0 --port 8007 --device auto --dtype auto
```

启动后交互式文档：<http://127.0.0.1:8007/docs>

### 2. WebUI

```bat
Audio8 WebUI.bat
```

浏览器打开 <http://127.0.0.1:7868>

---

## 二、能力总览（全部暴露）

| 能力 | 说明 |
| --- | --- |
| 纯文本合成 | 不提供参考音频，直接由文本生成语音 |
| 参考音频 + 参考文本（音色克隆） | 提供 `reference_audio` + `reference_text`，克隆音色 |
| 采样温度 `temperature` | 控制随机性（>0） |
| 核采样 `top_p` | (0,1] |
| Top-K `top_k` | 候选词数量 |
| 随机种子 `seed` | 可复现 |
| 贪心解码 `greedy` | 关闭采样，确定性输出 |
| 解码长度 `max_new_tokens` / `retry_max_new_tokens` | 控制生成长度，未正常结束时自动用更长长度重试 |
| 设备 `device` | auto / cpu / cuda / cuda:N |
| 精度 `dtype` | auto / bfloat16 / float16 / float32 |
| 保存声学 token `save_codes` | 额外输出 `.npy` 声学 token |
| 批量合成 `JSONL` | 多条文本，每条可独立配置参考音频/文本 |

> 说明：当前 `Audio8_TTS` Preview 版推理脚本 `audio8_tts_infer.py` 的推理入口
> 仅接入 **文本 + 参考音频/文本** 两种条件输入；README 中提到的视频/图像多模态
> 输入属于数据预处理阶段能力，本 API/WebUI 已按已实现推理能力完整暴露。
> 若后续官方开放多模态推理接口，可平滑扩展。

---

## 三、API 端点

### 3.1 `POST /tts` — 文本 / 音色克隆合成

支持两种方式提供参考音频（二选一）：

1. `multipart/form-data` 上传文件（`reference_audio_file`）
2. 表单字段 `reference_audio` 传入**服务端已存在**的文件路径

**请求字段（form-data）**

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `text` | string | 是 | — | 合成文本 |
| `reference_text` | string | 否* | — | 参考音频对应文本（*与参考音频必须同时提供） |
| `reference_audio_file` | file | 否 | — | 上传的参考音频文件 |
| `reference_audio` | string | 否 | — | 服务端已存在的参考音频路径 |
| `model_name` | string | 否 | `default` | `default` 或用 ModelsManager 注册名/路径 |
| `device` | string | 否 | `auto` | auto/cpu/cuda/cuda:N |
| `dtype` | string | 否 | `auto` | auto/bfloat16/float16/float32 |
| `max_new_tokens` | int | 否 | `1024` | 解码长度 |
| `retry_max_new_tokens` | int | 否 | `2000` | 重试长度（须 ≥ max_new_tokens） |
| `temperature` | float | 否 | `0.8` | 采样温度（>0） |
| `top_p` | float | 否 | `0.95` | 核采样 (0,1] |
| `top_k` | int | 否 | `50` | Top-K |
| `seed` | int | 否 | `42` | 随机种子 |
| `greedy` | bool | 否 | `false` | 贪心解码 |
| `save_codes` | bool | 否 | `false` | 保存 `.npy` token |

**请求示例（curl，上传参考音频）**

```bash
curl -X POST http://127.0.0.1:8007/tts \
  -F "text=今天天气真好，我们出去走走吧。" \
  -F "reference_audio_file=@ref.wav" \
  -F "reference_text=这是参考音频的文本内容。" \
  -F "temperature=0.8" -F "top_p=0.95" -F "top_k=50" \
  -F "seed=42" -F "max_new_tokens=1024"
```

**请求示例（curl，纯文本 + 服务端路径参考音频）**

```bash
curl -X POST http://127.0.0.1:8007/tts \
  -F "text=你好，我是音频克隆音色。" \
  -F "reference_audio=uploads/ref.wav" \
  -F "reference_text=参考文本" \
  -F "device=auto" -F "dtype=auto"
```

**Python 示例（纯文本）**

```python
import requests
r = requests.post("http://127.0.0.1:8007/tts", data={
    "text": "纯文本合成示例。",
    "temperature": 0.8, "top_p": 0.95, "top_k": 50,
    "seed": 42, "max_new_tokens": 1024,
})
print(r.json())
# {'status': 'OK', 'output_audio': '...', 'audio_url': '/audio/xxxx.wav', 'sample_rate': 44100, ...}
```

**响应字段**

```json
{
  "status": "OK",                 // OK 或 NO_EOS（未生成结束符）
  "output_audio": "绝对路径",
  "audio_url": "/audio/<name>.wav",
  "reference_audio": "参考音频路径或 null",
  "code_frames": 1234,
  "waveform_samples": 56789,
  "sample_rate": 44100,
  "codes_file": "可选，save_codes=true 时返回 .npy 路径"
}
```

---

### 3.2 `POST /tts/json` — 批量合成

**请求体（JSON）**

```json
{
  "items": [
    {"text": "第一条文本"},
    {"text": "克隆音色", "reference_audio": "uploads/ref.wav", "reference_text": "参考文字"},
    {"text": "第三条"}
  ],
  "model_name": "default",
  "device": "auto",
  "dtype": "auto",
  "max_new_tokens": 1024,
  "retry_max_new_tokens": 2000,
  "temperature": 0.8,
  "top_p": 0.95,
  "top_k": 50,
  "seed": 42,
  "greedy": false,
  "save_codes": false,
  "batch_size": 1
}
```

**请求示例**

```bash
curl -X POST http://127.0.0.1:8007/tts/json \
  -H "Content-Type: application/json" \
  -d @batch.json
```

**响应**

```json
{
  "records": [
    {"status": "OK", "output_audio": "...", "audio_url": "/audio/xxx.wav", "sample_rate": 44100},
    {"status": "NO_EOS", "output_audio": "...", "audio_url": "/audio/yyy.wav"}
  ],
  "failures": [
    {"text": "失败文本", "error": "..."}
  ]
}
```

---

### 3.3 `GET /models` — 模型信息

```bash
curl http://127.0.0.1:8007/models
```

返回当前加载模型、设备、精度、采样率，以及 ModelsManager 管理的模型列表。

### 3.4 `GET /health` — 健康检查

```json
{ "status": "ok" }
```

### 3.5 `GET /audio/{filename}` — 获取音频

```bash
curl -O http://127.0.0.1:8007/audio/<name>.wav
```

---

## 四、WebUI 使用

打开 <http://127.0.0.1:7868>：

- **文本合成 / 音色克隆**：填写文本；如需克隆音色，上传参考音频并填写参考文本；
  调整 device/dtype、采样参数、解码长度、seed、greedy、save_codes，点击「开始合成」。
- **批量合成 (JSONL)**：每行粘贴一个 JSON（见 3.2 的 items 格式），点击「开始批量合成」。

---

## 五、错误码

| 状态码 | 含义 |
| --- | --- |
| 400 | 参数错误（如参考音频与文本未同时提供、路径不存在） |
| 500 | 推理失败（见 detail 字段） |
| 404 | 音频文件不存在 |

---

## 六、与其它 TTS 的统一约定

- 上传目录：`uploads/`
- 输出目录：`outputs/audio8_tts/`
- 模型管理：复用 `tools/models_manager.py`，可用名称或路径指定模型。
- 端口：API `8007`，WebUI `7868`。
