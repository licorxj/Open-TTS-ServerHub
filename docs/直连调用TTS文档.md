# LcTTS 直连调用 TTS 文档

> **绕过管家，直接启动并调用各个 TTS 引擎自带的 API 服务。**
>
> 适合：固定只用某一个引擎 / 已有脚本或第三方工具要对接 / 想绕开中间层做压测。
> 想"一个端口调用全部引擎、按需拉起自动卸载"请看 [TTS管家使用文档](TTS管家使用文档.md)。

---

## 1. 直连 vs 经管家，怎么选

| | **直连引擎**（本文） | **经 LcTTS 管家** |
| --- | --- | --- |
| 端口 | 每个引擎一个：`8007 / 8021 / 8853~8858` | 统一 `5199` |
| 进程 | 自己启、自己管 | 管家按需拉起 / 空闲自动卸载 |
| 显存 | 开几个占几份 | 同时只常驻 1~4 个，超上限按 LRU 淘汰 |
| 请求参数 | 各引擎各写各的 | **完全透传，与直连一致** |
| 换引擎 | 自己启停、改端口 | 换个 `model` 名即可 |
| 适用 | 单引擎固定使用、外部系统对接、压测 | 多引擎切换、显存紧张、Agent / 工作流调用 |

两者**不冲突**：管家只是"帮你执行原本一模一样的启动命令"，直连方式完全保留。

---

## 2. 速查表

| 引擎 | 启动脚本 | 端口 | 健康检查 | 声音克隆 | 声音设计 | 任务型 |
| --- | --- | --- | --- | --- | --- | --- |
| **Audio8** | `启动_audio8_api.bat` | `8007` | `GET /health` | `POST /tts` | — | 否（同步） |
| **AuK** | `启动_auk_api.bat` | `8021` | `GET /health` | `POST /v1/generate` | — | 否（同步） |
| **OmniVoice** | `启动_omnivoice_api.bat` | `8853` | `GET /health` | `POST /api/v1/voice/clone` | `POST /api/v1/voice/design` | **是** |
| **OmniVoice 说书版** | `启动_omnivoice说书api.bat` | `8853` | 无 | `POST /tts`（流式 wav） | — | 否（流式） |
| **VoxCPM** | `启动_vox_api.bat` | `8854` | `GET /health` | `POST /api/v1/voice/clone` | `POST /api/v1/voice/design` | **是** |
| **IndexTTS-2** | `启动_index_api.bat` | `8855` | `GET /health` | `POST /api/v1/voice/clone` | — | **是** |
| **dots.tts** | `启动_dots_api.bat` | `8856` | `GET /health` | `POST /api/v1/voice/clone` | — | **是** |
| **Confucius4-TTS** | `启动_confucius4_api.bat` | `8857` | `GET /health` | `POST /api/v1/voice/clone` | — | **是** |
| **IndexTTS-2.5** | `启动_index25_api.bat` | `8858` | `GET /api/health` | `POST /api/tts` | — | 否（同步 / base64） |

> ⚠ **OmniVoice 与说书版端口都是 `8853`**，同时只能开一个（两个脚本内 `uvicorn.run` 端口硬编码）。
>
> 每个服务都自带 Swagger：`http://127.0.0.1:<端口>/docs`，参数与响应以它为准。

---

## 3. 通用约定

- **监听地址**：均默认 `0.0.0.0`，本机调用写 `127.0.0.1`。
- **请求体格式**：
  - 多数引擎合成接口是 **`multipart/form-data`**（`Form(...)` / `File(...)`）——用 `-F` 或 `FormData`
  - **IndexTTS-2.5** 与 **说书版** 是 **`application/json`**
- **参考音频两种给法**（多数引擎都支持）：
  - **文件上传**：`ref_audio=@/path/to.wav`（跨机器调用时用）
  - **服务端本地路径**：`ref_audio_path=/abs/path.wav`（★推荐，本机调用免传输，且引擎内部会优先用它）
