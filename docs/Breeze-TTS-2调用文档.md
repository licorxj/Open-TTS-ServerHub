# Breeze-TTS-2 调用文档

> 本仓库聚合的第九个 TTS 引擎：**Breeze-TTS-2**（[breezeblue-ai/breeze-tts](https://github.com/breezeblue-ai/breeze-tts)，
> 权重托管于 [ModelScope BreezeBlue/Breeze-TTS-2](https://modelscope.cn/models/BreezeBlue/Breeze-TTS-2)）。
> 它是一个为实时交互设计的开源权重 TTS 模型，在 Artificial Analysis TTS 榜单开源权重中排名第一，
> 支持**声音克隆 / 声音引导 / 声音设计**，中英双语、单模型、采样率 **24000 Hz、单声道**。

---

## ⚠️ 许可与运行要求

- **模型权重**采用 *BreezeBlue Research and Non-Commercial License*，**仅可用于研究与非商用场景**。
- **推理运行时强制要求 NVIDIA CUDA GPU**（代码内 `FastBreezeStreamingRuntime` 仅接受 `cuda` 设备）。
  无 GPU 时会直接报错，不会退化到 CPU。
- 引擎运行在独立的 `breezeenv` 虚拟环境中（见 [依赖说明](依赖说明.md) §4.1），需要
  `transformers==4.57.3` + `qwen-tts==0.1.1`，与共享 `py312env` 的 `transformers 5.3.0` 隔离。

---

## 1. 启动

```bat
:: 方式一：双击启动脚本（后台常驻，自动用 breezeenv）
启动_breeze_tts_api.bat

:: 方式二：命令行（breezeenv）
breezeenv\Scripts\python.exe breeze_tts_api.py --host 0.0.0.0 --port 8860 --device cuda --model models/Breeze-TTS-2
```

- 首次调用接口会自动从 ModelScope 把权重下载到 `models/Breeze-TTS-2`（约 7.7 GB，含 `audio_tokenizer`）。
- Swagger 文档：`http://localhost:8860/docs`
- 健康检查：`GET http://localhost:8860/health`

---

## 2. 三种能力对照

| 能力 | 需要什么 | 端点 | 典型用途 |
| --- | --- | --- | --- |
| 🎙️ **声音克隆 Voice Clone** | 参考音频 + 参考音频的**精确文字稿** | `POST /api/v1/voice/clone` | 复刻某人音色/节奏/情绪 |
| 🎛️ **声音引导 Voice Direction** | 参考音频 + 精确文字稿 + 自然语言指令 | `POST /api/v1/voice/clone`（带 `instruction`） | 在克隆基础上引导语气/情绪/节奏 |
| 🎨 **声音设计 Voice Design** | 仅自然语言指令（**无**参考音频） | `POST /api/v1/voice/design` | 凭描述直接生成全新音色 |

> **关键差异（与本项目其它引擎不同）**：
> - 克隆**必须**提供 `ref_text`（参考音频的逐字转写，重复也要写出）；只传参考音频不传 `ref_text` 会被拒绝。
> - **没有** `language` / `speed` / `pitch` 参数。语言由文本与指令自动判断；节奏、情绪、风格通过 `instruction` 描述。
> - 支持情绪/动作事件：英文用圆括号 `(laugh)` `(cough)` `(clears throat)` `(sigh)`，
>   中文用方括号 `[笑]` `[咳嗽]` `[清嗓子]` `[叹气]`，直接写在 `text` 里即可。

---

## 3. 端点与参数

### 3.1 `POST /api/v1/voice/clone`（克隆 / 引导）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `text` | string (form) | ✅ | 要合成的文本（支持表情事件） |
| `ref_audio` | file (form) | 二选一 | 参考音频文件上传 |
| `ref_audio_path` | string (form) | 二选一 | 参考音频在服务器上的本地路径（推荐，免上传） |
| `ref_text` | string (form) | ✅（克隆/引导） | 参考音频的精确文字稿 |
| `instruction` | string (form) | ❌ | 自然语言指令；提供后变为**声音引导** |
| `cfg_scale` | number (form) | ❌ | CFG 引导强度，默认 `1.0`；**引导/设计建议设 `4`** |
| `seed` | integer (form) | ❌ | 随机种子，默认 `42` |
| `output_path` | string (form) | ❌ | 指定输出 wav 路径 |

### 3.2 `POST /api/v1/voice/design`（设计）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `text` | string (form) | ✅ | 要合成的文本 |
| `instruction` | string (form) | ✅ | 音色描述，如 `温柔自信的年轻女性，声音清晰，语气亲切` |
| `ref_audio` / `ref_audio_path` | — | ❌（提供会被拒绝） | 设计无需参考音频 |
| `cfg_scale` | number (form) | ❌ | 默认 `4.0` |
| `seed` | integer (form) | ❌ | 默认 `42` |
| `output_path` | string (form) | ❌ | 指定输出 wav 路径 |

### 3.3 任务查询 / 下载 / 进度（与管家透传一致）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/tasks/{task_id}` | 查询任务状态、进度、RTF、音频时长 |
| `GET` | `/api/v1/tasks` | 任务列表（`?status=&limit=&offset=`） |
| `DELETE` | `/api/v1/tasks/{task_id}` | 删除已完成/失败任务 |
| `GET` | `/api/v1/voice/download/{task_id}` | 下载生成的 wav |
| `GET` | `/api/v1/tasks/{task_id}/progress` | SSE 进度流（`data: {...}`） |

---

## 4. 调用示例

### 4.1 curl — 声音克隆（上传参考音频）

```bash
curl -X POST http://localhost:8860/api/v1/voice/clone \
  -F "text=没想到过了这么久，你还记得我的声音。" \
  -F "ref_audio=@reference_zh.wav" \
  -F "ref_text=这是中文参考音频的准确文字稿。" \
  -F "cfg_scale=1.0" \
  -F "seed=42"
# -> {"task_id": "...", "status": "pending", "message": "任务已创建"}
```

拿到 `task_id` 后轮询：

```bash
curl http://localhost:8860/api/v1/tasks/<task_id>          # 看 status/progress/rtf
curl http://localhost:8860/api/v1/voice/download/<task_id> -o out.wav
```

### 4.2 curl — 声音引导（克隆 + 指令）

```bash
curl -X POST http://localhost:8860/api/v1/voice/clone \
  -F "text=[叹气] 我们需要谈谈昨晚发生的事。" \
  -F "ref_audio=@reference.wav" \
  -F "ref_text=This is the exact transcript of the reference audio." \
  -F "instruction=Speak slowly with a restrained, serious tone." \
  -F "cfg_scale=4"
```

### 4.3 curl — 声音设计（无参考音频）

```bash
curl -X POST http://localhost:8860/api/v1/voice/design \
  -F "text=(laugh) 欢迎来到今晚的故事时间，让我们一起开始吧。" \
  -F "instruction=一位温柔自信的年轻女性，声音清晰，语气亲切，表达轻快而富有感染力。" \
  -F "cfg_scale=4"
```

### 4.4 Python

```python
import requests

# 声音克隆（用服务器本地参考音频路径，免上传）
r = requests.post("http://localhost:8860/api/v1/voice/clone", data={
    "text": "今天天气真不错。",
    "ref_audio_path": "models/Breeze-TTS-2/examples/reference_zh.wav",  # 自行准备
    "ref_text": "参考音频的准确转写。",
    "cfg_scale": "1.0",
    "seed": "42",
})
task_id = r.json()["task_id"]

# 轮询
import time
while True:
    s = requests.get(f"http://localhost:8860/api/v1/tasks/{task_id}").json()
    if s["status"] in ("completed", "failed"):
        break
    time.sleep(0.5)

if s["status"] == "completed":
    audio = requests.get(f"http://localhost:8860/api/v1/voice/download/{task_id}").content
    open("out.wav", "wb").write(audio)
    print("RTF:", s.get("rtf"), "音频时长:", s.get("audio_duration"))
```

---

## 5. 经 TTS 管家调用

在 `config/tts_hub.yaml` 中已注册为 `breeze_tts`（别名 `breeze` / `breeze-tts-2`）。
管家启动时若不在常驻上限内，会按需拉起 `breezeenv` 子进程：

```bash
# 自动拉起 Breeze-TTS-2 并透传合成（经管家 5199）
POST http://localhost:5199/api/tts?model=breeze
     Body(JSON/Form) 透传到 /api/v1/voice/clone

# 查看支持的参数表（前端工作台据此自动渲染表单）
GET  http://localhost:5199/api/hub/engines/breeze/params
```

> 管家转发遵循与其它引擎相同的透传协议：multipart 参考音频、`output_path`、
> 任务型 `task_id` 轮询、wav 二进制下载、SSE 进度全部原样转发。详见
> [LcTTS 管家接口文档](LcTTS管家接口文档.md) 与 [TTS 管家使用文档](TTS管家使用文档.md)。

---

## 6. 模型自动下载与集中存放

权重经 `tools/models_manager.py` 登记（`MODEL_REGISTRY["breeze_tts"]`）：

- 仓库：`BreezeBlue/Breeze-TTS-2`（ModelScope，国内镜像，下载更快）
- 本地目录：`models/Breeze-TTS-2`（集中存放，不入库）
- 首次调用即自动 `snapshot_download`；`allow_patterns` 只拉推理必需文件
  （根 `config.json` / 分词器 / 分片权重 + `audio_tokenizer/*`），跳过 README / assets。
- 校验：`config.json`、`model.safetensors.index.json`、`audio_tokenizer/config.json`、`tokenizer.json`
  齐备才认为就绪；服务启动时会额外确认 `audio_tokenizer` 子目录存在。

手动预下载（可选）：

```bash
breezeenv\Scripts\python.exe -c "from tools.models_manager import ModelsManager; \
print(ModelsManager().get_model_path('breeze_tts'))"
```

---

## 7. 依赖与环境

见 [依赖说明](依赖说明.md) §4.1：`breezeenv`（基于 `py312env --system-site-packages`）
安装 `qwen-tts==0.1.1` 即可，权重与代码均不入库。
