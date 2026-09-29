# TTS API 管家使用文档

> 一个 FastAPI 进程，统一托管仓库里所有 TTS 引擎 API 服务。
> **调用端只用 `model` 名称说话**：管家自动卸载当前引擎、拉起目标引擎，并把参数、通信、输出全部透传。

---

## 1. 它解决什么问题

| 痛点 | 管家做法 |
| --- | --- |
| 每个引擎一个端口、一个进程，全启动显存直接爆 | 管家**同时只常驻 1 个引擎**（`hub.max_active`），切 `model` 就自动卸载上一个 |
| 各引擎参数名五花八门 | 参数**原样透传**，不做翻译；同时用注册表把参数表暴露出来供查询 |
| 不知道某引擎支持哪些参数 | `GET /api/hub/engines/{name}/params` 直接返回完整参数表（类型/默认值/范围/说明） |
| 想改默认参数却要改 yaml 再重启 | `PUT /api/hub/engines/{name}/config` 在线改，持久化 |
| 长音频 / SSE 进度 / 任务轮询 | 全部 `StreamingResponse` 透传，状态码与 `Content-Type` 原样保留 |

管家自身**不加载任何模型**，内存占用极低，可长期常驻。

---

## 2. 启动

三种启动方式，按场景选：

| 脚本 | 场景 | 说明 |
| --- | --- | --- |
| **`启动TTS管家桌面端.bat`** | **日常使用** | 后台无控制台窗口常驻（`pythonw`），自动等待就绪后用浏览器「应用模式」打开面板，体验接近桌面软件。关闭用配套的 `关闭TTS管家桌面端.bat` |
| `启动_TTS管家.bat` | 需要看实时日志 | 前台运行，管家输出直接打印；关窗口即停 |
| `启动_TTS管家_开发.bat` | 改前端代码 | 后端 5199 + Vite 5180，热更新（需 Node 18+） |

> ⚠ 实测 **Edge 的 `--app=` 模式有缺陷**：它只取回 `index.html`，却不加载后续 JS/CSS，
> 窗口表现为全白（管家日志里只有一次 `GET /ui`，没有任何 `/ui/assets/*` 请求）。这是 Edge 侧问题，与页面无关。
> 脚本因此**优先使用 Chrome 的 `--app` 模式**（无地址栏、无标签页，窗口标题即页面标题 —— 实测正常）；
> 只有机器上没有 Chrome 时才退回 Edge 的 `--new-window` 普通打开，保证能用。
> 想让 Edge 也去掉地址栏：在 Edge 里打开面板后，菜单 → 应用 → 安装此站点为应用。

或手动：

```bash
py312env\python.exe tts_hub_server.py                 # 默认 0.0.0.0:5199
py312env\python.exe tts_hub_server.py --port 9100
py312env\python.exe tts_hub_server.py --host 127.0.0.1
```

- 面板：<http://localhost:5199/ui>
- 文档：<http://localhost:5199/docs>

新增依赖：`httpx>=0.27`（已写入根 `requirements.txt`）。

> 面板是 `frontend/`（Vue 3 + Element Plus）的构建产物，已随仓库发布在 `tts_hub/static/`，
> **运行时不需要 Node**。若该目录缺失，访问 `/ui` 会显示构建指引页，接口不受影响。

---

## 3. 核心概念

### 3.1 model 名称

`model` 可以是**引擎 key、display_name 或任意 alias**，大小写不敏感。例如 `voxcpm` / `vox` / `VoxCPM2` 都指向同一个引擎。

```http
GET /api/hub/models          # 列出全部可用名称与别名
```