- **音频输出**：全部 `wav`。
- **模型下载**：首次请求某引擎会自动下载权重到 `models/`（由 `tools/models_manager.py` 管理，
  国内优先走 ModelScope / hf-mirror 镜像），**首次调用会很慢**，属正常。
- **并发**：多数引擎内部用线程池 + 信号量串行化，并发过高会自动排队；IndexTTS-2.5 默认严格串行
  （`server.max_concurrency`）。

---

## 4. 各引擎调用详解

### 4.1 VoxCPM（8854）★中文效果优秀

```bash
启动_vox_api.bat
# 或：py312env\python.exe VoxCPM\voxcpm-api_server.py --port 8854
curl http://127.0.0.1:8854/health
```

`POST /api/v1/voice/clone`（multipart）— 声音克隆

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `text` | string | **必填** | 要合成的文本 |
| `ref_audio` | file | — | 参考音频文件 |
| `ref_audio_path` | string | — | 参考音频本地路径（★推荐） |
| `instruct` | string | — | 风格 / 情绪指令 |
| `language` | string | `zh` | 语言代码 |
| `cfg_value` | number | `2.0` | 引导强度 `1~5` |
| `inference_timesteps` | int | `10` | 扩散步数 `1~50` |
| `normalize` | bool | `false` | 文本归一化 |
| `denoise` | bool | `true` | 去噪 |
| `speed` | number | — | 语速因子 |
| `output_path` | string | — | 指定输出 wav 路径 |

```bash
curl -X POST http://127.0.0.1:8854/api/v1/voice/clone \
  -F "text=今天天气真不错，适合出去走走。" \
  -F "ref_audio_path=VoxCPM/examples/example.wav" \
  -F "cfg_value=2.0" \
  -F "inference_timesteps=10"
```

响应（任务型）：

```json
{ "task_id": "3f2a...", "status": "pending", "message": "声音克隆任务已创建" }
```

其他端点：`/api/v1/voice/design`（`instruct` 必填）、`/api/v1/voice/ultimate_clone`（用 `prompt_text`）、
`/api/v1/voice/batch`（批量）。

---

### 4.2 OmniVoice（8853）

```bash
启动_omnivoice_api.bat
# 或：py312env\python.exe omnivoice\omni_api_server.py --host 0.0.0.0 --port 8853 --model models/omnivoice
```

`POST /api/v1/voice/clone`（multipart）

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `text` | string | **必填** | 要合成的文本 |
| `ref_audio` / `ref_audio_path` | file / string | 二选一 | 参考音频 |
| `ref_text` | string | — | 参考音频对应文本 |
| `instruct` | string | — | 风格指令 |
| `language` | string | — | 语言代码或名称 |
| `num_steps` | int | `32` | 扩散步数 `1~100` |
| `guidance_scale` | number | `2.0` | 引导尺度 `0~10` |
| `speed` | number | `1.0` | 语速因子 |
| `duration` | number | — | 固定输出时长 `1~300` 秒 |
| `denoise` | bool | `true` | 去噪 |
| `max_workers` | int | `1` | 处理线程数 `1~8` |

```bash
curl -X POST http://127.0.0.1:8853/api/v1/voice/clone \
  -F "text=你好，这是 OmniVoice 的声音克隆。" \
  -F "ref_audio=@D:/ref.wav" \
  -F "num_steps=32"
```

`POST /api/v1/voice/design`：`instruct` **必填**（如 `female, low pitch, gentle`），无需参考音频。

---

### 4.3 OmniVoice 说书版（8853，流式）

长文本专用：一次 POST，**流式返回整段 wav**，内部自动分块拼接（`audio_chunk_duration=30s`）。

```bash
启动_omnivoice说书api.bat
```

`POST /tts` — **JSON**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `text` | string | **必填**，长文本 |
| `voice_name` | string | `voice/<name>.wav` 里已注册的音色名（优先） |
| `ref_audio` | string | 参考音频路径（`voice_name` 优先） |
| `ref_text` | string | 参考音频对应文本 |

