<script setup>
import { ref, reactive, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useHubStore } from '../stores/hub'
import { api, synth, stripAlias, downloadUrl } from '../api'
import DynamicField from '../components/DynamicField.vue'
import StateDot from '../components/StateDot.vue'
import { normalize, normProgress, engineOf, base64ToBlob, missingText, engineErrorText } from '../utils/tasks'
import { ElMessage } from 'element-plus'

// keep-alive 的 include 靠组件 name 匹配；<script setup> 需显式声明
defineOptions({ name: 'StudioView' })

const hub = useHubStore()
const route = useRoute()
const router = useRouter()

const meta = ref(null)
const params = ref({})
const fields = reactive({})
const files = reactive({})
const audioUrls = reactive({}) // 参考音频本地预览地址：key -> objectURL
const endpoint = ref('')
const format = ref('auto')
const sending = ref(false)
const loadingMeta = ref(false)

/* ---------------- 参考音色（voice_id）----------------
   克隆类端点可指定用哪个音色当参考音频；不选则由管家使用配置的默认音色。
   与下方「项目音色库」下拉不同：那边是把音频下载下来上传给引擎，
   这边是直接把音色 ID 交给管家，由管家在服务端注入参考音频。 */
const voiceOptions = ref([])
const voiceId = ref('')

const isCloneEndpoint = computed(() => String(endpoint.value || '').includes('clone'))

async function loadVoiceOptions() {
  try {
    const r = await api.publicVoices()
    voiceOptions.value = r?.voices || []
  } catch {
    voiceOptions.value = []
  }
}

const engineName = computed({
  get: () => hub.current,
  set: (v) => hub.setCurrent(v),
})

const engine = computed(() => hub.engineByName[hub.current] || null)

/** 可直接调用的端点（过滤掉含 {task_id} 的任务类路径） */
const endpointKeys = computed(() =>
  Object.entries(meta.value?.endpoints || {})
    .filter(([, p]) => !String(p).includes('{'))
    .map(([k]) => k),
)

const orderedKeys = computed(() => {
  const keys = Object.keys(params.value)
  const req = keys.filter((k) => params.value[k]?.required)
  const opt = keys.filter((k) => !params.value[k]?.required)
  return [...req, ...opt]
})

const fileKeys = computed(() => orderedKeys.value.filter((k) => params.value[k]?.type === 'file'))

/** 该字段在当前端点下是否必填（引擎级 required + 端点级 endpoint_requires.required） */
function isRequired(key) {
  if (params.value[key]?.required) return true
  const req = meta.value?.endpoint_requires?.[endpoint.value]?.required || []
  return req.includes(key)
}
const normalKeys = computed(() => orderedKeys.value.filter((k) => params.value[k]?.type !== 'file'))

/* ---------------- 字段角色分类 ----------------
   把动态参数表拆成三块：
   - audio      参考音频（文件字段）
   - text       朗读文本（主文本字段）
   - instruction 朗读指令（instruction 字段，且非主文本）
   - param      其他生成参数
---------------------------------------------------------------- */
function fieldRole(k) {
  const sc = params.value[k] || {}
  if (sc.type === 'file') return 'audio'
  if (isMainTextField(k)) return 'text'
  if (k === 'instruction') return 'instruction'
  return 'param'
}

const inputAudioKeys = computed(() => fileKeys.value)
const inputTextKeys = computed(() => normalKeys.value.filter((k) => isMainTextField(k)))
const inputInstructionKeys = computed(() =>
  normalKeys.value.filter((k) => k === 'instruction' && !isMainTextField(k)),
)
const paramKeys = computed(() =>
  normalKeys.value.filter((k) => {
    if (isMainTextField(k)) return false
    if (k === 'instruction') return false
    return true
  }),
)

/* ---------------- 配音文本：默认值 + 常用短语 ----------------
   各引擎的"主文本字段"叫法不同（text / input_text / instruction…），
   统一识别后：进场预填一段默认文本，并提供常用短语下拉一键替换。            */
const DEFAULT_TEXT = '你好，欢迎使用 LcTTS 管家统一语音调度。'

const PHRASES = [
  '今天天气真不错，适合出门走走。',
  '你好，欢迎使用 LcTTS 管家统一语音调度。',
  '语音合成技术正在改变我们与设备交互的方式。',
  '请稍等，我正在为你生成这段音频。',
  '这是一段用于测试音色相似度的参考文本，注意听语气和停顿。',
  '2026 年 9 月 30 日，星期三，晴，气温 22 摄氏度。',
  '山重水复疑无路，柳暗花明又一村。',
  '他惊讶地问："这是真的吗？"随后又笑了起来。',
  '各位听众朋友大家好，欢迎收听今天的节目。',
  '前沿科技让生活更美好，也让创作变得更简单。',
]

const TEXT_FIELDS = ['text', 'input_text', 'gen_text', 'prompt', 'input']
// 这些是"参考音频对应文本"，不是要被念的内容，不能预填默认文案
const REF_TEXT_FIELDS = ['ref_text', 'prompt_text', 'reference_text', 'emo_text', 'emotion_text', 'asr_text']

/** 是不是"要被念出来的那段文字" */
function isMainTextField(k) {
  // instruction 有歧义：AuK 用它承载正文，Breeze 等用它做风格指令。
  // 判据是该引擎有没有独立的 text 类字段 —— 有则 instruction 只是指令。
  if (k === 'instruction') {
    return !orderedKeys.value.some((x) => x !== 'instruction' && TEXT_FIELDS.includes(x))
  }
  if (TEXT_FIELDS.includes(k)) return true
  if (REF_TEXT_FIELDS.includes(k)) return false
  return !!params.value[k]?.required && params.value[k]?.type === 'string' && /(^|_)text$/.test(k)
}

function applyPhrase(k, v) {
  if (v) fields[k] = v
}

function shorten(s, n = 30) {
  return s.length > n ? `${s.slice(0, n)}…` : s
}

