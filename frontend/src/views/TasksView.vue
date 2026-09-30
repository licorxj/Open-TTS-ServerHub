<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useHubStore } from '../stores/hub'
import { api, downloadUrl } from '../api'
import { ElMessage } from 'element-plus'

const hub = useHubStore()

const addOpen = ref(false)
const newTask = ref({ engine: '', id: '', endpoint: 'clone' })
const refreshing = ref(false)

// 音频监听台：点击「播放」才去拉取音频（懒加载），并按 task 缓存 Blob URL
const audio = ref(null)
const playing = ref(false)
const audioCache = new Map()

const DONE = ['completed', 'success', 'succeeded', 'finished', 'done']
const FAIL = ['failed', 'error', 'cancelled']

const rows = computed(() => hub.tasks)

/** 引擎查找：key / display_name / alias 都能命中（外部任务可能存的是任意一种写法） */
function engineOf(name) {
  if (!name) return null
  const hit = hub.engineByName[name]
  if (hit) return hit
  const t = String(name).toLowerCase()
  return (
    hub.engines.find(
      (e) =>
        e.name.toLowerCase() === t ||
        String(e.display_name || '').toLowerCase() === t ||
        (e.aliases || []).some((a) => String(a).toLowerCase() === t),
    ) || null
  )
}

/* ---------------- RTF 与耗时：优先用轮询写入的字段，缺失时回落到 result 原始响应 ----------------
   这样即使是"RTF 功能上线之前"创建、或只有原始响应没有快照字段的旧任务，也能正常显示。 */
function pickNum(row, field, rawKey) {
  const v = row[field] ?? row.result?.[rawKey]
  const n = Number(v)
  return Number.isFinite(n) && n > 0 ? n : null
}

const rtfOf = (row) => pickNum(row, 'rtf', 'rtf')
const audioOf = (row) => pickNum(row, 'audioDuration', 'audio_duration')
const inferOf = (row) => pickNum(row, 'inferenceTime', 'inference_time')

