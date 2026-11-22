# 在线 MiMo TTS（小米 mimo-v2.5-tts）

云端接口，**不占本机显存、不拉起进程**，只要能联网 + 有 API Key 即可；
适合"本机显存被占 / 只想快速试一句 / 需要预置精品音色"的场景。

## 凭据

优先级：环境变量 `MIMO_API_KEY` > `mimotts_online/config.json` 的 `api_key`。
`api_base` 默认 `https://api.xiaomimimo.com/v1`（可用 `mimo_api_base` 或 `MIMO_API_BASE` 覆盖）。

## 三种模式（对应三个模型）

| `--mode` | 模型 | 需要的参数 | 说明 |
| --- | --- | --- | --- |
| `preset` | `mimo-v2.5-tts` | `--voice` | 预置精品音色，`--instruct` 可附加自然语言风格指令 |
| `design` | `mimo-v2.5-tts-voicedesign` | `--instruct`（音色描述，必填） | 用文字描述创造音色，无参考音频 |
| `clone` | `mimo-v2.5-tts-voiceclone` | `--ref-audio` | 参考音频（脚本自动 base64 成 data URL），`--instruct` 可选风格 |

预置音色：`mimo_default`、`冰糖`、`茉莉`、`苏打`、`白桦`、`Mia`、`Chloe`、`Milo`、`Dean`。

## 实际请求形态（脚本已封装，仅供排错参考）

`POST {api_base}/chat/completions`，OpenAI 兼容：

```json
{
  "model": "mimo-v2.5-tts",
  "messages": [
    {"role": "user", "content": "用温柔的语气"},
    {"role": "assistant", "content": "要合成的文本"}
  ],
  "audio": {"format": "wav", "voice": "冰糖"}
}
```

- `design`：`user` 放音色描述，`assistant` 放文本，`audio` 不带 `voice`
- `clone`：`audio.voice` 放 `data:audio/wav;base64,<...>`，`user` 可为空串
- 音频在 `choices[0].message.audio.data`（base64 wav）

## 常见错误

| 现象 | 原因 / 处理 |
| --- | --- |
| `401` / `Invalid token` | Key 不对或过期：改 `mimotts_online/config.json` 或设 `MIMO_API_KEY` |
| `Missing --ref-audio` | clone 模式必须给参考音频 |
| `MiMo 未返回音频` | 响应里没有 `audio.data`，把返回体贴给用户排查（多为额度/模型名问题） |
| 时间明显长于本机 | 走公网 + 排队，属正常；脚本超时给到 `--timeout` 默认 1800s |

## 与本机引擎的差别

| | 本机引擎（5190 管家） | MiMo 在线 |
| --- | --- | --- |
| 显存 | 占用，跑完自动释放 | 不占 |
| 启动 | 冷启动要加载模型（几十秒~几分钟） | 零启动 |
| 音色 | 克隆/设计自由度高（IndexTTS 中文最强） | 预置精品音色 + 声音设计 |
| 联网 | 不需要 | 必须 |
| pid | 有（记录在状态文件） | 无 |
