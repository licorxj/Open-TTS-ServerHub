<script setup>
import { ref, reactive, computed, watch, onMounted } from 'vue'
import { useHubStore } from '../stores/hub'
import { api } from '../api'
import DynamicField from '../components/DynamicField.vue'
import { ElMessage } from 'element-plus'

const hub = useHubStore()

const engineName = ref(hub.current || '')
const cfg = ref(null)
const params = ref({})
const draft = reactive({}) // 正在编辑的 defaults
const overrides = ref({})
const dirty = ref(false)
const saving = ref(false)

const hubCfg = reactive({})
const hubDraft = reactive({})
const hubDirty = ref(false)

// 与后端 registry.MUTABLE_HUB_KEYS 对齐
const HUB_KEYS = [
  'max_active',
  'idle_ttl_seconds',
  'idle_check_interval',
  'forward_timeout',
  'unload_on_switch',
  'stop_engines_on_exit',
  'adopt_existing',
  'allow_shutdown_api',
]

const engine = computed(() => hub.engineByName[engineName.value] || null)

/** 参数表里尚未进入 defaults、且可填的键 */
const addable = computed(() =>
  Object.keys(params.value).filter(
    (k) => params.value[k]?.type !== 'file' && !(k in draft),
  ),
)

async function load() {
  if (!engineName.value) return
  try {
    const [c, p] = await Promise.all([api.getConfig(engineName.value), api.params(engineName.value)])
    cfg.value = c
    params.value = p.params || {}
    overrides.value = c.overrides || {}
    Object.keys(draft).forEach((k) => delete draft[k])
    Object.entries(c.current.defaults || {}).forEach(([k, v]) => (draft[k] = v))
    dirty.value = false
  } catch (e) {
    ElMessage.error(e.message)
  }
}

onMounted(async () => {
  await hub.refreshEngines()
  if (!engineName.value) engineName.value = hub.current
  await load()
  try {
    const h = await api.getHubConfig()
    const cur = h.current || {}
    Object.entries(cur).forEach(([k, v]) => (hubCfg[k] = v))
    // 只把「允许修改」的键放进草稿，否则 PUT 时会把 port/host 一并提交而被后端拒绝
    HUB_KEYS.forEach((k) => {
      if (k in cur) hubDraft[k] = cur[k]
    })
  } catch {
    /* ignore */
  }
})

watch(engineName, load)

function onEdit() {
  dirty.value = true
}

const addKeyName = ref(null)

function addKey(k) {
  if (!k) return
  const sc = params.value[k] || {}
  draft[k] = sc.default ?? (sc.type === 'boolean' ? false : sc.type === 'number' || sc.type === 'integer' ? 0 : '')
  addKeyName.value = null
  dirty.value = true
}

function delKey(k) {
  delete draft[k]
  dirty.value = true
}

