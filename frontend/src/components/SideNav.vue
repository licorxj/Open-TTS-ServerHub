<script setup>
import { useRouter } from 'vue-router'
import { navItems } from '../router'
import { useHubStore } from '../stores/hub'

const router = useRouter()
const hub = useHubStore()
</script>

<template>
  <nav class="nav">
    <router-link
      v-for="(it, i) in navItems"
      :key="it.path"
      class="nav__item"
      :to="it.path"
      :style="{ '--d': i * 60 + 'ms' }"
    >
      <span class="nav__bar"></span>
      <span class="nav__label">{{ it.label }}</span>
      <span class="nav__code">{{ it.code }}</span>
    </router-link>

    <div class="spacer"></div>

    <div class="nav__foot">
      <div class="hud">HUB PORT</div>
      <div class="num" style="color: var(--amber)">{{ hub.status.hub?.port || 5199 }}</div>
      <div class="hud" style="margin-top: 8px">REGISTRY</div>
      <div class="mono-sm" style="color: var(--text-dim)">config/tts_hub.yaml</div>
      <div class="nav__dot" :style="{ background: hub.online ? 'var(--cyan)' : 'var(--red)' }"></div>
    </div>
  </nav>
</template>

<style scoped>
.nav {
  width: 176px;
  flex: none;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 14px 10px;
  border-right: 1px solid var(--line);
  background: linear-gradient(180deg, rgba(21, 26, 32, 0.6), transparent);
}

.nav__item {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 11px 10px 11px 14px;
  color: var(--text-dim);
  text-decoration: none;
  border: 1px solid transparent;
  transition: all 0.18s var(--ease);
}

.nav__bar {
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  width: 2px;
  height: 0;
  background: var(--amber);
  transition: height 0.2s var(--ease);
}

.nav__item:hover {
  color: var(--text);
  background: rgba(255, 255, 255, 0.025);
}

.nav__item.router-link-active {
  color: var(--amber-2);
  background: var(--amber-soft);
  border-color: var(--line);
}

.nav__item.router-link-active .nav__bar {
  height: 70%;
}

.nav__label {
  font-size: 13.5px;
  letter-spacing: 0.04em;
}

.nav__code {
  font-size: 10px;
  letter-spacing: 0.12em;
  color: var(--text-mute);
}

.nav__foot {
  position: relative;
  padding: 14px 10px;
  border-top: 1px solid var(--line);
  font-size: 12.5px;
}

.nav__dot {
  position: absolute;
  right: 10px;
  top: 14px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  box-shadow: 0 0 6px currentColor;
}
</style>
