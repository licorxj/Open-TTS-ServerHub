<script setup>
import { ref, reactive, computed, watch, onMounted } from 'vue'
import { useHubStore } from '../stores/hub'
import { api, synth, stripAlias } from '../api'
import DynamicField from '../components/DynamicField.vue'
import StateDot from '../components/StateDot.vue'
import { ElMessage } from 'element-plus'

const hub = useHubStore()

const meta = ref(null)
const params = ref({})
const fields = reactive({})
const files = reactive({})
const endpoint = ref('')
const format = ref('auto')
const sending = ref(false)
const result = ref(null)
const loadingMeta = ref(false)

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
const normalKeys = computed(() => orderedKeys.value.filter((k) => params.value[k]?.type !== 'file'))

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

async function loadMeta() {
  if (!hub.current) return
  loadingMeta.value = true
  try {
    meta.value = await api.params(hub.current)
    // stripAlias：/params 会额外暴露规范别名字段，表单里只保留引擎原生字段
    params.value = stripAlias(meta.value.params)
    endpoint.value = meta.value.default_endpoint || 'clone'
    Object.keys(fields).forEach((k) => delete fields[k])
    Object.keys(files).forEach((k) => delete files[k])
    result.value = null
    applySmartDefaults()
  } catch (e) {
    ElMessage.error(e.message)
    meta.value = null
    params.value = {}
  } finally {
    loadingMeta.value = false
  }
}

onMounted(loadMeta)
watch(() => hub.current, loadMeta)

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
}

function onFile(k, uploadFile) {
  files[k] = uploadFile.raw
}
function onFileRemove(k) {
  delete files[k]
}

async function onSend() {
  const missing = orderedKeys.value.filter((k) => params.value[k]?.required && (fields[k] === '' || fields[k] === undefined))
  if (missing.length) {
    ElMessage.warning(`缺少必填参数：${missing.join(', ')}`)
    return
  }
  sending.value = true
  result.value = null
  try {
    const payload = {}
    Object.entries(fields).forEach(([k, v]) => {
      if (v !== '' && v !== undefined && v !== null) payload[k] = v
    })
    const r = await synth({
      model: engineName.value,
      endpoint: endpoint.value,
      fields: payload,
      files: { ...files },
      bodyMode: meta.value?.body_mode || 'auto',
      format: format.value,
    })
    result.value = r
    if (r.ok && r.kind === 'json' && r.json?.task_id) {
      hub.addTask({
        id: r.json.task_id,
        engine: r.engine,
        endpoint: endpoint.value,
      })
      ElMessage.success('任务已创建，可在「任务中心」跟踪进度')
    } else if (r.ok) {
      ElMessage.success(`完成 · ${r.ms}ms`)
    } else {
      ElMessage.error(`引擎返回 HTTP ${r.status}`)
    }
  } catch (e) {
    ElMessage.error(`请求失败：${e.message}`)
  } finally {
    sending.value = false
  }
}

function download() {
  if (!result.value?.url) return
  const a = document.createElement('a')
  a.href = result.value.url
  a.download = `${engineName.value}-${Date.now()}.wav`
  a.click()
}

