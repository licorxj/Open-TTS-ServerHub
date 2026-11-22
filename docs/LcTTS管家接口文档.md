# LcTTS 管家 · 接口文档

> **面向要给管家写调用代码的开发者**：完整的 HTTP API 参考（端点、参数、真实响应、示例）。
>
> - 想了解「怎么启动、怎么配置、概念是什么」→ [TTS管家使用文档](TTS管家使用文档.md)
> - 想绕过管家直连各引擎 → [直连调用TTS文档](直连调用TTS文档.md)
> - 交互式的接口浏览：`http://127.0.0.1:5190/docs`（OpenAPI / Swagger）

---

## 1. 基础信息

| 项 | 值 |
| --- | --- |
| Base URL | `http://127.0.0.1:5190`（默认端口 `5190`，可用 `--port` 改） |
| 协议 | HTTP/1.1 |
| 认证 | 无（仅监听本机 / 内网，请勿直接暴露到公网） |
| 请求编码 | UTF-8 |
| 响应编码 | UTF-8，JSON 为 `application/json` |
| OpenAPI | `GET /docs`、`GET /openapi.json` |

**通用响应头**（合成类接口）：

```
X-TTS-Engine: voxcpm            # 实际服务的引擎 key
X-TTS-Engine-Display: VoxCPM2   # 显示名
X-TTS-Engine-Port: 8854         # 引擎进程端口
```

> 可由 `hub.expose_engine_header`（默认 `true`）关闭。

---

## 2. 30 秒跑通一次合成

```bash
# 1) 看有哪些模型可用
curl http://127.0.0.1:5190/api/hub/models

# 2) 看这个引擎支持什么参数
curl http://127.0.0.1:5190/api/hub/engines/voxcpm/params

# 3) 合成（model 决定拉起哪个引擎，其余参数原样透传）
curl -X POST "http://127.0.0.1:5190/api/tts?model=voxcpm" \
  -F "text=今天天气真不错" \
  -F "ref_audio_path=VoxCPM/examples/example.wav"
```

首次请求该引擎时，管家会自动拉起它（含模型下载，可能数分钟）；之后请求就是毫秒级。

---

## 3. 接口总览

| 分类 | 方法 | 路径 | 说明 |
| --- | --- | --- | --- |
| 概览 | GET | `/` | 服务信息与端点索引 |
| 概览 | GET | `/health` | 健康检查 + 运行状态 |
| 能力发现 | GET | `/api/hub/models` | 全部可用 model 名（含别名） |
| 能力发现 | GET | `/api/hub/engines` | 引擎列表 |
| 能力发现 | GET | `/api/hub/engines/{name}` | 引擎详情 |
| 能力发现 | GET | `/api/hub/engines/{name}/params` | **该引擎支持的参数表** |
| 配置 | GET | `/api/hub/engines/{name}/config` | 读引擎配置 |
| 配置 | PUT | `/api/hub/engines/{name}/config` | 改引擎配置（持久化） |
| 配置 | POST | `/api/hub/engines/{name}/config/reset` | 恢复默认 |
| 配置 | GET | `/api/hub/config` | 读管家级配置 |
| 配置 | PUT | `/api/hub/config` | 改管家级配置（持久化） |
| 生命周期 | GET | `/api/hub/status` | 运行状态（含孤儿引擎） |
| 生命周期 | POST | `/api/hub/switch` | 切换 / 预拉起模型 |
| 生命周期 | POST | `/api/hub/unload` | 卸载全部引擎 |
| 生命周期 | POST | `/api/hub/reconcile?kill=false` | 状态对账 / 清理孤儿引擎 |
| 生命周期 | POST | `/api/hub/engines/{name}/start` | 拉起 |
| 生命周期 | POST | `/api/hub/engines/{name}/stop` | 停止 |
| 生命周期 | POST | `/api/hub/engines/{name}/restart` | 重启 |
| 生命周期 | GET | `/api/hub/engines/{name}/log` | 查看引擎子进程日志 |
| 生命周期 | POST | `/api/hub/shutdown` | 关闭管家 |
| 合成 | POST | `/api/tts` | **统一合成入口** |
| 合成 | POST | `/api/tts/{endpoint}` | 合成 + 指定端点 |
| 音色 | GET | `/api/voices` | **音色列表（含唯一音色 ID）** |
| 音色 | GET | `/api/voices/{id}/audio` | 取音色音频 |
| 透传 | ANY | `/api/hub/passthrough/{path}` | 任意路径透传 |
| 观测 | GET | `/api/hub/requests` | **请求记录**（含外部程序调用） |
| 观测 | POST | `/api/hub/requests/clear` | 清空请求记录 |
| 观测 | GET | `/api/hub/tasks` | **服务端任务台账**（外部请求创建的任务也在内） |
| 观测 | POST | `/api/hub/tasks/clear` | 清空任务台账 |
| 观测 | GET | `/api/hub/journal/stats` | 请求日志统计 |
| Skill | GET | `/api/hub/skill` | **Skill 安装信息（绝对路径）** |
| 面板 | GET | `/ui` | 可视化控制台 |

