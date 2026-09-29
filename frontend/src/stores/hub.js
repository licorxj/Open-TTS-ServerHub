import { defineStore } from 'pinia'
import { api } from '../api'

const LS_MODEL = 'tts-hub:current-model'

export const useHubStore = defineStore('hub', {
  state: () => ({
    engines: [],
    status: { active: [], active_count: 0, max_active: 1, available: [] },
    current: localStorage.getItem(LS_MODEL) || '',
    tasks: [], // 任务中心：{ id, engine, endpoint, createdAt, state, progress, result }
    ready: false,
    busy: false,
    busyText: '',
    lastError: '',
    online: true,
    _timer: null,
  }),

  getters: {
    activeMap: (s) => Object.fromEntries((s.status.active || []).map((a) => [a.name, a])),
    activeNames: (s) => (s.status.active || []).map((a) => a.name),
    engineByName: (s) => Object.fromEntries(s.engines.map((e) => [e.name, e])),
    currentEngine: (s) => s.engines.find((e) => e.name === s.current) || null,
    stateOf: (s) => (name) => s.activeMap[name]?.state || 'stopped',
    isActive: (s) => (name) => (s.status.active || []).some((a) => a.name === name),
    enabledEngines: (s) => s.engines.filter((e) => e.enabled),
  },

  actions: {
    setCurrent(name) {
      this.current = name
      localStorage.setItem(LS_MODEL, name)
    },

    async refreshEngines() {
      try {
        const r = await api.engines(false)
        this.engines = r.engines || []
        this.online = true
        if (!this.current || !this.engineByName[this.current]) {
          const first = this.enabledEngines[0]
          if (first) this.setCurrent(first.name)
        }
      } catch (e) {
        this.online = false
        this.lastError = e.message
      }
    },

    async refreshStatus() {
      try {
        this.status = await api.status()
        this.online = true
      } catch (e) {
        this.online = false
        this.lastError = e.message
      }
    },

    async refreshAll() {
      await Promise.all([this.refreshEngines(), this.refreshStatus()])
      this.ready = true
    },

    /** 启动状态轮询；有引擎正在拉起时自动提速 */
    startPolling() {
      if (this._timer) return
      const tick = async () => {
        await this.refreshStatus()
        const busyLoading = (this.status.active || []).some((a) => a.state === 'starting')
        this._timer = setTimeout(tick, busyLoading ? 1500 : 5000)
      }
      this._timer = setTimeout(tick, 5000)
    },

    stopPolling() {
      if (this._timer) clearTimeout(this._timer)
      this._timer = null
    },

    async withBusy(text, fn) {
      this.busy = true
      this.busyText = text
      this.lastError = ''
      try {
        return await fn()
      } catch (e) {
        this.lastError = e.message
        throw e
      } finally {
        this.busy = false
        this.busyText = ''
        await this.refreshStatus()
      }
    },

    switchTo(name) {
      this.setCurrent(name)
      return this.withBusy(`正在拉起 ${name}（首次需下载/加载模型，可能数分钟）`, () =>
        api.switchTo(name),
      )
    },
    stop(name) {
      return this.withBusy(`正在停止 ${name}`, () => api.stop(name, true))
    },
    restart(name) {
      return this.withBusy(`正在重启 ${name}`, () => api.restart(name))
    },
    unloadAll() {
      return this.withBusy('正在卸载全部引擎', () => api.unload(true))
    },

    // ---------- 任务中心 ----------
    addTask(task) {
      if (this.tasks.some((t) => t.id === task.id && t.engine === task.engine)) return
      this.tasks.unshift({
        state: 'running',
        progress: 0,
        createdAt: Date.now(),
        result: null,
        ...task,
      })
      this.persistTasks()
    },
    removeTask(idx) {
      this.tasks.splice(idx, 1)
      this.persistTasks()
    },
    persistTasks() {
      try {
        localStorage.setItem('tts-hub:tasks', JSON.stringify(this.tasks.slice(0, 40)))
      } catch {
        /* 忽略 */
      }
    },
    restoreTasks() {
      try {
        const raw = localStorage.getItem('tts-hub:tasks')
        if (raw) this.tasks = JSON.parse(raw)
      } catch {
        this.tasks = []
      }
    },
  },
})