```bash
curl -X POST http://127.0.0.1:8853/tts \
  -H "Content-Type: application/json" \
  -d '{"text":"第一章 山雨欲来……","voice_name":"narrator"}' \
  -o story.wav
```

> 该脚本**没有 `/health`**，也没有任务查询接口；响应头带 `X-Task-Id`。

---

### 4.4 VoxCPM 之外的任务型引擎（8855 / 8856 / 8857）

#### IndexTTS-2（8855）

`POST /api/v1/voice/clone`（multipart）— 细粒度情绪控制

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `text` | **必填** | 要合成的文本 |
| `spk_audio` / `spk_audio_path` | — | 说话人参考音频 |
| `emo_control_method` | `speaker` | `speaker` / `reference` / `vector` / `text` |
| `emo_audio` / `emo_audio_path` | — | 情绪参考音频 |
| `emo_vector` | — | 8 维情绪向量（逗号分隔） |
| `emo_text` / `emo_alpha` | `1.0` | 情绪文本 / 强度 |
| `instruct` | — | 风格指令 |
| `use_random` | `false` | 随机采样 |
| `max_text_tokens_per_segment` | `120` | 分句最大 token |
| `do_sample` / `top_p` / `top_k` / `temperature` | `true` / `0.8` / `30` / `0.8` | 采样参数 |
| `num_beams` / `repetition_penalty` / `length_penalty` / `max_mel_tokens` | `3` / `10.0` / `0.0` / `1500` | 解码参数 |

#### dots.tts（8856）

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `text` | **必填** | 要合成的文本 |
| `prompt_text` | — | 参考音频对应文本 |
| `instruct` | — | 风格 / 情绪指令 |
| `language` | — | 语言 |
| `num_steps` | `10` | `1~100` |
| `guidance_scale` | `1.2` | `0~5` |
| `speaker_scale` | `1.5` | 说话人相似度权重 `0~5` |
| `normalize_text` | `false` | 文本归一化 |
| `max_generate_length` | `500` | `50~2000` |
| `seed` | `42` | 随机种子 |

```bash
curl -X POST http://127.0.0.1:8856/api/v1/voice/clone \
  -F "text=今天天气真不错" -F "prompt_text=参考音说的什么" -F "num_steps=10"
```

#### Confucius4-TTS（8857）

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `text` | **必填** | 要合成的文本 |
| `ref_audio` / `ref_audio_path` | — | 参考音频 |
| `lang` | `zh` | 语言 |
| `temperature` / `top_p` / `top_k` | `0.8` / `0.8` / `30` | 采样参数 |
| `repetition_penalty` | `10.0` | 重复惩罚 |
| `max_length` | `1520` | 最大长度 |
| `inference_cfg_rate` | `0.7` | CFG 比率 |
| `num_beams` / `n_timesteps` | `1` / `15` | ⚠ 由服务端 config **强制覆盖**，传了也不生效 |

---

### 4.5 IndexTTS-2.5（8858）★同步返回 base64

与其他引擎不同：**JSON 请求 + 同步返回**，没有任务轮询。

```bash
启动_index25_api.bat
curl http://127.0.0.1:8858/api/health
```

`POST /api/tts`（`application/json`）

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `input_text` | **必填** | ⚠ 是 `input_text`，不是 `text` |
| `speaker_audio` | — | 参考音频：本地路径**或 base64** |
| `speaker_audio_path` | — | 参考音频本地路径 |
| `lang` | `zh` | `zh` / `en` / `ja` / `es` / `zhen` |
| `speed` | — | 语速 `0.3~3.0`（超出自动截断） |
| `emo_control_method` | `reference` | `reference` / `vector` / `instruct` |
| `emo_vector` | — | 8 维情绪向量（JSON 数组字符串） |
| `emo_audio` | — | 情绪参考音频 base64 |
| `instruct` / `emotion_text` / `emotion_alpha` | — | 情绪控制 |
| `interval_silence` | — | 句间停顿毫秒 |
| `max_text_tokens_per_segment` | — | 分句最大 token |
| `do_sample` / `top_p` / `top_k` / `temperature` | — | 采样参数 |
| `num_beams` / `repetition_penalty` / `length_penalty` / `max_mel_tokens` | — | 解码参数 |
| `output_path` | — | 传了则落盘，不传返回 base64 |