---

## 4. 能力发现

### 4.1 `GET /api/hub/models`

列出所有可用的 model 名（key / display_name / alias 均可作为 `model` 参数，大小写不敏感）。

```json
{
  "count": 9,
  "models": [
    {
      "engine": "voxcpm",
      "model": "voxcpm",
      "display_name": "VoxCPM2",
      "aliases": ["vox", "voxcpm", "voxcpm2", "VoxCPM"],
      "names": ["voxcpm", "VoxCPM2", "vox", "voxcpm2", "VoxCPM"],
      "enabled": true,
      "port": 8854,
      "description": "openbmb/VoxCPM2：声音克隆 / 终极克隆 / 声音设计，中文效果优秀"
    }
  ]
}
```

### 4.2 `GET /api/hub/engines`

参数：`with_params`（bool，默认 false）—— `true` 时每项附带完整 `params`。

```json
{
  "count": 9,
  "engines": [
    {
      "name": "voxcpm",
      "display_name": "VoxCPM2",
      "aliases": ["vox", "voxcpm", "voxcpm2", "VoxCPM"],
      "description": "openbmb/VoxCPM2：声音克隆 / 终极克隆 / 声音设计，中文效果优秀",
      "enabled": true,
      "state": "stopped",
      "pid": null,
      "port": 8854,
      "host": "127.0.0.1",
      "health": { "mode": "http", "path": "/health", "timeout": 900, "interval": 3 },
      "endpoints": {
        "clone": "/api/v1/voice/clone",
        "design": "/api/v1/voice/design",
        "ultimate_clone": "/api/v1/voice/ultimate_clone",
        "task": "/api/v1/tasks/{task_id}",
        "download": "/api/v1/voice/download/{task_id}",
        "progress": "/api/v1/tasks/{task_id}/progress",
        "batch": "/api/v1/voice/batch"
      },
      "default_endpoint": "clone",
      "file_fields": ["ref_audio"],
      "body_mode": "auto",
      "defaults": {
        "cfg_value": 2.0, "inference_timesteps": 10,
        "denoise": true, "normalize": false, "language": "zh"
      }
    }
  ]
}
```

`state`：`stopped` / `starting` / `ready` / `failed`。

### 4.3 `GET /api/hub/engines/{name}/params` ★

**前端/客户端动态渲染表单就用它。**

```json
{
  "engine": "voxcpm",
  "display_name": "VoxCPM2",
  "body_mode": "auto",
  "file_fields": ["ref_audio"],
  "endpoints": { "clone": "/api/v1/voice/clone", "...": "..." },
  "default_endpoint": "clone",
  "current_defaults": { "cfg_value": 2.0, "language": "zh" },
  "required": ["text"],
  "count": 12,
  "params": {
    "text":            { "type": "string",  "required": true, "desc": "要合成的文本" },
    "instruct":        { "type": "string",  "desc": "风格/情绪指令" },
    "ref_audio":       { "type": "file",    "desc": "参考音频文件（multipart 上传）" },
    "ref_audio_path":  { "type": "string",  "desc": "参考音频本地路径（推荐，免上传）" },
    "cfg_value":       { "type": "number",  "default": 2.0, "range": [1, 5], "desc": "引导强度" },
    "inference_timesteps": { "type": "integer", "default": 10, "range": [1, 50], "desc": "扩散步数" },
    "denoise":         { "type": "boolean", "default": true, "desc": "是否去噪" },
    "emo_control_method": { "type": "string", "default": "reference", "enum": ["reference","vector","instruct"] }
  }
}
```

字段 schema 的键：

| 键 | 说明 |
| --- | --- |
| `type` | `string` / `number` / `integer` / `boolean` / `file` |
| `required` | 是否必填 |
| `default` | 引擎侧默认值 |
| `range` | `[min, max]`（数值型） |
| `enum` | 可选值列表 |
| `desc` | 中文说明 |

### 4.4 `GET /api/hub/engines/{name}`