| model（key） | 别名 | 引擎 | 端口 |
| --- | --- | --- | --- |
| `voxcpm` | `vox` `voxcpm2` `VoxCPM` | VoxCPM2 | 8854 |
| `omnivoice` | `omni` `OmniVoice` | OmniVoice | 8853 |
| `omnivoice_story` | `omni_story` `story` `说书` | OmniVoice 说书版 | 8853 |
| `indextts2` | `index` `index2` `IndexTTS-2` | IndexTTS-2 | 8855 |
| `indextts25` | `index25` `IndexTTS-2.5` | IndexTTS-2.5 | 8858 |
| `dots` | `dots_tts` `dot` | dots.tts | 8856 |
| `confucius4` | `confucius` `Confucius4` | Confucius4-TTS | 8857 |
| `audio8` | `audio8_tts` `Audio8` | Audio8 | 8007 |
| `auk` | `AuK` | AuK | 8021 |

> `omnivoice` 与 `omnivoice_story` 端口都是 8853（脚本内硬编码）。管家保证同一时刻只有一个活跃引擎，所以不冲突。

### 3.2 model 从哪里读（三选一，优先级从高到低）

1. Query：`POST /api/tts?model=voxcpm`
2. 请求头：`X-TTS-Model: voxcpm`
3. JSON 请求体字段：`{"model": "voxcpm", "text": "..."}`（会被摘掉，不透传给引擎）

### 3.3 endpoint

引擎的多个接口用 `endpoints` 注册（clone / design / ultimate_clone / task / download / progress …）。

- 不传 → 用 `default_endpoint`（一般是 `clone`）
- `POST /api/tts/design?model=voxcpm`
- 或 query：`POST /api/tts?model=voxcpm&endpoint=design`
- 或请求头：`X-TTS-Endpoint: design`
- 也可以直接写真实路径：`POST /api/tts/api/v1/voice/design?model=voxcpm`

---

## 4. 接口清单

### 4.1 查询类（给请求方"发现能力"用）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/hub/models` | 所有可用 model 名 + 别名 + 端口 |
| GET | `/api/hub/engines` | 所有引擎概览（`?with_params=true` 附带参数表） |
| GET | `/api/hub/engines/{name}` | 引擎详情（含 runtime / endpoints / params） |
| GET | `/api/hub/engines/{name}/params` | **支持参数表**：类型、默认值、范围、是否必填、文件字段 |
| GET | `/api/hub/engines/{name}/config` | 当前可修改配置 + 已有的运行期覆盖 |
| GET | `/api/hub/config` | 管家级配置 |

### 4.2 配置修改类

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| PUT | `/api/hub/engines/{name}/config` | 合并修改引擎配置并持久化 |
| POST | `/api/hub/engines/{name}/config/reset` | 恢复 `config/tts_hub.yaml` 原始值 |
| PUT | `/api/hub/config` | 修改管家级配置 |

**可改字段**：`enabled` `display_name` `aliases` `description` `runtime` `server` `health`
`endpoints` `default_endpoint` `defaults` `file_fields` `body_mode`。
`params`（接口能力声明）不可改——它描述引擎"支持什么"，不是配置项。

修改写入 `config/tts_hub.overrides.yaml`（覆盖层），**不会破坏主注册表的注释**；删掉该文件即恢复出厂。

### 4.3 生命周期类

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/hub/status` | 当前活跃引擎、PID、状态、空闲时长、在途请求数 |
| POST | `/api/hub/switch?model=voxcpm` | 切换/预拉起（会自动卸载当前引擎） |
| POST | `/api/hub/unload` | 卸载全部引擎释放显存（`?force=true` 忽略在途请求） |
| POST | `/api/hub/engines/{name}/start` | 拉起 |
| POST | `/api/hub/engines/{name}/stop` | 停止 |
| POST | `/api/hub/engines/{name}/restart` | 重启（改了 runtime/server 后用它） |
| GET | `/api/hub/engines/{name}/log?lines=200` | 看引擎子进程日志（模型加载进度/报错全在这） |
| POST | `/api/hub/shutdown` | **关闭管家**：先卸载全部引擎释放显存，再退出进程（`关闭TTS管家桌面端.bat` 用它） |
| GET | `/api/hub/status` | 每个活跃引擎额外返回 `idle_expires_in`（距空闲回收剩余秒数） |

### 4.4 合成透传

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/tts?model=<名>` | 统一合成入口 |
| POST | `/api/tts/{endpoint}?model=<名>` | 指定端点 |
| ANY | `/api/hub/passthrough/{path}?model=<名>` | 任意路径透传（任务查询/音频下载/SSE 进度） |

