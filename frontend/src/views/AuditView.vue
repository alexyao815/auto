<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, formatTime } from '../api'
const logs = ref<any[]>([])
onMounted(async () => { logs.value = await api('/audit-logs?limit=500') })
</script>
<template><div class="page-head"><div><h1>操作审计</h1><p>共享账号场景下记录来源 IP、操作对象与时间</p></div></div><div class="panel"><el-table :data="logs" empty-text="暂无审计记录"><el-table-column label="时间" min-width="175"><template #default="s">{{ formatTime(s.row.created_at) }}</template></el-table-column><el-table-column prop="source_ip" label="来源 IP" /><el-table-column prop="operation" label="操作" /><el-table-column prop="object_type" label="对象类型" /><el-table-column prop="object_id" label="对象 ID" min-width="240" /><el-table-column label="详情" min-width="260"><template #default="s"><span class="mono">{{ JSON.stringify(s.row.detail) }}</span></template></el-table-column></el-table></div></template>