引擎详情（在 4.2 基础上多一个 `params` 和精简的 `runtime`）。

---

## 5. 配置读写

### 5.1 `GET /api/hub/engines/{name}/config`

返回当前生效值 + 已有覆盖 + 可改字段白名单。

```json
{
  "engine": "voxcpm",
  "current": { "enabled": true, "display_name": "VoxCPM2", "server": {"host":"127.0.0.1","port":8854},
               "health": { "mode": "http", "path": "/health", "timeout": 900, "interval": 3 },
               "endpoints": { "...": "..." }, "default_endpoint": "clone",
               "file_fields": ["ref_audio"], "body_mode": "auto",
               "defaults": { "cfg_value": 2.0, "language": "zh" },
               "runtime": { "script": "VoxCPM/voxcpm-api_server.py", "args": ["--port","8854"] } },
  "overrides": {},
  "mutable_keys": ["enabled","display_name","aliases","description","runtime","server",
                   "health","endpoints","default_endpoint","defaults","file_fields","body_mode"]
}
```

### 5.2 `PUT /api/hub/engines/{name}/config`

请求体是要**合并**的补丁（不是全量覆盖）。最常用的是改 `defaults`：

```bash
curl -X PUT http://127.0.0.1:5190/api/hub/engines/voxcpm/config \
  -H "Content-Type: application/json" \
  -d '{"defaults":{"cfg_value":3.0,"inference_timesteps":15,"language":"zh"}}'
```

```json
{ "ok": true, "engine": "voxcpm", "need_restart": false,
  "message": "配置已保存，下次请求即生效",
  "defaults": { "cfg_value": 3.0, "inference_timesteps": 15, "language": "zh" } }
```

- 改 `runtime` / `server` / `health` 时 `need_restart: true`，需 `POST .../restart` 才生效
- `params`（接口能力声明）**不可改**，传了会返回 400
- 改动写入 `config/tts_hub.overrides.yaml`，**不改动主注册表** `config/tts_hub.yaml`

### 5.3 `POST /api/hub/engines/{name}/config/reset`

清空该引擎的运行期覆盖，恢复主注册表配置。返回 `{ "ok": true, "engine": "...", "defaults": {...} }`。

### 5.4 `GET` / `PUT /api/hub/config`

管家级配置。可改字段（其余会被 400 拒绝）：

`max_active` / `idle_ttl_seconds` / `idle_check_interval` / `forward_timeout` /
`unload_on_switch` / `stop_engines_on_exit` / `expose_engine_header` /
`adopt_existing` / `allow_shutdown_api` /
`orphan_policy` / `orphan_idle_ttl_seconds` / `cleanup_orphans_on_exit`

```bash
curl -X PUT http://127.0.0.1:5190/api/hub/config \
  -H "Content-Type: application/json" \
  -d '{"max_active":2,"idle_ttl_seconds":600}'
```

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `max_active` | `1` | 同时常驻引擎数上限 `1~4`；超上限按 LRU 淘汰 |
| `idle_ttl_seconds` | `300` | 空闲多久自动卸载；`0` = 常驻不回收 |
| `idle_check_interval` | `15` | 空闲巡检间隔（秒） |
| `forward_timeout` | `0` | 转发超时（秒），`0` = 不限 |
| `unload_on_switch` | `false` | `true` = 每次切换都强制独占（无视 `max_active>1`） |
| `adopt_existing` | `false` | `true` = 端口已有服务时直接接管，不自己拉起 |
| `allow_shutdown_api` | `true` | 是否允许 `POST /api/hub/shutdown` |
| `stop_engines_on_exit` | `true` | 管家退出时结束引擎子进程 |
| `orphan_policy` | `adopt` | hub 重启后发现「端口在监听、但本进程没登记」的孤儿引擎时：`adopt` 接管并纳入配额/空闲回收；`kill` 立即结束释放显存；`ignore` 只提示不接管 |
| `orphan_idle_ttl_seconds` | `300` | 被接管的孤儿引擎空闲多久自动结束；`0` = 跟随 `idle_ttl_seconds` |
| `cleanup_orphans_on_exit` | `true` | 管家退出时是否连孤儿引擎一起结束，避免遗留占显存 |

---

## 6. 生命周期

### 6.1 `GET /api/hub/status`

