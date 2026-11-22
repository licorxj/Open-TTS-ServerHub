// 任务状态工具：任务型引擎（返回 task_id）的状态归一化
// 「测试工作台」与「任务中心」共用同一套判定，避免两处逻辑漂移。

export const DONE = ['completed', 'success', 'succeeded', 'finished', 'done']
export const FAIL = ['failed', 'error', 'cancelled', 'canceled']

/** 引擎原始状态 -> pending / running / done / failed */
export function normalize(state) {
  if (!state) return 'running'
  const s = String(state).toLowerCase()
  if (DONE.includes(s)) return 'done'
  if (FAIL.includes(s)) return 'failed'
  if (s === 'pending' || s === 'queued') return 'pending'
  return 'running'
}

/** 引擎原始进度（0~1 或 0~100）-> 0~100 整数 */
export function normProgress(p) {
  if (p === undefined || p === null) return 0
  const n = Number(p)
  if (Number.isNaN(n)) return 0
  return n <= 1 ? Math.round(n * 100) : Math.round(n)
}

/**
 * 引擎查找：key / display_name / alias 都能命中
 * @param {Array} engines hub.engines
 * @param {string} name 引擎名
 */
export function engineOf(engines, name) {
  if (!name) return null
  const list = engines || []
  const hit = list.find((e) => e.name === name)
  if (hit) return hit
  const t = String(name).toLowerCase()
  return (
    list.find(
      (e) =>
        e.name.toLowerCase() === t ||
        String(e.display_name || '').toLowerCase() === t ||
        (e.aliases || []).some((a) => String(a).toLowerCase() === t),
    ) || null
  )
}

/** base64（可带 data: 前缀）-> Blob */
export function base64ToBlob(b64, mime = 'audio/wav') {
  const raw = String(b64 || '')
  const clean = raw.includes(',') ? raw.slice(raw.indexOf(',') + 1) : raw
  const bin = atob(clean)
  const arr = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i)
  return new Blob([arr], { type: mime })
}

/* ---------------- 缺参提示与引擎错误文案 ---------------- */

/** 常见参数名 -> 中文说明（让「缺少必填参数」看得懂） */
export const FIELD_LABELS = {
  text: '要合成的文本',
  input_text: '要合成的文本',
  ref_text: '参考音频的精确文字稿',
  prompt_text: '参考音频的精确文字稿',
  reference_text: '参考音频的精确文字稿',
  ref_audio: '参考音频文件',
  ref_audio_path: '参考音频本地路径',
  speaker_audio: '参考音频文件',
  spk_audio: '参考音频文件',
  audio: '参考音频文件',
  reference_audio_file: '参考音频文件',
  instruction: '音色 / 语气指令',
  prompt: '提示词',
}

/** 把参数字段名翻译成中文说明 */
export function fieldLabel(name) {
  const k = String(name || '')
  if (FIELD_LABELS[k]) return FIELD_LABELS[k]
  return k
}

/**
 * 缺参提示：把字段名列表转成中文可读文案
 * @param {string[]} list 字段名列表（可能含 "a 或 b" 这种二选一组合）
 */
export function missingText(list = []) {
  return (list || [])
    .map((x) => String(x).split(' 或 ').map(fieldLabel).join(' 或 '))
    .join('、')
}

/**
 * 从引擎响应里抽出可读错误文案
 * @param {object} r synth() 的返回
 */
export function engineErrorText(r) {
  const status = r?.status
  const d = r?.json?.detail ?? r?.json?.message ?? null
  if (typeof d === 'string' && d.trim()) return `引擎返回 HTTP ${status}：${d}`
  if (d && typeof d === 'object') {
    const parts = []
    if (d.message) parts.push(String(d.message))
    if (Array.isArray(d.missing) && d.missing.length) parts.push(`缺少 ${missingText(d.missing)}`)
    if (d.hint) parts.push(String(d.hint))
    if (parts.length) return `引擎返回 HTTP ${status}：${parts.join('；')}`
  }
  if (r?.text) return `引擎返回 HTTP ${status}：${String(r.text).slice(0, 200)}`
  return `引擎返回 HTTP ${status}`
}
