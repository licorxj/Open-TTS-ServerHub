<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useHubStore } from '../stores/hub'
import { ElMessage } from 'element-plus'
import { managerCall } from '../api'

const hub = useHubStore()
const status = ref(null)
const logText = ref('')
const busy = ref(false)
const autoLog = ref(true)
const errMsg = ref('')

const managerPort = computed(() => Number(hub.status?.manager?.port) || 5290)
const mgrUrl = computed(() => `http://${window.location.hostname}:${managerPort.value}/`)
const b = computed(() => status.value?.backend || null)
const alive = computed(() => !!b.value?.alive)
const portOpen = computed(() => !!b.value?.port_open)

async function loadStatus() {
  try {
    status.value = await managerCall(managerPort.value, '/api/manager/status')
    errMsg.value = ''
  } catch (e) {
    errMsg.value = '管理器未连接：' + (e.message || e)
  }
}

async function loadLog() {
  try {
    const r = await managerCall(managerPort.value, '/api/manager/backend/log?lines=200')
    logText.value = r.content || '(空)'
  } catch (e) {
    /* 管理器离线时不打断 */
  }
}

async function act(action) {
  if (busy.value) return
  busy.value = true
  try {
    await managerCall(managerPort.value, '/api/manager/backend/' + action, { method: 'POST' })
    const label = {
      start: '已发送「启动后端」指令',
      stop: '已发送「停止后端」指令',
      restart: '已发送「重启后端」指令',
    }[action]
    ElMessage.success(label)
    await loadStatus()
  } catch (e) {
    ElMessage.error('操作失败：' + (e.message || e))
  } finally {
    busy.value = false
  }
}

let t1 = null
let t2 = null
onMounted(() => {
  loadStatus()
  loadLog()
  t1 = setInterval(loadStatus, 3000)
  t2 = setInterval(() => {
    if (autoLog.value) loadLog()
  }, 3000)
})
onUnmounted(() => {
  clearInterval(t1)
  clearInterval(t2)
})
</script>

<template>
  <div class="mgr">
    <header class="mgr__head">
      <div>
        <h2>后端管理 · 统一管理器</h2>
        <p class="sub">
          管理器监听端口 {{ managerPort }} · 后端(hub) {{ b?.port }} · 端口守护会在崩溃 / 端口掉线时自动重启
        </p>
      </div>
      <div class="state" :class="errMsg ? 'err' : alive ? 'ok' : 'off'">
        {{
          errMsg
            ? '管理器离线'
            : alive
            ? b?.want_up
              ? '后端运行中'
              : '后端已停止'
            : '后端未运行'
        }}
      </div>
    </header>

    <div v-if="errMsg" class="banner err">{{ errMsg }}</div>

    <div v-else-if="status" class="grid">
      <div class="card">
        <div class="card__title">后端 (hub) 状态</div>
        <div class="kv">
          <span>运行状态</span>
          <b :style="{ color: alive ? 'var(--cyan)' : 'var(--red)' }">{{ alive ? '运行中' : '未运行' }}</b>
        </div>
        <div class="kv">
          <span>端口连通</span>
          <b :style="{ color: portOpen ? 'var(--cyan)' : 'var(--red)' }">{{ portOpen ? '✅' : '❌' }}</b>
        </div>
        <div class="kv"><span>守护意愿</span><span>{{ b?.want_up ? '应保持运行' : '已手动停止' }}</span></div>
        <div class="kv"><span>PID</span><span>{{ b?.pid ?? '-' }}</span></div>
        <div class="kv"><span>累计重启</span><span>{{ b?.restarts ?? 0 }}</span></div>
        <div class="kv"><span>运行时长</span><span>{{ b?.uptime ?? 0 }} s</span></div>
        <div class="kv"><span>最近错误</span><span class="err-txt">{{ b?.last_error || '-' }}</span></div>
        <div class="actions">
          <button class="btn go" :disabled="busy" @click="act('start')">▶ 启动</button>
          <button class="btn warn" :disabled="busy" @click="act('stop')">■ 停止</button>
          <button class="btn" :disabled="busy" @click="act('restart')">⟳ 重启</button>
        </div>
        <p class="tip">停止后管理器不会自动拉起；只有崩溃（进程退出 / 端口掉线）才会被端口守护自动重启。</p>
      </div>

      <div class="card">
        <div class="card__title">管理器信息</div>
        <div class="kv"><span>管理器 PID</span><span>{{ status.manager?.pid ?? '-' }}</span></div>
        <div class="kv"><span>管理器端口</span><span>{{ status.manager?.port }}</span></div>
        <div class="kv"><span>守护间隔</span><span>{{ status.guard_interval }} s</span></div>
        <div class="kv">
          <span>前端</span>
          <span>{{ status.frontend ? (status.frontend.alive ? '运行中' : '未运行') : '未独立配置(用 /ui)' }}</span>
        </div>
        <div class="kv">
          <span>独立控制页</span>
          <span><a :href="mgrUrl" target="_blank">打开</a></span>
        </div>
      </div>
    </div>

    <div class="card log">
      <div class="card__title">
        后端日志 (logs/manager/backend.log)
        <label class="auto"><input type="checkbox" v-model="autoLog" /> 自动刷新</label>
        <button class="btn sm" @click="loadLog">刷新</button>
      </div>
      <pre>{{ logText }}</pre>
    </div>
  </div>
