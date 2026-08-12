import { createRouter, createWebHistory } from 'vue-router'
import LoginView from './views/LoginView.vue'
import AppLayout from './components/AppLayout.vue'
import { pinia } from './stores'
import { useAuthStore } from './stores/auth'

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
  // 首次进入时由服务端确认 Session；同标签页内后续导航复用结果，业务 API 的
  // 401 仍会立即清除缓存并跳转登录页，不能只依赖缓存维持失效会话。
  const authenticated = await useAuthStore(pinia).ensureSession()
  return authenticated || { path: '/login', query: { redirect: to.fullPath } }
})

export default router