```json
{
  "hub": { "pid": 10012, "root": "Y:\\LcTTSHub", "port": 5190 },
  "active": [
    {
      "name": "voxcpm", "display_name": "VoxCPM2", "state": "ready",
      "pid": 22884, "managed": true, "host": "127.0.0.1", "port": 8854,
      "base_url": "http://127.0.0.1:8854",
      "uptime_seconds": 130.2, "idle_seconds": 12.4, "inflight": 0,
      "last_error": null, "log_path": "Y:\\LcTTSHub\\logs\\voxcpm.log",
      "idle_ttl_seconds": 300, "idle_expires_in": 287.6
    }
  ],
  "active_count": 1, "max_active": 1,
  "idle_ttl_seconds": 300, "idle_check_interval": 15,
  "available": ["voxcpm","omnivoice","omnivoice_story","indextts2","indextts25",
                "dots","confucius4","audio8","auk"]
}
```

- `idle_expires_in`：距空闲自动卸载还剩多少秒；常驻（`ttl=0`）、拉起中、有在途请求时为 `null`
- `managed: false` 表示引擎是外部已运行的进程被接管；`orphan: true` 表示本 hub 进程没有亲自拉起它
  （上一代 hub 遗留 / 外部启动）。两者都**可以被 `force=true` 关停**：管家会按引擎端口反查 PID
  并结束整棵进程树。旧实现遇到「没有 Popen 句柄」直接跳过，导致孤儿进程永远关不掉、显存堆积。
- `/api/hub/status` 与 `/api/hub/engines` 还返回 `orphan` / `detached` / `pid` / `inflight` /
  `idle_seconds` / `idle_expires_in` / `last_error` / `log_path`；`status` 另含 `orphan_policy`、
  `orphan_count`、`orphans[]`（`name`/`pid`/`port`）与 `reconcile`（最近一次对账报告）。

### 6.2 `POST /api/hub/switch?model=<名>`

切换 / 预拉起。**会先卸载当前引擎再拉起目标**（受 `max_active` / `unload_on_switch` 影响）。

```bash
curl -X POST "http://127.0.0.1:5190/api/hub/switch?model=index25"
```

```json
{ "ok": true, "elapsed": 48.2, "engine": { "name": "indextts25", "state": "ready", "...": "..." } }
```

> 首次拉起含模型下载，接口会**一直等到就绪才返回**（默认最长 900s），客户端请给足超时。

### 6.3 `POST /api/hub/unload?force=false`

卸载全部引擎释放显存。有请求在途且未加 `force=true` 时返回 409。
返回 `{ "ok": true, "stopped": ["voxcpm"], "active_count": 0, ... }`。

### 6.4 单引擎操作

| 接口 | 参数 | 说明 |
| --- | --- | --- |
| `POST /api/hub/engines/{name}/start` | — | 拉起（等价于 switch 到它） |
| `POST /api/hub/engines/{name}/stop` | `force` | 停止；有在途请求需 `force=true` |
| `POST /api/hub/engines/{name}/restart` | `force`（默认 true） | 改了 `runtime`/`server`/`health` 后用它 |
| `GET /api/hub/engines/{name}/log?lines=200` | `lines` `1~5000` | 引擎子进程日志尾部 |

```bash
curl "http://127.0.0.1:5190/api/hub/engines/voxcpm/log?lines=200"
# {"engine":"voxcpm","lines":200,"content":"= = = ...\n>> VoxCPM API 服务启动中..."}
```

### 6.5 `POST /api/hub/shutdown`

**关闭管家**：先卸载全部引擎释放显存，再退出进程。桌面端「关闭」脚本用它。

```json
{ "ok": true, "stopped": ["voxcpm"], "message": "管家正在退出（引擎已卸载，显存已释放）" }
```

> ⚠ 不要用 `taskkill` 强杀 `pythonw.exe` —— 会留下引擎子进程继续占显存。
> 若已经留下孤儿进程，新版管家启动时会按端口表自动对账找回，也可用
> `POST /api/hub/reconcile?kill=true` 直接结束（见 6.6）。

### 6.6 `POST /api/hub/reconcile?kill=false`

以**系统端口表**为外部事实来源重建引擎状态，修复「hub 重启后活跃表清空、引擎进程却还在
后台占显存」的问题。管家启动、每轮空闲巡检、退出收尾时都会自动对账；此接口用于手动触发
或立即清理。`kill=true` 会结束所有引擎端口上「非本 hub 亲自拉起」的进程
（总控台右栏「状态对账」面板的「清理孤儿进程」按钮）。

```bash
curl -X POST "http://127.0.0.1:5190/api/hub/reconcile"
curl -X POST "http://127.0.0.1:5190/api/hub/reconcile?kill=true"
```

响应（省略 `status` 的其余字段）：

