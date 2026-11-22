# 本机引擎接口表（走 5190 管家）

调用方**只发规范名**（`text` / `ref_audio_path` / `ref_text` / `instruct` / `speed` / `language` / `output_path`），
管家会按引擎把它翻译成原生字段（IndexTTS 的 `speaker_audio_path`、AuK 的 `instruction` 等）。
`tts_agent.py` 已经封装好这一切，下表用于**选接口**和**判断该给什么参数**。

| 接口名 | 显示名 | 端口 | 端点（mode） | 参考音频 | 特点 / 注意 |
| --- | --- | --- | --- | --- | --- |
| `indextts25` | IndexTTS-2.5 | 8858 | `clone`（=/api/tts，JSON） | `--ref-audio`（speaker_audio） | 中文最好，支持情绪向量/指令、句间停顿、SSE 流式；`speed` 0.3~3.0；2.5 需要参考音频 |
| `indextts2` | IndexTTS-2 | 8855 | `clone`（任务型） | `--ref-audio`（spk_audio） | 情绪控制四种方式：speaker/reference/vector/text，参数最多 |
| `voxcpm` | VoxCPM2 | 8854 | `clone` / `design` / `ultimate` | `--ref-audio` | 克隆、声音设计（`instruct`）、终极克隆（还需 `--ref-text`）；`cfg_value` / `inference_timesteps` 可调 |
| `omnivoice` | OmniVoice | 8853 | `clone` / `design` | `--ref-audio` | 克隆需 `--ref-text`；设计用 `--instruct`；`num_steps` 默认 32 |
| `omnivoice_story` | OmniVoice 说书版 | 8853（与 omnivoice 互斥） | `plain`（=/tts） | `voice_name` 或 `--ref-audio` | 长文本一次成稿、自动分块拼接；`--param voice_name=xxx` 选 voice/ 下已注册音色 |
| `dots` | dots.tts | 8856 | `clone`（任务型） | `--ref-audio` | 可给 `--ref-text`；`speaker_scale` 调相似度、`num_steps` 默认 10 |
| `confucius4` | Confucius4-TTS | 8857 | `clone`（任务型） | `--ref-audio` | 长文本分段 + 交叉淡入淡出；`--language zh` |
| `audio8` | Audio8 | 8007 | `clone`（=/tts，返回 wav） | `--ref-audio` | 参考音频文字可留空（自动 ASR）；`--ref-text` 可加速 |
| `auk` | AuK | 8021 | `clone`（=/v1/generate，任务型） | `--ref-audio` | 文本由 `instruction` 承载（管家自动翻译）；`--param variant=...` |
| `breeze_tts` | Breeze-TTS-2 | 8860 | `clone` / `design` | `--ref-audio` + `--ref-text` | 24kHz 中英双语；**clone 必须给 `--ref-text`**；`--param cfg_scale=4` 增强指令遵循 |

## mode 与端点的关系

- `--mode plain`：用引擎的 `default_endpoint`（多数就是 clone）
- `--mode clone`：参考音频克隆
- `--mode design`：纯文字描述音色（需要 `--instruct`；只有 voxcpm / omnivoice / breeze_tts 支持）
- `--mode ultimate`：VoxCPM 终极克隆（`--ref-audio` + `--ref-text`）

## 任务型 vs 直返型（脚本已自动处理）

- **任务型**（voxcpm、indextts2、dots、confucius4、omnivoice、breeze_tts、auk）：提交返回 `task_id`，
  脚本轮询 `endpoints.task`（`/api/v1/tasks/{task_id}`）到 `completed/done`，再用 `endpoints.download` 取 wav。
  状态值：`pending` / `running` / `completed` / `failed`（IndexTTS-2.5 是 `idle/running/done/error`）。
- **直返型**（indextts25、omnivoice_story、audio8）：一次请求直接给 `file` 路径或 wav 字节。

## 默认值来自哪里

引擎默认参数在 `config/tts_hub.yaml` 的 `engines.<name>.defaults`，管家转发时会自动注入（请求里同名键优先）。
运行期可用 `PUT /api/hub/engines/{name}/config` 覆盖（写入 `config/tts_hub.overrides.yaml`）。
查询某引擎完整的参数表（含必填项、取值范围）：

```powershell
curl http://127.0.0.1:5190/api/hub/engines/indextts25/params
```

## 参考音频要求

- 5~15 秒、单人、无背景音乐、采样率 ≥16kHz 的 wav/mp3 最佳
- 传**服务端本地绝对路径**（脚本已把相对路径转绝对），比 multipart 上传更快更稳
- `--ref-text` 写参考音频的**原文**，不是要合成的新文本

