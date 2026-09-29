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
  switchTo: (model) => json(`/api/hub/switch${q({ model })}`, { method: 'POST' }),
  unload: (force = false) => json(`/api/hub/unload${q({ force })}`, { method: 'POST' }),
  start: (name) => json(`/api/hub/engines/${encodeURIComponent(name)}/start`, { method: 'POST' }),
  stop: (name, force = false) =>
    json(`/api/hub/engines/${encodeURIComponent(name)}/stop${q({ force })}`, { method: 'POST' }),
  restart: (name) => json(`/api/hub/engines/${encodeURIComponent(name)}/restart`, { method: 'POST' }),
  log: (name, lines = 200) =>
    json(`/api/hub/engines/${encodeURIComponent(name)}/log${q({ lines })}`),

  // ---------- 透传 ----------
  passthrough: (model, path) =>
    json(`/api/hub/passthrough/${String(path).replace(/^\//, '')}${q({ model })}`),
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
