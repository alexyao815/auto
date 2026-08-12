<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, formatTime } from '../api'
import { errorText } from '../feedback'
const logs = ref<any[]>([]); const loading = ref(false); const error = ref('')
async function load() { loading.value = true; error.value = ''; try { logs.value = await api('/audit-logs?limit=500') } catch (reason) { error.value = errorText(reason, '审计记录加载失败') } finally { loading.value = false } }
onMounted(load)
</script>
<template><div class="page-head"><div><h1>操作审计</h1><p>共享账号场景下记录来源 IP、操作对象与时间</p></div><el-button :loading="loading" @click="load">刷新</el-button></div><el-alert v-if="error" :title="error" type="error" show-icon :closable="false"><el-button link @click="load">重试</el-button></el-alert><div class="panel" v-loading="loading"><el-table :data="logs" empty-text="暂无审计记录"><el-table-column label="时间" min-width="175"><template #default="s">{{ formatTime(s.row.created_at) }}</template></el-table-column><el-table-column prop="source_ip" label="来源 IP" /><el-table-column prop="operation" label="操作" /><el-table-column prop="object_type" label="对象类型" /><el-table-column prop="object_id" label="对象 ID" min-width="240" /><el-table-column label="详情" min-width="260"><template #default="s"><span class="mono">{{ JSON.stringify(s.row.detail) }}</span></template></el-table-column></el-table></div></template>