## 音色库兜底：`--voice-id`（克隆端点免参考音频文件）

管家（5190）支持 `voice_id`：**clone 类端点没传参考音频时**，自动从项目音色库
（`voice/` 目录，索引 `voice/voice.json`）取音频当参考音频注入引擎。

- 取值：音色唯一 ID（`v` + 8 位十六进制，如 `vf5a1f65b`）/ 历史 ID / 音频文件名；`GET /api/voices` 可查。
- 优先级：`--ref-audio`（自己传的，以你为准） > `--voice-id` > 管家默认音色 `hub.default_voice`。
- `voice_id` 是**管家内部参数**，不会透传给引擎；命中后管家改写成 `ref_audio_path` + multipart 上传。
- 响应头回显：`X-Hub-Voice-Id` / `X-Hub-Voice-Source`（`request` = 来自 `voice_id`，`default` = 来自默认音色）。
- 音色不存在 / 音频文件缺失 → `400`（`detail.message` 为「音色不存在」或「音色音频文件缺失」）。
- 只对**端点名含 clone** 的克隆类端点生效（`clone` / `clone_with_pe` / `ultimate_clone`），design 端点无效。
- MiMo 在线接口不支持 `voice_id`（仍用 `--ref-audio`）。

```powershell
# 用音色库里的音色克隆，不用自己准备参考音频
py312env\python.exe tts-agent-skill\scripts\tts_agent.py run `
  --interface voxcpm --mode clone --voice-id vf5a1f65b --text "你好世界"
```

## 显存

`hub.max_active: 1` —— 同时只有一个引擎常驻。`run` 结束会自动停引擎；
若进程被强杀导致显存不释放，跑 `tts_agent.py stop` 或看 `logs/<engine>.log` 找残留 pid。

## 语音转写（ASR）：音频 → 文字

与上面所有 TTS 引擎不同，ASR **不属于合成引擎**、不占 `model` 选择，统一走管家的独立 ASR 子系统。
调用：`tts_agent.py asr --audio <路径>`，或直接打对外 API `POST /api/asr`。

### 两套后端（由 `tools/asr_factory.py` 裁决）

| 后端（`--backend`） | 模型（默认） | 依赖 / 缓存 | 特点 |
| --- | --- | --- | --- |
| `sensevoice`（默认） | `iic/SenseVoiceSmall` | funASR / ModelScope（`MODELSCOPE_CACHE`） | 非自回归，CPU 秒级（6s 音频约 1~2s 推理，首次加载 ~8s），推荐 |
| `whisper` | `openai/whisper-large-v3-turbo` | transformers / HuggingFace（`HF_HOME`） | 兼容旧配置，CPU 约 40s 级；需 torchcodec，慢 |

别名：`funasr` / `SenseVoice` → sensevoice；`fast_whisper` / `faster_whisper` → whisper；未知值回退 sensevoice。
环境变量 `LC_ASR_BACKEND` 可覆盖；管家配置 `hub.asr_backend` 亦可（`PUT /api/hub/config`）。

### 关键实现约束（排错必读）

- **独立子进程**：管家主进程（uvicorn）里 import torch / 初始化 CUDA 会死锁，转写一律由 `tts_hub/_asr_runner.py`
  子进程完成，跑完即退、不常驻模型/显存。`status` 里 `loaded=false, mode="subprocess"` 是正常现象。
- **输出文字稿已清理**：SenseVoice 的 `<|zh|>` / `<|NEUTRAL|>` 等标签、Whisper 的 CJK 空格都已压掉，
  可直接当克隆的参考文字稿用。SenseVoice 无原生词级时间戳。
- **默认 CPU**：`hub.asr_device` 留空即用 CPU（稳定、不抢 GPU 显存）；要更快可显式填 `cuda`。
- **离线**：子进程设 `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1`，模型须预先缓存
  （SenseVoice 走 ModelScope，Whisper 走 HuggingFace）。
- **失败不抛 5xx**：`/api/asr` 以 `ok=false` + `error` 返回转写失败，调用方按 `ok` 判断即可。

### 相关端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/asr` | 【对外】转写：JSON `{"path":...,"backend":...}` 或 multipart `file=@a.wav` |
| GET | `/api/hub/asr/status` | 当前 ASR 配置 + 最近一次转写状态（含 backend/model/elapsed/error） |
| POST | `/api/hub/asr/unload` | 兼容旧端点；子进程模式无需卸载，直接返回 ok |
| POST | `/api/hub/voices/{id}/transcribe` | 转写音色库里的音频并回写参考文字稿（入库场景） |
| POST | `/api/hub/voices` | 上传音色时 `auto_transcribe=1` 会自动转写一次并入库 |
