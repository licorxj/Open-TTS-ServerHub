// TTS 管家 API 客户端（同源，无需 CORS；dev 下由 vite proxy 转发到 9000）

async function json(path, options = {}) {
  const resp = await fetch(path, options)
  const text = await resp.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = { raw: text }
  }
  if (!resp.ok) {
    const detail = data?.detail ?? data
    const msg =
      typeof detail === 'string'
        ? detail
        : detail?.message || detail?.msg || `HTTP ${resp.status}`
    const err = new Error(msg)
    err.status = resp.status
    err.detail = detail
    throw err
  }
  return data
}

const q = (params = {}) => {
  const usp = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') usp.append(k, v)
  })
  const s = usp.toString()
  return s ? `?${s}` : ''
}

export const api = {
  // ---------- 查询 ----------
  health: () => json('/api/hub/status'),
  models: () => json('/api/hub/models'),
  engines: (withParams = false) => json(`/api/hub/engines${q({ with_params: withParams })}`),
  engine: (name) => json(`/api/hub/engines/${encodeURIComponent(name)}`),
  params: (name) => json(`/api/hub/engines/${encodeURIComponent(name)}/params`),

  // ---------- 引擎统一启停（与后端 backend 同级，由 hub 代为拉起 / 回收）----------
  startEngine: (name) =>
    json(`/api/hub/engines/${encodeURIComponent(name)}/start`, { method: 'POST' }),
  stopEngine: (name, force = false) =>
    json(`/api/hub/engines/${encodeURIComponent(name)}/stop${q({ force })}`, { method: 'POST' }),
  restartEngine: (name) =>
    json(`/api/hub/engines/${encodeURIComponent(name)}/restart`, { method: 'POST' }),
  engineLog: (name, lines = 200) =>
    json(`/api/hub/engines/${encodeURIComponent(name)}/log${q({ lines })}`),
  // 以端口表为准重建状态：kill=true 会结束所有「非本 hub 拉起」的引擎进程，立即释放显存
  reconcile: (kill = false) =>
    json(`/api/hub/reconcile${q({ kill })}`, { method: 'POST' }),

  // ---------- 配置 ----------
  getConfig: (name) => json(`/api/hub/engines/${encodeURIComponent(name)}/config`),
  putConfig: (name, patch) =>
    json(`/api/hub/engines/${encodeURIComponent(name)}/config`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }),
  resetConfig: (name) =>
    json(`/api/hub/engines/${encodeURIComponent(name)}/config/reset`, { method: 'POST' }),
  getHubConfig: () => json('/api/hub/config'),
  putHubConfig: (patch) =>
    json('/api/hub/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }),

  // ---------- 生命周期 ----------
  status: () => json('/api/hub/status'),
  // ---------- Skill 安装信息（绝对路径由后端给出）----------
  skill: () => json('/api/hub/skill'),
  switchTo: (model) => json(`/api/hub/switch${q({ model })}`, { method: 'POST' }),
  unload: (force = false) => json(`/api/hub/unload${q({ force })}`, { method: 'POST' }),
  start: (name) => json(`/api/hub/engines/${encodeURIComponent(name)}/start`, { method: 'POST' }),
  stop: (name, force = false) =>
    json(`/api/hub/engines/${encodeURIComponent(name)}/stop${q({ force })}`, { method: 'POST' }),
  restart: (name) => json(`/api/hub/engines/${encodeURIComponent(name)}/restart`, { method: 'POST' }),
  log: (name, lines = 200) =>
    json(`/api/hub/engines/${encodeURIComponent(name)}/log${q({ lines })}`),

  // ---------- 项目音色库 ----------
  voices: () => json('/api/hub/voices'),
  voiceAudioUrl: (id) => `/api/hub/voices/${encodeURIComponent(id)}/audio`,
  saveVoice: async ({ file, name = '', gender = '', age = '', text = '', autoTranscribe = true }) => {
    const fd = new FormData()
    fd.append('file', file, file.name || 'voice.wav')
    fd.append('name', name)
    fd.append('gender', gender)
    fd.append('age', age)
    fd.append('text', text || '')
    fd.append('auto_transcribe', autoTranscribe ? '1' : '0')
    return json('/api/hub/voices', { method: 'POST', body: fd })
  },
  updateVoice: (id, patch) =>
    json(`/api/hub/voices/${encodeURIComponent(id)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }),
  // 用 ASR 转写音色音频并更新参考文字稿（overwrite=false 时已有文本则跳过）
  transcribeVoice: (id, overwrite = true) =>
    json(`/api/hub/voices/${encodeURIComponent(id)}/transcribe${q({ overwrite })}`, {
      method: 'POST',
    }),
  // 管家级 ASR 状态 / 卸载（释放显存）
  asrStatus: () => json('/api/hub/asr/status'),
  asrUnload: () => json('/api/hub/asr/unload', { method: 'POST' }),
  deleteVoice: (id, removeFile = true) =>
    json(`/api/hub/voices/${encodeURIComponent(id)}${q({ remove_file: removeFile })}`, {
      method: 'DELETE',
    }),
  // 默认音色（克隆模式未传参考音频时的兜底）
  getDefaultVoice: () => json('/api/hub/voices/default'),
  setDefaultVoice: (voiceId) =>
    json('/api/hub/voices/default', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ voice_id: voiceId || '' }),
    }),

  // ---------- 对外的音色接口（外部程序也走这一套）----------
  publicVoices: () => json('/api/voices'),
  publicVoiceAudioUrl: (id) => `/api/voices/${encodeURIComponent(id)}/audio`,

  // ---------- 透传 ----------
  passthrough: (model, path) =>
    json(`/api/hub/passthrough/${String(path).replace(/^\//, '')}${q({ model })}`),

  // ---------- 观测：请求日志与服务端任务台账 ----------
  requests: (params = {}) => json(`/api/hub/requests${q(params)}`),
  clearRequests: () => json('/api/hub/requests/clear', { method: 'POST' }),
  serverTasks: (params = {}) => json(`/api/hub/tasks${q(params)}`),
  clearServerTasks: () => json('/api/hub/tasks/clear', { method: 'POST' }),
  journalStats: () => json('/api/hub/journal/stats'),
}

/**
 * 合成请求。返回 { ok, kind, status, ms, engine, blob, url, json, text }
 * kind: 'audio' | 'json' | 'text'
 */
export async function synth({ model, endpoint, fields = {}, files = {}, bodyMode = 'auto', format = 'auto' }) {
  const url = `/api/tts${endpoint ? `/${String(endpoint).replace(/^\//, '')}` : ''}${q({ model })}`
  const t0 = performance.now()

  let body
  const headers = {}
  // 请求体格式：显式指定 > 引擎声明的 body_mode > 默认 multipart
  // （多数引擎合成接口是 Form(...)/File(...)，multipart 最通用；
  //   IndexTTS-2.5 / 说书版 的 body_mode 为 json，走 JSON）
  const useJson = format === 'json' ? true : format === 'form' ? false : bodyMode === 'json'

  if (useJson) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(fields)
  } else {
    // 绝大多数引擎的合成接口是 Form(...) / File(...)，用 multipart 最通用
    const fd = new FormData()
    Object.entries(fields).forEach(([k, v]) => {
      if (v === '' || v === null || v === undefined) return
      fd.append(k, typeof v === 'boolean' ? String(v) : v)
    })
    Object.entries(files).forEach(([k, f]) => {
      if (f) fd.append(k, f)
    })
    body = fd
  }

  const resp = await fetch(url, { method: 'POST', headers, body })
  const ms = Math.round(performance.now() - t0)
  const ct = resp.headers.get('content-type') || ''
  const engine = resp.headers.get('X-TTS-Engine') || model

  const base = { ok: resp.ok, status: resp.status, ms, engine }

  if (ct.includes('audio') || ct.includes('octet-stream')) {
    const blob = await resp.blob()
    return { ...base, kind: 'audio', blob, url: URL.createObjectURL(blob), size: blob.size }
  }

  const text = await resp.text()
  let parsed = null
  try {
    parsed = text ? JSON.parse(text) : null
  } catch {
    /* 非 JSON */
  }
  if (parsed) return { ...base, kind: 'json', json: parsed, text }
  return { ...base, kind: 'text', text }
}

/** 下载引擎产物（走管家 passthrough） */
export function downloadUrl(model, path) {
  return `/api/hub/passthrough/${String(path).replace(/^\//, '')}${q({ model })}`
}

/**
 * 剔除参数表里的「规范别名字段」（带 alias_of 的条目）。
 *
 * 管家为了让外部客户端按统一规范名调用，会在 /params 里额外暴露
 * text / input_text / ref_audio / prompt_text… 等别名，
 * 但它们与某个原生字段指向同一个引擎参数。
 * 面板表单只保留原生字段，否则同一个输入框会重复出现 3~4 次。
 */
export function stripAlias(params = {}) {
  const out = {}
  for (const [k, sc] of Object.entries(params || {})) {
    if (sc && typeof sc === 'object' && sc.alias_of) continue
    out[k] = sc
  }
  return out
}

/**
 * 调用「统一管理器（守护进程）」的控制 API。
 * 管理器监听独立端口（默认 5290），与 hub 不同源，故使用绝对地址。
 * 面板通过它实现「启停 / 重启后端」。
 */
export async function managerCall(port, path, options = {}) {
  const base = `http://${window.location.hostname}:${port}`
  const resp = await fetch(base + path, options)
  let data = null
  try {
    const text = await resp.text()
    data = text ? JSON.parse(text) : null
  } catch {
    /* 非 JSON */
  }
  if (!resp.ok) {
    const detail = data?.detail ?? data
    const msg =
      typeof detail === 'string'
        ? detail
        : detail?.message || detail?.msg || `HTTP ${resp.status}`
    const err = new Error(msg)
    err.status = resp.status
    throw err
  }
  return data
}