---

## 5. 调用示例

### 5.1 最简：JSON 透传（VoxCPM 克隆）

```bash
curl -X POST "http://localhost:5199/api/tts?model=voxcpm" ^
  -H "Content-Type: application/json" ^
  -d "{\"text\":\"今天天气真不错\",\"ref_audio_path\":\"VoxCPM/examples/example.wav\"}"
```

管家行为：发现当前没有/不是 `voxcpm` → 卸载旧引擎 → 拉起 `VoxCPM/voxcpm-api_server.py --port 8854`
→ 轮询 `/health` 直到就绪 → 把请求体（合并 `defaults` 后）POST 到 `http://127.0.0.1:8854/api/v1/voice/clone`
→ 响应原样回吐。

响应头会带：`X-TTS-Engine: voxcpm`、`X-TTS-Engine-Port: 8854`。

### 5.2 带参考音频文件上传（multipart 透传）

```bash
curl -X POST "http://localhost:5199/api/tts?model=omnivoice" ^
  -F "text=你好世界" ^
  -F "ref_audio=@D:/ref.wav" ^
  -F "num_steps=32"
```

`ref_audio` 文件会被原样转发给引擎，不会被管家落地。

### 5.3 声音设计

```bash
curl -X POST "http://localhost:5199/api/tts/design?model=voxcpm" ^
  -H "Content-Type: application/json" ^
  -d "{\"text\":\"你好\",\"instruct\":\"female, low pitch, gentle\"}"
```

### 5.4 说书版：直接拿 wav 流

```bash
curl -X POST "http://localhost:5199/api/tts?model=story" ^
  -H "Content-Type: application/json" ^
  -d "{\"text\":\"第一章 ...\",\"voice_name\":\"narrator\"}" -o out.wav
```

### 5.5 任务型引擎（VoxCPM / OmniVoice / dots / Confucius4 / IndexTTS-2）

第一步返回 `{"task_id": "..."}`，之后用 passthrough 查询与下载：

```bash
:: 查任务状态
curl "http://localhost:5199/api/hub/passthrough/api/v1/tasks/<task_id>?model=voxcpm"

:: 下音频
curl "http://localhost:5199/api/hub/passthrough/api/v1/voice/download/<task_id>?model=voxcpm" -o out.wav

:: SSE 进度流
curl "http://localhost:5199/api/hub/passthrough/api/v1/tasks/<task_id>/progress?model=voxcpm"
```

### 5.6 在线改默认参数

```bash
curl -X PUT "http://localhost:5199/api/hub/engines/voxcpm/config" ^
  -H "Content-Type: application/json" ^
  -d "{\"defaults\":{\"cfg_value\":3.0,\"inference_timesteps\":15,\"language\":\"zh\"}}"
```

之后所有 `model=voxcpm` 的请求，若请求体里没显式给这些字段，就会自动带上新默认值。

```bash
curl -X POST "http://localhost:5199/api/hub/engines/voxcpm/config/reset"
```

### 5.7 预拉起（避免首次请求等待）

```bash
curl -X POST "http://localhost:5199/api/hub/switch?model=index25"
curl "http://localhost:5199/api/hub/engines/index25/log?lines=100"   # 看加载进度
```

---

## 6. 配置文件 `config/tts_hub.yaml`

一个文件注册全部引擎，结构如下（完整版见文件内注释）：

