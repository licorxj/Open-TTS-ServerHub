# AuK 语音生成 / 编辑 API 调用文档

基于腾讯混元 **AuK**（Tencent-Hunyuan/AuK）的 FastAPI 服务，暴露其全部语音生成、编辑与修复能力。

> 服务文件：`auk_api_server.py`　启动脚本：`启动_auk_api.bat`　配置：`config/auk_server.yaml`　服务层：`AuK/auk_service.py`

---

## 一、启动

```bat
双击 启动_auk_api.bat
# 或
python auk_api_server.py --host 0.0.0.0 --port 8021 --device auto --dtype bf16
```

- 首次启动会在后台**自动下载模型**（经魔搭）：`models/AuK`、`models/AuK-Flash`、`models/Qwen2.5-Omni-3B`；下载完成前 `/health` 返回 `ready:false`，生成接口返回 503。
- 模型统一由 `tools/models_manager.py` 管理，已存在则直接复用、不重复下载。

### 依赖

运行本服务需要安装 AuK 的依赖（见 `AuK/requirements.txt`）：gradio、modelscope、transformers、torch/torchaudio、qwen-omni-utils、**x-transformers（CFM 主干，必需）**、soundfile、openai、funasr、PyYAML 等。若环境缺失，请先安装后再启动。

> 本机 `py312env` 已补齐的关键依赖：`pip install "x-transformers==1.44.8" "qwen-omni-utils==0.0.9" "silero-vad==6.2.1"`。
> - `tencentcloud-sdk-python-asr` 在本项目中为**可选**：上游默认 ASR 走腾讯云，本项目改用 `tools/asr.py`（Whisper）复用已有 ASR，故无需安装该 SDK（PE 的 VAD 分支用到 `silero-vad`）。
> - `AuK/src/auk/infer/pe.py` 顶部已将其改为可选导入，缺失不阻断启动。

---

## 二、模型与功能映射

| 类别 | 子能力 | 接口 |
|---|---|---|
| 语音合成 | Zero-shot TTS（音色克隆）、Instruct TTS（文字描述风格合成） | `/v1/generate`、`/v1/generate_with_pe` |
| 内容编辑 | 说话内容改词（插入/删除/替换）、歌词编辑 | 同上（指令驱动） |
| 声学编辑 | 音调、语速、音量 | 同上 |
| 副语言编辑 | 情感转换、音色编辑、去口音、非语言声增删、耳语转换 | 同上 |
| 修复增强 | 语音增强（去噪/去混响）、说话人分离、人声/歌声提取、音质提升 | 同上 |

AuK 全部能力由**指令（instruction）**驱动。你可以：

- **直接给标准指令** → 调 `/v1/generate`（如 `将语速调整为1.25倍。`、`Say the following with the same voice: "..."`）。
- **给口语化指令** → 调 `/v1/generate_with_pe`，由内置 LLM（Prompt Enhancer）自动分类成 18 类任务并生成标准指令。

支持的任务类型见 `GET /v1/tasks`。

---

## 三、接口

### 1. 健康检查
`GET /health`
```json
{ "status": "ok", "ready": true, "building": false, "error": null,
  "variants": ["AuK (Base)", "AuK-Flash ⚡"] }
```

### 2. 任务列表
`GET /v1/tasks` → 返回 `{ "tasks": [ {task_type, name, needs_audio, needs_text, subtypes}, ... ] }`

### 3. 标准指令生成
`POST /v1/generate`（multipart/form-data）

| 字段 | 类型 | 说明 |
|---|---|---|
| `instruction` | str | **必填**，标准指令 |
| `variant` | str | `AuK (Base)`（默认）或 `AuK-Flash ⚡` |
| `audio` | file | 输入/参考音频（编辑/克隆类任务必需） |
| `gen_seconds` | float | 目标时长(秒)；Instruct TTS 无参考音时必需 |
| `ref_text` | str | 参考音频转写文本（可选，提升质量） |
| `gen_text` | str | 目标合成文本（用于时长估算，可选） |
| `nfe` | int | Base 采样步数（默认 32；Flash 忽略） |
| `cfg` | float | Base CFG 强度（默认 2.0；Flash 忽略） |
| `seed` | int | 随机种子（可选） |
| `task_type` | str | 任务类型提示（可选） |

返回：`{ "audio_url": "/v1/files/<id>.wav", "sample_rate": 24000 }`

### 4. 口语指令生成（Prompt Enhancer）
`POST /v1/generate_with_pe`（multipart/form-data）

| 字段 | 类型 | 说明 |
|---|---|---|
| `instruction` | str | **必填**，口语化指令（如「把这段话语速调快一点」） |
| `variant` | str | `AuK (Base)`（默认）或 `AuK-Flash ⚡` |
| `audio` | file | 输入/参考音频（部分任务必需） |
| `use_pe` | bool | 是否启用 Prompt Enhancer（默认 true） |
| `gen_seconds` | float | 目标时长；`use_pe=false` 时必需且 > 0 |
| `ref_text` / `gen_text` | str | 可选 |
| `nfe` / `cfg` / `seed` | - | 同 `/v1/generate` |
| `llm_api_key` / `llm_base_url` / `llm_model` | str | OpenAI 兼容 LLM 凭据（也可在 `config/auk_server.yaml` 的 `llm:` 段或环境变量 `LLM_API_KEY/BASE_URL/MODEL_NAME` 配置）|

> 关闭 PE（`use_pe=false`）时，instruction 即作为标准指令直接执行，需要 `gen_seconds>0`。

返回：
```json
{
  "audio_url": "/v1/files/<id>.wav",
  "sample_rate": 24000,
  "metadata": {
    "task": "speed_edit / 1.25x",
    "duration": "8.24 s",
    "asr_content": "…参考音频转写…",
    "instruction": "将语速调整为1.25倍。"
  }
}
```

---

## 四、调用示例（curl）

音色克隆（标准指令）：
```bash
curl -F "instruction=Say the following with the same voice: 'Hello, this is a cloned voice.'" \
     -F "audio=@ref.wav" \
     -F "gen_seconds=4.0" \
     http://127.0.0.1:8021/v1/generate
```

口语指令（PE 自动解析）：
```bash
curl -F "instruction=把这段话语速调快一点" \
     -F "audio=@input.wav" \
     -F "use_pe=true" \
     -F "llm_api_key=sk-xxx" -F "llm_base_url=https://.../v1" -F "llm_model=gpt-4o-mini" \
     http://127.0.0.1:8021/v1/generate_with_pe
```

下载结果：`http://127.0.0.1:8021<audio_url>`

---

## 五、说明

- **ASR 复用**：参考音频的转写复用项目已有的 `tools/asr.py`（Whisper），不依赖腾讯云或额外 SenseVoice 模型。
- **GPU 串行**：AuK 推理在 GPU 上串行（`gpu_concurrency: 1`），多并发会排队。
- **AuK-Flash**：固定 4 步蒸馏、CFG-off 配方，忽略 `nfe`/`cfg` 参数。
- **模型管理**：三个模型仓库统一存放于 `models/` 下，缺失即经魔搭自动下载；若其它引擎已下载 `Qwen2.5-Omni-3B` 到等价目录，会在 `models_manager` 的 `candidate_dirs` 中被直接复用，避免重复下载。
