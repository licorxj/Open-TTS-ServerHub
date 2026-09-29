<script setup>
import { onMounted, onBeforeUnmount, computed } from 'vue'
import { useHubStore } from './stores/hub'
import TopBar from './components/TopBar.vue'
import SideNav from './components/SideNav.vue'

const hub = useHubStore()

onMounted(async () => {
  hub.restoreTasks()
  await hub.refreshAll()
  hub.startPolling()
})

onBeforeUnmount(() => hub.stopPolling())

const offline = computed(() => !hub.online)
</script>

<template>
  <div class="shell">
    <!-- 顶部状态栏 -->
    <TopBar />

    <div class="shell__main">
      <SideNav />

      <main class="shell__content">
        <div class="offline" v-if="offline">
          <span class="hud hud--amber">SIGNAL LOST</span>
          无法连接管家服务（{{ hub.lastError }}）—— 确认 <code>tts_hub_server.py</code> 已启动
        </div>
        <router-view v-slot="{ Component }">
          <transition name="fade" mode="out-in">
            <component :is="Component" class="rise" />
          </transition>
        </router-view>
      </main>
    </div>

    <!-- 全局忙碌遮罩：拉起/卸载引擎时 -->
    <div class="busy" v-if="hub.busy">
      <div class="busy__box panel panel--notch">
        <div class="busy__bar"><i></i></div>
        <div class="busy__text">{{ hub.busyText }}</div>
        <div class="hud">引擎加载动辄数十秒至数分钟，请耐心等待 · 可在总控台查看实时日志</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.shell {
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  height: 100%;
}

.shell__main {
  flex: 1;
  display: flex;
  min-height: 0;
}

.shell__content {
  flex: 1;
  min-width: 0;
  overflow: auto;
  padding: 18px 20px 28px;
}

.offline {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 16px;
  padding: 12px 16px;
  border: 1px solid var(--amber-line);
  background: var(--amber-soft);
  color: var(--amber-2);
  font-size: 12.5px;
  border-radius: var(--radius);
}

.offline code {
  color: var(--text);
}

.busy {
  position: fixed;
  inset: 0;
  z-index: 3000;
  display: grid;
  place-items: center;
  background: rgba(8, 9, 11, 0.72);
  backdrop-filter: blur(3px);
}

.busy__box {
  width: min(460px, 88vw);
  padding: 22px 24px 20px;
  background: var(--panel-2);
  border-color: var(--amber-line);
}

.busy__bar {
  height: 2px;
  background: var(--line);
  overflow: hidden;
  margin-bottom: 16px;
}

.busy__bar i {
  display: block;
  width: 34%;
  height: 100%;
  background: var(--amber);
  animation: slide 1.1s linear infinite;
}

@keyframes slide {
  from {
    transform: translateX(-100%);
  }
  to {
    transform: translateX(330%);
  }
}

.busy__text {
  font-family: var(--font-display);
  font-size: 14px;
  color: var(--amber-2);
  margin-bottom: 6px;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.18s var(--ease);
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
