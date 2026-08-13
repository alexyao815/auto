<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { api } from '../api'
import { errorText, showApiError } from '../feedback'

type RoleRule = { role: string; matcher_type: 'process'; pattern: string; enabled: boolean }

const form = reactive<any>({ role_detection_rules: [] as RoleRule[] })
const loading = ref(false)
const saving = ref(false)
const credential = ref('')
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    const values = await api<any>('/settings')
    Object.assign(form, values)
    form.role_detection_rules ||= []
  } catch (reason) {
    error.value = errorText(reason, '系统设置加载失败')
  } finally {
    loading.value = false
  }
}

function addRule() {
  form.role_detection_rules.push({ role: '', matcher_type: 'process', pattern: '', enabled: true })
}

function removeRule(index: number) {
  form.role_detection_rules.splice(index, 1)
}

async function save() {
  if (saving.value) return
  saving.value = true
  try {
    const payload = { ...form, role_detection_rules: form.role_detection_rules.map((rule: RoleRule) => ({ ...rule })) }
    // 掩码只表示数据库已有凭据，绝不能作为新 credential 回写。
    delete payload.salt_api_credential
    if (credential.value) payload.salt_api_credential = credential.value
    Object.assign(form, await api('/settings', { method: 'PATCH', body: JSON.stringify(payload) }))
    credential.value = ''
    ElMessage.success('系统设置已保存')
  } catch (reason) {
    showApiError(reason, '系统设置保存失败')
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page-head"><div><h1>系统设置</h1><p>管理 Automation Center 运行参数与进程角色规则</p></div></div>
  <el-alert v-if="error" :title="error" type="error" show-icon :closable="false"><el-button link @click="load">重试</el-button></el-alert>
  <div v-loading="loading">
    <div class="two-column">
      <div class="panel">
        <div class="panel-title"><h2>Salt Settings</h2></div>
        <el-form label-position="top">
          <el-form-item label="salt_api_url"><el-input v-model="form.salt_api_url" /></el-form-item>
          <el-form-item label="salt_api_username"><el-input v-model="form.salt_api_username" /></el-form-item>
          <el-form-item label="salt_api_credential"><el-input v-model="credential" type="password" :placeholder="form.salt_api_credential || '输入新凭据'" /></el-form-item>
          <el-form-item label="request_timeout (1–300 秒)"><el-input-number v-model="form.salt_request_timeout" :min="1" :max="300" /></el-form-item>
        </el-form>
      </div>
      <div class="panel">
        <div class="panel-title"><h2>Execution & Node</h2></div>
        <el-form label-position="top">
          <el-form-item label="default_step_timeout (1–86400 秒)"><el-input-number v-model="form.default_step_timeout" :min="1" :max="86400" /></el-form-item>
          <el-form-item label="execution_log_retention_days (1–365 天)"><el-input-number v-model="form.execution_log_retention_days" :min="1" :max="365" /></el-form-item>
          <el-form-item label="node_status_check_interval (5–3600 秒)"><el-input-number v-model="form.node_status_check_interval" :min="5" :max="3600" /></el-form-item>
          <el-form-item label="max_upload_size (bytes)"><el-input-number v-model="form.max_upload_size" :min="1048576" :max="10737418240" /></el-form-item>
        </el-form>
      </div>
    </div>

    <div class="panel">
      <div class="panel-title">
        <div><h2>进程角色规则</h2><span class="muted">区分大小写的字面包含匹配；每次自动识别只执行一次批量进程扫描</span></div>
        <el-button type="primary" link @click="addRule">新增规则</el-button>
      </div>
      <el-table :data="form.role_detection_rules" empty-text="暂无规则，自动角色识别将不可启动">
        <el-table-column label="角色" min-width="180"><template #default="scope"><el-input v-model="scope.row.role" maxlength="64" /></template></el-table-column>
        <el-table-column label="类型" width="130"><template #default><el-tag>process</el-tag></template></el-table-column>
        <el-table-column label="进程匹配文本" min-width="260"><template #default="scope"><el-input v-model="scope.row.pattern" maxlength="255" /></template></el-table-column>
        <el-table-column label="启用" width="100"><template #default="scope"><el-switch v-model="scope.row.enabled" /></template></el-table-column>
        <el-table-column label="操作" width="90"><template #default="scope"><el-button type="danger" link @click="removeRule(scope.$index)">删除</el-button></template></el-table-column>
      </el-table>
    </div>
    <div class="form-actions"><el-button type="primary" :loading="saving" :disabled="loading || saving" @click="save">保存设置</el-button></div>
  </div>
</template>
