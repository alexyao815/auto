import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessageBox } from 'element-plus'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../src/api'
import NodesView from '../src/views/NodesView.vue'
import SettingsView from '../src/views/SettingsView.vue'
import TaskCreateView from '../src/views/TaskCreateView.vue'

vi.mock('../src/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api')>()
  return { ...actual, api: vi.fn() }
})

const node = {
  id: 'node-1',
  hostname: 'node-1',
  management_ip: '192.0.2.1',
  online_status: 'ONLINE',
  enabled: true,
  roles: ['compute', '人工-标签'],
  role_details: [
    { role: 'compute', sources: ['auto'] },
    { role: '人工-标签', sources: ['manual'] },
  ],
}

describe('role management pages', () => {
  beforeEach(() => {
    vi.mocked(api).mockReset()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('shows role sources and sends only one detection request while the first is pending', async () => {
    let finishDetection: ((value: any) => void) | undefined
    vi.mocked(api).mockImplementation((path, options) => {
      if (path === '/nodes') return Promise.resolve([node]) as any
      if (path === '/nodes/pending') return Promise.resolve([]) as any
      if (path.startsWith('/nodes/role-detection-jobs?')) return Promise.resolve([]) as any
      if (path === '/nodes/role-detection-jobs' && options?.method === 'POST') {
        return new Promise(resolve => { finishDetection = resolve }) as any
      }
      return Promise.resolve({}) as any
    })

    const wrapper = mount(NodesView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(wrapper.text()).toContain('compute · auto')
    expect(wrapper.text()).toContain('人工-标签 · manual')
    expect(wrapper.findAll('button').some(button => button.text().trim() === '删除')).toBe(false)
    const detectButton = wrapper.findAll('button').find(button => button.text().includes('自动判断角色'))!
    await detectButton.trigger('click')
    await detectButton.trigger('click')
    expect(vi.mocked(api).mock.calls.filter(call => call[0] === '/nodes/role-detection-jobs' && call[1]?.method === 'POST')).toHaveLength(1)

    finishDetection?.({
      id: 'job-1', status: 'WAITING', total_node_count: 1, target_node_count: 1,
      success_count: 0, failed_count: 0, skipped_count: 0, created_at: new Date().toISOString(), results: [],
    })
    await flushPromises()
    wrapper.unmount()
  })

  it('offers deletion only for offline nodes and calls the protected delete API once', async () => {
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as any)
    vi.mocked(api).mockImplementation((path, options) => {
      if (path === '/nodes') return Promise.resolve([{ ...node, online_status: 'OFFLINE' }]) as any
      if (path === '/nodes/pending') return Promise.resolve([]) as any
      if (path.startsWith('/nodes/role-detection-jobs?')) return Promise.resolve([]) as any
      if (path === '/nodes/node-1' && options?.method === 'DELETE') return Promise.resolve(undefined) as any
      return Promise.resolve({}) as any
    })

    const wrapper = mount(NodesView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const deleteButton = wrapper.findAll('button').find(button => button.text().trim() === '删除')!
    await deleteButton.trigger('click')
    await flushPromises()

    expect(confirm).toHaveBeenCalledOnce()
    expect(vi.mocked(api).mock.calls.filter(call => call[0] === '/nodes/node-1' && call[1]?.method === 'DELETE')).toHaveLength(1)
    wrapper.unmount()
  })

  it('edits process rules and never writes the credential mask back', async () => {
    let savedPayload: any
    vi.mocked(api).mockImplementation((path, options) => {
      if (path === '/settings' && !options?.method) {
        return Promise.resolve({
          salt_api_url: 'http://127.0.0.1:8000', salt_api_username: 'automation-center',
          salt_api_credential: '********', salt_request_timeout: 30, default_step_timeout: 30,
          execution_log_retention_days: 7, node_status_check_interval: 60, max_upload_size: 1048576,
          role_detection_rules: [],
        }) as any
      }
      savedPayload = JSON.parse(String(options?.body))
      return Promise.resolve({ ...savedPayload, salt_api_credential: '********' }) as any
    })

    const wrapper = mount(SettingsView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const addButton = wrapper.findAll('button').find(button => button.text().includes('新增规则'))!
    await addButton.trigger('click')
    await wrapper.get('input[maxlength="64"]').setValue('计算-节点')
    await wrapper.get('input[maxlength="255"]').setValue('nova-compute')
    const saveButton = wrapper.findAll('button').find(button => button.text().includes('保存设置'))!
    await saveButton.trigger('click')
    await flushPromises()
    expect(savedPayload.salt_api_credential).toBeUndefined()
    expect(savedPayload.role_detection_rules).toEqual([
      { role: '计算-节点', matcher_type: 'process', pattern: 'nova-compute', enabled: true },
    ])
  })

  it('uses current node labels as dynamic task role options', async () => {
    vi.mocked(api).mockImplementation(path => {
      if (path === '/packages') return Promise.resolve([]) as any
      if (path === '/nodes') return Promise.resolve([node]) as any
      return Promise.resolve({}) as any
    })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/tasks/create', component: TaskCreateView }],
    })
    await router.push('/tasks/create')
    await router.isReady()
    const wrapper = mount(TaskCreateView, { global: { plugins: [router, ElementPlus] } })
    await flushPromises()
    const optionValues = wrapper.findAllComponents({ name: 'ElOption' }).map(option => option.props('value'))
    expect(optionValues).toContain('compute')
    expect(optionValues).toContain('人工-标签')
  })
})
