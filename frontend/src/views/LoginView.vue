<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api, setCsrfToken } from '../api'

const router = useRouter(); const route = useRoute(); const loading = ref(false)
const form = reactive({ username: '', password: '' })
async function submit() {
  loading.value = true
  try {
    const result = await api<{ csrf_token: string }>('/auth/login', { method: 'POST', body: JSON.stringify(form) })
    setCsrfToken(result.csrf_token)
    ElMessage.success('登录成功')
    router.replace(String(route.query.redirect || '/dashboard'))
  } catch (error) { ElMessage.error((error as Error).message) } finally { loading.value = false }
}
</script>
<template>
  <div class="login-page">
    <section class="login-hero"><span class="environment">AUTOMATION CENTER V1</span><h1>让每一次云平台维护，都清晰、可靠、可追溯。</h1><p>集中管理 Salt 节点、维护包和执行任务。节点级 FIFO 调度，完整状态快照与实时日志，让已验证的修复安全抵达目标节点。</p></section>
    <section class="login-card-wrap"><el-form class="login-card" :model="form" @submit.prevent="submit"><h2>登录维护中心</h2><p>使用平台固定运维账号继续</p><el-form-item label="账号"><el-input v-model="form.username" autocomplete="username" /></el-form-item><el-form-item label="密码"><el-input v-model="form.password" type="password" autocomplete="current-password" show-password @keyup.enter="submit" /></el-form-item><el-button type="primary" size="large" :loading="loading" @click="submit">安全登录</el-button></el-form></section>
  </div>
</template>

