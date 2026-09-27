# OmniVoice / VoxCPM / DotTTS / IndexTTS API Reference

此文件基于项目文档 `y:\Omnivoice\vox_and_omni_API调用文档.md`，并核对了实际服务端与 Windows 启动脚本。

## 服务与启动

| service | model | base_url | port | Windows 启动脚本 |
|---|---|---|---|---|
| `vox` | VoxCPM | `http://localhost:8854` | `8854` | `y:\Omnivoice\启动_vox_api.bat` |
| `omni` | OmniVoice | `http://localhost:8853` | `8853` | `y:\Omnivoice\启动_omnivoice_api.bat` |
| `dot` | DotTTS | `http://localhost:8856` | `8856` | `y:\Omnivoice\启动_dots_api.bat` |
| `index` | IndexTTS | `http://localhost:8855` | `8855` | `y:\Omnivoice\启动_index_api.bat` |

## 通用规则

- 参考音频使用 `ref_audio` 或 `ref_audio_path` 二选一。
- 本机调用优先使用 `ref_audio_path`。
- 提交任务后，先取 `task_id`，再调用状态接口轮询。
- 状态终态为 `completed` 或 `failed`。
- 出错时服务通常返回：

```json
{
  "detail": "错误描述信息"
}
```

## 通用接口

### 健康检查

- `GET /health`

关键字段：

- `status`: `healthy` 或 `degraded`
- `model_loaded`: 模型是否加载
- `device`: 当前设备
- `gpu_available`: GPU 是否可用
- `running_tasks`: 运行中任务数
- `pending_tasks`: 排队任务数
- `max_workers`: 最大工作线程数

### 查询任务

- `GET /api/v1/tasks/{task_id}`

常见返回字段：

- `task_id`
- `task_type`
- `status`
- `progress`
- `message`
- `output_path`
- `rtf`
- `audio_duration`
- `inference_time`
- `error`

### 下载音频

- `GET /api/v1/voice/download/{task_id}`

### 实时推送

- `ws://localhost:{port}/ws/tasks/{task_id}`

## VoxCPM

### `POST /api/v1/voice/clone`

用途：

- 可控克隆
- 支持风格指令

表单参数：

- 必填: `text`
- 参考音频: `ref_audio` 或 `ref_audio_path`
- 可选: `instruct`, `speed`, `language`, `output_path`, `cfg_value`, `inference_timesteps`, `normalize`, `denoise`

`speed` 参数（0.5-2.0）映射为语速提示词拼接到 instruct 最前面：

| speed 范围 | 映射提示词 |
|-----------|----------|
| ≤ 0.5 | 语速很慢 |
| 0.5~0.75 | 语速偏慢 |
| 0.75~1.0 | 语速略微偏慢 |
| 1.0~1.25 | 语速略微偏快 |
| 1.25~1.5 | 语速偏快 |
| > 1.5 | 语速很快 |

### `POST /api/v1/voice/ultimate_clone`

用途：

- 极致克隆
- 音频续写

表单参数：

- 必填: `text`
- 参考音频: `ref_audio` 或 `ref_audio_path`
- 可选: `prompt_text`, `speed`, `language`, `output_path`, `cfg_value`, `inference_timesteps`, `normalize`, `denoise`

> 当传入 `speed` 参数时，自动切换为指令克隆模式（reference_wav_path + 语速 instruct），不再使用续写模式。

### `POST /api/v1/voice/design`

用途：

- 纯文本声音设计

表单参数：

- 必填: `text`, `instruct`
- 可选: `speed`, `language`, `output_path`, `cfg_value`, `inference_timesteps`, `normalize`

## OmniVoice

### `POST /api/v1/voice/clone`

用途：

- 声音克隆

表单参数：

- 必填: `text`
- 参考音频: `ref_audio` 或 `ref_audio_path`
- 可选: `ref_text`, `instruct`, `language`, `output_path`, `num_steps`, `guidance_scale`, `speed`, `duration`, `denoise`, `max_workers`

### `POST /api/v1/voice/design`

用途：

- 纯文本声音设计

表单参数：

- 必填: `text`, `instruct`
- 可选: `language`, `output_path`, `num_steps`, `guidance_scale`, `speed`, `duration`, `denoise`

支持的 OmniVoice 设计提示常见要素：

- 性别: `male`, `female`
- 年龄: `child`, `teenager`, `young adult`, `middle-aged`, `elderly`
- 音调: `very low pitch`, `low pitch`, `moderate pitch`, `high pitch`, `very high pitch`
- 风格: `whisper`
- 英语口音: `american accent`, `british accent`, `australian accent`, `indian accent`
- 汉语方言: `四川话`, `陕西话`, `河南话`, `东北话`

## DotTTS

### `POST /api/v1/voice/clone`

用途：

- 声音克隆（支持续写克隆、X-vector 克隆、指令克隆）

表单参数：

- 必填: `text`
- 参考音频: `ref_audio` 或 `ref_audio_path`
- 可选: `prompt_text`, `instruct`, `language`, `output_path`, `num_steps`, `guidance_scale`, `speaker_scale`, `normalize_text`, `max_generate_length`, `seed`

`instruct` 参数：提供声音风格控制指令（如 "温柔的语气说"），使用 instruction_tts 模板。当提供 instruct 时自动切换为指令克隆模式。

## IndexTTS

### `POST /api/v1/voice/clone`

用途：

- 声音克隆（支持情感控制）

表单参数：

- 必填: `text`
- 参考音频: `spk_audio` 或 `spk_audio_path`
- 可选: `instruct`, `emo_control_method`, `emo_audio`/`emo_audio_path`, `emo_vector`, `emo_text`, `emo_alpha`, `use_random`, `output_path`, `max_text_tokens_per_segment`, `do_sample`, `top_p`, `top_k`, `temperature`, `num_beams`, `repetition_penalty`, `length_penalty`, `max_mel_tokens`

`instruct` 参数：提供自然语言情感描述（如 "害怕、紧张的语气"），自动使用 text 情感控制模式，将指令送入 Qwen 情感模型提取情感向量。

## 典型流程

1. `health` 检查服务可用。
2. 调用 `clone`、`ultimate-clone` 或 `design`。
3. 记录 `task_id`。
4. 轮询 `status` 或使用 `wait`。
5. 读取 `output_path`，必要时再 `download`。
