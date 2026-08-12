import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../src/api'
import router from '../src/router'
import { pinia } from '../src/stores'
import { useAuthStore } from '../src/stores/auth'

vi.mock('../src/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api')>()
  return { ...actual, api: vi.fn() }
})

describe('authenticated navigation', () => {
  beforeEach(async () => {
    vi.mocked(api).mockReset()
    useAuthStore(pinia).$reset()
    await router.replace('/login')
  })

  it('checks the server session once and reuses it across page navigation', async () => {
    vi.mocked(api).mockResolvedValue({ username: 'admin' })
    await router.push('/dashboard')
    await router.push('/nodes')
    await router.push('/packages')
    await router.push('/tasks')
    expect(api).toHaveBeenCalledTimes(1)
    expect(api).toHaveBeenCalledWith('/auth/me')
  })
})
