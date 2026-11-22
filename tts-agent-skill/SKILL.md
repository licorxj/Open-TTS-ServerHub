---
name: tts-hub-api
description: 调用本项目的 TTS 语音合成接口（IndexTTS-2/2.5、VoxCPM、OmniVoice、dots.tts、Confucius4、Audio8、AuK、Breeze-TTS-2 与本机 5190 管家），以及在线 MiMo（mimo-v2.5-tts）接口做预置音色/音色设计/音色克隆；也支持语音转写 ASR（SenseVoice 默认 / Whisper，把音频转成文字）。用于「把这段文字转成语音」「克隆这个声音」「配音」「TTS」「text to speech」「voice clone」「把音频转成文字」「语音识别」「ASR」「转写」等请求。合成前必须先向用户确认用哪个接口或沿用上次接口；脚本会自动拉起 API、记录 pid，任务结束自动关闭释放显存。
---

# LcTTS 智能体调用（TTS Hub API）

本 skill 覆盖两类任务：

- **语音合成（TTS）**：文字 → 音频，需要先选接口（铁律一）。
- **语音转写（ASR）**：音频 → 文字，走管家统一 ASR（SenseVoice 默认 / Whisper），**不需要选接口**，见下方「语音转写」章节。

所有操作都通过一个 CLI 完成，**不要**自己拼 curl 去打引擎端口，也不要去双击 `.bat`。

```powershell
py312env\python.exe tts-agent-skill\scripts\tts_agent.py <子命令> ...
```

> 上面命令以**项目根 `Y:\LcTTSHub`** 为当前目录。若从 skill 目录（`.codebuddy/skills/tts-hub-api`，它是 `tts-agent-skill` 的软链接）执行，改用
> `..\..\..\py312env\python.exe scripts\tts_agent.py <子命令> ...`（脚本内部按自身位置定位项目根，任意 cwd 都能跑）。

| 子命令 | 作用 |
| --- | --- |
| `interfaces` | 列出可用接口：本机引擎（含端口/端点/说明）+ 在线 MiMo；标出「上次使用」 |
| `last` | 只看上次用的接口、模式、参考音频 |
| `run` | 合成一句话 → 落盘 wav → 自动关闭进程 |
| `asr` | 语音转写：音频文件 → 文字（默认 SenseVoice，不需要选接口） |
| `status` | 管家是否在跑、哪些引擎活着、记录的 pid |
| `stop` | 手动关闭本次拉起的引擎与管家（释放显存） |

## 铁律一：合成任务先问用户用哪个接口

> 仅适用于**语音合成（run）**。语音转写（asr）不需要选接口，直接走管家统一 ASR，见下方「语音转写（ASR）」章节。

**每次开始新的配音/合成任务前**，先执行 `interfaces`（必要时加 `last`），然后用提问工具问用户，二选一/多选：

- 选项 1：**沿用上次接口** —— 仅当 `last` 有记录时提供，写明「上次：`<接口名>`（模式 `<mode>`）」
- 选项 2..N：本机引擎（`indextts25` / `voxcpm` / `omnivoice` / `dots` / `confucius4` / `audio8` / `auk` / `breeze_tts`）
- 选项 N+1：`mimo`（在线小米 MiMo，不占显存、不走本机 GPU）

问法示例（直接照这个结构问，不要自作主张选一个）：

> 用哪个 TTS 接口？
> 1. 沿用上次：`indextts25`（clone 模式）
> 2. 本机 IndexTTS-2.5（推荐中文/情绪控制）
> 3. 本机 VoxCPM2（声音设计/终极克隆）
> 4. 在线 MiMo（不占显存，需联网 + API Key）

用户答「上次」「还是刚才那个」→ 传 `--interface last`；用户没说、也拒绝回答时，**默认 `--interface last`**（有记录时），并在结果里说明用的是哪个。

## 铁律二：脚本负责拉起与关闭，不要绕过它

`run` 内部顺序：

1. 管家 `127.0.0.1:5190` 没起就用 `py312env\python.exe` 后台拉起（日志 `logs/hub_agent.log`）；
2. 把 pid 写进 `tmp/tts_agent_state.json`；
3. 调 `POST /api/tts?model=<引擎>`，管家按需拉起引擎（首次加载模型可能几分钟，默认超时 1800s）；
4. 结果落盘到 `outputs/agent/tts_<引擎>_<时间>.wav`（或 `--out`）；
5. **无论成功失败**都会停引擎；管家若是本脚本拉起的也一并关掉，显存立即释放。

需要保留进程连续做多条（例如一次做 20 句）时才加 `--keep`，做完必须手动跑 `stop`。

## 标准流程

```powershell
# 1. 看接口 + 上次记录
py312env\python.exe tts-agent-skill\scripts\tts_agent.py interfaces

# 2. （问过用户后）克隆音色合成
py312env\python.exe tts-agent-skill\scripts\tts_agent.py run `
  --interface index25 --mode clone `
  --ref-audio voice/sample.wav --ref-text "参考音频的文字" `
  --text "要合成的文本" --out outputs/agent/demo.wav