```json
{
  "ok": true,
  "report": {
    "policy": "adopt", "scanned": 10,
    "adopted": ["voxcpm"], "killed": [], "dropped": [], "ignored": [],
    "orphan_count": 1,
    "orphans": [{ "name": "voxcpm", "host": "127.0.0.1", "port": 8854, "pid": 51472 }]
  },
  "active_count": 1, "orphan_count": 1
}
```

- `adopted`：接管并纳入 `max_active` 配额与空闲回收
- `killed`：已结束的进程（`kill=true` 或 `orphan_policy=kill`）
- `dropped`：内存里有登记、但进程/端口已不存在的幽灵实例
- `orphans[]`：仍占着端口的外部进程，总控台引擎卡片显示为「外部进程」且可直接停止

---

## 7. 合成透传（核心）

### 7.1 `POST /api/tts?model=<名>`

统一合成入口。`model` 三种传法（优先级从高到低）：

1. Query：`POST /api/tts?model=voxcpm`
2. 请求头：`X-TTS-Model: voxcpm`
3. JSON 请求体字段：`{"model":"voxcpm", ...}`（会被摘掉，不透传给引擎）

`endpoint` 三种传法：

1. 路径：`POST /api/tts/design?model=voxcpm`
2. Query：`POST /api/tts?model=voxcpm&endpoint=design`
3. 请求头：`X-TTS-Endpoint: design`

不传则用引擎的 `default_endpoint`（一般是 `clone`）。也可以直接写真实路径：
`POST /api/tts/api/v1/voice/design?model=voxcpm`。

### 7.2 请求体格式

| 情况 | 格式 |
| --- | --- |
| 引擎 `body_mode = json`（IndexTTS-2.5 / 说书版） | `application/json` |
| 其它（多数引擎是 `Form(...)` / `File(...)`） | `multipart/form-data` |
| 有文件上传 | `multipart/form-data` |

**默认值注入**：请求里没显式给的字段，管家会自动补上引擎的 `defaults`；
请求里给了的以请求为准。

```bash
# multipart（多数引擎）
curl -X POST "http://127.0.0.1:5190/api/tts?model=voxcpm" \
  -F "text=你好世界" \
  -F "ref_audio=@D:/ref.wav" \
  -F "cfg_value=2.5"

# JSON（IndexTTS-2.5）
curl -X POST "http://127.0.0.1:5190/api/tts?model=index25" \
  -H "Content-Type: application/json" \
  -d '{"input_text":"你好","speaker_audio":"examples/ref.wav","lang":"zh"}'
```

> **注意（PowerShell / cmd 引号）**：`Content-Type: application/json` 的请求体必须是**合法 JSON**。
> 管家校验不通过时直接返回 `400`（`请求体解析失败: ... 不是合法 JSON`），
> 不会再把坏 body 原样转发给引擎（否则引擎只会回一句 `body.text Field required` 之类的误导性 422）。
> 在 PowerShell 里推荐把 JSON 写进文件再 `--data-binary "@body.json"`，避免命令行引号被吃掉。

### 7.3 响应

**原样透传**引擎的响应，保留：

- HTTP 状态码
- `Content-Type`（`application/json` / `audio/wav` / `text/event-stream`）
- `Content-Disposition`、`Content-Encoding`

形态可能是：

| 形态 | Content-Type | 出现场景 |
| --- | --- | --- |
| **二进制 wav** | `audio/wav` | 说书版 `/tts`、IndexTTS-2.5 的 `/api/tts/form` |
| **JSON（任务）** | `application/json` | 任务型引擎：`{ "task_id": "...", "status": "pending" }` |
| **JSON（含 base64）** | `application/json` | IndexTTS-2.5 的 `/api/tts`（不传 `output_path`） |
| **JSON（含 audio_url）** | `application/json` | Audio8 的 `/tts` |
| **SSE 流** | `text/event-stream` | 经 passthrough 请求的 `progress` 端点 |

### 7.4 `ANY /api/hub/passthrough/{path}?model=<名>`

把**任意路径、任意方法**透传给指定引擎，用于任务查询 / 音频下载 / SSE 进度 / 批量接口。

支持 `GET POST PUT PATCH DELETE HEAD OPTIONS`。

```bash
# 查任务
curl "http://127.0.0.1:5190/api/hub/passthrough/api/v1/tasks/<task_id>?model=voxcpm"

# 下音频
curl "http://127.0.0.1:5190/api/hub/passthrough/api/v1/voice/download/<task_id>?model=voxcpm" -o out.wav

# SSE 进度
curl "http://127.0.0.1:5190/api/hub/passthrough/api/v1/tasks/<task_id>/progress?model=voxcpm"
```

