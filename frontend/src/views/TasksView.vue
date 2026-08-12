<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, formatTime, statusType } from '../api'
const tasks = ref<any[]>([])
onMounted(async () => { tasks.value = await api('/tasks') })
</script>
<template><div class="page-head"><div><h1>任务中心</h1><p>查看节点级执行状态、失败原因与历史结果</p></div><router-link to="/tasks/create"><el-button type="primary">创建维护任务</el-button></router-link></div><div class="panel"><el-table :data="tasks" empty-text="暂无任务"><el-table-column label="Task ID" min-width="260"><template #default="s"><router-link :to="`/tasks/${s.row.id}`" class="mono">{{ s.row.id }}</router-link></template></el-table-column><el-table-column prop="package_name" label="维护包" /><el-table-column label="Revision" width="85"><template #default="s">v{{ s.row.package_revision }}</template></el-table-column><el-table-column label="状态"><template #default="s"><el-tag :type="statusType(s.row.status)">{{ s.row.status }}</el-tag></template></el-table-column><el-table-column label="成功/总数"><template #default="s">{{ s.row.success_count }}/{{ s.row.target_node_count }}</template></el-table-column><el-table-column label="创建时间" min-width="170"><template #default="s">{{ formatTime(s.row.created_at) }}</template></el-table-column></el-table></div></template>