```bash
curl -X POST http://127.0.0.1:8858/api/tts \
  -H "Content-Type: application/json" \
  -d '{"input_text":"你好，这是 IndexTTS 2.5。","speaker_audio":"examples/ref.wav","lang":"zh"}'
```

响应：

```json
{ "code": 0, "audio_base64": "...", "format": "wav", "sample_rate": 22050, "duration": 2.4, "elapsed": 1.8 }
```

其他端点：`POST /api/tts/form`（表单方式，直接返回 wav 流）、`POST /api/tts/stream`（SSE）、
`POST /api/tts/base64`、`WS /ws`。

---

### 4.6 Audio8（8007）★零样本克隆，可自动 ASR

```bash
启动_audio8_api.bat
```

`POST /tts`（multipart，**同步返回**，非任务型）

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `text` | **必填** | 要合成的文本 |
| `reference_audio_file` | — | 参考音频文件（上传） |
| `reference_audio` | — | 参考音频本地路径 |
| `reference_text` | — | 参考音频文本；**留空则用 Whisper 自动转写** |
| `model_name` | `default` | 模型变体 |
| `device` / `dtype` | `auto` | 推理设备 / 精度 |
| `max_new_tokens` | `1024` | 最大新 token |
| `temperature` / `top_p` / `top_k` | `0.8` / `0.95` / `50` | 采样参数 |
| `seed` / `greedy` | `42` / `false` | 随机种子 / 贪心解码 |
| `save_codes` | `false` | 保存音频码 |

```bash
curl -X POST http://127.0.0.1:8007/tts \
  -F "text=你好世界" \
  -F "reference_audio=D:/ref.wav"
```

响应包含 `output_audio` / `audio_url` / `sample_rate` / `waveform_samples` 等；
`GET /audio/{filename}` 可取音频，`POST /tts/json` 支持批量。

---

### 4.7 AuK（8021）★零样本 / Instruct / 语音编辑增强

```bash
启动_auk_api.bat    # 内部会设 AUK_VARIANTS="AuK (Base)"
```

`POST /v1/generate`（multipart，同步）

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `instruction` | **必填** | ⚠ AuK 用 `instruction` 承载文本/指令 |
| `variant` | `AuK (Base)` | 模型变体 |
| `audio` | — | 参考音频文件（克隆 / 编辑时提供） |
| `gen_seconds` | — | 生成时长（秒） |
| `ref_text` / `gen_text` | — | 参考文本 / 目标文本 |
| `nfe` / `cfg` | `32` / `2.0` | 函数评估步数 / 引导尺度 |
| `seed` | — | 随机种子 |
| `task_type` | — | 任务类型，见 `GET /v1/tasks` |

```bash
curl -X POST http://127.0.0.1:8021/v1/generate \
  -F "instruction=用温柔的女声说：今天天气真好" \
  -F "audio=@D:/ref.wav" \
  -F "nfe=32"
```

响应：`{ "audio_url": "...", "sample_rate": 24000 }`。
另有 `POST /v1/generate_with_pe`（带 Prompt Enhancer LLM，额外 `use_pe` / `llm_api_key` / `llm_base_url` / `llm_model`）。

---

## 5. 异步任务型引擎的通用流程

**任务型**：OmniVoice(8853) / VoxCPM(8854) / IndexTTS-2(8855) / dots(8856) / Confucius4(8857)

```
① POST 合成  →  { "task_id": "..." }
② GET  /api/v1/tasks/{task_id}             轮询状态与进度
③ GET  /api/v1/voice/download/{task_id}    下载 wav
   （可选）GET /api/v1/tasks/{task_id}/progress   SSE 实时进度
   （可选）WS  /ws/tasks/{task_id}                WebSocket 进度
```

