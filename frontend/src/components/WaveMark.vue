<script setup>
// 声波 / VU 表风格的标识块
defineProps({ size: { type: Number, default: 22 } })
</script>

<template>
  <div class="mark" :style="{ height: size + 'px' }">
    <i v-for="n in 5" :key="n" :style="{ animationDelay: n * 0.12 + 's' }"></i>
  </div>
</template>

<style scoped>
.mark {
  display: flex;
  align-items: flex-end;
  gap: 2px;
  padding: 0 2px;
  border-left: 2px solid var(--amber);
}

.mark i {
  width: 2px;
  background: var(--amber);
  animation: vu 1.6s var(--ease) infinite;
}

.mark i:nth-child(1) { height: 30%; }
.mark i:nth-child(2) { height: 62%; }
.mark i:nth-child(3) { height: 100%; }
.mark i:nth-child(4) { height: 48%; }
.mark i:nth-child(5) { height: 74%; }

@keyframes vu {
  0%, 100% { transform: scaleY(0.42); }
  50% { transform: scaleY(1); }
}
</style>
