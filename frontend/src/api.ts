export type ApiProblem = { status: number; detail: string | unknown }
export type ApiRequestOptions = RequestInit & { timeoutMs?: number }

const API_ROOT = '/api/v1'
export const AUTH_EXPIRED_EVENT = 'automation-center:auth-expired'

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

export async function api<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  const { timeoutMs = 0, ...requestOptions } = options
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
  const controller = timeoutMs > 0 ? new AbortController() : undefined
  const timeout = controller ? window.setTimeout(() => controller.abort(), timeoutMs) : undefined
  let signal = requestOptions.signal
  if (controller) signal = signal ? AbortSignal.any([signal, controller.signal]) : controller.signal
  let response: Response
  try {
    response = await fetch(`${API_ROOT}${path}`, { ...requestOptions, headers, signal, credentials: 'include' })
  } finally {
    if (timeout !== undefined) window.clearTimeout(timeout)
  }
  if (response.status === 204) return undefined as T
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    const error = new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || data)) as Error & ApiProblem
    error.status = response.status
    error.detail = data.detail
    if (response.status === 401) {
      clearCsrfToken()
      window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT))
    }
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
  if (status === 'PARTIAL_FAILED' || status === 'SKIPPED_OFFLINE') return 'warning'
  if (status === 'RUNNING') return 'primary'
  if (status === 'WAITING') return 'warning'
  return 'info'
}
