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

function engineOf(name) {
  return hub.engineByName[name] || null
}

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

let timer = null
onMounted(async () => {
  await hub.refreshEngines()
  newTask.value.engine = hub.current
  if (rows.value.some((t) => t.state === 'running' || t.state === 'pending')) refreshAll()
  timer = setInterval(() => {
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
function rtfClass(v) {
  const n = Number(v)
  if (n <= 0) return ''
  if (n < 1) return 'rtf--fast'
  if (n < 2) return 'rtf--mid'
  return 'rtf--slow'
}

function rtfTitle(t) {
  const parts = []
  if (t.audioDuration) parts.push(`音频时长 ${t.audioDuration.toFixed(2)}s`)
  if (t.inferenceTime) parts.push(`推理耗时 ${t.inferenceTime.toFixed(2)}s`)
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
        任务型引擎（voxcpm / omnivoice / dots / confucius4 / indextts2）返回 task_id，在此轮询与下载
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
        <el-table-column prop="engine" label="引擎" width="130">
          <template #default="{ row }">
            <span class="eng">{{ engineOf(row.engine)?.display_name || row.engine }}</span>
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
            <span v-if="row.rtf" class="rtf" :class="rtfClass(row.rtf)" :title="rtfTitle(row)">
              {{ row.rtf.toFixed(3) }}
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