```yaml
hub:                      # 管家自身
  host: "0.0.0.0"
  port: 5199               # 刻意避开 8000/8080/8888/9000 等常用端口
  max_active: 1           # 同时常驻引擎数上限（1~4）：1 = 切换即卸载；N = 允许 N 个并存
  idle_ttl_seconds: 300   # 空闲自动卸载，默认 300 = 5 分钟；0 = 常驻不回收
  idle_check_interval: 15 # 空闲巡检间隔（秒）
  unload_on_switch: false # true = 每次切换都强制独占（无视 max_active>1）
  stop_engines_on_exit: true
  forward_timeout: 0      # 转发超时，0=不限
  log_dir: "logs/tts_hub"
  allow_shutdown_api: true # 允许 POST /api/hub/shutdown（桌面端「关闭」脚本依赖）

runtime:                  # 公共运行时（引擎可覆盖）
  python: "{root}/py312env/python.exe"
  cwd: "{root}"
  path_prepend: [...]
  env_unset: [CONDA_PREFIX, VIRTUAL_ENV, PYTHONPATH, ...]
  env: {HF_ENDPOINT: "https://hf-mirror.com", HF_HOME: "{root}/hf_download", ...}

engines:
  voxcpm:
    display_name: "VoxCPM2"
    aliases: ["vox", "voxcpm2"]
    enabled: true
    runtime:
      script: "VoxCPM/voxcpm-api_server.py"
      args: ["--port", "{port}"]
    server: {host: "127.0.0.1", port: 8854}
    health: {mode: http, path: "/health", timeout: 900, interval: 3}
    endpoints: {clone: "/api/v1/voice/clone", design: "/api/v1/voice/design", ...}
    default_endpoint: clone
    file_fields: ["ref_audio"]
    defaults: {cfg_value: 2.0, inference_timesteps: 10}
    params:                # 参数表，供 /params 查询
      text: {type: string, required: true, desc: "要合成的文本"}
      cfg_value: {type: number, default: 2.0, range: [1, 5], desc: "引导强度"}
```

占位符：`{root}` = 仓库根、`{port}` = 该引擎 `server.port`（仅在 `runtime` 段展开，`endpoints` 里的 `{task_id}` 保留原样）。

### 新增一个引擎

在 `config/tts_hub.yaml` 的 `engines:` 下加一段即可，无需改代码。
改完 `config/tts_hub.yaml` 不需要重启管家——注册表按文件 mtime 自动热重载。

---

## 7. 运行机制细节

1. **切换与配额**：`ensure(name)` 持全局 `asyncio.Lock`，按 `max_active` 决定要腾几个位：
   - `max_active = 1`（默认）：切换即卸载当前引擎 —— `taskkill /F /T`（Windows）/ `killpg`（Linux）
     结束整棵进程树，真正释放显存，再拉起目标
   - `max_active = N`：允许 N 个引擎**并存**；只有"再加一个会超上限"时，才按**最久未使用（LRU）**
     淘汰到刚好放得下
   - `unload_on_switch = true`：每次切换都强制独占，无视 `max_active > 1`

   之后拉起目标脚本 → 轮询 `health.path` 直到就绪（首次含模型下载，超时默认 900s）→ 转发请求。
   **以上三处配置都可在总控台概览区或配置编辑器里在线修改，下一次请求立即按新配额执行，无需重启。**
2. **在途保护**：切换/卸载时若引擎仍有请求在途，会等待其结束（最多 300s），`force=true` 可跳过。
3. **端口占用**：若目标端口已被其它进程占用，默认报 503 并提示；可在 overrides 里设
   `hub.adopt_existing: true` 让管家直接接管已有服务（此时管家不会去杀它）。
