<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { useHubStore } from '../stores/hub'
import EngineCard from '../components/EngineCard.vue'
import LogDrawer from '../components/LogDrawer.vue'
import StateDot from '../components/StateDot.vue'
import { api } from '../api'
import { ElMessage } from 'element-plus'

const hub = useHubStore()
const logOpen = ref(false)
const logEngine = ref('')

const active = computed(() => hub.status.active || [])
const currentDetail = computed(() => hub.activeMap[hub.current] || null)

/* ---------------- 概览区可交互配置（改动即时下发并生效） ---------------- */
const maxActive = ref(1)
const keepAlive = ref(false)
const ttlMinutes = ref(5)
const savingCfg = ref(false)

const TTL_OPTIONS = [1, 2, 3, 5, 10, 15, 30, 60]

// 把服务端状态同步到本地草稿；下发过程中不同步，避免请求飞行中被旧值回灌
watch(
  () => hub.status.max_active,
  (v) => {
    if (savingCfg.value || v === undefined || v === null) return
    if (Number(v) !== maxActive.value) maxActive.value = Number(v)
  },
  { immediate: true },
)

watch(
  () => hub.status.idle_ttl_seconds,
  (v) => {
    if (savingCfg.value || v === undefined || v === null) return
    const t = Number(v)
    keepAlive.value = t <= 0
    if (t > 0) {
      const m = Math.max(1, Math.round(t / 60))
      if (m !== ttlMinutes.value && TTL_OPTIONS.includes(m)) ttlMinutes.value = m
    }
  },
  { immediate: true },
)

async function applyHubConfig(patch, okText) {
  savingCfg.value = true
  try {
    await api.putHubConfig(patch)
    await hub.refreshStatus() // 立即回读，保证界面与服务端一致
    ElMessage.success(okText)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    savingCfg.value = false
  }
}

function onMaxActive(v) {
  applyHubConfig({ max_active: Number(v) }, `同时常驻上限已设为 ${v}，立即生效`)
}

function onKeepAlive(on) {
  if (on) {
    applyHubConfig({ idle_ttl_seconds: 0 }, '已设为常驻：空闲不再自动卸载')
  } else {
    applyHubConfig(
      { idle_ttl_seconds: ttlMinutes.value * 60 },
      `已开启空闲回收：空闲 ${ttlMinutes.value} 分钟后卸载引擎`,
    )
  }
}

function onTtlMinutes(v) {
  if (keepAlive.value || savingCfg.value) return
  applyHubConfig({ idle_ttl_seconds: Number(v) * 60 }, `空闲回收时长已设为 ${v} 分钟`)
}

const ttlText = computed(() => (keepAlive.value ? '常驻' : `${ttlMinutes.value} 分钟`))

const idleHint = computed(() => {
  if (keepAlive.value) return '空闲不自动卸载'
  const a = active.value.find((x) => x.idle_expires_in !== null && x.idle_expires_in !== undefined)
  return a ? `最近 ${fmtLeft(a.idle_expires_in)} 后释放显存` : '空闲后自动卸载'
})

function fmtLeft(sec) {
  const s = Math.max(0, Math.round(sec))
  const m = Math.floor(s / 60)
  return m > 0 ? `${m}m${String(s % 60).padStart(2, '0')}s` : `${s}s`
}

onMounted(() => hub.refreshAll())

function openLog(name) {
  logEngine.value = name
  logOpen.value = true
}
</script>

