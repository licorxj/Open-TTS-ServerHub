<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useHubStore } from '../stores/hub'
import { api } from '../api'
import { ElMessage } from 'element-plus'

const hub = useHubStore()

const items = ref([])
const stats = ref(null)
const loading = ref(false)
const auto = ref(true)
const kind = ref('')
const liveOnly = ref(false)

const KIND_TEXT = {
  synth: '合成',
  passthrough: '透传',
  lifecycle: '生命周期',
  config: '配置',
  query: '查询',
  panel: '面板',
  other: '其它',
}

const KIND_TYPE = {
  synth: 'warning',
  passthrough: 'warning',
  lifecycle: 'primary',
  config: 'info',
  query: 'info',
  other: '',
}

const shown = computed(() => (liveOnly.value ? items.value.filter((r) => r.inflight) : items.value))

async function load() {
  loading.value = true
  try {
    const [r, s] = await Promise.all([
      api.requests({ limit: 300, kind: kind.value || undefined }),
      api.journalStats(),
    ])
    items.value = r.items || []
    stats.value = s
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

async function clearAll() {
  try {
    const r = await api.clearRequests()
    ElMessage.success(`已清空 ${r.cleared} 条记录`)
    load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

let timer = null
onMounted(async () => {
  await hub.refreshEngines()
  await load()
  timer = setInterval(() => auto.value && load(), 5000)
})
onBeforeUnmount(() => timer && clearInterval(timer))

function statusClass(code) {
  if (!code) return ''
  if (code >= 500) return 'st--err'
  if (code >= 400) return 'st--warn'
  if (code >= 200 && code < 300) return 'st--ok'
  return ''
}

function engineLabel(name) {
  if (!name) return '—'
  return hub.engineByName[name]?.display_name || name
}

function fmtDur(ms) {
  if (!ms) return '—'
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${Math.round(ms)}ms`
}
</script>

<template>
  <div>
    <div class="ctrl panel">
      <span class="hud">REQUEST LOG</span>
      <span class="mono-sm">
        经过管家的每一次调用（含外部程序直连），最近 {{ stats?.requests_kept ?? 0 }} 条 ·
        台账任务 {{ stats?.tasks_kept ?? 0 }} 个
      </span>

      <div class="spacer"></div>

      <div class="row" style="gap: 8px">
        <el-select v-model="kind" size="small" style="width: 120px" clearable placeholder="全部类型" @change="load">
          <el-option value="synth" label="合成" />
          <el-option value="passthrough" label="透传" />
          <el-option value="lifecycle" label="生命周期" />
          <el-option value="config" label="配置" />
          <el-option value="query" label="查询" />
          <el-option value="other" label="其它" />
        </el-select>
        <el-checkbox v-model="liveOnly" size="small">只看在途</el-checkbox>
        <el-checkbox v-model="auto" size="small">自动刷新</el-checkbox>
        <button class="btn" :disabled="loading" @click="load">刷新</button>
        <button class="btn btn--danger" @click="clearAll">清空</button>
      </div>
    </div>

    <div class="panel">
      <el-table
        :data="shown"
        size="small"
        height="calc(100vh - 250px)"
        empty-text="暂无记录 —— 发起一次合成或从外部程序调用 /api/tts 即可看到"
      >
        <el-table-column label="时间" width="96">
          <template #default="{ row }"><span class="num mono-sm">{{ row.clock }}</span></template>
        </el-table-column>

        <el-table-column label="来源" width="120">
          <template #default="{ row }">
            <span class="mono-sm">{{ row.client }}</span>
          </template>
        </el-table-column>

        <el-table-column label="类型" width="92">
          <template #default="{ row }">
            <el-tag :type="KIND_TYPE[row.kind] || 'info'" size="small" effect="plain">
              {{ KIND_TEXT[row.kind] || row.kind }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="请求" min-width="260">
          <template #default="{ row }">
            <span class="method" :class="`method--${row.method.toLowerCase()}`">{{ row.method }}</span>
            <code class="path">{{ row.path }}</code>
            <span v-if="row.query" class="query">?{{ row.query }}</span>
          </template>
        </el-table-column>

        <el-table-column label="引擎" width="130">
          <template #default="{ row }">
            <span v-if="row.engine" class="eng">{{ engineLabel(row.engine) }}</span>
            <span v-else class="mono-sm">—</span>
          </template>
        </el-table-column>

        <el-table-column label="目标" min-width="170">
          <template #default="{ row }">
            <code v-if="row.endpoint" class="path">{{ row.endpoint }}</code>
            <span v-else class="mono-sm">—</span>
          </template>
        </el-table-column>

        <el-table-column label="task_id" width="130">
          <template #default="{ row }">
            <code v-if="row.task_id" class="tid" :title="row.task_id">{{ row.task_id.slice(0, 12) }}…</code>
            <span v-else class="mono-sm">—</span>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="80" align="right">
          <template #default="{ row }">
            <span v-if="row.inflight" class="st st--live">进行中</span>
            <span v-else class="st num" :class="statusClass(row.status)">{{ row.status || '—' }}</span>
          </template>
        </el-table-column>

        <el-table-column label="耗时" width="86" align="right">
          <template #default="{ row }">
            <span class="num mono-sm">{{ fmtDur(row.duration_ms) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="备注" min-width="150">
          <template #default="{ row }">
            <span v-if="row.error" class="err" :title="row.error">{{ row.error.slice(0, 60) }}</span>
            <span v-else class="mono-sm">—</span>
          </template>
        </el-table-column>
      </el-table>
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
  flex-wrap: wrap;
}

.method {
  display: inline-block;
  min-width: 46px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.04em;
}

.method--get {
  color: var(--cyan);
}

.method--post {
  color: var(--amber);
}

.method--put,
.method--patch {
  color: var(--violet);
}

.method--delete {
  color: var(--red);
}

.path {
  color: var(--text);
  font-size: 12px;
}

.query {
  color: var(--text-mute);
  font-size: 11px;
  word-break: break-all;
}

.eng {
  color: var(--amber-2);
  font-size: 12.5px;
}

.tid {
  color: var(--cyan);
  font-size: 11.5px;
}

.st {
  font-size: 12.5px;
  font-weight: 700;
}

.st--ok {
  color: var(--green);
}

.st--warn {
  color: var(--amber);
}

.st--err {
  color: var(--red);
}

.st--live {
  color: var(--cyan);
  animation: pulse 1.2s var(--ease) infinite;
}

.err {
  color: #ffb0b0;
  font-size: 11.5px;
}
</style>
