export type ApiProblem = { status: number; detail: string | unknown }

const API_ROOT = '/api/v1'

export function setCsrfToken(token: string) {
  // Session Cookie 为 HttpOnly，CSRF Token 则只保存在当前标签页并显式放入写请求头。
  sessionStorage.setItem('automation-center-csrf', token)
}

export function clearCsrfToken() {
  sessionStorage.removeItem('automation-center-csrf')
}

export function getCsrfToken() {
  return sessionStorage.getItem('automation-center-csrf') || ''
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (options.body && !(options.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const method = (options.method || 'GET').toUpperCase()
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    // 所有跨模块写操作统一注入 CSRF，页面组件不应各自复制这段安全逻辑。
    const csrf = getCsrfToken()
    if (csrf) headers.set('X-CSRF-Token', csrf)
  }
  const response = await fetch(`${API_ROOT}${path}`, { ...options, headers, credentials: 'include' })
  if (response.status === 204) return undefined as T
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    const error = new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || data)) as Error & ApiProblem
    error.status = response.status
    error.detail = data.detail
    throw error
  }
  return data as T
}

export function formatTime(value?: string | null) {
  if (!value) return '—'
  // API/数据库使用 UTC；显示层统一转换为项目锁定的 Asia/Shanghai。
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', dateStyle: 'short', timeStyle: 'medium' }).format(new Date(value))
}

export function statusType(status: string) {
  if (status === 'SUCCESS' || status === 'ONLINE') return 'success'
  if (status === 'FAILED' || status === 'OFFLINE') return 'danger'
  if (status === 'RUNNING') return 'primary'
  if (status === 'WAITING') return 'warning'
  return 'info'
}
