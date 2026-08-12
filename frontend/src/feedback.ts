import { ElMessage } from 'element-plus'

import type { ApiProblem } from './api'

export function errorText(error: unknown, fallback = '请求失败，请稍后重试') {
  const candidate = error as Partial<Error & ApiProblem>
  if (candidate.name === 'AbortError') return '请求超时，请检查服务状态后重试'
  if (candidate.status === 503) return candidate.message || '服务暂时繁忙，请稍后重试'
  return candidate.message || fallback
}

export function showApiError(error: unknown, fallback?: string) {
  ElMessage.error(errorText(error, fallback))
}

export function isDialogCancelled(error: unknown) {
  return error === 'cancel' || error === 'close'
}
