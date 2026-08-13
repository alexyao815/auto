<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { api, formatTime, statusType } from '../api'
import { errorText, isDialogCancelled, showApiError } from '../feedback'

type RoleDetail = { role: string; sources: string[] }
type NodeItem = {
  id: string
  hostname: string
  management_ip?: string | null
  online_status: string
  enabled: boolean
  roles: string[]
  role_details?: RoleDetail[]
  last_check_time?: string | null
}
type DetectionResult = {
  node_id_snapshot: string
  hostname_snapshot: string
  status: string
  matched_roles: string[]
  added_roles: string[]
  failure_reason?: string | null
}
type DetectionJob = {
  id: string
  status: string
  total_node_count: number
  target_node_count: number
  success_count: number
  failed_count: number
  skipped_count: number
  created_at: string
  finished_at?: string | null
  results?: DetectionResult[]
}

const nodes = ref<NodeItem[]>([])
const pending = ref<{ id: string }[]>([])
const jobs = ref<DetectionJob[]>([])
const selectedJob = ref<DetectionJob>()
const loading = ref(false)
const refreshing = ref(false)
const detecting = ref(false)
const actionId = ref('')
const error = ref('')
const roleDialogVisible = ref(false)
const editingNode = ref<NodeItem>()
const editingRoles = ref<string[]>([])
let pollTimer: number | undefined

const activeJob = computed(() => jobs.value.find(job => ['WAITING', 'RUNNING'].includes(job.status)))
const detectionProgress = computed(() => {
  const job = selectedJob.value
  if (!job || job.target_node_count === 0) return 100
  return Math.round(((job.success_count + job.failed_count) / job.target_node_count) * 100)
})

function stopPolling() {
  if (pollTimer !== undefined) window.clearInterval(pollTimer)
  pollTimer = undefined
}

function upsertJob(job: DetectionJob) {
  const existing = jobs.value.findIndex(item => item.id === job.id)
  if (existing >= 0) jobs.value.splice(existing, 1, job)
  else jobs.value.unshift(job)
}

async function loadNodes() {
  ;[nodes.value, pending.value] = await Promise.all([
    api<NodeItem[]>('/nodes'),
    api<{ id: string }[]>('/nodes/pending'),
  ])
}

async function pollJob(jobId: string) {
  try {
    const job = await api<DetectionJob>(`/nodes/role-detection-jobs/${encodeURIComponent(jobId)}`)
    selectedJob.value = job
    upsertJob(job)
    if (!['WAITING', 'RUNNING'].includes(job.status)) {
      stopPolling()
      await loadNodes()
      ElMessage.success('自动角色识别已完成')
    }
  } catch (reason) {
    stopPolling()
    showApiError(reason, '角色识别状态查询失败')
  }
}

function startPolling(jobId: string) {
  stopPolling()
  pollTimer = window.setInterval(() => void pollJob(jobId), 1000)
}

async function loadJobs() {
  jobs.value = await api<DetectionJob[]>('/nodes/role-detection-jobs?limit=20')
  if (!jobs.value.length) {
    selectedJob.value = undefined
    return
  }
  const job = await api<DetectionJob>(`/nodes/role-detection-jobs/${encodeURIComponent(jobs.value[0].id)}`)
  selectedJob.value = job
  upsertJob(job)
  if (['WAITING', 'RUNNING'].includes(job.status)) startPolling(job.id)
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    await Promise.all([loadNodes(), loadJobs()])
  } catch (reason) {
    error.value = errorText(reason, '节点列表加载失败')
  } finally {
    loading.value = false
  }
}

async function refresh() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    nodes.value = await api<NodeItem[]>('/nodes/refresh', { method: 'POST' })
    ElMessage.success('节点在线状态已刷新')
  } catch (reason) {
    showApiError(reason, '节点探测失败')
  } finally {
    refreshing.value = false
  }
}

async function detectRoles() {
  if (detecting.value || activeJob.value) return
  detecting.value = true
  try {
    const job = await api<DetectionJob>('/nodes/role-detection-jobs', { method: 'POST' })
    selectedJob.value = job
    upsertJob(job)
    startPolling(job.id)
    ElMessage.success('自动角色识别任务已创建')
  } catch (reason) {
    showApiError(reason, '自动角色识别任务创建失败')
  } finally {
    detecting.value = false
  }
}

