<script setup>
import { computed } from 'vue'
import { useHubStore } from '../stores/hub'
import { ElMessage } from 'element-plus'
import StateDot from './StateDot.vue'

const props = defineProps({
  engine: { type: Object, required: true },
  index: { type: Number, default: 0 },
})
const emit = defineEmits(['log'])

const hub = useHubStore()

const st = computed(() => hub.activeMap[props.engine.name] || null)
const state = computed(() => st.value?.state || 'stopped')
const isActive = computed(() => !!st.value)
const isCurrent = computed(() => hub.current === props.engine.name)

function fmt(sec) {
  if (!sec && sec !== 0) return '—'
  const s = Math.floor(sec)
  const m = Math.floor(s / 60)
  return m > 0 ? `${m}m${s % 60}s` : `${s}s`
}

function fmtLeft(sec) {
  const s = Math.max(0, Math.round(sec))
  const m = Math.floor(s / 60)
  return m > 0 ? `${m}m${String(s % 60).padStart(2, '0')}s` : `${s}s`
}

async function onSwitch() {
  try {
    await hub.switchTo(props.engine.name)
    ElMessage.success(`${props.engine.display_name} 已就绪`)
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function onStop() {
  try {
    await hub.stop(props.engine.name)
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function onRestart() {
  try {
    await hub.restart(props.engine.name)
  } catch (e) {
    ElMessage.error(e.message)
  }
}
</script>

<template>
  <div
    class="card panel"
    :class="{ 'card--active': isActive, 'card--current': isCurrent, 'card--off': !engine.enabled }"
    :style="{ animationDelay: index * 45 + 'ms' }"
  >
    <span class="card__flag" v-if="isActive">ACTIVE</span>

    <div class="card__hd">
      <StateDot :state="state" />
      <span class="card__name">{{ engine.display_name }}</span>
      <span class="card__port num">:{{ engine.port }}</span>
    </div>

    <div class="card__alias">
      <code>{{ engine.name }}</code>
      <span v-for="a in engine.aliases.slice(0, 3)" :key="a" class="chip">{{ a }}</span>
    </div>

    <p class="card__desc">{{ engine.description || '—' }}</p>

    <div class="card__stats">
      <span>{{ { ready: '就绪', starting: '拉起中', failed: '失败', stopped: '未启动' }[state] }}</span>
      <span v-if="st">PID {{ st.pid || '外部' }}</span>
      <span v-if="st">UP {{ fmt(st.uptime_seconds) }}</span>
      <span v-if="st && st.inflight > 0" class="card__inflight">在途 {{ st.inflight }}</span>
      <span
        v-else-if="st && st.idle_expires_in !== null && st.idle_expires_in !== undefined"
        class="card__idle"
        :class="{ 'card__idle--warn': st.idle_expires_in <= 60 }"
        title="空闲超过设定时长后管家会自动卸载该引擎并释放显存"
      >闲置回收 {{ fmtLeft(st.idle_expires_in) }}</span>
      <span v-else class="mono-sm">{{ Object.keys(engine.endpoints || {}).length }} 端点</span>
    </div>

    <div class="card__ops">
      <button class="btn btn--sm btn--primary" :disabled="hub.busy" @click="onSwitch">
        {{ isActive ? '设为当前' : '切换并拉起' }}
      </button>
      <div class="spacer"></div>
      <button class="btn btn--sm btn--ghost" title="查看日志" :disabled="hub.busy" @click="emit('log', engine.name)">
        日志
      </button>
      <button class="btn btn--sm btn--ghost" v-if="isActive" :disabled="hub.busy" @click="onRestart">重启</button>
      <button class="btn btn--sm btn--danger" v-if="isActive" :disabled="hub.busy" @click="onStop">停止</button>
    </div>
  </div>
</template>

<style scoped>
.card {
  position: relative;
  padding: 16px 16px 14px;
  overflow: hidden;
  transition: border-color 0.2s var(--ease), transform 0.2s var(--ease), background 0.2s var(--ease);
  animation: rise 0.45s var(--ease) both;
}

.card::after {
  content: '';
  position: absolute;
  inset: 0;
  pointer-events: none;
  background: linear-gradient(135deg, rgba(255, 164, 43, 0.05), transparent 40%);
  opacity: 0;
  transition: opacity 0.2s var(--ease);
}

.card:hover {
  border-color: var(--line-3);
  transform: translateY(-2px);
}

.card:hover::after {
  opacity: 1;
}

.card--current {
  border-color: var(--amber-line);
}

.card--active {
  background: linear-gradient(180deg, rgba(63, 216, 192, 0.05), transparent 60%), var(--panel);
  border-color: rgba(63, 216, 192, 0.34);
}

.card--off {
  opacity: 0.42;
}

.card__flag {
  position: absolute;
  top: 0;
  right: 0;
  padding: 2px 8px 2px 10px;
  font-size: 9px;
  letter-spacing: 0.16em;
  color: #06251f;
  background: var(--cyan);
  clip-path: polygon(10px 0, 100% 0, 100% 100%, 0 100%);
  font-weight: 700;
}

.card__hd {
  display: flex;
  align-items: center;
  gap: 8px;
}

.card__name {
  font-family: var(--font-display);
  font-size: 15.5px;
  font-weight: 600;
  letter-spacing: 0.03em;
}

.card__port {
  margin-left: auto;
  font-size: 12px;
  color: var(--text-dim);
}

.card__alias {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin: 8px 0 10px;
}

.card__alias code {
  font-size: 12px;
  color: var(--amber);
  background: var(--amber-soft);
  padding: 1px 6px;
  border-radius: 3px;
}

.chip {
  font-size: 10.5px;
  color: var(--text-dim);
  border: 1px solid var(--line-2);
  padding: 1px 6px;
  border-radius: 3px;
}

.card__desc {
  margin: 0 0 12px;
  font-size: 12.5px;
  color: var(--text-dim);
  line-height: 1.62;
  min-height: 40px;
}

.card__stats {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 11.5px;
  color: var(--text-dim);
  padding-top: 11px;
  border-top: 1px dashed var(--line-2);
}

.card__inflight {
  color: var(--amber);
}

.card__idle {
  color: var(--cyan);
}

.card__idle--warn {
  color: var(--amber);
}

.card__ops {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-top: 12px;
}
</style>