# 3. 音色设计（不需要参考音频，靠文字描述）
py312env\python.exe tts-agent-skill\scripts\tts_agent.py run `
  --interface vox --mode design --instruct "年轻女性，温柔，语速偏慢" --text "你好"

# 2b. 用项目音色库里的音色克隆（不必自己准备参考音频文件）
#     voice_id 可用唯一 ID / 历史 ID / 音频文件名；先 GET http://127.0.0.1:5190/api/voices 查
py312env\python.exe tts-agent-skill\scripts\tts_agent.py run `
  --interface voxcpm --mode clone --voice-id vf5a1f65b --text "要合成的文本"

# 4. 在线 MiMo（三种模式）
py312env\python.exe tts-agent-skill\scripts\tts_agent.py run --interface mimo --mode preset --voice 冰糖 --text "你好"
py312env\python.exe tts-agent-skill\scripts\tts_agent.py run --interface mimo --mode design --instruct "低沉男声，纪录片旁白" --text "你好"
py312env\python.exe tts-agent-skill\scripts\tts_agent.py run --interface mimo --mode clone --ref-audio voice/sample.wav --text "你好"

# 5. 出错/被中断后清理残留
py312env\python.exe tts-agent-skill\scripts\tts_agent.py stop
```

多条文本批量时，推荐 `--keep` 连续跑，最后 `stop`：

```powershell
foreach ($line in $texts) { ... run --keep --interface last --text $line ... }
py312env\python.exe tts-agent-skill\scripts\tts_agent.py stop
```

## 参数（规范名由管家翻译成各引擎原生名）

| 参数 | 说明 |
| --- | --- |
| `--text` / `-t` | 必填，要合成的文本 |
| `--ref-audio` | 参考音频路径（`clone` 必需）；支持相对项目根，脚本会转绝对路径 |
| `--voice-id` | 音色库唯一 ID（`v`+8 位十六进制，如 `vf5a1f65b`）/历史 ID/音频文件名；**clone 时可替代 `--ref-audio`**，管家自动把音色库音频当参考音频注入（`GET /api/voices` 可查；仅本机引擎，MiMo 不支持） |
| `--ref-text` | 参考音频对应文字；Breeze / VoxCPM ultimate 必需 |
| `--instruct` | 音色/情绪指令；`design` 模式必填，MiMo design 也用它 |
| `--voice` | 仅 MiMo preset：`冰糖`/`茉莉`/`苏打`/`白桦`/`Mia`/`Chloe`/`Milo`/`Dean` |
| `--language` | `zh`/`en`/`ja` 等（按引擎支持） |
| `--speed` | 语速因子（engine 范围不同，IndexTTS-2.5 为 0.3~3.0） |
| `--param K=V` | 引擎独有参数，可重复，自动识别数字/布尔/JSON |
| `--mode` | `plain`（默认端点）/`clone`/`design`/`ultimate`（VoxCPM 终极克隆） |
| `--out` | 输出路径，默认 `outputs/agent/tts_<engine>_<ts>.wav` |
| `--keep` | 不关闭进程（批量时用，结束记得 `stop`） |
| `--json` | 机器可读结果（含 `output_path` / `task_id` / `engine_pid`） |

引擎能力、端点、必填参数见 `references/interfaces.md`；在线 MiMo 细节见 `references/mimo_online.md`。

## 语音转写（ASR）：音频 → 文字

把一段音频转成文字。**不需要问用户选接口**——统一走管家 `POST /api/asr`，默认 SenseVoice（funASR，CPU 秒级），Whisper 作兼容备选。脚本会自动拉起管家、转写完关闭，模型在独立子进程里加载，跑完即退、不占常驻显存。

```powershell
# 转写本地音频（相对项目根或绝对路径均可）
py312env\python.exe tts-agent-skill\scripts\tts_agent.py asr `
  --audio outputs/agent/demo.wav

# 显式指定后端 / 设备；机器可读输出
py312env\python.exe tts-agent-skill\scripts\tts_agent.py asr `
  -a voice/sample.wav --backend sensevoice --device cpu --json
```

| 参数 | 说明 |
| --- | --- |
| `--audio` / `-a` | **必填**，要转写的音频文件路径（相对项目根或绝对路径） |
| `--backend` | `sensevoice`（默认，快）/ `whisper`；留空用管家配置 `hub.asr_backend` |
| `--model` | 模型名，留空用后端默认（SenseVoiceSmall / whisper-large-v3-turbo） |
| `--device` | `cpu`（默认）/ `cuda`；留空用管家配置 `hub.asr_device` |
| `--timeout` | 转写超时秒数；0 = 用管家配置 `hub.asr_timeout`（默认 120s） |
| `--json` | 机器可读输出（含 `text` / `backend` / `model` / `elapsed`） |
| `--no-start` | 不允许自动拉起管家（必须已在运行） |

对外 HTTP 接口（供非本 skill 的程序调用）：

```http
POST http://127.0.0.1:5190/api/asr
Content-Type: application/json

