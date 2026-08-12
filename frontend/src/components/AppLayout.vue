<script setup lang="ts">
import { useRoute, useRouter } from 'vue-router'
import { api, clearCsrfToken } from '../api'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const nav = [
  ['/dashboard', '运行总览'], ['/nodes', '节点中心'], ['/packages', '维护包中心'],
  ['/tasks', '任务中心'], ['/settings', '系统设置'], ['/audit', '操作审计'],
]

async function logout() {
  try { await api('/auth/logout', { method: 'POST' }) } catch { /* session may already be gone */ }
  clearCsrfToken()
  auth.markLoggedOut()
  ElMessage.success('已退出登录')
  router.replace('/login')
}
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand"><span class="brand-mark">AC</span><div><strong>自动化维护中心</strong><small>Automation Center V1</small></div></div>
      <nav>
        <router-link v-for="item in nav" :key="item[0]" :to="item[0]" :class="{ active: route.path.startsWith(item[0]) }">{{ item[1] }}</router-link>
      </nav>
      <div class="sidebar-foot"><span class="health-dot"></span> 单实例运行中</div>
    </aside>
    <main class="main-panel">
      <header class="topbar"><div><span class="environment">PRODUCTION</span><span class="muted">Asia/Shanghai</span></div><el-button text @click="logout">退出登录</el-button></header>
      <section class="page"><router-view /></section>
    </main>
  </div>
</template>