</template>

<style scoped>
.mgr {
  padding: 18px 22px;
  max-width: 1100px;
}
.mgr__head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
}
.mgr__head h2 {
  margin: 0;
  font-size: 18px;
}
.sub {
  margin: 4px 0 0;
  color: var(--text-dim);
  font-size: 12.5px;
}
.state {
  padding: 6px 14px;
  border-radius: 999px;
  font-size: 13px;
  border: 1px solid var(--line);
}
.state.ok {
  color: var(--cyan);
  border-color: var(--cyan);
}
.state.off {
  color: var(--amber);
  border-color: var(--amber);
}
.state.err {
  color: var(--red);
  border-color: var(--red);
}
.banner {
  padding: 12px;
  border-radius: 8px;
  margin-bottom: 14px;
}
.banner.err {
  background: rgba(255, 107, 107, 0.12);
  border: 1px solid var(--red);
  color: var(--red);
}
.grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}
@media (max-width: 760px) {
  .grid {
    grid-template-columns: 1fr;
  }
}
.card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 16px;
}
.card__title {
  font-size: 13px;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin-bottom: 10px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.kv {
  display: flex;
  justify-content: space-between;
  padding: 6px 0;
  border-bottom: 1px dashed var(--line);
  font-size: 13.5px;
}
.kv span:last-child {
  font-family: Consolas, monospace;
}
.err-txt {
  color: var(--red);
  font-size: 12px;
  max-width: 55%;
  text-align: right;
  word-break: break-all;
}
.actions {
  display: flex;
  gap: 8px;
  margin-top: 14px;
}
.btn {
  cursor: pointer;
  border: 1px solid var(--line);
  background: rgba(255, 255, 255, 0.03);
  color: var(--text);
  border-radius: 8px;
  padding: 8px 14px;
  font-size: 13px;
  transition: 0.15s;
}
.btn:hover:not(:disabled) {
  border-color: var(--amber);
}
.btn.go {
  color: var(--cyan);
  border-color: var(--cyan);
}
.btn.warn {
  color: var(--amber);
  border-color: var(--amber);
}
.btn.sm {
  padding: 4px 10px;
  font-size: 12px;
}
.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.tip {
  margin: 10px 0 0;
  font-size: 11.5px;
  color: var(--text-dim);
}
.auto {
  font-size: 12px;
  color: var(--text-dim);
  margin-left: auto;
}

.log {
  margin-top: 14px;
}
.log pre {
  background: #0b0d12;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 12px;
  max-height: 340px;
  overflow: auto;
  font-size: 12px;
  color: #b8e0c8;
  white-space: pre-wrap;
  margin: 0;
}
a {
  color: var(--cyan);
}
</style>
