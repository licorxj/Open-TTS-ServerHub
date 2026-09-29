<script setup>
import { computed } from 'vue'
import { useHubStore } from '../stores/hub'
import { ElMessage, ElMessageBox } from 'element-plus'
import WaveMark from './WaveMark.vue'
import StateDot from './StateDot.vue'

const hub = useHubStore()

const active = computed(() => hub.status.active || [])
const primary = computed(() => active.value[0] || null)

function fmt(sec) {
  if (!sec && sec !== 0) return '—'
  const s = Math.floor(sec)
  const m = Math.floor(s / 60)
  return m > 0 ? `${m}m ${s % 60}s` : `${s}s`
}

function fmtLeft(sec) {
  const s = Math.max(0, Math.round(sec))
  const m = Math.floor(s / 60)
  return m > 0 ? `${m}m${String(s % 60).padStart(2, '0')}s` : `${s}s`
}

async function onUnload() {
  try {
    await ElMessageBox.confirm('将结束当前所有引擎进程并释放显存，确定？', '卸载全部引擎', {
      type: 'warning',
      confirmButtonText: '卸载',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    await hub.unloadAll()
    ElMessage.success('已卸载全部引擎')
  } catch (e) {
    ElMessage.error(e.message)
  }
}
</script>

<template>
  <header class="topbar">
    <div class="brand">
      <WaveMark :size="24" />
      <div class="brand__txt">
        <div class="brand__name"><em>Lc</em>TTS&nbsp;管家</div>
        <div class="brand__sub">统一调度控制台</div>
      </div>
    </div>

    <div class="rule"></div>

    <!-- 当前活跃引擎 -->
    <div class="active">
      <template v-if="primary">
        <StateDot :state="primary.state" />
        <span class="hud">ACTIVE ENGINE</span>
        <span class="active__name">{{ primary.display_name }}</span>
        <span class="tag">:{{ primary.port }}</span>
        <span class="tag">PID {{ primary.pid || '外部' }}</span>
        <span class="tag">UP {{ fmt(primary.uptime_seconds) }}</span>
        <span class="tag" v-if="primary.inflight > 0">在途 {{ primary.inflight }}</span>
        <span
          class="tag"
          :class="{ 'tag--warn': primary.idle_expires_in != null && primary.idle_expires_in <= 60 }"
          v-if="primary.idle_expires_in != null"
          title="空闲超过该时长后管家会自动卸载引擎并释放显存"
        >闲置回收 {{ fmtLeft(primary.idle_expires_in) }}</span>
      </template>
      <template v-else>
        <i class="dot"></i>
        <span class="hud">NO ENGINE LOADED</span>
        <span class="mono-sm">显存空闲 · 发起合成或点击「切换」即按需拉起</span>
      </template>
    </div>

    <div class="spacer"></div>

    <div class="meta">
      <div class="meta__item">
        <span class="hud">ENGINES</span>
        <b class="num">{{ hub.engines.length }}</b>
      </div>
      <div class="meta__item">
        <span class="hud">LOADED</span>
        <b class="num" :style="{ color: primary ? 'var(--cyan)' : 'var(--text-mute)' }">
          {{ active.length }}/{{ hub.status.max_active }}
        </b>
      </div>
    </div>

    <button class="btn btn--danger" :disabled="!active.length || hub.busy" @click="onUnload">
      卸载全部
    </button>
  </header>
</template>

<style scoped>
.topbar {
  display: flex;
  align-items: center;
  gap: 16px;
  height: 54px;
  padding: 0 18px;
  border-bottom: 1px solid var(--line);
  background: linear-gradient(180deg, #0d1014, #08090b);
  position: relative;
}

.topbar::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  bottom: -1px;
  height: 1px;
  background: linear-gradient(90deg, var(--amber-line), transparent 42%);
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
}

.brand__name {
  font-family: var(--font-display);
  font-size: 16px;
  font-weight: 700;
  letter-spacing: 0.08em;
  line-height: 1.1;
}

.brand__name em {
  font-style: normal;
  color: var(--amber);
  letter-spacing: 0.02em;
}

.brand__sub {
  font-size: 10px;
  letter-spacing: 0.22em;
  color: var(--text-mute);
}

.rule {
  width: 1px;
  height: 26px;
  background: var(--line);
}

.active {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.tag {
  font-size: 11px;
  padding: 2px 7px;
  border: 1px solid var(--line-2);
  color: var(--text-dim);
  border-radius: 3px;
  white-space: nowrap;
}

.tag--warn {
  color: var(--amber-2);
  border-color: var(--amber-line);
  background: var(--amber-soft);
}

.meta {
  display: flex;
  gap: 18px;
}

.meta__item {
  display: flex;
  flex-direction: column;
  line-height: 1.25;
}

.meta__item b {
  font-size: 13px;
}

.active__name {
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 600;
  color: var(--cyan);
}
</style>