/** 载入参数表后：主文本字段若没有配置默认值，就预填默认文案 */
function applySmartDefaults() {
  orderedKeys.value.forEach((k) => {
    const sc = params.value[k] || {}
    if (!isMainTextField(k)) return
    if (sc.default !== undefined && sc.default !== null && sc.default !== '') return
    if (fields[k] === undefined || fields[k] === '') fields[k] = DEFAULT_TEXT
  })
}

/* ---------------- 页面参数草稿缓存 ----------------
   keep-alive 负责「切标签不丢」：内存态连同已上传文件、参考音频预览、
   已合成音频列表一起保留。下面再用 localStorage 兜一层纯文本参数，
   避免刷新浏览器后表单被清空。File 对象与 objectURL 无法序列化，
   不入库（刷新后需重选参考音频，属预期行为）。                        */
const DRAFT_PREFIX = 'tts-hub:studio:draft:'
const draftKey = () => DRAFT_PREFIX + hub.current
let restoring = false

function saveDraft() {
  if (restoring || !hub.current) return
  try {
    localStorage.setItem(
      draftKey(),
      JSON.stringify({ fields: { ...fields }, endpoint: endpoint.value, format: format.value }),
    )
  } catch {
    /* 隐私模式或配额超限时静默降级为纯内存缓存 */
  }
}

function restoreDraft() {
  try {
    const raw = localStorage.getItem(draftKey())
    if (!raw) return
    const d = JSON.parse(raw)
    Object.entries(d.fields || {}).forEach(([k, v]) => {
      // 只恢复当前参数表仍存在的字段，避免引擎参数变更后的旧字段残留
      if (k in params.value) fields[k] = v
    })
    if (d.endpoint && endpointKeys.value.includes(d.endpoint)) endpoint.value = d.endpoint
    if (['auto', 'form', 'json'].includes(d.format)) format.value = d.format
  } catch {
    /* 缓存损坏则忽略 */
  }
}

// 实时落盘：用户每改一个输入框都同步一次草稿
watch([() => fields, endpoint, format], saveDraft, { deep: true })

async function loadMeta() {
  if (!hub.current) return
  loadingMeta.value = true
  restoring = true
  try {
    meta.value = await api.params(hub.current)
    // stripAlias：/params 会额外暴露规范别名字段，表单里只保留引擎原生字段
    params.value = stripAlias(meta.value.params)
    endpoint.value = meta.value.default_endpoint || 'clone'
    Object.keys(fields).forEach((k) => delete fields[k])
    Object.keys(files).forEach((k) => delete files[k])
    Object.keys(audioUrls).forEach((k) => {
      URL.revokeObjectURL(audioUrls[k])
      delete audioUrls[k]
    })
    currentVoiceId.value = ''
    voicePick.value = ''
    applySmartDefaults()
    restoreDraft()
  } catch (e) {
    ElMessage.error(e.message)
    meta.value = null
    params.value = {}
  } finally {
    loadingMeta.value = false
    // 等本轮被跳过的 watch 回调跑完，再解除保护并显式落盘一次
    await nextTick()
    restoring = false
    saveDraft()
  }
}

onMounted(() => {
  loadMeta()
  loadVoices()
  loadVoiceOptions()
})
watch(() => hub.current, loadMeta)
onBeforeUnmount(() => {
  Object.keys(audioUrls).forEach((k) => URL.revokeObjectURL(audioUrls[k]))
  audioOutputs.value.forEach((o) => o.url && URL.revokeObjectURL(o.url))
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
})

function fillDefaults() {
  const d = meta.value?.current_defaults || {}
  Object.entries(d).forEach(([k, v]) => {
    if (k in params.value) fields[k] = v
  })
  ElMessage.success('已填充该引擎的默认参数')
}

function clearAll() {
  Object.keys(fields).forEach((k) => delete fields[k])
  Object.keys(files).forEach((k) => delete files[k])
  Object.keys(audioUrls).forEach((k) => {
    URL.revokeObjectURL(audioUrls[k])
    delete audioUrls[k]
  })
  // 重置到默认文案；草稿由上面的 watch 自动更新为这份重置后的状态
  applySmartDefaults()
}

function onFile(k, uploadFile) {
  if (audioUrls[k]) URL.revokeObjectURL(audioUrls[k])
  files[k] = uploadFile.raw
  audioUrls[k] = URL.createObjectURL(uploadFile.raw)
  // 手动上传 = 外部音色：允许一键存为项目音色
  currentVoiceId.value = ''
}
function onFileRemove(k) {
  delete files[k]
  currentVoiceId.value = ''
  if (audioUrls[k]) {
    URL.revokeObjectURL(audioUrls[k])
    delete audioUrls[k]
  }
}

/* ---------------- 项目音色库（voice/ 目录 + voice.json） ----------------
   下拉里的音色来自项目根目录 voice/ 下的预置音色；选中后直接把该音色的
   音频片段载入为参考音频。载入的是「外部音色」（手动上传的音频）时，
   右侧「保存音色」按钮亮起，可把当前片段登记进项目音色库。            */
const voices = ref([])
const voiceLoading = ref(false)
const currentVoiceId = ref('') // 当前载入的预置音色 id（空 = 外部上传）
const voicePick = ref('')
const voiceDlg = reactive({
  visible: false,
  name: '',
  gender: 'female',
  age: 'young',
  text: '',
  autoTranscribe: true,
  saving: false,
})

const GENDER_OPTS = [
  { v: 'female', l: '女声' },
  { v: 'male', l: '男声' },
  { v: 'neutral', l: '中性' },
]
const AGE_OPTS = [
  { v: 'child', l: '童声' },
  { v: 'teen', l: '少年' },
  { v: 'young', l: '青年' },
  { v: 'middle', l: '中年' },
  { v: 'old', l: '老年' },
]

function genderLabel(g) {
  return GENDER_OPTS.find((x) => x.v === g)?.l || '未标注'
}
function ageLabel(a) {
  return AGE_OPTS.find((x) => x.v === a)?.l || '未标注'
}

/** 主参考音频字段（音色下拉作用于它） */
const audioKey = computed(() => inputAudioKeys.value[0] || '')
/** 只有「外部音色」（手动上传、未来自音色库）才能保存 */
const canSaveVoice = computed(() => !!files[audioKey.value] && !currentVoiceId.value)