function fmtSize(b) {
  if (!b) return '—'
  return b > 1024 * 1024 ? `${(b / 1048576).toFixed(2)} MB` : `${(b / 1024).toFixed(1)} KB`
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
      <!-- 左：参数表单 -->
      <section class="panel panel--notch">
        <div class="panel__hd">
          <span class="panel__title">请求参数 · 透传给引擎</span>
          <div class="row">
            <button class="btn btn--sm btn--ghost" @click="fillDefaults">填充默认</button>
            <button class="btn btn--sm btn--ghost" @click="clearAll">清空</button>
          </div>
        </div>

        <div class="panel__body">
          <div v-if="loadingMeta" class="mono-sm">加载参数表…</div>
          <div v-else-if="!orderedKeys.length" class="mono-sm">该引擎未声明参数</div>

          <el-form v-else label-position="top" size="small" class="form">
            <div
              v-for="k in normalKeys"
              :key="k"
              class="fld"
              :class="{ 'fld--req': params[k]?.required }"
            >
              <div class="fld__hd">
                <span class="fld__name">{{ k }}</span>
                <span class="fld__req" v-if="params[k]?.required">必填</span>
                <span class="fld__type">{{ params[k]?.type }}</span>
                <template v-if="isMainTextField(k)">
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
                </template>
              </div>
              <DynamicField :field="k" :schema="params[k] || {}" v-model="fields[k]" />
              <div class="fld__desc" v-if="params[k]?.desc">{{ params[k].desc }}</div>
            </div>

            <!-- 文件字段 -->
            <div v-for="k in fileKeys" :key="'f' + k" class="fld fld--file">
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
              <button v-if="files[k]" class="btn btn--sm btn--danger" @click.prevent="onFileRemove(k)">移除</button>
              <div class="fld__desc" v-if="params[k]?.desc">{{ params[k].desc }}</div>
            </div>
          </el-form>
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

      <!-- 右：结果 -->
      <aside class="panel">
        <div class="panel__hd">
          <span class="panel__title">响应 · 原样回吐</span>
          <span v-if="result" class="mono-sm">
            HTTP {{ result.status }} · {{ result.ms }}ms · {{ result.engine }}
          </span>
        </div>
        <div class="panel__body">
          <div v-if="!result" class="placeholder">
            <div class="ph__bars"><i v-for="n in 22" :key="n"></i></div>
            <div class="mono-sm">尚未发起请求</div>
          </div>

          <template v-else>
            <!-- 音频 -->
            <div v-if="result.kind === 'audio'" class="res">
              <audio :src="result.url" controls class="player"></audio>
              <div class="row" style="margin-top: 10px">
                <button class="btn btn--primary" @click="download">下载 wav</button>
                <span class="mono-sm">{{ fmtSize(result.size) }}</span>
              </div>
            </div>

            <!-- JSON -->
            <div v-else-if="result.kind === 'json'" class="res">
              <div v-if="result.json?.task_id" class="taskbox">
                <span class="hud hud--amber">ASYNC TASK</span>
                <code>{{ result.json.task_id }}</code>
                <span class="mono-sm">已加入任务中心，去那里轮询与下载</span>
              </div>
              <pre class="json">{{ JSON.stringify(result.json, null, 2) }}</pre>
            </div>

            <div v-else class="res">
              <pre class="json">{{ result.text }}</pre>
            </div>
          </template>
        </div>
      </aside>
    </div>
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

.cols {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 420px;
  gap: 16px;
  align-items: start;
}

.form {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 20px 22px;
}

.fld {
  min-width: 0;
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

.fld__desc {
  margin-top: 6px;
  font-size: 11.5px;
  color: var(--text-dim);
  line-height: 1.6;
}

/* 常用短语下拉（下拉面板挂在 body，样式见 styles/main.css 的 .phrases-popper） */
.phrases {
  width: 176px;
  flex: none;
}

.fld--file {
  grid-column: span 2;
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
  height: 240px;
  color: var(--text-mute);
  font-size: 12.5px;
}

.ph__bars {
  display: flex;
  align-items: flex-end;
  gap: 3px;
  height: 46px;
  opacity: 0.35;
}

.ph__bars i {
  width: 3px;
  background: var(--amber);
  height: 20%;
}

.ph__bars i:nth-child(3n) { height: 60%; }
.ph__bars i:nth-child(4n) { height: 90%; }
.ph__bars i:nth-child(5n) { height: 38%; }

.player {
  width: 100%;
  height: 36px;
  filter: invert(0.92) hue-rotate(180deg) saturate(0.6);
}

.taskbox {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 12px;
  margin-bottom: 10px;
  border: 1px solid var(--amber-line);
  background: var(--amber-soft);
}

.taskbox code {
  color: var(--text);
  font-size: 12.5px;
}

.json {
  margin: 0;
  padding: 12px 14px;
  background: var(--bg-deep);
  border: 1px solid var(--line);
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-dim);
  max-height: 480px;
  overflow: auto;
}

@media (max-width: 1280px) {
  .cols {
    grid-template-columns: 1fr;
  }
  .form {
    grid-template-columns: 1fr;
  }
}
</style>