{"path": "outputs/agent/demo.wav", "backend": "", "device": ""}
```

也可用 multipart 上传：`file=@a.wav`（可选表单字段 `backend` / `model` / `device` / `timeout`）。返回 `{"ok": true, "text": "...", "error": "", "elapsed": 1.4, "backend": "sensevoice", "model": "iic/SenseVoiceSmall", "device": "cpu"}`；失败 `ok=false` + `error`，不抛 5xx。后端/模型配置见 `references/interfaces.md` 的「语音转写（ASR）」小节。

## 汇报给用户的内容

合成任务（run）：

- 用的是哪个接口、哪种模式
- 输出文件的绝对路径、时长/大小（能拿到就报）
- 已自动关闭（`引擎 pid=… 已停止，管家已关闭，显存已释放`）；若加了 `--keep`，提醒用户执行 `stop`

转写任务（asr）：

- 转写出的文字原文（这是主要交付物，直接贴在回复里）
- 用的后端（默认 SenseVoice）与耗时（`--json` 时有）
- 失败时把 `error` 原样说明（如音频不存在、格式不支持、模型加载失败）

## 排错顺序

1. `status`：管家/引擎是否在跑、pid 是否与状态文件一致
2. 看引擎日志 `logs/<engine>.log` 尾部（模型加载、显存不足、参考音频格式错误都在这里）
3. 管家日志 `logs/hub_agent.log`
4. 首次加载慢：不要急着判定失败，`--timeout` 默认 1800s
5. 输出 wav 存在但静音（RMS=0）：多半是半精度 NaN，见 `README` / 引擎相关说明，不要反复重试
6. 卡住或中断后用 `stop` 清残留，避免显存被旧进程占着
7. 引擎回 `422 {"detail":[{"loc":["body","text"],...}]}`：几乎都是**请求体不是合法 JSON**被原样转发
   （PowerShell 下 `-d "{\"text\":...}"` 的引号会被吃掉）。管家现在会先拦：
   `Content-Type: application/json` 但体非法 → 直接 `400 请求体解析失败: ... 不是合法 JSON`。
   手工 curl 请把 JSON 写进文件后 `--data-binary "@body.json"`；用脚本则不会踩坑（urllib 发标准 JSON）。
8. 引擎回 `400` 且提示缺参数：管家有「端点级必填契约」，`detail.missing` 会列出缺哪个（如 Breeze clone 缺 `ref_text`）。
9. 需要看管家转发决策时：设环境变量 `TTS_HUB_DEBUG=1` 重启管家，日志落在 `tts_hub_debug.log`
   （记录转发体槽位 json/data/files、注入的音色与字段、别名归一化明细）。

## 已知环境问题：OSError 1455「页面文件太小」

管家返回 `503 引擎进程提前退出`，日志尾部是：

```
File ".../transformers/modeling_utils.py", line 4490, in from_pretrained
    with safe_open(checkpoint_files[0], framework="pt") as f:
OSError: 页面文件太小，无法完成操作。 (os error 1455)
```

这不是 skill 或引擎代码的问题，是**系统提交内存不够**（模型加载要 mmap 大文件）。
手工用 `.bat` 起引擎也一样会崩。处理顺序：

1. 关掉不需要的重型进程（其它项目的 uvicorn/celery、浏览器多标签、Electron 应用），再重跑；
2. 或调大页面文件（系统属性 → 高级 → 性能 → 虚拟内存），重启后重跑；
3. 仍不行就别在本机跑本地引擎，用 `--interface mimo`（在线，不占内存）。

判断是否属于这类问题：`直接启动引擎的报错完全一致` + 系统 `CommitUsed` 逼近 `CommitLimit`（任务管理器“提交大小”）。

## 不要做

- 不要并行跑两个本机引擎（`max_active: 1`，且会互相抢显存）
- 不要直接 `taskkill python.exe`（会留下引擎子进程占显存；用 `stop`）
- 不要修改 `config/tts_hub.yaml` 里的端口/路径来"绕开"问题
- 不要在未询问用户的情况下换成另一个引擎重试（音色会变）
- 不要在 clone 模式下既不给 `--ref-audio` 也不给 `--voice-id`（没有参考音频克隆不了）
- 不要用 `-d "{\"text\":\"...\"}"` 这种写法在 PowerShell 里手工拼 JSON（引号会被吃掉，引擎回 422）
- 语音转写不要用 TTS 引擎去做（引擎内的 ASR 只为克隆参考文字服务）；直接用 `asr` 子命令或 `/api/asr`
- 转写长音频要加大 `--timeout`（默认跟随 `hub.asr_timeout`，首次加载模型较慢）
