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
    meta: { title: '测试工作台', icon: 'studio' },
  },
  {
    path: '/voices',
    name: 'voices',
    component: () => import('../views/VoicesView.vue'),
    meta: { title: '音色库', icon: 'voices' },
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
  {
    path: '/requests',
    name: 'requests',
    component: () => import('../views/RequestsView.vue'),
    meta: { title: '请求记录', icon: 'requests' },
  },
  {
    path: '/manager',
    name: 'manager',
    component: () => import('../views/ManagerView.vue'),
    meta: { title: '后端管理', icon: 'manager' },
  },
  {
    path: '/skill',
    name: 'skill',
    component: () => import('../views/SkillView.vue'),
    meta: { title: 'Skill 安装', icon: 'skill' },
  },
]

export const navItems = [
  { path: '/console', label: '总控台', code: 'CTRL' },
  { path: '/studio', label: '测试工作台', code: 'STUDIO' },
  { path: '/voices', label: '音色库', code: 'VOICE' },
  { path: '/config', label: '配置编辑器', code: 'CONFIG' },
  { path: '/tasks', label: '任务中心', code: 'TASKS' },
  { path: '/requests', label: '请求记录', code: 'LOG' },
  { path: '/manager', label: '后端管理', code: 'MGR' },
  { path: '/skill', label: 'Skill 安装', code: 'SKILL' },
]

export default createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
})
