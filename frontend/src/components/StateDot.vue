<script setup>
const props = defineProps({
  state: { type: String, default: 'stopped' },
  label: { type: Boolean, default: false },
})

const TEXT = {
  ready: '就绪',
  starting: '拉起中',
  failed: '失败',
  stopped: '未启动',
}
</script>

<template>
  <span class="row" style="gap: 6px">
    <i class="dot" :class="`dot--${props.state}`"></i>
    <span v-if="props.label" class="st">{{ TEXT[props.state] || props.state }}</span>
  </span>
</template>

<style scoped>
.st {
  font-size: 11px;
  color: var(--text-dim);
  letter-spacing: 0.05em;
}
</style>
