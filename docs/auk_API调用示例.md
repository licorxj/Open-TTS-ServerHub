# AuK API 调用示例与参数说明

> 配套文档：`docs/auk_API调用文档.md`
> 服务文件：`auk_api_server.py`　启动：`启动_auk_api.bat`
> 默认地址：`http://127.0.0.1:8021`

本文件聚焦三件事：**支持的语言列表**、**接口参数说明**、**可直接复制的调用示例**。

---

## 一、语言支持列表

AuK 的核心语言能力为 **中文（zh）** 与 **英文（en）** 两种，所有任务模板、Prompt Enhancer 分类、文本规范化均围绕这两种语言展开。

| 能力 | 支持的语言 / 取值 |
|---|---|
| **指令语言** | `zh`（中文）、`en`（英文） |
| **待合成文本语言**（仅 TTS 类） | `zh`（中文）、`en`（英文），可与指令语言不同 |
| **去口音（accent_edit）方言** | 安徽 / 东北 / 福建 / 湖北 / 湖南 / 四川 / 藏语（转标准普通话） |
| **情感转换（emotion_edit）** | `happy`(开心)、`angry`(愤怒)、`sad`(悲伤)、`fearful`(恐惧)、`surprised`(惊讶)、`disgusted`(厌恶)、`calm`(平静)、`excited`(兴奋) |
| **非语言声（nonverbal_edit）事件** | 呼吸/换气/喘气(breath)、笑声(laugh)、叹气(sigh)、惊讶“哦”(surprise-oh)、清嗓(throatclearing)、咳嗽(cough)、应答“嗯”(confirmation-en) 等（中英文候选名称） |

> 说明：其它语种（日/韩/法等）非 AuK 训练覆盖范围，不保证效果。ASR 复用项目既有 `tools/asr.py`（Whisper），其识别语种取决于所选 Whisper 模型。

---

## 二、接口与参数

服务共 4 个端点。生成类接口均接收 `multipart/form-data`。

### 1. `GET /health`　健康检查
返回：`{ "status": "ok|loading", "ready": bool, "building": bool, "error": str|null, "variants": [...] }`
首次启动会自动下载模型（数十 GB），下载期间 `ready:false`，生成接口返回 503。

### 2. `GET /v1/tasks`　任务列表
返回 AuK 支持的 18 类任务（task_type / name / needs_audio / needs_text / subtypes）。

### 3. `POST /v1/generate`　标准指令生成
直接传入**标准指令**（自然语言句子）。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `instruction` | str | ✅ | 标准指令，如 `将语速调整为1.25倍。` |
| `variant` | str | | `AuK (Base)`（默认，可调 NFE/CFG）或 `AuK-Flash ⚡`（4 步蒸馏、CFG-off） |
| `audio` | file | 条件 | 输入/参考音频（编辑、克隆、修复类任务必需） |
| `gen_seconds` | float | 条件 | 目标时长(秒)；Instruct TTS 无参考音时必需 |
| `ref_text` | str | | 参考音频转写文本（可选，提升质量） |
| `gen_text` | str | | 目标合成文本（用于时长估算，可选） |
| `nfe` | int | | Base 采样步数（默认 32；Flash 忽略） |
| `cfg` | float | | Base CFG 强度（默认 2.0；Flash 忽略） |
| `seed` | int | | 随机种子（可选） |
| `task_type` | str | | 任务类型提示（可选） |

返回：`{ "audio_url": "/v1/files/<id>.wav", "sample_rate": 24000 }`

### 4. `POST /v1/generate_with_pe`　口语指令生成
传入**口语化指令**，由内置 LLM（Prompt Enhancer）自动分类为 18 类任务并生成标准指令；可选复用现有 Whisper ASR 转写参考音频。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `instruction` | str | ✅ | 口语化指令，如 `把这段话语速调快一点` |
| `variant` | str | | `AuK (Base)` / `AuK-Flash ⚡` |
| `audio` | file | 条件 | 输入/参考音频（部分任务必需） |
| `use_pe` | bool | | 是否启用 Prompt Enhancer（默认 true） |
| `gen_seconds` | float | 条件 | 目标时长；`use_pe=false` 时必需且 > 0 |
| `ref_text` | str | | 可选 |
| `gen_text` | str | | 可选 |
| `nfe` / `cfg` / `seed` | - | | 同 `/v1/generate` |
| `llm_api_key` | str | 条件 | OpenAI 兼容 LLM 密钥（也可写 `config/auk_server.yaml` 的 `llm:` 段或环境变量 `LLM_API_KEY`） |
| `llm_base_url` | str | 条件 | LLM 端点（如 `https://.../v1`）；环境变量 `LLM_BASE_URL` |
| `llm_model` | str | 条件 | LLM 模型名；环境变量 `LLM_MODEL_NAME` |

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

## 三、调用示例

> 约定：`BASE=http://127.0.0.1:8021`；结果音频通过 `curl $BASE<audio_url> -o out.wav` 下载。
> AuK-Flash 请自行把 `variant` 改为 `AuK-Flash ⚡`，并忽略 `nfe`/`cfg`。

### 3.1 语音合成

**Zero-shot TTS（音色克隆）— 标准指令**
```bash
curl -F "instruction=Say the following with the same voice: \"Hello, this is a cloned voice.\"" \
     -F "audio=@ref.wav" \
     -F "gen_seconds=4.0" \
     $BASE/v1/generate
```

