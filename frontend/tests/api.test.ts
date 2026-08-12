import { afterEach, describe, expect, it, vi } from 'vitest'
import { AUTH_EXPIRED_EVENT, api, formatTime, getCsrfToken, setCsrfToken, statusType } from '../src/api'

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); sessionStorage.clear() })

describe('frontend utilities', () => {
  it('stores the csrf token in session storage', () => { setCsrfToken('token-1'); expect(getCsrfToken()).toBe('token-1') })
  it('maps task states to visual types', () => { expect(statusType('SUCCESS')).toBe('success'); expect(statusType('FAILED')).toBe('danger'); expect(statusType('WAITING')).toBe('warning') })
  it('renders absent time consistently', () => { expect(formatTime(null)).toBe('—') })
  it('returns structured 503 details to the page', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: '数据库正在处理其他写操作，请稍后重试' }), { status: 503, headers: { 'Content-Type': 'application/json' } })))
    await expect(api('/tasks')).rejects.toMatchObject({ status: 503, message: '数据库正在处理其他写操作，请稍后重试' })
  })
  it('clears csrf and emits an auth-expired event on 401', async () => {
    setCsrfToken('csrf')
    const listener = vi.fn(); window.addEventListener(AUTH_EXPIRED_EVENT, listener, { once: true })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: '未登录' }), { status: 401, headers: { 'Content-Type': 'application/json' } })))
    await expect(api('/auth/me')).rejects.toMatchObject({ status: 401 })
    expect(getCsrfToken()).toBe('')
    expect(listener).toHaveBeenCalledOnce()
  })
  it('aborts a login request after its explicit timeout', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn((_url, options: RequestInit) => new Promise((_resolve, reject) => {
      options.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
    })))
    const request = api('/auth/login', { method: 'POST', body: '{}', timeoutMs: 10 })
    const rejection = expect(request).rejects.toMatchObject({ name: 'AbortError' })
    await vi.advanceTimersByTimeAsync(10)
    await rejection
  })
})
