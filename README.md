# Open TTS Server Hub

> 一个 Python 环境，聚合当下最热门的开源 TTS 引擎；统一 API、统一模型管理、统一测试页 —— 告别为每个引擎重复搭环境的痛苦。

[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org)
[![API](https://img.shields.io/badge/API-FastAPI-red.svg)](https://fastapi.tiangolo.com)

---

## 为什么需要它

玩过多个开源 TTS 项目的人都知道：每个项目都有自己的 Python 版本、自己的 `transformers`/`torch` 补丁、自己的模型下载方式。
想在同一台机器上同时用 IndexTTS、OmniVoice、VoxCPM、AuK…… 往往要切来切去地重建环境，磁盘和显存都被重复占用。

**Open TTS Server Hub** 把这 8 个热门开源 TTS 引擎收进**同一个仓库、同一个 Python 3.12 环境**，并统一封装成一套 API：
你只装一次依赖，就能按需调用任意引擎，用到哪个接口才下载哪个模型。

---

## ✨ 核心优势

### 1. 一个环境解决所有热门 TTS 开源项目
无需为每个引擎单独搭建、复制、维护一套 Python 环境。
所有引擎共享同一份依赖与工具层（`tools/`），并统一接入模型缓存（`models/`），互不复刻权重。

已加入的开源 TTS 项目：

| 引擎目录 | 上游开源项目 | 默认端口 | 主要能力 |
| --- | --- | --- | --- |
| `Audio8_TTS` | [Audio8/Audio8-TTS](https://github.com/Audio8/Audio8-TTS)（Preview 0.6B） | 8007 | 音色克隆 / 纯文本合成 |
| `AuK` | [Tencent-Hunyuan/AuK](https://github.com/Tencent-Hunyuan/AuK)（+AuK-Flash） | 8021 | 零样本 TTS、Instruct TTS、语音编辑/增强 |
| `Confucius4-TTS` | Confucius4-TTS | 8857 | 声音克隆 |
| `dots` | [rednote-hilab/dots.tts-soar](https://github.com/rednote-hilab/dots.tts) | 8856 | 声音克隆 / 设计 |
| `index_tts2` | [IndexTeam/IndexTTS-2](https://github.com/index-team/IndexTTS) | 8855 | 声音克隆 / 情绪控制 |
| `index_tts25` | IndexTTS-2.5 | 8858 | 声音克隆 / 设计（新版） |
| `omnivoice` | [k2-fsa/OmniVoice](https://github.com/k2-fsa/OmniVoice) | 8853 | 声音克隆 / 设计 |
| `VoxCPM` | [openbmb/VoxCPM2](https://github.com/OpenBMB/VoxCPM) | 8854 | 声音克隆 / 终极克隆 / 设计 |

### 2. 完善的 API 接口 + 调用文档 + 一键生成调用 Skill
每个引擎都封装为独立的 **FastAPI** 服务（`*_api_server.py`），接口风格高度统一：

- 健康检查：`GET /api/health`
- 声音克隆：`POST /api/v1/voice/clone`
- 声音设计：`POST /api/v1/voice/design`

每个服务自带 **Swagger / OpenAPI 文档**（`http://localhost:<端口>/docs`），参数、响应一目了然。

并且项目内置可直接复用的 **Agent 调用 Skill**（如 `omnivoice-tts-skill/SKILL.md` + `invoke_tts_api.py`），
配合统一 OpenAPI，可为任意引擎快速派生出"交给 AI Agent 一键调用"的 Skill，让智能体直接操作本地 TTS 服务。

### 3. 便捷的测试页面与 WebUI
- 根目录提供**聚合测试与调用平台** `api_聚合快速调用测试.html`：一个页面同时勾选、对比、调用全部引擎，
  自带差异化参数面板（语速、方言、情绪、去噪、随机采样等）。
- 各引擎也自带 WebUI（如 `index_tts25/webui.py`、`index_tts2/webui.py`、`Audio8_TTS/WebUI.py`、`AuK/app.py`），开箱即用。

### 4. 线程优化 + flash_attn 加速
- API 服务基于**异步 + 线程池**架构，支持并发合成与轮询式任务，充分利用多核算力，提升批量/并行请求吞吐。
- 集成 **FlashAttention（`flash_attn==2.8.3`）** 加速注意力计算，降低显存占用、提升推理速度；
  Windows 预编译 wheel 已归档于 `whl/`（见下方「依赖与 flash_attn」）。

### 5. 模型自动化管理（用到即下，国内源）
模型统一由 `tools/models_manager.py` 管理，首次调用接口时**按需自动下载**，并存于 `models/`：

- 支持 **HuggingFace Hub** 与 **ModelScope（魔搭，国内镜像）** 双来源；
- 按引擎登记所需文件（`required_files` / `allow_patterns`），只拉取推理必需权重，不浪费带宽；
- 模型自动缓存、跨引擎复用（`candidate_dirs`），避免重复下载。

国内网络环境下优先走 ModelScope 等镜像，下载更快更稳。

---

## 项目结构

```
Open-TTS-SeverHub/
├── audio8_api_server.py      # Audio8  API 服务
├── auk_api_server.py         # AuK     API 服务
├── confucius4_api_server.py   # Confucius4-TTS API 服务
├── dots_tts_api.py           # dots.tts API 服务
├── index_tts25/index_api_server.py   # IndexTTS-2.5 API 服务
├── omnivoice/omni_api_server.py       # OmniVoice API 服务
├── VoxCPM/voxcpm-api_server.py        # VoxCPM API 服务
├── api_聚合快速调用测试.html   # 聚合测试/调用平台
├── tools/                    # 共享工具层：模型管理 / ASR / 文本归一化
│   ├── models_manager.py     # 模型自动下载与管理
│   ├── asr.py                # Whisper 语音识别（克隆参考文本）
│   └── text_normalize.py     # 中英文文本规范化
├── packages/index25/        # 补丁版 transformers 等（开箱即用，已随仓库）
├── Audio8_TTS/  AuK/  Confucius4-TTS/  dots/  index_tts2/  index_tts25/  omnivoice/  VoxCPM/
├── whl/                      # Windows 预编译 wheel（如 flash_attn，不入库）
├── models/                   # 模型权重（运行时自动下载，不入库）
├── requirements.txt          # 共享层运行时依赖
├── docs/依赖说明.md           # 详细依赖与安装说明
└── .gitattributes            # 超大词典（unidic sys.dic 179MB）走 Git LFS
```

---

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/licorxj/Open-TTS-ServerHub.git
cd Open-TTS-SeverHub
```

### 2. 准备 Python 3.12 环境

```bash
python -m venv py312env
py312env\Scripts\activate        # Windows
# source py312env/bin/activate   # Linux / macOS

python -m pip install -U pip
pip install -r requirements.txt   # 共享聚合层依赖
```

> 各引擎还有自己的依赖（见各自 `requirements.txt` / `pyproject.toml`），按需安装。
> **IndexTTS-2.5** 依赖补丁版 transformers，需使用专属环境 `index25env` 并把 `packages/index25` 置于 `PYTHONPATH` 最前。
> 详见 [docs/依赖说明.md](docs/依赖说明.md)。

### 3. 安装 flash_attn（可选加速，Windows）

```bash
pip install --no-deps whl/flash_attn-2.8.3+cu128torch2.8-cp312-cp312-win_amd64.whl
```

> 版本须与本地 python(3.12) / torch(2.8+cu128) / CUDA(12.8) 严格匹配；其他平台请按官方说明安装对应 wheel。

### 4. 启动引擎 API 服务

仓库根目录提供一键启动脚本（`.bat`），例如：

```bash
启动_omnivoice_api.bat      # OmniVoice  -> 8853
启动_vox_api.bat            # VoxCPM     -> 8854
启动_index_api.bat          # IndexTTS-2 -> 8855
启动_dots_api.bat           # dots.tts   -> 8856
启动_confucius4_api.bat     # Confucius4 -> 8857
启动_index25_api.bat        # IndexTTS-2.5 -> 8858
启动_audio8_api.bat         # Audio8     -> 8007
启动_auk_api.bat            # AuK        -> 8021
```

或手动启动任一服务：

```bash
python omnivoice/omni_api_server.py --host 0.0.0.0 --port 8853 --device auto
```

首次调用某引擎接口时，模型会经 `tools/models_manager` 自动下载到 `models/`。

### 5. 打开测试页 / WebUI

- 双击根目录 `api_聚合快速调用测试.html`，在浏览器中勾选引擎、填写文本与参考音频，即可对比调用全部 TTS 服务。
- 各引擎 WebUI：`python index_tts25/webui.py` 等。

---

## API 调用

每个引擎服务默认端口如下，接口风格统一：

| 引擎 | 端口 | 健康检查 | 克隆 | 设计 |
| --- | --- | --- | --- | --- |
| OmniVoice | 8853 | `/api/health` | `/api/v1/voice/clone` | `/api/v1/voice/design` |
| VoxCPM | 8854 | `/api/health` | `/api/v1/voice/clone` | `/api/v1/voice/design` |
| IndexTTS-2 | 8855 | `/api/health` | `/api/v1/voice/clone` | `/api/v1/voice/design` |
| dots.tts | 8856 | `/api/health` | `/api/v1/voice/clone` | `/api/v1/voice/design` |
| Confucius4-TTS | 8857 | `/api/health` | `/api/v1/voice/clone` | （仅克隆） |
| IndexTTS-2.5 | 8858 | `/api/health` | `/api/v1/voice/clone` | `/api/v1/voice/design` |
| Audio8 | 8007 | `/api/health` | `/api/v1/voice/clone` | `/api/v1/voice/design` |
| AuK | 8021 | `/api/health` | `/api/v1/voice/clone` | `/api/v1/voice/design` |

### 调用示例（克隆）

```bash
curl -X POST http://localhost:8853/api/v1/voice/clone \
  -F "text=今天天气真不错" \
  -F "ref_audio_path=VoxCPM/examples/example.wav" \
  -F "language=zh"
```

- 完整参数与响应字段见每个服务的 **Swagger 文档**：`http://localhost:<端口>/docs`
- 想交给 AI Agent 调用？参考 `omnivoice-tts-skill/SKILL.md`，并基于对应 `/docs` 为任意引擎快速生成调用 Skill。

---

## 模型自动化管理

`tools/models_manager.py` 以 `MODEL_REGISTRY` 登记各引擎模型来源与所需文件：

- `vox` / `omnivoice` / `audio8`：HuggingFace Hub
- `indextts` / `dots` / `auk` / `auk_flash` / `qwen_omni`：ModelScope（国内镜像，下载更快）
- 首次调用接口时自动 `snapshot_download` 至 `models/`，后续直接复用；
- 支持 `allow_patterns` 只拉推理必需文件，并支持跨引擎候选目录复用。

---

## 依赖与 flash_attn

- 共享层依赖见根目录 `requirements.txt`（FastAPI / uvicorn / huggingface_hub / modelscope / torch / transformers 等）。
- 各引擎差异依赖见各自 `requirements.txt` / `pyproject.toml`。
- `flash_attn==2.8.3`（cu128 / torch2.8 / cp312 / win_amd64）预编译 wheel 归档于 `whl/`，用于加速注意力计算。
- 完整安装顺序、版本补丁（`packages/index25`）、模型下载与启动入口见 [docs/依赖说明.md](docs/依赖说明.md)。

---

## 文档

- [依赖说明](docs/依赖说明.md)：结构、Python 环境、共享层与各引擎依赖对照、补丁说明、安装顺序、模型下载与启动入口。

---

## 许可证与声明

- 本仓库为各开源 TTS 引擎的**聚合与统一封装**，各引擎的权重与代码版权归其原始开源项目所有，使用时请遵守各自许可证。
- 模型权重**不随仓库分发**，运行时应按各引擎许可从官方/HuggingFace/ModelScope 下载。
