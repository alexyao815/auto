import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import LoginView from '../src/views/LoginView.vue'
import { api, setCsrfToken } from '../src/api'
import { useAuthStore } from '../src/stores/auth'

vi.mock('../src/api', () => ({
  api: vi.fn(),
  setCsrfToken: vi.fn(),
}))

describe('LoginView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(api).mockReset()
    vi.mocked(setCsrfToken).mockReset()
  })

  it('submits the fixed account and keeps the csrf token for later writes', async () => {
    vi.mocked(api).mockResolvedValue({ username: 'admin', csrf_token: 'csrf-1' })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/login', component: LoginView },
        { path: '/dashboard', component: { template: '<div>dashboard</div>' } },
      ],
    })
    await router.push('/login')
    await router.isReady()
    const wrapper = mount(LoginView, { global: { plugins: [router, ElementPlus] } })

    const inputs = wrapper.findAll('input')
    await inputs[0].setValue('admin')
    await inputs[1].setValue('correct-password')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(api).toHaveBeenCalledWith('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username: 'admin', password: 'correct-password' }),
      timeoutMs: 10_000,
    })
    expect(setCsrfToken).toHaveBeenCalledWith('csrf-1')
    expect(useAuthStore().username).toBe('admin')
    expect(router.currentRoute.value.fullPath).toBe('/dashboard')
  })

  it('ignores duplicate submits while the first login is pending', async () => {
    let finish!: (value: { username: string; csrf_token: string }) => void
    vi.mocked(api).mockReturnValue(new Promise((resolve) => { finish = resolve }))
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/login', component: LoginView }, { path: '/dashboard', component: { template: '<div>dashboard</div>' } }],
    })
    await router.push('/login'); await router.isReady()
    const wrapper = mount(LoginView, { global: { plugins: [createPinia(), router, ElementPlus] } })
    await wrapper.findAll('input')[0].setValue('admin')
    await wrapper.findAll('input')[1].setValue('correct-password')
    void wrapper.get('form').trigger('submit')
    void wrapper.get('form').trigger('submit')
    expect(api).toHaveBeenCalledTimes(1)
    finish({ username: 'admin', csrf_token: 'csrf-1' })
    await flushPromises()
  })
})