async function accept(id: string) {
  actionId.value = id
  try {
    await api(`/nodes/pending/${encodeURIComponent(id)}/accept`, { method: 'POST' })
    ElMessage.success(`已接受 ${id}`)
    await loadNodes()
  } catch (reason) {
    showApiError(reason, '接受节点失败')
  } finally {
    actionId.value = ''
  }
}

async function reject(id: string) {
  try {
    await ElMessageBox.confirm(`确认拒绝 Pending Key ${id}？`, '拒绝节点', { type: 'warning' })
    actionId.value = id
    await api(`/nodes/pending/${encodeURIComponent(id)}/reject`, { method: 'POST' })
    await loadNodes()
  } catch (reason) {
    if (!isDialogCancelled(reason)) showApiError(reason, '拒绝节点失败')
  } finally {
    actionId.value = ''
  }
}

async function toggle(row: NodeItem) {
  actionId.value = row.id
  try {
    await api(`/nodes/${encodeURIComponent(row.id)}`, {
      method: 'PATCH',
      body: JSON.stringify({ enabled: !row.enabled }),
    })
    await loadNodes()
  } catch (reason) {
    showApiError(reason, '更新节点状态失败')
  } finally {
    actionId.value = ''
  }
}

async function removeNode(row: NodeItem) {
  if (row.online_status !== 'OFFLINE' || actionId.value) return
  actionId.value = row.id
  try {
    await ElMessageBox.confirm(
      `确认删除离线节点 ${row.hostname}？仅删除当前节点记录，历史任务继续保留；如果 Salt Minion Key 仍存在，后续探测可能重新发现该节点。`,
      '删除节点',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
    await api(`/nodes/${encodeURIComponent(row.id)}`, { method: 'DELETE' })
    // DELETE 已成功后直接更新当前表格，避免额外 Salt Key 查询失败造成
    // “节点实际已删除但页面提示失败”的误导。
    nodes.value = nodes.value.filter(node => node.id !== row.id)
    ElMessage.success('离线节点已删除')
  } catch (reason) {
    if (!isDialogCancelled(reason)) showApiError(reason, '删除节点失败')
  } finally {
    actionId.value = ''
  }
}

function editRoles(row: NodeItem) {
  editingNode.value = row
  editingRoles.value = [...row.roles]
  roleDialogVisible.value = true
}

async function saveRoles() {
  if (!editingNode.value || actionId.value) return
  actionId.value = editingNode.value.id
  try {
    await api(`/nodes/${encodeURIComponent(editingNode.value.id)}`, {
      method: 'PATCH',
      body: JSON.stringify({ roles: editingRoles.value }),
    })
    roleDialogVisible.value = false
    await loadNodes()
    ElMessage.success('节点角色已更新')
  } catch (reason) {
    showApiError(reason, '更新节点角色失败')
  } finally {
    actionId.value = ''
  }
}

function roleDetails(row: NodeItem): RoleDetail[] {
  return row.role_details || row.roles.map(role => ({ role, sources: [] }))
}

onMounted(load)
onBeforeUnmount(stopPolling)
</script>

<template>
  <div class="page-head">
    <div>
      <h1>节点中心</h1>
      <p>在线探测与角色识别相互独立，自动识别只补充缺失标签</p>
    </div>
    <div>
      <el-button :loading="refreshing" :disabled="loading || refreshing" @click="refresh">立即探测</el-button>
      <el-button type="primary" :loading="detecting" :disabled="loading || detecting || Boolean(activeJob)" @click="detectRoles">
        自动判断角色
      </el-button>
    </div>
  </div>

  <el-alert v-if="error" :title="error" type="error" show-icon :closable="false">
    <el-button link @click="load">重试</el-button>
  </el-alert>

  <div v-loading="loading">
    <div v-if="selectedJob" class="panel">
      <div class="panel-title">
        <h2>最近角色识别任务</h2>
        <el-tag :type="statusType(selectedJob.status)">{{ selectedJob.status }}</el-tag>
      </div>
      <el-progress :percentage="detectionProgress" :status="selectedJob.status === 'FAILED' ? 'exception' : undefined" />
      <p class="muted">
        目标 {{ selectedJob.target_node_count }}，成功 {{ selectedJob.success_count }}，失败 {{ selectedJob.failed_count }}，离线跳过 {{ selectedJob.skipped_count }}；创建于 {{ formatTime(selectedJob.created_at) }}
      </p>
      <el-table v-if="selectedJob.results?.length" :data="selectedJob.results" size="small">
        <el-table-column prop="hostname_snapshot" label="节点" />
        <el-table-column label="状态" width="150">
          <template #default="scope"><el-tag :type="statusType(scope.row.status)" size="small">{{ scope.row.status }}</el-tag></template>
        </el-table-column>
        <el-table-column label="匹配角色"><template #default="scope">{{ scope.row.matched_roles.join(', ') || '—' }}</template></el-table-column>
        <el-table-column label="新增角色"><template #default="scope">{{ scope.row.added_roles.join(', ') || '—' }}</template></el-table-column>
        <el-table-column prop="failure_reason" label="失败原因" />
      </el-table>
    </div>

    <div v-if="pending.length" class="panel">
      <div class="panel-title"><h2>Pending Keys</h2><el-tag type="warning">{{ pending.length }} 待处理</el-tag></div>
      <el-table :data="pending">
        <el-table-column prop="id" label="Minion ID" />
        <el-table-column width="190">
          <template #default="scope">
            <el-button type="primary" link :loading="actionId === scope.row.id" @click="accept(scope.row.id)">接受</el-button>
            <el-button type="danger" link :disabled="Boolean(actionId)" @click="reject(scope.row.id)">拒绝</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div class="panel">
      <div class="panel-title"><h2>已接入节点</h2><span class="muted">Disabled 节点不可被任务选择，但仍参与角色识别</span></div>
      <el-table :data="nodes" empty-text="尚未接入节点">
        <el-table-column prop="hostname" label="Hostname" />
        <el-table-column prop="management_ip" label="管理 IP" />
        <el-table-column label="角色" min-width="240">
          <template #default="scope">
            <el-tag
              v-for="detail in roleDetails(scope.row)"
              :key="detail.role"
              :type="detail.sources.includes('manual') ? 'warning' : 'success'"
              size="small"
              style="margin-right: 5px"
            >{{ detail.role }} · {{ detail.sources.join('+') || 'unknown' }}</el-tag>
            <span v-if="!scope.row.roles.length">—</span>
          </template>
        </el-table-column>
        <el-table-column label="在线"><template #default="scope"><el-tag :type="statusType(scope.row.online_status)">{{ scope.row.online_status }}</el-tag></template></el-table-column>
        <el-table-column label="可用"><template #default="scope"><el-tag :type="scope.row.enabled ? 'success' : 'info'">{{ scope.row.enabled ? 'Enabled' : 'Disabled' }}</el-tag></template></el-table-column>
        <el-table-column label="最近探测" min-width="170"><template #default="scope">{{ formatTime(scope.row.last_check_time) }}</template></el-table-column>
        <el-table-column label="操作" width="260">
          <template #default="scope">
            <el-button link :disabled="Boolean(actionId)" @click="editRoles(scope.row)">编辑标签</el-button>
            <el-button link :loading="actionId === scope.row.id" :type="scope.row.enabled ? 'warning' : 'success'" @click="toggle(scope.row)">{{ scope.row.enabled ? '禁用' : '启用' }}</el-button>
            <el-button
              v-if="scope.row.online_status === 'OFFLINE'"
              link
              type="danger"
              :loading="actionId === scope.row.id"
              :disabled="Boolean(actionId) && actionId !== scope.row.id"
              @click="removeNode(scope.row)"
            >删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>

  <el-dialog v-model="roleDialogVisible" title="编辑节点标签" width="520px">
    <p class="muted">可输入中英文、数字、点、下划线或短横线。删除自动标签后，如果进程仍匹配，下次识别会重新添加。</p>
    <el-select v-model="editingRoles" multiple filterable allow-create default-first-option style="width: 100%" placeholder="输入标签后按 Enter">
      <el-option v-for="role in editingNode?.roles || []" :key="role" :label="role" :value="role" />
    </el-select>
    <template #footer>
      <el-button @click="roleDialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="actionId === editingNode?.id" @click="saveRoles">保存</el-button>
    </template>
  </el-dialog>
</template>