路径模板里的 `{task_id}` 需自行替换成真实 id（可从引擎详情的 `endpoints` 里取模板）。
若只有一个活跃引擎，`model` 可省略。

### 7.5 参考音色：`voice_id` 与默认音色

克隆类端点（端点名含 `clone`，如 `clone` / `clone_with_pe` / `controllable_clone`）在**没有传参考音频**时，
管家可以自动用**项目音色库**里的音色音频当参考音频。音色的选择顺序：

1. 请求里带的 `voice_id`（**外部程序可指定音色**）；
2. 管家配置的默认音色 `hub.default_voice`（面板「音色库」页设置）；
3. 都没有 → 按引擎原样报错（缺必填参数）。

`voice_id` 是**管家内部参数**，不会透传给引擎；它可放在请求体、也可以放 Query：

```bash
# 请求体
curl -X POST "http://127.0.0.1:5190/api/tts/clone?model=voxcpm" \
  -H "Content-Type: application/json" \
  -d '{"text":"你好世界","voice_id":"v1a2b3c4d"}'

# Query
curl -X POST "http://127.0.0.1:5190/api/tts/clone?model=voxcpm&voice_id=v1a2b3c4d" \
  -F "text=你好世界"
```

- `voice_id` 取 **音色唯一 ID**（`GET /api/voices` 可查），也兼容历史 ID 与音频文件名；
- 自己传了参考音频（`ref_audio` / `ref_audio_path` 等）时，**以你传的为准**，管家不再注入；
- 音色不存在 / 音频文件缺失 → `400`，`detail.message` 为「音色不存在」或「音色音频文件缺失」；
- 命中的音色若登记了「参考文本」，会一并作为 `ref_text` 传给引擎；
- 响应头 `X-Hub-Voice-Id` / `X-Hub-Voice-Source`（`request` = 来自 `voice_id`，`default` = 来自默认音色）。

### 7.6 `GET /api/voices` ★ 音色列表

```json
{
  "count": 19,
  "default_voice": "v5a1f65b",
  "voices": [
    {
      "id": "v5a1f65b",
      "name": "好听女声",
      "gender": "female",
      "age": "young",
      "has_text": true,
      "size": 1284412,
      "exists": true,
      "is_default": true,
      "audio_url": "/api/voices/v5a1f65b/audio"
    }
  ],
  "usage": {
    "clone": "POST /api/tts/clone?model=<引擎>  body: {\"text\": \"...\", \"voice_id\": \"<音色ID>\"}",
    "note": "voice_id 为空时，管家使用已配置的默认音色；仍未配置则按引擎原样报错"
  }
}
```

> **音色 ID 说明**：每个音色有一个全局唯一的 ID（`v` + 8 位十六进制），与音频文件名解耦，
> 改名/重名/中文名都不会再造成调用冲突。历史 ID（早期用文件名当 ID）保留在索引的 `legacy_ids` 里，仍可反查。

### 7.7 `GET /api/voices/{voice_id}/audio`

取音色音频原始文件（`voice/` 目录下的原件），可用于外部试听或自建参考音频。

```bash
curl "http://127.0.0.1:5190/api/voices/v5a1f65b/audio" -o ref.wav
```

---

## 8. 错误码

| 状态码 | 含义 | 典型 `detail` |
| --- | --- | --- |
| `400` | 缺少 `model` / 未知 `endpoint` / 请求体解析失败 | `{"message":"缺少 model 参数...","available":[...]}` |
| `403` | 接口被配置禁用（如 `allow_shutdown_api: false`） | `{"detail":"shutdown 接口已在配置中禁用..."}` |
| `404` | 未知引擎 | `{"detail":"未注册的引擎: xxx"}` |
| `409` | 有请求在途，无法卸载 | `{"detail":"引擎 xxx 正在处理 N 个请求..."}` |
| `422` | 引擎侧参数校验失败（原样透传） | 引擎自己的 FastAPI 校验信息 |
| `502` | 转发到引擎失败 | `{"message":"转发到引擎 xxx 失败: ...","engine":"xxx"}` |
| `503` | 引擎拉起失败 / 端口被占用 / 就绪超时 | `{"message":"端口 127.0.0.1:8854 已被其它进程占用..."}` |

错误响应统一形如（HTTPException）：