4. **日志**：引擎子进程 stdout/stderr 落 `logs/tts_hub/{engine}.log`，可用 `/log` 接口实时查看。
5. **空闲回收（默认开启，5 分钟）**：`hub.idle_ttl_seconds`（默认 `300`）> 0 时，后台每
   `idle_check_interval`（默认 15s）巡检一次，把"已就绪 + 无在途请求 + 空闲超过 TTL"的引擎
   结束进程、释放显存，并在管家日志里打印 `[idle-reaper] ... 已自动卸载引擎 xxx`。
   - **正在拉起的引擎不会被回收**：模型加载动辄数分钟，`_wait_ready` 期间会持续刷新活跃时间，
     且 `starting` 状态的实例被巡检跳过，避免"刚加载完就被杀"。
   - 想让它常驻：把 `idle_ttl_seconds` 设为 `0`（可 `PUT /api/hub/config` 在线改，也可在
     配置编辑器的「管家级配置」里改）。
   - 面板上能看到倒计时：顶栏与总控台显示 `闲置回收 4m12s`。
6. **退出清理**：管家关闭时默认结束所有由它拉起的引擎（`hub.stop_engines_on_exit`）。
   ⚠ 不要用任务管理器强杀 `pythonw.exe` —— 那会留下 TTS 引擎子进程继续占用显存。
   请用 `关闭TTS管家桌面端.bat` 或 `POST /api/hub/shutdown`。
7. **透传保真**：请求体按 Content-Type 分别处理（JSON / multipart / urlencoded / 原始字节），
   响应统一用 `StreamingResponse(aiter_raw())` 回吐，保留状态码、`Content-Type`、
   `Content-Disposition`、`Content-Encoding`。

---

## 8. 排错

| 现象 | 处理 |
| --- | --- |
| 503 引擎就绪超时 | `GET /api/hub/engines/{name}/log?lines=200` 看是在下载模型还是报错；调大 `health.timeout` |
| 503 端口被占用 | 关掉占用端口的程序，或设 `hub.adopt_existing: true` |
| 引擎返回 422 参数不对 | `GET /api/hub/engines/{name}/params` 核对字段名（如 IndexTTS-2.5 是 `input_text` 不是 `text`） |
| 拿到 `{"task_id": ...}` 而不是音频 | 该引擎是异步任务型，用 `/api/hub/passthrough/...` 轮询并下载 |
| 409 无法卸载 | 有请求在途，加 `?force=true` |
| 改了 port/script 没生效 | 用 `POST /api/hub/engines/{name}/restart` |

---

## 9. 可视化控制台（frontend/）

技术栈：**Vue 3 + Vite + Element Plus + Pinia + vue-router**。

### 四个模块

| 模块 | 路由 | 能力 |
| --- | --- | --- |
| 总控台 | `/ui/console` | 引擎卡片墙（状态灯 / 端口 / PID / 运行时长）、切换并拉起、重启、停止、**日志抽屉实时看模型加载进度**、当前引擎端点与 defaults 速览；概览区可直接调 **常驻上限（1~4）** 与 **是否常驻 / 空闲回收时长**，改完即时下发生效 |
| 合成工作台 | `/ui/studio` | 按 `/params` **自动渲染参数表单**（滑块 / 开关 / 枚举 / 多行文本 / 参考音频上传）、endpoint 切换（clone/design/…）、请求体格式（自动/Form/JSON）、结果区音频播放与下载 |
| 配置编辑器 | `/ui/config` | 在线编辑引擎 `defaults`（表单化，可从参数表添加键）、查看运行期覆盖层 JSON、管家级配置（max_active / idle_ttl / 超时 / 卸载开关） |
| 任务中心 | `/ui/tasks` | 异步任务表格，2.5s 自动轮询进度；含 **RTF 列**（`<1` 绿 / `<2` 黄 / `≥2` 红，悬停看音频时长与推理耗时）；**「播放」按钮懒加载音频**（点击才拉取字节并缓存，不预取），带波形监听台；也可一键下载。合成返回 `task_id` 会自动入列 |

### 开发与构建

**日常使用**：双击 `启动_TTS管家.bat` 即可，用的是已构建的 `tts_hub/static/`。