```bash
# ① 提交
TASK=$(curl -s -X POST http://127.0.0.1:8854/api/v1/voice/clone \
  -F "text=你好" -F "ref_audio_path=VoxCPM/examples/example.wav" | jq -r .task_id)

# ② 轮询
curl http://127.0.0.1:8854/api/v1/tasks/$TASK

# ③ 下载
curl http://127.0.0.1:8854/api/v1/voice/download/$TASK -o out.wav
```

任务状态响应（完成后）：

```json
{
  "task_id": "...", "status": "completed", "progress": 1,
  "rtf": 0.42, "audio_duration": 3.52, "inference_time": 1.48,
  "output_path": "outputs/xxx.wav", "error": null
}
```

- `status`：`pending` / `running` / `completed` / `failed`
- `progress`：`0~1` 或 `0~100`（各引擎略有差异）
- **`rtf` = 推理耗时 ÷ 音频时长**，`< 1` 表示生成快于实时
- `DELETE /api/v1/tasks/{task_id}` 可删除任务

---

## 6. 多语言示例

### Python（requests）

```python
import requests

BASE = "http://127.0.0.1:8854"

# 提交
r = requests.post(
    f"{BASE}/api/v1/voice/clone",
    data={"text": "你好，这是直连调用。", "ref_audio_path": "VoxCPM/examples/example.wav"},
    timeout=300,
)
task_id = r.json()["task_id"]

# 轮询
import time
while True:
    st = requests.get(f"{BASE}/api/v1/tasks/{task_id}", timeout=10).json()
    if st["status"] in ("completed", "failed"):
        break
    time.sleep(1)
print("RTF:", st.get("rtf"))

# 下载
wav = requests.get(f"{BASE}/api/v1/voice/download/{task_id}", timeout=120).content
open("out.wav", "wb").write(wav)
```

### JavaScript（fetch / Node 18+）

```js
const BASE = 'http://127.0.0.1:8854'

const fd = new FormData()
fd.append('text', '你好，这是直连调用。')
fd.append('ref_audio_path', 'VoxCPM/examples/example.wav')

const { task_id } = await (await fetch(`${BASE}/api/v1/voice/clone`, { method: 'POST', body: fd })).json()

let st
for (;;) {
  st = await (await fetch(`${BASE}/api/v1/tasks/${task_id}`)).json()
  if (st.status === 'completed' || st.status === 'failed') break
  await new Promise((r) => setTimeout(r, 1000))
}

const buf = Buffer.from(await (await fetch(`${BASE}/api/v1/voice/download/${task_id}`)).arrayBuffer())
```

---

## 7. 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| **首次请求极慢** | 正在下载模型权重到 `models/`，等它下载完；看服务端日志进度 |
| **`connection refused`** | 该引擎服务没启动，先跑对应的 `启动_xxx_api.bat` |
| **`422 Unprocessable Entity`** | 参数名或类型不对。核对 `/docs`：如 IndexTTS-2.5 是 `input_text` 不是 `text`；AuK 是 `instruction` |
| **`503 模型未加载`** | 引擎还在加载中（AuK 启动后异步构建），稍等或看日志 |
| **端口占用** | OmniVoice 与说书版都是 `8853`，同时只能开一个；或关掉已在跑的同端口服务 |
| **CUDA OOM** | 同时开了太多引擎，每个模型都要占显存；只保留需要的，或改走管家（自动按需拉起 / 回收） |
| **`ref_audio_path` 找不到文件** | 路径是**相对于引擎服务的 cwd**（仓库根目录），用绝对路径最稳 |
| **合成出来是静音 / 极短** | 参考音频质量差或太短；换一段 3~10 秒清晰人声 |

---

## 8. 相关文档

- [TTS管家使用文档](TTS管家使用文档.md) —— 统一入口、model 映射、透传规则、配置查询与在线修改
- [依赖说明](依赖说明.md) —— 环境、依赖、模型下载与启动入口
- 各引擎 Swagger：`http://127.0.0.1:<端口>/docs`
- 聚合测试页：仓库根目录 `api_聚合快速调用测试.html`