```json
{ "detail": { "message": "未知模型: xxx", "available": ["voxcpm", "..."] } }
```
或纯字符串：`{ "detail": "未注册的引擎: xxx" }`

---

## 9. 完整调用流程

### 9.1 同步型引擎（Audio8 / IndexTTS-2.5 / 说书版 / AuK）

```
POST /api/tts?model=<名>  →  直接拿到音频或 JSON
```

### 9.2 异步任务型（VoxCPM / OmniVoice / IndexTTS-2 / dots / Confucius4）

```
① POST /api/tts?model=voxcpm            → { "task_id": "..." }
② GET  /api/hub/passthrough/api/v1/tasks/{task_id}?model=voxcpm
③ GET  /api/hub/passthrough/api/v1/voice/download/{task_id}?model=voxcpm
```

Python 完整封装：

```python
import time, requests

BASE = "http://127.0.0.1:5190"
MODEL = "voxcpm"


def endpoints():
    """从管家拿端点模板，避免硬编码路径"""
    return requests.get(f"{BASE}/api/hub/engines/{MODEL}/params", timeout=10).json()["endpoints"]


def synth(text, ref_audio_path, **kw):
    data = {"text": text, "ref_audio_path": ref_audio_path, **kw}
    r = requests.post(f"{BASE}/api/tts", params={"model": MODEL}, data=data, timeout=900)
    r.raise_for_status()
    return r.json()


def wait(task_id, timeout=1800):
    ep = endpoints()
    url = f"{BASE}/api/hub/passthrough{ep['task'].replace('{task_id}', task_id)}"
    end = time.time() + timeout
    while time.time() < end:
        st = requests.get(url, params={"model": MODEL}, timeout=15).json()
        if st["status"] in ("completed", "failed"):
            return st
        time.sleep(1)
    raise TimeoutError(task_id)


def download(task_id):
    ep = endpoints()
    url = f"{BASE}/api/hub/passthrough{ep['download'].replace('{task_id}', task_id)}"
    return requests.get(url, params={"model": MODEL}, timeout=300).content


task = synth("你好，这是通过管家调用的一次合成。", "VoxCPM/examples/example.wav")
st = wait(task["task_id"])
print("RTF:", st.get("rtf"), "音频时长:", st.get("audio_duration"))
open("out.wav", "wb").write(download(task["task_id"]))
```

JavaScript：

```js
const BASE = 'http://127.0.0.1:5190'
const MODEL = 'voxcpm'

const ep = await (await fetch(`${BASE}/api/hub/engines/${MODEL}/params`)).json().then((r) => r.endpoints)
const pt = (tpl, id) =>
  `${BASE}/api/hub/passthrough${tpl.replace('{task_id}', id)}?model=${MODEL}`

const fd = new FormData()
fd.append('text', '你好，这是通过管家调用的一次合成。')
fd.append('ref_audio_path', 'VoxCPM/examples/example.wav')

const { task_id } = await (await fetch(`${BASE}/api/tts?model=${MODEL}`, { method: 'POST', body: fd })).json()

let st
for (;;) {
  st = await (await fetch(pt(ep.task, task_id))).json()
  if (st.status === 'completed' || st.status === 'failed') break
  await new Promise((r) => setTimeout(r, 1000))
}
console.log('RTF:', st.rtf)
```

---

## 10. 与直连调用的关系

管家**不改变引擎的任何接口语义**：

| | 直连引擎 | 经管家 |
| --- | --- | --- |
| URL | `http://127.0.0.1:8854/api/v1/voice/clone` | `http://127.0.0.1:5190/api/tts?model=voxcpm` |
| 参数名 | `text` / `ref_audio_path` / ... | **完全一致**（原样透传） |
| 响应 | 引擎原始响应 | **完全一致**（状态码与头原样回吐） |
| 端点选择 | 用路径 | 用 `endpoint` 参数 + 注册表映射 |

所以 **直连调通的代码，把 URL 换成 `POST /api/tts?model=xxx`、参数不动即可**。

---

## 11. 观测：请求记录与任务台账

管家会把**经过它的每一次调用**记在服务端（内存，重启即清空），因此
**外部程序直接调用管家创建的任务，面板上也能看到** —— 不再依赖浏览器本地存储。

### 11.1 `GET /api/hub/requests`

参数：`limit`(默认100, ≤1000) / `offset` / `kind` / `engine` / `only_synth`

`kind` 取值：`synth`（合成）/ `passthrough`（透传）/ `lifecycle`（启停卸载）/
`config`（配置读写）/ `query`（查询）/ `other`