async function save() {
  saving.value = true
  try {
    const clean = {}
    Object.entries(draft).forEach(([k, v]) => {
      if (v !== '' && v !== null && v !== undefined) clean[k] = v
    })
    await api.putConfig(engineName.value, { defaults: clean })
    ElMessage.success('已保存到 config/tts_hub.overrides.yaml')
    await load()
    await hub.refreshEngines()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

async function reset() {
  try {
    await api.resetConfig(engineName.value)
    ElMessage.success('已恢复默认')
    await load()
    await hub.refreshEngines()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function saveHub() {
  try {
    await api.putHubConfig({ ...hubDraft })
    ElMessage.success('管家配置已保存')
    hubDirty.value = false
    await hub.refreshStatus()
  } catch (e) {
    ElMessage.error(e.message)
  }
}
</script>

<template>
  <div>
    <div class="ctrl panel">
      <span class="hud">TARGET ENGINE</span>
      <el-select v-model="engineName" size="small" style="width: 240px" filterable>
        <el-option v-for="e in hub.engines" :key="e.name" :value="e.name" :label="e.display_name" />
      </el-select>
      <span class="mono-sm" v-if="engine">:{{ engine.port }} · body={{ engine.body_mode }}</span>
      <div class="spacer"></div>
      <span v-if="dirty" class="badge">未保存</span>
      <button class="btn" @click="reset">恢复默认</button>
      <button class="btn btn--primary" :disabled="saving" @click="save">保存配置</button>
    </div>

    <div class="cols">
      <!-- defaults 编辑 -->
      <section class="panel panel--notch">
        <div class="panel__hd">
          <span class="panel__title">默认参数 defaults</span>
          <span class="mono-sm">请求方未显式传值时，由管家自动注入</span>
        </div>
        <div class="panel__body">
          <div v-if="!Object.keys(draft).length" class="mono-sm" style="margin-bottom: 12px">
            该引擎当前没有默认参数，可从下方添加。
          </div>

          <div class="rows">
            <div v-for="(v, k) in draft" :key="k" class="row-item">
              <div class="row-item__hd">
                <span class="k">{{ k }}</span>
                <span class="t">{{ params[k]?.type || 'any' }}</span>
                <div class="spacer"></div>
                <button class="btn btn--sm btn--danger" @click="delKey(k)">移除</button>
              </div>
              <DynamicField
                :field="k"
                :schema="params[k] || { type: typeof v === 'boolean' ? 'boolean' : typeof v === 'number' ? 'number' : 'string' }"
                :model-value="v"
                @update:model-value="((val) => { draft[k] = val; onEdit() })"
              />
              <div class="d" v-if="params[k]?.desc">{{ params[k].desc }}</div>
            </div>
          </div>

          <div class="adder">
            <span class="hud">ADD PARAM</span>
            <el-select
              v-model="addKeyName"
              size="small"
              style="width: 240px"
              placeholder="从参数表添加…"
              clearable
              @change="addKey"
            >
              <el-option v-for="k in addable" :key="k" :value="k" :label="k" />
            </el-select>
          </div>
        </div>
      </section>

      <!-- 覆盖层 / 管家配置 -->
      <aside>
        <div class="panel">
          <div class="panel__hd"><span class="panel__title">运行期覆盖层</span></div>
          <div class="panel__body">
            <pre class="kv">{{ JSON.stringify(overrides, null, 2) }}</pre>
            <div class="mono-sm" style="margin-top: 8px">
              写入 <code>config/tts_hub.overrides.yaml</code>，主注册表
              <code>config/tts_hub.yaml</code> 不被改动。
            </div>
          </div>
        </div>

        <div class="panel" style="margin-top: 14px">
          <div class="panel__hd">
            <span class="panel__title">管家级配置</span>
            <button class="btn btn--sm btn--primary" @click="saveHub">保存</button>
          </div>
          <div class="panel__body hubcfg">
            <label class="hc">
              <span>max_active</span>
              <el-input-number v-model="hubDraft.max_active" :min="1" :max="4" size="small" />
              <em>同时常驻引擎数上限（1~4）；超上限时按最久未使用淘汰</em>
            </label>
            <label class="hc">
              <span>idle_ttl_seconds</span>
              <el-input-number v-model="hubDraft.idle_ttl_seconds" :min="0" :step="30" size="small" />
              <em>空闲自动卸载，默认 300 = 5 分钟（0=常驻不回收）</em>
            </label>
            <label class="hc">
              <span>idle_check_interval</span>
              <el-input-number
                v-model="hubDraft.idle_check_interval"
                :min="3"
                :max="600"
                :step="5"
                size="small"
              />
              <em>空闲巡检间隔（秒）</em>
            </label>
            <label class="hc">
              <span>forward_timeout</span>
              <el-input-number v-model="hubDraft.forward_timeout" :min="0" :step="60" size="small" />
              <em>转发超时（0=不限）</em>
            </label>
            <label class="hc hc--sw">
              <span>unload_on_switch</span>
              <el-switch v-model="hubDraft.unload_on_switch" size="small" />
              <em>每次切换都强制独占（无视 max_active &gt; 1，永远只留 1 个）</em>
            </label>
            <label class="hc hc--sw">
              <span>stop_engines_on_exit</span>
              <el-switch v-model="hubDraft.stop_engines_on_exit" size="small" />
              <em>管家退出时结束引擎</em>
            </label>
            <label class="hc hc--sw">
              <span>adopt_existing</span>
              <el-switch v-model="hubDraft.adopt_existing" size="small" />
              <em>接管端口上已有服务</em>
            </label>
            <label class="hc hc--sw">
              <span>allow_shutdown_api</span>
              <el-switch v-model="hubDraft.allow_shutdown_api" size="small" />
              <em>允许通过 API 关闭管家（桌面端「关闭」脚本依赖）</em>
            </label>
          </div>
        </div>
      </aside>
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

.badge {
  font-size: 11px;
  padding: 2px 8px;
  background: var(--amber);
  color: #1a1305;
  border-radius: 3px;
  letter-spacing: 0.08em;
  font-weight: 700;
}

.cols {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 400px;
  gap: 16px;
  align-items: start;
}

.rows {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px 18px;
}

.row-item {
  padding: 14px 15px;
  border: 1px solid var(--line);
  background: var(--panel-2);
  border-radius: var(--radius);
}

.row-item__hd {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.k {
  font-size: 13px;
  color: var(--amber-2);
  font-weight: 500;
}

.t {
  font-size: 10.5px;
  color: var(--text-mute);
  border: 1px solid var(--line-2);
  padding: 1px 5px;
  border-radius: 3px;
}

.d {
  margin-top: 8px;
  font-size: 11.5px;
  color: var(--text-dim);
  line-height: 1.6;
}

.adder {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 18px;
  padding-top: 16px;
  border-top: 1px dashed var(--line-2);
}

.kv {
  margin: 0;
  padding: 12px 14px;
  background: var(--bg-deep);
  border: 1px solid var(--line);
  font-size: 12px;
  color: var(--text-dim);
  max-height: 320px;
  overflow: auto;
  line-height: 1.65;
}

.kv code,
.mono-sm code {
  color: var(--amber);
}

.hubcfg {
  display: flex;
  flex-direction: column;
}

/* 两行布局：标签 + 控件同行，说明独占一行 —— 避免说明被挤成窄列折行 */
.hc {
  display: grid;
  grid-template-columns: 1fr auto;
  grid-template-areas:
    'name ctrl'
    'desc desc';
  align-items: center;
  gap: 4px 14px;
  padding: 11px 0;
  border-bottom: 1px dashed var(--line);
  font-size: 13px;
}

.hc:last-child {
  border-bottom: 0;
  padding-bottom: 0;
}

.hc > span {
  grid-area: name;
  color: var(--text);
  font-weight: 500;
}

.hc :deep(.el-input-number),
.hc :deep(.el-switch) {
  grid-area: ctrl;
}

.hc em {
  grid-area: desc;
  font-style: normal;
  font-size: 11.5px;
  color: var(--text-dim);
  line-height: 1.55;
}

@media (max-width: 1180px) {
  .cols {
    grid-template-columns: 1fr;
  }
}
</style>