<template>
  <div>
    <!-- 概览 -->
    <div class="stats">
      <div class="stat panel">
        <span class="hud">REGISTERED</span>
        <b class="num">{{ hub.engines.length }}</b>
        <span class="mono-sm">已注册引擎</span>
      </div>
      <div class="stat panel">
        <div class="stat__top">
          <span class="hud">LOADED</span>
          <el-select
            v-model="maxActive"
            size="small"
            style="width: 72px"
            :disabled="savingCfg"
            @change="onMaxActive"
          >
            <el-option v-for="n in 4" :key="n" :value="n" :label="`${n} 个`" />
          </el-select>
        </div>
        <b class="num" :style="{ color: active.length ? 'var(--cyan)' : 'var(--text-mute)' }">
          {{ active.length }} / {{ hub.status.max_active }}
        </b>
        <span class="mono-sm">同时常驻引擎数上限（1~4）</span>
      </div>

      <div class="stat panel">
        <div class="stat__top">
          <span class="hud">IDLE TTL</span>
          <div class="row" style="gap: 7px">
            <span class="mono-sm">{{ keepAlive ? '常驻' : '自动卸载' }}</span>
            <el-switch v-model="keepAlive" size="small" :disabled="savingCfg" @change="onKeepAlive" />
          </div>
        </div>
        <b class="num" :style="{ color: keepAlive ? 'var(--text-mute)' : 'var(--amber)' }">
          {{ ttlText }}
        </b>
        <div class="stat__bottom">
          <el-select
            v-if="!keepAlive"
            v-model="ttlMinutes"
            size="small"
            style="width: 104px"
            :disabled="savingCfg"
            @change="onTtlMinutes"
          >
            <el-option v-for="m in TTL_OPTIONS" :key="m" :value="m" :label="`${m} 分钟`" />
          </el-select>
          <span class="mono-sm">{{ idleHint }}</span>
        </div>
      </div>
      <div class="stat panel">
        <span class="hud">SIGNAL</span>
        <b class="num" :style="{ color: hub.online ? 'var(--green)' : 'var(--red)' }">
          {{ hub.online ? 'ONLINE' : 'OFFLINE' }}
        </b>
        <span class="mono-sm">管家链路</span>
      </div>
    </div>

    <div class="grid2">
      <!-- 引擎卡片墙 -->
      <section>
        <div class="sec__hd">
          <span class="hud hud--amber">ENGINE ARRAY</span>
          <span class="mono-sm">点击「切换并拉起」→ 自动卸载当前引擎、拉起目标引擎</span>
        </div>

        <div class="cards">
          <EngineCard
            v-for="(e, i) in hub.engines"
            :key="e.name"
            :engine="e"
            :index="i"
            @log="openLog"
          />
        </div>
      </section>

      <!-- 当前引擎详情 -->
      <aside>
        <div class="panel panel--notch">
          <div class="panel__hd">
            <span class="panel__title">当前引擎</span>
            <StateDot :state="currentDetail?.state || 'stopped'" />
          </div>
          <div class="panel__body">
            <template v-if="hub.currentEngine">
              <div class="cur__name">{{ hub.currentEngine.display_name }}</div>
              <div class="cur__meta">
                <span>key <code>{{ hub.currentEngine.name }}</code></span>
                <span>端口 <code>:{{ hub.currentEngine.port }}</code></span>
                <span>状态 <code>{{ currentDetail?.state || 'stopped' }}</code></span>
              </div>

              <div class="hud" style="margin: 14px 0 6px">ENDPOINTS</div>
              <div class="eps">
                <div v-for="(p, k) in hub.currentEngine.endpoints" :key="k" class="ep">
                  <span class="ep__k">{{ k }}</span>
                  <code class="ep__v">{{ p }}</code>
                </div>
              </div>

              <div class="hud" style="margin: 14px 0 6px">DEFAULTS</div>
              <pre class="kv">{{ JSON.stringify(hub.currentEngine.defaults || {}, null, 2) }}</pre>

              <div class="row" style="margin-top: 14px">
                <a
                  class="btn btn--sm btn--ghost"
                  :href="`http://127.0.0.1:${hub.currentEngine.port}/docs`"
                  target="_blank"
                >Swagger :{{ hub.currentEngine.port }}</a>
                <button class="btn btn--sm btn--ghost" @click="openLog(hub.currentEngine.name)">日志</button>
              </div>
            </template>
            <div v-else class="mono-sm">尚未选择引擎</div>
          </div>
        </div>

        <div class="panel" style="margin-top: 14px">
          <div class="panel__hd"><span class="panel__title">运行提示</span></div>
          <div class="panel__body tips">
            <p>· 管家自己不加载模型，内存占用极低，可长期常驻。</p>
            <p>· 切换模型 = 杀掉旧引擎进程树并释放显存，再拉起新引擎；首次含模型下载，超时默认 900s。</p>
            <p>
              · 空闲超过 <b style="color: var(--amber)">{{ ttlText }}</b> 无请求会自动卸载引擎、释放显存
              （可在「配置编辑器」调整，设为 0 即常驻不回收）。
            </p>
            <p>· 拉起过程可点「日志」实时查看进度。</p>
            <p>· 「Swagger」按钮直连该引擎端口，打开它自己的接口文档。</p>
          </div>
        </div>
      </aside>
    </div>

    <LogDrawer v-model="logOpen" :engine="logEngine" />
  </div>
</template>

<style scoped>
.stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-bottom: 18px;
}

.stat {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 13px 16px;
}

/* 概览卡内嵌控件行 */
.stat__top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-height: 26px;
}

.stat__bottom {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 2px;
}

.stat b {
  font-family: var(--font-display);
  font-size: 22px;
  line-height: 1.25;
}

.sec__hd {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 12px;
}

.grid2 {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 320px;
  gap: 16px;
  align-items: start;
}

.cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(258px, 1fr));
  gap: 12px;
}

.cur__name {
  font-family: var(--font-display);
  font-size: 17px;
  font-weight: 600;
  color: var(--amber-2);
}

.cur__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 6px;
  font-size: 12px;
  color: var(--text-dim);
}

.cur__meta code {
  color: var(--text);
}

.eps {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.ep {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
}

.ep__k {
  width: 92px;
  flex: none;
  color: var(--text-dim);
}

.ep__v {
  color: var(--cyan);
  word-break: break-all;
}

.kv {
  margin: 0;
  padding: 10px 12px;
  background: var(--bg-deep);
  border: 1px solid var(--line);
  font-size: 12px;
  color: var(--text-dim);
  max-height: 200px;
  overflow: auto;
  line-height: 1.6;
}

.tips {
  font-size: 12.5px;
  color: var(--text-dim);
  line-height: 1.85;
}

.tips p {
  margin: 0 0 8px;
}

@media (max-width: 1180px) {
  .grid2 {
    grid-template-columns: 1fr;
  }
  .stats {
    grid-template-columns: repeat(2, 1fr);
  }
}
</style>