```json
{
  "total": 12,
  "count": 12,
  "offset": 0,
  "items": [
    {
      "id": 12,
      "time": "2026-09-29T09:03:45",
      "clock": "09:03:45",
      "ts": 1790438625.07,
      "method": "POST",
      "path": "/api/tts",
      "query": "model=omnivoice",
      "kind": "synth",
      "client": "127.0.0.1",
      "model": "omnivoice",
      "engine": "omnivoice",
      "endpoint": "/api/v1/voice/design",
      "task_id": "a3609b96-492a-489a-b249-055e2844db70",
      "status": 200,
      "duration_ms": 812.4,
      "inflight": false,
      "error": null
    }
  ]
}
```

- `endpoint` 是**引擎侧的真实路径**（注册表映射后的结果），不是管家的 URL
- `duration_ms` 对 SSE/音频流是"到响应开始"的时间，不是完整传输时间
- 面板静态资源（`/ui/*`）、`/docs`、`/health` 探活**不记录**，避免刷屏
- `POST /api/hub/requests/clear` 本身会被留痕（审计需要），所以清空后仍剩 1 条

### 11.2 `GET /api/hub/tasks`

服务端任务台账：由 `/api/tts` 响应里嗅探出的 `task_id`（响应体是 JSON 且 ≤64KB 时）。

参数：`limit`(默认200) / `engine`

```json
{
  "total": 3,
  "count": 3,
  "items": [
    {
      "key": "omnivoice:a3609b96-492a-489a-b249-055e2844db70",
      "task_id": "a3609b96-492a-489a-b249-055e2844db70",
      "engine": "omnivoice",
      "endpoint": "/api/v1/voice/design",
      "created_at": 1790438625.07,
      "created_time": "2026-09-29T09:03:45",
      "client": "127.0.0.1",
      "request_id": 12
    }
  ]
}
```

拿到 `task_id` 后，用 `passthrough` 轮询状态与下载（见 §9.2）。

> 台账只记「任务被创建」，不含进度/RTF —— 那些仍由实际轮询引擎获得（面板会做这件事）。

### 11.3 `GET /api/hub/journal/stats`

```json
{
  "uptime_seconds": 877.5,
  "requests_kept": 12,
  "requests_inflight": 0,
  "tasks_kept": 3,
  "by_kind": { "synth": 5, "passthrough": 6, "query": 1 },
  "by_engine": { "omnivoice": 11 }
}
```

---

## 12. `GET /api/hub/skill`（Skill 安装信息）

返回本机 Skill 的**绝对路径**与安装状态，供前端「Skill 安装」页展示；用户可一键复制提示词给智能体完成安装。

```json
{
  "ok": true,
  "name": "tts-hub-api",
  "description": "调用本项目的 TTS 语音合成接口（IndexTTS-2/2.5、VoxCPM …）",
  "exists": true,
  "root": "Y:\\LcTTSHub",
  "skill_dir": "Y:\\LcTTSHub\\tts-agent-skill",
  "skill_md": "Y:\\LcTTSHub\\tts-agent-skill\\SKILL.md",
  "script": "Y:\\LcTTSHub\\tts-agent-skill\\scripts\\tts_agent.py",
  "references": ["interfaces.md", "mimo_online.md"],
  "installed_path": "Y:\\LcTTSHub\\.codebuddy\\skills\\tts-hub-api",
  "install_targets": [
    "C:\\Users\\Administrator\\.codebuddy\\skills\\tts-hub-api",
    "Y:\\LcTTSHub\\.codebuddy\\skills\\tts-hub-api"
  ],
  "python": "Y:\\LcTTSHub\\py312env\\python.exe",
  "hub_port": 5190
}
```

| 字段 | 说明 |
| --- | --- |
| `skill_dir` / `skill_md` / `script` | Skill 目录、SKILL.md、CLI 脚本的绝对路径 |
| `name` / `description` | 取自 SKILL.md frontmatter |
| `installed_path` | 已检测到的技能安装目录（用户级优先），未安装为 `null` |
| `install_targets` | 建议的安装位置（CodeBuddy 技能目录） |
| `hub_port` | 本机管家端口（提示词里给智能体用） |

---

## 13. 相关文档

- [TTS管家使用文档](TTS管家使用文档.md) —— 启动、配置、生命周期、面板、排错
- [直连调用TTS文档](直连调用TTS文档.md) —— 绕过管家直连各引擎的完整参数与示例
- [依赖说明](依赖说明.md) —— 环境与依赖
- 交互式接口：`http://127.0.0.1:5190/docs`