function fmtTime(ts) {
  const d = new Date(ts)
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

function normalize(state) {
  if (!state) return 'running'
  const s = String(state).toLowerCase()
  if (DONE.includes(s)) return 'done'
  if (FAIL.includes(s)) return 'failed'
  if (s === 'pending' || s === 'queued') return 'pending'
  return 'running'
}

function normProgress(p) {
  if (p === undefined || p === null) return 0
  const n = Number(p)
  if (Number.isNaN(n)) return 0
  return n <= 1 ? Math.round(n * 100) : Math.round(n)
}

async function queryOne(t) {
  const eng = engineOf(t.engine)
  if (!eng?.endpoints?.task) {
    t.state = 'failed'
    t.result = { error: '该引擎没有任务查询端点' }
    return
  }
  const path = eng.endpoints.task.replace('{task_id}', t.id)
  try {
    const r = await api.passthrough(t.engine, path)
    t.result = r
    t.state = normalize(r.status)
    t.progress = normProgress(r.progress)
    if (r.error) t.error = r.error
    // 5 个任务型引擎的任务状态都带这三个字段；非任务型引擎没有
    t.rtf = Number(r.rtf) || null
    t.audioDuration = Number(r.audio_duration) || null
    t.inferenceTime = Number(r.inference_time) || null
  } catch (e) {
    // 任务可能已被引擎清理
    t.state = 'failed'
    t.error = e.message
  }
}

async function refreshAll() {
  refreshing.value = true
  const live = rows.value.filter((t) => t.state === 'running' || t.state === 'pending')
  await Promise.all(live.map(queryOne))
  hub.persistTasks()
  refreshing.value = false
}

/** 把服务端的任务台账合并进列表 —— 外部程序直接调管家创建的任务也能出现在这里 */
async function syncServerTasks() {
  try {
    const r = await api.serverTasks({ limit: 200 })
    const known = new Set(hub.tasks.map((t) => `${t.engine}:${t.id}`))
    const add = []
    for (const s of r.items || []) {
      if (!s.task_id) continue
      const key = `${s.engine}:${s.task_id}`
      if (known.has(key)) continue
      add.push({
        id: s.task_id,
        engine: s.engine,
        endpoint: s.endpoint || 'clone',
        createdAt: Math.round((s.created_at || 0) * 1000) || Date.now(),
        state: 'pending',
        progress: 0,
        result: null,
        external: true, // 不是从本面板发起的
        client: s.client,
      })
      known.add(key)
    }
    if (add.length) {
      hub.tasks.unshift(...add.reverse()) // items 为「新→旧」，倒序插入保持最新在前
      hub.persistTasks()
    }
  } catch {
    /* 服务端台账不可用时静默降级为纯本地模式 */
  }
}

let timer = null
let tick = 0
onMounted(async () => {
  await hub.refreshEngines()
  newTask.value.engine = hub.current
  await syncServerTasks()
  await refreshAll()
  timer = setInterval(async () => {
    tick++
    if (tick % 5 === 0) await syncServerTasks() // 约每 12s 与服务端台账对齐一次
    if (rows.value.some((t) => t.state === 'running' || t.state === 'pending')) refreshAll()
  }, 2500)
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
  audioCache.forEach((v) => URL.revokeObjectURL(v.url))
  audioCache.clear()
})

function canDownload(t) {
  const eng = engineOf(t.engine)
  return t.state === 'done' && !!eng?.endpoints?.download
}

function download(t) {
  const eng = engineOf(t.engine)
  const path = eng.endpoints.download.replace('{task_id}', t.id)
  window.open(downloadUrl(t.engine, path), '_blank')
}

function canPlay(t) {
  const eng = engineOf(t.engine)
  return t.state === 'done' && !!eng?.endpoints?.download
}

/** RTF 配色：沿用各引擎自己的阈值 —— <1 绿（快于实时）/ <2 黄 / >=2 红 */
function rtfClass(row) {
  const n = rtfOf(row)
  if (!n) return ''
  if (n < 1) return 'rtf--fast'
  if (n < 2) return 'rtf--mid'
  return 'rtf--slow'
}

function rtfTitle(row) {
  const parts = []
  const a = audioOf(row)
  const i = inferOf(row)
  if (a) parts.push(`音频时长 ${a.toFixed(2)}s`)
  if (i) parts.push(`推理耗时 ${i.toFixed(2)}s`)
  parts.push('RTF = 推理耗时 ÷ 音频时长，<1 表示生成快于实时')
  return parts.join(' · ')
}

function isPlaying(t) {
  return (
    audio.value?.taskId === t.id &&
    audio.value?.engine === t.engine &&
    !audio.value?.error &&
    !audio.value?.loading
  )
}

/** 懒加载播放：首次点击才请求音频字节，同一任务重复播放走缓存 */
async function play(t) {
  const key = `${t.engine}:${t.id}`
  const eng = engineOf(t.engine)
  if (!eng?.endpoints?.download) {
    ElMessage.warning('该引擎没有下载端点，无法播放')
    return
  }

  playing.value = false
  if (audioCache.has(key)) {
    audio.value = { ...audioCache.get(key), engine: t.engine, taskId: t.id }
    return
  }

  audio.value = { loading: true, engine: t.engine, taskId: t.id }
  const path = eng.endpoints.download.replace('{task_id}', t.id)
  try {
    const resp = await fetch(downloadUrl(t.engine, path))
    if (!resp.ok) throw new Error(`引擎返回 HTTP ${resp.status}`)
    const blob = await resp.blob()
    if (!blob.size) throw new Error('返回内容为空')
    const url = URL.createObjectURL(blob)
    audioCache.set(key, { url, size: blob.size })
    audio.value = { url, size: blob.size, engine: t.engine, taskId: t.id }
  } catch (e) {
    audio.value = {
      error: `拉取失败：${e.message}。引擎可能已被空闲回收，或任务在引擎重启后被清理。`,
      engine: t.engine,
      taskId: t.id,
    }
  }
}

function closePlayer() {
  audio.value = null
  playing.value = false
}

function fmtSize(b) {
  if (!b) return '—'
  return b > 1048576 ? `${(b / 1048576).toFixed(2)} MB` : `${(b / 1024).toFixed(1)} KB`
}

function addTask() {
  if (!newTask.value.engine || !newTask.value.id.trim()) {
    ElMessage.warning('请填写引擎与 task_id')
    return
  }
  hub.addTask({
    id: newTask.value.id.trim(),
    engine: newTask.value.engine,
    endpoint: newTask.value.endpoint,
  })
  newTask.value.id = ''
  addOpen.value = false
  queryOne(hub.tasks[0])
}

const stateText = { pending: '排队中', running: '运行中', done: '完成', failed: '失败' }
const stateColor = { pending: 'info', running: 'warning', done: 'success', failed: 'danger' }
</script>

<template>
  <div>
    <div class="ctrl panel">
      <span class="hud">ASYNC TASKS</span>
      <span class="mono-sm">
        任务型引擎返回 task_id；由管家发起的和<b style="color: var(--text)">外部直接调用管家</b>创建的任务都会出现在这里
      </span>
      <div class="spacer"></div>
      <button class="btn" :disabled="refreshing" @click="refreshAll">刷新</button>
      <button class="btn btn--primary" @click="addOpen = !addOpen">＋ 添加任务</button>
    </div>

    <div v-if="addOpen" class="panel addbox">
      <div class="addbox__row">
        <span class="hud">ENGINE</span>
        <el-select v-model="newTask.engine" size="small" style="width: 200px">
          <el-option v-for="e in hub.engines" :key="e.name" :value="e.name" :label="e.display_name" />
        </el-select>
        <span class="hud">TASK ID</span>
        <el-input v-model="newTask.id" size="small" style="width: 300px" placeholder="粘贴 task_id" />
        <button class="btn btn--primary" @click="addTask">加入</button>
      </div>
    </div>

    <div class="panel">
      <el-table :data="rows" size="small" empty-text="暂无任务 —— 在「合成工作台」发起一次任务型引擎的合成即可自动出现">
        <el-table-column prop="engine" label="引擎" width="148">
          <template #default="{ row }">
            <span class="eng">{{ engineOf(row.engine)?.display_name || row.engine }}</span>
            <span
              v-if="row.external"
              class="ext"
              :title="`由外部请求创建${row.client ? '（来自 ' + row.client + '）' : ''}`"
            >外部</span>
          </template>
        </el-table-column>

        <el-table-column prop="id" label="TASK ID" min-width="220">
          <template #default="{ row }"><code class="tid">{{ row.id }}</code></template>
        </el-table-column>

        <el-table-column label="状态" width="96">
          <template #default="{ row }">
            <el-tag :type="stateColor[row.state]" size="small" effect="dark">
              {{ stateText[row.state] || row.state }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="进度" width="180">
          <template #default="{ row }">
            <el-progress
              :percentage="row.progress || 0"
              :status="row.state === 'done' ? 'success' : row.state === 'failed' ? 'exception' : ''"
              :stroke-width="8"
              :show-text="false"
            />
            <span class="mono-sm num">{{ row.progress || 0 }}%</span>
          </template>
        </el-table-column>

        <el-table-column width="104" align="right">
          <template #header>
            <span class="th" title="RTF = 推理耗时 ÷ 音频时长；<1 表示生成快于实时">
              RTF
            </span>
          </template>
          <template #default="{ row }">
            <span v-if="rtfOf(row)" class="rtf" :class="rtfClass(row)" :title="rtfTitle(row)">
              {{ rtfOf(row).toFixed(3) }}
            </span>
            <span v-else class="mono-sm">—</span>
          </template>
        </el-table-column>

        <el-table-column label="创建" width="90">
          <template #default="{ row }"><span class="mono-sm num">{{ fmtTime(row.createdAt) }}</span></template>
        </el-table-column>

        <el-table-column label="操作" width="292" align="right">
          <template #default="{ row, $index }">
            <button class="btn btn--sm" @click="queryOne(row)">查询</button>
            <button class="btn btn--sm btn--play" :disabled="!canPlay(row)" @click="play(row)">
              {{ isPlaying(row) ? '播放中' : '播放' }}
            </button>
            <button class="btn btn--sm btn--primary" :disabled="!canDownload(row)" @click="download(row)">
              下载
            </button>
            <button class="btn btn--sm btn--danger" @click="hub.removeTask($index)">删除</button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 音频监听台：懒加载播放 -->
    <div class="panel player" v-if="audio">
      <div class="player__hd">
        <span class="hud hud--amber">MONITOR</span>
        <span class="player__title">{{ engineOf(audio.engine)?.display_name || audio.engine }}</span>
        <code class="tid">{{ audio.taskId }}</code>
        <div class="spacer"></div>
        <span class="mono-sm" v-if="audio.size">{{ fmtSize(audio.size) }}</span>
        <button class="btn btn--sm btn--ghost" @click="closePlayer">关闭</button>
      </div>
      <div class="player__body">
        <div v-if="audio.loading" class="mono-sm">
          正在拉取音频…（若引擎已被空闲回收，管家会尝试重新拉起，可能耗时较久）
        </div>
        <div v-else-if="audio.error" class="player__err">{{ audio.error }}</div>
        <template v-else>
          <audio
            :src="audio.url"
            controls
            autoplay
            class="player__audio"
            @play="playing = true"
            @pause="playing = false"
            @ended="playing = false"
          ></audio>
          <div class="bars" :class="{ 'bars--on': playing }">
            <i v-for="n in 48" :key="n" :style="{ '--i': n }"></i>
          </div>
        </template>
      </div>
    </div>

    <div class="panel" style="margin-top: 14px" v-if="rows.some((t) => t.result)">
      <div class="panel__hd"><span class="panel__title">最近一次查询详情</span></div>
      <div class="panel__body">
        <pre class="kv">{{ JSON.stringify(rows.find((t) => t.result)?.result, null, 2) }}</pre>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ctrl {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 13px 16px;
  margin-bottom: 16px;
}

.addbox {
  padding: 14px 16px;
  margin-bottom: 16px;
}

.addbox__row {
  display: flex;
  align-items: center;
  gap: 12px;
}

.eng {
  color: var(--amber-2);
  font-size: 13px;
}

.ext {
  margin-left: 6px;
  font-size: 10px;
  padding: 1px 5px;
  border: 1px solid var(--line-3);
  color: var(--text-dim);
  border-radius: 3px;
  cursor: help;
  vertical-align: 1px;
}

.tid {
  color: var(--cyan);
  font-size: 11.5px;
}

.kv {
  margin: 0;
  padding: 12px 14px;
  background: var(--bg-deep);
  border: 1px solid var(--line);
  font-size: 12px;
  color: var(--text-dim);
  max-height: 340px;
  overflow: auto;
  line-height: 1.65;
}

/* ---- RTF 列 ---- */
.th {
  cursor: help;
  border-bottom: 1px dotted var(--line-3);
}

.rtf {
  font-variant-numeric: tabular-nums;
  font-size: 13px;
  font-weight: 700;
  cursor: help;
}

.rtf--fast {
  color: var(--green);
}

.rtf--mid {
  color: var(--amber);
}

.rtf--slow {
  color: var(--red);
}

/* ---- 音频监听台 ---- */
.player {
  margin-top: 14px;
  animation: rise 0.35s var(--ease) both;
}

.player__hd {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--line);
}

.player__title {
  font-family: var(--font-display);
  font-size: 14px;
  font-weight: 600;
  color: var(--cyan);
}

.player__body {
  padding: 14px;
}

.player__err {
  font-size: 12.5px;
  color: #ffb0b0;
  line-height: 1.75;
}

.player__audio {
  width: 100%;
  height: 36px;
  filter: invert(0.92) hue-rotate(180deg) saturate(0.6);
}

.bars {
  display: flex;
  align-items: flex-end;
  gap: 2px;
  height: 34px;
  margin-top: 12px;
}

.bars i {
  flex: 1;
  height: calc(16% + var(--i) * 1.7%);
  background: var(--line-2);
  border-radius: 1px;
  transform-origin: bottom;
  transition: background 0.3s var(--ease);
}

.bars--on i {
  background: var(--amber);
  animation: eq 1s var(--ease) infinite;
  animation-delay: calc(var(--i) * -0.055s);
}

@keyframes eq {
  0%,
  100% {
    transform: scaleY(0.4);
  }
  50% {
    transform: scaleY(1);
  }
}
</style>