**改前端代码时**：双击 **`启动_TTS管家_开发.bat`**，它会一口气拉起两个窗口并打开浏览器：

- 后端管家 `python tts_hub_server.py --port 5199`
- 前端 Vite dev server `:5180`（`/api` 已代理到 5199）
- 自动打开 <http://127.0.0.1:5180/console>，改动 `frontend/src` 下任意文件**热更新，无需 build**
- 若 `:5199` 已有服务在监听则跳过启动后端；首次运行会自动 `npm install`

手动方式等价：

```bash
cd frontend
npm install          # 仅开发期需要，Node 18+
npm run dev          # http://127.0.0.1:5180，/api 已代理到管家 5199
npm run build        # → 输出到 tts_hub/static/，由 FastAPI 挂载
```

> `启动_TTS管家_开发.bat` 需要 Node；只使用面板（不改代码）请用 `启动_TTS管家.bat`，运行时零依赖。

- 产物是纯静态文件，提交进仓库后**用户机器上无需 Node**。
- 管家侧通过 `/ui` 与 `/ui/{path:path}` 两个路由托管，未匹配的路径回退 `index.html`（SPA history 路由），
  `index.html` 不缓存、带 hash 的 assets 长缓存。
- 未构建时访问 `/ui` 会显示构建指引页。

### 目录

```
frontend/
├── index.html
├── vite.config.js          # base: 生产 /ui/ · 开发 /；proxy /api → 5199
├── src/
│   ├── main.js             # Vue + Pinia + Router + Element Plus(暗色)
│   ├── App.vue             # 顶栏 / 侧栏 / 忙碌遮罩
│   ├── styles/main.css     # 设计系统：琥珀工业控制台配色、网格底纹、EP 变量覆写
│   ├── api/index.js        # 管家 REST 客户端 + synth() 透传
│   ├── stores/hub.js       # 引擎状态、轮询、任务持久化
│   ├── components/         # TopBar / SideNav / EngineCard / LogDrawer / DynamicField / WaveMark / StateDot
│   ├── views/              # Console / Studio / Config / Tasks
│   └── router/index.js
└── package.json
```

> 新增引擎时前端**无需改代码**：参数表单由 `/api/hub/engines/{name}/params` 的 schema 驱动，
> 只需在 `config/tts_hub.yaml` 里加一段即可。

---

## 10. 文件结构

```
tts_hub_server.py         # 启动入口
tts_hub/
├── static/               # 控制台构建产物（frontend/ 输出，随仓库发布）
├── registry.py           # 配置注册表：加载 / 占位符展开 / 运行期覆盖持久化
├── manager.py            # 引擎进程生命周期：拉起 / 卸载 / 健康检查 / 切换 / 日志
├── proxy.py              # 透传层：表单 / JSON / 流式 / SSE
└── server.py             # FastAPI 服务
config/tts_hub.yaml       # ★ 引擎注册表（唯一注册源）
config/tts_hub.overrides.yaml  # 运行期修改（自动生成，已 gitignore）
logs/tts_hub/*.log        # 各引擎子进程日志（含 hub.log / hub.err.log）
启动TTS管家桌面端.bat      # 日常：后台无窗口常驻 + 应用模式打开面板
关闭TTS管家桌面端.bat      # 配套：走 API 优雅关闭（先卸载引擎再退出）
启动_TTS管家.bat          # 前台运行，看实时日志
启动_TTS管家_开发.bat      # 开发模式（后端 + Vite 热更新，需 Node）
```

> 三个 `.bat` 均为 **GBK 编码 + CRLF 换行**。这是 Windows 批处理的硬性要求：
> cmd 按系统 ANSI(936) 代码页解析 `.bat` 并把参数编码后传给子进程，UTF-8 中文会错位截断命令行、
> 编坏 `powershell -Command` 的中文参数；而 LF-only 换行会让 `if (...)` 多行块碎裂成伪命令。