**Instruct TTS（文字描述风格合成，无需参考音）— 走 PE**
```bash
curl -F "instruction=用年轻女生温柔体贴的语气说“宝宝，欢迎回来，今天上班累不累呀”" \
     -F "llm_api_key=sk-xxx" -F "llm_base_url=https://api.xxx/v1" -F "llm_model=gpt-4o-mini" \
     $BASE/v1/generate_with_pe
```

### 3.2 内容编辑

```bash
# 把“今天下午开会”改成“明天上午开会”（content_edit / replace）
curl -F "instruction=把这段录音里“今天下午开会”改成“明天上午开会”" \
     -F "audio=@input.wav" \
     $BASE/v1/generate_with_pe

# 在“你好”后面加上“呀”（content_edit / insert_after）
curl -F "instruction=在“你好”后面加上“呀”" -F "audio=@input.wav" $BASE/v1/generate_with_pe

# 歌词编辑（vocal_edit）：把“明天你好”改成“未来你好”
curl -F "instruction=把这首歌里唱的“明天你好”改成“未来你好”" \
     -F "audio=@vocal.wav" $BASE/v1/generate_with_pe
```

### 3.3 声学编辑

```bash
# 语速 0.5/0.75/1.25/1.5/2.0 倍
curl -F "instruction=将语速调整为1.25倍。" -F "audio=@input.wav" $BASE/v1/generate_with_pe
# 音量 升高/降低 5/10/15 分贝
curl -F "instruction=将音量升高10分贝。" -F "audio=@input.wav" $BASE/v1/generate_with_pe
# 音调 升高/降低 1/2/3 个半音
curl -F "instruction=将音调降低2个半音。" -F "audio=@input.wav" $BASE/v1/generate_with_pe
```

### 3.4 副语言编辑

```bash
# 情感转换（8 选 1）
curl -F "instruction=把这段语音的情感转变为开心" -F "audio=@input.wav" $BASE/v1/generate_with_pe
# 音色编辑（文字描述目标音色，保留内容）
curl -F "instruction=把这段音频的音色换成低沉磁性的男声，内容别变" -F "audio=@input.wav" $BASE/v1/generate_with_pe
# 去口音（转标准普通话）
curl -F "instruction=请去掉这段语音里的方言口音，保持说话人音色一致。" -F "audio=@input.wav" $BASE/v1/generate_with_pe
# 耳语转换
curl -F "instruction=把这段话转换成耳语" -F "audio=@input.wav" $BASE/v1/generate_with_pe
# 非语言声：开头加笑声 / 删除所有呼吸声
curl -F "instruction=在语音开头增加笑声" -F "audio=@input.wav" $BASE/v1/generate_with_pe
curl -F "instruction=删除音频中所有的呼吸声" -F "audio=@input.wav" $BASE/v1/generate_with_pe
```

### 3.5 修复增强

```bash
# 语音增强（去噪 + 去混响）
curl -F "instruction=请清理这段输入语音，去掉噪声和混响，保留所有人声" -F "audio=@input.wav" $BASE/v1/generate_with_pe
# 说话人分离（只保留第一个说话人）
curl -F "instruction=这段音频中只保留第一个开始说话的人" -F "audio=@input.wav" $BASE/v1/generate_with_pe
# 提取人声（仅歌声 / 所有人声）
curl -F "instruction=请只保留歌声，其余声音都去掉" -F "audio=@song.wav" $BASE/v1/generate_with_pe
# 音质提升（去电话感 / 带宽扩展）
curl -F "instruction=这段音频像打电话，请消除电话音色恢复清晰人声" -F "audio=@input.wav" $BASE/v1/generate_with_pe
```

### 3.6 Python 调用示例

```python
import requests

BASE = "http://127.0.0.1:8021"

# 标准指令（内容编辑）
def edit_text(audio_path, instruction):
    with open(audio_path, "rb") as f:
        r = requests.post(f"{BASE}/v1/generate_with_pe", files={"audio": f},
                          data={"instruction": instruction,
                                "llm_api_key": "sk-xxx",
                                "llm_base_url": "https://api.xxx/v1",
                                "llm_model": "gpt-4o-mini"})
    r.raise_for_status()
    js = r.json()
    # 下载音频
    audio_url = js["audio_url"]
    audio = requests.get(BASE + audio_url).content
    with open("out.wav", "wb") as out:
        out.write(audio)
    print("task:", js["metadata"]["task"], "duration:", js["metadata"]["duration"])
    return js

edit_text("input.wav", "把这段录音里“今天下午开会”改成“明天上午开会”")
```

### 3.7 关闭 Prompt Enhancer（直接标准指令）

当 `use_pe=false` 时，`instruction` 作为标准指令直接执行，需提供 `gen_seconds>0`：

```bash
curl -F "instruction=将语速调整为1.25倍。" \
     -F "audio=@input.wav" -F "use_pe=false" -F "gen_seconds=8.0" \
     $BASE/v1/generate_with_pe
```

---

## 四、常见问题

- **返回 503**：模型仍在后台下载，等待 `/health` 返回 `ready:true` 后重试。
- **PE 报错“缺少 LLM 配置”**：`/v1/generate_with_pe` 启用 PE 时必须提供 LLM 三要素（请求参数、yaml 或环境变量）。若只想用标准指令，改用 `/v1/generate` 或 `use_pe=false`。
- **AuK-Flash**：固定 4 步、CFG-off，传入的 `nfe`/`cfg` 会被忽略。
- **GPU 串行**：AuK 推理在 GPU 上串行（`gpu_concurrency: 1`），并发请求会自动排队。