async function loadVoices() {
  voiceLoading.value = true
  try {
    const r = await api.voices()
    voices.value = (r.voices || []).filter((v) => v.exists !== false)
  } catch (e) {
    ElMessage.error(`音色列表加载失败：${e.message}`)
  } finally {
    voiceLoading.value = false
  }
}

/** 选中预置音色 → 载入其音频片段作为参考音频 */
async function onVoicePick(id) {
  const k = audioKey.value
  if (!k || !id) return
  try {
    const resp = await fetch(api.voiceAudioUrl(id))
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const blob = await resp.blob()
    const v = voices.value.find((x) => x.id === id)
    const file = new File([blob], v?.file || `${id}.wav`, { type: blob.type || 'audio/wav' })
    if (audioUrls[k]) URL.revokeObjectURL(audioUrls[k])
    files[k] = file
    audioUrls[k] = URL.createObjectURL(blob)
    currentVoiceId.value = id
    ElMessage.success(`已载入音色：${v?.name || id}`)
  } catch (e) {
    ElMessage.error(`音色载入失败：${e.message}`)
    voicePick.value = ''
  }
}

function openSaveVoice() {
  if (!canSaveVoice.value) return
  const f = files[audioKey.value]
  voiceDlg.name = (f?.name || '新音色').replace(/\.[^.]+$/, '')
  voiceDlg.text = ''
  voiceDlg.autoTranscribe = true
  voiceDlg.visible = true
}

async function confirmSaveVoice() {
  const k = audioKey.value
  const f = files[k]
  if (!f) return
  voiceDlg.saving = true
  try {
    const r = await api.saveVoice({
      file: f,
      name: voiceDlg.name,
      gender: voiceDlg.gender,
      age: voiceDlg.age,
      text: voiceDlg.text || '',
      autoTranscribe: voiceDlg.autoTranscribe !== false,
    })
    voices.value = (r.voices || []).filter((v) => v.exists !== false)
    currentVoiceId.value = r.voice?.id || ''
    voicePick.value = r.voice?.id || ''
    voiceDlg.visible = false
    const asr = r.asr || {}
    if (asr.ok) {
      ElMessage.success(`音色「${r.voice?.name || ''}」已保存，并自动转写出参考文字稿（${asr.elapsed || 0}s）`)
    } else if (asr.error) {
      ElMessage.warning(`音色「${r.voice?.name || ''}」已保存，但自动转写失败：${asr.error}`)
    } else {
      ElMessage.success(`音色「${r.voice?.name || ''}」已保存到项目音色库`)
    }
  } catch (e) {
    ElMessage.error(`保存音色失败：${e.message}`)
  } finally {
    voiceDlg.saving = false
  }
}

/* 从「音色库」页点「工作台使用」跳过来：自动载入该音色 */
watch(
  () => route.query.voice,
  async (vid) => {
    if (!vid) return
    try {
      if (!meta.value) await loadMeta()
      await loadVoices()
      const v = voices.value.find((x) => x.id === vid)
      if (!v) {
        ElMessage.warning(`音色不存在：${vid}`)
        return
      }
      voicePick.value = v.id
      await onVoicePick(v.id)
      // 同时把这个音色设为服务端兜底用的参考音色（voice_id）
      voiceId.value = v.id
    } finally {
      // 清掉 query，避免再次切页时重复载入
      router.replace({ path: '/studio', query: {} })
    }
  },
)

async function onSend() {
  // 必填校验：引擎级（params.required）+ 端点级（endpoint_requires）
  // 端点级很关键：同一个引擎不同端点要的参数不一样
  // （Breeze 的 clone 必须给参考音频 + 参考文字稿，design 反而不要参考音频）。
  const missing = orderedKeys.value.filter(
    (k) => params.value[k]?.required && (fields[k] === '' || fields[k] === undefined),
  )
  const contract = meta.value?.endpoint_requires?.[endpoint.value] || {}
  for (const k of contract.required || []) {
    const empty =
      fields[k] === '' || fields[k] === undefined || fields[k] === null || files[k] === undefined
    if (empty && !missing.includes(k)) missing.push(k)
  }
  for (const group of contract.any_of || []) {
    if (!group || !group.length) continue
    const filled = group.some(
      (g) =>
        !(fields[g] === '' || fields[g] === undefined || fields[g] === null) || files[g] !== undefined,
    )
    if (!filled) missing.push(group.join(' 或 '))
  }
  if (missing.length) {
    ElMessage.warning(`缺少必填参数：${missingText(missing)}`)
    return
  }
  sending.value = true
  try {
    const payload = {}
    Object.entries(fields).forEach(([k, v]) => {
      if (v !== '' && v !== undefined && v !== null) payload[k] = v
    })
    // 参考音色：克隆类端点可指定 voice_id（管家内部参数，不会发给引擎）
    if (isCloneEndpoint.value && voiceId.value) payload.voice_id = voiceId.value
    const r = await synth({
      model: engineName.value,
      endpoint: endpoint.value,
      fields: payload,
      files: { ...files },
      bodyMode: meta.value?.body_mode || 'auto',
      format: format.value,
    })
    if (r.ok && r.kind === 'json' && r.json?.task_id) {
      hub.addTask({
        id: r.json.task_id,
        engine: r.engine,
        endpoint: endpoint.value,
      })
      // 任务型引擎：先在输出列表里占位（排队中），随轮询完成后就地可播放/下载
      pushTaskOutput(r, r.json.task_id)
      ElMessage.success('任务已创建，进度与成品将在右侧「合成音频输出」中跟踪')
    } else if (r.ok && r.kind === 'audio') {
      pushOutput(r)
      ElMessage.success(`完成 · ${r.ms}ms（音频已加入右侧输出列表）`)
    } else if (r.ok && r.kind === 'json' && hasInlineAudio(r.json)) {
      // base64 / audio_url 形态：直接把音频取回并入列
      const ok = await pushInlineAudio(r)
      ElMessage[ok ? 'success' : 'error'](
        ok ? `完成 · ${r.ms}ms（音频已加入右侧输出列表）` : '引擎返回了音频地址，但取回失败',
      )
    } else if (r.ok) {
      ElMessage.success(`完成 · ${r.ms}ms（成品可在「任务中心」查看）`)
    } else {
      ElMessage.error(engineErrorText(r))
    }
  } catch (e) {
    ElMessage.error(`请求失败：${e.message}`)
  } finally {
    sending.value = false
  }
}

