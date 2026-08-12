import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../src/api'
import DashboardView from '../src/views/DashboardView.vue'

vi.mock('../src/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api')>()
  return { ...actual, api: vi.fn() }
})

describe('page request feedback', () => {
  beforeEach(() => { vi.mocked(api).mockReset() })

  it('shows a visible error and lets the operator retry', async () => {
    vi.mocked(api)
      .mockRejectedValueOnce(new Error('数据库正在处理其他写操作，请稍后重试'))
      .mockRejectedValueOnce(new Error('数据库正在处理其他写操作，请稍后重试'))
      .mockResolvedValueOnce({ nodes: {}, packages: 0, tasks: {} })
      .mockResolvedValueOnce([])
    const wrapper = mount(DashboardView, { global: { plugins: [ElementPlus], stubs: ['router-link'] } })
    await flushPromises()
    expect(wrapper.text()).toContain('数据库正在处理其他写操作，请稍后重试')
    await wrapper.get('.el-alert button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('数据库正在处理其他写操作，请稍后重试')
    expect(api).toHaveBeenCalledTimes(4)
  })
})
