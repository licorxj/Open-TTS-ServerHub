<script setup>
import { computed } from 'vue'

const props = defineProps({
  field: { type: String, required: true },
  schema: { type: Object, default: () => ({}) },
  modelValue: { type: [String, Number, Boolean, null], default: '' },
})
const emit = defineEmits(['update:modelValue'])

const type = computed(() => (props.schema.type || 'string').toLowerCase())
const required = computed(() => !!props.schema.required)
const hasEnum = computed(() => Array.isArray(props.schema.enum) && props.schema.enum.length > 0)
const range = computed(() =>
  Array.isArray(props.schema.range) && props.schema.range.length === 2 ? props.schema.range : null,
)

// 长文本字段用多行输入
const multiline = computed(
  () => type.value === 'string' && /(^text$|_text$|prompt_text$)/.test(props.field),
)

// 只提示"默认值 / 请输入"，不重复 desc（desc 由字段下方单独展示，避免同句话出现两次）
const placeholder = computed(() => {
  const d = props.schema.default
  if (d !== undefined && d !== null && d !== '') return `默认 ${d}`
  if (required.value) return `请输入 ${props.field}`
  return '留空则用配置默认值'
})

function set(v) {
  emit('update:modelValue', v === '' || v === null ? '' : v)
}

const numVal = computed({
  get: () => (props.modelValue === '' || props.modelValue === null ? undefined : Number(props.modelValue)),
  set: (v) => set(v === undefined || v === null || Number.isNaN(v) ? '' : v),
})
</script>

<template>
  <!-- 枚举 -->
  <el-select
    v-if="hasEnum"
    :model-value="modelValue === '' ? undefined : modelValue"
    :placeholder="placeholder"
    clearable
    size="small"
    style="width: 100%"
    @update:model-value="set"
  >
    <el-option v-for="o in schema.enum" :key="o" :value="o" :label="String(o)" />
  </el-select>

  <!-- 数值（带范围：滑块 + 输入框） -->
  <div v-else-if="type === 'number' || type === 'integer'" class="numfield">
    <el-slider
      v-if="range"
      :model-value="numVal ?? schema.default ?? range[0]"
      :min="range[0]"
      :max="range[1]"
      :step="type === 'integer' ? 1 : 0.05"
      size="small"
      style="flex: 1"
      @update:model-value="set"
    />
    <el-input-number
      :model-value="numVal"
      :min="range ? range[0] : undefined"
      :max="range ? range[1] : undefined"
      :step="type === 'integer' ? 1 : 0.1"
      :controls="!range"
      size="small"
      :style="{ width: range ? '92px' : '100%' }"
      :placeholder="placeholder"
      @update:model-value="set"
    />
  </div>

  <!-- 布尔 -->
  <el-switch
    v-else-if="type === 'boolean'"
    :model-value="modelValue === true"
    size="small"
    @update:model-value="set"
  />

  <!-- 长文本 -->
  <el-input
    v-else-if="multiline"
    :model-value="String(modelValue ?? '')"
    type="textarea"
    :rows="3"
    resize="vertical"
    size="small"
    :placeholder="placeholder"
    @update:model-value="set"
  />

  <!-- 字符串 -->
  <el-input
    v-else
    :model-value="String(modelValue ?? '')"
    size="small"
    clearable
    :placeholder="placeholder"
    @update:model-value="set"
  />
</template>

<style scoped>
.numfield {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
}
</style>
