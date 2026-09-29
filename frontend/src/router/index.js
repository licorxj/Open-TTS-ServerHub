import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/', redirect: '/console' },
  {
    path: '/console',
    name: 'console',
    component: () => import('../views/ConsoleView.vue'),
    meta: { title: '总控台', icon: 'console' },
  },
  {
    path: '/studio',
    name: 'studio',
    component: () => import('../views/StudioView.vue'),
    meta: { title: '合成工作台', icon: 'studio' },
  },
  {
    path: '/config',
    name: 'config',
    component: () => import('../views/ConfigView.vue'),
    meta: { title: '配置编辑器', icon: 'config' },
  },
  {
    path: '/tasks',
    name: 'tasks',
    component: () => import('../views/TasksView.vue'),
    meta: { title: '任务中心', icon: 'tasks' },
  },
]

export const navItems = [
  { path: '/console', label: '总控台', code: 'CTRL' },
  { path: '/studio', label: '合成工作台', code: 'STUDIO' },
  { path: '/config', label: '配置编辑器', code: 'CONFIG' },
  { path: '/tasks', label: '任务中心', code: 'TASKS' },
]

export default createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
})
