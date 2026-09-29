<script setup>
import { ref, watch, onBeforeUnmount } from 'vue'
import { api } from '../api'
import { ElMessage } from 'element-plus'

const props = defineProps({
  modelValue: Boolean,
  engine: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue'])

const content = ref('')
const lines = ref(200)
const auto = ref(true)
const loading = ref(false)
let timer = null

async function load() {
  if (!props.engine) return
  loading.value = true
  try {
    const r = await api.log(props.engine, lines.value)
    content.value = r.content || '(暂无日志)'
  } catch (e) {
    content.value = `读取失败: ${e.message}`
  } finally {
    loading.value = false
  }
}

const close = () => emit('update:modelValue', false)

function stopTimer() {
  if (timer) clearInterval(timer)
  timer = null
}

watch(
  () => [props.modelValue, props.engine],
  ([open]) => {
    stopTimer()
    if (!open) return
    load()
    if (auto.value) timer = setInterval(load, 2500)
  },
  { immediate: true },
)

watch(lines, () => props.modelValue && load())

watch(auto, (v) => {
  stopTimer()
  if (v && props.modelValue) timer = setInterval(load, 2500)
})

onBeforeUnmount(stopTimer)

async function copy() {
  try {
    await navigator.clipboard.writeText(content.value)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('复制失败，请手动选择')
  }
}
</script>

<template>
  <el-drawer
    :model-value="modelValue"
    :title="`引擎日志 · ${engine}`"
    size="58%"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="wrap">
      <div class="bar">
        <span class="hud">TAIL</span>
        <el-select v-model="lines" size="small" style="width: 96px">
          <el-option :value="100" label="100 行" />
          <el-option :value="200" label="200 行" />
          <el-option :value="500" label="500 行" />
          <el-option :value="2000" label="2000 行" />
        </el-select>
        <el-checkbox v-model="auto" size="small">自动刷新</el-checkbox>
        <div class="spacer"></div>
        <el-button size="small" @click="load">刷新</el-button>
        <el-button size="small" type="primary" plain @click="copy">复制</el-button>
      </div>

      <pre class="log">{{ content }}</pre>

      <div class="foot mono-sm">
        日志来源：<code>logs/tts_hub/{{ engine }}.log</code> · 模型下载与加载进度、报错堆栈都在这里
      </div>
    </div>
  </el-drawer>
</template>

<style scoped>
.wrap {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--line);
}

.log {
  flex: 1;
  margin: 0;
  padding: 16px;
  overflow: auto;
  background: var(--bg-deep);
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.75;
  white-space: pre-wrap;
  word-break: break-all;
}

.foot {
  padding: 8px 14px;
  border-top: 1px solid var(--line);
}

.foot code {
  color: var(--amber);
}
</style>
