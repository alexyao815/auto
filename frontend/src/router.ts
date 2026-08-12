import { createRouter, createWebHistory } from 'vue-router'
import { api } from './api'
import LoginView from './views/LoginView.vue'
import AppLayout from './components/AppLayout.vue'

const DashboardView = () => import('./views/DashboardView.vue')
const NodesView = () => import('./views/NodesView.vue')
const PackagesView = () => import('./views/PackagesView.vue')
const TasksView = () => import('./views/TasksView.vue')
const TaskCreateView = () => import('./views/TaskCreateView.vue')
const TaskDetailView = () => import('./views/TaskDetailView.vue')
const SettingsView = () => import('./views/SettingsView.vue')
const AuditView = () => import('./views/AuditView.vue')

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', component: LoginView },
    {
      path: '/', component: AppLayout, meta: { auth: true }, children: [
        { path: '', redirect: '/dashboard' },
        { path: 'dashboard', component: DashboardView },
        { path: 'nodes', component: NodesView },
        { path: 'packages', component: PackagesView },
        { path: 'tasks', component: TasksView },
        { path: 'tasks/create', component: TaskCreateView },
        { path: 'tasks/:id', component: TaskDetailView },
        { path: 'settings', component: SettingsView },
        { path: 'audit', component: AuditView },
      ],
    },
  ],
})

router.beforeEach(async (to) => {
  if (!to.matched.some((record) => record.meta.auth)) return true
  // 路由进入前以服务端 Session 为准，不能只依赖前端缓存推断登录状态。
  try {
    await api('/auth/me')
    return true
  } catch {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
})

export default router