function fmtSize(b) {
  if (!b) return '—'
  return b > 1024 * 1024 ? `${(b / 1048576).toFixed(2)} MB` : `${(b / 1024).toFixed(1)} KB`
}

/* ---------------- 合成音频输出列表 ----------------
   记录本工作台每一次合成的产物，供就地播放/下载。四种返回形态统一入列：
     kind='stream'  直返音频流（index25 stream / 部分引擎 clone）
     kind='task'    任务型（返回 task_id）→ 轮询任务端点 → 从下载端点取回音频
     kind='base64'  JSON 里带 audio_base64 → 本地解码
     kind='url'     JSON 里带 audio_url → 经管家 passthrough 取回
   条目状态：pending / running / done / failed。列表是会话内存态：keep-alive
   保证切标签回来不丢，刷新浏览器后 objectURL 失效属预期（需重新合成）。   */
const audioOutputs = ref([])
let outputSeq = 0

function fmtTime(ts) {
  const d = new Date(ts)
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

/** 取本次朗读文本做列表标题（各引擎主文本字段名不同） */
function currentText() {
  const k = inputTextKeys.value[0] || inputInstructionKeys.value[0]
  return k && fields[k] ? String(fields[k]) : ''
}

function outputTitle(seq) {
  const text = currentText()
  return text ? shorten(text.replace(/\s+/g, ' '), 24) : `合成音频 #${seq}`
}

/** 列表上限 30 条，超出时回收最旧一条的 objectURL */
function trimOutputs() {
  while (audioOutputs.value.length > 30) {
    const old = audioOutputs.value.pop()
    if (old?.url) URL.revokeObjectURL(old.url)
  }
}

function pushOutput(r) {
  const item = {
    id: ++outputSeq,
    kind: 'stream',
    state: 'done',
    progress: 100,
    url: r.url,
    size: r.size || 0,
    engine: r.engine || engineName.value,
    endpoint: endpoint.value,
    ms: r.ms,
    title: outputTitle(outputSeq),
    text: currentText(),
    createdAt: Date.now(),
    error: '',
  }
  audioOutputs.value.unshift(item)
  trimOutputs()
  return item
}

/* ---------------- 任务型引擎：轮询 + 取回成品 ---------------- */
let pollTimer = null

function ensurePolling() {
  if (pollTimer) return
  pollTimer = setInterval(pollOnce, 2500)
}

function stopPollingIfIdle() {
  const live = audioOutputs.value.some((o) => o.kind === 'task' && (o.state === 'pending' || o.state === 'running'))
  if (!live && pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function pollOnce() {
  const live = audioOutputs.value.filter(
    (o) => o.kind === 'task' && (o.state === 'pending' || o.state === 'running'),
  )
  for (const item of live) {
    try {
      await queryTask(item)
    } catch {
      /* 单个任务失败不影响其它 */
    }
  }
  stopPollingIfIdle()
}

/** 查询一次任务状态；完成后再取回音频字节 */
async function queryTask(item) {
  const eng = engineOf(hub.engines, item.engine)
  if (!eng?.endpoints?.task) {
    item.state = 'failed'
    item.error = '该引擎没有任务查询端点'
    return
  }
  const path = eng.endpoints.task.replace('{task_id}', item.taskId)
  try {
    const res = await api.passthrough(item.engine, path)
    item.result = res
    item.state = normalize(res.status)
    item.progress = normProgress(res.progress)
    if (res.error) item.error = res.error
    if (res.rtf) item.rtf = Number(res.rtf) || null
    if (res.audio_duration) item.audioDuration = Number(res.audio_duration) || null
    if (res.inference_time) item.inferenceTime = Number(res.inference_time) || null
    if (item.state === 'done') await fetchTaskAudio(item, res)
  } catch (e) {
    // 引擎可能刚被拉起导致首次查询超时；连续失败多次才判定为失败
    item.error = e.message
    item.failCount = (item.failCount || 0) + 1
    if (item.failCount >= 5) item.state = 'failed'
  }
}

/** 任务完成后取回音频字节
 *  取址优先级：引擎直接给出的音频地址 > 引擎声明的下载端点（{task_id} 模板）
 *  > 任务响应里的 output_path（相对路径，最后兜底） */
async function fetchTaskAudio(item, res) {
  if (item.url) return
  const eng = engineOf(hub.engines, item.engine)
  let path = null
  if (res?.audio_url) path = String(res.audio_url)
  else if (eng?.endpoints?.download) path = eng.endpoints.download.replace('{task_id}', item.taskId)
  else if (res?.output_path) path = String(res.output_path)
  if (!path) {
    item.error = '任务已完成，但该引擎未提供音频下载端点'
    return
  }
  try {
    const resp = await fetch(downloadUrl(item.engine, path))
    if (!resp.ok) throw new Error(`引擎返回 HTTP ${resp.status}`)
    const blob = await resp.blob()
    if (!blob.size) throw new Error('返回内容为空')
    item.url = URL.createObjectURL(blob)
    item.size = blob.size
    item.error = ''
  } catch (e) {
    item.error = `取回音频失败：${e.message}`
  }
}

/** 任务型：插入占位条目并开始轮询 */
function pushTaskOutput(r, taskId) {
  const item = {
    id: ++outputSeq,
    kind: 'task',
    state: 'pending',
    progress: 0,
    taskId,
    url: '',
    size: 0,
    engine: r.engine || engineName.value,
    endpoint: endpoint.value,
    ms: r.ms,
    title: outputTitle(outputSeq),
    text: currentText(),
    createdAt: Date.now(),
    error: '',
    failCount: 0,
  }
  audioOutputs.value.unshift(item)
  trimOutputs()
  ensurePolling()
  queryTask(item)
  return item
}

/** 手动重试：重新查询任务状态并尝试取回音频 */
async function retryTask(item) {
  item.failCount = 0
  item.error = ''
  if (item.state !== 'done') {
    item.state = 'running'
    ensurePolling()
  }
  await queryTask(item)
}

/* ---------------- base64 / audio_url 形态 ---------------- */
function hasInlineAudio(j) {
  if (!j || typeof j !== 'object') return false
  return !!(j.audio_base64 || j.audio_url || j.url)
}

/** JSON 里自带音频：base64 本地解码；audio_url 经管家 passthrough 取回 */
async function pushInlineAudio(r) {
  const j = r.json || {}
  try {
    if (j.audio_base64) {
      const blob = base64ToBlob(j.audio_base64, j.format ? `audio/${j.format}` : 'audio/wav')
      const item = {
        id: ++outputSeq,
        kind: 'base64',
        state: 'done',
        progress: 100,
        url: URL.createObjectURL(blob),
        size: blob.size,
        engine: r.engine || engineName.value,
        endpoint: endpoint.value,
        ms: r.ms,
        title: outputTitle(outputSeq),
        text: currentText(),
        createdAt: Date.now(),
        error: '',
      }
      if (j.duration) item.audioDuration = Number(j.duration) || null
      audioOutputs.value.unshift(item)
      trimOutputs()
      return true
    }
    // audio_url / url：走管家 passthrough 取回二进制
    let path = String(j.audio_url || j.url)
    if (/^https?:\/\//i.test(path)) {
      const resp = await fetch(path)
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const blob = await resp.blob()
      const item = {
        id: ++outputSeq,
        kind: 'url',
        state: 'done',
        progress: 100,
        url: URL.createObjectURL(blob),
        size: blob.size,
        engine: r.engine || engineName.value,
        endpoint: endpoint.value,
        ms: r.ms,
        title: outputTitle(outputSeq),
        text: currentText(),
        createdAt: Date.now(),
        error: '',
      }
      audioOutputs.value.unshift(item)
      trimOutputs()
      return true
    }
    if (path.startsWith('/')) path = path.slice(1)
    const resp = await fetch(downloadUrl(r.engine || engineName.value, path))
    if (!resp.ok) throw new Error(`引擎返回 HTTP ${resp.status}`)
    const blob = await resp.blob()
    if (!blob.size) throw new Error('返回内容为空')
    const item = {
      id: ++outputSeq,
      kind: 'url',
      state: 'done',
      progress: 100,
      url: URL.createObjectURL(blob),
      size: blob.size,
      engine: r.engine || engineName.value,
      endpoint: endpoint.value,
      ms: r.ms,
      title: outputTitle(outputSeq),
      text: currentText(),
      createdAt: Date.now(),
      error: '',
    }
    audioOutputs.value.unshift(item)
    trimOutputs()
    return true
  } catch (e) {
    ElMessage.error(`取回音频失败：${e.message}`)
    return false
  }
}

function removeOutput(item) {
  const i = audioOutputs.value.indexOf(item)
  if (i >= 0) audioOutputs.value.splice(i, 1)
  if (item.url) URL.revokeObjectURL(item.url)
  stopPollingIfIdle()
}

function clearOutputs() {
  audioOutputs.value.forEach((o) => o.url && URL.revokeObjectURL(o.url))
  audioOutputs.value = []
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function downloadOutput(item) {
  if (!item.url) return
  const a = document.createElement('a')
  a.href = item.url
  a.download = `${item.engine}-${item.createdAt}.wav`
  a.click()
}

/* 列表条目状态展示 */
const STATE_TEXT = { pending: '排队中', running: '生成中', done: '完成', failed: '失败' }
function stateTextOf(o) {
  return STATE_TEXT[o.state] || o.state
}
</script>

<template>
  <div class="studio">
    <!-- 顶部控制条 -->
    <div class="ctrl panel">
      <div class="ctrl__item">
        <span class="hud">ENGINE</span>
        <el-select v-model="engineName" size="small" style="width: 220px" filterable>
          <el-option v-for="e in hub.enabledEngines" :key="e.name" :value="e.name" :label="e.display_name">
            <span class="row" style="justify-content: space-between">
              <span>{{ e.display_name }}</span>
              <StateDot :state="hub.stateOf(e.name)" />
            </span>
          </el-option>
        </el-select>
      </div>

      <div class="ctrl__item" v-if="endpointKeys.length">
        <span class="hud">ENDPOINT</span>
        <el-radio-group v-model="endpoint" size="small">
          <el-radio-button v-for="k in endpointKeys" :key="k" :value="k">{{ k }}</el-radio-button>
        </el-radio-group>
      </div>

      <div class="ctrl__item" v-if="isCloneEndpoint">
        <span class="hud">参考音色</span>
        <el-select
          v-model="voiceId"
          size="small"
          style="width: 200px"
          clearable
          filterable
          placeholder="用默认音色"
        >
          <el-option
            v-for="v in voiceOptions"
            :key="v.id"
            :value="v.id"
            :label="v.name || v.id"
            :disabled="!v.exists"
          >
            <span class="row" style="justify-content: space-between; gap: 12px">
              <span>{{ v.name || v.id }}</span>
              <span class="mono-sm" style="opacity: 0.6">{{ v.id }}</span>
            </span>
          </el-option>
        </el-select>
      </div>

      <div class="ctrl__item">
        <span class="hud">BODY</span>
        <el-select v-model="format" size="small" style="width: 104px">
          <el-option value="auto" label="自动" />
          <el-option value="form" label="Form" />
          <el-option value="json" label="JSON" />
        </el-select>
      </div>

      <div class="spacer"></div>

      <div class="ctrl__path mono-sm" v-if="meta">
        POST <code>/api/tts/{{ endpoint }}?model={{ engineName }}</code>
        → <code>{{ meta.endpoints?.[endpoint] || '—' }}</code>
      </div>
    </div>

    <div class="cols">
      <!-- 左：输入区（参考音频 / 朗读文本 / 朗读指令） -->
      <section class="panel panel--notch">
        <div class="panel__hd">
          <span class="panel__title">输入 · 参考音频 / 朗读文本 / 朗读指令</span>
          <div class="row">
            <button class="btn btn--sm btn--ghost" @click="fillDefaults">填充默认</button>
            <button class="btn btn--sm btn--ghost" @click="clearAll">清空</button>
          </div>
        </div>

        <div class="panel__body">
          <div v-if="loadingMeta" class="mono-sm">加载参数表…</div>
          <div v-else-if="!orderedKeys.length" class="mono-sm">该引擎未声明参数</div>

          <template v-else>
            <!-- 参考音频 -->
            <div class="sub">
              <div class="sub__hd">参考音频</div>
              <div v-if="!inputAudioKeys.length" class="sub__empty mono-sm">该接口无需上传参考音频</div>
              <div v-for="k in inputAudioKeys" :key="'a' + k" class="fld fld--file">
                <div class="fld__hd">
                  <span class="fld__name">{{ k }}</span>
                  <span class="fld__type">file</span>
                </div>
                <el-upload
                  :auto-upload="false"
                  :show-file-list="false"
                  accept="audio/*,.wav,.mp3,.flac,.m4a"
                  :on-change="(f) => onFile(k, f)"
                >
                  <button class="upbtn" type="button">
                    <span v-if="files[k]">✓ {{ files[k].name }}</span>
                    <span v-else>选择参考音频…</span>
                  </button>
                </el-upload>
                <button v-if="files[k]" class="btn btn--sm btn--danger" @click.prevent="onFileRemove(k)">
                  移除
                </button>
                <!-- 音色库：选择预置音色直接载入；外部音频可一键存为项目音色 -->
                <div class="voicerow" v-if="k === audioKey">
                  <el-select
                    v-model="voicePick"
                    size="small"
                    class="voicesel"
                    placeholder="从项目音色库选择…"
                    :loading="voiceLoading"
                    clearable
                    filterable
                    @change="onVoicePick"
                  >
                    <el-option v-for="v in voices" :key="v.id" :value="v.id" :label="v.name">
                      <span class="row" style="justify-content: space-between; gap: 12px">
                        <span>{{ v.name }}</span>
                        <span class="mono-sm">{{ genderLabel(v.gender) }} · {{ ageLabel(v.age) }}</span>
                      </span>
                    </el-option>
                  </el-select>
                  <button
                    class="btn btn--sm"
                    :class="canSaveVoice ? 'btn--primary' : 'btn--ghost'"
                    :disabled="!canSaveVoice"
                    title="把当前载入的音频片段保存为项目音色"
                    @click="openSaveVoice"
                  >
                    保存音色
                  </button>
                </div>
                <!-- 参考音频播放控件 -->
                <audio v-if="audioUrls[k]" :src="audioUrls[k]" controls class="player player--ref"></audio>
                <div class="fld__desc" v-if="params[k]?.desc">{{ params[k].desc }}</div>
              </div>
            </div>

            <!-- 朗读文本 -->
            <div class="sub">
              <div class="sub__hd">朗读文本</div>
              <div v-if="!inputTextKeys.length" class="sub__empty mono-sm">该接口无需填写主文本</div>
              <div v-for="k in inputTextKeys" :key="'t' + k" class="fld">
                <div class="fld__hd">
                  <span class="fld__name">{{ k }}</span>
                  <span class="fld__req" v-if="isRequired(k)">必填</span>
                  <span class="fld__type">{{ params[k]?.type }}</span>
                  <div class="spacer"></div>
                  <el-select
                    :model-value="null"
                    class="phrases"
                    size="small"
                    placeholder="常用短语"
                    popper-class="phrases-popper"
                    title="选择后替换下方文本框内容"
                    @change="(v) => applyPhrase(k, v)"
                  >
                    <el-option v-for="(p, i) in PHRASES" :key="i" :value="p" :label="p">
                      <span class="phrases__opt" :title="p">{{ shorten(p) }}</span>
                    </el-option>
                  </el-select>
                </div>
                <DynamicField :field="k" :schema="params[k] || {}" v-model="fields[k]" />
                <div class="fld__desc" v-if="params[k]?.desc">{{ params[k].desc }}</div>
              </div>
            </div>

            <!-- 朗读指令 -->
            <div class="sub">
              <div class="sub__hd">朗读指令</div>
              <div v-if="!inputInstructionKeys.length" class="sub__empty mono-sm">该接口无独立朗读指令字段</div>
              <div v-for="k in inputInstructionKeys" :key="'i' + k" class="fld">
                <div class="fld__hd">
                  <span class="fld__name">{{ k }}</span>
                  <span class="fld__req" v-if="isRequired(k)">必填</span>
                  <span class="fld__type">{{ params[k]?.type }}</span>
                </div>
                <DynamicField :field="k" :schema="params[k] || {}" v-model="fields[k]" />
                <div class="fld__desc" v-if="params[k]?.desc">{{ params[k].desc }}</div>
              </div>
            </div>
          </template>
        </div>

        <div class="send">
          <button class="btn btn--primary btn--lg" :disabled="sending || !engineName" @click="onSend">
            {{ sending ? '合成中…' : '发送并透传' }}
          </button>
          <span class="mono-sm">
            首次请求会自动拉起 {{ engine?.display_name || '引擎' }}，耗时可能较长
          </span>
        </div>
      </section>

      <!-- 中：其他生成参数 -->
      <section class="panel">
        <div class="panel__hd">
          <span class="panel__title">其他生成参数</span>
          <span class="mono-sm">每项下方注明用途</span>
        </div>
        <div class="panel__body">
          <div v-if="loadingMeta" class="mono-sm">加载参数表…</div>
          <div v-else-if="!paramKeys.length" class="mono-sm">无其他生成参数</div>

          <!-- 两列布置：每个设置项之间用虚线分隔 -->
          <div v-else class="pgrid">
            <div v-for="k in paramKeys" :key="'p' + k" class="fld">
              <div class="fld__hd">
                <span class="fld__name">{{ k }}</span>
                <span class="fld__req" v-if="isRequired(k)">必填</span>
                <span class="fld__type">{{ params[k]?.type }}</span>
              </div>
              <DynamicField :field="k" :schema="params[k] || {}" v-model="fields[k]" />
              <div class="fld__desc" v-if="params[k]?.desc">{{ params[k].desc }}</div>
              <div class="fld__desc fld__desc--empty" v-else>（该参数用途暂无说明）</div>
            </div>
          </div>
        </div>
      </section>

      <!-- 右：合成音频输出列表（本工作台每次合成的产物，就地播放/下载） -->
      <aside class="panel">
        <div class="panel__hd">
          <span class="panel__title">
            合成音频输出
            <span class="panel__count" v-if="audioOutputs.length">{{ audioOutputs.length }}</span>
          </span>
          <button
            class="btn btn--sm btn--ghost"
            :disabled="!audioOutputs.length"
            @click="clearOutputs"
          >
            清空列表
          </button>
        </div>
        <div class="panel__body">
          <div v-if="!audioOutputs.length" class="placeholder">
            <div class="mono-sm">暂无合成音频</div>
            <div class="placeholder__hint">点「发送合成」后，生成的音频会在这里显示并可直接播放、下载。</div>
          </div>

          <div v-else class="alist">
            <div v-for="o in audioOutputs" :key="o.id" class="aitem">
              <div class="aitem__hd">
                <div class="aitem__title" :title="o.text">{{ o.title }}</div>
                <span class="aitem__state" :class="'aitem__state--' + o.state">{{ stateTextOf(o) }}</span>
                <button class="btn btn--sm btn--ghost" title="从列表移除" @click="removeOutput(o)">×</button>
              </div>

              <!-- 已就绪：就地播放 -->
              <audio v-if="o.url" :src="o.url" controls preload="metadata" class="aplayer"></audio>

              <!-- 任务型：排队/生成中，进度条 -->
              <div v-else-if="o.kind === 'task' && (o.state === 'pending' || o.state === 'running')" class="aitem__prog">
                <el-progress
                  :percentage="o.progress || 0"
                  :status="o.state === 'done' ? 'success' : o.state === 'failed' ? 'exception' : ''"
                  :stroke-width="8"
                  :show-text="false"
                />
                <span class="mono-sm">{{ o.progress || 0 }}%</span>
              </div>

              <div v-else class="aitem__err mono-sm">
                {{ o.error || '暂无音频' }}
              </div>

              <div class="aitem__meta mono-sm">
                <span>{{ fmtTime(o.createdAt) }}</span>
                <span v-if="o.engine"> · {{ o.engine }}</span>
                <span v-if="o.ms"> · {{ o.ms }}ms</span>
                <span v-if="o.size"> · {{ fmtSize(o.size) }}</span>
                <span v-if="o.taskId"> · {{ o.taskId }}</span>
              </div>
              <div class="aitem__acts">
                <button
                  v-if="o.kind === 'task' && !o.url"
                  class="btn btn--sm btn--ghost"
                  title="重新查询任务状态并尝试取回音频"
                  @click="retryTask(o)"
                >
                  重试
                </button>
                <button class="btn btn--sm btn--ghost" :disabled="!o.url" @click="downloadOutput(o)">下载 wav</button>
              </div>
            </div>
          </div>
        </div>
      </aside>
    </div>

    <!-- 保存音色：填写音色属性，存入项目音色库（voice/ + voice.json） -->
    <el-dialog
      v-model="voiceDlg.visible"
      title="保存为项目音色"
      width="420px"
      append-to-body
    >
      <div class="vdlg">
        <label class="vdlg__row">
          <span class="vdlg__label">音色名称</span>
          <el-input v-model="voiceDlg.name" size="small" placeholder="例如：女声-新闻播报" />
        </label>
        <label class="vdlg__row">
          <span class="vdlg__label">性别</span>
          <el-select v-model="voiceDlg.gender" size="small" style="width: 100%">
            <el-option v-for="o in GENDER_OPTS" :key="o.v" :value="o.v" :label="o.l" />
          </el-select>
        </label>
        <label class="vdlg__row">
          <span class="vdlg__label">年龄</span>
          <el-select v-model="voiceDlg.age" size="small" style="width: 100%">
            <el-option v-for="o in AGE_OPTS" :key="o.v" :value="o.v" :label="o.l" />
          </el-select>
        </label>
        <label class="vdlg__row">
          <span class="vdlg__label">参考文字稿</span>
          <el-input
            v-model="voiceDlg.text"
            type="textarea"
            :rows="2"
            size="small"
            placeholder="可留空：留空时会用 ASR 自动转写这段音频并存进音色库"
          />
        </label>
        <label class="vdlg__row">
          <span class="vdlg__label">自动转写</span>
          <el-checkbox v-model="voiceDlg.autoTranscribe" :disabled="!!voiceDlg.text.trim()">
            未填写文字稿时自动 ASR 转写（入库后克隆直接复用，省掉引擎侧重复转写）
          </el-checkbox>
        </label>
        <p class="mono-sm vdlg__tip">
          保存后音频文件会复制到项目 voice/ 目录，并写入 voice/voice.json 音色目录。
        </p>
      </div>
      <template #footer>
        <button class="btn btn--sm btn--ghost" @click="voiceDlg.visible = false">取消</button>
        <button class="btn btn--sm btn--primary" :disabled="voiceDlg.saving" @click="confirmSaveVoice">
          {{ voiceDlg.saving ? '保存中…' : '保存' }}
        </button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.ctrl {
  display: flex;
  align-items: center;
  gap: 22px;
  padding: 14px 16px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}

.ctrl__item {
  display: flex;
  align-items: center;
  gap: 10px;
}

.ctrl__path code {
  color: var(--amber);
}

/* 三栏：输入 | 其他生成参数 | 合成音频输出 */
.cols {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) 360px;
  gap: 16px;
  align-items: start;
}

.fld {
  min-width: 0;
}

/* ---------- 其他生成参数：两列网格 + 虚线分隔 ---------- */
.pgrid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  column-gap: 18px;
  row-gap: 2px;
}

.pgrid > .fld {
  border-top: 1px dashed var(--line);
  padding: 12px 0 14px;
}

/* 窄列里数值控件改为上下堆叠，避免滑块被挤扁 */
.pgrid :deep(.numfield) {
  flex-wrap: wrap;
}

.pgrid :deep(.numfield .el-slider) {
  flex: 1 1 100%;
  min-width: 100%;
  margin-right: 0;
}

.pgrid :deep(.numfield .el-input-number) {
  flex: 1 1 100%;
  width: 100% !important;
}

.fld__hd {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 7px;
}

.fld__name {
  font-size: 13px;
  color: var(--text);
  font-weight: 500;
  letter-spacing: 0.01em;
}

.fld--req .fld__name {
  color: var(--amber-2);
  font-weight: 600;
}

.fld__req {
  font-size: 10px;
  color: #1a1305;
  background: var(--amber);
  padding: 1px 5px;
  border-radius: 3px;
  letter-spacing: 0.08em;
  font-weight: 700;
}

.fld__type {
  font-size: 10.5px;
  color: var(--text-mute);
  border: 1px solid var(--line-2);
  padding: 1px 5px;
  border-radius: 3px;
}

/* 参数用途说明 */
.fld__desc {
  margin-top: 6px;
  font-size: 11.5px;
  color: var(--text-dim);
  line-height: 1.6;
}

.fld__desc--empty {
  opacity: 0.55;
  font-style: italic;
}

/* 输入区三个子分区 */
.sub {
  padding-bottom: 16px;
  margin-bottom: 16px;
  border-bottom: 1px dashed var(--line);
}
.sub:last-child {
  border-bottom: none;
  margin-bottom: 0;
  padding-bottom: 0;
}

.sub__hd {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-mute);
  letter-spacing: 0.12em;
  text-transform: uppercase;
  margin-bottom: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.sub__hd::before {
  content: '';
  width: 3px;
  height: 12px;
  background: var(--amber);
  border-radius: 2px;
}

.sub__empty {
  padding: 4px 0 8px;
  opacity: 0.6;
}

.sub .fld {
  margin-bottom: 14px;
}
.sub .fld:last-child {
  margin-bottom: 0;
}

/* 常用短语下拉（下拉面板挂在 body，样式见 styles/main.css 的 .phrases-popper） */
.phrases {
  width: 176px;
  flex: none;
}

.fld--file {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 8px 10px;
  align-items: center;
}
.fld--file .fld__hd {
  grid-column: 1 / -1;
}
.fld--file .fld__desc {
  grid-column: 1 / -1;
}

.upbtn {
  height: 32px;
  padding: 0 14px;
  background: var(--panel-3);
  border: 1px dashed var(--line-3);
  color: var(--text);
  font-family: var(--font-mono);
  font-size: 12px;
  cursor: pointer;
  border-radius: var(--radius);
}

.upbtn:hover {
  border-color: var(--amber);
  color: var(--amber-2);
  background: var(--amber-soft);
}

/* 音色库行：下拉选择 + 保存音色 */
.voicerow {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 2px;
}

.voicesel {
  flex: 1 1 auto;
  min-width: 0;
}

/* 保存音色弹窗 */
.vdlg {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.vdlg__row {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.vdlg__label {
  font-size: 12px;
  color: var(--text-dim);
  letter-spacing: 0.04em;
}

.vdlg__tip {
  margin: 0;
  line-height: 1.6;
}

/* 参考音频播放控件 */
.player--ref {
  grid-column: 1 / -1;
  width: 100%;
  height: 36px;
  filter: invert(0.92) hue-rotate(180deg) saturate(0.6);
}

.send {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 14px 16px;
  border-top: 1px solid var(--line);
  background: linear-gradient(180deg, rgba(255, 164, 43, 0.05), transparent);
}

.placeholder {
  display: grid;
  place-items: center;
  gap: 14px;
  height: 180px;
  color: var(--text-mute);
  font-size: 12.5px;
}

.placeholder__hint {
  max-width: 260px;
  text-align: center;
  color: var(--text-mute);
  font-size: 11.5px;
  line-height: 1.6;
  opacity: 0.8;
}

.panel__count {
  display: inline-block;
  min-width: 18px;
  padding: 0 5px;
  margin-left: 6px;
  border-radius: 9px;
  background: var(--amber);
  color: #1a1305;
  font-size: 11px;
  font-weight: 700;
  text-align: center;
  line-height: 18px;
}

/* 合成音频输出列表 */
.alist {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.aitem {
  border: 1px solid var(--line);
  border-radius: var(--radius);
  overflow: hidden;
  background: var(--bg-deep);
}

.aitem__hd {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px 6px;
}

.aitem__title {
  flex: 1;
  min-width: 0;
  font-size: 12.5px;
  font-weight: 500;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 条目状态标签 */
.aitem__state {
  flex: none;
  font-size: 10.5px;
  padding: 1px 6px;
  border-radius: 3px;
  border: 1px solid var(--line-2);
  color: var(--text-mute);
}
.aitem__state--done {
  color: var(--green);
  border-color: var(--green);
}
.aitem__state--running,
.aitem__state--pending {
  color: var(--amber);
  border-color: var(--amber);
}
.aitem__state--failed {
  color: var(--red);
  border-color: var(--red);
}

/* 任务型条目进度 */
.aitem__prog {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 10px 2px;
}
.aitem__prog .el-progress {
  flex: 1;
}

.aitem__err {
  padding: 10px 10px 2px;
  color: #ffb0b0;
  line-height: 1.6;
  word-break: break-all;
}

.aplayer {
  display: block;
  width: calc(100% - 20px);
  height: 36px;
  margin: 0 10px;
}

.aitem__meta {
  padding: 8px 10px 0;
  color: var(--text-mute);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.aitem__acts {
  display: flex;
  justify-content: flex-end;
  padding: 6px 10px 10px;
}

@media (max-width: 1380px) {
  .cols {
    grid-template-columns: minmax(0, 1fr) 360px;
  }
  /* 输入与参数上下堆叠占满首列 */
  .cols > section:first-child {
    grid-column: 1 / -1;
  }
  /* 中栏变窄，参数区退回单列 */
  .pgrid {
    grid-template-columns: minmax(0, 1fr);
  }
}

@media (max-width: 980px) {
  .cols {
    grid-template-columns: 1fr;
  }
  .cols > section:first-child {
    grid-column: auto;
  }
}
</style>
