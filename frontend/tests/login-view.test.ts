import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import LoginView from '../src/views/LoginView.vue'
import { api, setCsrfToken } from '../src/api'

vi.mock('../src/api', () => ({
  api: vi.fn(),
  setCsrfToken: vi.fn(),
}))

describe('LoginView', () => {
  beforeEach(() => {
    vi.mocked(api).mockReset()
    vi.mocked(setCsrfToken).mockReset()
  })

  it('submits the fixed account and keeps the csrf token for later writes', async () => {
    vi.mocked(api).mockResolvedValue({ csrf_token: 'csrf-1' })
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
    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(api).toHaveBeenCalledWith('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username: 'admin', password: 'correct-password' }),
    })
    expect(setCsrfToken).toHaveBeenCalledWith('csrf-1')
    expect(router.currentRoute.value.fullPath).toBe('/dashboard')
  })
})
